import asyncio
import contextlib
import logging
import signal
import time
from typing import Any

from aiohttp import web

from stream_archive.config import AppConfig, get_config
from stream_archive.eventsub import EventSubClient
from stream_archive.http import build_shared_client
from stream_archive.kick_api import KickAPI
from stream_archive.kick_webhook import KickWebhook
from stream_archive.monitor import Monitor
from stream_archive.notifier import Notifier
from stream_archive.recorder import Recorder
from stream_archive.telegram import TelegramController
from stream_archive.twitch_api import TwitchAPI
from stream_archive.updater import UpdateChecker, _installed_app_version
from stream_archive.youtube_streamer import YouTubeStreamer

logger = logging.getLogger(__name__)

_shutdown_event: asyncio.Event | None = None

_HEALTH_HOST = "127.0.0.1"
_HEALTH_PORT = 9100

_READY = False  # flips True once recorder and API clients exist


def _setup_signal_handlers() -> None:
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    def handle_signal(signum: int, frame: Any) -> None:
        logger.info("[scheduler] Received signal %s, initiating shutdown...", signum)
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


async def _healthz(request: web.Request) -> web.Response:
    return web.Response(status=200, text="ok")


async def _readyz(request: web.Request) -> web.Response:
    """Readiness for orchestrators. 200 only after clients exist."""
    return web.Response(status=200 if _READY else 503, text="ready" if _READY else "starting")


async def _start_health_server(host: str = _HEALTH_HOST, port: int = _HEALTH_PORT) -> web.AppRunner | None:
    """Loopback-only liveness endpoint for the container HEALTHCHECK.

    If the bind fails, the app logs a warning and runs without a healthcheck.
    """
    app = web.Application()
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/readyz", _readyz)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port)
        await site.start()
    except OSError as e:
        await runner.cleanup()
        logger.warning("[scheduler] health endpoint unavailable on %s:%s: %s", host, port, e)
        return None
    return runner


async def run_scheduler() -> None:
    global _shutdown_event, _READY
    _setup_signal_handlers()

    assert _shutdown_event is not None

    config = get_config()
    channels = config.channels
    output_mode = config.output_mode

    logger.info("Starting StreamArchive...")
    logger.info("Monitoring channels: %s", ", ".join(channels))
    logger.info("Recording directory: %s", config.recording_dir)
    logger.info("Monitoring interval: %gs", config.monitoring_interval)
    logger.info("Output mode: %s", output_mode)

    shared_http = build_shared_client()
    twitch_api = TwitchAPI(config, http=shared_http)
    notifier = Notifier(config.bot_telegram_api, config.telegram_user_id)
    health_runner = await _start_health_server()

    # Constructed unconditionally so a live /mode youtube|both always has a
    # streamer available. It only stores paths and creates an httpx client.
    # A missing youtube_token.json is handled per task in _stream_youtube.
    youtube_streamer = YouTubeStreamer(config)
    logger.info("YouTube streaming enabled (privacy: %s)", config.youtube.privacy_status)

    recorder = Recorder(config, youtube_streamer, notifier)
    monitor = Monitor(recorder, notifier)

    kick_api = KickAPI(config, http=shared_http)
    _READY = True

    eventsub = EventSubClient(twitch_api, monitor, config)
    await eventsub.start()

    kick_webhook = KickWebhook(config, monitor, recorder, kick_api, notifier)
    if config.kick.webhook.enabled:
        await kick_webhook.start()
        logger.info("[kick_webhook] started (public: %s)", config.kick.webhook.public_url)

    updater = UpdateChecker(config, notifier, http=shared_http)
    updater_task = asyncio.create_task(updater.run_loop())
    logger.info("[updater] Update check enabled (every %gh)", config.update_check.interval_hours)

    telegram = TelegramController(
        config,
        recorder,
        monitor,
        eventsub,
        on_restart=lambda: _shutdown_event.set(),
        updater=updater,
        kick_webhook=kick_webhook,
        http=shared_http,
    )
    await telegram.start()

    version = _installed_app_version() or "unknown"
    try:
        await notifier.notify_startup(config.channels, version)
    except Exception:
        logger.error("[scheduler] notify_startup failed", exc_info=True)
    await eventsub.wait_ready(timeout=15)

    try:
        await _run_loop(monitor, twitch_api, kick_api, config, recorder)
    except asyncio.CancelledError:
        pass
    finally:
        await _shutdown(
            health_runner=health_runner,
            kick_webhook=kick_webhook,
            eventsub=eventsub,
            twitch_api=twitch_api,
            kick_api=kick_api,
            recorder=recorder,
            notifier=notifier,
            updater=updater,
            updater_task=updater_task,
            telegram=telegram,
            youtube_streamer=youtube_streamer,
            config=config,
            shared_http=shared_http,
        )


async def _run_loop(
    monitor: Monitor,
    twitch_api: TwitchAPI,
    kick_api: KickAPI,
    config: AppConfig,
    recorder: Recorder,
) -> None:
    """Poll channels until shutdown. Keeps the 5s poll and 86400s restart constants."""
    assert _shutdown_event is not None
    last_cleanup: float | None = None
    while not _shutdown_event.is_set():
        try:
            await monitor.check_channels(twitch_api, kick_api, config)
        except Exception as e:
            logger.error("[scheduler] Error in check_channels: %s", e)
            await asyncio.sleep(5)
            continue

        retention_days = config.retention_days
        if retention_days > 0 and (last_cleanup is None or time.monotonic() - last_cleanup >= 86400):
            removed = await recorder.cleanup_old_recordings(retention_days)
            logger.info("[scheduler] Retention cleanup removed %d expired recording(s)", removed)
            last_cleanup = time.monotonic()

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_shutdown_event.wait(), timeout=config.monitoring_interval)


async def _shutdown(
    *,
    health_runner: web.AppRunner | None,
    kick_webhook: KickWebhook | None,
    eventsub: EventSubClient,
    twitch_api: TwitchAPI,
    kick_api: KickAPI,
    recorder: Recorder,
    notifier: Notifier,
    updater: UpdateChecker,
    updater_task: asyncio.Task[None],
    telegram: TelegramController,
    youtube_streamer: YouTubeStreamer,
    config: AppConfig,
    shared_http: Any,
) -> None:
    """Close everything in order. Each close has its own guard, so one failure never skips the rest."""
    global _READY
    _READY = False
    logger.info("[scheduler] Shutting down, stopping all recordings...")
    try:
        await notifier.notify_shutdown()
    except Exception:
        logger.error("[scheduler] notify_shutdown failed", exc_info=True)
    try:
        updater_task.cancel()
        await asyncio.gather(updater_task, return_exceptions=True)
    except Exception:
        logger.error("[scheduler] updater task cancel failed", exc_info=True)
    if health_runner is not None:
        try:
            await health_runner.cleanup()
        except Exception:
            logger.error("[scheduler] health server cleanup failed", exc_info=True)
    if kick_webhook is not None and config.kick.webhook.enabled:
        try:
            await kick_webhook.close()
        except Exception:
            logger.error("[scheduler] kick webhook close failed", exc_info=True)
    try:
        await eventsub.close()
    except Exception:
        logger.error("[scheduler] eventsub close failed", exc_info=True)
    try:
        await twitch_api.close()
    except Exception:
        logger.error("[scheduler] twitch api close failed", exc_info=True)
    try:
        await kick_api.close()
    except Exception:
        logger.error("[scheduler] kick api close failed", exc_info=True)
    try:
        await recorder.close()
    except Exception:
        logger.error("[scheduler] recorder close failed", exc_info=True)
    try:
        await telegram.stop()
    except Exception:
        logger.error("[scheduler] telegram stop failed", exc_info=True)
    try:
        await youtube_streamer.close()
    except Exception:
        logger.error("[scheduler] youtube streamer close failed", exc_info=True)
    try:
        await notifier.close()
    except Exception:
        logger.error("[scheduler] notifier close failed", exc_info=True)
    try:
        await updater.close()
    except Exception:
        logger.error("[scheduler] updater close failed", exc_info=True)
    try:
        await shared_http.aclose()
    except Exception:
        logger.error("[scheduler] shared http close failed", exc_info=True)
    logger.info("[scheduler] Shutdown complete")


def main() -> None:
    """Console entry point for ``stream-archive``.

    Sets up logging and runs the scheduler.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    # Webhook heartbeats would otherwise log one line per POST.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.server").setLevel(logging.WARNING)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_scheduler())
