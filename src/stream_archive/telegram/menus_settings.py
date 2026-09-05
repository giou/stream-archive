"""Reply-keyboard menus for global settings: chat, mode, quality, limits, disk.

Each ``menu_*`` function routes one press or typed value for one chat. State
reads and writes go through that chat's ``MenuState`` only.
"""

from typing import TYPE_CHECKING

from stream_archive.telegram.commands_settings import _QUALITY_PRESETS
from stream_archive.telegram.menu_state import ChatId, MenuResult

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def menu_chat(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the platform pick of the chat menu."""
    state = ctrl._state_for(chat_id)
    if text in ("Twitch", "Kick"):
        state.menu = "chat_twitch" if text == "Twitch" else "chat_kick"
        return await ctrl.menu_text(state.menu, chat_id=chat_id), ctrl.reply_keyboard(state.menu)
    return None


async def menu_chat_platform(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the on/off pick for one chat platform."""
    state = ctrl._state_for(chat_id)
    if text in ("On", "Off"):
        platform = "twitch" if state.menu == "chat_twitch" else "kick"
        result = await ctrl.handle_chat([text.lower(), platform], chat_id=chat_id)
        state.menu = "chat"
        return result, ctrl.reply_keyboard("chat")
    return None


async def menu_mode(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the output-mode pick."""
    state = ctrl._state_for(chat_id)
    if text in ("disk", "youtube", "both"):
        result = ctrl.handle_mode([text], chat_id=chat_id)
        state.menu = "root"
        return result, ctrl.reply_keyboard("root")
    return None


async def menu_quality(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the global quality pick."""
    state = ctrl._state_for(chat_id)
    if text in _QUALITY_PRESETS:
        result = ctrl.handle_quality([text], chat_id=chat_id)
        state.menu = "root"
        return result, ctrl.reply_keyboard("root")
    return None


async def menu_retention(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the retention preset or open the custom value menu."""
    state = ctrl._state_for(chat_id)
    values = {"1 day": "1", "3 days": "3", "7 days": "7", "14 days": "14", "30 days": "30", "Off": "0"}
    if text in values:
        result = ctrl.handle_retention([values[text]], chat_id=chat_id)
        state.menu = "root"
        return result, ctrl.reply_keyboard("root")
    if text == "Custom":
        state.custom, state.menu = "retention", "custom"
        return await ctrl.menu_text("custom", chat_id=chat_id), ctrl.reply_keyboard("custom")
    return None


async def menu_limits(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the max-recordings / max-YouTube presets (``maxrec``/``maxyt``)."""
    state = ctrl._state_for(chat_id)
    menu = state.menu
    values = {"0 (unlimited)": "0", "1": "1", "2": "2", "3": "3", "5": "5"}
    if text in values:
        handler = ctrl.handle_maxrecordings if menu == "maxrec" else ctrl.handle_maxyoutube
        result = handler([values[text]], chat_id=chat_id)
        state.menu = "root"
        return result, ctrl.reply_keyboard("root")
    if text == "Custom":
        state.custom, state.menu = menu, "custom"
        return await ctrl.menu_text("custom", chat_id=chat_id), ctrl.reply_keyboard("custom")
    return None


async def menu_disk(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the disk menu: max total opens sizes, delete oldest toggles."""
    state = ctrl._state_for(chat_id)
    if text == "Max total":
        state.menu = "disk_maxsize"
        return await ctrl.menu_text("disk_maxsize", chat_id=chat_id), ctrl.reply_keyboard("disk_maxsize")
    if text == "Delete oldest":
        if ctrl._config.disk.delete_oldest:
            result = ctrl.handle_disk(["delete_oldest", "off"], chat_id=chat_id)
            return result, ctrl.reply_keyboard("disk")
        return (
            "Enable 'delete oldest'? When the disk is over the max total, the oldest recordings will be deleted.",
            ctrl._confirm_keyboard("confirm_delete_oldest", "on"),
        )
    return None


async def menu_disk_maxsize(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the max-total preset or open the custom value menu."""
    state = ctrl._state_for(chat_id)
    values = {"0": "0", "25": "25", "50": "50", "100": "100", "200": "200"}
    if text in values:
        result = ctrl.handle_disk(["maxsize", values[text]], chat_id=chat_id)
        state.menu = "disk"
        return result, ctrl.reply_keyboard("disk")
    if text == "Custom":
        state.custom, state.menu = state.menu, "custom"
        return await ctrl.menu_text("custom", chat_id=chat_id), ctrl.reply_keyboard("custom")
    return None


async def menu_custom(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Take any text as the value for the pending custom setting."""
    state = ctrl._state_for(chat_id)
    setting = state.custom or ""
    if setting == "retention":
        result = ctrl.handle_retention([text], chat_id=chat_id)
    elif setting == "maxrec":
        result = ctrl.handle_maxrecordings([text], chat_id=chat_id)
    elif setting == "maxyt":
        result = ctrl.handle_maxyoutube([text], chat_id=chat_id)
    elif setting == "channel_hold":
        result = ctrl.handle_channel_hold([state.channel or "", text], chat_id=chat_id)
    else:
        result = ctrl.handle_disk(["maxsize", text], chat_id=chat_id)
    if result.startswith("\u274c") or result.startswith("Usage"):
        return result, ctrl.reply_keyboard("custom")
    parent = (
        "channel" if setting == "channel_hold" else ("root" if setting in ("retention", "maxrec", "maxyt") else "disk")
    )
    state.menu = parent
    return result, ctrl.reply_keyboard(parent)
