"""Web panel commands: enable, disable, and the password.

The panel is set up from the Remote access menu only. It runs on the
shared listener, so every enable or disable reconciles that listener
through ``KickWebhook.apply_state``. The password hash lives in
``web.password_hash`` in config.json, plus a generated session secret.
A first enable generates a password and shows it once, like the API key flow.
"""

import html
import logging
import secrets
from typing import Any

from stream_archive.config import AppConfig, endpoint_base_url
from stream_archive.telegram.menu_state import is_error

logger = logging.getLogger(__name__)

#: Reply for a chat that is not the admin's private chat. The password is
#: a secret: anyone in the chat can read it and then control the app.
_PASSWORD_ELSEWHERE = "\U0001f512 I show the panel password only in our private chat - open the bot there."


def _html(text: str) -> str:
    """Escape plain text for HTML, the parse mode of the password replies."""
    return html.escape(text, quote=False)


def _code_span(password: str) -> str:
    """The password as an HTML code span. One tap on it copies the password."""
    return f"<code>{html.escape(password, quote=False)}</code>"


class WebCommands:
    _config: AppConfig
    _apply: Any
    _kick_webhook: Any
    _send_admin: Any
    _admin_id: int

    def _web_state_text(self) -> str:
        """One-line state of the web panel."""
        return "on" if self._config.web.enabled else "off"

    def _web_panel_url(self) -> str:
        """Public URL of the panel, or "" when no public URL is set."""
        base = endpoint_base_url(self._config)
        return f"{base}/" if base else ""

    def _password_reveal(self, label: str, password: str, chat_id: int | None) -> str:
        """The password under ``label`` for the admin's private chat, or a pointer.

        The reply is HTML and the password is a code span, so one tap on
        it copies the password. A group chat keeps the secret out of the
        message: the whole group would otherwise read it.
        """
        if chat_id is not None and chat_id != self._admin_id:
            return _html(_PASSWORD_ELSEWHERE)
        return f"{_html(label)}\n{_code_span(password)}"

    async def _set_web_enabled(self, enabled: bool, chat_id: int | None = None) -> str:
        """Enable or disable the web panel and persist the change.

        A first enable generates the password and shows it once.
        Disabling keeps the hash, so a later enable works without handing
        out a new one. Generation needs a private chat: a group enable
        turns the panel on but leaves the secret ungenerated, or the
        group would burn the one-time password unseen.
        """
        import asyncio

        from stream_archive.webui import hash_password

        is_private = chat_id is None or chat_id == self._admin_id
        created = enabled and not self._config.web.password_hash and is_private
        password = secrets.token_urlsafe(18) if created else ""
        if created:
            loop = asyncio.get_running_loop()
            hashed = await loop.run_in_executor(None, hash_password, password)
        else:
            hashed = self._config.web.password_hash

        def mutate(candidate: AppConfig) -> None:
            candidate.web.enabled = enabled
            candidate.web.password_hash = hashed
            if not candidate.web.session_secret.strip():
                candidate.web.session_secret = secrets.token_urlsafe(32)

        result: str = self._apply(mutate, lambda c: f"Web panel {'enabled' if enabled else 'disabled'}", chat_id)
        if is_error(result):
            return _html(result)
        lines = [_html(result)]
        if self._kick_webhook is not None:
            try:
                await self._kick_webhook.apply_state()
            except Exception:
                logger.warning("[telegram] Failed to reconcile the webhook listener", exc_info=True)
                lines.append(_html("\u26a0\ufe0f The listener could not be reconfigured - check the logs."))
        if enabled:
            url = self._web_panel_url()
            if url:
                lines.append(_html(f"Panel URL: {url}"))
                if not self._config.endpoint.enabled:
                    lines.append(
                        _html("\u26a0\ufe0f The endpoint is off, so this URL is not reachable from outside yet.")
                    )
            else:
                lines.append(
                    _html(
                        "No public URL yet - set up a tunnel under Settings, then Remote access to reach the panel from outside."
                    )
                )
        if created:
            lines.append(
                self._password_reveal("Panel password (keep it secret - New password replaces it):", password, chat_id)
            )
        elif enabled and not self._config.web.password_hash:
            lines.append(_html("No password yet - open our private chat and tap Enable Web panel to generate one."))
        return "\n\n".join(lines)

    async def _new_web_password(self, chat_id: int | None = None) -> str:
        """Replace the panel password. Old sessions end at once.

        A group chat gets the pointer without rotating: generating here
        would burn the one-time secret unseen and lock the admin out.
        """
        if chat_id is not None and chat_id != self._admin_id:
            return _html(_PASSWORD_ELSEWHERE)
        import asyncio

        from stream_archive.webui import hash_password

        password = secrets.token_urlsafe(18)
        loop = asyncio.get_running_loop()
        hashed = await loop.run_in_executor(None, hash_password, password)

        def mutate(candidate: AppConfig) -> None:
            candidate.web.password_hash = hashed
            if not candidate.web.session_secret.strip():
                candidate.web.session_secret = secrets.token_urlsafe(32)

        summary = "Panel password replaced - all browser sessions ended"
        result: str = self._apply(mutate, lambda c: summary, chat_id)
        if is_error(result):
            return _html(result)
        return f"{_html(result)}\n\n" + self._password_reveal("New panel password (keep it secret):", password, chat_id)
