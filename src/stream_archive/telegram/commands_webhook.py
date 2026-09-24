import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from stream_archive.config import AppConfig, endpoint_base_url, normalize_endpoint_url, webhook_public_url
from stream_archive.telegram.menu_state import ChatId, MenuState, is_error
from stream_archive.tunnels import (
    CloudflaredTunnel,
    decode_token,
    parse_public_hostname,
    tailscale_funnel_off,
    tailscale_funnel_url,
    token_from_input,
    valid_token,
    write_ingress_config,
)

if TYPE_CHECKING:
    from stream_archive.kick_webhook import KickWebhook

logger = logging.getLogger(__name__)

_KICK_DASHBOARD_HINT = (
    "Paste this URL into the Kick app under Settings \u2192 Developer \u2192 your app \u2192 Enable webhooks."
)

_CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"

#: User-facing names of the tunnel values in endpoint.tunnel.
_TUNNEL_LABELS = {"cloudflare": "Cloudflare tunnel", "tailscale": "Tailscale funnel"}

#: Reply for a Cloudflare API body that is not the documented JSON object.
_CLOUDFLARE_BAD_BODY = "\u274c Cloudflare API request failed: unexpected response from Cloudflare."

#: Pages the zone lookup reads before it stops. The API lists 50 zones per
#: page, so this covers 1000 zones. A body that claims more pages is broken.
_CLOUDFLARE_MAX_ZONE_PAGES = 20


def _json_body(response: httpx.Response) -> dict[str, Any]:
    """JSON body of a Cloudflare API response. A wrong shape reads as an empty object.

    A proxy can answer with an HTML page instead of JSON, and a valid JSON
    body can still be a list or a null. The DNS flow must never raise, so
    every wrong shape reads as no fields.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def public_url_note(config: AppConfig) -> str:
    """Endpoint and Kick webhook URL lines for the tunnel setup replies."""
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
    _cloudflared: CloudflaredTunnel
    _send_admin: Any
    _admin_id: int
    reply_keyboard: Any

    def _webhook_state_text(self) -> str:
        """One-line state of the Kick webhook feature."""
        return "on" if self._config.kick.webhook.enabled else "off"

    def _endpoint_state_text(self) -> str:
        """One-line state of the public endpoint: tunnel and base URL."""
        ep = self._config.endpoint
        if not ep.enabled:
            return "off"
        base = endpoint_base_url(self._config)
        return f"on ({ep.tunnel} \u00b7 {base}/)" if ep.tunnel else f"on ({base}/)"

    async def _tailscale_webhook_url(self) -> tuple[str | None, str | None]:
        """Enable a tailscale funnel for the listener port and return its public URL.

        Returns (url, None) on success, or (None, hint) with a user-facing
        explanation when tailscale is missing or unusable. Never raises.
        """
        return await tailscale_funnel_url(self._config.endpoint.listen_port)

    async def _cloudflared_quick_start(self) -> tuple[str | None, str | None]:
        """Start a quick tunnel for the listener port and return (url, hint)."""
        url, hint = await self._cloudflared.start_quick(self._config.endpoint.listen_port)
        return (normalize_endpoint_url(url), hint) if url else (None, hint)

    async def _cloudflared_named_start(self, token: str, config_path: Path | None = None) -> tuple[bool, str | None]:
        """Start a named tunnel with ``cloudflared tunnel run --token``."""
        return await self._cloudflared.start_named(token, config_path)

    def _cloudflared_stop(self) -> None:
        """Stop the managed cloudflared process. Idempotent."""
        self._cloudflared.stop()

    async def _start_cloudflare_tunnel(self) -> tuple[str | None, str | None]:
        """Start the saved managed Cloudflare tunnel. Return (url, hint).

        A stored token starts the named tunnel with the local ingress
        config. Without a token the quick tunnel starts, and its temporary
        trycloudflare URL can differ from the stored one.
        """
        ep = self._config.endpoint
        if ep.cloudflare_token:
            host = parse_public_hostname(ep.public_url or "")
            if host is None:
                # The saved URL holds no plain hostname, so no ingress config
                # can point at this listener. Report it, do not raise.
                return None, (
                    "The saved public URL has no usable hostname. "
                    "Send the hostname again, for example kick.example.com."
                )
            cfg = await self._write_cloudflared_config(host)
            ok, hint = await self._cloudflared_named_start(ep.cloudflare_token, config_path=cfg)
            return (normalize_endpoint_url(ep.public_url), None) if ok else (None, hint)
        url, hint = await self._cloudflared_quick_start()
        return (url, hint) if url else (None, hint)

    async def _restore_cloudflared(self) -> None:
        """Restart an app-managed cloudflared after a service restart (endpoint on)."""
        try:
            ep = self._config.endpoint
            if not (ep.enabled and ep.tunnel == "cloudflare" and ep.cloudflare_managed):
                return
            url, hint = await self._start_cloudflare_tunnel()
            if not self._tunnel_active("cloudflare"):
                # A Disable press landed while cloudflared started, so the
                # process would publish the listener port for nothing.
                self._cloudflared_stop()
                return
            if url is None:
                await self._send_admin(f"\u274c cloudflared failed to restart your tunnel:\n{hint}")
            elif url != normalize_endpoint_url(ep.public_url):

                def mutate(candidate: AppConfig) -> None:
                    candidate.endpoint.public_url = url
                    candidate.kick.webhook.setup_notified = False

                result: str = self._apply(mutate, lambda c: "public_url updated")
                if is_error(result):
                    # The running tunnel serves the new URL, but config.json
                    # keeps the old one. Report it instead of the success note.
                    await self._send_admin(
                        f"\u274c The tunnel restarted with a new URL, but I could not save it:\n{result}"
                    )
                    return
                note = await self._reachability_note(url, "cloudflare")
                await self._send_admin(
                    "\U0001f4a1 Your cloudflared quick tunnel restarted with a new temporary URL:\n\n"
                    f"```\n{url}\n```\n"
                    "The previous trycloudflare URL expired. " + public_url_note(self._config) + note
                )
        except Exception:
            logger.exception("[telegram] cloudflared restore failed")

    async def _handle_cloudflare_token(self, text: str, chat_id: int | None = None) -> tuple[bool, str]:
        """Validate a pasted cloudflared token/command and persist it.

        Return (True, message) with the next-step prompt, or (False, error).
        """
        token = token_from_input(text)
        if not valid_token(token):
            return False, (
                "\u274c That doesn't look like a cloudflared tunnel token.\n\n"
                "Send the token from the Cloudflare dashboard command "
                "(cloudflared service install <TOKEN>) - or paste the whole command."
            )
        result: str = self._apply(
            lambda candidate: setattr(candidate.endpoint, "cloudflare_token", token),
            lambda c: "token saved",
            chat_id,
        )
        if is_error(result):
            return False, result
        return True, (
            "\u2705 Tunnel token accepted.\n\n"
            "Send the public hostname to use for the webhook, e.g. kick.example.com - "
            "I'll point your tunnel at this app automatically."
        )

    async def _write_cloudflared_config(self, host: str) -> Path:
        """Write the local ingress config for the named tunnel and return its path."""
        ep = self._config.endpoint
        return write_ingress_config(self._config._workdir, host, ep.listen_port, ep.cloudflare_token)

    async def _cloudflare_zone(
        self, client: httpx.AsyncClient, headers: dict[str, str], host: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Find the longest-suffix zone of ``host`` in the account's zones.

        Read every page of the zone list. Return (zone, None) on a match,
        (None, None) when no zone matches, and (None, error) on a failed
        request or a broken body. Never raises.
        """
        best: dict[str, Any] | None = None
        best_name = ""
        page = 1
        while True:
            try:
                resp = await client.get(f"{_CLOUDFLARE_API}/zones?per_page=50&page={page}", headers=headers)
            except (httpx.HTTPError, ValueError) as e:
                return None, f"\u274c Cloudflare API request failed: {e}"
            if resp.status_code != 200:
                return None, (
                    "\u274c The token can't list zones - it needs Zone read (use the 'Edit zone DNS' template)."
                )
            body = _json_body(resp)
            zones = body.get("result") or []
            if not isinstance(zones, list):
                return None, _CLOUDFLARE_BAD_BODY
            for zone in zones:
                # A zone without a usable name selects nothing and reads as
                # a broken reply, not as a zone that misses.
                name = zone.get("name") if isinstance(zone, dict) else None
                if not isinstance(name, str) or not name:
                    return None, _CLOUDFLARE_BAD_BODY
                lowered = name.lower()
                if (host == lowered or host.endswith("." + lowered)) and len(lowered) > len(best_name):
                    best, best_name = zone, lowered
            info = body.get("result_info")
            total_pages = info.get("total_pages") if isinstance(info, dict) else None
            if not isinstance(total_pages, int) or page >= total_pages or page >= _CLOUDFLARE_MAX_ZONE_PAGES:
                return best, None
            page += 1

    async def _create_cloudflare_dns(self, api_token: str, chat_id: int | None = None) -> tuple[bool, str]:
        """Create the CNAME for the named tunnel's hostname via the Cloudflare API.

        Return (True, message) on success, including when the record already
        points at the tunnel, or (False, error) otherwise. Never raises.
        """
        chat = chat_id if chat_id is not None else self._admin_id
        host = self._state_for(chat).cloudflare_hostname or ""
        ep = self._config.endpoint
        data = decode_token(ep.cloudflare_token)
        tunnel_id = (data or {}).get("t") or ""
        account_id = (data or {}).get("a") or ""
        if not host or not tunnel_id:
            return False, "\u274c Missing hostname or tunnel token - start the Named tunnel flow again."
        headers = {"Authorization": f"Bearer {api_token}"}
        target = f"{tunnel_id}.cfargotunnel.com"
        client = self._http
        if client is None:
            return False, "\u274c HTTP client is not ready - try again in a moment."
        # Account-owned tokens (cfat_ prefix) reject the user-scoped
        # verify endpoint. Fall back to the account-scoped endpoint.
        # _json_body never raises and never returns another shape than a
        # dict, so a proxy page or a null reads as an inactive token.
        try:
            verify = await client.get(f"{_CLOUDFLARE_API}/user/tokens/verify", headers=headers)
            if verify.status_code != 200 and account_id:
                verify = await client.get(f"{_CLOUDFLARE_API}/accounts/{account_id}/tokens/verify", headers=headers)
        except (httpx.HTTPError, ValueError) as e:
            return False, f"\u274c Cloudflare API request failed: {e}"
        verify_result = _json_body(verify).get("result")
        verify_active = (
            verify.status_code == 200 and isinstance(verify_result, dict) and verify_result.get("status") == "active"
        )
        if not verify_active:
            return False, "\u274c That Cloudflare API token is not valid."
        zone, zone_error = await self._cloudflare_zone(client, headers, host)
        if zone_error is not None:
            return False, zone_error
        if zone is None:
            return False, (
                f"\u274c No Cloudflare zone matches {host} - is the domain on the Cloudflare account of this API token?"
            )
        zone_id = zone.get("id")
        if not isinstance(zone_id, str) or not zone_id:
            return False, _CLOUDFLARE_BAD_BODY
        try:
            existing_resp = await client.get(
                f"{_CLOUDFLARE_API}/zones/{zone_id}/dns_records?name={host}&type=CNAME", headers=headers
            )
        except (httpx.HTTPError, ValueError) as e:
            return False, f"\u274c Cloudflare API request failed: {e}"
        if existing_resp.status_code != 200:
            # A 403, 429, or 5xx is not an empty result. Treat the lookup
            # as failed instead of posting a second record.
            return False, "\u274c The token can't read DNS records - it needs Zone\u2192DNS edit rights."
        existing = _json_body(existing_resp).get("result") or []
        if not isinstance(existing, list) or any(not isinstance(record, dict) for record in existing):
            return False, _CLOUDFLARE_BAD_BODY
        if existing:
            # The lookup returns every record with this name. A record that
            # already points at the tunnel is enough; any other one blocks
            # the create call with a duplicate-record error.
            for record in existing:
                if record.get("content") == target:
                    return True, "\u2705 DNS record already points at your tunnel."
            return False, (f"\u274c {host} is already used by another DNS record ({existing[0].get('content')}).")
        try:
            created = await client.post(
                f"{_CLOUDFLARE_API}/zones/{zone_id}/dns_records",
                headers=headers,
                json={"type": "CNAME", "name": host, "content": target, "proxied": True},
            )
        except (httpx.HTTPError, ValueError) as e:
            return False, f"\u274c Cloudflare API request failed: {e}"
        if created.status_code not in (200, 201):
            errors = _json_body(created).get("errors")
            first_error = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else None
            err = (first_error or {}).get("message", created.text)
            return False, f"\u274c Could not create the DNS record: {err}"
        return True, "\u2705 DNS record created - the hostname now points at your tunnel."

    async def _finish_named_setup(self, dns_note: str | None, chat_id: int | None = None) -> tuple[str, Any]:
        """Wire up the named tunnel: local ingress config, run, enable the webhook.

        ``dns_note`` is the DNS success message, or None when the user chose
        'skip'. Then include the manual CNAME instructions instead.
        """
        chat = chat_id if chat_id is not None else self._admin_id
        state = self._state_for(chat)
        host = state.cloudflare_hostname or ""
        if not host:
            return "\u274c No hostname - start the Named tunnel flow again.", self.reply_keyboard(
                "kick_cloudflare", chat_id=chat
            )
        token = self._config.endpoint.cloudflare_token
        cfg = await self._write_cloudflared_config(host)
        ok, hint = await self._cloudflared_named_start(token, config_path=cfg)
        if not ok:
            # The retry path reads the hostname from the state, so keep it
            # until the start works.
            return f"\u274c cloudflared failed to start:\n{hint}", self.reply_keyboard(
                "kick_cloudflare_dns", chat_id=chat
            )
        state.cloudflare_hostname = None
        url = normalize_endpoint_url(f"https://{host}")
        result = await self._apply_endpoint_state(
            True, url, "cloudflare", cloudflare_token=token, cloudflare_managed=True, chat_id=chat
        )
        if is_error(result):
            # The apply failed, so the endpoint stays off. Stop the
            # cloudflared process that nothing points at.
            self._cloudflared_stop()
            return result, self.reply_keyboard("kick_cloudflare", chat_id=chat)
        if dns_note is None:
            data = decode_token(token)
            tunnel_id = (data or {}).get("t") or "your-tunnel"
            dns_note = (
                "\n\nOne last step: add this DNS record in the Cloudflare dashboard "
                "(DNS \u2192 Records \u2192 Add record):\n"
                f"CNAME {host} \u2192 {tunnel_id}.cfargotunnel.com (proxied).\n"
                "Kick only reaches the endpoint once the record resolves."
            )
        note = await self._reachability_note(url, "cloudflare")
        state.menu = "kick_cloudflare"
        return (
            f"{result}\n\n{public_url_note(self._config)}\n" + dns_note + note,
            self.reply_keyboard("kick_cloudflare", chat_id=chat),
        )

    async def _apply_cloudflare_url(self, text: str, chat_id: int | None = None) -> tuple[str, Any]:
        """Enable the endpoint with a pasted URL of the user's own (external) tunnel.

        The app does not manage this tunnel and never restarts it on boot.
        """
        chat = chat_id if chat_id is not None else self._admin_id
        state = self._state_for(chat)
        url = normalize_endpoint_url(text)
        # Pass the stored token back: the pasted URL replaces the public
        # URL only, so a later named-tunnel switch keeps the saved token.
        result = await self._apply_endpoint_state(
            True, url, "cloudflare", cloudflare_token=self._config.endpoint.cloudflare_token, chat_id=chat
        )
        if is_error(result):
            return result, self.reply_keyboard(state.menu, chat_id=chat)
        note = await self._reachability_note(url, "cloudflare")
        state.menu = "kick_cloudflare"
        return (
            f"{result}\n\n{public_url_note(self._config)}{note}",
            self.reply_keyboard("kick_cloudflare", chat_id=chat),
        )

    async def _probe_webhook_url(self, url: str) -> bool:
        """True when the public URL answers an HTTP request (tunnel and DNS work).

        Any response counts, including a 4xx from the receiver. The point
        is that the request reached the app through the tunnel.
        """
        client = self._http
        if client is None:
            return False
        try:
            await client.get(url)
        except httpx.HTTPError:
            return False
        return True

    async def _reachability_note(self, url: str, tunnel: str = "") -> str:
        """Probe the public URL and return a user-facing status line.

        Tailscale funnels need no probe. The funnel check just verified the
        host's tailscaled. Containers cannot reach the host's tailnet IP
        (Docker hairpin), so a probe always fails there.
        """
        if tunnel == "tailscale":
            return ""
        if await self._probe_webhook_url(url):
            return "\n\n\u2705 URL is reachable - save it in Kick and I'll confirm when the first event arrives."
        return (
            "\n\n\u26a0\ufe0f The URL doesn't respond yet - if you skipped the DNS step, "
            "add the DNS record first; otherwise check the tunnel logs."
        )

    def _tunnel_active(self, tunnel: str) -> bool:
        """True when the endpoint runs on ``tunnel`` right now."""
        ep = self._config.endpoint
        return ep.enabled and ep.tunnel == tunnel

    async def _teardown_tunnel(self, tunnel: str) -> None:
        """Stop the managed tunnel that the endpoint no longer uses."""
        if tunnel == "tailscale":
            if not await tailscale_funnel_off():
                logger.warning("[telegram] Cannot turn off the tailscale funnel for the listener port")
        elif tunnel == "cloudflare":
            self._cloudflared_stop()

    async def _tailscale_enable(self, chat_id: int | None = None) -> tuple[bool, str]:
        """Enable the endpoint on a tailscale funnel. Return (ok, message)."""
        port = self._config.endpoint.listen_port
        url, hint = await self._tailscale_webhook_url()
        if url is None:
            return False, f"{hint}\n\nFix tailscale and tap On again, or use Cloudflare tunnel instead."
        # Pass the stored token back: a switch to tailscale must not
        # discard the saved named-tunnel setup.
        result: str = await self._apply_endpoint_state(
            True, url, "tailscale", cloudflare_token=self._config.endpoint.cloudflare_token, chat_id=chat_id
        )
        if is_error(result):
            # The funnel above is already on. The apply failed, so the
            # endpoint stays off and the funnel would keep publishing the
            # listener port. Turn it off again.
            await self._teardown_tunnel("tailscale")
            return False, result
        note = await self._reachability_note(url, "tailscale")
        return True, (
            f"{result}\n\ntailscale funnel {port} is enabled on this host.\n{public_url_note(self._config)}{note}"
        )

    async def _disable_endpoint(self, chat_id: int | None = None, tunnel: str | None = None) -> str:
        """Turn the endpoint off and stop its tunnel. The setup stays saved.

        ``tunnel`` scopes the press to one tunnel menu, so an Off press
        there never stops the endpoint of another tunnel.
        """
        if tunnel is not None and not self._tunnel_active(tunnel):
            return f"{_TUNNEL_LABELS.get(tunnel, tunnel)} is not on."
        if not self._config.endpoint.enabled:
            return "Endpoint is already off."
        result: str = await self._apply_endpoint_state(False, chat_id=chat_id)
        if is_error(result):
            return result
        saved = self._config.endpoint
        where = f"{saved.tunnel} \u00b7 {saved.public_url}" if saved.tunnel else saved.public_url
        return f"{result}\n\nYour setup is saved ({where}). Tap Enable to restore it."

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
        return result

    async def _enable_endpoint(self, chat_id: int | None = None, tunnel: str | None = None) -> str:
        """Turn the endpoint on again with the saved tunnel setup.

        ``tunnel`` scopes the press to one tunnel menu: the enable then
        needs a saved setup for that tunnel. The Tailscale menu has no
        stored setup to restore, so it runs the funnel flow itself.
        """
        ep = self._config.endpoint
        if ep.enabled:
            if tunnel is None or ep.tunnel == tunnel:
                return "Endpoint is already on."
            active = _TUNNEL_LABELS.get(ep.tunnel, "your own URL")
            return f"The active tunnel is {active}. Turn it off first, or pick a tunnel below."
        if tunnel is not None and ep.tunnel != tunnel:
            return f"No saved {_TUNNEL_LABELS.get(tunnel, tunnel)}. Pick a tunnel below, or send me your public URL."
        if not ep.public_url:
            return "No saved tunnel yet. Choose Cloudflare tunnel or Tailscale funnel, or send me your public URL."
        if ep.tunnel == "tailscale":
            _ok, message = await self._tailscale_enable(chat_id=chat_id)
            return message
        tunnel = ep.tunnel
        started_cloudflared = False
        if tunnel == "cloudflare" and ep.cloudflare_managed:
            url, hint = await self._start_cloudflare_tunnel()
            if url is None:
                return f"\u274c {hint}"
            started_cloudflared = True
        else:
            url = ep.public_url
        result: str = await self._apply_endpoint_state(
            True,
            url,
            tunnel,
            cloudflare_token=ep.cloudflare_token,
            cloudflare_managed=ep.cloudflare_managed,
            chat_id=chat_id,
        )
        if is_error(result):
            if started_cloudflared:
                # The apply failed, so the endpoint stays off. Stop the
                # cloudflared process that nothing points at.
                self._cloudflared_stop()
            return result
        note = await self._reachability_note(url, tunnel)
        return f"{result}\n\n{public_url_note(self._config)}{note}"

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
        tunnel: str = "",
        cloudflare_token: str = "",
        cloudflare_managed: bool = False,
        chat_id: int | None = None,
    ) -> str:
        """Persist endpoint.{enabled,public_url,tunnel,...} and reconcile live state.

        One tunnel exposes the endpoint, which carries the Kick webhook
        and the control API. Enabling a different provider, or disabling
        the endpoint, tears down the previously managed tunnel: the
        tailscale funnel for the listener port, or the cloudflared
        subprocess. ``cloudflare_managed`` marks a cloudflare tunnel that
        the app started itself and restores on boot. A pasted URL with no
        token is the user's own tunnel, and the app never restarts it.
        Disabling keeps the saved URL and tunnel, so On restores the same
        setup without new input.
        """
        ep = self._config.endpoint
        old_tunnel = ep.tunnel
        old_managed = ep.cloudflare_managed
        was_enabled = ep.enabled
        # Snapshot for the rollback: a failed reconcile must leave the saved
        # state as it was. Keep it as the new state, and config would claim
        # an endpoint that nothing serves.
        before = ep.model_dump()
        notified_before = self._config.kick.webhook.setup_notified

        def mutate(candidate: AppConfig) -> None:
            ce = candidate.endpoint
            if enabled:
                # Set public_url first: the model requires an http(s)
                # URL the moment enabled flips to True.
                ce.public_url = url
                ce.tunnel = cast(Any, tunnel)
                ce.cloudflare_token = cloudflare_token
                ce.cloudflare_managed = cloudflare_managed
                # The delivery confirmation belongs to the URL, so a new
                # enable (or a new URL) proves delivery again.
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
            # report the failure, so the caller stops the tunnel that it
            # started and config claims only what the listener serves.
            try:
                await self._kick_webhook.apply_state()
                if enabled:
                    await self._kick_webhook.sync_channels(self._config.channels)
            except Exception as e:
                logger.exception("[telegram] endpoint reconcile failed")
                self._restore_endpoint_state(before, notified_before, chat_id)
                return f"\u274c The listener could not be reconfigured: {e}"
        if enabled:
            # A managed cloudflared must go when this enable no longer runs
            # it: another provider takes over, or the pasted URL says that
            # the tunnel belongs to the user. The tunnel name alone cannot
            # tell the two apart.
            if old_tunnel != tunnel or (old_tunnel == "cloudflare" and old_managed and not cloudflare_managed):
                await self._teardown_tunnel(old_tunnel)
        elif was_enabled:
            await self._teardown_tunnel(old_tunnel)
        return result
