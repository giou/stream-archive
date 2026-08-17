import asyncio
from typing import Any

from stream_archive import disk
from stream_archive.config import AppConfig


class SystemCommands:
    _config: AppConfig
    _recorder: Any
    _updater: Any
    _on_restart: Any
    _webhook_state_text: Any

    def handle_help(self) -> str:
        return (
            "Available commands:\n"
            "/help - this list\n"
            "/status - current settings\n"
            "/channels - monitored channels\n"
            "/add <channel|twitch:<channel>|kick:<channel>|url> - start monitoring a channel (twitch:<name>, kick:<name>, or a twitch.tv/kick.com profile URL)\n"
            "/remove <channel|kick:<channel>|url> - stop monitoring a channel\n"
            "/retention <days> - recording retention\n"
            "/mode [channel] <disk|youtube|both|default> - output mode (per-channel override when a channel is given)\n"
            "/reload - re-read config.json\n"
            "/restart - restart the service\n"
            "/update - check for and apply updates (restarts after app/plugin changes; Docker streamlink needs an image rebuild)\n"
            "/quality [value] - preferred stream quality (best, 1080p, 720p, ...)\n"
            "/maxrecordings <n> - concurrent recording limit (0 = unlimited)\n"
            "/maxyoutube <n> - concurrent YouTube re-stream limit (0 = unlimited)\n"
            "/disk - show disk limits\n"
            "/disk <maxsize|delete_oldest> <value> - set disk limit\n"
            "/chat [on|off] [twitch|kick] - enable or disable live chat recording (add twitch or kick for one platform; off stops in-flight capture)\n"
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
        overrides = c.channel_output_modes
        per_channel = ""
        if overrides:
            per_channel = (
                "Per-channel output: " + ", ".join(f"{k} \u2192 {v}" for k, v in sorted(overrides.items())) + "\n"
            )
        rec_parts = []
        for info in active:
            part = f"{info['channel']} ({disk.format_duration(info['duration_s'])}"
            if info["size_mb"] is not None:
                part += f", {disk.format_bytes(int(info['size_mb'] * 1024 * 1024))}"
            rec_parts.append(part + ")")
        rec_now = ", ".join(rec_parts) if rec_parts else "none"
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
        return (
            f"Channels ({len(c.channels)}): {', '.join(c.channels)}\n"
            f"Output mode: {c.output_mode}\n"
            f"{per_channel}"
            f"{retention}\n"
            f"Chat recording: {chat_state}\n"
            f"Kick chat recording: {'enabled' if k.record_chat else 'disabled'}\n"
            f"Kick webhook: {webhook_state}\n"
            f"Quality: {c.preferred_quality}\n"
            f"Simultaneous recordings: {rec_limit}\n"
            f"YouTube re-streams: {yt_limit}\n"
            f"Recording now: {rec_now}\n"
            f"Disk: {disk_snap['free_gb']:.1f} GB free of {disk_snap['total_fs_gb']:.1f} GB \u00b7 recordings: {disk_snap['dir_gb']:.1f} GB\n"
            f"{disk_limit_line}\n"
            f"Update check: {'enabled' if c.update_check.enabled else 'disabled'} "
            f"(every {c.update_check.interval_hours:g}h)"
        )

    def handle_restart(self) -> str:
        if self._on_restart is None:
            return "Restart is not available (no shutdown callback configured)"
        asyncio.get_running_loop().call_later(0.5, self._on_restart)
        return "\U0001f504 Restarting... the service will come back in a few seconds"

    async def handle_update(self) -> str:
        if self._updater is None:
            return "Update checks are not configured"
        report = await self._updater.check(notify=False)
        available = [s for s in ("app", "streamlink", "plugin") if s in report and report[s]["status"] == "update"]

        if not available:
            lines = []
            if "app" in report:
                lines.append(f"• stream-archive: {(report['app'].get('local') or '')[:7]} (main)")
            if "streamlink" in report:
                lines.append(f"• streamlink: {report['streamlink'].get('latest')}")
            if "plugin" in report:
                lines.append(f"• streamlink-ttvlol: {report['plugin'].get('latest')}")
            return "\u2705 Up to date\n" + "\n".join(lines)

        results = await self._updater.apply(report)
        display = {"app": "stream-archive", "streamlink": "streamlink", "plugin": "streamlink-ttvlol"}
        lines = []
        applied = 0  # running code/plugin actually changed
        rebuild_required = False
        for source in ("app", "streamlink", "plugin"):
            if source not in results:
                continue
            status, detail = results[source]
            if status == "applied":
                applied += 1
                if source == "app":
                    lines.append(
                        f'• stream-archive: pulled {report["app"].get("behind")} commit(s) — "{report["app"].get("subject")}"'
                    )
                elif source == "streamlink":
                    lines.append(
                        f"• streamlink: {report['streamlink'].get('current')} → {report['streamlink'].get('latest')} ({detail})"
                    )
                else:
                    lines.append(
                        f"• streamlink-ttvlol: {report['plugin'].get('current')} → {report['plugin'].get('latest')} (plugins/twitch.py replaced)"
                    )
            elif status == "applied_rebuild":
                rebuild_required = True
                lines.append(
                    f"• streamlink: {report['streamlink'].get('current')} → {report['streamlink'].get('latest')} ({detail})"
                )
            elif status == "failed":
                lines.append(f"• {display[source]}: {detail}")
        body = "\n".join(lines)
        rebuild_block = "\n\nRun on the host:\ndocker compose up -d --build" if rebuild_required else ""
        if applied and self._on_restart is not None:
            asyncio.get_running_loop().call_later(0.5, self._on_restart)
            return f"\U0001f504 Updates applied\n{body}{rebuild_block}\nRestarting the service..."
        if applied:
            return f"\U0001f504 Updates applied\n{body}{rebuild_block}\nRestart is not available (foreground run) — restart manually"
        if rebuild_required:
            return f"\U0001f504 Updates applied\n{body}{rebuild_block}"
        return f"\u274c Update failed\n{body}\nNo restart triggered."
