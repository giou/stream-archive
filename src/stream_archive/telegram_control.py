import asyncio
import base64
import copy
import json
import logging
import re
import secrets

from pathlib import Path
from urllib.parse import urlsplit

import httpx

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
from src.stream_archive.config import (
    is_kick_channel,
    kick_bare_name,
    normalize_channel_name,
    reload_config,
    save_config,
)

logger = logging.getLogger(__name__)

_QUALITY_PRESETS = ("best", "1080p", "720p", "480p", "360p")

_TAILSCALE_STATUS_TIMEOUT = 5    # `tailscale status --json` / serve status
_TAILSCALE_FUNNEL_TIMEOUT = 90   # first funnel enable provisions HTTPS certs (can take a minute)

_CLOUDFLARED_QUICK_TIMEOUT = 60   # wait for the trycloudflare URL after spawn
_CLOUDFLARED_RUN_TIMEOUT = 20     # wait for a named tunnel to register
_CLOUDFLARED_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_CLOUDFLARED_INSTALL_RE = re.compile(r"^cloudflared(?:\.exe)?\s+service\s+install\s+(\S+)\s*$")
_KICK_DASHBOARD_HINT = ("Paste this URL into the Kick app under "
                        "Settings \u2192 Developer \u2192 your app \u2192 Enable webhooks.")
_CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
_HOSTNAME_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def _decode_cloudflared_token(token):
    """Decode a cloudflared install token to its JSON payload (or None)."""
    padded = token + "=" * (-len(token) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return json.loads(decoder(padded))
        except Exception:
            continue
    return None


def _valid_cloudflare_token(token):
    """True when the token is cloudflared install credentials {a: account, t: tunnel, s: secret}."""
    data = _decode_cloudflared_token(token)
    return bool(
        isinstance(data, dict)
        and all(isinstance(data.get(k), str) and data[k] for k in ("a", "t", "s"))
    )


def _normalize_webhook_url(url):
    """Append the receiver path when the URL points at the host root."""
    if urlsplit(url).path in ("", "/"):
        return url.rstrip("/") + "/kick/webhook"
    return url


def _parse_public_hostname(text):
    """Extract a bare hostname (at least one dot) from user input; None when invalid."""
    text = text.strip()
    if re.match(r"^https?://", text):
        host = urlsplit(text).hostname
    else:
        host = text
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host if _HOSTNAME_RE.match(host) else None


class TelegramController:
    """Telegram control surface for the admin user (config['telegram_user_id']).

    All state changes go through ``_apply``: validate on a deep copy, persist
    atomically to config.json, then swap into the live dict. A failed command
    leaves both memory and disk untouched.
    """

    def __init__(self, config, recorder, monitor, eventsub, on_restart=None, updater=None, kick_webhook=None):
        self._config = config
        self._recorder = recorder
        self._monitor = monitor
        self._eventsub = eventsub
        self._on_restart = on_restart
        self._updater = updater
        self._kick_webhook = kick_webhook
        self._admin_id = config["telegram_user_id"]
        self._app = Application.builder().token(config["bot_telegram_api"]).build()
        self._menu = "root"        # current reply-keyboard menu
        self._menu_channel = None  # channel name when _menu == "channel"
        self._custom_setting = None  # parent setting when _menu == "custom"
        self._cloudflare_hostname = None  # hostname picked during the named-tunnel flow
        self._confirm_done = set()   # callback_data already confirmed (double-tap guard)
        self._cloudflared = None         # running cloudflared subprocess (quick or named)
        self._cloudflared_drain = None   # task draining its stdout so the pipe never fills

    def command_list(self):
        """BotCommand entries for the Telegram /-menu, shown only to the admin."""
        return [
            BotCommand("start", "Show available commands and open settings"),
            BotCommand("help", "Show available commands"),
            BotCommand("status", "Show current status and settings"),
            BotCommand("channels", "List monitored channels"),
            BotCommand("add", "Start monitoring a channel (twitch:name or kick:name)"),
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
        # Re-arm the /settings reply keyboard after a restart: keyboard buttons
        # are plain text routed by in-memory menu state, which resets on boot,
        # so a stale on-screen keyboard would be dead until /settings is typed.
        try:
            await self._app.bot.send_message(
                chat_id=self._admin_id,
                text=await self.menu_text("root"),
                reply_markup=self.reply_keyboard("root"),
            )
        except Exception:
            logger.warning("[telegram] Failed to re-send settings menu after restart", exc_info=True)
        w = (self._config.get("kick") or {}).get("webhook") or {}
        if w.get("enabled") and w.get("tunnel") == "cloudflare" and w.get("cloudflare_managed"):
            asyncio.create_task(self._restore_cloudflared())

    async def stop(self):
        self._cloudflared_stop()
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
                    ["Disk"], ["Kick webhook"]]
        elif menu == "channels":
            rows = [["Add channel"],
                    *([f"\u2022 {ch}"] for ch in self._config["channels"]),
                    ["Back"]]
        elif menu == "channel":
            rows = [["Delete channel"], ["Mode: disk", "Mode: youtube"],
                    ["Mode: both", "Mode: default"], ["Back"]]
        elif menu == "chat":
            rows = [["Twitch", "Kick"], ["Back"]]
        elif menu in ("chat_twitch", "chat_kick"):
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
            rows = [["Max total"], ["Delete oldest"], ["Back"]]
        elif menu == "disk_maxsize":
            rows = [["0", "25", "50"], ["100", "200"], ["Custom", "Back"]]
        elif menu == "kick_webhook":
            rows = [["Off", "Cloudflare tunnel"], ["Tailscale funnel"], ["Back"]]
        elif menu == "kick_cloudflare":
            rows = [["Quick tunnel", "Named tunnel"], ["Back"]]
        elif menu == "kick_cloudflare_dns":
            rows = [["Skip"], ["Back"]]
        elif menu in ("kick_cloudflare_token", "kick_cloudflare_hostname"):
            rows = [["Back"]]
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
            return ("Send the channel name or profile URL to monitor "
                    "(Twitch: twitch:<name> or https://twitch.tv/...; Kick: kick:<name> or https://kick.com/...):")
        if menu == "kick_webhook":
            return f"Kick webhook: {self._webhook_state_text()}\n\nChoose the tunnel you use to expose this service:"
        if menu == "kick_cloudflare":
            return ("Cloudflare tunnel\n\n"
                    "\u2022 Quick tunnel \u2014 no Cloudflare account needed, temporary URL.\n"
                    "\u2022 Named tunnel \u2014 your Cloudflare account, stable hostname.\n"
                    "\u2022 Already running your own tunnel? Send me its URL directly.")
        if menu == "kick_cloudflare_token":
            return ("Send your tunnel token:\n\n"
                    "cloudflared service install <TOKEN>\n\n"
                    "Paste the whole command or just the token \u2014 I'll run cloudflared for you.")
        if menu == "kick_cloudflare_hostname":
            return ("Send the public hostname to use for the webhook, e.g. kick.example.com.\n\n"
                    "I'll point your tunnel at this app automatically \u2014 no dashboard configuration needed.")
        if menu == "kick_cloudflare_dns":
            return ("Send your Cloudflare API token so I can create the DNS record automatically:\n\n"
                    "dash.cloudflare.com \u2192 My Profile \u2192 API Tokens \u2192 Create Token:\n"
                    "\u2022 Permissions: Zone \u2192 Read, DNS \u2192 Edit\n"
                    "\u2022 Zone Resources: Include \u2192 your domain (e.g. the part after the "
                    "dot of your hostname)\n"
                    "(the 'Edit zone DNS' template has exactly those two)\n\n"
                    "Or tap the Skip button to create the DNS record yourself \u2014 "
                    "I'll give you the exact record.")
        if menu == "channel":
            ch = channel
            override = (c.get("channel_output_modes") or {}).get(ch)
            mode = override or f"default (global: {c['output_mode']})"
            return (f"Channel: {ch}\nOutput mode: {mode}\n\n"
                    "Tap Delete to remove it, or set its output mode.")
        if menu == "chat":
            kick_chat = (c.get("kick") or {}).get("record_chat", True)
            return (f"Chat recording (Twitch): {'on' if c.get('record_chat', True) else 'off'}\n"
                    f"Kick chat recording: {'on' if kick_chat else 'off'}\n\nChoose a platform:")
        if menu == "chat_twitch":
            return f"Twitch chat recording: {'on' if c.get('record_chat', True) else 'off'}. Choose:"
        if menu == "chat_kick":
            kick_chat = (c.get("kick") or {}).get("record_chat", True)
            return f"Kick chat recording: {'on' if kick_chat else 'off'}. Choose:"
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
        if menu == "custom":
            labels = {
                "retention": (f"Retention: {c['retention_days']} day(s) (0 = disabled)", " in days"),
                "maxrec": (f"Max recordings: {c.get('max_concurrent_recordings', 0)} (0 = unlimited)", ""),
                "maxyt": (f"Max YouTube re-streams: {c.get('max_concurrent_youtube_streams', 0)} (0 = unlimited)", ""),
                "disk_maxsize": (f"Max total: {d.get('max_total_gb', 0):g} GB (0 = disabled)", " in GB"),
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
                    "chat": "root", "chat_twitch": "chat", "chat_kick": "chat",
                    "mode": "root", "quality": "root",
                    "retention": "root", "maxrec": "root", "maxyt": "root", "disk": "root",
                    "disk_maxsize": "disk",
                    "kick_webhook": "root", "kick_cloudflare": "kick_webhook",
                    "kick_cloudflare_token": "kick_cloudflare",
                    "kick_cloudflare_hostname": "kick_cloudflare_token",
                    "kick_cloudflare_dns": "kick_cloudflare_hostname",
                }.get(self._menu)
            if parent is None:  # no Back button on root
                return None
            if self._menu in ("kick_cloudflare_hostname", "kick_cloudflare_dns"):
                self._cloudflare_hostname = None
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
                result = self.handle_disk(["maxsize", text])
            if result.startswith("\u274c") or result.startswith("Usage"):
                return result, self.reply_keyboard("custom")
            parent = "root" if setting in ("retention", "maxrec", "maxyt") else "disk"
            self._menu = parent
            return result, self.reply_keyboard(parent)
        if self._menu == "kick_cloudflare_token":  # any text is a token candidate
            ok, message = await self._handle_cloudflare_token(text)
            if not ok:
                return message, self.reply_keyboard("kick_cloudflare_token")
            self._menu = "kick_cloudflare_hostname"
            return message, self.reply_keyboard("kick_cloudflare_hostname")
        if self._menu == "kick_cloudflare_hostname":  # any text is a hostname candidate
            host = _parse_public_hostname(text)
            if host is None:
                return ("\u274c That doesn't look like a public hostname "
                        "(e.g. kick.example.com).", self.reply_keyboard("kick_cloudflare_hostname"))
            self._cloudflare_hostname = host
            self._menu = "kick_cloudflare_dns"
            return (f"Hostname {host} \u2014 " + await self.menu_text("kick_cloudflare_dns"),
                    self.reply_keyboard("kick_cloudflare_dns"))
        if self._menu == "kick_cloudflare_dns":  # API token or 'skip'
            if text.strip().lower() == "skip":
                return await self._finish_named_setup(dns_note=None)
            ok, message = await self._create_cloudflare_dns(text.strip())
            if not ok:
                return message, self.reply_keyboard("kick_cloudflare_dns")
            return await self._finish_named_setup(dns_note=message)
        menu = self._menu
        if menu == "root":
            if text == "Status":
                return await self.handle_status(), self.reply_keyboard("root")
            new_menu = {
                "Channels": "channels", "Chat recording": "chat", "Output mode": "mode",
                "Quality": "quality", "Retention": "retention", "Max recordings": "maxrec",
                "Max YouTube": "maxyt", "Disk": "disk", "Kick webhook": "kick_webhook",
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
            if text in ("Twitch", "Kick"):
                self._menu = "chat_twitch" if text == "Twitch" else "chat_kick"
                return await self.menu_text(self._menu), self.reply_keyboard(self._menu)
            return None
        if menu in ("chat_twitch", "chat_kick"):
            if text in ("On", "Off"):
                platform = "twitch" if menu == "chat_twitch" else "kick"
                result = await self.handle_chat([text.lower(), platform])
                self._menu = "chat"
                return result, self.reply_keyboard("chat")
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
            if text == "Max total":
                self._menu = "disk_maxsize"
                return await self.menu_text("disk_maxsize"), self.reply_keyboard("disk_maxsize")
            if text == "Delete oldest":
                if (self._config.get("disk") or {}).get("delete_oldest", True):
                    result = self.handle_disk(["delete_oldest", "off"])
                    return result, self.reply_keyboard("disk")
                return ("Enable 'delete oldest'? When the disk is over the max total, "
                        "the oldest recordings will be deleted.",
                        self._confirm_keyboard("confirm_delete_oldest", "on"))
            return None
        if menu == "disk_maxsize":
            values = {"0": "0", "25": "25", "50": "50", "100": "100", "200": "200"}
            if text in values:
                result = self.handle_disk(["maxsize", values[text]])
                self._menu = "disk"
                return result, self.reply_keyboard("disk")
            if text == "Custom":
                self._custom_setting, self._menu = menu, "custom"
                return await self.menu_text("custom"), self.reply_keyboard("custom")
            return None
        if menu == "kick_webhook":
            port = (self._config.get("kick") or {}).get("webhook", {}).get("listen_port", 8787)
            if text == "Off":
                result = await self._apply_webhook_state(False, "")
                self._menu = "root"
                return result, self.reply_keyboard("root")
            if text == "Cloudflare tunnel":
                self._menu = "kick_cloudflare"
                return await self.menu_text("kick_cloudflare"), self.reply_keyboard("kick_cloudflare")
            if text == "Tailscale funnel":
                old_tunnel = (self._config.get("kick") or {}).get("webhook", {}).get("tunnel") or ""
                url, hint = await self._tailscale_webhook_url()
                if url is not None:
                    result = await self._apply_webhook_state(True, url, "tailscale")
                    self._menu = "kick_webhook"
                    note = await self._reachability_note(url, "tailscale")
                    msg = (f"{result}\n\n```\n{url}\n```\n"
                           f"tailscale funnel {port} is enabled on this host.\n"
                           + _KICK_DASHBOARD_HINT + note)
                    if old_tunnel == "cloudflare":
                        msg += "\nYour cloudflared tunnel has been stopped."
                    return msg, self.reply_keyboard("kick_webhook")
                self._menu = "kick_cloudflare"
                return (f"{hint}\n\n"
                        "Fix tailscale and tap Tailscale funnel again, or use Cloudflare tunnel instead.",
                        self.reply_keyboard("kick_cloudflare"))
            return None
        if menu == "kick_cloudflare":
            if re.match(r"^https?://", text):  # own tunnel already running
                return await self._apply_cloudflare_url(text)
            if text == "Quick tunnel":
                url, hint = await self._cloudflared_quick_start()
                if url is None:
                    return f"\u274c {hint}", self.reply_keyboard("kick_cloudflare")
                result = await self._apply_webhook_state(True, url, "cloudflare", cloudflare_managed=True)
                self._menu = "kick_webhook"
                note = await self._reachability_note(url, "cloudflare")
                return (f"{result}\n\n```\n{url}\n```\n"
                        "cloudflared quick tunnel is running on this host.\n"
                        + _KICK_DASHBOARD_HINT + note,
                        self.reply_keyboard("kick_webhook"))
            if text == "Named tunnel":
                self._menu = "kick_cloudflare_token"
                return await self.menu_text("kick_cloudflare_token"), self.reply_keyboard("kick_cloudflare_token")
            return None
        return None

    def _confirm_keyboard(self, action, value):
        # Nonce makes the callback data unique per confirm message, so the
        # double-tap guard never swallows a later confirm/cancel on a new message.
        nonce = secrets.token_hex(4)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Confirm", callback_data=f"{action}:{value}:{nonce}"),
             InlineKeyboardButton("Cancel", callback_data=f"cancel:{nonce}")],
        ])

    def handle_help(self):
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

    def _webhook_state_text(self):
        w = (self._config.get("kick") or {}).get("webhook") or {}
        if not w.get("enabled"):
            return "off"
        tunnel = w.get("tunnel") or ""
        url = w.get("public_url", "")
        return f"on ({tunnel} \u00b7 {url})" if tunnel else f"on ({url})"

    async def handle_status(self):
        c = self._config
        active = self._recorder.recording_info()
        disk_snap = await self._recorder.disk_snapshot()
        c_disk = c.get("disk", {})
        days = c["retention_days"]
        retention = f"Retention: {days} day" + ("s" if days != 1 else "") if days else "Retention: disabled"
        chat_state = "enabled" if c.get("record_chat", True) else "disabled"
        k = c.get("kick") or {}
        webhook_state = self._webhook_state_text()
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
            f"Kick chat recording: {'enabled' if k.get('record_chat', True) else 'disabled'}\n"
            f"Kick webhook: {webhook_state}\n"
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
        ch = normalize_channel_name(args[0])
        if ch is None:
            return f"\u274c Invalid channel name: {args[0]!r} (use twitch:<name> for Twitch or kick:<name> for Kick)"

        def mutate(candidate):
            if ch in candidate["channels"]:
                raise ValueError(f"{ch} is already monitored")
            candidate["channels"].append(ch)

        result = self._apply(mutate, lambda c: f"Added {ch} \u2014 {len(c['channels'])} channel(s) monitored")
        if not result.startswith("\u274c"):
            if is_kick_channel(ch):
                if self._kick_webhook:
                    await self._kick_webhook.add_channel(ch)
            else:
                await self._eventsub.add_channel(ch)
        return result

    async def handle_remove(self, args):
        if len(args) != 1:
            return "Usage: /remove <channel>"
        ch = normalize_channel_name(args[0])
        if ch is None:
            return f"\u274c Invalid channel name: {args[0]!r} (use twitch:<name> for Twitch or kick:<name> for Kick)"

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
            if is_kick_channel(ch):
                if self._kick_webhook:
                    await self._kick_webhook.remove_channel(ch)
            else:
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
            normalized = normalize_channel_name(ch)
            if normalized is None:
                return f"\u274c Invalid channel name: {ch!r} (use twitch:<name> for Twitch or kick:<name> for Kick)"
            ch = normalized
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
        if self._kick_webhook:
            await self._kick_webhook.sync_channels(self._config["channels"])
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
        usage = "Usage: /disk <maxsize|delete_oldest> <value>"
        if not args:
            d = c.get("disk", {})
            return (
                "Disk limits:\n"
                f"max total: {d.get('max_total_gb', 0):g} GB (0 = disabled, delete oldest: {'on' if d.get('delete_oldest', True) else 'off'})"
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
        return usage

    async def handle_chat(self, args):
        if not args:
            twitch_state = "enabled" if self._config.get("record_chat", True) else "disabled"
            kick_state = "enabled" if (self._config.get("kick") or {}).get("record_chat", True) else "disabled"
            return f"Chat recording: {twitch_state}\nKick chat recording: {kick_state}"
        if len(args) == 1 and args[0].lower() in ("on", "off"):
            enabled = args[0].lower() == "on"

            def mutate(candidate):
                candidate["record_chat"] = enabled
                candidate.setdefault("kick", {})["record_chat"] = enabled

            text = self._apply(
                mutate,
                lambda candidate: f"Chat recording {'enabled' if enabled else 'disabled'}",
            )
            if not enabled and not text.startswith("\u274c"):
                for channel in self._recorder.active_channels():
                    await self._recorder.stop_chat(channel)
            return text
        if len(args) == 2 and args[0].lower() in ("on", "off") and args[1].lower() in ("twitch", "kick"):
            enabled = args[0].lower() == "on"
            platform = args[1].lower()

            def mutate(candidate):
                if platform == "twitch":
                    candidate["record_chat"] = enabled
                else:
                    candidate.setdefault("kick", {})["record_chat"] = enabled

            label = "Twitch chat recording" if platform == "twitch" else "Kick chat recording"
            text = self._apply(
                mutate,
                lambda candidate: f"{label} {'enabled' if enabled else 'disabled'}",
            )
            if not enabled and not text.startswith("\u274c"):
                for channel in self._recorder.active_channels():
                    if platform == "twitch" and not is_kick_channel(channel):
                        await self._recorder.stop_chat(channel, "twitch")
                    elif platform == "kick" and is_kick_channel(channel):
                        await self._recorder.stop_chat(channel, "kick")
            return text
        return "Usage: /chat <on|off> [twitch|kick]"

    async def _tailscale_webhook_url(self):
        """Detect tailscale, enable a funnel for the webhook port, return its public URL.

        Returns (url, None) on success, or (None, hint) with a user-facing
        explanation when tailscale is missing or unusable. Never raises.
        """
        port = (self._config.get("kick") or {}).get("webhook", {}).get("listen_port", 8787)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TAILSCALE_STATUS_TIMEOUT)
        except FileNotFoundError:
            return None, (
                "Tailscale is not installed in this container.\n"
                "Install it on the host: curl -fsSL https://tailscale.com/install.sh | sh\n"
                "then log in: tailscale up"
            )
        except asyncio.TimeoutError:
            await self._kill_proc(proc)
            return None, "tailscale status timed out \u2014 is the tailscale daemon running on the host?"
        if proc.returncode != 0:
            return None, (
                "tailscale status failed (daemon not running or not logged in): "
                + (stderr.decode(errors="replace").strip() or f"exit {proc.returncode}")
            )
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return None, "tailscale status returned unparseable output"
        dns_name = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
        if not dns_name:
            return None, "tailscale status shows no machine DNS name \u2014 is this machine in a tailnet?"
        proc = None
        try:
            # --bg: register the funnel with the daemon and exit (the plain form
            # serves in the foreground and never returns); --yes: no interactive
            # prompts (which would hang a piped subprocess).
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "funnel", "--bg", "--yes", str(port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TAILSCALE_FUNNEL_TIMEOUT)
        except FileNotFoundError:
            return None, "Tailscale is not installed in this container."
        except asyncio.TimeoutError:
            await self._kill_proc(proc)
            return None, (
                "tailscale funnel timed out (first enable provisions HTTPS certificates and can take "
                "a minute) \u2014 tap Tailscale funnel again in a moment."
            )
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            # Already enabled (e.g. a previous attempt finished after its timeout,
            # or the user re-clicks the menu): verify the funnel really serves
            # our port before reporting success.
            if "listener already exists" not in stderr_text or not await self._funnel_serving(port):
                return None, (
                    f"tailscale funnel {port} failed: "
                    + (stderr_text or f"exit {proc.returncode}")
                )
        return f"https://{dns_name}/kick/webhook", None

    async def _funnel_serving(self, port):
        """True when a foreground tailscale funnel config proxies / to 127.0.0.1:<port>."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "serve", "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_TAILSCALE_STATUS_TIMEOUT)
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            return False
        if proc.returncode != 0:
            return False
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return False
        target = f"http://127.0.0.1:{port}"
        for fg in (data.get("Foreground") or {}).values():
            for host in (fg.get("Web") or {}).values():
                for handler in (host.get("Handlers") or {}).values():
                    if handler.get("Proxy") == target:
                        return True
        return False

    async def _kill_proc(self, proc):
        if proc is None:
            return
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass

    async def _cloudflared_quick_start(self):
        """Run a cloudflared quick tunnel; return (url, None) or (None, hint).

        The spawned process is kept as the managed tunnel; callers enable the
        webhook with the published trycloudflare URL.
        """
        self._cloudflared_stop()
        port = (self._config.get("kick") or {}).get("webhook", {}).get("listen_port", 8787)
        try:
            proc = await asyncio.create_subprocess_exec(
                # --no-autoupdate is a root flag: it must precede the subcommand.
                "cloudflared", "--no-autoupdate", "tunnel", "--url", f"http://127.0.0.1:{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError:
            return None, (
                "cloudflared is not installed in this container.\n"
                "Rebuild the image (docker compose up -d --build) after adding cloudflared."
            )
        try:
            url, tail = await asyncio.wait_for(
                self._wait_cloudflared_url(proc), timeout=_CLOUDFLARED_QUICK_TIMEOUT
            )
        except asyncio.TimeoutError:
            await self._kill_proc(proc)
            return None, (
                "cloudflared did not publish a trycloudflare URL within "
                f"{_CLOUDFLARED_QUICK_TIMEOUT}s \u2014 tap Quick tunnel again."
            )
        if url is None:
            await self._kill_proc(proc)
            return None, "cloudflared exited before publishing a URL:\n" + "\n".join(tail[-8:])
        self._cloudflared = proc
        self._cloudflared_drain = asyncio.create_task(self._drain_cloudflared(proc))
        return _normalize_webhook_url(url), None

    async def _cloudflared_named_start(self, token, config_path=None):
        """Run ``cloudflared tunnel run --token``; return (True, None) or (False, hint).

        With ``config_path`` the local ingress file is used (no dashboard
        configuration needed). Flag order matters: ``--no-autoupdate`` and
        ``--config`` are ``tunnel``-command options and must precede ``run``.
        """
        self._cloudflared_stop()
        cmd = ["cloudflared", "tunnel", "--no-autoupdate"]
        if config_path is not None:
            cmd += ["--config", str(config_path)]
        cmd += ["run", "--token", token]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError:
            return False, (
                "cloudflared is not installed in this container.\n"
                "Rebuild the image (docker compose up -d --build) after adding cloudflared."
            )
        try:
            registered, tail = await asyncio.wait_for(
                self._wait_cloudflared_registered(proc), timeout=_CLOUDFLARED_RUN_TIMEOUT
            )
        except asyncio.TimeoutError:
            if proc.returncode is None:  # still running: connection registered
                self._cloudflared = proc
                self._cloudflared_drain = asyncio.create_task(self._drain_cloudflared(proc))
                return True, None
            await self._kill_proc(proc)
            return False, "cloudflared exited during startup."
        if not registered:
            await self._kill_proc(proc)
            return False, "cloudflared exited:\n" + "\n".join(tail[-8:])
        self._cloudflared = proc
        self._cloudflared_drain = asyncio.create_task(self._drain_cloudflared(proc))
        return True, None

    async def _wait_cloudflared_url(self, proc):
        """Read cloudflared output until the trycloudflare URL or EOF; (url, tail_lines)."""
        tail = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                return None, tail
            decoded = line.decode(errors="replace").strip()
            tail.append(decoded)
            m = _CLOUDFLARED_URL_RE.search(decoded)
            if m:
                return m.group(0), tail

    async def _wait_cloudflared_registered(self, proc):
        """Read cloudflared output until the named tunnel registers or EOF; (ok, tail_lines)."""
        tail = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                return False, tail
            decoded = line.decode(errors="replace").strip()
            tail.append(decoded)
            if "Registered tunnel connection" in decoded:
                return True, tail

    async def _drain_cloudflared(self, proc):
        """Discard cloudflared output so its pipe never fills and blocks the tunnel."""
        try:
            while await proc.stdout.readline():
                pass
        except Exception:
            pass

    def _cloudflared_stop(self):
        """Kill the managed cloudflared subprocess and its drain task (idempotent)."""
        drain, self._cloudflared_drain = self._cloudflared_drain, None
        proc, self._cloudflared = self._cloudflared, None
        if drain is not None:
            drain.cancel()
        if proc is None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    async def _restore_cloudflared(self):
        """Restart an app-managed cloudflared after a service restart (webhook enabled)."""
        try:
            w = (self._config.get("kick") or {}).get("webhook") or {}
            if not (w.get("enabled") and w.get("tunnel") == "cloudflare" and w.get("cloudflare_managed")):
                return
            token = w.get("cloudflare_token") or ""
            if token:
                host = urlsplit(w.get("public_url") or "").hostname
                cfg = await self._write_cloudflared_config(host) if host else None
                ok, hint = await self._cloudflared_named_start(token, config_path=cfg)
                if not ok:
                    await self._send_admin(f"\u274c cloudflared failed to restart your named tunnel:\n{hint}")
                return
            url, hint = await self._cloudflared_quick_start()
            if url is None:
                await self._send_admin(f"\u274c cloudflared quick tunnel failed to restart:\n{hint}")
                return
            if url != w.get("public_url"):
                self._apply(
                    lambda candidate: candidate.setdefault("kick", {}).setdefault("webhook", {})
                                       .update({"public_url": url, "setup_notified": False}),
                    lambda c: "public_url updated",
                )
                note = await self._reachability_note(url, "cloudflare")
                await self._send_admin(
                    "\U0001f4a1 Your cloudflared quick tunnel restarted with a new temporary URL:\n\n"
                    f"```\n{url}\n```\n"
                    "The previous trycloudflare URL expired. " + _KICK_DASHBOARD_HINT + note
                )
        except Exception:
            logger.exception("[telegram] cloudflared restore failed")

    async def _send_admin(self, text):
        try:
            await self._app.bot.send_message(chat_id=self._admin_id, text=text)
        except Exception:
            logger.warning("[telegram] Failed to notify admin", exc_info=True)

    async def _handle_cloudflare_token(self, text):
        """Validate a pasted cloudflared token/command and persist it.

        Returns (True, message) with the next-step prompt, or (False, error).
        """
        text = text.strip()
        m = _CLOUDFLARED_INSTALL_RE.match(text)
        token = m.group(1) if m else text
        if not _valid_cloudflare_token(token):
            return False, ("\u274c That doesn't look like a cloudflared tunnel token.\n\n"
                           "Send the token from the Cloudflare dashboard command "
                           "(cloudflared service install <TOKEN>) \u2014 or paste the whole command.")
        result = self._apply(
            lambda candidate: candidate.setdefault("kick", {}).setdefault("webhook", {})
                               .__setitem__("cloudflare_token", token),
            lambda c: "token saved",
        )
        if result.startswith("\u274c"):
            return False, result
        return True, ("\u2705 Tunnel token accepted.\n\n"
                      "Send the public hostname to use for the webhook, e.g. kick.example.com \u2014 "
                      "I'll point your tunnel at this app automatically.")

    async def _write_cloudflared_config(self, host):
        """Write the local ingress config for the named tunnel; returns its path."""
        wh = (self._config.get("kick") or {}).get("webhook") or {}
        port = wh.get("listen_port", 8787)
        data = _decode_cloudflared_token(wh.get("cloudflare_token") or "")
        tunnel_id = (data or {}).get("t") or "tunnel"
        directory = Path(self._config.get("_workdir", ".")) / "cloudflared"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{tunnel_id}.yml"
        path.write_text(
            "ingress:\n"
            f"  - hostname: {host}\n"
            f"    service: http://127.0.0.1:{port}\n"
            "  - service: http_status:404\n"
        )
        return path

    async def _create_cloudflare_dns(self, api_token):
        """Create the CNAME for the named tunnel's hostname via the Cloudflare API.

        Returns (True, message) on success (or when the record already points
        at the tunnel), (False, error) otherwise. Never raises.
        """
        host = self._cloudflare_hostname or ""
        wh = (self._config.get("kick") or {}).get("webhook") or {}
        data = _decode_cloudflared_token(wh.get("cloudflare_token") or "")
        tunnel_id = (data or {}).get("t") or ""
        account_id = (data or {}).get("a") or ""
        if not host or not tunnel_id:
            return False, "\u274c Missing hostname or tunnel token \u2014 start the Named tunnel flow again."
        headers = {"Authorization": f"Bearer {api_token}"}
        target = f"{tunnel_id}.cfargotunnel.com"
        try:
            async with httpx.AsyncClient(timeout=15, base_url=_CLOUDFLARE_API) as client:
                # Account-owned tokens (cfat_ prefix) reject the user-scoped
                # verify endpoint, so fall back to the account-scoped one.
                verify = await client.get("/user/tokens/verify", headers=headers)
                if verify.status_code != 200 and account_id:
                    verify = await client.get(f"/accounts/{account_id}/tokens/verify", headers=headers)
                if verify.status_code != 200 or (verify.json().get("result") or {}).get("status") != "active":
                    return False, "\u274c That Cloudflare API token is not valid."
                zones_resp = await client.get("/zones?per_page=50", headers=headers)
                if zones_resp.status_code != 200:
                    return False, ("\u274c The token can't list zones \u2014 it needs Zone read "
                                   "(use the 'Edit zone DNS' template).")
                zone = None
                for z in zones_resp.json().get("result") or []:
                    name = (z.get("name") or "").lower()
                    if host == name or host.endswith("." + name):
                        if zone is None or len(name) > len(zone["name"]):
                            zone = z
                if zone is None:
                    return False, (f"\u274c No Cloudflare zone matches {host} \u2014 is the domain "
                                   "on the Cloudflare account of this API token?")
                zone_id = zone["id"]
                existing_resp = await client.get(
                    f"/zones/{zone_id}/dns_records?name={host}&type=CNAME", headers=headers
                )
                existing = (existing_resp.json().get("result") or []) if existing_resp.status_code == 200 else []
                if existing:
                    if existing[0].get("content") == target:
                        return True, "\u2705 DNS record already points at your tunnel."
                    return False, (f"\u274c {host} is already used by another DNS record "
                                   f"({existing[0].get('content')}).")
                created = await client.post(
                    f"/zones/{zone_id}/dns_records", headers=headers,
                    json={"type": "CNAME", "name": host, "content": target, "proxied": True},
                )
                if created.status_code not in (200, 201):
                    err = (created.json().get("errors") or [{}])[0].get("message", created.text)
                    return False, f"\u274c Could not create the DNS record: {err}"
        except httpx.HTTPError as e:
            return False, f"\u274c Cloudflare API request failed: {e}"
        return True, "\u2705 DNS record created \u2014 the hostname now points at your tunnel."

    async def _finish_named_setup(self, dns_note):
        """Wire up the named tunnel: local ingress config, run, enable the webhook.

        ``dns_note`` is the DNS success message, or None when the user chose
        'skip' (then the manual CNAME instructions are included instead).
        """
        host = self._cloudflare_hostname or ""
        self._cloudflare_hostname = None
        if not host:
            return "\u274c No hostname \u2014 start the Named tunnel flow again.", self.reply_keyboard("kick_cloudflare")
        wh = (self._config.get("kick") or {}).get("webhook") or {}
        token = wh.get("cloudflare_token") or ""
        cfg = await self._write_cloudflared_config(host)
        ok, hint = await self._cloudflared_named_start(token, config_path=cfg)
        if not ok:
            return f"\u274c cloudflared failed to start:\n{hint}", self.reply_keyboard("kick_cloudflare_dns")
        url = _normalize_webhook_url(f"https://{host}")
        result = await self._apply_webhook_state(True, url, "cloudflare",
                                                 cloudflare_token=token, cloudflare_managed=True)
        if result.startswith("\u274c"):
            return result, self.reply_keyboard("kick_cloudflare")
        if dns_note is None:
            data = _decode_cloudflared_token(token)
            tunnel_id = (data or {}).get("t") or "your-tunnel"
            dns_note = ("\n\nOne last step: add this DNS record in the Cloudflare dashboard "
                        "(DNS \u2192 Records \u2192 Add record):\n"
                        f"CNAME {host} \u2192 {tunnel_id}.cfargotunnel.com (proxied).\n"
                        "The webhook only starts receiving events once the record resolves.")
        note = await self._reachability_note(url, "cloudflare")
        self._menu = "kick_webhook"
        return (f"{result}\n\n```\n{url}\n```\n" + _KICK_DASHBOARD_HINT + "\n" + dns_note + note,
                self.reply_keyboard("kick_webhook"))

    async def _apply_cloudflare_url(self, text):
        """Enable the webhook with a pasted URL of the user's own (external) tunnel.

        The tunnel is not app-managed: it is never restarted on boot.
        """
        url = _normalize_webhook_url(text.strip())
        result = await self._apply_webhook_state(True, url, "cloudflare")
        if result.startswith("\u274c"):
            return result, self.reply_keyboard(self._menu)
        note = await self._reachability_note(url, "cloudflare")
        self._menu = "kick_webhook"
        return (f"{result}\n\n```\n{url}\n```\n" + _KICK_DASHBOARD_HINT + note,
                self.reply_keyboard("kick_webhook"))

    async def _probe_webhook_url(self, url):
        """True when the public URL answers an HTTP request (tunnel + DNS work).

        Any response counts \u2014 including 4xx from the receiver \u2014 because the
        point is that the request reached the app through the tunnel.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(url)
            return True
        except httpx.HTTPError:
            return False

    async def _reachability_note(self, url, tunnel=""):
        """Probe the public URL and return a user-facing status line.

        Tailscale funnels are exempt: the funnel was just verified against the
        host's tailscaled, and containers cannot reach the host's tailnet IP
        (Docker hairpin), so a probe would always fail there.
        """
        if tunnel == "tailscale":
            return ""
        if await self._probe_webhook_url(url):
            return ("\n\n\u2705 URL is reachable \u2014 save it in Kick and I'll confirm "
                    "when the first event arrives.")
        return ("\n\n\u26a0\ufe0f The URL doesn't respond yet \u2014 if you skipped the DNS step, "
                "add the DNS record first; otherwise check the tunnel logs.")

    async def _tailscale_funnel_off(self):
        """Turn off the app-managed tailscale funnel for the webhook port (best effort).

        Newer tailscale CLIs reject ``--bg <port> off``; the documented form is
        ``tailscale funnel --https=443 off`` (funnels only ever listen on 443).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "funnel", "--https=443", "off",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=_TAILSCALE_STATUS_TIMEOUT)
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            return False
        return proc.returncode == 0

    async def _apply_webhook_state(self, enabled, url, tunnel="", cloudflare_token="", cloudflare_managed=False):
        """Persist kick.webhook.{enabled,public_url,tunnel,...} and reconcile live state.

        Kick has a single webhook URL, so only one tunnel may expose the
        receiver: enabling a different provider \u2014 or disabling \u2014 tears down the
        previously app-managed tunnel (the tailscale funnel for the webhook
        port, or the cloudflared subprocess). ``cloudflare_managed`` marks a
        cloudflare tunnel the app started itself (restored on boot); a pasted
        URL with no token is the user's own tunnel and is never restarted.
        """
        wh = (self._config.get("kick") or {}).get("webhook") or {}
        old_tunnel = wh.get("tunnel") or ""
        result = self._apply(
            lambda candidate: (candidate.setdefault("kick", {}).setdefault("webhook", {})
                               .update({"enabled": enabled, "public_url": url,
                                        "tunnel": tunnel if enabled else "",
                                        "cloudflare_token": cloudflare_token if enabled else "",
                                        "cloudflare_managed": cloudflare_managed if enabled else False,
                                        # Re-arm the "webhook is working" confirmation: it
                                        # fires on the first verified Kick event, so a
                                        # re-enable (new tunnel/URL) confirms again.
                                        "setup_notified": False if enabled else wh.get("setup_notified", True)})),
            lambda c: f"Kick webhook {'enabled' if enabled else 'disabled'}",
        )
        if result.startswith("\u274c"):
            return result
        if self._kick_webhook is not None:
            if enabled:
                await self._kick_webhook.start()          # idempotent
                await self._kick_webhook.sync_channels(self._config["channels"])
            else:
                await self._kick_webhook.close()          # idempotent
        if old_tunnel == "tailscale" and tunnel != "tailscale":
            if not await self._tailscale_funnel_off():
                logger.warning("[telegram] Could not turn off the tailscale funnel for the webhook port")
        if old_tunnel == "cloudflare" and tunnel != "cloudflare":
            self._cloudflared_stop()
        return result

    async def handle_callback(self, data):
        """Apply one confirmation-button press; returns (reply_text, markup) or None.

        Wire format (from ``_confirm_keyboard``): ``confirm_<action>:<value>:<nonce>``
        and ``cancel:<nonce>``. The nonce makes every confirm message's buttons
        unique, so the double-tap guard only ever guards the same message.
        """
        parts = data.split(":")
        action = parts[0]
        if action == "cancel" and len(parts) == 2:
            if data in self._confirm_done:  # double-tap on the same message
                return None
            self._confirm_done.add(data)
            return "Cancelled \u2014 nothing changed", None
        if action == "confirm_remove" and len(parts) >= 3:
            if data in self._confirm_done:  # double-tap on the same message
                return None
            # The channel sits between the action and the nonce; it may itself
            # contain ':' (kick:<slug>), so rejoin the middle parts.
            value = ":".join(parts[1:-1])
            if value not in self._config["channels"]:
                return f"{value} is no longer monitored", None  # stale confirm message
            self._confirm_done.add(data)
            result = await self.handle_remove([value])  # stops recording + eventsub, clears override
            self._menu, self._menu_channel = "channels", None
            return result, None
        if action == "confirm_delete_oldest" and len(parts) == 3 and parts[1] == "on":
            if data in self._confirm_done:  # double-tap on the same message
                return None
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
        try:
            result = await self.handle_callback(query.data)
        except Exception:
            logger.exception("[telegram] Callback %s failed", query.data)
            error_text = "\u274c Unexpected error \u2014 see logs"
            try:
                await query.answer()
            except BadRequest:
                pass
            try:
                await query.edit_message_text(error_text, reply_markup=None)
            except BadRequest:  # confirm message vanished -> send a fresh one
                await context.bot.send_message(chat_id=query.from_user.id, text=error_text)
            return
        if result is None:  # double-tap or unknown data: silent ack, no toast
            try:
                await query.answer()
            except BadRequest:
                pass
            return
        text, _ = result
        try:
            await query.answer()
        except BadRequest:
            pass
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
