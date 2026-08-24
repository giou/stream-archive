import asyncio
import logging
import time
from typing import Any

from stream_archive.config import AppConfig, bare_name, is_kick_channel, kick_bare_name

logger = logging.getLogger(__name__)

FAILURE_NOTIFY_INTERVAL = 1800
DISK_NOTIFY_INTERVAL = 1800


class Monitor:
    def __init__(self, recorder: Any, notifier: Any):
        self.recorder = recorder
        self.notifier = notifier
        self._live_channels: set[str] = set()
        self._last_failure_notify: dict[str, float] = {}
        self._last_disk_notify = -float(DISK_NOTIFY_INTERVAL)
        self._locks: dict[str, asyncio.Lock] = {}
        self._warned_unknown_kick: set[str] = set()
        self._kick_api_error_logged = False

    def _lock_for(self, channel: str) -> asyncio.Lock:
        if channel not in self._locks:
            self._locks[channel] = asyncio.Lock()
        return self._locks[channel]

    async def check_channels(self, twitch_api: Any, kick_api: Any, config: AppConfig) -> None:
        snapshot = None
        twitch_channels = [c for c in config.channels if not is_kick_channel(c)]
        if twitch_channels:
            try:
                resolved = await twitch_api.resolve_user_ids([bare_name(c) for c in twitch_channels])
            except Exception as e:
                logger.error("[monitor] resolve_user_ids failed: %s", e)
                resolved = {}
            if not resolved:
                logger.warning("[monitor] Failed to resolve user IDs")
            else:
                identity_by_bare = {bare_name(c): c for c in twitch_channels}
                user_ids = {identity_by_bare[bare]: uid for bare, uid in resolved.items() if bare in identity_by_bare}
                # A failed Helix fetch must not read as "everyone offline":
                # the sweep below stops every live recording on an API
                # outage. Leave streams unset and skip both loops instead.
                streams: dict[str, Any] | None = None
                try:
                    streams = await twitch_api.get_live_streams(user_ids)
                except Exception as e:
                    logger.error("[monitor] get_live_streams failed: %s", e)

                if streams is not None:
                    user_to_channel = {v: k for k, v in user_ids.items()}

                    snapshot = await self._snapshot_if_needed(config)

                    for user_id, stream in sorted(streams.items(), key=lambda kv: user_to_channel.get(kv[0], "")):
                        channel = user_to_channel.get(user_id)
                        if channel is None:
                            logger.warning("[monitor] Got stream for unknown user %s, skipping", user_id)
                            continue
                        snapshot = await self._ensure_recording(
                            channel,
                            stream.get("title"),
                            stream.get("game_name"),
                            user_id,
                            config,
                            snapshot,
                        )

                    for channel in twitch_channels:
                        uid = user_ids.get(channel)
                        if channel in self._live_channels and uid not in streams:
                            await self._ensure_stopped(channel, config)

        kick_channels = [c for c in config.channels if is_kick_channel(c)]
        if kick_channels:
            try:
                statuses = await kick_api.get_channel_statuses([kick_bare_name(c) for c in kick_channels])
            except Exception as e:
                # Log once per failure episode; the poll retries every
                # interval anyway, so per-cycle error lines are just spam.
                if not self._kick_api_error_logged:
                    self._kick_api_error_logged = True
                    logger.error("[monitor] kick get_channel_statuses failed: %s", e)
                else:
                    logger.debug("[monitor] kick get_channel_statuses still failing: %s", e)
            else:
                self._kick_api_error_logged = False
                if snapshot is None:
                    snapshot = await self._snapshot_if_needed(config)
                for ch in kick_channels:
                    bare = kick_bare_name(ch)
                    status = statuses.get(bare)
                    if status is None:
                        if bare not in self._warned_unknown_kick:
                            self._warned_unknown_kick.add(bare)
                            logger.warning("[monitor] kick channel not found: %s", ch)
                        if ch in self._live_channels:
                            await self._ensure_stopped(ch, config)
                    elif status["is_live"]:
                        snapshot = await self._ensure_recording(
                            ch, status["title"], status["game"], None, config, snapshot
                        )
                    elif ch in self._live_channels:
                        await self._ensure_stopped(ch, config)

    async def handle_online(
        self, channel: str, title: str | None, game: str | None, user_id: str | None, config: AppConfig
    ) -> None:
        """EventSub stream.online entry point."""
        if channel not in config.channels:
            return
        snapshot = await self._snapshot_if_needed(config)
        await self._ensure_recording(channel, title, game, user_id, config, snapshot)

    async def handle_offline(self, channel: str, config: AppConfig) -> None:
        """EventSub stream.offline entry point."""
        if channel not in config.channels:
            return
        await self._ensure_stopped(channel, config)

    async def _snapshot_if_needed(self, config: AppConfig) -> Any:
        disk_cfg = config.disk
        need_snap = disk_cfg.max_total_gb > 0
        return await self.recorder.disk_snapshot() if need_snap else None

    async def _ensure_recording(
        self, channel: str, title: str | None, game: str | None, user_id: str | None, config: AppConfig, snapshot: Any
    ) -> Any:
        """Start (or restart) the recording for a channel that is live. Returns the snapshot."""
        async with self._lock_for(channel):
            if channel in self._live_channels:
                if self.recorder.is_recording(channel):
                    return snapshot
                if self.recorder.ended_clean(channel):
                    # The stream ended on its own; the offline webhook/API
                    # poll hasn't caught up yet. Restarting now would resolve
                    # a dead stream URL every poll cycle until it does.
                    logger.debug("[monitor] %s ended cleanly, awaiting offline event", channel)
                    return snapshot
                logger.warning("[monitor] %s recording stopped unexpectedly, restarting", channel)
                ok, snapshot = await self._start_or_block(
                    channel,
                    title,
                    game,
                    config,
                    snapshot,
                    user_id=user_id,
                )
                if ok:
                    self._last_failure_notify.pop(channel, None)
                    logger.info("[monitor] %s recording restarted", channel)
                else:
                    await self._handle_start_failure(channel)
                return snapshot
            ok, snapshot = await self._start_or_block(
                channel,
                title,
                game,
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
            return snapshot

    async def _ensure_stopped(self, channel: str, config: AppConfig) -> None:
        """Stop the recording for a channel that is no longer live."""
        async with self._lock_for(channel):
            if channel not in self._live_channels:
                return
            self._live_channels.discard(channel)
            result = await self.recorder.stop(channel)
            file_info = result.get("file_info") if result else None
            yt_info = result.get("youtube_info") if result else None
            youtube_url = yt_info["youtube_url"] if yt_info else None
            await self.notifier.notify_offline(channel, file_info, youtube_url)
            logger.info("[monitor] %s is OFFLINE", channel)

    def remove_channel(self, channel: str) -> None:
        self._live_channels.discard(channel)

    async def _start_or_block(
        self,
        channel: str,
        title: str | None,
        game: str | None,
        config: AppConfig,
        snapshot: Any,
        user_id: str | None = None,
    ) -> tuple[bool, Any]:
        reason, snapshot = await self._start_blocked_reason(channel, config, snapshot)
        if reason:
            logger.warning("[monitor] %s not started: %s", channel, reason)
            await self._notify_blocked(channel, reason)
            return False, snapshot
        reserve_reason = await self.recorder.reserve_start(channel)
        if reserve_reason:
            logger.warning("[monitor] %s not started: %s", channel, reserve_reason)
            await self._notify_blocked(channel, reserve_reason)
            return False, snapshot
        try:
            ok = await self.recorder.start(channel, title=title, game=game, user_id=user_id)
        finally:
            self.recorder.release_start(channel)
        return ok, snapshot

    async def _start_blocked_reason(self, channel: str, config: AppConfig, snapshot: Any) -> tuple[str | None, Any]:
        """Return (reason_or_None, snapshot). Raises nothing: snapshot failures fail open."""
        reason = self.recorder.youtube_restart_blocked_reason(channel)
        if reason:
            return (reason, snapshot)
        try:
            disk_cfg = config.disk
            cap = disk_cfg.max_total_gb
            if snapshot is not None and cap > 0 and snapshot["dir_gb"] >= cap:
                if disk_cfg.delete_oldest:
                    await self.recorder.delete_oldest_to_cap()
                    snapshot = await self.recorder.disk_snapshot()
                    if snapshot["dir_gb"] >= cap:
                        return (f"recording archive at {cap:g} GB cap (nothing to delete)", snapshot)
                else:
                    return (f"recording archive at {cap:g} GB cap", snapshot)
            return (None, snapshot)
        except Exception as e:
            logger.error("[monitor] disk gate failed, proceeding: %s", e)
            return (None, snapshot)

    async def _notify_blocked(self, channel: str, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_disk_notify < DISK_NOTIFY_INTERVAL:
            return
        self._last_disk_notify = now
        await self.notifier.notify(f"\u26a0\ufe0f Not recording {channel}: {reason}")

    async def _handle_start_failure(self, channel: str) -> None:
        now = time.monotonic()
        if now - self._last_failure_notify.get(channel, -FAILURE_NOTIFY_INTERVAL) < FAILURE_NOTIFY_INTERVAL:
            return
        self._last_failure_notify[channel] = now
        await self.notifier.notify(
            f"\u26a0\ufe0f Failed to start recording for {channel}. Will retry automatically on the next check."
        )
