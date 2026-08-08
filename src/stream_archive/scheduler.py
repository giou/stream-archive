import asyncio
import logging
import re
import signal
import time
from pathlib import Path
from src.stream_archive.config import get_config
from src.stream_archive.twitch_api import TwitchAPI
from src.stream_archive.monitor import Monitor
from src.stream_archive.recorder import Recorder
from src.stream_archive.notifier import Notifier
from src.stream_archive.youtube_streamer import YouTubeStreamer
from src.stream_archive.telegram_control import TelegramController
from src.stream_archive.updater import UpdateChecker
from src.stream_archive.eventsub import EventSubClient

logger = logging.getLogger(__name__)

_shutdown_event = None


def _setup_signal_handlers():
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    def handle_signal(signum, frame):
        logger.info("[scheduler] Received signal %s, initiating shutdown...", signum)
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def _app_version(workdir):
    """Best-effort app version from pyproject.toml (the project is not pip-installed)."""
    try:
        text = (Path(workdir) / "pyproject.toml").read_text()
    except OSError:
        return None
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


async def run_scheduler():
    global _shutdown_event
    _setup_signal_handlers()

    config = get_config()
    channels = config["channels"]
    output_mode = config["output_mode"]

    logger.info("Starting StreamArchive...")
    logger.info("Monitoring channels: %s", ", ".join(channels))
    logger.info("Recording directory: %s", config["recording_dir"])
    logger.info("Monitoring interval: %ss", config["monitoring_interval"])
    logger.info("Output mode: %s", output_mode)

    retention_days = config.get("retention_days", 0)
    last_cleanup = None

    twitch_api = TwitchAPI()
    notifier = Notifier(config["bot_telegram_api"], config["telegram_user_id"])

    # Constructed unconditionally so a live /mode youtube|both always has a
    # streamer available; it only stores paths and creates an httpx client.
    # Missing youtube_token.json is handled per-task in _stream_youtube.
    youtube_streamer = YouTubeStreamer(config)
    logger.info("YouTube streaming enabled (privacy: %s)", config["youtube"]["privacy_status"])

    recorder = Recorder(config, youtube_streamer, notifier)
    monitor = Monitor(recorder, notifier)

    eventsub = EventSubClient(twitch_api, monitor, config)
    await eventsub.start()

    updater = UpdateChecker(config, notifier)
    updater_task = asyncio.create_task(updater.run_loop())
    logger.info("[updater] Update check enabled (every %sh)", config["update_check"]["interval_hours"])

    telegram = TelegramController(
        config, recorder, monitor, eventsub, on_restart=lambda: _shutdown_event.set(), updater=updater
    )
    await telegram.start()

    # Startup notification: monitored channels + current version (git short sha).
    sha = await updater.local_sha()
    ver = _app_version(config["_workdir"])
    if ver and sha:
        version = f"v{ver} ({sha[:7]})"
    elif ver:
        version = f"v{ver}"
    else:
        version = "unknown"
    await notifier.notify_startup(config["channels"], version)
    await eventsub.wait_ready(timeout=15)
    logger.info("[scheduler] EventSub: %s", eventsub.status())

    try:
        while not _shutdown_event.is_set():
            try:
                await monitor.check_channels(twitch_api, config)
            except Exception as e:
                logger.error("[scheduler] Error in check_channels: %s", e)
                await asyncio.sleep(5)
                continue

            retention_days = config.get("retention_days", 0)
            if retention_days > 0 and (last_cleanup is None or time.monotonic() - last_cleanup >= 86400):
                removed = await recorder.cleanup_old_recordings(retention_days)
                logger.info("[scheduler] Retention cleanup removed %d expired recording(s)", removed)
                last_cleanup = time.monotonic()

            try:
                await asyncio.wait_for(_shutdown_event.wait(), timeout=config["monitoring_interval"])
            except asyncio.TimeoutError:
                pass
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
        await twitch_api.close()
        await youtube_streamer.close()
        logger.info("[scheduler] Shutdown complete")


async def main():
    try:
        await run_scheduler()
    except KeyboardInterrupt:
        pass
