import asyncio
import copy
import logging

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from src.stream_archive import disk
from src.stream_archive.config import _CHANNEL_RE, reload_config, save_config

logger = logging.getLogger(__name__)

_QUALITY_PRESETS = ("best", "1080p", "720p", "480p", "360p")


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
        self._menu = "root"        # current reply-keyboard menu
        self._menu_channel = None  # channel name when _menu == "channel"
        self._custom_setting = None  # parent setting when _menu == "custom"
        self._confirm_done = set()   # callback_data already confirmed (double-tap guard)

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
            BotCommand("settings", "Open the settings menu (reply keyboard buttons)"),
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
            MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(user_id=self._admin_id), self._on_text),
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
        await self._app.updater.start_polling(allowed_updates=["message", "callback_query"])
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

    def reply_keyboard(self, menu="root", channel=None):
        """Reply-keyboard rows for ``menu``; button labels are the routing literals."""
        if menu == "root":
            rows = [["Channels", "Status"], ["Chat recording", "Output mode"],
                    ["Quality", "Retention"], ["Max recordings", "Max YouTube"],
                    ["Disk"]]
        elif menu == "channels":
            rows = [["Add channel"],
                    *([f"\u2022 {ch}"] for ch in self._config["channels"]),
                    ["Back"]]
        elif menu == "channel":
            rows = [["Delete channel"], ["Mode: disk", "Mode: youtube"],
                    ["Mode: both", "Mode: default"], ["Back"]]
        elif menu == "chat":
            rows = [["On", "Off"], ["Back"]]
        elif menu == "mode":
            rows = [["disk", "youtube", "both"], ["Back"]]
        elif menu == "quality":
            rows = [["best", "1080p", "720p"], ["480p", "360p"], ["Back"]]
        elif menu == "retention":
            rows = [["1 day", "3 days", "7 days"], ["14 days", "30 days", "Off"],
                    ["Custom", "Back"]]
        elif menu in ("maxrec", "maxyt"):
            rows = [["0 (unlimited)", "1", "2"], ["3", "5"], ["Custom", "Back"]]
        elif menu == "disk":
            rows = [["Max total", "Check interval"], ["Delete oldest"], ["Back"]]
        elif menu == "disk_maxsize":
            rows = [["0", "25", "50"], ["100", "200"], ["Custom", "Back"]]
        elif menu == "disk_interval":
            rows = [["30", "60", "120"], ["300"], ["Custom", "Back"]]
        else:  # add_channel, custom
            rows = [["Back"]]
        return ReplyKeyboardMarkup(
            rows, resize_keyboard=True,
            input_field_placeholder="Tap a button or type a command",
        )

    async def menu_text(self, menu="root", channel=None):
        """Status/instruction body shown above the reply keyboard for ``menu``."""
        c = self._config
        if menu == "root":
            return await self.handle_status()
        if menu == "channels":
            return (f"Channels ({len(c['channels'])}): {', '.join(c['channels'])}\n\n"
                    "Tap a channel to manage it, or add a new one.")
        if menu == "add_channel":
            return "Send the channel name to monitor (letters, numbers, underscores):"
        if menu == "channel":
            ch = channel
            override = (c.get("channel_output_modes") or {}).get(ch)
            mode = override or f"default (global: {c['output_mode']})"
            return (f"Channel: {ch}\nOutput mode: {mode}\n\n"
                    "Tap Delete to remove it, or set its output mode.")
        if menu == "chat":
            return f"Chat recording: {'on' if c.get('record_chat', True) else 'off'}. Choose:"
        if menu == "mode":
            return f"Output mode: {c['output_mode']}. Choose:"
        if menu == "quality":
            return f"Quality: {c.get('preferred_quality', 'best')}. Choose:"
        if menu == "retention":
            return f"Retention: {c['retention_days']} day(s) (0 = disabled). Choose:"
        if menu == "maxrec":
            return f"Max recordings: {c.get('max_concurrent_recordings', 0)} (0 = unlimited). Choose:"
        if menu == "maxyt":
            return f"Max YouTube re-streams: {c.get('max_concurrent_youtube_streams', 0)} (0 = unlimited). Choose:"
        if menu == "disk":
            return self.handle_disk([]) + "\n\nChoose a limit:"
        d = c.get("disk") or {}
        if menu == "disk_maxsize":
            return (f"Max total: {d.get('max_total_gb', 0):g} GB (0 = disabled)\n"
                    "Limits total recording size; when exceeded, the oldest recordings are deleted "
                    "(or recording stops). Choose:")
        if menu == "disk_interval":
            return (f"Check interval: {d.get('check_interval_s', 60):g} s\n"
                    "How often the disk limits are checked. Choose:")
        if menu == "custom":
            labels = {
                "retention": (f"Retention: {c['retention_days']} day(s) (0 = disabled)", " in days"),
                "maxrec": (f"Max recordings: {c.get('max_concurrent_recordings', 0)} (0 = unlimited)", ""),
                "maxyt": (f"Max YouTube re-streams: {c.get('max_concurrent_youtube_streams', 0)} (0 = unlimited)", ""),
                "disk_maxsize": (f"Max total: {d.get('max_total_gb', 0):g} GB (0 = disabled)", " in GB"),
                "disk_interval": (f"Check interval: {d.get('check_interval_s', 60):g} s", " in seconds"),
            }
            label, units = labels[self._custom_setting]
            return f"{label}. Send the new value{units}:"
        return await self.handle_status()

    async def handle_reply_text(self, text):
        """Route one reply-keyboard press or typed value.

        Returns ``(reply_text, reply_markup)`` for ``reply_text(..., reply_markup=)``
        (both markup kinds are valid there), or ``None`` to ignore the message.
        """
        if text == "Back":
            if self._menu == "custom":
                parent = "root" if self._custom_setting in ("retention", "maxrec", "maxyt") else "disk"
            else:
                parent = {
                    "channels": "root", "add_channel": "channels", "channel": "channels",
                    "chat": "root", "mode": "root", "quality": "root",
                    "retention": "root", "maxrec": "root", "maxyt": "root", "disk": "root",
                    "disk_maxsize": "disk", "disk_interval": "disk",
                }.get(self._menu)
            if parent is None:  # no Back button on root
                return None
            self._menu, self._menu_channel = parent, None
            return await self.menu_text(parent), self.reply_keyboard(parent)
        if self._menu == "add_channel":  # any text is a candidate channel name
            result = await self.handle_add([text])
            if result.startswith("\u274c") or result.startswith("Usage"):
                return result, self.reply_keyboard("add_channel")
            self._menu = "channels"
            return result, self.reply_keyboard("channels")
        if self._menu == "custom":  # any text is a value for _custom_setting
            setting = self._custom_setting
            if setting == "retention":
                result = self.handle_retention([text])
            elif setting == "maxrec":
                result = self.handle_maxrecordings([text])
            elif setting == "maxyt":
                result = self.handle_maxyoutube([text])
            else:
                cmd = {"disk_maxsize": "maxsize", "disk_interval": "interval"}[setting]
                result = self.handle_disk([cmd, text])
            if result.startswith("\u274c") or result.startswith("Usage"):
                return result, self.reply_keyboard("custom")
            parent = "root" if setting in ("retention", "maxrec", "maxyt") else "disk"
            self._menu = parent
            return result, self.reply_keyboard(parent)
        menu = self._menu
        if menu == "root":
            if text == "Status":
                return await self.handle_status(), self.reply_keyboard("root")
            new_menu = {
                "Channels": "channels", "Chat recording": "chat", "Output mode": "mode",
                "Quality": "quality", "Retention": "retention", "Max recordings": "maxrec",
                "Max YouTube": "maxyt", "Disk": "disk",
            }.get(text)
            if new_menu is None:
                return None
            self._menu = new_menu
            return await self.menu_text(new_menu), self.reply_keyboard(new_menu)
        if menu == "channels":
            if text == "Add channel":
                self._menu = "add_channel"
                return await self.menu_text("add_channel"), self.reply_keyboard("add_channel")
            if text.startswith("\u2022 ") and text[2:] in self._config["channels"]:
                self._menu, self._menu_channel = "channel", text[2:]
                return (await self.menu_text("channel", text[2:]),
                        self.reply_keyboard("channel"))
            return None
        if menu == "channel":
            ch = self._menu_channel
            if ch is None:
                return None
            if text == "Delete channel":
                return (f"Remove {ch} from monitoring? This stops any active recording "
                        f"and removes its output-mode override.",
                        self._confirm_keyboard("confirm_remove", ch))
            values = {"Mode: disk": "disk", "Mode: youtube": "youtube",
                      "Mode: both": "both", "Mode: default": "default"}
            if text in values:
                result = self.handle_mode([ch, values[text]])
                return result, self.reply_keyboard("channel")
            return None
        if menu == "chat":
            if text in ("On", "Off"):
                result = await self.handle_chat([text.lower()])
                self._menu = "root"
                return result, self.reply_keyboard("root")
            return None
        if menu == "mode":
            if text in ("disk", "youtube", "both"):
                result = self.handle_mode([text])
                self._menu = "root"
                return result, self.reply_keyboard("root")
            return None
        if menu == "quality":
            if text in _QUALITY_PRESETS:
                result = self.handle_quality([text])
                self._menu = "root"
                return result, self.reply_keyboard("root")
            return None
        if menu == "retention":
            values = {"1 day": "1", "3 days": "3", "7 days": "7",
                      "14 days": "14", "30 days": "30", "Off": "0"}
            if text in values:
                result = self.handle_retention([values[text]])
                self._menu = "root"
                return result, self.reply_keyboard("root")
            if text == "Custom":
                self._custom_setting, self._menu = "retention", "custom"
                return await self.menu_text("custom"), self.reply_keyboard("custom")
            return None
        if menu in ("maxrec", "maxyt"):
            values = {"0 (unlimited)": "0", "1": "1", "2": "2", "3": "3", "5": "5"}
            if text in values:
                handler = self.handle_maxrecordings if menu == "maxrec" else self.handle_maxyoutube
                result = handler([values[text]])
                self._menu = "root"
                return result, self.reply_keyboard("root")
            if text == "Custom":
                self._custom_setting, self._menu = menu, "custom"
                return await self.menu_text("custom"), self.reply_keyboard("custom")
            return None
        if menu == "disk":
            subs = {"Max total": "disk_maxsize", "Check interval": "disk_interval"}
            if text in subs:
                self._menu = subs[text]
                return await self.menu_text(subs[text]), self.reply_keyboard(subs[text])
            if text == "Delete oldest":
                if (self._config.get("disk") or {}).get("delete_oldest", True):
                    result = self.handle_disk(["delete_oldest", "off"])
                    return result, self.reply_keyboard("disk")
                return ("Enable 'delete oldest'? When the disk is over the max total, "
                        "the oldest recordings will be deleted.",
                        self._confirm_keyboard("confirm_delete_oldest", "on"))
            return None
        if menu in ("disk_maxsize", "disk_interval"):
            cmds = {"disk_maxsize": "maxsize", "disk_interval": "interval"}
            values = {
                "disk_maxsize": {"0": "0", "25": "25", "50": "50", "100": "100", "200": "200"},
                "disk_interval": {"30": "30", "60": "60", "120": "120", "300": "300"},
            }[menu]
            if text in values:
                result = self.handle_disk([cmds[menu], values[text]])
                self._menu = "disk"
                return result, self.reply_keyboard("disk")
            if text == "Custom":
                self._custom_setting, self._menu = menu, "custom"
                return await self.menu_text("custom"), self.reply_keyboard("custom")
            return None
        return None

    def _confirm_keyboard(self, action, value):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Confirm", callback_data=f"confirm_{action}:{value}"),
             InlineKeyboardButton("Cancel", callback_data="cancel")],
        ])

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
            "/disk <maxsize|interval|delete_oldest> <value> - set disk limit\n"
            "/chat [on|off] - enable or disable live chat recording (off stops in-flight capture)\n"
            "/settings - open the settings menu (reply keyboard buttons)\n"
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
        cap = c_disk.get("max_total_gb", 0)
        if cap > 0:
            if c_disk.get("delete_oldest", True):
                disk_limits.append(f"max {cap:g} GB (delete oldest when over)")
            else:
                disk_limits.append(f"max {cap:g} GB (stop recording when over)")
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
        usage = "Usage: /disk <maxsize|interval|delete_oldest> <value>"
        if not args:
            d = c.get("disk", {})
            return (
                "Disk limits:\n"
                f"max total: {d.get('max_total_gb', 0):g} GB (0 = disabled, delete oldest: {'on' if d.get('delete_oldest', True) else 'off'})\n"
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
        if cmd == "maxsize":
            return self._apply(
                lambda candidate: candidate.setdefault("disk", {}).__setitem__("max_total_gb", v),
                lambda candidate: f"Disk max total set to {v:g} GB",
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
        """Apply one confirmation-button press; returns (reply_text, markup) or None."""
        if data in self._confirm_done:  # double-tap guard
            return None
        action, _, value = data.partition(":")
        if action == "cancel":
            self._confirm_done.add(data)
            return "Cancelled \u2014 nothing changed", None
        if action == "confirm_remove" and value in self._config["channels"]:
            self._confirm_done.add(data)
            result = await self.handle_remove([value])  # stops recording + eventsub, clears override
            self._menu, self._menu_channel = "channels", None
            return result, None
        if action == "confirm_delete_oldest" and value == "on":
            self._confirm_done.add(data)
            result = self.handle_disk(["delete_oldest", "on"])
            self._menu = "disk"
            return result, None
        return None  # unknown -> ignore

    async def _on_callback(self, update, context):
        query = update.callback_query
        user = update.effective_user
        if user is None or user.id != self._admin_id:  # non-admin presses are ignored
            await query.answer()
            return
        result = await self.handle_callback(query.data)
        if result is None:
            await query.answer("Already processed" if query.data in self._confirm_done else "")
            return
        text, _ = result
        await query.answer()
        try:
            await query.edit_message_text(text, reply_markup=None)
        except BadRequest:  # message vanished mid-flight -> resend
            await context.bot.send_message(chat_id=query.from_user.id, text=text)
        await self._send_menu(context)

    async def _send_menu(self, context):
        """Re-render the reply keyboard for the current menu state."""
        await context.bot.send_message(
            chat_id=self._admin_id,
            text=await self.menu_text(self._menu, self._menu_channel),
            reply_markup=self.reply_keyboard(self._menu, self._menu_channel),
        )

    async def _on_text(self, update, context):
        result = await self.handle_reply_text(update.effective_message.text)
        if result is None:
            return
        text, markup = result
        await update.effective_message.reply_text(text, reply_markup=markup)

    async def _cmd_help(self, update, context):
        self._menu, self._menu_channel = "root", None
        await update.effective_message.reply_text(self.handle_help(), reply_markup=self.reply_keyboard("root"))

    async def _cmd_status(self, update, context):
        self._menu, self._menu_channel = "root", None
        await update.effective_message.reply_text(await self.handle_status(), reply_markup=self.reply_keyboard("root"))

    async def _cmd_settings(self, update, context):
        self._menu, self._menu_channel = "root", None
        await update.effective_message.reply_text(await self.menu_text("root"), reply_markup=self.reply_keyboard("root"))

    async def _cmd_start(self, update, context):
        self._menu, self._menu_channel = "root", None
        await update.effective_message.reply_text(self.handle_help(), reply_markup=self.reply_keyboard("root"))

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
