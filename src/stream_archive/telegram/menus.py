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

from stream_archive.config import api_base_url
from stream_archive.telegram import menus_api as api_menus
from stream_archive.telegram import menus_kick as kick_menus
from stream_archive.telegram import menus_root as root_menus
from stream_archive.telegram import menus_settings as settings_menus
from stream_archive.telegram.commands_settings import (
    COUNT_CHOICES,
    DISK_SIZE_CHOICES,
    HOLD_CHOICES,
    MODE_CHOICES,
    QUALITY_CHOICES,
    RETENTION_CHOICES,
)
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


#: Prefix of the button that holds the current value.
_ACTIVE = "\u2713 "


def _mark(label: str, active: bool) -> str:
    """Prefix the label with the active marker when ``active`` is true."""
    return f"{_ACTIVE}{label}" if active else label


def _unmark(text: str) -> str:
    """Remove the active marker from a pressed button label."""
    return text[len(_ACTIVE) :] if text.startswith(_ACTIVE) else text


def _toggle_action(enabled: bool) -> str:
    """Verb of the one-button toggle for one state."""
    return "Disable" if enabled else "Enable"


def _rows(choices: dict[str, str], width: int) -> list[list[str]]:
    """Wrap the labels of a label-to-value table into rows of ``width``."""
    labels = list(choices)
    return [labels[i : i + width] for i in range(0, len(labels), width)]


def _marked_rows(layout: list[list[str]], choices: dict[str, str], current: str | None) -> list[list[str]]:
    """Mark the cells whose label maps to ``current``."""
    return [[_mark(label, choices.get(label) == current) for label in row] for row in layout]


# Keyboards that do not read config state.
_STATIC_KEYBOARDS: dict[str, list[list[str]]] = {
    "root": [["Channels", "Output mode"], ["Quality", "Chat recording"], ["Storage & limits", "Remote access"]],
    "add_channel": [["Back"]],
    "custom": [["Back"]],
    "kick_cloudflare_token": [["Back"]],
    "kick_cloudflare_hostname": [["Back"]],
    "kick_cloudflare_dns": [["Skip DNS"], ["Back"]],
}


def _keyboard(ctrl: TelegramController, state: MenuState, menu: str) -> ReplyKeyboardMarkup:
    """Build the keyboard for ``menu``."""
    c = ctrl._config
    if menu == "channels":
        # Back stays in the first row: a long channel list would push it out of view.
        return _frame([["Back"], ["Add channel"], *([f"\u2022 {ch}"] for ch in c.channels)])
    if menu == "channel":
        ch = state.channel or ""
        mode_override = c.channel_output_modes.get(ch)
        buttons = [_mark(f"Mode: {label}", value == mode_override) for label, value in MODE_CHOICES.items()]
        buttons.append(_mark("Mode: Global", mode_override is None))
        return _frame([buttons[:2], buttons[2:], ["Quality", "Hold delay"], ["Remove channel"], ["Back"]])
    if menu == "channel_hold":
        ch = state.channel or ""
        hold_override = c.channel_youtube_hold_seconds.get(ch)
        rows = _marked_rows(
            _rows(HOLD_CHOICES, 3), HOLD_CHOICES, f"{hold_override:g}" if hold_override is not None else None
        )
        rows.append([_mark("Global", hold_override is None), "Custom"])
        rows.append(["Back"])
        return _frame(rows)
    if menu == "channel_quality":
        ch = state.channel or ""
        quality_override = c.channel_preferred_qualities.get(ch)
        rows = _marked_rows(_rows(QUALITY_CHOICES, 3), QUALITY_CHOICES, quality_override)
        # No Custom button here: the quality value is a fixed label list,
        # and menu_channel_quality has no custom branch.
        rows.append([_mark("Global", quality_override is None)])
        rows.append(["Back"])
        return _frame(rows)
    if menu == "chat":
        return _frame(
            [
                [f"{_toggle_action(c.record_chat)} Twitch chat"],
                [f"{_toggle_action(c.kick.record_chat)} Kick chat"],
                ["Back"],
            ]
        )
    if menu == "mode":
        return _frame([*_marked_rows(_rows(MODE_CHOICES, 3), MODE_CHOICES, c.output_mode), ["Back"]])
    if menu == "quality":
        return _frame([*_marked_rows(_rows(QUALITY_CHOICES, 3), QUALITY_CHOICES, c.preferred_quality), ["Back"]])
    if menu == "retention":
        rows = _marked_rows(_rows(RETENTION_CHOICES, 3), RETENTION_CHOICES, f"{c.retention_days:g}")
        return _frame([*rows, ["Custom"], ["Back"]])
    if menu in ("maxrec", "maxyt"):
        count = c.max_concurrent_recordings if menu == "maxrec" else c.max_concurrent_youtube_streams
        rows = _marked_rows(_rows(COUNT_CHOICES, 3), COUNT_CHOICES, f"{count:g}")
        return _frame([*rows, ["Custom"], ["Back"]])
    if menu == "storage":
        return _frame([["Retention", "Disk limits"], ["Max recordings", "Max restreams"], ["Back"]])
    if menu == "disk":
        return _frame([["Max total size"], [f"{_toggle_action(c.disk.delete_oldest)} delete oldest"], ["Back"]])
    if menu == "disk_maxsize":
        rows = _marked_rows(_rows(DISK_SIZE_CHOICES, 3), DISK_SIZE_CHOICES, f"{c.disk.max_total_gb:g}")
        return _frame([*rows, ["Custom"], ["Back"]])
    if menu == "remote_access":
        return _frame(
            [
                [f"{_toggle_action(c.endpoint.enabled)} endpoint"],
                ["Cloudflare tunnel", "Tailscale funnel"],
                ["Kick webhook", "API"],
                ["Back"],
            ]
        )
    if menu == "api":
        return _frame([[f"{_toggle_action(c.api.enabled)} API"], ["Show key", "Rotate key"], ["Back"]])
    if menu == "kick_webhook":
        return _frame([[f"{_toggle_action(c.kick.webhook.enabled)} Kick webhook"], ["Back"]])
    if menu == "kick_cloudflare":
        return _frame(
            [
                [f"{_toggle_action(ctrl._tunnel_active('cloudflare'))} Cloudflare tunnel"],
                ["Quick tunnel", "Named tunnel"],
                ["Back"],
            ]
        )
    if menu == "kick_tailscale":
        return _frame([[f"{_toggle_action(ctrl._tunnel_active('tailscale'))} Tailscale funnel"], ["Back"]])
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


async def _text_remote_access(ctrl: TelegramController, state: MenuState) -> str:
    return (
        f"Endpoint: {ctrl._endpoint_state_text()}\n"
        f"Kick webhook: {ctrl._webhook_state_text()}\n"
        f"Control API: {ctrl._api_state_text()}\n\n"
        "Cloudflare tunnel and Tailscale funnel set the public URL. The toggle starts and stops "
        "the endpoint. The Kick webhook and the control API are served there."
    )


async def _text_api(ctrl: TelegramController, state: MenuState) -> str:
    if not ctrl._config.api.enabled:
        return (
            "Control API: off\n\n"
            "The API manages channels and settings over HTTP under /api/v1/, on the same "
            "listener and public URL as the Kick webhook.\n"
            "Enable it and I generate the API key."
        )
    base = api_base_url(ctrl._config)
    url_line = f"Base URL: {base}" if base else "Base URL: none yet \u2014 set up a tunnel under Remote access."
    text = f"Control API: on\n{url_line}\nTap Show key to display the key. Changes apply on the next cycle."
    if not ctrl._config.kick.webhook.enabled:
        text += "\nThe public URL needs remote access. Turn it on to reach the API from outside."
    return text


async def _text_kick_webhook(ctrl: TelegramController, state: MenuState) -> str:
    text = f"Kick webhook: {ctrl._webhook_state_text()}\n"
    if not ctrl._config.endpoint.enabled:
        text += "The endpoint is off, so Kick cannot deliver events.\n"
    return text + "\nThe endpoint and its tunnels are set in Remote access."


async def _text_kick_cloudflare(ctrl: TelegramController, state: MenuState) -> str:
    ep = ctrl._config.endpoint
    active = ctrl._tunnel_active("cloudflare")
    text = f"Cloudflare tunnel: {'on' if active else 'off'}\n"
    if not active and ep.tunnel == "cloudflare" and ep.public_url:
        text += f"Saved setup: {ep.public_url}. Tap Enable to restore it.\n"
    return (
        f"{text}\n"
        "\u2022 Quick tunnel \u2014 no Cloudflare account needed, temporary URL.\n"
        "\u2022 Named tunnel \u2014 your Cloudflare account, stable hostname.\n"
        "\u2022 Already running your own tunnel? Send me its URL directly."
    )


async def _text_kick_tailscale(ctrl: TelegramController, state: MenuState) -> str:
    ep = ctrl._config.endpoint
    active = ctrl._tunnel_active("tailscale")
    text = f"Tailscale funnel: {'on' if active else 'off'}"
    if active:
        text += f" \u00b7 {ep.public_url}"
    elif ep.tunnel == "tailscale" and ep.public_url:
        text += f"\nSaved setup: {ep.public_url}. Tap Enable to restore it."
    return (
        f"{text}\n\n"
        f"Enable runs tailscale funnel {ep.listen_port} on this host for the endpoint. "
        "Disable stops the funnel and the endpoint."
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
    mode = override or f"global ({c.output_mode})"
    q_override = c.channel_preferred_qualities.get(ch)
    quality_text = q_override or f"global ({c.preferred_quality})"
    hold_override = c.channel_youtube_hold_seconds.get(ch)
    hold_text = f"{hold_override:g}s" if hold_override is not None else f"global ({c.youtube.hold_seconds:g}s)"
    return f"Channel: {ch}\nOutput mode: {mode}\nQuality: {quality_text}\nHold delay: {hold_text}"


async def _text_channel_hold(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    ch = state.channel or ""
    hold_override = c.channel_youtube_hold_seconds.get(ch)
    eff = c.youtube.hold_seconds if hold_override is None else hold_override
    return (
        f"YouTube hold delay for {ch}: {eff:g}s (0 = end immediately)\n"
        f"Global: {c.youtube.hold_seconds:g}s\n\n"
        "When the source stream stops, the broadcast stays open this long, waiting for the "
        "streamer to return \u2014 a return within the delay reuses the same broadcast instead "
        "of creating a new one."
    )


async def _text_channel_quality(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    ch = state.channel or ""
    q_override = c.channel_preferred_qualities.get(ch)
    return (
        f"Recording quality for {ch}: {q_override or f'global ({c.preferred_quality})'}\n\n"
        "Audio only records sound only. It forces output to disk (no YouTube re-stream)."
    )


async def _text_chat(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    kick_chat = c.kick.record_chat
    return (
        f"Chat recording (Twitch): {'on' if c.record_chat else 'off'}\n"
        f"Kick chat recording: {'on' if kick_chat else 'off'}\n\n"
        "Off stops an active capture for that platform."
    )


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


def _retention_label(days: float) -> str:
    """Retention as a short label, for example ``off`` or ``7 days``."""
    if not days:
        return "off"
    return f"{days:g} day" + ("s" if days != 1 else "")


async def _text_retention(ctrl: TelegramController, state: MenuState) -> str:
    return f"Retention: {_retention_label(ctrl._config.retention_days)}. Choose:"


async def _text_maxrec(ctrl: TelegramController, state: MenuState) -> str:
    return f"Max recordings: {ctrl._config.max_concurrent_recordings:g} (0 = unlimited). Choose:"


async def _text_maxyt(ctrl: TelegramController, state: MenuState) -> str:
    return f"Max YouTube re-streams: {ctrl._config.max_concurrent_youtube_streams:g} (0 = unlimited). Choose:"


async def _text_storage(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    d = c.disk
    return (
        f"Retention: {_retention_label(c.retention_days)} (0 = disabled)\n"
        f"Max total size: {d.max_total_gb:g} GB (0 = disabled)\n"
        f"Delete oldest: {'on' if d.delete_oldest else 'off'}\n"
        f"Max recordings: {c.max_concurrent_recordings:g} (0 = unlimited)\n"
        f"Max YouTube re-streams: {c.max_concurrent_youtube_streams:g} (0 = unlimited)"
    )


async def _text_disk(ctrl: TelegramController, state: MenuState) -> str:
    body: str = ctrl.handle_disk([])
    return body + "\n\nChoose what to change:"


async def _text_disk_maxsize(ctrl: TelegramController, state: MenuState) -> str:
    d = ctrl._config.disk
    return (
        f"Max total size: {d.max_total_gb:g} GB (0 = disabled)\n"
        "Limits the total archive size (recordings and chat). When the limit is reached, the "
        "oldest archive files are deleted (or recording stops). Choose:"
    )


async def _text_custom(ctrl: TelegramController, state: MenuState) -> str:
    c = ctrl._config
    d = c.disk
    ch = state.channel or ""
    hold_override = c.channel_youtube_hold_seconds.get(ch)
    eff = c.youtube.hold_seconds if hold_override is None else hold_override
    labels = {
        "retention": (f"Retention: {_retention_label(c.retention_days)} (0 = disabled)", " in days"),
        "maxrec": (f"Max recordings: {c.max_concurrent_recordings:g} (0 = unlimited)", ""),
        "maxyt": (f"Max YouTube re-streams: {c.max_concurrent_youtube_streams:g} (0 = unlimited)", ""),
        "disk_maxsize": (f"Max total size: {d.max_total_gb:g} GB (0 = disabled)", " in GB"),
        "channel_hold": (f"Hold delay for {ch}: {eff:g}s (0 = end immediately)", " in seconds"),
    }
    # A chat can hold a custom menu name that no longer exists after a
    # restart, so this lookup must not raise.
    label, units = labels.get(state.custom or "", ("Value", ""))
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
    "mode": _pair("mode", _text_mode),
    "quality": _pair("quality", _text_quality),
    "retention": _pair("retention", _text_retention),
    "maxrec": _pair("maxrec", _text_maxrec),
    "maxyt": _pair("maxyt", _text_maxyt),
    "disk": _pair("disk", _text_disk),
    "disk_maxsize": _pair("disk_maxsize", _text_disk_maxsize),
    "custom": _pair("custom", _text_custom),
    "storage": _pair("storage", _text_storage),
    "remote_access": _pair("remote_access", _text_remote_access),
    "api": _pair("api", _text_api),
    "kick_webhook": _pair("kick_webhook", _text_kick_webhook),
    "kick_cloudflare": _pair("kick_cloudflare", _text_kick_cloudflare),
    "kick_tailscale": _pair("kick_tailscale", _text_kick_tailscale),
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
    "mode": settings_menus.menu_mode,
    "quality": settings_menus.menu_quality,
    "retention": settings_menus.menu_retention,
    "maxrec": settings_menus.menu_limits,
    "maxyt": settings_menus.menu_limits,
    "disk": settings_menus.menu_disk,
    "disk_maxsize": settings_menus.menu_disk_maxsize,
    "custom": settings_menus.menu_custom,
    "storage": settings_menus.menu_storage,
    "remote_access": kick_menus.menu_remote_access,
    "api": api_menus.menu_api,
    "kick_webhook": kick_menus.menu_kick_webhook,
    "kick_cloudflare": kick_menus.menu_kick_cloudflare,
    "kick_tailscale": kick_menus.menu_kick_tailscale,
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
    "mode": "root",
    "quality": "root",
    "retention": "storage",
    "maxrec": "storage",
    "maxyt": "storage",
    "storage": "root",
    "disk": "storage",
    "disk_maxsize": "disk",
    "remote_access": "root",
    "api": "remote_access",
    "kick_webhook": "remote_access",
    "kick_cloudflare": "remote_access",
    "kick_tailscale": "remote_access",
    "kick_cloudflare_token": "kick_cloudflare",
    "kick_cloudflare_hostname": "kick_cloudflare_token",
    "kick_cloudflare_dns": "kick_cloudflare_hostname",
}


def _custom_parent(custom: str) -> str:
    """Back-button target for the custom value menu."""
    if custom == "channel_hold":
        return "channel"
    if custom in ("retention", "maxrec", "maxyt"):
        return "storage"
    return "disk"


async def menu_back(ctrl: TelegramController, chat_id: ChatId) -> MenuResult:
    """Move one chat up one menu level."""
    state = ctrl._state_for(chat_id)
    if state.menu == "custom":
        parent: str | None = _custom_parent(state.custom or "")
    else:
        parent = PARENT.get(state.menu)
    if parent is None:
        if state.menu != "root":  # unknown menu: fall back to root, like dispatch_text
            state.menu, state.channel = "root", None
            return await ctrl.menu_text("root", chat_id=chat_id), ctrl.reply_keyboard("root")
        return None  # no Back button on root
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
    text = _unmark(text)
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
