"""Confirm-button handling and deferred apply warnings for the Telegram bot.

Prompts live under ``(chat_id, nonce)`` keys, so one chat can never confirm
another chat's prompt. Plain string keys predate per-chat state; they read as
the acting chat's entry.
"""

import logging
import secrets
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

from stream_archive.telegram.menu_state import ChatId, split_key

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController

logger = logging.getLogger(__name__)


class AdminCallbackQueryHandler(CallbackQueryHandler[Any, Any]):
    """CallbackQueryHandler that only fires for one user.

    PTB's CallbackQueryHandler takes no ``filters`` (unlike CommandHandler),
    so the admin gate lives here instead of the callback body. Non-admin
    presses never reach the callback.
    """

    def __init__(self, *args: Any, admin_id: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._admin_id = admin_id

    def check_update(self, update: object) -> bool | object | None:
        user = getattr(update, "effective_user", None)
        if user is None or getattr(user, "id", None) != self._admin_id:
            return None
        return super().check_update(update)


def confirm_keyboard(action: str, value: str) -> InlineKeyboardMarkup:
    """Build a confirm/cancel keyboard with a unique nonce per message."""
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


def _lookup_pending(pending: dict[Any, Any], chat_id: ChatId, nonce: str) -> tuple[Any, Any | None]:
    """Find a pending entry for ``(chat_id, nonce)``, else a legacy plain key."""
    key = (chat_id, nonce)
    if key in pending:
        return key, pending[key]
    if nonce in pending:
        return nonce, pending[nonce]
    return key, None


async def handle_callback(ctrl: TelegramController, data: str, chat_id: ChatId) -> tuple[str, Any] | None:
    """Apply one confirmation-button press for ``chat_id``.

    Return ``(reply_text, markup)`` on success or ``None`` for an unknown
    or already handled press. Wire format (from ``confirm_keyboard``):
    ``confirm_<action>:<value>:<nonce>`` and ``cancel:<nonce>``. Apply-now
    warnings use ``apply_now:<nonce>``, audio-only switches use
    ``audio_confirm:<nonce>``. The nonce makes every confirm message's
    buttons unique, so the double-tap guard covers only the same message.
    """
    state = ctrl._state_for(chat_id)
    parts = data.split(":")
    action = parts[0]
    if action == "cancel" and len(parts) == 2:
        if (chat_id, data) in ctrl._confirm_done:  # double-tap on the same message
            return None
        _key, audio_pending = _lookup_pending(ctrl._pending_audio_switch, chat_id, parts[1])
        if audio_pending is not None:
            del ctrl._pending_audio_switch[_key]  # a later confirm press is harmless
        ctrl._confirm_done.add((chat_id, data))
        return "Cancelled \u2014 nothing changed", None
    if action == "confirm_remove" and len(parts) >= 3:
        if (chat_id, data) in ctrl._confirm_done:  # double-tap on the same message
            return None
        # The channel sits between the action and the nonce. The channel
        # name can itself contain ':' (kick:<slug>), so rejoin the middle parts.
        value = ":".join(parts[1:-1])
        if value not in ctrl._config.channels:
            return f"{value} is no longer monitored", None  # stale confirm message
        ctrl._confirm_done.add((chat_id, data))
        result = await ctrl.handle_remove([value], chat_id=chat_id)  # stops recording + eventsub, clears override
        state.menu, state.channel = "channels", None
        return result, None
    if action == "confirm_delete_oldest" and len(parts) == 3 and parts[1] == "on":
        if (chat_id, data) in ctrl._confirm_done:  # double-tap on the same message
            return None
        ctrl._confirm_done.add((chat_id, data))
        result = ctrl.handle_disk(["delete_oldest", "on"], chat_id=chat_id)
        state.menu = "disk"
        return result, None
    if action == "apply_now" and len(parts) == 2:
        if (chat_id, data) in ctrl._confirm_done:  # double-tap on the same message
            return None
        key, pending = _lookup_pending(ctrl._pending_apply, chat_id, parts[1])
        if pending is None:
            return None  # stale message: bot restarted or already handled
        del ctrl._pending_apply[key]
        ctrl._apply_warnings_sent.discard(key)
        ctrl._apply_warnings_sent.discard(parts[1])
        ctrl._confirm_done.add((chat_id, data))
        summary, channels = pending
        lines = []
        for ch in channels:
            ok = await ctrl._recorder.restart(ch)
            lines.append(f"{ch}: {'restarted with the new settings' if ok else 'no longer recording'}")
        return f"\u2705 Applied: {summary}\n" + "\n".join(lines), None
    if action == "audio_confirm" and len(parts) == 2:
        if (chat_id, data) in ctrl._confirm_done:
            return None
        key, audio_pending = _lookup_pending(ctrl._pending_audio_switch, chat_id, parts[1])
        if audio_pending is None:
            return None  # the message is stale: the bot handled it or restarted
        del ctrl._pending_audio_switch[key]
        ctrl._confirm_done.add((chat_id, data))
        ctrl._apply_warnings_sent.discard(key)
        ctrl._apply_warnings_sent.discard(parts[1])
        quality_mutate, channels = audio_pending

        def combined(candidate: Any) -> None:
            quality_mutate(candidate)
            live = set(candidate.channels)
            for ch in channels:
                if ch in live:
                    candidate.channel_output_modes[ch] = "disk"

        result = ctrl._apply(
            combined,
            lambda c: f"Quality set to audio_only; output mode disk for {', '.join(channels)}",
            chat_id,
        )
        return result, None
    return None


async def maybe_send_apply_warnings(ctrl: TelegramController) -> None:
    """Send apply-now warnings stashed by _apply.

    _apply stores warnings when deferred-effect settings changed while
    channels recorded. Entries stay in ``_pending_apply`` until the
    admin answers. The nonce in the message callback data must still
    resolve when the admin taps a button. ``_apply_warnings_sent``
    records which nonces the bot already messaged, so a later trigger
    does not resend. A prompt whose affected recordings have all ended
    (removed channel or finished stream) is dropped, not sent.
    """
    for key in list(ctrl._pending_apply):
        chat_id, nonce = split_key(key, ctrl._admin_id)
        summary, channels = ctrl._pending_apply[key]
        channels = [ch for ch in channels if ctrl._recorder.is_recording(ch)]
        if not channels:
            # Every affected recording already ended (the channel was
            # removed or the stream finished). Nothing can be applied
            # to a running recording, so drop the stale prompt.
            del ctrl._pending_apply[key]
            ctrl._apply_warnings_sent.discard(key)
            continue
        if key in ctrl._apply_warnings_sent:
            continue
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
            await ctrl._app.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
            ctrl._apply_warnings_sent.add(key)
        except Exception:
            logger.warning("[telegram] Failed to send apply-now warning", exc_info=True)
    for key in list(ctrl._pending_audio_switch):
        if key in ctrl._apply_warnings_sent:
            continue
        chat_id, nonce = split_key(key, ctrl._admin_id)
        _mutate, channels = ctrl._pending_audio_switch[key]
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
            await ctrl._app.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
            ctrl._apply_warnings_sent.add(key)
        except Exception:
            logger.warning("[telegram] Failed to send audio-only warning", exc_info=True)
