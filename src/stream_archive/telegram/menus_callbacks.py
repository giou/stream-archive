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

from stream_archive.telegram.menu_state import ChatId

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
        pending_key = (chat_id, parts[1])
        ctrl._pending_audio_switch.pop(pending_key, None)  # a later confirm press is harmless
        # The caller drops the inline keyboard after a cancel, so the
        # Apply-now button of the same nonce is gone. Do not keep its entry.
        ctrl._pending_apply.pop(pending_key, None)
        ctrl._apply_warnings_sent.discard(pending_key)
        ctrl._mark_confirm_done(chat_id, data)
        return "Cancelled \u2014 nothing changed", None
    if action == "confirm_remove" and len(parts) >= 3:
        if (chat_id, data) in ctrl._confirm_done:  # double-tap on the same message
            return None
        # The channel sits between the action and the nonce. The channel
        # name can itself contain ':' (kick:<slug>), so rejoin the middle parts.
        value = ":".join(parts[1:-1])
        if value not in ctrl._config.channels:
            return f"{value} is no longer monitored", None  # stale confirm message
        ctrl._mark_confirm_done(chat_id, data)
        result = await ctrl.handle_remove([value], chat_id=chat_id)  # stops recording + eventsub, clears override
        state.menu, state.channel = "channels", None
        return result, None
    if action == "confirm_delete_oldest" and len(parts) == 3 and parts[1] == "on":
        if (chat_id, data) in ctrl._confirm_done:  # double-tap on the same message
            return None
        ctrl._mark_confirm_done(chat_id, data)
        result = ctrl.handle_disk(["delete_oldest", "on"], chat_id=chat_id)
        state.menu = "disk"
        return result, None
    if action == "apply_now" and len(parts) == 2:
        if (chat_id, data) in ctrl._confirm_done:  # double-tap on the same message
            return None
        key = (chat_id, parts[1])
        pending = ctrl._pending_apply.pop(key, None)
        if pending is None:
            return None  # stale message: the bot restarted or handled it
        ctrl._apply_warnings_sent.discard(key)
        ctrl._mark_confirm_done(chat_id, data)
        summary, channels = pending
        lines = []
        failed = False
        for ch in channels:
            try:
                ok = await ctrl._recorder.restart(ch)
            except Exception:
                # One failed restart must not discard the other channels of the
                # prompt, and must not escape as a plain "Unexpected error".
                logger.exception("[telegram] Failed to restart the recording of %s", ch)
                lines.append(f"{ch}: restart failed \u2014 see logs")
                failed = True
                continue
            lines.append(f"{ch}: {'restarted with the new settings' if ok else 'no longer recording'}")
        head = "\u26a0\ufe0f Applied with errors" if failed else "\u2705 Applied"
        return f"{head}: {summary}\n" + "\n".join(lines), None
    if action == "audio_confirm" and len(parts) == 2:
        if (chat_id, data) in ctrl._confirm_done:
            return None
        key = (chat_id, parts[1])
        audio_pending = ctrl._pending_audio_switch.pop(key, None)
        if audio_pending is None:
            return None  # the message is stale: the bot handled it or restarted
        ctrl._mark_confirm_done(chat_id, data)
        ctrl._apply_warnings_sent.discard(key)
        quality_mutate, channels = audio_pending

        def combined(candidate: Any) -> None:
            quality_mutate(candidate)
            live = set(candidate.channels)
            for ch in channels:
                if ch in live:
                    candidate.channel_output_modes[ch] = "disk"

        try:
            result = ctrl._apply(
                combined,
                lambda c: f"Quality set to audio_only; output mode disk for {', '.join(channels)}",
                chat_id,
            )
        except Exception:
            # The press is already marked handled, so report the failure
            # instead of escaping with a generic error and no explanation.
            logger.exception("[telegram] Failed to apply the audio-only switch")
            return "\u274c The change failed \u2014 see logs", None
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
        chat_id, nonce = key
        # A cancel or apply-now press can pop the entry during the awaits
        # of this loop, so read it and never raise on a missing key.
        entry = ctrl._pending_apply.get(key)
        if entry is None:
            continue
        summary, channels = entry
        channels = [ch for ch in channels if ctrl._recorder.is_recording(ch)]
        if not channels:
            # Every affected recording already ended (the channel was
            # removed or the stream finished). Nothing can be applied
            # to a running recording, so drop the stale prompt.
            ctrl._pending_apply.pop(key, None)
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
            # The entry stays pending, so every later trigger retries this
            # send. Log the chat and the nonce to keep the repeat diagnosable.
            logger.warning(
                "[telegram] Failed to send apply-now warning to chat %s (nonce %s)", chat_id, nonce, exc_info=True
            )
    for key in list(ctrl._pending_audio_switch):
        if key in ctrl._apply_warnings_sent:
            continue
        chat_id, nonce = key
        # A cancel or confirm press can pop the entry during the awaits of
        # this loop, so read it and never raise on a missing key.
        audio_entry = ctrl._pending_audio_switch.get(key)
        if audio_entry is None:
            continue
        _mutate, channels = audio_entry
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
            logger.warning(
                "[telegram] Failed to send audio-only warning to chat %s (nonce %s)", chat_id, nonce, exc_info=True
            )
