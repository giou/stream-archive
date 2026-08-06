import logging
import time

logger = logging.getLogger(__name__)

FAILURE_NOTIFY_INTERVAL = 1800


class Monitor:
    def __init__(self, recorder, notifier):
        self.recorder = recorder
        self.notifier = notifier
        self._live_channels = set()
        self._last_failure_notify = {}

    async def check_channels(self, twitch_api, config):
        try:
            user_ids = await twitch_api.resolve_user_ids(config["channels"])
            if not user_ids:
                logger.warning("[monitor] Failed to resolve user IDs")
                return
        except Exception as e:
            logger.error("[monitor] resolve_user_ids failed: %s", e)
            return

        try:
            streams = await twitch_api.get_live_streams(user_ids)
        except Exception as e:
            logger.error("[monitor] get_live_streams failed: %s", e)
            return

        user_to_channel = {v: k for k, v in user_ids.items()}

        for user_id, stream in streams.items():
            channel = user_to_channel.get(user_id)
            if channel is None:
                logger.warning("[monitor] Got stream for unknown user %s, skipping", user_id)
                continue
            if channel not in self._live_channels:
                ok = await self.recorder.start(
                    channel,
                    title=stream.get("title"),
                    game=stream.get("game_name"),
                )
                if ok:
                    self._live_channels.add(channel)
                    self._last_failure_notify.pop(channel, None)
                    logger.info("[monitor] %s is LIVE", channel)
                else:
                    await self._handle_start_failure(channel)
            elif not self.recorder.is_recording(channel):
                logger.warning("[monitor] %s recording stopped unexpectedly, restarting", channel)
                ok = await self.recorder.start(
                    channel,
                    title=stream.get("title"),
                    game=stream.get("game_name"),
                )
                if ok:
                    self._last_failure_notify.pop(channel, None)
                    logger.info("[monitor] %s recording restarted", channel)
                else:
                    await self._handle_start_failure(channel)

        for channel in config["channels"]:
            user_id = user_ids.get(channel)
            if channel in self._live_channels and user_id not in streams:
                self._live_channels.discard(channel)
                result = await self.recorder.stop(channel)
                file_info = result.get("file_info") if result else None
                yt_info = result.get("youtube_info") if result else None
                youtube_url = yt_info["youtube_url"] if yt_info else None
                await self.notifier.notify_offline(channel, file_info, youtube_url)
                logger.info("[monitor] %s is OFFLINE", channel)

    async def _handle_start_failure(self, channel):
        now = time.monotonic()
        if now - self._last_failure_notify.get(channel, 0.0) < FAILURE_NOTIFY_INTERVAL:
            return
        self._last_failure_notify[channel] = now
        await self.notifier.notify(
            f"\u26a0\ufe0f Failed to start recording for {channel}. Will retry automatically on the next check."
        )
