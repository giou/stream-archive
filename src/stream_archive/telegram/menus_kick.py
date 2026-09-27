"""Reply-keyboard menus for Remote access: the endpoint and the Kick webhook.

The Remote access menu owns the endpoint toggle and the public URL entry.
The Kick webhook menu owns its toggle and its own public URL entry. Both
URLs point at proxies the user runs themselves. Each ``menu_*`` function
routes one press or typed value for one chat.
"""

import re
from typing import TYPE_CHECKING

from stream_archive.telegram.menu_state import ChatId, MenuResult, open_menu

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController


async def menu_remote_access(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Remote access menu: the endpoint toggle, a pasted URL, or a feature."""
    if text == "Enable endpoint":
        return await ctrl._enable_endpoint(chat_id=chat_id), ctrl.reply_keyboard("remote_access", chat_id=chat_id)
    if text == "Disable endpoint":
        return await ctrl._disable_endpoint(chat_id=chat_id), ctrl.reply_keyboard("remote_access", chat_id=chat_id)
    if re.match(r"(?i)https?://", text.strip()):  # own proxy already running
        # The enable prompt asks for a pasted URL, so take it here too.
        return await ctrl._apply_endpoint_url(text, chat_id=chat_id)
    new_menu = {
        "Kick webhook": "kick_webhook",
        "API": "api",
        "Web panel": "web",
    }.get(text)
    if new_menu is None:
        return None
    return await open_menu(ctrl, new_menu, chat_id)


async def menu_kick_webhook(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route the Kick webhook toggle and its own URL entry."""
    if text == "Enable Kick webhook":
        return await ctrl._set_webhook_enabled(True, chat_id=chat_id), ctrl.reply_keyboard(
            "kick_webhook", chat_id=chat_id
        )
    if text == "Disable Kick webhook":
        return await ctrl._set_webhook_enabled(False, chat_id=chat_id), ctrl.reply_keyboard(
            "kick_webhook", chat_id=chat_id
        )
    if text == "Set Kick URL":
        return await open_menu(ctrl, "kick_webhook_url", chat_id)
    if text == "Test delivery":
        return await ctrl._test_kick_delivery(chat_id), ctrl.reply_keyboard("kick_webhook", chat_id=chat_id)
    return None


async def menu_kick_webhook_url(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Take any text as the Kick entry URL of the user's own proxy."""
    if re.match(r"(?i)https?://", text.strip()):
        return await ctrl._set_kick_url(text, chat_id=chat_id)
    return (
        "\u274c That doesn't look like a public URL (e.g. https://kick.example.com).",
        ctrl.reply_keyboard("kick_webhook_url", chat_id=chat_id),
    )
