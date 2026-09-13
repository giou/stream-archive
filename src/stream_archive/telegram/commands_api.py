"""Control API commands: enable, disable, show the key, and rotate the key.

The API is set up from the Remote Access menu only. It runs on the Kick
webhook listener, so every enable or disable reconciles that listener
through ``KickWebhook.apply_state``. The key lives in ``api.key`` in
config.json and is generated on the first enable.
"""

import secrets
from typing import Any

from stream_archive.config import AppConfig, api_base_url


class ApiCommands:
    _config: AppConfig
    _apply: Any
    _kick_webhook: Any
    _send_admin: Any

    def _api_state_text(self) -> str:
        """One-line state of the control API."""
        return "on" if self._config.api.enabled else "off"

    async def _notify_api_changes(self, lines: list[str]) -> None:
        """Tell the admin what the control API changed. Never raises.

        The API replies to its client and sends this message in parallel,
        so the admin always sees API-driven changes beside bot-driven ones.
        """
        if not lines:
            return
        body = "\n".join(f"\u2022 {line}" for line in lines)
        await self._send_admin(f"\U0001f310 Control API\n{body}")

    def _api_key_text(self) -> str:
        """The current API key, or a hint when the API was never enabled."""
        key = self._config.api.key
        if not key:
            return "No API key yet \u2014 enable the API to generate one."
        return (
            f"API key:\n{key}\n\n"
            "Send it as an Authorization header (Bearer <key>) or an X-API-Key header.\n"
            "Keep it secret: anyone who has it can change your channels and settings."
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
        if result.startswith("\u274c"):
            return result
        if self._kick_webhook is not None:
            await self._kick_webhook.apply_state()
        lines = [result]
        base = api_base_url(self._config)
        if base:
            lines.append(f"Base URL: {base}")
        else:
            lines.append("No public URL yet \u2014 set up a tunnel under Kick webhook to reach the API from outside.")
        if created:
            lines.append(f"API key (keep it secret \u2014 Show key shows it again):\n{key}")
        return "\n\n".join(lines)

    async def _rotate_api_key(self, chat_id: int | None = None) -> str:
        """Replace the API key. The old key stops working at once."""
        key = secrets.token_urlsafe(32)
        existed = bool(self._config.api.key)

        def mutate(candidate: AppConfig) -> None:
            candidate.api.key = key

        summary = "API key rotated \u2014 the old key stopped working" if existed else "API key generated"
        result: str = self._apply(mutate, lambda c: summary, chat_id)
        if result.startswith("\u274c"):
            return result
        return f"{result}\n\nNew API key (keep it secret):\n{key}"
