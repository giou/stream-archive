"""Reply-keyboard menus for global settings: chat, mode, quality, limits, disk.

Each ``menu_*`` function routes one press or typed value for one chat. State
reads and writes go through that chat's ``MenuState`` only.
"""

from typing import TYPE_CHECKING

from stream_archive.telegram.commands_settings import (
    COUNT_CHOICES,
    DISK_SIZE_CHOICES,
    MODE_CHOICES,
    QUALITY_CHOICES,
    RETENTION_CHOICES,
)
from stream_archive.telegram.menu_state import ChatId, MenuResult

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def menu_chat(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the two chat-capture toggles."""
    toggles = {
        "Enable Twitch chat": ["on", "twitch"],
        "Disable Twitch chat": ["off", "twitch"],
        "Enable Kick chat": ["on", "kick"],
        "Disable Kick chat": ["off", "kick"],
    }
    args = toggles.get(text)
    if args is None:
        return None
    result = await ctrl.handle_chat(args, chat_id=chat_id)
    return result, ctrl.reply_keyboard("chat")


async def menu_mode(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the output-mode pick."""
    state = ctrl._state_for(chat_id)
    if text in MODE_CHOICES:
        result = ctrl.handle_mode([MODE_CHOICES[text]], chat_id=chat_id)
        state.menu = "root"
        return result, ctrl.reply_keyboard("root")
    return None


async def menu_quality(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the global quality pick."""
    state = ctrl._state_for(chat_id)
    if text in QUALITY_CHOICES:
        result = ctrl.handle_quality([QUALITY_CHOICES[text]], chat_id=chat_id)
        state.menu = "root"
        return result, ctrl.reply_keyboard("root")
    return None


async def menu_retention(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the retention preset or open the custom value menu."""
    state = ctrl._state_for(chat_id)
    if text in RETENTION_CHOICES:
        result = ctrl.handle_retention([RETENTION_CHOICES[text]], chat_id=chat_id)
        state.menu = "storage"
        return result, ctrl.reply_keyboard("storage")
    if text == "Custom":
        state.custom, state.menu = "retention", "custom"
        return await ctrl.menu_text("custom", chat_id=chat_id), ctrl.reply_keyboard("custom")
    return None


async def menu_limits(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the max-recordings / max-restreams presets (``maxrec``/``maxyt``)."""
    state = ctrl._state_for(chat_id)
    menu = state.menu
    if text in COUNT_CHOICES:
        handlers = {"maxrec": ctrl.handle_maxrecordings, "maxyt": ctrl.handle_maxyoutube}
        handler = handlers.get(menu)
        if handler is None:  # unknown limits menu: never guess a setting
            return None
        result = handler([COUNT_CHOICES[text]], chat_id=chat_id)
        state.menu = "storage"
        return result, ctrl.reply_keyboard("storage")
    if text == "Custom":
        state.custom, state.menu = menu, "custom"
        return await ctrl.menu_text("custom", chat_id=chat_id), ctrl.reply_keyboard("custom")
    return None


async def menu_storage(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Storage & limits menu."""
    state = ctrl._state_for(chat_id)
    new_menu = {
        "Retention": "retention",
        "Disk limits": "disk",
        "Max recordings": "maxrec",
        "Max restreams": "maxyt",
    }.get(text)
    if new_menu is None:
        return None
    state.menu = new_menu
    return await ctrl.menu_text(new_menu, chat_id=chat_id), ctrl.reply_keyboard(new_menu)


async def menu_disk(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the disk menu: max total size opens the sizes, delete oldest toggles."""
    state = ctrl._state_for(chat_id)
    if text == "Max total size":
        state.menu = "disk_maxsize"
        return await ctrl.menu_text("disk_maxsize", chat_id=chat_id), ctrl.reply_keyboard("disk_maxsize")
    if text == "Enable delete oldest":
        return (
            "Enable 'delete oldest'? When the disk is over the max total size, the oldest recordings will be deleted.",
            ctrl._confirm_keyboard("confirm_delete_oldest", "on"),
        )
    if text == "Disable delete oldest":
        result = ctrl.handle_disk(["delete_oldest", "off"], chat_id=chat_id)
        return result, ctrl.reply_keyboard("disk")
    return None


async def menu_disk_maxsize(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the max-total-size preset or open the custom value menu."""
    state = ctrl._state_for(chat_id)
    if text in DISK_SIZE_CHOICES:
        result = ctrl.handle_disk(["maxsize", DISK_SIZE_CHOICES[text]], chat_id=chat_id)
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
    elif setting == "disk_maxsize":
        result = ctrl.handle_disk(["maxsize", text], chat_id=chat_id)
    else:  # unknown custom setting: never write it into another setting
        return None
    if result.startswith("\u274c") or result.startswith("Usage"):
        return result, ctrl.reply_keyboard("custom")
    parent = (
        "channel"
        if setting == "channel_hold"
        else ("storage" if setting in ("retention", "maxrec", "maxyt") else "disk")
    )
    state.menu = parent
    # The channel menu marks the overrides of the selected channel, so it
    # needs the channel. Other parent menus are global.
    return result, ctrl.reply_keyboard(parent, state.channel if parent == "channel" else None)
