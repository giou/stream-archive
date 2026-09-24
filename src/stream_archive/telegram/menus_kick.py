"""Reply-keyboard menus for Remote access: the tunnels and the Kick webhook.

The Remote access menu owns the master toggle and the tunnel picks. Each
tunnel menu owns its own toggle. Each ``menu_*`` function routes one press
or typed value for one chat. State reads and writes go through that chat's
``MenuState`` only.
"""

import re
from typing import TYPE_CHECKING

from stream_archive.telegram.commands_webhook import public_url_note
from stream_archive.telegram.menu_state import ChatId, MenuResult, is_error, open_menu
from stream_archive.tunnels import parse_public_hostname

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def menu_remote_access(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Remote access menu: the endpoint toggle, a tunnel, or a feature."""
    if text == "Enable endpoint":
        return await ctrl._enable_endpoint(chat_id=chat_id), ctrl.reply_keyboard("remote_access", chat_id=chat_id)
    if text == "Disable endpoint":
        return await ctrl._disable_endpoint(chat_id=chat_id), ctrl.reply_keyboard("remote_access", chat_id=chat_id)
    if re.match(r"(?i)https?://", text.strip()):  # own tunnel already running
        # The enable prompt asks for a pasted URL, so take it here too.
        return await ctrl._apply_cloudflare_url(text, chat_id=chat_id)
    new_menu = {
        "Cloudflare tunnel": "kick_cloudflare",
        "Tailscale funnel": "kick_tailscale",
        "Kick webhook": "kick_webhook",
        "API": "api",
        "Web panel": "web",
    }.get(text)
    if new_menu is None:
        return None
    return await open_menu(ctrl, new_menu, chat_id)


async def menu_kick_webhook(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Kick webhook toggle. The endpoint lives in Remote access."""
    if text == "Enable Kick webhook":
        return await ctrl._set_webhook_enabled(True, chat_id=chat_id), ctrl.reply_keyboard(
            "kick_webhook", chat_id=chat_id
        )
    if text == "Disable Kick webhook":
        return await ctrl._set_webhook_enabled(False, chat_id=chat_id), ctrl.reply_keyboard(
            "kick_webhook", chat_id=chat_id
        )
    return None


async def menu_kick_cloudflare(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Cloudflare tunnel pick: the toggle, own URL, quick, or named."""
    if re.match(r"(?i)https?://", text.strip()):  # own tunnel already running
        return await ctrl._apply_cloudflare_url(text, chat_id=chat_id)
    if text == "Enable Cloudflare tunnel":
        return (
            await ctrl._enable_endpoint(chat_id=chat_id, tunnel="cloudflare"),
            ctrl.reply_keyboard("kick_cloudflare", chat_id=chat_id),
        )
    if text == "Disable Cloudflare tunnel":
        return (
            await ctrl._disable_endpoint(chat_id=chat_id, tunnel="cloudflare"),
            ctrl.reply_keyboard("kick_cloudflare", chat_id=chat_id),
        )
    if text == "Quick tunnel":
        # One cloudflared process serves the whole app, so two presses must
        # not interleave: the loser of a race would stop the winner's live
        # tunnel. The lock also makes the cleanup below own the process.
        async with ctrl._cloudflared_lock:
            url, hint = await ctrl._cloudflared_quick_start()
            if url is None:
                detail = hint or "cloudflared published no tunnel URL - see logs"
                return f"\u274c {detail}", ctrl.reply_keyboard("kick_cloudflare", chat_id=chat_id)
            result = await ctrl._apply_endpoint_state(True, url, "cloudflare", cloudflare_managed=True, chat_id=chat_id)
            if is_error(result):
                # The apply failed, so the endpoint stays off. Stop the process
                # and do not claim that the quick tunnel is running.
                ctrl._cloudflared_stop()
                return result, ctrl.reply_keyboard("kick_cloudflare", chat_id=chat_id)
            ctrl._enter_menu(chat_id, "kick_cloudflare")
        note = await ctrl._reachability_note(url, "cloudflare")
        return (
            f"{result}\n\ncloudflared quick tunnel is running on this host.\n{public_url_note(ctrl._config)}{note}",
            ctrl.reply_keyboard("kick_cloudflare", chat_id=chat_id),
        )
    if text == "Named tunnel":
        return await open_menu(ctrl, "kick_cloudflare_token", chat_id)
    return None


async def menu_kick_tailscale(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Tailscale funnel toggle. The menu stays here when tailscale is missing."""
    if text == "Enable Tailscale funnel":
        # The message points to the Cloudflare tunnel when tailscale is missing.
        _ok, message = await ctrl._tailscale_enable(chat_id=chat_id)
        return message, ctrl.reply_keyboard("kick_tailscale", chat_id=chat_id)
    if text == "Disable Tailscale funnel":
        return (
            await ctrl._disable_endpoint(chat_id=chat_id, tunnel="tailscale"),
            ctrl.reply_keyboard("kick_tailscale", chat_id=chat_id),
        )
    return None


async def menu_kick_token(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Take any text as a tunnel-token candidate."""
    ok, message = await ctrl._handle_cloudflare_token(text, chat_id=chat_id)
    if not ok:
        return message, ctrl.reply_keyboard("kick_cloudflare_token", chat_id=chat_id)
    ctrl._enter_menu(chat_id, "kick_cloudflare_hostname")
    return message, ctrl.reply_keyboard("kick_cloudflare_hostname", chat_id=chat_id)


async def menu_kick_hostname(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Take any text as a hostname candidate."""
    host = parse_public_hostname(text)
    if host is None:
        return (
            "\u274c That doesn't look like a public hostname (e.g. kick.example.com).",
            ctrl.reply_keyboard("kick_cloudflare_hostname", chat_id=chat_id),
        )
    # The DNS step owns the hostname, so it survives the entry to that menu.
    state = ctrl._state_for(chat_id)
    ctrl._enter_menu(chat_id, "kick_cloudflare_dns")
    state.cloudflare_hostname = host
    return (
        f"Hostname {host} - " + await ctrl.menu_text("kick_cloudflare_dns", chat_id=chat_id),
        ctrl.reply_keyboard("kick_cloudflare_dns", chat_id=chat_id),
    )


async def menu_kick_dns(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Take an API token or the Skip DNS button for the DNS step."""
    if text.strip().lower() == "skip dns":
        return await ctrl._finish_named_setup(None, chat_id=chat_id)
    token = text.strip()
    if not token:  # a blank value would only reach the Cloudflare API
        return (
            "Send your Cloudflare API token, or tap Skip DNS to create the record yourself.",
            ctrl.reply_keyboard("kick_cloudflare_dns", chat_id=chat_id),
        )
    ok, message = await ctrl._create_cloudflare_dns(token, chat_id=chat_id)
    if not ok:
        return message, ctrl.reply_keyboard("kick_cloudflare_dns", chat_id=chat_id)
    return await ctrl._finish_named_setup(message, chat_id=chat_id)
