"""Control API commands: enable, disable, show the key, and rotate the key.

The API is set up from the Remote access menu only. It runs on the Kick
webhook listener, so every enable or disable reconciles that listener
through ``KickWebhook.apply_state``. The key lives in ``api.key`` in
config.json and is generated on the first enable.
"""

import html
import logging
import secrets
from typing import Any

from stream_archive.config import AppConfig, api_base_url
from stream_archive.telegram.menu_state import is_error

logger = logging.getLogger(__name__)

#: Reply for a chat that is not the admin's private chat. The key is a secret:
#: anyone in the chat can read it and then control the app.
_KEY_ELSEWHERE = "\U0001f512 I show the API key only in our private chat \u2014 open the bot there and press Show key."


def _html(text: str) -> str:
    """Escape plain text for HTML, the parse mode of the key replies."""
    return html.escape(text, quote=False)


def _code_span(key: str) -> str:
    """The key as an HTML code span. One tap on it copies the key."""
    return f"<code>{html.escape(key, quote=False)}</code>"


class ApiCommands:
    _config: AppConfig
    _apply: Any
    _kick_webhook: Any
    _send_admin: Any
    _admin_id: int

    def _api_state_text(self) -> str:
        """One-line state of the control API."""
        return "on" if self._config.api.enabled else "off"

    async def notify_api_changes(self, lines: list[str]) -> None:
        """Tell the admin what the control API changed. Never raises.

        The API replies to its client and sends this message in parallel,
        so the admin always sees API-driven changes beside bot-driven ones.
        """
        if not lines:
            return
        body = "\n".join(f"\u2022 {line}" for line in lines)
        try:
            await self._send_admin(f"\U0001f310 Control API\n{body}")
        except Exception:
            logger.warning("[telegram] Failed to notify the admin about API changes", exc_info=True)

    def _key_reveal(self, label: str, key: str, chat_id: int | None) -> str:
        """The key under ``label`` for the admin's private chat, or a pointer.

        The reply is HTML and the key is a code span, so one tap on it
        copies the key. A group chat keeps the secret out of the message: the
        whole group would otherwise read it.
        """
        if chat_id is not None and chat_id != self._admin_id:
            return _html(_KEY_ELSEWHERE)
        return f"{_html(label)}\n{_code_span(key)}"

    def _api_key_text(self, chat_id: int | None = None) -> str:
        """The key as HTML text, or a hint when the API was never enabled.

        The key controls the app, so a chat other than the admin's private
        chat gets a pointer instead of the key.
        """
        if chat_id is not None and chat_id != self._admin_id:
            return _html(_KEY_ELSEWHERE)
        key = self._config.api.key
        if not key:
            return _html("No API key yet \u2014 enable the API to generate one.")
        return (
            f"{_html('API key:')}\n{_code_span(key)}\n\n"
            f"{_html('Send it as an Authorization header (Bearer <key>) or an X-API-Key header.')}\n"
            f"{_html('Keep it secret: anyone who has it can change your channels and settings.')}"
        )

    async def _set_api_enabled(self, enabled: bool, chat_id: int | None = None) -> str:
        """Enable or disable the control API and persist the change.

        A first enable generates the key and shows it. Disabling keeps the
        key, so a later enable works without handing out a new one.
        """
        created = enabled and not self._config.api.key
        key = secrets.token_urlsafe(32) if created else self._config.api.key

        def mutate(candidate: AppConfig) -> None:
            candidate.api.enabled = enabled
            candidate.api.key = key

        result: str = self._apply(mutate, lambda c: f"Control API {'enabled' if enabled else 'disabled'}", chat_id)
        if is_error(result):
            return _html(result)
        lines = [_html(result)]
        if self._kick_webhook is not None:
            try:
                await self._kick_webhook.apply_state()
            except Exception:
                logger.warning("[telegram] Failed to reconcile the webhook listener", exc_info=True)
                lines.append(_html("\u26a0\ufe0f The listener could not be reconfigured \u2014 check the logs."))
        if enabled:
            # The base URL guides the setup of an enabled API. It is
            # noise on the disable path, where no reachability matters.
            base = api_base_url(self._config)
            if base:
                lines.append(_html(f"Base URL: {base}"))
                if not self._config.endpoint.enabled:
                    lines.append(
                        _html("\u26a0\ufe0f The endpoint is off, so this URL is not reachable from outside yet.")
                    )
            else:
                lines.append(
                    _html(
                        "No public URL yet \u2014 set up a tunnel under Settings, then Remote access to reach the API from outside."
                    )
                )
        if created:
            lines.append(self._key_reveal("API key (keep it secret \u2014 Show key shows it again):", key, chat_id))
        return "\n\n".join(lines)

    async def _rotate_api_key(self, chat_id: int | None = None) -> str:
        """Replace the API key. The old key stops working at once."""
        key = secrets.token_urlsafe(32)
        existed = bool(self._config.api.key)

        def mutate(candidate: AppConfig) -> None:
            candidate.api.key = key

        summary = "API key rotated \u2014 the old key stopped working" if existed else "API key generated"
        result: str = self._apply(mutate, lambda c: summary, chat_id)
        if is_error(result):
            return _html(result)
        return f"{_html(result)}\n\n" + self._key_reveal("New API key (keep it secret):", key, chat_id)
