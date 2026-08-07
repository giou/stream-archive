import logging
import time

logger = logging.getLogger(__name__)

FAILURE_NOTIFY_INTERVAL = 1800
DISK_NOTIFY_INTERVAL = 1800


class Monitor:
    def __init__(self, recorder, notifier):
        self.recorder = recorder
        self.notifier = notifier
        self._live_channels = set()
        self._last_failure_notify = {}
        self._last_disk_notify = 0.0

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

        disk_cfg = config.get("disk", {})
        need_snap = disk_cfg.get("min_free_gb", 0) > 0 or disk_cfg.get("max_total_gb", 0) > 0
        snapshot = await self.recorder.disk_snapshot() if need_snap else None

        for user_id, stream in sorted(streams.items(), key=lambda kv: user_to_channel.get(kv[0], "")):
            channel = user_to_channel.get(user_id)
            if channel is None:
                logger.warning("[monitor] Got stream for unknown user %s, skipping", user_id)
                continue
            if channel not in self._live_channels:
                ok, snapshot = await self._start_or_block(
                    channel,
                    stream.get("title"),
                    stream.get("game_name"),
                    config,
                    snapshot,
                    user_id=user_id,
                )
                if ok:
                    self._live_channels.add(channel)
                    self._last_failure_notify.pop(channel, None)
                    logger.info("[monitor] %s is LIVE", channel)
                else:
                    await self._handle_start_failure(channel)
            elif not self.recorder.is_recording(channel):
                logger.warning("[monitor] %s recording stopped unexpectedly, restarting", channel)
                ok, snapshot = await self._start_or_block(
                    channel,
                    stream.get("title"),
                    stream.get("game_name"),
                    config,
                    snapshot,
                    user_id=user_id,
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

    def remove_channel(self, channel):
        self._live_channels.discard(channel)

    async def _start_or_block(self, channel, title, game, config, snapshot, user_id=None):
        reason, snapshot = await self._start_blocked_reason(channel, config, snapshot)
        if reason:
            logger.warning("[monitor] %s not started: %s", channel, reason)
            await self._notify_blocked(channel, reason)
            return False, snapshot
        ok = await self.recorder.start(channel, title=title, game=game, user_id=user_id)
        return ok, snapshot

    async def _start_blocked_reason(self, channel, config, snapshot):
        """Return (reason_or_None, snapshot). Raises nothing: snapshot failures fail open."""
        try:
            disk_cfg = config.get("disk", {})
            min_free = disk_cfg.get("min_free_gb", 0)
            cap = disk_cfg.get("max_total_gb", 0)
            if snapshot is not None:
                if min_free > 0 and snapshot["free_gb"] < min_free:
                    return (f"free disk space below {min_free:g} GB ({snapshot['free_gb']:.1f} GB free)", snapshot)
                if cap > 0 and snapshot["dir_gb"] >= cap:
                    if disk_cfg.get("evict_when_over", True):
                        await self.recorder.evict_to_cap()
                        snapshot = await self.recorder.disk_snapshot()
                        if snapshot["dir_gb"] >= cap:
                            return (f"recording archive at {cap:g} GB cap (nothing to evict)", snapshot)
                    else:
                        return (f"recording archive at {cap:g} GB cap", snapshot)
            max_rec = config.get("max_concurrent_recordings", 0)
            if max_rec > 0 and len(self.recorder.active_channels()) >= max_rec:
                return (f"concurrent recording limit reached ({max_rec}/{max_rec})", snapshot)
            max_yt = config.get("max_concurrent_youtube_streams", 0)
            if max_yt > 0:
                mode = config.get("channel_output_modes", {}).get(channel, config.get("output_mode", "disk"))
                if mode in ("youtube", "both") and self.recorder.youtube_active_count() >= max_yt:
                    return (f"YouTube re-stream limit reached ({max_yt}/{max_yt})", snapshot)
            return (None, snapshot)
        except Exception as e:
            logger.error("[monitor] disk gate failed, proceeding: %s", e)
            return (None, snapshot)

    async def _notify_blocked(self, channel, reason):
        now = time.monotonic()
        if now - self._last_disk_notify < DISK_NOTIFY_INTERVAL:
            return
        self._last_disk_notify = now
        await self.notifier.notify(f"\u26a0\ufe0f Not recording {channel}: {reason}")

    async def _handle_start_failure(self, channel):
        now = time.monotonic()
        if now - self._last_failure_notify.get(channel, 0.0) < FAILURE_NOTIFY_INTERVAL:
            return
        self._last_failure_notify[channel] = now
        await self.notifier.notify(
            f"\u26a0\ufe0f Failed to start recording for {channel}. Will retry automatically on the next check."
        )
