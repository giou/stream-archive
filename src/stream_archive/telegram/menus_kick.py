"""Reply-keyboard menus for the Kick webhook tunnel setup.

Each ``menu_*`` function routes one press or typed value for one chat. State
reads and writes go through that chat's ``MenuState`` only.
"""

import re
from typing import TYPE_CHECKING

from stream_archive.telegram.commands_webhook import _KICK_DASHBOARD_HINT, _parse_public_hostname
from stream_archive.telegram.menu_state import ChatId, MenuResult

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def menu_kick_webhook(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the tunnel pick: off, Cloudflare, or Tailscale."""
    state = ctrl._state_for(chat_id)
    port = ctrl._config.kick.webhook.listen_port
    if text == "Off":
        result = await ctrl._apply_webhook_state(False, "", chat_id=chat_id)
        state.menu = "root"
        return result, ctrl.reply_keyboard("root")
    if text == "Cloudflare tunnel":
        state.menu = "kick_cloudflare"
        return await ctrl.menu_text("kick_cloudflare", chat_id=chat_id), ctrl.reply_keyboard("kick_cloudflare")
    if text == "Tailscale funnel":
        old_tunnel = ctrl._config.kick.webhook.tunnel
        url, hint = await ctrl._tailscale_webhook_url()
        if url is not None:
            result = await ctrl._apply_webhook_state(True, url, "tailscale", chat_id=chat_id)
            state.menu = "kick_webhook"
            note = await ctrl._reachability_note(url, "tailscale")
            msg = (
                f"{result}\n\n```\n{url}\n```\n"
                f"tailscale funnel {port} is enabled on this host.\n" + _KICK_DASHBOARD_HINT + note
            )
            if old_tunnel == "cloudflare":
                msg += "\nYour cloudflared tunnel has been stopped."
            return msg, ctrl.reply_keyboard("kick_webhook")
        state.menu = "kick_cloudflare"
        return (
            f"{hint}\n\nFix tailscale and tap Tailscale funnel again, or use Cloudflare tunnel instead.",
            ctrl.reply_keyboard("kick_cloudflare"),
        )
    return None


async def menu_kick_cloudflare(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Cloudflare tunnel pick: own URL, quick, or named."""
    state = ctrl._state_for(chat_id)
    if re.match(r"^https?://", text):  # own tunnel already running
        return await ctrl._apply_cloudflare_url(text, chat_id=chat_id)
    if text == "Quick tunnel":
        url, hint = await ctrl._cloudflared_quick_start()
        if url is None:
            return f"\u274c {hint}", ctrl.reply_keyboard("kick_cloudflare")
        result = await ctrl._apply_webhook_state(True, url, "cloudflare", cloudflare_managed=True, chat_id=chat_id)
        state.menu = "kick_webhook"
        note = await ctrl._reachability_note(url, "cloudflare")
        return (
            f"{result}\n\n```\n{url}\n```\n"
            "cloudflared quick tunnel is running on this host.\n" + _KICK_DASHBOARD_HINT + note,
            ctrl.reply_keyboard("kick_webhook"),
        )
    if text == "Named tunnel":
        state.menu = "kick_cloudflare_token"
        return await ctrl.menu_text("kick_cloudflare_token", chat_id=chat_id), ctrl.reply_keyboard(
            "kick_cloudflare_token"
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
    host = _parse_public_hostname(text)
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
