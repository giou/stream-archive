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

from stream_archive.config import (
    AUDIO_ONLY_QUALITY,
    AppConfig,
    apply_config_change,
    effective_quality,
    is_kick_channel,
)
from stream_archive.http import build_http_client
from stream_archive.telegram import menus
from stream_archive.telegram import menus_callbacks as callbacks
from stream_archive.telegram.commands_api import ApiCommands
from stream_archive.telegram.commands_channels import ChannelsCommands
from stream_archive.telegram.commands_mtproto import MtprotoCommands
from stream_archive.telegram.commands_settings import SettingsCommands
from stream_archive.telegram.commands_system import SystemCommands
from stream_archive.telegram.commands_web import WebCommands
from stream_archive.telegram.commands_webhook import WebhookCommands
from stream_archive.telegram.menu_state import ChatId, ChatStateMixin, MenuResult
from stream_archive.telegram.menus_commands import CommandsMixin
from stream_archive.tunnels import CloudflaredTunnel

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
        expected_mode = new.channel_output_modes.get(ch, new.output_mode)
        if expected_mode != "disk" and effective_quality(new, ch) == AUDIO_ONLY_QUALITY:
            # Mirror Recorder._effective_mode: audio-only always records to disk.
            expected_mode = "disk"
        if (
            rec.get("output_mode") != expected_mode
            or rec.get("preferred_quality") != effective_quality(new, ch)
            or (not is_kick_channel(ch) and new.record_chat and not rec.get("record_chat"))
            or (is_kick_channel(ch) and new.kick.record_chat and not rec.get("kick_record_chat"))
        ):
            affected.add(ch)
    return sorted(affected)


class TelegramController(
    ChatStateMixin,
    CommandsMixin,
    ChannelsCommands,
    SettingsCommands,
    ApiCommands,
    WebCommands,
    WebhookCommands,
    SystemCommands,
    MtprotoCommands,
):
    _config: AppConfig
    _recorder: Any
    _monitor: Any
    _eventsub: Any
    _updater: Any
    _kick_webhook: Any
    _twitch_api: Any
    _kick_api: Any
    _mtproto: Any
    _mtproto_tasks: set[asyncio.Task[None]]
    _sending_paths: set[str]
    _mtproto_sends: dict[tuple[ChatId, str], asyncio.Task[None]]
    _http: Any
    _owns_http: bool
    _app: Any
    _admin_id: int
    _admin_filter: Any
    _enabled: bool
    _cloudflared: CloudflaredTunnel
    _cloudflared_lock: asyncio.Lock
    _restore_task: asyncio.Task[None] | None

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
        mtproto: Any = None,
    ) -> None:
        self._config = config
        self._recorder = recorder
        self._monitor = monitor
        self._eventsub = eventsub
        self._on_restart = on_restart
        self._updater = updater
        self._kick_webhook = kick_webhook
        self._mtproto = mtproto
        self._twitch_api: Any = None
        self._kick_api: Any = None
        if http is not None:
            self._http = http
            self._owns_http = False
        else:
            self._http = build_http_client()
            self._owns_http = True
        self._admin_id = config.telegram_user_id
        # An empty token or a zero id disables the bot. The web panel
        # then controls the app through the same command methods.
        self._enabled = bool(config.telegram_user_id > 0 and config.bot_telegram_api.strip())
        # One filter object gates every command handler, so a reload can
        # re-point the whole gate at a changed telegram_user_id in place.
        self._admin_filter = filters.User(user_id=self._admin_id) if self._enabled else None
        self._app: Any = None
        if self._enabled:
            self._app = Application.builder().token(config.bot_telegram_api).build()
        self._init_chat_state()
        self._mtproto_tasks: set[asyncio.Task[None]] = set()
        self._sending_paths: set[str] = set()
        self._mtproto_sends: dict[tuple[ChatId, str], asyncio.Task[None]] = {}
        self._pending_delete: dict[tuple[ChatId, str], str] = {}
        self._pending_bulk_delete: dict[tuple[ChatId, str], str | None] = {}
        self._cloudflared = CloudflaredTunnel()
        # One managed cloudflared process serves the whole app, so a tunnel
        # press must not interleave with another one. See menu_kick_cloudflare.
        self._cloudflared_lock = asyncio.Lock()
        self._restore_task = None
        self._callback_handler: Any = None

    @property
    def enabled(self) -> bool:
        """True when the bot polls Telegram. False means web-panel control."""
        return self._enabled

    def bind_live_check(self, twitch_api: Any, kick_api: Any) -> None:
        """Give /add an immediate live check through one monitor sweep.

        The scheduler calls this once the API clients exist. Without it
        a new channel waits for the next poll cycle to start recording.
        """
        self._twitch_api = twitch_api
        self._kick_api = kick_api

    def rebind_admin(self) -> None:
        """Point every admin gate at the current config.

        The handler filters and the callback gate are built once, from the
        admin id the process started with. A config reload that changes
        ``telegram_user_id`` must reach them here, or the previous identity
        keeps every operation and the new one is authorized nowhere. A
        reload never starts or stops the bot: enabling or disabling it
        needs a restart, like a changed bot token.
        """
        new_id = self._config.telegram_user_id
        if new_id == self._admin_id:
            return
        logger.info("[telegram] Admin identity changed to %s", new_id)
        self._admin_id = new_id
        if self._admin_filter is not None:
            self._admin_filter.user_ids = frozenset({new_id})
        if self._callback_handler is not None:
            self._callback_handler.rebind(new_id)

    def command_handlers(self) -> list[Any]:
        """Handlers of the admin commands, the reply text, and the buttons.

        The tests and ``start`` use this list, so every advertised command
        has a handler. Keep the order: a command handler comes before the
        text handler, and the text handler before the buttons.
        """
        if not self._enabled:
            return []
        admin = self._admin_filter
        self._callback_handler = callbacks.AdminCallbackQueryHandler(self._on_callback, admin_id=self._admin_id)
        return [
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
            CommandHandler("recordings", self._cmd_recordings, filters=admin),
            CommandHandler("settings", self._cmd_settings, filters=admin),
            CommandHandler("start", self._cmd_help, filters=admin),
            MessageHandler(filters.TEXT & ~filters.COMMAND & admin, self._on_text),
            self._callback_handler,
        ]

    async def start(self) -> None:
        if not self._enabled:
            logger.info("[telegram] Bot disabled (no token), web panel controls the app")
            return
        self._app.add_handlers(self.command_handlers())
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
                text=await self.menu_text("root", chat_id=self._admin_id),
                reply_markup=self.reply_keyboard("root", chat_id=self._admin_id),
            )
        except Exception:
            logger.warning("[telegram] Failed to re-send settings menu after restart", exc_info=True)
        ep = self._config.endpoint
        if ep.enabled and ep.tunnel == "cloudflare" and ep.cloudflare_managed:
            # Keep the reference: stop() cancels and awaits this task, and a
            # task without a reference can be collected mid-flight.
            self._restore_task = asyncio.create_task(self._restore_cloudflared())

    async def stop(self) -> None:
        """Stop the tunnel, the bot, and the owned HTTP session.

        Every step runs, even when an earlier one fails: the updater raises
        when it never started, and the owned session must still close. A
        failure is logged and does not stop the remaining steps. The bot
        steps run only when polling started; the tunnel and the owned
        session close in every mode, since __init__ owns them regardless.
        """
        # A restore task that still runs can start a cloudflared process
        # after this teardown, so cancel it and wait for it first. A failure
        # in the task is not a reason to skip the remaining steps.
        if self._restore_task is not None:
            self._restore_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self._restore_task
                except Exception:
                    logger.warning("[telegram] cloudflared restore task failed at stop", exc_info=True)
            self._restore_task = None
        if self._enabled and self._app is not None:
            updater = self._app.updater
            if updater is not None:
                try:
                    await updater.stop()
                except Exception:
                    logger.warning("[telegram] Failed to stop the updater", exc_info=True)
            try:
                await self._app.stop()
            except Exception:
                logger.warning("[telegram] Failed to stop the bot", exc_info=True)
            try:
                await self._app.shutdown()
            except Exception:
                logger.warning("[telegram] Failed to shut down the bot", exc_info=True)
        try:
            self._cloudflared_stop()
        except Exception:
            logger.warning("[telegram] Failed to stop cloudflared", exc_info=True)
        if self._owns_http:
            try:
                await self._http.aclose()
            except Exception:
                logger.warning("[telegram] Failed to close the HTTP session", exc_info=True)

    def _apply(
        self, mutate: Callable[[AppConfig], Any], ok_text: Callable[[AppConfig], str], chat_id: ChatId | None = None
    ) -> str:
        try:
            candidate = apply_config_change(self._config, mutate)
        except ValueError as e:
            return f"\u274c {e}"
        affected = _deferred_affected_channels(candidate, self._recorder.recording_settings())
        if affected:
            chat = chat_id if chat_id is not None else self._admin_id
            self._pending_apply[(chat, secrets.token_hex(4))] = (ok_text(candidate), affected)
            self._prune_pending(self._pending_apply, chat)
        text = ok_text(candidate)
        # Every applied change lands in the operator event feed, so the web
        # panel shows bot, API, and panel changes alike.
        from stream_archive import events as _events

        _events.record("config", None, text)
        return text

    def _confirm_keyboard(self, action: str, value: str) -> Any:
        return callbacks.confirm_keyboard(action, value)

    async def handle_callback(self, data: str, chat_id: ChatId | None = None) -> tuple[str, Any] | None:
        """Apply one confirmation-button press for ``chat_id`` (admin by default)."""
        return await callbacks.handle_callback(self, data, chat_id if chat_id is not None else self._admin_id)

    async def _maybe_send_apply_warnings(self) -> None:
        await callbacks.maybe_send_apply_warnings(self)

    async def _replace_callback_text(self, query: Any, context: Any, text: str, reply_markup: Any = None) -> None:
        """Replace the pressed message with ``text``, or resend it if it went away.

        A reply keyboard cannot edit an inline message in place, so a
        ReplyKeyboardMarkup answer goes out as a fresh message (with the
        inline prompt removed) instead of failing into BadRequest and a
        duplicate. Any other Telegram error must not escape either: the
        press is already answered, and the remaining steps still run.
        """
        from telegram import ReplyKeyboardMarkup as _RKM

        if isinstance(reply_markup, _RKM):
            with contextlib.suppress(Exception):
                await query.edit_message_text(text, reply_markup=None)
            try:
                await context.bot.send_message(chat_id=query.from_user.id, text=text, reply_markup=reply_markup)
            except Exception:
                logger.warning("[telegram] Failed to send the callback reply", exc_info=True)
            return
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
        except BadRequest:
            # The message vanished, or its text did not change: send a fresh one.
            try:
                await context.bot.send_message(chat_id=query.from_user.id, text=text, reply_markup=reply_markup)
            except Exception:
                logger.warning("[telegram] Failed to send the callback reply", exc_info=True)
        except Exception:
            logger.warning("[telegram] Failed to edit the callback message", exc_info=True)

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
            error_text = "\u274c Unexpected error - see logs"
            with contextlib.suppress(BadRequest):
                await query.answer()
            await self._replace_callback_text(query, context, error_text)
            return
        if result is None:  # double-tap or unknown data: silent ack, no toast
            with contextlib.suppress(BadRequest):
                await query.answer()
            return
        text, markup = result
        with contextlib.suppress(BadRequest):
            await query.answer()
        await self._replace_callback_text(query, context, text, reply_markup=markup)
        # A callback can apply a change that defers onto a running
        # recording (the audio-only confirm). Send its prompt now, as the
        # text paths do, instead of waiting for the next typed message.
        await self._maybe_send_apply_warnings()
        from telegram import ReplyKeyboardMarkup as _RKM

        if isinstance(markup, _RKM):
            return
        await self._send_menu(context, self._callback_chat_of(update))

    async def _send_menu(self, context: Any, chat_id: ChatId) -> None:
        """Re-render the reply keyboard for one chat's menu state."""
        state = self._state_for(chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=await self.menu_text(state.menu, state.channel, chat_id=chat_id),
            reply_markup=self.reply_keyboard(state.menu, state.channel, chat_id=chat_id),
        )

    def _sending_rec_path(self, path: str) -> bool:
        """True when an upload of ``path`` already runs."""
        return path in self._sending_paths

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
                await message.reply_text("\u274c Unexpected error - see logs")

    async def _send_admin(self, text: str) -> None:
        if not self._enabled or self._app is None:
            logger.debug("[telegram] Admin notice dropped (bot disabled)")
            return
        try:
            await self._app.bot.send_message(chat_id=self._admin_id, text=text)
        except Exception:
            logger.warning("[telegram] Failed to notify admin", exc_info=True)

    def reply_keyboard(
        self, menu: str = "root", channel: str | None = None, chat_id: ChatId | None = None
    ) -> ReplyKeyboardMarkup:
        """Reply-keyboard rows for ``menu``. Button labels are the routing literals.

        The handlers pass the chat they serve, so the marks come from that
        chat's state. Without a chat id the admin chat supplies it, as in
        ``menu_text``. The state channel fills ``channel`` when the caller
        passes none.
        """
        state = self._state_for(chat_id if chat_id is not None else self._admin_id)
        return menus.render_keyboard(
            self, menu, channel if channel is not None else state.channel, rec_path=state.rec_path
        )

    async def menu_text(self, menu: str = "root", channel: str | None = None, chat_id: ChatId | None = None) -> str:
        """Return the status or instruction body shown above the reply keyboard for ``menu``."""
        state = self._state_for(chat_id if chat_id is not None else self._admin_id)
        return await menus.render_text(
            self,
            menu,
            channel if channel is not None else state.channel,
            state.custom,
            rec_channel=state.rec_channel,
            rec_path=state.rec_path,
        )

    async def handle_reply_text(self, text: str, chat_id: ChatId | None = None) -> MenuResult:
        """Route one reply-keyboard press or typed value for ``chat_id`` (admin by default)."""
        return await menus.dispatch_text(self, chat_id if chat_id is not None else self._admin_id, text)

    async def _open_recordings(self, chat_id: ChatId) -> MenuResult:
        """Open the recordings browser for ``chat_id`` (slash command entry)."""
        from stream_archive.telegram import menus_recordings as rec

        return await rec.open_recordings(self, chat_id)
