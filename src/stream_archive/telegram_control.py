import asyncio
import copy
import logging

from telegram.ext import Application, CommandHandler, filters

from src.stream_archive.config import _CHANNEL_RE, reload_config, save_config

logger = logging.getLogger(__name__)


class TelegramController:
    """Telegram control surface for the admin user (config['telegram_user_id']).

    All state changes go through ``_apply``: validate on a deep copy, persist
    atomically to config.json, then swap into the live dict. A failed command
    leaves both memory and disk untouched.
    """

    def __init__(self, config, recorder, monitor, on_restart=None, updater=None):
        self._config = config
        self._recorder = recorder
        self._monitor = monitor
        self._on_restart = on_restart
        self._updater = updater
        self._admin_id = config["telegram_user_id"]
        self._app = Application.builder().token(config["bot_telegram_api"]).build()

    async def start(self):
        admin = filters.User(user_id=self._admin_id)
        self._app.add_handlers([
            CommandHandler("help", self._cmd_help, filters=admin),
            CommandHandler("status", self._cmd_status, filters=admin),
            CommandHandler("channels", self._cmd_channels, filters=admin),
            CommandHandler("add", self._cmd_add, filters=admin),
            CommandHandler("remove", self._cmd_remove, filters=admin),
            CommandHandler("retention", self._cmd_retention, filters=admin),
            CommandHandler("mode", self._cmd_mode, filters=admin),
            CommandHandler("reload", self._cmd_reload, filters=admin),
            CommandHandler("restart", self._cmd_restart, filters=admin),
            CommandHandler("update", self._cmd_update, filters=admin),
        ])
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(allowed_updates=["message"])
        logger.info("[telegram] Bot polling started (admin id=%s)", self._admin_id)

    async def stop(self):
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    def _apply(self, mutate, ok_text):
        candidate = copy.deepcopy(self._config)
        try:
            mutate(candidate)
            save_config(candidate)
        except ValueError as e:
            return f"\u274c {e}"
        self._config.clear()
        self._config.update(candidate)
        return ok_text(candidate)

    def handle_help(self):
        return (
            "Available commands:\n"
            "/help - this list\n"
            "/status - current settings\n"
            "/channels - monitored channels\n"
            "/add <channel> - start monitoring a channel\n"
            "/remove <channel> - stop monitoring a channel\n"
            "/retention <days> - recording retention\n"
            "/mode [channel] <disk|youtube|both|default> - output mode (per-channel override when a channel is given)\n"
            "/reload - re-read config.json\n"
            "/restart - restart the service\n"
            "/update - check for and apply updates (restarts the service)"
        )

    def handle_status(self):
        c = self._config
        active = self._recorder.active_channels()
        days = c["retention_days"]
        retention = f"Retention: {days} day" + ("s" if days != 1 else "") if days else "Retention: disabled"
        overrides = c.get("channel_output_modes") or {}
        per_channel = ""
        if overrides:
            per_channel = (
                f"Per-channel modes: {', '.join(f'{k}={v}' for k, v in sorted(overrides.items()))}\n"
            )
        return (
            f"Channels ({len(c['channels'])}): {', '.join(c['channels'])}\n"
            f"Output mode: {c['output_mode']}\n"
            f"{per_channel}"
            f"{retention}\n"
            f"Monitoring interval: {c['monitoring_interval']}s\n"
            f"Recording now: {', '.join(active) if active else 'none'}\n"
            f"Update check: {'enabled' if (c.get('update_check') or {}).get('enabled', True) else 'disabled'} "
            f"(every {(c.get('update_check') or {}).get('interval_hours', 24)}h)"
        )

    def handle_channels(self):
        return "\n".join(f"{i}. {ch}" for i, ch in enumerate(self._config["channels"], 1))

    def handle_add(self, args):
        if len(args) != 1:
            return "Usage: /add <channel>"
        ch = args[0]
        if not _CHANNEL_RE.match(ch):
            return f"\u274c Invalid channel name: {ch!r}"

        def mutate(candidate):
            if ch in candidate["channels"]:
                raise ValueError(f"{ch} is already monitored")
            candidate["channels"].append(ch)

        return self._apply(mutate, lambda c: f"Added {ch} \u2014 {len(c['channels'])} channel(s) monitored")

    async def handle_remove(self, args):
        if len(args) != 1:
            return "Usage: /remove <channel>"
        ch = args[0]

        def mutate(candidate):
            if ch not in candidate["channels"]:
                raise ValueError(f"{ch} is not in the monitored list")
            candidate["channels"].remove(ch)
            candidate.setdefault("channel_output_modes", {}).pop(ch, None)

        result = self._apply(mutate, lambda c: f"Removed {ch} \u2014 {len(c['channels'])} channel(s) monitored")
        if not result.startswith("\u274c") and self._recorder.is_recording(ch):
            await self._recorder.stop(ch)
            self._monitor.remove_channel(ch)
        return result

    def handle_retention(self, args):
        if len(args) != 1:
            return "Usage: /retention <days>"
        try:
            n = int(args[0])
        except ValueError:
            return "\u274c retention must be an integer"

        def mutate(candidate):
            candidate["retention_days"] = n

        return self._apply(mutate, lambda c: f"Retention set to {n} day(s)")

    def handle_mode(self, args):
        if len(args) == 1:
            m = args[0].lower()

            def mutate(candidate):
                candidate["output_mode"] = m

            return self._apply(mutate, lambda c: f"Output mode set to {m}")

        if len(args) == 2:
            ch, m = args[0], args[1].lower()
            if not _CHANNEL_RE.match(ch):
                return f"\u274c Invalid channel name: {ch!r}"
            if m == "default":
                def mutate(candidate):
                    candidate.setdefault("channel_output_modes", {}).pop(ch, None)
                return self._apply(
                    mutate, lambda c: f"Output mode for {ch} reset to global ({c['output_mode']})"
                )

            def mutate(candidate):
                candidate.setdefault("channel_output_modes", {})[ch] = m

            return self._apply(mutate, lambda c: f"Output mode for {ch} set to {m}")

        return "Usage: /mode <disk|youtube|both> or /mode <channel> <disk|youtube|both|default>"

    def handle_reload(self):
        try:
            reload_config(self._config)
        except ValueError as e:
            return f"\u274c Reload failed: {e}"
        return "\u2705 Config reloaded from config.json"

    def handle_restart(self):
        if self._on_restart is None:
            return "Restart is not available (no shutdown callback configured)"
        asyncio.get_running_loop().call_later(0.5, self._on_restart)
        return "\U0001f504 Restarting... the service will come back in a few seconds"

    async def handle_update(self):
        if self._updater is None:
            return "Update checks are not configured"
        report = await self._updater.check(notify=False)
        available = [s for s in ("app", "streamlink", "plugin")
                     if s in report and report[s]["status"] == "update"]

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
        applied = 0
        for source in ("app", "streamlink", "plugin"):
            if source not in results:
                continue
            status, detail = results[source]
            if status == "applied":
                applied += 1
                if source == "app":
                    lines.append(f'• stream-archive: pulled {report["app"].get("behind")} commit(s) — "{report["app"].get("subject")}"')
                elif source == "streamlink":
                    lines.append(f"• streamlink: {report['streamlink'].get('current')} → {report['streamlink'].get('latest')} (uv.lock updated)")
                else:
                    lines.append(f"• streamlink-ttvlol: {report['plugin'].get('current')} → {report['plugin'].get('latest')} (plugins/twitch.py replaced)")
            elif status == "failed":
                lines.append(f"• {display[source]}: {detail}")
        body = "\n".join(lines)
        if applied and self._on_restart is not None:
            asyncio.get_running_loop().call_later(0.5, self._on_restart)
            return f"\U0001f504 Updates applied\n{body}\nRestarting the service..."
        if applied:
            return f"\U0001f504 Updates applied\n{body}\nRestart is not available (foreground run) — restart manually"
        return f"\u274c Update failed\n{body}\nNo restart triggered."

    async def _cmd_help(self, update, context):
        await update.effective_message.reply_text(self.handle_help())

    async def _cmd_status(self, update, context):
        await update.effective_message.reply_text(self.handle_status())

    async def _cmd_channels(self, update, context):
        await update.effective_message.reply_text(self.handle_channels())

    async def _cmd_add(self, update, context):
        await update.effective_message.reply_text(self.handle_add(context.args or []))

    async def _cmd_remove(self, update, context):
        await update.effective_message.reply_text(await self.handle_remove(context.args or []))

    async def _cmd_retention(self, update, context):
        await update.effective_message.reply_text(self.handle_retention(context.args or []))

    async def _cmd_mode(self, update, context):
        await update.effective_message.reply_text(self.handle_mode(context.args or []))

    async def _cmd_reload(self, update, context):
        await update.effective_message.reply_text(self.handle_reload())

    async def _cmd_restart(self, update, context):
        await update.effective_message.reply_text(self.handle_restart())

    async def _cmd_update(self, update, context):
        await update.effective_message.reply_text(await self.handle_update())
