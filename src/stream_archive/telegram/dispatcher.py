import asyncio
import contextlib
import logging
import re
import secrets
from collections.abc import Callable
from typing import Any

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

from stream_archive.config import (
    AppConfig,
    _replace_in_place,
    effective_quality,
    is_kick_channel,
    save_config,
)
from stream_archive.telegram.commands_channels import ChannelsCommands
from stream_archive.telegram.commands_settings import _QUALITY_PRESETS, SettingsCommands
from stream_archive.telegram.commands_system import SystemCommands
from stream_archive.telegram.commands_webhook import _KICK_DASHBOARD_HINT, WebhookCommands, _parse_public_hostname

logger = logging.getLogger(__name__)


def _deferred_affected_channels(new: AppConfig, recordings: dict[str, dict[str, Any]]) -> list[str]:
    """Active channels whose in-flight recording a config change affects.

    The check compares against the settings each recording actually uses,
    snapshotted at recording start, not against the previous config. After
    a declined change the config already holds the new value while the
    recording keeps the old settings, so the same change must warn again.
    Deferred effects: output mode (global or per-channel override),
    preferred quality, and chat capture enabled (disabling chat stops an
    in-flight capture immediately and never warns).
    """
    affected: set[str] = set()
    for ch, rec in recordings.items():
        if (
            rec.get("output_mode") != new.channel_output_modes.get(ch, new.output_mode)
            or rec.get("preferred_quality") != effective_quality(new, ch)
            or (not is_kick_channel(ch) and new.record_chat and not rec.get("record_chat"))
            or (is_kick_channel(ch) and new.kick.record_chat and not rec.get("kick_record_chat"))
        ):
            affected.add(ch)
    return sorted(affected)


class TelegramController(ChannelsCommands, SettingsCommands, WebhookCommands, SystemCommands):
    _config: AppConfig
    _recorder: Any
    _monitor: Any
    _eventsub: Any
    _on_restart: Callable[[], None] | None
    _updater: Any
    _kick_webhook: Any
    _app: Application[Any, Any, Any, Any, Any, Any]
    _admin_id: int
    _menu: str
    _menu_channel: str | None
    _custom_setting: str | None
    _cloudflare_hostname: str | None
    _confirm_done: set[str]
    _pending_apply: dict[str, tuple[str, list[str]]]
    _apply_warnings_sent: set[str]
    _pending_audio_switch: dict[str, tuple[Callable[[AppConfig], Any], list[str]]]
    _cloudflared: Any
    _cloudflared_drain: Any

    def __init__(
        self,
        config: AppConfig,
        recorder: Any,
        monitor: Any,
        eventsub: Any,
        on_restart: Callable[[], None] | None = None,
        updater: Any = None,
        kick_webhook: Any = None,
    ) -> None:
        self._config = config
        self._recorder = recorder
        self._monitor = monitor
        self._eventsub = eventsub
        self._on_restart = on_restart
        self._updater = updater
        self._kick_webhook = kick_webhook
        self._admin_id = config.telegram_user_id
        self._app = Application.builder().token(config.bot_telegram_api).build()
        self._menu = "root"  # current reply-keyboard menu
        self._menu_channel = None  # channel name when _menu == "channel"
        self._custom_setting = None  # parent setting when _menu == "custom"
        self._cloudflare_hostname = None  # hostname picked during the named-tunnel flow
        self._confirm_done = set()  # callback_data already confirmed (double-tap guard)
        self._pending_apply = {}  # nonce -> (summary, channels) awaiting apply-now confirmation
        self._pending_audio_switch = {}  # nonce -> (quality mutation, channels) awaiting audio-only confirm
        self._apply_warnings_sent = set()  # nonces already messaged to the admin
        self._cloudflared = None  # running cloudflared subprocess (quick or named)
        self._cloudflared_drain = None

    def command_list(self) -> list[BotCommand]:
        """BotCommand entries for the Telegram /-menu that only the admin sees."""
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
            BotCommand("quality", "Show or set quality (global or per-channel)"),
            BotCommand("maxrecordings", "Set concurrent recording limit"),
            BotCommand("maxyoutube", "Set YouTube re-stream limit"),
            BotCommand("disk", "Show or set disk limits"),
            BotCommand("chat", "Toggle live chat recording"),
            BotCommand("settings", "Open the settings menu (reply keyboard buttons)"),
        ]

    async def start(self) -> None:
        admin = filters.User(user_id=self._admin_id)
        self._app.add_handlers(
            [
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
            ]
        )
        await self._app.initialize()
        await self._app.start()
        try:
            await self._app.bot.set_my_commands(self.command_list(), scope=BotCommandScopeChat(chat_id=self._admin_id))
        except Exception:
            logger.warning("[telegram] Failed to register command menu", exc_info=True)
        updater = self._app.updater
        if updater is None:
            raise RuntimeError("telegram updater not available")
        await updater.start_polling(allowed_updates=["message", "callback_query"])
        logger.info("[telegram] Bot polling started (admin id=%s)", self._admin_id)
        # Re-arm the /settings reply keyboard after a restart. Keyboard
        # buttons are plain text routed by in-memory menu state, and that
        # state resets on boot. A stale on-screen keyboard stays dead until
        # the admin types /settings.
        try:
            await self._app.bot.send_message(
                chat_id=self._admin_id,
                text=await self.menu_text("root"),
                reply_markup=self.reply_keyboard("root"),
            )
        except Exception:
            logger.warning("[telegram] Failed to re-send settings menu after restart", exc_info=True)
        w = self._config.kick.webhook
        if w.enabled and w.tunnel == "cloudflare" and w.cloudflare_managed:
            asyncio.create_task(self._restore_cloudflared())

    async def stop(self) -> None:
        self._cloudflared_stop()
        updater = self._app.updater
        if updater is not None:
            await updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    def _apply(self, mutate: Callable[[AppConfig], Any], ok_text: Callable[[AppConfig], str]) -> str:
        candidate = self._config.model_copy(deep=True)
        try:
            mutate(candidate)
            save_config(candidate)
        except ValueError as e:
            return f"\u274c {e}"
        affected = _deferred_affected_channels(candidate, self._recorder.recording_settings())
        _replace_in_place(self._config, candidate)
        if affected:
            self._pending_apply[secrets.token_hex(4)] = (ok_text(candidate), affected)
        return ok_text(candidate)

    async def _maybe_send_apply_warnings(self) -> None:
        """Send apply-now warnings stashed by _apply.

        _apply stores warnings when deferred-effect settings changed while
        channels recorded. Entries stay in ``_pending_apply`` until the
        admin answers. The nonce in the message callback data must still
        resolve when the admin taps a button. ``_apply_warnings_sent``
        records which nonces the bot already messaged, so a later trigger
        does not resend.
        """
        for nonce in list(self._pending_apply):
            if nonce in self._apply_warnings_sent:
                continue
            summary, channels = self._pending_apply[nonce]
            text = (
                f"\u26a0\ufe0f {summary}, but recording in progress for: {', '.join(channels)}\n"
                "The running recording keeps the previous settings until it ends.\n"
                "Apply the new settings now (restarts the recording) or keep the current recording?"
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Apply now", callback_data=f"apply_now:{nonce}"),
                        InlineKeyboardButton("Keep current recording", callback_data=f"cancel:{nonce}"),
                    ],
                ]
            )
            try:
                await self._app.bot.send_message(chat_id=self._admin_id, text=text, reply_markup=markup)
                self._apply_warnings_sent.add(nonce)
            except Exception:
                logger.warning("[telegram] Failed to send apply-now warning", exc_info=True)

        for nonce in list(self._pending_audio_switch):
            if nonce in self._apply_warnings_sent:
                continue
            channels = self._pending_audio_switch[nonce][1]
            text = (
                f"\u26a0\ufe0f Setting audio_only quality will set output mode to disk for: {', '.join(channels)}\n"
                "Audio-only cannot be restreamed to YouTube."
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Confirm", callback_data=f"audio_confirm:{nonce}"),
                        InlineKeyboardButton("Cancel", callback_data=f"cancel:{nonce}"),
                    ],
                ]
            )
            try:
                await self._app.bot.send_message(chat_id=self._admin_id, text=text, reply_markup=markup)
                self._apply_warnings_sent.add(nonce)
            except Exception:
                logger.warning("[telegram] Failed to send audio-only warning", exc_info=True)

    def reply_keyboard(self, menu: str = "root", channel: str | None = None) -> ReplyKeyboardMarkup:
        """Reply-keyboard rows for ``menu``. Button labels are the routing literals."""
        if menu == "root":
            rows = [
                ["Channels", "Status"],
                ["Chat recording", "Output mode"],
                ["Quality", "Retention"],
                ["Max recordings", "Max YouTube"],
                ["Disk", "Kick webhook"],
            ]
        elif menu == "channels":
            rows = [["Back"], ["Add channel"], *([f"\u2022 {ch}"] for ch in self._config.channels)]
        elif menu == "channel":
            rows = [
                ["Back"],
                ["Mode: disk", "Mode: youtube"],
                ["Mode: both", "Mode: default"],
                ["Hold delay", "Quality"],
                ["Delete channel"],
            ]
        elif menu == "channel_hold":
            rows = [["0 (off)", "30s", "60s", "120s"], ["300s", "600s", "Default"], ["Custom", "Back"]]
        elif menu == "channel_quality":
            rows = [["best", "1080p", "720p"], ["480p", "360p", "audio_only"], ["Default"], ["Back"]]
        elif menu == "chat":
            rows = [["Twitch", "Kick"], ["Back"]]
        elif menu in ("chat_twitch", "chat_kick"):
            rows = [["On", "Off"], ["Back"]]
        elif menu == "mode":
            rows = [["disk", "youtube", "both"], ["Back"]]
        elif menu == "quality":
            rows = [["best", "1080p", "720p"], ["480p", "360p", "audio_only"], ["Back"]]
        elif menu == "retention":
            rows = [["1 day", "3 days", "7 days"], ["14 days", "30 days", "Off"], ["Custom", "Back"]]
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
            rows,
            resize_keyboard=True,
            input_field_placeholder="Tap a button or type a command",
        )

    async def menu_text(self, menu: str = "root", channel: str | None = None) -> str:
        """Return the status or instruction body shown above the reply keyboard for ``menu``."""
        c = self._config
        if menu == "root":
            return await self.handle_status()
        if menu == "channels":
            return (
                f"Channels ({len(c.channels)}): {', '.join(c.channels)}\n\n"
                "Tap a channel to manage it, or add a new one."
            )
        if menu == "add_channel":
            return (
                "Send the channel name or profile URL to monitor "
                "(Twitch: twitch:<name> or https://twitch.tv/...; Kick: kick:<name> or https://kick.com/...):"
            )
        if menu == "kick_webhook":
            return f"Kick webhook: {self._webhook_state_text()}\n\nChoose the tunnel you use to expose this service:"
        if menu == "kick_cloudflare":
            return (
                "Cloudflare tunnel\n\n"
                "\u2022 Quick tunnel \u2014 no Cloudflare account needed, temporary URL.\n"
                "\u2022 Named tunnel \u2014 your Cloudflare account, stable hostname.\n"
                "\u2022 Already running your own tunnel? Send me its URL directly."
            )
        if menu == "kick_cloudflare_token":
            return (
                "Send your tunnel token:\n\n"
                "cloudflared service install <TOKEN>\n\n"
                "Paste the whole command or just the token \u2014 I'll run cloudflared for you."
            )
        if menu == "kick_cloudflare_hostname":
            return (
                "Send the public hostname to use for the webhook, e.g. kick.example.com.\n\n"
                "I'll point your tunnel at this app automatically \u2014 no dashboard configuration needed."
            )
        if menu == "kick_cloudflare_dns":
            return (
                "Send your Cloudflare API token so I can create the DNS record automatically:\n\n"
                "dash.cloudflare.com \u2192 My Profile \u2192 API Tokens \u2192 Create Token:\n"
                "\u2022 Permissions: Zone \u2192 Read, DNS \u2192 Edit\n"
                "\u2022 Zone Resources: Include \u2192 your domain (e.g. the part after the "
                "dot of your hostname)\n"
                "(the 'Edit zone DNS' template has exactly those two)\n\n"
                "Or tap the Skip button to create the DNS record yourself \u2014 "
                "I'll give you the exact record."
            )
        if menu == "channel":
            ch = channel or ""
            override = c.channel_output_modes.get(ch)
            mode = override or f"default (global: {c.output_mode})"
            q_override = c.channel_preferred_qualities.get(ch)
            quality_text = q_override or f"default (global: {c.preferred_quality})"
            hold_override = c.channel_youtube_hold_seconds.get(ch)
            hold_text = (
                f"{hold_override:g}s" if hold_override is not None else f"default (global: {c.youtube.hold_seconds:g}s)"
            )
            return f"Channel: {ch}\nOutput mode: {mode}\nQuality: {quality_text}\nHold delay: {hold_text}"
        if menu == "channel_hold":
            ch = channel or ""
            hold_override = c.channel_youtube_hold_seconds.get(ch)
            eff = c.youtube.hold_seconds if hold_override is None else hold_override
            return (
                f"YouTube hold delay for {ch}: {eff:g}s (0 = end immediately)\n"
                f"Global default: {c.youtube.hold_seconds:g}s\n\n"
                "When the source stream stops, the broadcast stays open this long, waiting for the "
                "streamer to return \u2014 a return within the delay reuses the same broadcast instead "
                "of creating a new one."
            )
        if menu == "channel_quality":
            ch = channel or ""
            q_override = c.channel_preferred_qualities.get(ch)
            return (
                f"Recording quality for {ch}: "
                f"{q_override or f'default (global: {c.preferred_quality})'}\n\n"
                "audio_only records sound only; it forces output to disk (no YouTube re-stream)."
            )
        if menu == "chat":
            kick_chat = c.kick.record_chat
            return (
                f"Chat recording (Twitch): {'on' if c.record_chat else 'off'}\n"
                f"Kick chat recording: {'on' if kick_chat else 'off'}\n\nChoose a platform:"
            )
        if menu == "chat_twitch":
            return f"Twitch chat recording: {'on' if c.record_chat else 'off'}. Choose:"
        if menu == "chat_kick":
            kick_chat = c.kick.record_chat
            return f"Kick chat recording: {'on' if kick_chat else 'off'}. Choose:"
        if menu == "mode":
            return f"Output mode: {c.output_mode}. Choose:"
        if menu == "quality":
            text = f"Quality: {c.preferred_quality}. Choose:"
            if c.channel_preferred_qualities:
                text += "\nPer-channel: " + ", ".join(
                    f"{ch} \u2192 {q}" for ch, q in sorted(c.channel_preferred_qualities.items())
                )
            return text
        if menu == "retention":
            return f"Retention: {c.retention_days} day(s) (0 = disabled). Choose:"
        if menu == "maxrec":
            return f"Max recordings: {c.max_concurrent_recordings} (0 = unlimited). Choose:"
        if menu == "maxyt":
            return f"Max YouTube re-streams: {c.max_concurrent_youtube_streams} (0 = unlimited). Choose:"
        if menu == "disk":
            return self.handle_disk([]) + "\n\nChoose a limit:"
        d = c.disk
        if menu == "disk_maxsize":
            return (
                f"Max total: {d.max_total_gb:g} GB (0 = disabled)\n"
                "Limits total recording size; when exceeded, the oldest recordings are deleted "
                "(or recording stops). Choose:"
            )
        if menu == "custom":
            ch = self._menu_channel or ""
            hold_override = c.channel_youtube_hold_seconds.get(ch)
            eff = c.youtube.hold_seconds if hold_override is None else hold_override
            labels = {
                "retention": (f"Retention: {c.retention_days} day(s) (0 = disabled)", " in days"),
                "maxrec": (f"Max recordings: {c.max_concurrent_recordings} (0 = unlimited)", ""),
                "maxyt": (f"Max YouTube re-streams: {c.max_concurrent_youtube_streams} (0 = unlimited)", ""),
                "disk_maxsize": (f"Max total: {d.max_total_gb:g} GB (0 = disabled)", " in GB"),
                "channel_hold": (f"Hold delay for {ch}: {eff:g}s (0 = end immediately)", " in seconds"),
            }
            label, units = labels[self._custom_setting or ""]
            return f"{label}. Send the new value{units}:"
        return await self.handle_status()

    async def handle_reply_text(self, text: str) -> tuple[str, ReplyKeyboardMarkup | InlineKeyboardMarkup] | None:
        """Route one reply-keyboard press or typed value.

        Return ``(reply_text, reply_markup)`` for ``reply_text(..., reply_markup=)``.
        Both markup kinds are valid there. Return ``None`` to ignore the message.
        """
        if text == "Back":
            parent: str | None
            if self._menu == "custom":
                parent = (
                    "channel"
                    if self._custom_setting == "channel_hold"
                    else ("root" if self._custom_setting in ("retention", "maxrec", "maxyt") else "disk")
                )
            else:
                parent = {
                    "channels": "root",
                    "add_channel": "channels",
                    "channel": "channels",
                    "channel_hold": "channel",
                    "channel_quality": "channel",
                    "chat": "root",
                    "chat_twitch": "chat",
                    "chat_kick": "chat",
                    "mode": "root",
                    "quality": "root",
                    "retention": "root",
                    "maxrec": "root",
                    "maxyt": "root",
                    "disk": "root",
                    "disk_maxsize": "disk",
                    "kick_webhook": "root",
                    "kick_cloudflare": "kick_webhook",
                    "kick_cloudflare_token": "kick_cloudflare",
                    "kick_cloudflare_hostname": "kick_cloudflare_token",
                    "kick_cloudflare_dns": "kick_cloudflare_hostname",
                }.get(self._menu)
            if parent is None:  # no Back button on root
                return None
            if self._menu in ("kick_cloudflare_hostname", "kick_cloudflare_dns"):
                self._cloudflare_hostname = None
            if parent == "channel":
                self._menu = "channel"
                return await self.menu_text("channel", self._menu_channel), self.reply_keyboard("channel")
            self._menu, self._menu_channel = parent or "", None
            return await self.menu_text(parent), self.reply_keyboard(parent)
        if self._menu == "add_channel":  # any text is a candidate channel name
            result = await self.handle_add([text])
            if result.startswith("\u274c") or result.startswith("Usage"):
                return result, self.reply_keyboard("add_channel")
            self._menu = "channels"
            return result, self.reply_keyboard("channels")
        if self._menu == "custom":  # any text is a value for _custom_setting
            setting = self._custom_setting or ""
            if setting == "retention":
                result = self.handle_retention([text])
            elif setting == "maxrec":
                result = self.handle_maxrecordings([text])
            elif setting == "maxyt":
                result = self.handle_maxyoutube([text])
            elif setting == "channel_hold":
                result = self.handle_channel_hold([self._menu_channel or "", text])
            else:
                result = self.handle_disk(["maxsize", text])
            if result.startswith("\u274c") or result.startswith("Usage"):
                return result, self.reply_keyboard("custom")
            parent = (
                "channel"
                if setting == "channel_hold"
                else ("root" if setting in ("retention", "maxrec", "maxyt") else "disk")
            )
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
                return (
                    "\u274c That doesn't look like a public hostname (e.g. kick.example.com).",
                    self.reply_keyboard("kick_cloudflare_hostname"),
                )
            self._cloudflare_hostname = host
            self._menu = "kick_cloudflare_dns"
            return (
                f"Hostname {host} \u2014 " + await self.menu_text("kick_cloudflare_dns"),
                self.reply_keyboard("kick_cloudflare_dns"),
            )
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
                "Channels": "channels",
                "Chat recording": "chat",
                "Output mode": "mode",
                "Quality": "quality",
                "Retention": "retention",
                "Max recordings": "maxrec",
                "Max YouTube": "maxyt",
                "Disk": "disk",
                "Kick webhook": "kick_webhook",
            }.get(text)
            if new_menu is None:
                return None
            self._menu = new_menu
            return await self.menu_text(new_menu), self.reply_keyboard(new_menu)
        if menu == "channels":
            if text == "Add channel":
                self._menu = "add_channel"
                return await self.menu_text("add_channel"), self.reply_keyboard("add_channel")
            if text.startswith("\u2022 ") and text[2:] in self._config.channels:
                self._menu, self._menu_channel = "channel", text[2:]
                return (await self.menu_text("channel", text[2:]), self.reply_keyboard("channel"))
            return None
        if menu == "channel":
            ch = self._menu_channel
            if ch is None:
                return None
            if text == "Delete channel":
                return (
                    f"Remove {ch} from monitoring? This stops any active recording "
                    f"and removes its output-mode override.",
                    self._confirm_keyboard("confirm_remove", ch),
                )
            if text == "Hold delay":
                self._menu = "channel_hold"
                return await self.menu_text("channel_hold", ch), self.reply_keyboard("channel_hold")
            if text == "Quality":
                self._menu = "channel_quality"
                return await self.menu_text("channel_quality", ch), self.reply_keyboard("channel_quality")
            values = {
                "Mode: disk": "disk",
                "Mode: youtube": "youtube",
                "Mode: both": "both",
                "Mode: default": "default",
            }
            if text in values:
                result = self.handle_mode([ch, values[text]])
                return result, self.reply_keyboard("channel")
            return None
        if menu == "channel_hold":
            ch = self._menu_channel
            if ch is None:
                return None
            values = {
                "0 (off)": "0",
                "30s": "30",
                "60s": "60",
                "120s": "120",
                "300s": "300",
                "600s": "600",
                "Default": "default",
            }
            if text in values:
                result = self.handle_channel_hold([ch, values[text]])
                self._menu = "channel"
                return result, self.reply_keyboard("channel")
            if text == "Custom":
                self._custom_setting, self._menu = "channel_hold", "custom"
                return await self.menu_text("custom"), self.reply_keyboard("custom")
            return None
        if menu == "channel_quality":
            ch = self._menu_channel
            if ch is None:
                return None
            if text == "Default":
                result = self.handle_quality([ch, "default"])
            elif text in _QUALITY_PRESETS:
                result = self.handle_quality([ch, text])
            else:
                return None
            self._menu = "channel"
            return result, self.reply_keyboard("channel")
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
            values = {"1 day": "1", "3 days": "3", "7 days": "7", "14 days": "14", "30 days": "30", "Off": "0"}
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
                if self._config.disk.delete_oldest:
                    result = self.handle_disk(["delete_oldest", "off"])
                    return result, self.reply_keyboard("disk")
                return (
                    "Enable 'delete oldest'? When the disk is over the max total, "
                    "the oldest recordings will be deleted.",
                    self._confirm_keyboard("confirm_delete_oldest", "on"),
                )
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
            port = self._config.kick.webhook.listen_port
            if text == "Off":
                result = await self._apply_webhook_state(False, "")
                self._menu = "root"
                return result, self.reply_keyboard("root")
            if text == "Cloudflare tunnel":
                self._menu = "kick_cloudflare"
                return await self.menu_text("kick_cloudflare"), self.reply_keyboard("kick_cloudflare")
            if text == "Tailscale funnel":
                old_tunnel = self._config.kick.webhook.tunnel
                url, hint = await self._tailscale_webhook_url()
                if url is not None:
                    result = await self._apply_webhook_state(True, url, "tailscale")
                    self._menu = "kick_webhook"
                    note = await self._reachability_note(url, "tailscale")
                    msg = (
                        f"{result}\n\n```\n{url}\n```\n"
                        f"tailscale funnel {port} is enabled on this host.\n" + _KICK_DASHBOARD_HINT + note
                    )
                    if old_tunnel == "cloudflare":
                        msg += "\nYour cloudflared tunnel has been stopped."
                    return msg, self.reply_keyboard("kick_webhook")
                self._menu = "kick_cloudflare"
                return (
                    f"{hint}\n\nFix tailscale and tap Tailscale funnel again, or use Cloudflare tunnel instead.",
                    self.reply_keyboard("kick_cloudflare"),
                )
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
                return (
                    f"{result}\n\n```\n{url}\n```\n"
                    "cloudflared quick tunnel is running on this host.\n" + _KICK_DASHBOARD_HINT + note,
                    self.reply_keyboard("kick_webhook"),
                )
            if text == "Named tunnel":
                self._menu = "kick_cloudflare_token"
                return await self.menu_text("kick_cloudflare_token"), self.reply_keyboard("kick_cloudflare_token")
            return None
        return None

    def _confirm_keyboard(self, action: str, value: str) -> InlineKeyboardMarkup:
        # The nonce makes the callback data unique per confirm message, so the
        # double-tap guard never drops a later confirm or cancel on a new message.
        nonce = secrets.token_hex(4)
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Confirm", callback_data=f"{action}:{value}:{nonce}"),
                    InlineKeyboardButton("Cancel", callback_data=f"cancel:{nonce}"),
                ],
            ]
        )

    async def handle_callback(self, data: str) -> tuple[str, Any] | None:
        """Apply one confirmation-button press.

        Return ``(reply_text, markup)`` on success or ``None`` for an unknown
        or already handled press. Wire format (from ``_confirm_keyboard``):
        ``confirm_<action>:<value>:<nonce>`` and ``cancel:<nonce>``. Apply-now
        warnings use ``apply_now:<nonce>``, audio-only switches use
        ``audio_confirm:<nonce>``. The nonce makes every confirm message's
        buttons unique, so the double-tap guard covers only the same message.
        """
        parts = data.split(":")
        action = parts[0]
        if action == "cancel" and len(parts) == 2:
            if data in self._confirm_done:  # double-tap on the same message
                return None
            self._pending_audio_switch.pop(parts[1], None)  # a cancelled choice can never be confirmed later
            self._confirm_done.add(data)
            return "Cancelled \u2014 nothing changed", None
        if action == "confirm_remove" and len(parts) >= 3:
            if data in self._confirm_done:  # double-tap on the same message
                return None
            # The channel sits between the action and the nonce. The channel
            # name can itself contain ':' (kick:<slug>), so rejoin the middle parts.
            value = ":".join(parts[1:-1])
            if value not in self._config.channels:
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
        if action == "apply_now" and len(parts) == 2:
            if data in self._confirm_done:  # double-tap on the same message
                return None
            pending = self._pending_apply.pop(parts[1], None)
            if pending is None:
                return None  # stale message: bot restarted or already handled
            self._apply_warnings_sent.discard(parts[1])
            self._confirm_done.add(data)
            summary, channels = pending
            lines = []
            for ch in channels:
                ok = await self._recorder.restart(ch)
                lines.append(f"{ch}: {'restarted with the new settings' if ok else 'no longer recording'}")
            return f"\u2705 Applied: {summary}\n" + "\n".join(lines), None
        if action == "audio_confirm" and len(parts) == 2:
            if data in self._confirm_done:
                return None
            audio_pending = self._pending_audio_switch.pop(parts[1], None)
            if audio_pending is None:
                return None  # stale message: already handled or bot restarted
            self._confirm_done.add(data)
            self._apply_warnings_sent.discard(parts[1])
            quality_mutate, channels = audio_pending

            def combined(candidate: AppConfig) -> None:
                quality_mutate(candidate)
                live = set(candidate.channels)
                for ch in channels:
                    if ch in live:
                        candidate.channel_output_modes[ch] = "disk"

            result = self._apply(
                combined,
                lambda c: f"Quality set to audio_only; output mode disk for {', '.join(channels)}",
            )
            return result, None
        return None

    async def _on_callback(self, update: Any, context: Any) -> None:
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
            with contextlib.suppress(BadRequest):
                await query.answer()
            try:
                await query.edit_message_text(error_text, reply_markup=None)
            except BadRequest:  # confirm message vanished -> send a fresh one
                await context.bot.send_message(chat_id=query.from_user.id, text=error_text)
            return
        if result is None:  # double-tap or unknown data: silent ack, no toast
            with contextlib.suppress(BadRequest):
                await query.answer()
            return
        text, _ = result
        with contextlib.suppress(BadRequest):
            await query.answer()
        try:
            await query.edit_message_text(text, reply_markup=None)
        except BadRequest:  # message vanished mid-flight -> resend
            await context.bot.send_message(chat_id=query.from_user.id, text=text)
        await self._send_menu(context)

    async def _send_menu(self, context: Any) -> None:
        """Re-render the reply keyboard for the current menu state."""
        await context.bot.send_message(
            chat_id=self._admin_id,
            text=await self.menu_text(self._menu, self._menu_channel),
            reply_markup=self.reply_keyboard(self._menu, self._menu_channel),
        )

    async def _on_text(self, update: Any, context: Any) -> None:
        result = await self.handle_reply_text(update.effective_message.text)
        if result is None:
            return
        text, markup = result
        await update.effective_message.reply_text(text, reply_markup=markup)
        await self._maybe_send_apply_warnings()

    async def _send_admin(self, text: str) -> None:
        try:
            await self._app.bot.send_message(chat_id=self._admin_id, text=text)
        except Exception:
            logger.warning("[telegram] Failed to notify admin", exc_info=True)

    async def _cmd_help(self, update: Any, context: Any) -> None:
        self._menu, self._menu_channel = "root", None
        await update.effective_message.reply_text(self.handle_help(), reply_markup=self.reply_keyboard("root"))

    async def _cmd_status(self, update: Any, context: Any) -> None:
        self._menu, self._menu_channel = "root", None
        await update.effective_message.reply_text(await self.handle_status(), reply_markup=self.reply_keyboard("root"))

    async def _cmd_settings(self, update: Any, context: Any) -> None:
        self._menu, self._menu_channel = "root", None
        await update.effective_message.reply_text(
            await self.menu_text("root"), reply_markup=self.reply_keyboard("root")
        )

    async def _cmd_start(self, update: Any, context: Any) -> None:
        self._menu, self._menu_channel = "root", None
        await update.effective_message.reply_text(self.handle_help(), reply_markup=self.reply_keyboard("root"))

    async def _cmd_channels(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_channels())

    async def _cmd_add(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(await self.handle_add(context.args or []))

    async def _cmd_remove(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(await self.handle_remove(context.args or []))

    async def _cmd_retention(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_retention(context.args or []))

    async def _cmd_mode(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_mode(context.args or []))
        await self._maybe_send_apply_warnings()

    async def _cmd_reload(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(await self.handle_reload())

    async def _cmd_restart(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_restart())

    async def _cmd_update(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(await self.handle_update())

    async def _cmd_quality(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_quality(context.args or []))
        await self._maybe_send_apply_warnings()

    async def _cmd_maxrecordings(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_maxrecordings(context.args or []))

    async def _cmd_maxyoutube(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_maxyoutube(context.args or []))

    async def _cmd_disk(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_disk(context.args or []))

    async def _cmd_chat(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(await self.handle_chat(context.args or []))
        await self._maybe_send_apply_warnings()
