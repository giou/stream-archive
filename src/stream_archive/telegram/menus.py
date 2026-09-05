"""Reply-keyboard tables and routing for the Telegram bot.

``MENU`` maps each menu name to its render pair ``(text_fn, keyboard_fn)``.
``HANDLERS`` maps each menu name to the function that routes one press.
``dispatch_text`` is the only entry: Back handling plus a table lookup.
The dispatcher keeps no menu branches of its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from telegram import ReplyKeyboardMarkup

from stream_archive.telegram import menus_kick as kick_menus
from stream_archive.telegram import menus_root as root_menus
from stream_archive.telegram import menus_settings as settings_menus
from stream_archive.telegram.menu_state import ChatId, MenuResult, MenuState

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController

#: Render pair for one menu: body text plus keyboard.
MenuDef = tuple[
    "Callable[[TelegramController, MenuState], Awaitable[str]]",
    "Callable[[TelegramController, MenuState], ReplyKeyboardMarkup]",
]


def _frame(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    """Wrap button rows in a reply keyboard."""
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        input_field_placeholder="Tap a button or type a command",
    )


# Static keyboards. The channels menu lists live channels, so it builds dynamically.
_STATIC_KEYBOARDS: dict[str, list[list[str]]] = {
    "root": [
        ["Channels", "Status"],
        ["Chat recording", "Output mode"],
        ["Quality", "Retention"],
        ["Max recordings", "Max YouTube"],
        ["Disk", "Kick webhook"],
    ],
    "channel": [
        ["Back"],
        ["Mode: disk", "Mode: youtube"],
        ["Mode: both", "Mode: default"],
        ["Hold delay", "Quality"],
        ["Delete channel"],
    ],
    "channel_hold": [["0 (off)", "30s", "60s", "120s"], ["300s", "600s", "Default"], ["Custom", "Back"]],
    "channel_quality": [["best", "1080p", "720p"], ["480p", "360p", "audio_only"], ["Default"], ["Back"]],
    "chat": [["Twitch", "Kick"], ["Back"]],
    "chat_twitch": [["On", "Off"], ["Back"]],
    "chat_kick": [["On", "Off"], ["Back"]],
    "mode": [["disk", "youtube", "both"], ["Back"]],
    "quality": [["best", "1080p", "720p"], ["480p", "360p", "audio_only"], ["Back"]],
    "retention": [["1 day", "3 days", "7 days"], ["14 days", "30 days", "Off"], ["Custom", "Back"]],
    "maxrec": [["0 (unlimited)", "1", "2"], ["3", "5"], ["Custom", "Back"]],
    "maxyt": [["0 (unlimited)", "1", "2"], ["3", "5"], ["Custom", "Back"]],
    "disk": [["Max total"], ["Delete oldest"], ["Back"]],
    "disk_maxsize": [["0", "25", "50"], ["100", "200"], ["Custom", "Back"]],
    "kick_webhook": [["Off", "Cloudflare tunnel"], ["Tailscale funnel"], ["Back"]],
    "kick_cloudflare": [["Quick tunnel", "Named tunnel"], ["Back"]],
    "kick_cloudflare_dns": [["Skip"], ["Back"]],
    "kick_cloudflare_token": [["Back"]],
    "kick_cloudflare_hostname": [["Back"]],
    "add_channel": [["Back"]],
    "custom": [["Back"]],
}


def _keyboard(ctrl: TelegramController, state: MenuState, menu: str) -> ReplyKeyboardMarkup:
    """Build the keyboard for ``menu``."""
    if menu == "channels":
        rows = [["Back"], ["Add channel"], *([f"\u2022 {ch}"] for ch in ctrl._config.channels)]
        return _frame(rows)
    return _frame([list(row) for row in _STATIC_KEYBOARDS.get(menu, [["Back"]])])


async def _text_root(ctrl: TelegramController, state: MenuState) -> str:
    text: str = await ctrl.handle_status()
    return text


async def _text_channels(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    return f"Channels ({len(c.channels)}): {', '.join(c.channels)}\n\nTap a channel to manage it, or add a new one."


async def _text_add_channel(ctrl: TelegramController, state: MenuState) -> str:
    return (
        "Send the channel name or profile URL to monitor "
        "(Twitch: twitch:<name> or https://twitch.tv/...; Kick: kick:<name> or https://kick.com/...):"
    )


async def _text_kick_webhook(ctrl: TelegramController, state: MenuState) -> str:
    return f"Kick webhook: {ctrl._webhook_state_text()}\n\nChoose the tunnel you use to expose this service:"


async def _text_kick_cloudflare(ctrl: TelegramController, state: MenuState) -> str:
    return (
        "Cloudflare tunnel\n\n"
        "\u2022 Quick tunnel \u2014 no Cloudflare account needed, temporary URL.\n"
        "\u2022 Named tunnel \u2014 your Cloudflare account, stable hostname.\n"
        "\u2022 Already running your own tunnel? Send me its URL directly."
    )


async def _text_kick_cloudflare_token(ctrl: TelegramController, state: MenuState) -> str:
    return (
        "Send your tunnel token:\n\n"
        "cloudflared service install <TOKEN>\n\n"
        "Paste the whole command or just the token \u2014 I'll run cloudflared for you."
    )


async def _text_kick_cloudflare_hostname(ctrl: TelegramController, state: MenuState) -> str:
    return (
        "Send the public hostname to use for the webhook, e.g. kick.example.com.\n\n"
        "I'll point your tunnel at this app automatically \u2014 no dashboard configuration needed."
    )


async def _text_kick_cloudflare_dns(ctrl: TelegramController, state: MenuState) -> str:
    return (
        "Send your Cloudflare API token so I can create the DNS record automatically:\n\n"
        "dash.cloudflare.com \u2192 My Profile \u2192 API Tokens \u2192 Create Token:\n"
        "\u2022 Permissions: Zone \u2192 Read, DNS \u2192 Edit\n"
        "\u2022 Zone Resources: Include \u2192 your domain (e.g. the part after the "
        "dot of your hostname)\n"
        "(the 'Edit zone DNS' template has exactly those two)\n\n"
        "Or tap the Skip button to create the DNS record yourself \u2014 "
        "I'll give you the exact record."
    )


async def _text_channel(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    ch = state.channel or ""
    override = c.channel_output_modes.get(ch)
    mode = override or f"default (global: {c.output_mode})"
    q_override = c.channel_preferred_qualities.get(ch)
    quality_text = q_override or f"default (global: {c.preferred_quality})"
    hold_override = c.channel_youtube_hold_seconds.get(ch)
    hold_text = f"{hold_override:g}s" if hold_override is not None else f"default (global: {c.youtube.hold_seconds:g}s)"
    return f"Channel: {ch}\nOutput mode: {mode}\nQuality: {quality_text}\nHold delay: {hold_text}"


async def _text_channel_hold(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    ch = state.channel or ""
    hold_override = c.channel_youtube_hold_seconds.get(ch)
    eff = c.youtube.hold_seconds if hold_override is None else hold_override
    return (
        f"YouTube hold delay for {ch}: {eff:g}s (0 = end immediately)\n"
        f"Global default: {c.youtube.hold_seconds:g}s\n\n"
        "When the source stream stops, the broadcast stays open this long, waiting for the "
        "streamer to return \u2014 a return within the delay reuses the same broadcast instead "
        "of creating a new one."
    )


async def _text_channel_quality(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    ch = state.channel or ""
    q_override = c.channel_preferred_qualities.get(ch)
    return (
        f"Recording quality for {ch}: {q_override or f'default (global: {c.preferred_quality})'}\n\n"
        "audio_only records sound only; it forces output to disk (no YouTube re-stream)."
    )


async def _text_chat(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    kick_chat = c.kick.record_chat
    return (
        f"Chat recording (Twitch): {'on' if c.record_chat else 'off'}\n"
        f"Kick chat recording: {'on' if kick_chat else 'off'}\n\nChoose a platform:"
    )


async def _text_chat_twitch(ctrl: TelegramController, state: MenuState) -> str:
    return f"Twitch chat recording: {'on' if ctrl._config.record_chat else 'off'}. Choose:"


async def _text_chat_kick(ctrl: TelegramController, state: MenuState) -> str:
    kick_chat = ctrl._config.kick.record_chat
    return f"Kick chat recording: {'on' if kick_chat else 'off'}. Choose:"


async def _text_mode(ctrl: TelegramController, state: MenuState) -> str:
    return f"Output mode: {ctrl._config.output_mode}. Choose:"


async def _text_quality(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    text = f"Quality: {c.preferred_quality}. Choose:"
    if c.channel_preferred_qualities:
        text += "\nPer-channel: " + ", ".join(
            f"{ch} \u2192 {q}" for ch, q in sorted(c.channel_preferred_qualities.items())
        )
    return text


async def _text_retention(ctrl: TelegramController, state: MenuState) -> str:
    return f"Retention: {ctrl._config.retention_days} day(s) (0 = disabled). Choose:"


async def _text_maxrec(ctrl: TelegramController, state: MenuState) -> str:
    return f"Max recordings: {ctrl._config.max_concurrent_recordings} (0 = unlimited). Choose:"


async def _text_maxyt(ctrl: TelegramController, state: MenuState) -> str:
    return f"Max YouTube re-streams: {ctrl._config.max_concurrent_youtube_streams} (0 = unlimited). Choose:"


async def _text_disk(ctrl: TelegramController, state: MenuState) -> str:
    body: str = ctrl.handle_disk([])
    return body + "\n\nChoose a limit:"


async def _text_disk_maxsize(ctrl: TelegramController, state: MenuState) -> str:
    d = ctrl._config.disk
    return (
        f"Max total: {d.max_total_gb:g} GB (0 = disabled)\n"
        "Limits total recording size; when exceeded, the oldest recordings are deleted "
        "(or recording stops). Choose:"
    )


async def _text_custom(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    d = c.disk
    ch = state.channel or ""
    hold_override = c.channel_youtube_hold_seconds.get(ch)
    eff = c.youtube.hold_seconds if hold_override is None else hold_override
    labels = {
        "retention": (f"Retention: {c.retention_days} day(s) (0 = disabled)", " in days"),
        "maxrec": (f"Max recordings: {c.max_concurrent_recordings} (0 = unlimited)", ""),
        "maxyt": (f"Max YouTube re-streams: {c.max_concurrent_youtube_streams} (0 = unlimited)", ""),
        "disk_maxsize": (f"Max total: {d.max_total_gb:g} GB (0 = disabled)", " in GB"),
        "channel_hold": (f"Hold delay for {ch}: {eff:g}s (0 = end immediately)", " in seconds"),
    }
    label, units = labels[state.custom or ""]
    return f"{label}. Send the new value{units}:"


def _keys_for(menu: str) -> Callable[[TelegramController, MenuState], ReplyKeyboardMarkup]:
    """Build the keyboard function for ``menu``."""

    def build(ctrl: TelegramController, state: MenuState) -> ReplyKeyboardMarkup:
        return _keyboard(ctrl, state, menu)

    build.__name__ = f"keys_{menu}"
    return build


def _pair(menu: str, text_fn: Callable[[TelegramController, MenuState], Awaitable[str]]) -> MenuDef:
    return (text_fn, _keys_for(menu))


MENU: dict[str, MenuDef] = {
    "root": _pair("root", _text_root),
    "channels": _pair("channels", _text_channels),
    "add_channel": _pair("add_channel", _text_add_channel),
    "channel": _pair("channel", _text_channel),
    "channel_hold": _pair("channel_hold", _text_channel_hold),
    "channel_quality": _pair("channel_quality", _text_channel_quality),
    "chat": _pair("chat", _text_chat),
    "chat_twitch": _pair("chat_twitch", _text_chat_twitch),
    "chat_kick": _pair("chat_kick", _text_chat_kick),
    "mode": _pair("mode", _text_mode),
    "quality": _pair("quality", _text_quality),
    "retention": _pair("retention", _text_retention),
    "maxrec": _pair("maxrec", _text_maxrec),
    "maxyt": _pair("maxyt", _text_maxyt),
    "disk": _pair("disk", _text_disk),
    "disk_maxsize": _pair("disk_maxsize", _text_disk_maxsize),
    "custom": _pair("custom", _text_custom),
    "kick_webhook": _pair("kick_webhook", _text_kick_webhook),
    "kick_cloudflare": _pair("kick_cloudflare", _text_kick_cloudflare),
    "kick_cloudflare_token": _pair("kick_cloudflare_token", _text_kick_cloudflare_token),
    "kick_cloudflare_hostname": _pair("kick_cloudflare_hostname", _text_kick_cloudflare_hostname),
    "kick_cloudflare_dns": _pair("kick_cloudflare_dns", _text_kick_cloudflare_dns),
}

HANDLERS: dict[str, Callable[[TelegramController, ChatId, str], Awaitable[MenuResult]]] = {
    "root": root_menus.menu_root,
    "channels": root_menus.menu_channels,
    "add_channel": root_menus.menu_add_channel,
    "channel": root_menus.menu_channel,
    "channel_hold": root_menus.menu_channel_hold,
    "channel_quality": root_menus.menu_channel_quality,
    "chat": settings_menus.menu_chat,
    "chat_twitch": settings_menus.menu_chat_platform,
    "chat_kick": settings_menus.menu_chat_platform,
    "mode": settings_menus.menu_mode,
    "quality": settings_menus.menu_quality,
    "retention": settings_menus.menu_retention,
    "maxrec": settings_menus.menu_limits,
    "maxyt": settings_menus.menu_limits,
    "disk": settings_menus.menu_disk,
    "disk_maxsize": settings_menus.menu_disk_maxsize,
    "custom": settings_menus.menu_custom,
    "kick_webhook": kick_menus.menu_kick_webhook,
    "kick_cloudflare": kick_menus.menu_kick_cloudflare,
    "kick_cloudflare_token": kick_menus.menu_kick_token,
    "kick_cloudflare_hostname": kick_menus.menu_kick_hostname,
    "kick_cloudflare_dns": kick_menus.menu_kick_dns,
}

#: Back-button target per menu. Menus without an entry (root) have no Back button.
PARENT: dict[str, str] = {
    "channels": "root",
    "add_channel": "channels",
    "channel": "channels",
    "channel_hold": "channel",
    "channel_quality": "channel",
    "chat": "root",
    "chat_twitch": "chat",
    "chat_kick": "chat",
    "mode": "root",
    "quality": "root",
    "retention": "root",
    "maxrec": "root",
    "maxyt": "root",
    "disk": "root",
    "disk_maxsize": "disk",
    "kick_webhook": "root",
    "kick_cloudflare": "kick_webhook",
    "kick_cloudflare_token": "kick_cloudflare",
    "kick_cloudflare_hostname": "kick_cloudflare_token",
    "kick_cloudflare_dns": "kick_cloudflare_hostname",
}


def _custom_parent(custom: str) -> str:
    """Back-button target for the custom value menu."""
    if custom == "channel_hold":
        return "channel"
    if custom in ("retention", "maxrec", "maxyt"):
        return "root"
    return "disk"


async def menu_back(ctrl: TelegramController, chat_id: ChatId) -> MenuResult:
    """Move one chat up one menu level."""
    state = ctrl._state_for(chat_id)
    if state.menu == "custom":
        parent: str | None = _custom_parent(state.custom or "")
    else:
        parent = PARENT.get(state.menu)
    if parent is None:  # no Back button on root
        return None
    if state.menu in ("kick_cloudflare_hostname", "kick_cloudflare_dns"):
        state.cloudflare_hostname = None
    if parent == "channel":
        state.menu = "channel"
        return await ctrl.menu_text("channel", state.channel, chat_id=chat_id), ctrl.reply_keyboard("channel")
    state.menu, state.channel = parent, None
    return await ctrl.menu_text(parent, chat_id=chat_id), ctrl.reply_keyboard(parent)


async def dispatch_text(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route one reply-keyboard press or typed value for one chat.

    Return ``(reply_text, reply_markup)`` for ``reply_text(..., reply_markup=)``.
    Both markup kinds are valid there. Return ``None`` to ignore the message.
    """
    if text == "Back":
        return await menu_back(ctrl, chat_id)
    state = ctrl._state_for(chat_id)
    handler = HANDLERS.get(state.menu)
    if handler is None:  # unknown menu: reset to root instead of going silent
        state.menu, state.channel = "root", None
        return await ctrl.menu_text("root", chat_id=chat_id), ctrl.reply_keyboard("root")
    return await handler(ctrl, chat_id, text)


async def render_text(
    ctrl: TelegramController, menu: str, channel: str | None = None, custom: str | None = None
) -> str:
    """Render the body text for ``menu`` through the MENU table."""
    entry = MENU.get(menu)
    if entry is None:
        text: str = await ctrl.handle_status()
        return text
    text_fn, _ = entry
    state = MenuState(menu=menu, channel=channel, custom=custom)
    return await text_fn(ctrl, state)


def render_keyboard(ctrl: TelegramController, menu: str, channel: str | None = None) -> ReplyKeyboardMarkup:
    """Build the keyboard for ``menu`` through the MENU table."""
    entry = MENU.get(menu)
    if entry is None:
        return _frame([["Back"]])
    _, keyboard_fn = entry
    state = MenuState(menu=menu, channel=channel)
    return keyboard_fn(ctrl, state)
