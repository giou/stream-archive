"""Reply-keyboard menu for MTProto upload: the Remote access toggle."""

from typing import TYPE_CHECKING

from stream_archive.telegram.menu_state import ChatId, MenuResult

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def menu_mtproto(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the MTProto toggle. Credentials stay in config.json, never here."""
    if text == "Enable MTProto upload":
        result = await ctrl._set_mtproto_enabled(True, chat_id=chat_id)
        return result, ctrl.reply_keyboard("mtproto", chat_id=chat_id)
    if text == "Disable MTProto upload":
        result = await ctrl._set_mtproto_enabled(False, chat_id=chat_id)
        return result, ctrl.reply_keyboard("mtproto", chat_id=chat_id)
    return None
