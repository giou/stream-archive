"""Reply-keyboard menu for the web panel: enable, disable, and the password.

Each ``menu_*`` function routes one press for one chat. State reads and
writes go through that chat's ``MenuState`` only.
"""

from typing import TYPE_CHECKING

from telegram.constants import ParseMode

from stream_archive.telegram.menu_state import ChatId, MenuResult

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def _send_web_reply(ctrl: TelegramController, chat_id: ChatId, text: str) -> None:
    """Send one web reply as HTML, so a tap on the password copies it.

    The handler sends the message itself: the dispatcher sends plain text,
    and HTML renders the password as a code span. The ``None`` return then
    means "already answered" as well as "press not handled", so the
    dispatcher skips its apply-warning flush for these presses. Flush them
    here instead: Enable and New password go through the shared apply path,
    which can stash a warning for a running recording.
    """
    await ctrl._app.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=ctrl.reply_keyboard("web", chat_id=chat_id),
    )
    await ctrl._maybe_send_apply_warnings()


async def menu_web(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the web menu: the toggle and the password replacement."""
    if text == "Enable Web panel":
        await _send_web_reply(ctrl, chat_id, await ctrl._set_web_enabled(True, chat_id=chat_id))
        return None
    if text == "Disable Web panel":
        await _send_web_reply(ctrl, chat_id, await ctrl._set_web_enabled(False, chat_id=chat_id))
        return None
    if text == "New password":
        await _send_web_reply(ctrl, chat_id, await ctrl._new_web_password(chat_id=chat_id))
        return None
    return None
