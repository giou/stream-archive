"""Reply-keyboard menu for the control API: enable, disable, and the key.

Each ``menu_*`` function routes one press for one chat. State reads and
writes go through that chat's ``MenuState`` only.
"""

from typing import TYPE_CHECKING

from stream_archive.telegram.menu_state import ChatId, MenuResult

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def menu_api(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the API menu: the toggle, the key display, and the key rotation."""
    if text == "Enable API":
        return await ctrl._set_api_enabled(True, chat_id=chat_id), ctrl.reply_keyboard("api", chat_id=chat_id)
    if text == "Disable API":
        return await ctrl._set_api_enabled(False, chat_id=chat_id), ctrl.reply_keyboard("api", chat_id=chat_id)
    if text == "Show key":
        return ctrl._api_key_text(), ctrl.reply_keyboard("api", chat_id=chat_id)
    if text == "Rotate key":
        return await ctrl._rotate_api_key(chat_id=chat_id), ctrl.reply_keyboard("api", chat_id=chat_id)
    return None
