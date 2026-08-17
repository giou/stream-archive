import asyncio
import contextlib
import logging
import re
import signal
import time
from pathlib import Path
from typing import Any

from stream_archive.config import get_config
from stream_archive.eventsub import EventSubClient
from stream_archive.kick_api import KickAPI
from stream_archive.kick_webhook import KickWebhook
from stream_archive.monitor import Monitor
from stream_archive.notifier import Notifier
from stream_archive.recorder import Recorder
from stream_archive.telegram import TelegramController
from stream_archive.twitch_api import TwitchAPI
from stream_archive.updater import UpdateChecker
from stream_archive.youtube_streamer import YouTubeStreamer

logger = logging.getLogger(__name__)

_shutdown_event: asyncio.Event | None = None


def _setup_signal_handlers() -> None:
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    def handle_signal(signum: int, frame: Any) -> None:
        logger.info("[scheduler] Received signal %s, initiating shutdown...", signum)
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def _app_version(workdir: Path) -> str | None:
    """Best-effort app version from pyproject.toml (the project is not pip-installed)."""
    try:
        text = (Path(workdir) / "pyproject.toml").read_text()
    except OSError:
        return None
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


async def run_scheduler() -> None:
    global _shutdown_event
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

    retention_days = config.retention_days
    last_cleanup = None

    twitch_api = TwitchAPI(config)
    notifier = Notifier(config.bot_telegram_api, config.telegram_user_id)

    # Constructed unconditionally so a live /mode youtube|both always has a
    # streamer available; it only stores paths and creates an httpx client.
    # Missing youtube_token.json is handled per-task in _stream_youtube.
    youtube_streamer = YouTubeStreamer(config)
    logger.info("YouTube streaming enabled (privacy: %s)", config.youtube.privacy_status)

    recorder = Recorder(config, youtube_streamer, notifier)
    monitor = Monitor(recorder, notifier)

    kick_api = KickAPI(config)

    eventsub = EventSubClient(twitch_api, monitor, config)
    await eventsub.start()

    kick_webhook = KickWebhook(config, monitor, recorder, kick_api, notifier)
    if config.kick.webhook.enabled:
        await kick_webhook.start()
        logger.info("[kick_webhook] started (public: %s)", config.kick.webhook.public_url)

    updater = UpdateChecker(config, notifier)
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
    )
    await telegram.start()

    # Startup notification: monitored channels + current version (git short sha).
    sha = await updater.local_sha()
    ver = _app_version(config._workdir)
    if ver and sha:
        version = f"v{ver} ({sha[:7]})"
    elif ver:
        version = f"v{ver}"
    else:
        version = "unknown"
    await notifier.notify_startup(config.channels, version)
    await eventsub.wait_ready(timeout=15)
    logger.info("[scheduler] EventSub: %s", eventsub.status())

    try:
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
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("[scheduler] Shutting down, stopping all recordings...")
        await notifier.notify_shutdown()
        await recorder.stop_all()
        updater_task.cancel()
        await asyncio.gather(updater_task, return_exceptions=True)
        await updater.close()
        await telegram.stop()
        await notifier.close()
        await eventsub.close()
        if kick_webhook:
            await kick_webhook.close()
        await kick_api.close()
        await twitch_api.close()
        await youtube_streamer.close()
        logger.info("[scheduler] Shutdown complete")


def main() -> None:
    """Console entry point (``stream-archive``): logging setup + run loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_scheduler())
