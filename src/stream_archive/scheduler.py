import asyncio
import contextlib
import logging
import signal
import time
from typing import Any

from aiohttp import web

from stream_archive import events
from stream_archive.api import ControlAPI
from stream_archive.config import AppConfig, get_config, telegram_enabled
from stream_archive.eventsub import EventSubClient
from stream_archive.http import build_http_client
from stream_archive.kick_api import KickAPI
from stream_archive.kick_webhook import KickWebhook
from stream_archive.monitor import Monitor
from stream_archive.mtproto_upload import MtprotoUploader
from stream_archive.notifier import Notifier, NullNotifier
from stream_archive.recorder import Recorder
from stream_archive.telegram import TelegramController
from stream_archive.twitch_api import TwitchAPI
from stream_archive.updater import UpdateChecker, installed_app_version
from stream_archive.webui import WebUI
from stream_archive.youtube_streamer import YouTubeStreamer

logger = logging.getLogger(__name__)

_shutdown_event: asyncio.Event | None = None

_HEALTH_HOST = "127.0.0.1"
_HEALTH_PORT = 9100

#: Interval between retention sweeps of the archive.
_CLEANUP_INTERVAL_SECONDS = 86400.0
#: Retry delay after a failed retention sweep.
_CLEANUP_RETRY_SECONDS = 3600.0

#: Deadline for stopping every capture during shutdown, in seconds. The chat
#: finalizer renames its file after awaiting the emote fetch, and that fetch
#: is bounded per request rather than in total, so a slow emote CDN can hold
#: the teardown past the container's stop grace period and be killed with the
#: chat file still an unterminated .tmp. Smaller than the shipped
#: stop_grace_period (90 s), so the tree gets to finish its own cleanup.
_SHUTDOWN_DEADLINE_S = 60.0

_READY = False  # flips True once recorder and API clients exist; reset at the start of each run


def _setup_signal_handlers() -> None:
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    def handle_signal(signum: int) -> None:
        logger.info("[scheduler] Received signal %s, initiating shutdown...", signum)
        _shutdown_event.set()

    # A loop signal handler runs as a loop callback. A plain signal.signal
    # handler can set the event between the value check and the waiter of
    # Event.wait(), and the poll loop then misses the wakeup.
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_signal, signal.SIGTERM)
    loop.add_signal_handler(signal.SIGINT, handle_signal, signal.SIGINT)


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
    # A new run starts unready. The clients and the bot do not exist yet.
    _READY = False

    assert _shutdown_event is not None

    config = get_config()
    # The feed survives restarts: entries recorded before this boot return.
    events.load(events.feed_path(config.workdir))
    channels = config.channels
    output_mode = config.output_mode

    logger.info("Starting StreamArchive...")
    logger.info("Monitoring channels: %s", ", ".join(channels))
    logger.info("Recording directory: %s", config.recording_dir)
    logger.info("Monitoring interval: %gs", config.monitoring_interval)
    logger.info("Output mode: %s", output_mode)

    shared_http = build_http_client()

    # The guard starts before the first constructor. A constructor that
    # raises must still release what already exists: the shared client, the
    # updater task and the health server. Each name is None until built.
    health_runner: web.AppRunner | None = None
    twitch_api: TwitchAPI | None = None
    notifier: Notifier | None = None
    youtube_streamer: YouTubeStreamer | None = None
    recorder: Recorder | None = None
    kick_api: KickAPI | None = None
    eventsub: EventSubClient | None = None
    kick_webhook: KickWebhook | None = None
    updater: UpdateChecker | None = None
    updater_task: asyncio.Task[None] | None = None
    telegram: TelegramController | None = None
    mtproto: MtprotoUploader | None = None
    # Every resource inside the try below shuts down in order. A failed
    # Telegram start, for example, must not leave a recording or a held
    # YouTube broadcast behind.
    try:
        twitch_api = TwitchAPI(config, http=shared_http)
        # Without Telegram tokens the null object drops alerts and the
        # web panel controls the app. Both can run at once otherwise.
        notifier = Notifier(config) if telegram_enabled(config) else NullNotifier()
        health_runner = await _start_health_server()

        # Constructed unconditionally so a live /mode youtube|both always has a
        # streamer available. It only stores paths and creates an httpx client.
        # A missing youtube_token.json is handled per task in _stream_youtube.
        youtube_streamer = YouTubeStreamer(config)
        logger.info("YouTube streaming enabled (privacy: %s)", config.youtube.privacy_status)

        recorder = Recorder(config, youtube_streamer, notifier)
        monitor = Monitor(recorder, notifier)

        kick_api = KickAPI(config, http=shared_http)

        eventsub = EventSubClient(twitch_api, monitor, config)
        kick_webhook = KickWebhook(config, monitor, recorder, kick_api, notifier)

        updater = UpdateChecker(config, notifier, http=shared_http)
        updater_task = asyncio.create_task(updater.run_loop())
        if config.update_check.enabled:
            logger.info("[updater] Update check enabled (every %gh)", config.update_check.interval_hours)
        else:
            logger.info("[updater] Update check disabled")

        if config.mtproto.enabled:
            mtproto = MtprotoUploader(config)
            try:
                await mtproto.connect()
            except Exception:
                logger.warning("[mtproto] Connect failed at startup", exc_info=True)

        telegram = TelegramController(
            config,
            recorder,
            monitor,
            eventsub,
            on_restart=lambda: _shutdown_event.set(),
            updater=updater,
            kick_webhook=kick_webhook,
            http=shared_http,
            mtproto=mtproto,
        )
        control_api = ControlAPI(config, telegram, recorder)
        control_api.register_routes(kick_webhook)
        webui = WebUI(config, telegram, recorder, http=shared_http)
        webui.register_routes(kick_webhook)
        telegram.bind_live_check(twitch_api, kick_api)

        await eventsub.start()
        await kick_webhook.apply_state()
        if kick_webhook.listening_needed():
            logger.info("[kick_webhook] started (public: %s)", config.endpoint.public_url or "(none)")

        await telegram.start()
        if not telegram_enabled(config):
            logger.info("[scheduler] Telegram bot disabled, the web panel at / controls the app")
        elif config.web.enabled:
            logger.info("[scheduler] Web panel enabled at /, Telegram bot stays on")

        version = installed_app_version() or "unknown"
        try:
            await notifier.notify_startup(config.channels, version)
        except Exception:
            logger.error("[scheduler] notify_startup failed", exc_info=True)
        await eventsub.wait_ready(timeout=15)

        # Ready for orchestrators only now: the signal clients and the bot run.
        _READY = True
        await _run_loop(monitor, twitch_api, kick_api, config, recorder)
    except asyncio.CancelledError:
        logger.info("[scheduler] Scheduler task cancelled, shutting down")
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
            shared_http=shared_http,
            mtproto=mtproto,
        )


async def _pause(seconds: float) -> None:
    """Sleep, but wake at once when shutdown starts.

    A plain sleep holds the poll loop past a signal, and the teardown then
    runs into the container stop grace period.
    """
    assert _shutdown_event is not None
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_shutdown_event.wait(), timeout=seconds)


async def _run_loop(
    monitor: Monitor,
    twitch_api: TwitchAPI,
    kick_api: KickAPI,
    config: AppConfig,
    recorder: Recorder,
) -> None:
    """Poll channels until shutdown. Keeps the 5s poll and 86400s restart constants."""
    assert _shutdown_event is not None
    next_cleanup: float | None = None
    while not _shutdown_event.is_set():
        try:
            await monitor.check_channels(twitch_api, kick_api, config)
        except Exception as e:
            logger.error("[scheduler] Error in check_channels: %s", e, exc_info=True)
            await _pause(5)
            continue

        retention_days = config.retention_days
        if retention_days > 0 and (next_cleanup is None or time.monotonic() >= next_cleanup):
            # A failed cleanup must not kill the daemon. Log it and retry after
            # a backoff, so a broken tree cannot re-walk the archive on every
            # poll tick.
            cleanup_task: asyncio.Task[int] = asyncio.create_task(recorder.cleanup_old_recordings(retention_days))
            shutdown_task: asyncio.Task[bool] = asyncio.create_task(_shutdown_event.wait())
            # The sweep can walk a large archive. Race it against shutdown, so
            # a signal does not wait for the whole sweep and the rest of the
            # teardown fits in the container grace period.
            done, _ = await asyncio.wait({cleanup_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
            if cleanup_task in done:
                shutdown_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await shutdown_task
                try:
                    removed = cleanup_task.result()
                except Exception:
                    logger.error("[scheduler] Retention cleanup failed", exc_info=True)
                    next_cleanup = time.monotonic() + _CLEANUP_RETRY_SECONDS
                else:
                    logger.info("[scheduler] Retention cleanup removed %d expired recording(s)", removed)
                    next_cleanup = time.monotonic() + _CLEANUP_INTERVAL_SECONDS
            else:
                # Shutdown won. Stop the sweep before the teardown closes the
                # recorder: an orphaned sweep would keep unlinking files
                # through the close, and nothing would read its result.
                # Cancellation lands between two deletions, never inside one.
                cleanup_task.cancel()
                await asyncio.gather(cleanup_task, return_exceptions=True)
                logger.info("[scheduler] Shutdown during retention cleanup")

        await _pause(config.monitoring_interval)


async def _shutdown(
    *,
    health_runner: web.AppRunner | None,
    kick_webhook: KickWebhook | None,
    eventsub: EventSubClient | None,
    twitch_api: TwitchAPI | None,
    kick_api: KickAPI | None,
    recorder: Recorder | None,
    notifier: Notifier | None,
    updater: UpdateChecker | None,
    updater_task: asyncio.Task[None] | None,
    telegram: TelegramController | None,
    youtube_streamer: YouTubeStreamer | None,
    shared_http: Any,
    mtproto: MtprotoUploader | None = None,
) -> None:
    """Close everything in order. Each close has its own guard, so one failure never skips the rest.

    A resource that never got built is None. This method skips it.
    """
    global _READY
    _READY = False
    logger.info("[scheduler] Shutting down, stopping all recordings...")
    if notifier is not None:
        try:
            await notifier.notify_shutdown()
        except Exception:
            logger.error("[scheduler] notify_shutdown failed", exc_info=True)
    if updater_task is not None:
        try:
            updater_task.cancel()
            await asyncio.gather(updater_task, return_exceptions=True)
        except Exception:
            logger.error("[scheduler] updater task cancel failed", exc_info=True)
    # Telegram drives every component that closes below. Stop the bot first,
    # or a command that arrives during the teardown window runs against a
    # closed recorder or API client.
    if telegram is not None:
        try:
            await telegram.stop()
        except Exception:
            logger.error("[scheduler] telegram stop failed", exc_info=True)
    active = telegram._mtproto if telegram is not None else None
    if active is None:
        active = mtproto
    if active is not None or telegram is not None:
        try:
            tasks = list(telegram._mtproto_tasks) if telegram is not None else []
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if active is not None:
                await active.disconnect()
        except Exception:
            logger.error("[scheduler] mtproto disconnect failed", exc_info=True)
    if health_runner is not None:
        try:
            await health_runner.cleanup()
        except Exception:
            logger.error("[scheduler] health server cleanup failed", exc_info=True)
    if kick_webhook is not None:
        try:
            # Runs also when only the control API kept the listener alive.
            await kick_webhook.close()
        except Exception:
            logger.error("[scheduler] kick webhook close failed", exc_info=True)
    if eventsub is not None:
        try:
            await eventsub.close()
        except Exception:
            logger.error("[scheduler] eventsub close failed", exc_info=True)
    if twitch_api is not None:
        try:
            await twitch_api.close()
        except Exception:
            logger.error("[scheduler] twitch api close failed", exc_info=True)
    if kick_api is not None:
        try:
            await kick_api.close()
        except Exception:
            logger.error("[scheduler] kick api close failed", exc_info=True)
    if recorder is not None:
        deadline = asyncio.timeout(_SHUTDOWN_DEADLINE_S)
        try:
            # Bound the capture teardown. The chat finalizer writes its trailer
            # after awaiting the emote fetch, which is bounded per request and
            # not in total, so an unbounded close() can outlast the container's
            # stop grace period and be killed with the file still a .tmp. A
            # timeout cancels the finalizer, whose CancelledError path writes
            # the trailer without the emote images and renames the file.
            async with deadline:
                await recorder.close()
        except TimeoutError:
            if deadline.expired():
                logger.error(
                    "[scheduler] recorder close exceeded the %.0fs shutdown deadline; "
                    "open chat captures were finalized without their emote images",
                    _SHUTDOWN_DEADLINE_S,
                )
            else:
                # The close raised its own TimeoutError (IRC read, HTTP
                # request). That is its failure, not our deadline.
                logger.error("[scheduler] recorder close failed", exc_info=True)
        except Exception:
            logger.error("[scheduler] recorder close failed", exc_info=True)
    if youtube_streamer is not None:
        try:
            await youtube_streamer.close()
        except Exception:
            logger.error("[scheduler] youtube streamer close failed", exc_info=True)
    if notifier is not None:
        try:
            await notifier.close()
        except Exception:
            logger.error("[scheduler] notifier close failed", exc_info=True)
    if updater is not None:
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
