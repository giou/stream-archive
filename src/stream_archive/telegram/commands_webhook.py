"""Endpoint and Kick webhook management over Telegram.

Both surfaces use public URLs that the user publishes themselves: a
reverse proxy or tailnet serve in front of the loopback listeners. The
app never runs a tunnel. These commands save URLs, probe them, and
reconcile the listeners.
"""

import logging
from typing import TYPE_CHECKING, Any

import httpx

from stream_archive.config import AppConfig, endpoint_base_url, normalize_endpoint_url, webhook_public_url
from stream_archive.telegram.menu_state import ChatId, MenuState, is_error

if TYPE_CHECKING:
    from stream_archive.kick_webhook import KickWebhook

logger = logging.getLogger(__name__)

_KICK_DASHBOARD_HINT = (
    "Paste this URL into the Kick app under Settings \u2192 Developer \u2192 your app \u2192 Enable webhooks."
)


def public_url_note(config: AppConfig) -> str:
    """Endpoint and Kick webhook URL lines for the setup replies."""
    base = endpoint_base_url(config)
    return f"Endpoint: {base}/\nKick app webhook URL: {webhook_public_url(config)}\n" + _KICK_DASHBOARD_HINT


class WebhookCommands:
    def _state_for(self, chat_id: ChatId) -> MenuState:
        """Per-chat menu state. ChatStateMixin provides this on the controller."""
        raise NotImplementedError

    _config: AppConfig
    _apply: Any
    _kick_webhook: KickWebhook | None
    _http: httpx.AsyncClient | None
    _send_admin: Any
    _admin_id: int
    reply_keyboard: Any

    def _webhook_state_text(self) -> str:
        """One-line state of the Kick webhook feature."""
        return "on" if self._config.kick.webhook.enabled else "off"

    def _endpoint_state_text(self) -> str:
        """One-line state of the endpoint: base URL or off."""
        if not self._config.endpoint.enabled:
            return "off"
        return f"on ({endpoint_base_url(self._config)}/)"

    async def _probe_webhook_url(self, url: str) -> bool:
        """True when the public URL answers an HTTP request (proxy and DNS work).

        Any response counts, including a 4xx from the receiver. The point
        is that the request reached the app through the user's proxy.
        """
        client = self._http
        if client is None:
            return False
        try:
            await client.get(url)
        except httpx.HTTPError:
            return False
        return True

    async def _reachability_note(self, url: str) -> str:
        """Probe the public URL and return a user-facing status line."""
        if await self._probe_webhook_url(url):
            return "\n\n\u2705 URL is reachable - save it in Kick and I'll confirm when the first event arrives."
        return (
            "\n\n\u26a0\ufe0f The URL doesn't respond yet - start your reverse proxy "
            "and check its logs, then tap Enable again."
        )

    async def _disable_endpoint(self, chat_id: int | None = None) -> str:
        """Turn the endpoint off. The setup stays saved."""
        if not self._config.endpoint.enabled:
            return "Endpoint is already off."
        result: str = await self._apply_endpoint_state(False, chat_id=chat_id)
        if is_error(result):
            return result
        saved = self._config.endpoint
        return f"{result}\n\nYour setup is saved ({saved.public_url}). Tap Enable to restore it."

    async def _set_webhook_enabled(self, enabled: bool, chat_id: int | None = None) -> str:
        """Turn the Kick webhook feature on or off. The endpoint is untouched."""
        if self._config.kick.webhook.enabled == enabled:
            return f"Kick webhook is already {'on' if enabled else 'off'}."

        def mutate(candidate: AppConfig) -> None:
            candidate.kick.webhook.enabled = enabled
            if enabled:
                # Re-arm the delivery confirmation for the new enable.
                candidate.kick.webhook.setup_notified = False

        result: str = self._apply(mutate, lambda c: f"Kick webhook {'enabled' if enabled else 'disabled'}", chat_id)
        if is_error(result):
            return result
        if self._kick_webhook is not None:
            # The listener owns the sync loop. This call starts the loop and
            # its subscriptions, or stops both when the feature goes off.
            # apply_state raises when the listener cannot bind, and the call
            # sites expect an error reply rather than an exception.
            try:
                await self._kick_webhook.apply_state()
                if enabled and self._config.endpoint.enabled:
                    await self._kick_webhook.sync_channels(self._config.channels)
            except Exception as e:
                logger.exception("[telegram] webhook listener reconcile failed")
                return f"\u274c The listener could not be reconfigured: {e}"
        if enabled and not self._config.endpoint.enabled:
            return f"{result}\n\nThe endpoint is off, so Kick cannot deliver events yet."
        if enabled and not self._config.kick.webhook.public_url.strip():
            note = await self._reachability_note(endpoint_base_url(self._config))
            return (
                f"{result}\n\nKick follows the panel address. That address must reach "
                f"the public internet - a tailnet address never gets events.{note}"
            )
        return result

    async def _enable_endpoint(self, chat_id: int | None = None) -> str:
        """Turn the endpoint on again with the saved public URL."""
        ep = self._config.endpoint
        if ep.enabled:
            return "Endpoint is already on."
        if not ep.public_url:
            return "No saved public URL yet. Send me your public URL (your reverse proxy address)."
        result: str = await self._apply_endpoint_state(True, ep.public_url, chat_id=chat_id)
        if is_error(result):
            return result
        note = await self._reachability_note(ep.public_url)
        return f"{result}\n\n{public_url_note(self._config)}{note}"

    async def _apply_endpoint_url(self, text: str, chat_id: int | None = None) -> tuple[str, Any]:
        """Enable the endpoint with a pasted public URL of the user's own proxy.

        The app never manages this proxy and never touches it on boot.
        """
        url = normalize_endpoint_url(text)
        result: str = await self._apply_endpoint_state(True, url, chat_id=chat_id)
        if is_error(result):
            return result, self.reply_keyboard("remote_access", chat_id=chat_id)
        note = await self._reachability_note(url)
        return (
            f"{result}\n\n{public_url_note(self._config)}{note}",
            self.reply_keyboard("remote_access", chat_id=chat_id),
        )

    async def _set_kick_url(self, text: str, chat_id: int | None = None) -> tuple[str, Any]:
        """Save the Kick entry as the user's own public URL. The toggle stays separate."""
        url = normalize_endpoint_url(text)

        def mutate(candidate: AppConfig) -> None:
            candidate.kick.webhook.public_url = url
            candidate.kick.webhook.setup_notified = False

        result: str = self._apply(mutate, lambda c: f"Kick URL saved: {url}", chat_id)
        if is_error(result):
            return result, self.reply_keyboard("kick_webhook", chat_id=chat_id)
        note = await self._reachability_note(url)
        return (
            f"{result}\n\n{_KICK_DASHBOARD_HINT}{note}\n\nTap Enable Kick webhook.",
            self.reply_keyboard("kick_webhook", chat_id=chat_id),
        )

    async def _test_kick_delivery(self, chat_id: int | None = None) -> str:
        """Run the delivery test and report its timed result.

        The wait takes up to 3 minutes on a cold setup: the reply arrives
        when the first delivery lands or the timeout hits.
        """
        del chat_id
        if self._kick_webhook is None:
            return "Webhook listener unavailable - restart the app."
        ok, message = await self._kick_webhook.verify_delivery()
        mark = "\u2705" if ok else "\u274c"
        return f"{mark} {message}"

    def _restore_endpoint_state(self, before: dict[str, Any], notified: bool, chat_id: int | None) -> None:
        """Put a saved endpoint state back after a failed reconcile.

        The listener did not take the change. The state before the change
        is the true state, so save it again.
        """

        def mutate(candidate: AppConfig) -> None:
            target = candidate.endpoint
            # Turn the flag off first: the model rejects a public URL that
            # goes away while enabled is true. Set it back last, when the
            # URL it needs is in place.
            target.enabled = False
            for key, value in before.items():
                if key != "enabled":
                    setattr(target, key, value)
            target.enabled = before["enabled"]
            candidate.kick.webhook.setup_notified = notified

        result: str = self._apply(mutate, lambda c: "endpoint state restored", chat_id)
        if is_error(result):
            logger.error("[telegram] could not restore the saved endpoint state: %s", result)

    async def _apply_endpoint_state(
        self,
        enabled: bool,
        url: str = "",
        chat_id: int | None = None,
    ) -> str:
        """Persist endpoint.{enabled,public_url} and reconcile live state.

        Disabling keeps the saved URL, so On restores the same setup
        without new input. A failed listener reconcile restores the saved
        state, so config claims only what the listener serves.
        """
        # Snapshot for the rollback: a failed reconcile must leave the saved
        # state as it was. Keep it as the new state, and config would claim
        # an endpoint that nothing serves.
        before = self._config.endpoint.model_dump()
        notified_before = self._config.kick.webhook.setup_notified

        def mutate(candidate: AppConfig) -> None:
            ce = candidate.endpoint
            if enabled:
                # Set public_url first: the model requires an http(s)
                # URL the moment enabled flips to True.
                ce.public_url = url
                # The delivery confirmation belongs to the Kick URL, so a new
                # enable (or a new URL) proves delivery again. A separate
                # Kick entry keeps its own URL through endpoint changes.
                if not candidate.kick.webhook.public_url.strip():
                    candidate.kick.webhook.setup_notified = False
            ce.enabled = enabled

        result: str = self._apply(
            mutate,
            lambda c: f"Endpoint {'enabled' if enabled else 'disabled'}",
            chat_id,
        )
        if is_error(result):
            return result
        if self._kick_webhook is not None:
            # The listener also serves the control API, so this reconciles
            # both features instead of a plain start or stop. apply_state
            # raises when the listener cannot bind: put the state back and
            # report the failure, so config claims only what the listener
            # serves.
            try:
                await self._kick_webhook.apply_state()
                if enabled:
                    await self._kick_webhook.sync_channels(self._config.channels)
            except Exception as e:
                logger.exception("[telegram] endpoint reconcile failed")
                self._restore_endpoint_state(before, notified_before, chat_id)
                return f"\u274c The listener could not be reconfigured: {e}"
        return result
