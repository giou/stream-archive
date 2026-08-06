import asyncio
import logging
import signal
import time
from src.twitch_recorder.config import get_config
from src.twitch_recorder.twitch_api import TwitchAPI
from src.twitch_recorder.monitor import Monitor
from src.twitch_recorder.recorder import Recorder
from src.twitch_recorder.notifier import Notifier
from src.twitch_recorder.youtube_streamer import YouTubeStreamer

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


async def run_scheduler():
    global _shutdown_event
    _setup_signal_handlers()

    config = get_config()
    channels = config["channels"]
    output_mode = config["output_mode"]

    logger.info("Starting Twitch Monitor...")
    logger.info("Monitoring channels: %s", ", ".join(channels))
    logger.info("Recording directory: %s", config["recording_dir"])
    logger.info("Monitoring interval: %ss", config["monitoring_interval"])
    logger.info("Output mode: %s", output_mode)

    retention_days = config.get("retention_days", 0)
    last_cleanup = None

    twitch_api = TwitchAPI()
    notifier = Notifier(config["bot_telegram_api"], config["telegram_user_id"])
    youtube_streamer = None

    if output_mode in ("youtube", "both"):
        youtube_streamer = YouTubeStreamer(config)
        logger.info("YouTube streaming enabled (privacy: %s)", config["youtube"]["privacy_status"])

    recorder = Recorder(config, youtube_streamer, notifier)
    monitor = Monitor(recorder, notifier)

    try:
        while not _shutdown_event.is_set():
            try:
                await monitor.check_channels(twitch_api, config)
            except Exception as e:
                logger.error("[scheduler] Error in check_channels: %s", e)
                await asyncio.sleep(5)
                continue

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
        await recorder.stop_all()
        await notifier.close()
        await twitch_api.close()
        if youtube_streamer:
            await youtube_streamer.close()
        logger.info("[scheduler] Shutdown complete")


async def main():
    try:
        await run_scheduler()
    except KeyboardInterrupt:
        pass
