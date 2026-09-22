"""Reply-keyboard menus for channels: root, channel list, and per-channel settings.

Each ``menu_*`` function routes one press or typed value for one chat. State
reads and writes go through that chat's ``MenuState`` only.
"""

from typing import TYPE_CHECKING

from stream_archive.telegram.commands_settings import HOLD_CHOICES, MODE_CHOICES, QUALITY_CHOICES
from stream_archive.telegram.menu_state import (
    CHANNEL_BUTTON_PREFIX,
    ChatId,
    MenuResult,
    MenuState,
    is_error,
    open_menu,
    pick_preset,
)

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


def _channel_gone(ctrl: TelegramController, state: MenuState) -> str | None:
    """Notice when the channel of ``state`` is not selected, or not monitored.

    A chat can hold a channel menu while the channel leaves the monitoring
    list through /remove or the control API. A per-channel setting must then
    not write an override for a channel that nobody records.
    """
    ch = state.channel
    if ch and ch in ctrl._config.channels:
        return None
    return f"{ch} is no longer monitored." if ch else "That channel is no longer selected."


async def menu_root(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route a press on the root menu."""
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
    return await open_menu(ctrl, new_menu, chat_id)


async def menu_channels(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route a press on the channel list."""
    if text == "Add channel":
        ctrl._enter_menu(chat_id, "add_channel")
        return await ctrl.menu_text("add_channel", chat_id=chat_id), ctrl.reply_keyboard("add_channel", chat_id=chat_id)
    if text.startswith(CHANNEL_BUTTON_PREFIX):
        # The keyboard outlives the channel when /remove or the control API
        # drops it, so answer a press instead of dropping it. A blank suffix
        # is a malformed label, not a channel.
        ch = text[len(CHANNEL_BUTTON_PREFIX) :].strip()
        if not ch:
            return None
        if ch in ctrl._config.channels:
            ctrl._enter_menu(chat_id, "channel", channel=ch)
            return (
                await ctrl.menu_text("channel", ch, chat_id=chat_id),
                ctrl.reply_keyboard("channel", chat_id=chat_id),
            )
        ctrl._enter_menu(chat_id, "channels")
        return f"{ch} is no longer monitored.", ctrl.reply_keyboard("channels", chat_id=chat_id)
    return None


async def menu_add_channel(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Take any text as a candidate channel name."""
    result = await ctrl.handle_add([text], chat_id=chat_id)
    if is_error(result):
        return result, ctrl.reply_keyboard("add_channel", chat_id=chat_id)
    ctrl._enter_menu(chat_id, "channels")
    return result, ctrl.reply_keyboard("channels", chat_id=chat_id)


async def menu_channel(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route a press on one channel's menu."""
    state = ctrl._state_for(chat_id)
    ch = state.channel or ""
    gone = _channel_gone(ctrl, state)
    if gone is not None:
        ctrl._enter_menu(chat_id, "channels")
        return gone, ctrl.reply_keyboard("channels", chat_id=chat_id)
    if text == "Remove channel":
        return (
            f"Remove {ch} from monitoring? This stops any active recording and removes its output-mode override.",
            ctrl._confirm_keyboard("confirm_remove", ch),
        )
    if text == "Mode":
        return await open_menu(ctrl, "channel_mode", chat_id, channel=ch)
    if text == "Hold delay":
        return await open_menu(ctrl, "channel_hold", chat_id, channel=ch)
    if text == "Quality":
        return await open_menu(ctrl, "channel_quality", chat_id, channel=ch)
    return None


async def menu_channel_mode(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route an output-mode preset or the global value for one channel."""
    state = ctrl._state_for(chat_id)
    ch = state.channel or ""
    gone = _channel_gone(ctrl, state)
    if gone is not None:
        ctrl._enter_menu(chat_id, "channels")
        return gone, ctrl.reply_keyboard("channels", chat_id=chat_id)
    return await pick_preset(
        ctrl,
        text,
        MODE_CHOICES,
        lambda value: ctrl.handle_mode([ch, value], chat_id=chat_id),
        "channel",
        chat_id,
        global_value="default",
    )


async def menu_channel_hold(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route a hold-delay preset, the global value, or open the custom value menu."""
    state = ctrl._state_for(chat_id)
    ch = state.channel or ""
    gone = _channel_gone(ctrl, state)
    if gone is not None:
        ctrl._enter_menu(chat_id, "channels")
        return gone, ctrl.reply_keyboard("channels", chat_id=chat_id)
    if text in HOLD_CHOICES:
        result = ctrl.handle_channel_hold([ch, HOLD_CHOICES[text]], chat_id=chat_id)
        ctrl._enter_menu(chat_id, "channel")
        return result, ctrl.reply_keyboard("channel", chat_id=chat_id)
    if text == "Global":
        result = ctrl.handle_channel_hold([ch, "default"], chat_id=chat_id)
        ctrl._enter_menu(chat_id, "channel")
        return result, ctrl.reply_keyboard("channel", chat_id=chat_id)
    if text == "Custom":
        state.custom = "channel_hold"
        ctrl._enter_menu(chat_id, "custom")
        return await ctrl.menu_text("custom", chat_id=chat_id), ctrl.reply_keyboard("custom", chat_id=chat_id)
    return None


async def menu_channel_quality(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route a quality preset or the global value for one channel."""
    state = ctrl._state_for(chat_id)
    ch = state.channel or ""
    gone = _channel_gone(ctrl, state)
    if gone is not None:
        ctrl._enter_menu(chat_id, "channels")
        return gone, ctrl.reply_keyboard("channels", chat_id=chat_id)
    return await pick_preset(
        ctrl,
        text,
        QUALITY_CHOICES,
        lambda value: ctrl.handle_quality([ch, value], chat_id=chat_id),
        "channel",
        chat_id,
        global_value="default",
    )
