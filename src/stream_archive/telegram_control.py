import asyncio
import copy
import logging

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, filters

from src.stream_archive import disk
from src.stream_archive.config import _CHANNEL_RE, reload_config, save_config

logger = logging.getLogger(__name__)

_QUALITY_PRESETS = ("best", "1080p", "720p", "480p", "360p")


def _keyboards_equal(a, b):
    """True when both inline keyboards render identically (None == no keyboard)."""
    return (a.to_dict() if a is not None else {}) == (b.to_dict() if b is not None else {})


class TelegramController:
    """Telegram control surface for the admin user (config['telegram_user_id']).

    All state changes go through ``_apply``: validate on a deep copy, persist
    atomically to config.json, then swap into the live dict. A failed command
    leaves both memory and disk untouched.
    """

    def __init__(self, config, recorder, monitor, eventsub, on_restart=None, updater=None):
        self._config = config
        self._recorder = recorder
        self._monitor = monitor
        self._eventsub = eventsub
        self._on_restart = on_restart
        self._updater = updater
        self._admin_id = config["telegram_user_id"]
        self._app = Application.builder().token(config["bot_telegram_api"]).build()
        self._panel_message = None  # latest settings-panel message (auto-refresh target)
        self._panel_menu = "root"   # which submenu the panel currently shows
        self._refresh_task = None   # asyncio task keeping the panel current

    def command_list(self):
        """BotCommand entries for the Telegram /-menu, shown only to the admin."""
        return [
            BotCommand("start", "Show available commands and open settings"),
            BotCommand("help", "Show available commands"),
            BotCommand("status", "Show current status and settings"),
            BotCommand("channels", "List monitored channels"),
            BotCommand("add", "Start monitoring a channel"),
            BotCommand("remove", "Stop monitoring a channel"),
            BotCommand("retention", "Set recording retention in days"),
            BotCommand("mode", "Set output mode (disk, youtube, both)"),
            BotCommand("reload", "Re-read config.json from disk"),
            BotCommand("restart", "Restart the service"),
            BotCommand("update", "Check for and apply updates"),
            BotCommand("quality", "Show or set preferred quality"),
            BotCommand("maxrecordings", "Set concurrent recording limit"),
            BotCommand("maxyoutube", "Set YouTube re-stream limit"),
            BotCommand("disk", "Show or set disk limits"),
            BotCommand("chat", "Toggle live chat recording"),
            BotCommand("settings", "Open the settings panel with buttons"),
        ]

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
            CommandHandler("quality", self._cmd_quality, filters=admin),
            CommandHandler("maxrecordings", self._cmd_maxrecordings, filters=admin),
            CommandHandler("maxyoutube", self._cmd_maxyoutube, filters=admin),
            CommandHandler("disk", self._cmd_disk, filters=admin),
            CommandHandler("chat", self._cmd_chat, filters=admin),
            CommandHandler("settings", self._cmd_settings, filters=admin),
            CommandHandler("start", self._cmd_start, filters=admin),
            CallbackQueryHandler(self._on_callback),
        ])
        await self._app.initialize()
        await self._app.start()
        try:
            await self._app.bot.set_my_commands(
                self.command_list(), scope=BotCommandScopeChat(chat_id=self._admin_id)
            )
        except Exception:
            logger.warning("[telegram] Failed to register command menu", exc_info=True)
        self._refresh_task = asyncio.create_task(self._panel_refresh_loop())
        await self._app.updater.start_polling(allowed_updates=["message", "callback_query"])
        logger.info("[telegram] Bot polling started (admin id=%s)", self._admin_id)

    async def stop(self):
        if self._refresh_task is not None:
            self._refresh_task.cancel()
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

    @staticmethod
    def _back_row():
        return [InlineKeyboardButton("\u2190 Back", callback_data="back")]

    def settings_keyboard(self, menu="root"):
        """Inline buttons for the settings panel; ``menu`` selects the submenu.

        Root shows one row per setting (current value in the label); each row
        opens a submenu with that setting's choices. callback_data literals
        are the wire format.
        """
        if menu == "chat":
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("Chat: on", callback_data="chat:on"),
                 InlineKeyboardButton("Chat: off", callback_data="chat:off")],
                self._back_row(),
            ])
        if menu == "mode":
            return InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Mode: {m}", callback_data=f"mode:{m}")
                 for m in ("disk", "youtube", "both")],
                self._back_row(),
            ])
        if menu == "quality":
            return InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Quality: {q}", callback_data=f"quality:{q}")
                 for q in _QUALITY_PRESETS[:3]],
                [InlineKeyboardButton(f"Quality: {q}", callback_data=f"quality:{q}")
                 for q in _QUALITY_PRESETS[3:]],
                self._back_row(),
            ])
        if menu == "delete_oldest":
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("Delete oldest: on", callback_data="delete_oldest:on"),
                 InlineKeyboardButton("Delete oldest: off", callback_data="delete_oldest:off")],
                self._back_row(),
            ])
        c = self._config
        chat = "on" if c.get("record_chat", True) else "off"
        mode = c.get("output_mode", "disk")
        quality = c.get("preferred_quality", "best")
        oldest = "on" if c.get("disk", {}).get("delete_oldest", True) else "off"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Chat: {chat} \u203a", callback_data="menu:chat")],
            [InlineKeyboardButton(f"Mode: {mode} \u203a", callback_data="menu:mode")],
            [InlineKeyboardButton(f"Quality: {quality} \u203a", callback_data="menu:quality")],
            [InlineKeyboardButton(f"Delete oldest: {oldest} \u203a", callback_data="menu:delete_oldest")],
        ])

    def help_keyboard(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2699\ufe0f Settings", callback_data="panel")],
        ])

    async def settings_panel(self, menu="root"):
        """Current status text + the settings keyboard — the /settings message body."""
        return await self.handle_status(), self.settings_keyboard(menu)

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
            "/update - check for and apply updates (restarts after app/plugin changes; Docker streamlink needs an image rebuild)\n"
            "/quality [value] - preferred stream quality (best, 1080p, 720p, ...)\n"
            "/maxrecordings <n> - concurrent recording limit (0 = unlimited)\n"
            "/maxyoutube <n> - concurrent YouTube re-stream limit (0 = unlimited)\n"
            "/disk - show disk limits\n"
            "/disk <minfree|maxsize|fill|interval|delete_oldest> <value> - set disk limit\n"
            "/chat [on|off] - enable or disable live chat recording (off stops in-flight capture)\n"
            "/settings - open the settings panel with buttons\n"
            "/start - this help"
        )

    async def handle_status(self):
        c = self._config
        active = self._recorder.recording_info()
        disk_snap = await self._recorder.disk_snapshot()
        c_disk = c.get("disk", {})
        days = c["retention_days"]
        retention = f"Retention: {days} day" + ("s" if days != 1 else "") if days else "Retention: disabled"
        chat_state = "enabled" if c.get("record_chat", True) else "disabled"
        overrides = c.get("channel_output_modes") or {}
        per_channel = ""
        if overrides:
            per_channel = (
                f"Per-channel output: {', '.join(f'{k} \u2192 {v}' for k, v in sorted(overrides.items()))}\n"
            )
        rec_parts = []
        for info in active:
            part = f"{info['channel']} ({disk.format_duration(info['duration_s'])}"
            if info["size_mb"] is not None:
                part += f", {disk.format_bytes(int(info['size_mb'] * 1024 * 1024))}"
            rec_parts.append(part + ")")
        rec_now = ", ".join(rec_parts) if rec_parts else "none"
        max_rec = c.get("max_concurrent_recordings", 0)
        max_yt = c.get("max_concurrent_youtube_streams", 0)
        rec_limit = "unlimited" if not max_rec else f"{max_rec:g}"
        yt_limit = "unlimited" if not max_yt else f"{max_yt:g}"
        disk_limits = []
        min_free = c_disk.get("min_free_gb", 0)
        cap = c_disk.get("max_total_gb", 0)
        fill = c_disk.get("min_time_to_full_min", 0)
        if min_free > 0:
            disk_limits.append(f"keep {min_free:g} GB free")
        if cap > 0:
            if c_disk.get("delete_oldest", True):
                disk_limits.append(f"max {cap:g} GB (delete oldest when over)")
            else:
                disk_limits.append(f"max {cap:g} GB (stop recording when over)")
        if fill > 0:
            disk_limits.append(f"stop if disk fills within {fill:g} min")
        disk_limit_line = "Disk limits: " + " \u00b7 ".join(disk_limits) if disk_limits else "Disk limits: disabled"
        return (
            f"Channels ({len(c['channels'])}): {', '.join(c['channels'])}\n"
            f"Output mode: {c['output_mode']}\n"
            f"{per_channel}"
            f"{retention}\n"
            f"Chat recording: {chat_state}\n"
            f"Quality: {c.get('preferred_quality', 'best')}\n"
            f"Simultaneous recordings: {rec_limit}\n"
            f"YouTube re-streams: {yt_limit}\n"
            f"Recording now: {rec_now}\n"
            f"Disk: {disk_snap['free_gb']:.1f} GB free of {disk_snap['total_fs_gb']:.1f} GB \u00b7 recordings: {disk_snap['dir_gb']:.1f} GB\n"
            f"{disk_limit_line}\n"
            f"Update check: {'enabled' if (c.get('update_check') or {}).get('enabled', True) else 'disabled'} "
            f"(every {(c.get('update_check') or {}).get('interval_hours', 24)}h)"
        )

    def handle_channels(self):
        return "\n".join(f"{i}. {ch}" for i, ch in enumerate(self._config["channels"], 1))

    async def handle_add(self, args):
        if len(args) != 1:
            return "Usage: /add <channel>"
        ch = args[0]
        if not _CHANNEL_RE.match(ch):
            return f"\u274c Invalid channel name: {ch!r}"

        def mutate(candidate):
            if ch in candidate["channels"]:
                raise ValueError(f"{ch} is already monitored")
            candidate["channels"].append(ch)

        result = self._apply(mutate, lambda c: f"Added {ch} \u2014 {len(c['channels'])} channel(s) monitored")
        if not result.startswith("\u274c"):
            await self._eventsub.add_channel(ch)
        return result

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
        if not result.startswith("\u274c"):
            await self._eventsub.remove_channel(ch)
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

    async def handle_reload(self):
        try:
            reload_config(self._config)
        except ValueError as e:
            return f"\u274c Reload failed: {e}"
        await self._eventsub.sync_channels(self._config["channels"])
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
        applied = 0              # running code/plugin actually changed
        rebuild_required = False
        for source in ("app", "streamlink", "plugin"):
            if source not in results:
                continue
            status, detail = results[source]
            if status == "applied":
                applied += 1
                if source == "app":
                    lines.append(f'• stream-archive: pulled {report["app"].get("behind")} commit(s) — "{report["app"].get("subject")}"')
                elif source == "streamlink":
                    lines.append(f"• streamlink: {report['streamlink'].get('current')} → {report['streamlink'].get('latest')} ({detail})")
                else:
                    lines.append(f"• streamlink-ttvlol: {report['plugin'].get('current')} → {report['plugin'].get('latest')} (plugins/twitch.py replaced)")
            elif status == "applied_rebuild":
                rebuild_required = True
                lines.append(f"• streamlink: {report['streamlink'].get('current')} → {report['streamlink'].get('latest')} ({detail})")
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

    def handle_quality(self, args):
        if not args:
            return f"Quality: {self._config.get('preferred_quality', 'best')}"
        if len(args) == 1:
            q = args[0]
            return self._apply(
                lambda candidate: candidate.__setitem__("preferred_quality", q),
                lambda candidate: f"Quality set to {q}",
            )
        return "Usage: /quality <best|1080p|720p|...>"

    def handle_maxrecordings(self, args):
        if not args:
            return f"Max recordings: {self._config.get('max_concurrent_recordings', 0)} (0 = unlimited)"
        if len(args) == 1:
            try:
                n = int(args[0])
            except ValueError:
                return "\u274c max recordings must be an integer"
            return self._apply(
                lambda candidate: candidate.__setitem__("max_concurrent_recordings", n),
                lambda candidate: f"Max recordings set to {n}",
            )
        return "Usage: /maxrecordings <n> (0 = unlimited)"

    def handle_maxyoutube(self, args):
        if not args:
            return f"Max YouTube re-streams: {self._config.get('max_concurrent_youtube_streams', 0)} (0 = unlimited)"
        if len(args) == 1:
            try:
                n = int(args[0])
            except ValueError:
                return "\u274c max YouTube re-streams must be an integer"
            return self._apply(
                lambda candidate: candidate.__setitem__("max_concurrent_youtube_streams", n),
                lambda candidate: f"Max YouTube re-streams set to {n}",
            )
        return "Usage: /maxyoutube <n> (0 = unlimited)"

    def handle_disk(self, args):
        c = self._config
        usage = "Usage: /disk <minfree|maxsize|fill|interval|delete_oldest> <value>"
        if not args:
            d = c.get("disk", {})
            return (
                "Disk limits:\n"
                f"min free: {d.get('min_free_gb', 0):g} GB (0 = disabled)\n"
                f"max total: {d.get('max_total_gb', 0):g} GB (0 = disabled, delete oldest: {'on' if d.get('delete_oldest', True) else 'off'})\n"
                f"stop if full in < {d.get('min_time_to_full_min', 0):g} min (0 = disabled)\n"
                f"check every {d.get('check_interval_s', 60):g}s"
            )
        if len(args) != 2:
            return usage
        cmd, val = args[0].lower(), args[1]
        if cmd == "delete_oldest":
            if val == "on":
                return self._apply(
                    lambda candidate: candidate.setdefault("disk", {}).__setitem__("delete_oldest", True),
                    lambda candidate: "Delete oldest enabled",
                )
            if val == "off":
                return self._apply(
                    lambda candidate: candidate.setdefault("disk", {}).__setitem__("delete_oldest", False),
                    lambda candidate: "Delete oldest disabled",
                )
            return usage
        try:
            v = float(val)
        except ValueError:
            return f"\u274c {cmd} must be a number"
        if cmd == "minfree":
            return self._apply(
                lambda candidate: candidate.setdefault("disk", {}).__setitem__("min_free_gb", v),
                lambda candidate: f"Disk min free set to {v:g} GB",
            )
        if cmd == "maxsize":
            return self._apply(
                lambda candidate: candidate.setdefault("disk", {}).__setitem__("max_total_gb", v),
                lambda candidate: f"Disk max total set to {v:g} GB",
            )
        if cmd == "fill":
            return self._apply(
                lambda candidate: candidate.setdefault("disk", {}).__setitem__("min_time_to_full_min", v),
                lambda candidate: f"Disk fill guard set to {v:g} min",
            )
        if cmd == "interval":
            return self._apply(
                lambda candidate: candidate.setdefault("disk", {}).__setitem__("check_interval_s", v),
                lambda candidate: f"Disk check interval set to {v:g}s",
            )
        return usage

    async def handle_chat(self, args):
        if not args:
            state = "enabled" if self._config.get("record_chat", True) else "disabled"
            return f"Chat recording: {state}"
        if len(args) == 1 and args[0].lower() in ("on", "off"):
            enabled = args[0].lower() == "on"
            text = self._apply(
                lambda candidate: candidate.__setitem__("record_chat", enabled),
                lambda candidate: f"Chat recording {'enabled' if enabled else 'disabled'}",
            )
            if not enabled and not text.startswith("\u274c"):
                for channel in self._recorder.active_channels():
                    await self._recorder.stop_chat(channel)
            return text
        return "Usage: /chat <on|off>"

    async def handle_callback(self, data):
        """Apply one inline-button press; returns (reply_text, reply_markup).

        ``menu:<name>``/``back`` only navigate. Value presses delegate to the
        same validated handlers as the text commands and land back on the root
        menu (so the updated current value is visible). Unknown/stale callback
        data is a no-op re-render of the root.
        """
        action, _, value = data.partition(":")
        if action == "menu":
            menu = value if value in ("chat", "mode", "quality", "delete_oldest") else "root"
            self._panel_menu = menu
            return await self.settings_panel(menu)
        if action == "chat" and value in ("on", "off"):
            await self.handle_chat([value])
        elif action == "mode" and value in ("disk", "youtube", "both"):
            self.handle_mode([value])
        elif action == "quality" and value in _QUALITY_PRESETS:
            self.handle_quality([value])
        elif action == "delete_oldest" and value in ("on", "off"):
            self.handle_disk(["delete_oldest", value])
        # "back", "panel", and anything unknown: just re-render the root
        self._panel_menu = "root"
        return await self.settings_panel()

    async def _on_callback(self, update, context):
        query = update.callback_query
        user = update.effective_user
        if user is None or user.id != self._admin_id:  # non-admin presses are ignored
            await query.answer()
            return
        text, markup = await self.handle_callback(query.data)
        if query.message is None:  # original message was deleted -> send a fresh one
            await query.answer()
            self._panel_message = await context.bot.send_message(
                chat_id=query.from_user.id, text=text, reply_markup=markup
            )
        elif query.message.text == text and _keyboards_equal(query.message.reply_markup, markup):
            await query.answer("No change")  # state and view already match
        else:
            await query.answer()
            try:
                self._panel_message = await query.edit_message_text(text, reply_markup=markup)
            except BadRequest:  # message vanished mid-flight -> resend
                self._panel_message = await context.bot.send_message(
                    chat_id=query.from_user.id, text=text, reply_markup=markup
                )

    async def _panel_refresh_loop(self):
        """Background task: refresh the open settings panel every 30s."""
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    await self._refresh_panel(None)
                except Exception:
                    logger.warning("[telegram] Panel auto-refresh failed", exc_info=True)
        except asyncio.CancelledError:
            pass

    async def _refresh_panel(self, context):
        """Job: keep the open settings panel current without user interaction.

        Re-renders the panel body (status text) with the currently displayed
        submenu every 30s, but only edits when something actually changed.
        """
        message = self._panel_message
        if message is None:
            return
        text, markup = await self.settings_panel(self._panel_menu)
        if message.text == text and _keyboards_equal(message.reply_markup, markup):
            return
        try:
            self._panel_message = await message.edit_text(text, reply_markup=markup)
        except BadRequest:  # panel message was deleted -> stop refreshing it
            self._panel_message = None

    async def _cmd_help(self, update, context):
        await update.effective_message.reply_text(self.handle_help(), reply_markup=self.help_keyboard())

    async def _cmd_status(self, update, context):
        await update.effective_message.reply_text(await self.handle_status(), reply_markup=self.help_keyboard())

    async def _cmd_settings(self, update, context):
        text, markup = await self.settings_panel()
        self._panel_message = await update.effective_message.reply_text(text, reply_markup=markup)
        self._panel_menu = "root"

    async def _cmd_start(self, update, context):
        await update.effective_message.reply_text(self.handle_help(), reply_markup=self.help_keyboard())

    async def _cmd_channels(self, update, context):
        await update.effective_message.reply_text(self.handle_channels())

    async def _cmd_add(self, update, context):
        await update.effective_message.reply_text(await self.handle_add(context.args or []))

    async def _cmd_remove(self, update, context):
        await update.effective_message.reply_text(await self.handle_remove(context.args or []))

    async def _cmd_retention(self, update, context):
        await update.effective_message.reply_text(self.handle_retention(context.args or []))

    async def _cmd_mode(self, update, context):
        await update.effective_message.reply_text(self.handle_mode(context.args or []))

    async def _cmd_reload(self, update, context):
        await update.effective_message.reply_text(await self.handle_reload())

    async def _cmd_restart(self, update, context):
        await update.effective_message.reply_text(self.handle_restart())

    async def _cmd_update(self, update, context):
        await update.effective_message.reply_text(await self.handle_update())

    async def _cmd_quality(self, update, context):
        await update.effective_message.reply_text(self.handle_quality(context.args or []))

    async def _cmd_maxrecordings(self, update, context):
        await update.effective_message.reply_text(self.handle_maxrecordings(context.args or []))

    async def _cmd_maxyoutube(self, update, context):
        await update.effective_message.reply_text(self.handle_maxyoutube(context.args or []))

    async def _cmd_disk(self, update, context):
        await update.effective_message.reply_text(self.handle_disk(context.args or []))

    async def _cmd_chat(self, update, context):
        await update.effective_message.reply_text(await self.handle_chat(context.args or []))
