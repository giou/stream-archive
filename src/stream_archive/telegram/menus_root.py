"""Reply-keyboard menus for channels: root, channel list, and per-channel settings.

Each ``menu_*`` function routes one press or typed value for one chat. State
reads and writes go through that chat's ``MenuState`` only.
"""

from typing import TYPE_CHECKING

from stream_archive.telegram.commands_settings import HOLD_CHOICES, MODE_CHOICES, QUALITY_CHOICES
from stream_archive.telegram.menu_state import ChatId, MenuResult

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def menu_root(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route a press on the root menu."""
    state = ctrl._state_for(chat_id)
    new_menu = {
        "Channels": "channels",
        "Output mode": "mode",
        "Quality": "quality",
        "Chat recording": "chat",
        "Storage & limits": "storage",
        "Remote access": "remote_access",
    }.get(text)
    if new_menu is None:
        return None
    state.menu = new_menu
    return await ctrl.menu_text(new_menu, chat_id=chat_id), ctrl.reply_keyboard(new_menu)


async def menu_channels(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route a press on the channel list."""
    state = ctrl._state_for(chat_id)
    if text == "Add channel":
        state.menu = "add_channel"
        return await ctrl.menu_text("add_channel", chat_id=chat_id), ctrl.reply_keyboard("add_channel")
    if text.startswith("\u2022 ") and text[2:] in ctrl._config.channels:
        state.menu, state.channel = "channel", text[2:]
        return (await ctrl.menu_text("channel", text[2:], chat_id=chat_id), ctrl.reply_keyboard("channel"))
    return None


async def menu_add_channel(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Take any text as a candidate channel name."""
    state = ctrl._state_for(chat_id)
    result = await ctrl.handle_add([text], chat_id=chat_id)
    if result.startswith("\u274c") or result.startswith("Usage"):
        return result, ctrl.reply_keyboard("add_channel")
    state.menu = "channels"
    return result, ctrl.reply_keyboard("channels")


async def menu_channel(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route a press on one channel's menu."""
    state = ctrl._state_for(chat_id)
    ch = state.channel
    if ch is None:
        return None
    if text == "Remove channel":
        return (
            f"Remove {ch} from monitoring? This stops any active recording and removes its output-mode override.",
            ctrl._confirm_keyboard("confirm_remove", ch),
        )
    if text == "Hold delay":
        state.menu = "channel_hold"
        return await ctrl.menu_text("channel_hold", ch, chat_id=chat_id), ctrl.reply_keyboard("channel_hold")
    if text == "Quality":
        state.menu = "channel_quality"
        return await ctrl.menu_text("channel_quality", ch, chat_id=chat_id), ctrl.reply_keyboard("channel_quality")
    mode = text.removeprefix("Mode: ")
    if mode in MODE_CHOICES:
        result = ctrl.handle_mode([ch, MODE_CHOICES[mode]], chat_id=chat_id)
        return result, ctrl.reply_keyboard("channel")
    if text == "Mode: Global":
        result = ctrl.handle_mode([ch, "default"], chat_id=chat_id)
        return result, ctrl.reply_keyboard("channel")
    return None


async def menu_channel_hold(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route a hold-delay preset, the global value, or open the custom value menu."""
    state = ctrl._state_for(chat_id)
    ch = state.channel
    if ch is None:
        return None
    if text in HOLD_CHOICES:
        result = ctrl.handle_channel_hold([ch, HOLD_CHOICES[text]], chat_id=chat_id)
        state.menu = "channel"
        return result, ctrl.reply_keyboard("channel")
    if text == "Global":
        result = ctrl.handle_channel_hold([ch, "default"], chat_id=chat_id)
        state.menu = "channel"
        return result, ctrl.reply_keyboard("channel")
    if text == "Custom":
        state.custom, state.menu = "channel_hold", "custom"
        return await ctrl.menu_text("custom", chat_id=chat_id), ctrl.reply_keyboard("custom")
    return None


async def menu_channel_quality(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route a quality preset or the global value for one channel."""
    state = ctrl._state_for(chat_id)
    ch = state.channel
    if ch is None:
        return None
    if text == "Global":
        result = ctrl.handle_quality([ch, "default"], chat_id=chat_id)
    elif text in QUALITY_CHOICES:
        result = ctrl.handle_quality([ch, QUALITY_CHOICES[text]], chat_id=chat_id)
    else:
        return None
    state.menu = "channel"
    return result, ctrl.reply_keyboard("channel")
