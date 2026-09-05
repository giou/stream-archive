"""Telegram bot: command handlers plus routing to the menu modules.

All reply-keyboard branches live in ``menus.py`` (render) and the
``menus_*`` modules (routing). This controller only registers handlers,
tracks per-chat state, and applies config changes.
"""

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable
from typing import Any

from telegram import BotCommandScopeChat, ReplyKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from stream_archive.config import AppConfig, _replace_in_place, effective_quality, is_kick_channel, save_config
from stream_archive.http import build_shared_client
from stream_archive.telegram import menus
from stream_archive.telegram import menus_callbacks as callbacks
from stream_archive.telegram.commands_channels import ChannelsCommands
from stream_archive.telegram.commands_settings import SettingsCommands
from stream_archive.telegram.commands_system import SystemCommands
from stream_archive.telegram.commands_webhook import WebhookCommands
from stream_archive.telegram.menu_state import ChatId, ChatStateMixin, MenuResult
from stream_archive.telegram.menus_commands import CommandsMixin

logger = logging.getLogger(__name__)


def _deferred_affected_channels(new: AppConfig, recordings: dict[str, dict[str, Any]]) -> list[str]:
    """Active channels whose in-flight recording a config change affects.

    The check compares against the settings each recording actually uses,
    snapshotted at recording start, not against the previous config. After
    a declined change the config already holds the new value while the
    recording keeps the old settings, so the same change must warn again.
    Deferred effects: output mode (global or per-channel override),
    preferred quality, and chat capture enabled (disabling chat stops an
    in-flight capture immediately and never warns). A channel that the
    change removes from monitoring is never listed. The remove path stops
    its recording at once, so no deferred choice exists for it.
    """
    affected: set[str] = set()
    for ch, rec in recordings.items():
        if ch not in new.channels:
            continue
        if (
            rec.get("output_mode") != new.channel_output_modes.get(ch, new.output_mode)
            or rec.get("preferred_quality") != effective_quality(new, ch)
            or (not is_kick_channel(ch) and new.record_chat and not rec.get("record_chat"))
            or (is_kick_channel(ch) and new.kick.record_chat and not rec.get("kick_record_chat"))
        ):
            affected.add(ch)
    return sorted(affected)


class TelegramController(
    ChatStateMixin, CommandsMixin, ChannelsCommands, SettingsCommands, WebhookCommands, SystemCommands
):
    _config: AppConfig
    _recorder: Any
    _monitor: Any
    _eventsub: Any
    _updater: Any
    _kick_webhook: Any
    _http: Any
    _owns_http: bool
    _app: Application[Any, Any, Any, Any, Any, Any]
    _admin_id: int
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
        http: Any = None,
    ) -> None:
        self._config = config
        self._recorder = recorder
        self._monitor = monitor
        self._eventsub = eventsub
        self._on_restart = on_restart
        self._updater = updater
        self._kick_webhook = kick_webhook
        if http is not None:
            self._http = http
            self._owns_http = False
        else:
            self._http = build_shared_client()
            self._owns_http = True
        self._admin_id = config.telegram_user_id
        self._app = Application.builder().token(config.bot_telegram_api).build()
        self._init_chat_state()
        self._cloudflared = None  # running cloudflared subprocess (quick or named)
        self._cloudflared_drain = None

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
                CommandHandler("settings", self._cmd_settings, filters=admin),
                CommandHandler("start", self._cmd_start, filters=admin),
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(user_id=self._admin_id), self._on_text),
                callbacks.AdminCallbackQueryHandler(self._on_callback, admin_id=self._admin_id),
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
            msg = "telegram updater not available"
            raise RuntimeError(msg)
        await updater.start_polling(allowed_updates=["message", "callback_query"])
        logger.info("[telegram] Bot polling started (admin id=%s)", self._admin_id)
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
        if self._owns_http:
            await self._http.aclose()

    def _apply(
        self, mutate: Callable[[AppConfig], Any], ok_text: Callable[[AppConfig], str], chat_id: ChatId | None = None
    ) -> str:
        candidate = self._config.model_copy(deep=True)
        try:
            mutate(candidate)
            save_config(candidate)
        except ValueError as e:
            return f"\u274c {e}"
        affected = _deferred_affected_channels(candidate, self._recorder.recording_settings())
        _replace_in_place(self._config, candidate)
        if affected:
            chat = chat_id if chat_id is not None else self._admin_id
            self._pending_apply[(chat, secrets.token_hex(4))] = (ok_text(candidate), affected)
        return ok_text(candidate)

    def _confirm_keyboard(self, action: str, value: str) -> Any:
        return callbacks.confirm_keyboard(action, value)

    async def handle_callback(self, data: str, chat_id: ChatId | None = None) -> tuple[str, Any] | None:
        """Apply one confirmation-button press for ``chat_id`` (admin by default)."""
        return await callbacks.handle_callback(self, data, chat_id if chat_id is not None else self._admin_id)

    async def _maybe_send_apply_warnings(self) -> None:
        await callbacks.maybe_send_apply_warnings(self)

    async def _on_callback(self, update: Any, context: Any) -> None:
        # The handler filter already drops non-admin presses, so no check here.
        query = update.callback_query
        try:
            result = await self.handle_callback(query.data, self._callback_chat_of(update))
        except BadRequest:
            logger.warning("[telegram] Callback target vanished", exc_info=True)
            return
        except Exception:
            logger.error("[telegram] Callback %s failed", query.data, exc_info=True)
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
        await self._send_menu(context, self._callback_chat_of(update))

    async def _send_menu(self, context: Any, chat_id: ChatId) -> None:
        """Re-render the reply keyboard for one chat's menu state."""
        state = self._state_for(chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=await self.menu_text(state.menu, state.channel, chat_id=chat_id),
            reply_markup=self.reply_keyboard(state.menu, state.channel),
        )

    async def _on_text(self, update: Any, context: Any) -> None:
        message = update.effective_message
        if message is None or message.text is None:
            return
        chat_id = self._chat_of(update)
        try:
            result = await self.handle_reply_text(message.text, chat_id=chat_id)
            if result is None:
                return
            text, markup = result
            await message.reply_text(text, reply_markup=markup)
            await self._maybe_send_apply_warnings()
        except BadRequest:
            logger.warning("[telegram] Reply target vanished", exc_info=True)
        except Exception:
            logger.error("[telegram] Text handler failed", exc_info=True)
            with contextlib.suppress(BadRequest):
                await message.reply_text("\u274c Unexpected error \u2014 see logs")

    async def _send_admin(self, text: str) -> None:
        try:
            await self._app.bot.send_message(chat_id=self._admin_id, text=text)
        except Exception:
            logger.warning("[telegram] Failed to notify admin", exc_info=True)

    def reply_keyboard(self, menu: str = "root", channel: str | None = None) -> ReplyKeyboardMarkup:
        """Reply-keyboard rows for ``menu``. Button labels are the routing literals."""
        return menus.render_keyboard(self, menu, channel)

    async def menu_text(self, menu: str = "root", channel: str | None = None, chat_id: ChatId | None = None) -> str:
        """Return the status or instruction body shown above the reply keyboard for ``menu``."""
        state = self._state_for(chat_id if chat_id is not None else self._admin_id)
        return await menus.render_text(self, menu, channel if channel is not None else state.channel, state.custom)

    async def handle_reply_text(self, text: str, chat_id: ChatId | None = None) -> MenuResult:
        """Route one reply-keyboard press or typed value for ``chat_id`` (admin by default)."""
        return await menus.dispatch_text(self, chat_id if chat_id is not None else self._admin_id, text)
