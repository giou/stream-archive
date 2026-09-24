import asyncio
from typing import Any

from stream_archive import disk
from stream_archive.config import AppConfig

#: Most entries one /status list shows before it counts the rest.
_STATUS_LIST_LIMIT = 20

#: Character budget for one /status list. Four lists plus the fixed lines
#: must stay below the 4096-character limit of Telegram.
_STATUS_LIST_CHARS = 600


def _status_list(items: list[str]) -> str:
    """Join one /status list. A long list counts the rest instead of listing it.

    Telegram rejects a message longer than 4096 characters, so the channel
    and override lists of /status must not grow without a bound. The
    helper limits the entry count and the length of the joined text.
    """
    shown = items[:_STATUS_LIST_LIMIT]
    while shown and len(", ".join(shown)) > _STATUS_LIST_CHARS:
        shown.pop()
    if len(shown) == len(items):
        return ", ".join(shown)
    rest = len(items) - len(shown)
    head = ", ".join(shown)
    return f"{head} \u2026 and {rest} more" if head else f"\u2026 and {rest} more"


class SystemCommands:
    _config: AppConfig
    _recorder: Any
    _updater: Any
    _on_restart: Any
    _webhook_state_text: Any
    _endpoint_state_text: Any
    _mtproto_state_text: Any
    _web_state_text: Any

    def handle_help(self) -> str:
        return (
            "Available commands:\n"
            "/help - this list\n"
            "/status - current settings\n"
            "/channels - monitored channels\n"
            "/add <channel|twitch:<channel>|kick:<channel>|url> - start monitoring a channel (twitch:<name>, kick:<name>, or a twitch.tv/kick.com profile URL)\n"
            "/remove <channel|twitch:<channel>|kick:<channel>|url> - stop monitoring a channel\n"
            "/retention <days> - recording retention\n"
            "/mode [channel] <disk|youtube|both|default> - output mode (per-channel override when a channel is given)\n"
            "/reload - re-read config.json\n"
            "/restart - restart the service\n"
            "/update - check for updates (apply by pulling the image)\n"
            "/maxrecordings <n> - concurrent recording limit (0 = unlimited)\n"
            "/maxyoutube <n> - concurrent YouTube re-stream limit (0 = unlimited)\n"
            "/quality [channel] <value|default> - preferred stream quality (best, 1080p, ..., audio_only; per-channel override)\n"
            "/disk <maxsize|delete_oldest> <value> - set disk limit\n"
            "/disk - show disk limits\n"
            "/chat [on|off] [twitch|kick] - enable or disable live chat recording (add twitch or kick for one platform; off stops in-flight capture)\n"
            "/recordings - browse stored recordings (send or delete)\n"
            "/settings - open the settings menu (reply keyboard buttons)\n"
            "/start - this help"
        )

    async def handle_status(self) -> str:
        c = self._config
        active = self._recorder.recording_info()
        disk_snap = await self._recorder.disk_snapshot()
        c_disk = c.disk
        days = c.retention_days
        retention = f"Retention: {days:g} day" + ("s" if days != 1 else "") if days else "Retention: disabled"
        chat_state = "enabled" if c.record_chat else "disabled"
        k = c.kick
        webhook_state = self._webhook_state_text()
        endpoint_state = self._endpoint_state_text()
        overrides = c.channel_output_modes
        per_channel = ""
        if overrides:
            per_channel = (
                "Per-channel output: " + _status_list([f"{k} \u2192 {v}" for k, v in sorted(overrides.items())]) + "\n"
            )
        q_overrides = c.channel_preferred_qualities
        per_channel_q = ""
        if q_overrides:
            per_channel_q = (
                "Per-channel quality: "
                + _status_list([f"{ch} \u2192 {q}" for ch, q in sorted(q_overrides.items())])
                + "\n"
            )
        rec_parts = []
        for info in active:
            part = f"{info['channel']} ({disk.format_duration(info['duration_s'])}"
            if info["size_mb"] is not None:
                part += f", {disk.format_bytes(int(info['size_mb'] * 1024 * 1024))}"
            rec_parts.append(part + ")")
        rec_now = _status_list(rec_parts) if rec_parts else "none"
        max_rec = c.max_concurrent_recordings
        max_yt = c.max_concurrent_youtube_streams
        rec_limit = "unlimited" if not max_rec else f"{max_rec:g}"
        yt_limit = "unlimited" if not max_yt else f"{max_yt:g}"
        disk_limits = []
        cap = c_disk.max_total_gb
        if cap > 0:
            if c_disk.delete_oldest:
                disk_limits.append(f"max {cap:g} GB (delete oldest when over)")
            else:
                disk_limits.append(f"max {cap:g} GB (stop recording when over)")
        disk_limit_line = "Disk limits: " + " \u00b7 ".join(disk_limits) if disk_limits else "Disk limits: disabled"
        if disk_snap.get("usage_ok", True):
            disk_line = (
                f"Disk: {disk_snap['free_gb']:.1f} GB free of {disk_snap['total_fs_gb']:.1f} GB "
                f"\u00b7 archive: {disk_snap['archive_gb']:.1f} GB"
            )
        else:
            # The probe failed, so every filesystem number is 0.0 GB. Say
            # unknown instead of reporting a full disk.
            disk_line = f"Disk: usage unknown \u00b7 archive: {disk_snap['archive_gb']:.1f} GB"
        return (
            f"Channels ({len(c.channels)}): {_status_list(c.channels)}\n"
            f"Output mode: {c.output_mode}\n"
            f"{per_channel}"
            f"{per_channel_q}"
            f"{retention}\n"
            f"Chat recording: {chat_state}\n"
            f"Kick chat recording: {'enabled' if k.record_chat else 'disabled'}\n"
            f"Endpoint: {endpoint_state}\n"
            f"Kick webhook: {webhook_state}\n"
            f"MTProto upload: {self._mtproto_state_text()}\n"
            f"Web panel: {self._web_state_text()}\n"
            f"Quality: {c.preferred_quality}\n"
            f"Simultaneous recordings: {rec_limit}\n"
            f"YouTube re-streams: {yt_limit}\n"
            f"Recording now: {rec_now}\n"
            f"{disk_line}\n"
            f"{disk_limit_line}\n"
            f"Update check: {'enabled' if c.update_check.enabled else 'disabled'} "
            f"(every {c.update_check.interval_hours:g}h)"
        )

    def handle_restart(self) -> str:
        if self._on_restart is None:
            return "Restart is not available (no shutdown callback configured)"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return "Restart is not available (no running event loop)"
        loop.call_later(0.5, self._on_restart)
        from stream_archive import events as _events

        _events.record("config", None, "Restart requested")
        return "\U0001f504 Restarting... the service will come back in a few seconds"

    async def handle_update(self) -> str:
        if self._updater is None:
            return "Update checks are not configured"
        report = await self._updater.check(notify=False)
        data = report.get("app") or {}
        status = data.get("status")
        if status == "up_to_date":
            return f"✅ Up to date\n• stream-archive: v{data.get('current')}"
        if status != "update":
            return "❌ Update check failed - try again later."
        lines = [f"• stream-archive: v{data.get('current')} → v{data.get('latest')}"]
        cl = data.get("changelog") or []
        if cl:
            lines.append("  Changelog:")
            lines.extend(f"  • {ln}" for ln in cl)
        return (
            "📦 Updates available\n"
            + "\n".join(lines)
            + "\n\nApply by running:\ndocker compose pull && docker compose up -d"
        )
