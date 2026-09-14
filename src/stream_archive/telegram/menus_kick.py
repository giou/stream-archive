"""Reply-keyboard menus for Remote Access: the tunnels and the Kick webhook.

The Remote Access menu owns the master toggle and the tunnel picks. Each
tunnel menu owns its own On/Off. Each ``menu_*`` function routes one press
or typed value for one chat. State reads and writes go through that chat's
``MenuState`` only.
"""

import re
from typing import TYPE_CHECKING

from stream_archive.telegram.commands_webhook import public_url_note
from stream_archive.telegram.menu_state import ChatId, MenuResult
from stream_archive.tunnels import parse_public_hostname

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def menu_remote_access(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Remote Access menu: the endpoint toggle, a tunnel, or a feature."""
    state = ctrl._state_for(chat_id)
    if text == "On":
        return await ctrl._enable_endpoint(chat_id=chat_id), ctrl.reply_keyboard("remote_access")
    if text == "Off":
        return await ctrl._disable_endpoint(chat_id=chat_id), ctrl.reply_keyboard("remote_access")
    new_menu = {
        "Cloudflare tunnel": "kick_cloudflare",
        "Tailscale funnel": "kick_tailscale",
        "Kick webhook": "kick_webhook",
        "API": "api",
    }.get(text)
    if new_menu is None:
        return None
    state.menu = new_menu
    return await ctrl.menu_text(new_menu, chat_id=chat_id), ctrl.reply_keyboard(new_menu)


async def menu_kick_webhook(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Kick webhook toggle. The endpoint lives in Remote Access."""
    if text == "On":
        return await ctrl._set_webhook_enabled(True, chat_id=chat_id), ctrl.reply_keyboard("kick_webhook")
    if text == "Off":
        return await ctrl._set_webhook_enabled(False, chat_id=chat_id), ctrl.reply_keyboard("kick_webhook")
    return None


async def menu_kick_cloudflare(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Cloudflare tunnel pick: the toggle, own URL, quick, or named."""
    state = ctrl._state_for(chat_id)
    if re.match(r"^https?://", text):  # own tunnel already running
        return await ctrl._apply_cloudflare_url(text, chat_id=chat_id)
    if text == "On":
        return (
            await ctrl._enable_endpoint(chat_id=chat_id, tunnel="cloudflare"),
            ctrl.reply_keyboard("kick_cloudflare"),
        )
    if text == "Off":
        return (
            await ctrl._disable_endpoint(chat_id=chat_id, tunnel="cloudflare"),
            ctrl.reply_keyboard("kick_cloudflare"),
        )
    if text == "Quick tunnel":
        url, hint = await ctrl._cloudflared_quick_start()
        if url is None:
            return f"\u274c {hint}", ctrl.reply_keyboard("kick_cloudflare")
        result = await ctrl._apply_endpoint_state(True, url, "cloudflare", cloudflare_managed=True, chat_id=chat_id)
        state.menu = "kick_cloudflare"
        note = await ctrl._reachability_note(url, "cloudflare")
        return (
            f"{result}\n\ncloudflared quick tunnel is running on this host.\n{public_url_note(ctrl._config)}{note}",
            ctrl.reply_keyboard("kick_cloudflare"),
        )
    if text == "Named tunnel":
        state.menu = "kick_cloudflare_token"
        return await ctrl.menu_text("kick_cloudflare_token", chat_id=chat_id), ctrl.reply_keyboard(
            "kick_cloudflare_token"
        )
    return None


async def menu_kick_tailscale(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Tailscale funnel toggle."""
    state = ctrl._state_for(chat_id)
    if text == "On":
        ok, message = await ctrl._tailscale_enable(chat_id=chat_id)
        if not ok:
            state.menu = "kick_cloudflare"
            return message, ctrl.reply_keyboard("kick_cloudflare")
        state.menu = "kick_tailscale"
        return message, ctrl.reply_keyboard("kick_tailscale")
    if text == "Off":
        return (
            await ctrl._disable_endpoint(chat_id=chat_id, tunnel="tailscale"),
            ctrl.reply_keyboard("kick_tailscale"),
        )
    return None


async def menu_kick_token(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Take any text as a tunnel-token candidate."""
    state = ctrl._state_for(chat_id)
    ok, message = await ctrl._handle_cloudflare_token(text, chat_id=chat_id)
    if not ok:
        return message, ctrl.reply_keyboard("kick_cloudflare_token")
    state.menu = "kick_cloudflare_hostname"
    return message, ctrl.reply_keyboard("kick_cloudflare_hostname")


async def menu_kick_hostname(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Take any text as a hostname candidate."""
    state = ctrl._state_for(chat_id)
    host = parse_public_hostname(text)
    if host is None:
        return (
            "\u274c That doesn't look like a public hostname (e.g. kick.example.com).",
            ctrl.reply_keyboard("kick_cloudflare_hostname"),
        )
    state.cloudflare_hostname = host
    state.menu = "kick_cloudflare_dns"
    return (
        f"Hostname {host} \u2014 " + await ctrl.menu_text("kick_cloudflare_dns", chat_id=chat_id),
        ctrl.reply_keyboard("kick_cloudflare_dns"),
    )


async def menu_kick_dns(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Take an API token or 'skip' for the DNS step."""
    if text.strip().lower() == "skip":
        return await ctrl._finish_named_setup(None, chat_id=chat_id)
    ok, message = await ctrl._create_cloudflare_dns(text.strip(), chat_id=chat_id)
    if not ok:
        return message, ctrl.reply_keyboard("kick_cloudflare_dns")
    return await ctrl._finish_named_setup(message, chat_id=chat_id)
