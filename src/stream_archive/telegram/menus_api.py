"""Reply-keyboard menu for the control API: enable, disable, and the key.

Each ``menu_*`` function routes one press for one chat. State reads and
writes go through that chat's ``MenuState`` only.
"""

from typing import TYPE_CHECKING

from telegram.constants import ParseMode

from stream_archive.telegram.menu_state import ChatId, MenuResult

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def _send_key_reply(ctrl: TelegramController, chat_id: ChatId, text: str) -> None:
    """Send one API reply as HTML, so a tap on the key copies it.

    The handler sends the message itself: the dispatcher sends plain text,
    and HTML renders the key as a code span. The ``None`` return then means
    "already answered" as well as "press not handled", so the dispatcher
    skips its apply-warning flush for these presses. No API change defers
    onto a running recording, so there is nothing to flush.
    """
    await ctrl._app.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=ctrl.reply_keyboard("api", chat_id=chat_id),
    )


async def menu_api(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the API menu: the toggle, the key display, and the key rotation."""
    if text == "Enable API":
        await _send_key_reply(ctrl, chat_id, await ctrl._set_api_enabled(True, chat_id=chat_id))
        return None
    if text == "Disable API":
        await _send_key_reply(ctrl, chat_id, await ctrl._set_api_enabled(False, chat_id=chat_id))
        return None
    if text == "Show key":
        await _send_key_reply(ctrl, chat_id, ctrl._api_key_text(chat_id=chat_id))
        return None
    if text == "Rotate key":
        await _send_key_reply(ctrl, chat_id, await ctrl._rotate_api_key(chat_id=chat_id))
        return None
    return None
