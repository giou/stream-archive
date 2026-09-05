import asyncio
import base64
import contextlib
import json
import logging
import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from stream_archive.config import AppConfig
from stream_archive.telegram.menu_state import ChatId, MenuState

logger = logging.getLogger(__name__)


_TAILSCALE_STATUS_TIMEOUT = 5

_TAILSCALE_FUNNEL_TIMEOUT = 90

_CLOUDFLARED_QUICK_TIMEOUT = 60

_CLOUDFLARED_RUN_TIMEOUT = 20

_CLOUDFLARED_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

_CLOUDFLARED_INSTALL_RE = re.compile(r"^cloudflared(?:\.exe)?\s+service\s+install\s+(\S+)\s*$")

_KICK_DASHBOARD_HINT = (
    "Paste this URL into the Kick app under Settings \u2192 Developer \u2192 your app \u2192 Enable webhooks."
)

_CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"

_HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def _decode_cloudflared_token(token: str) -> dict[str, Any] | None:
    """Decode a cloudflared install token into its JSON payload, or return None."""
    padded = token + "=" * (-len(token) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            payload: dict[str, Any] | None = json.loads(decoder(padded))
        except Exception:
            continue
        return payload
    return None


def _valid_cloudflare_token(token: str) -> bool:
    """Return True when the token holds cloudflared install credentials.

    The decoded JSON payload must contain non-empty strings under the keys
    {a: account, t: tunnel, s: secret}.
    """
    data = _decode_cloudflared_token(token)
    return bool(isinstance(data, dict) and all(isinstance(data.get(k), str) and data[k] for k in ("a", "t", "s")))


def _normalize_webhook_url(url: str) -> str:
    """Append the receiver path when the URL points at the host root."""
    if urlsplit(url).path in ("", "/"):
        return url.rstrip("/") + "/kick/webhook"
    return url


def _parse_public_hostname(text: str) -> str | None:
    """Extract a bare hostname with at least one dot from user input, or return None."""
    text = text.strip()
    host = urlsplit(text).hostname if re.match(r"^https?://", text) else text
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host if _HOSTNAME_RE.match(host) else None


class WebhookCommands:
    def _state_for(self, chat_id: ChatId) -> MenuState:
        """Per-chat menu state. ChatStateMixin provides this on the controller."""
        raise NotImplementedError

    _config: AppConfig
    _apply: Any
    _kick_webhook: Any
    _http: httpx.AsyncClient | None
    _cloudflared: Any
    _cloudflared_drain: Any
    _send_admin: Any
    _admin_id: int
    reply_keyboard: Any

    def _webhook_state_text(self) -> str:
        w = self._config.kick.webhook
        if not w.enabled:
            return "off"
        tunnel = w.tunnel or ""
        url = w.public_url
        return f"on ({tunnel} \u00b7 {url})" if tunnel else f"on ({url})"

    async def _tailscale_webhook_url(self) -> tuple[str | None, str | None]:
        """Detect tailscale, enable a funnel for the webhook port, return its public URL.

        Returns (url, None) on success, or (None, hint) with a user-facing
        explanation when tailscale is missing or unusable. Never raises.
        """
        port = self._config.kick.webhook.listen_port
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale",
                "status",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TAILSCALE_STATUS_TIMEOUT)
        except FileNotFoundError:
            return None, (
                "Tailscale is not installed in this container.\n"
                "Install it on the host: curl -fsSL https://tailscale.com/install.sh | sh\n"
                "then log in: tailscale up"
            )
        except TimeoutError:
            await self._kill_proc(proc)
            return None, "tailscale status timed out \u2014 is the tailscale daemon running on the host?"
        if proc.returncode != 0:
            return None, (
                "tailscale status failed (daemon not running or not logged in): "
                + (stderr.decode(errors="replace").strip() or f"exit {proc.returncode}")
            )
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return None, "tailscale status returned unparseable output"
        dns_name = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
        if not dns_name:
            return None, "tailscale status shows no machine DNS name \u2014 is this machine in a tailnet?"
        proc = None
        try:
            # --bg registers the funnel with the daemon and exits. The plain
            # form serves in the foreground and never returns. --yes skips
            # the interactive prompts that hang a piped subprocess.
            proc = await asyncio.create_subprocess_exec(
                "tailscale",
                "funnel",
                "--bg",
                "--yes",
                str(port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TAILSCALE_FUNNEL_TIMEOUT)
        except FileNotFoundError:
            return None, "Tailscale is not installed in this container."
        except TimeoutError:
            await self._kill_proc(proc)
            return None, (
                "tailscale funnel timed out (first enable provisions HTTPS certificates and can take "
                "a minute) \u2014 tap Tailscale funnel again in a moment."
            )
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            # The funnel can already exist: a previous attempt finished
            # after its timeout, or the user re-clicked the menu. Verify
            # that the funnel really serves our port before reporting
            # success.
            if "listener already exists" not in stderr_text or not await self._funnel_serving(port):
                return None, (f"tailscale funnel {port} failed: " + (stderr_text or f"exit {proc.returncode}"))
        return f"https://{dns_name}/kick/webhook", None

    async def _funnel_serving(self, port: int) -> bool:
        """True when a foreground tailscale funnel proxies / to 127.0.0.1:<port>."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale",
                "serve",
                "status",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_TAILSCALE_STATUS_TIMEOUT)
        except TimeoutError, FileNotFoundError, OSError:
            return False
        if proc.returncode != 0:
            return False
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return False
        target = f"http://127.0.0.1:{port}"
        for fg in (data.get("Foreground") or {}).values():
            for host in (fg.get("Web") or {}).values():
                for handler in (host.get("Handlers") or {}).values():
                    if handler.get("Proxy") == target:
                        return True
        return False

    async def _kill_proc(self, proc: Any) -> None:
        if proc is None:
            return
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass

    async def _cloudflared_quick_start(self) -> tuple[str | None, str | None]:
        """Run a cloudflared quick tunnel and return (url, None) or (None, hint).

        Keep the spawned process as the managed tunnel. Callers enable the
        webhook with the published trycloudflare URL.
        """
        self._cloudflared_stop()
        port = self._config.kick.webhook.listen_port
        try:
            proc = await asyncio.create_subprocess_exec(
                # --no-autoupdate is a root flag. It must precede the subcommand.
                "cloudflared",
                "--no-autoupdate",
                "tunnel",
                "--url",
                f"http://127.0.0.1:{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError:
            return None, (
                "cloudflared is not installed in this container.\n"
                "Rebuild the image (docker compose up -d --build) after adding cloudflared."
            )
        try:
            url, tail = await asyncio.wait_for(self._wait_cloudflared_url(proc), timeout=_CLOUDFLARED_QUICK_TIMEOUT)
        except TimeoutError:
            await self._kill_proc(proc)
            return None, (
                "cloudflared did not publish a trycloudflare URL within "
                f"{_CLOUDFLARED_QUICK_TIMEOUT}s \u2014 tap Quick tunnel again."
            )
        if url is None:
            await self._kill_proc(proc)
            return None, "cloudflared exited before publishing a URL:\n" + "\n".join(tail[-8:])
        self._cloudflared = proc
        self._cloudflared_drain = asyncio.create_task(self._drain_cloudflared(proc))
        return _normalize_webhook_url(url), None

    async def _cloudflared_named_start(self, token: str, config_path: Path | None = None) -> tuple[bool, str | None]:
        """Start a named tunnel with ``cloudflared tunnel run --token``.

        Return (True, None) or (False, hint). With ``config_path``, use the
        local ingress file so no dashboard configuration is needed. Flag
        order matters: ``--no-autoupdate`` and ``--config`` are
        ``tunnel``-command options and must precede ``run``.
        """
        self._cloudflared_stop()
        cmd = ["cloudflared", "tunnel", "--no-autoupdate"]
        if config_path is not None:
            cmd += ["--config", str(config_path)]
        cmd += ["run", "--token", token]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError:
            return False, (
                "cloudflared is not installed in this container.\n"
                "Rebuild the image (docker compose up -d --build) after adding cloudflared."
            )
        try:
            registered, tail = await asyncio.wait_for(
                self._wait_cloudflared_registered(proc), timeout=_CLOUDFLARED_RUN_TIMEOUT
            )
        except TimeoutError:
            if proc.returncode is None:  # still running: the tunnel registered
                self._cloudflared = proc
                self._cloudflared_drain = asyncio.create_task(self._drain_cloudflared(proc))
                return True, None
            await self._kill_proc(proc)
            return False, "cloudflared exited during startup."
        if not registered:
            await self._kill_proc(proc)
            return False, "cloudflared exited:\n" + "\n".join(tail[-8:])
        self._cloudflared = proc
        self._cloudflared_drain = asyncio.create_task(self._drain_cloudflared(proc))
        return True, None

    async def _wait_cloudflared_url(self, proc: Any) -> tuple[str | None, list[str]]:
        """Read cloudflared output until the trycloudflare URL appears or EOF.

        Return (url, tail_lines).
        """
        tail: list[str] = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                return None, tail
            decoded = line.decode(errors="replace").strip()
            tail.append(decoded)
            m = _CLOUDFLARED_URL_RE.search(decoded)
            if m:
                return m.group(0), tail

    async def _wait_cloudflared_registered(self, proc: Any) -> tuple[bool, list[str]]:
        """Read cloudflared output until the named tunnel registers or EOF.

        Return (ok, tail_lines).
        """
        tail: list[str] = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                return False, tail
            decoded = line.decode(errors="replace").strip()
            tail.append(decoded)
            if "Registered tunnel connection" in decoded:
                return True, tail

    async def _drain_cloudflared(self, proc: Any) -> None:
        """Discard cloudflared output so its pipe never fills and blocks the tunnel."""
        try:
            while await proc.stdout.readline():
                pass
        except Exception:
            pass

    def _cloudflared_stop(self) -> None:
        """Kill the managed cloudflared subprocess and its drain task (idempotent)."""
        drain, self._cloudflared_drain = self._cloudflared_drain, None
        proc, self._cloudflared = self._cloudflared, None
        if drain is not None:
            drain.cancel()
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    async def _restore_cloudflared(self) -> None:
        """Restart an app-managed cloudflared after a service restart (webhook enabled)."""
        try:
            w = self._config.kick.webhook
            if w.enabled and w.tunnel == "cloudflare" and w.cloudflare_managed:
                token = w.cloudflare_token
                if token:
                    host = urlsplit(w.public_url or "").hostname
                    cfg = await self._write_cloudflared_config(host) if host else None
                    ok, hint = await self._cloudflared_named_start(token, config_path=cfg)
                    if not ok:
                        await self._send_admin(f"\u274c cloudflared failed to restart your named tunnel:\n{hint}")
                else:
                    url, hint = await self._cloudflared_quick_start()
                    if url is None:
                        await self._send_admin(f"\u274c cloudflared quick tunnel failed to restart:\n{hint}")
                    elif url != w.public_url:

                        def mutate(candidate: AppConfig) -> None:
                            candidate.kick.webhook.public_url = url
                            candidate.kick.webhook.setup_notified = False

                        self._apply(mutate, lambda c: "public_url updated")
                        note = await self._reachability_note(url, "cloudflare")
                        await self._send_admin(
                            "\U0001f4a1 Your cloudflared quick tunnel restarted with a new temporary URL:\n\n"
                            f"```\n{url}\n```\n"
                            "The previous trycloudflare URL expired. " + _KICK_DASHBOARD_HINT + note
                        )
        except Exception:
            logger.exception("[telegram] cloudflared restore failed")

    async def _handle_cloudflare_token(self, text: str, chat_id: int | None = None) -> tuple[bool, str]:
        """Validate a pasted cloudflared token/command and persist it.

        Return (True, message) with the next-step prompt, or (False, error).
        """
        text = text.strip()
        m = _CLOUDFLARED_INSTALL_RE.match(text)
        token = m.group(1) if m else text
        if not _valid_cloudflare_token(token):
            return False, (
                "\u274c That doesn't look like a cloudflared tunnel token.\n\n"
                "Send the token from the Cloudflare dashboard command "
                "(cloudflared service install <TOKEN>) \u2014 or paste the whole command."
            )
        result: str = self._apply(
            lambda candidate: setattr(candidate.kick.webhook, "cloudflare_token", token),
            lambda c: "token saved",
            chat_id,
        )
        if result.startswith("\u274c"):
            return False, result
        return True, (
            "\u2705 Tunnel token accepted.\n\n"
            "Send the public hostname to use for the webhook, e.g. kick.example.com \u2014 "
            "I'll point your tunnel at this app automatically."
        )

    async def _write_cloudflared_config(self, host: str) -> Path:
        """Write the local ingress config for the named tunnel and return its path."""
        wh = self._config.kick.webhook
        port = wh.listen_port
        data = _decode_cloudflared_token(wh.cloudflare_token)
        tunnel_id = (data or {}).get("t") or "tunnel"
        directory = Path(self._config._workdir) / "cloudflared"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{tunnel_id}.yml"
        path.write_text(
            f"ingress:\n  - hostname: {host}\n    service: http://127.0.0.1:{port}\n  - service: http_status:404\n"
        )
        return path

    async def _create_cloudflare_dns(self, api_token: str, chat_id: int | None = None) -> tuple[bool, str]:
        """Create the CNAME for the named tunnel's hostname via the Cloudflare API.

        Return (True, message) on success, including when the record already
        points at the tunnel, or (False, error) otherwise. Never raises.
        """
        chat = chat_id if chat_id is not None else self._admin_id
        host = self._state_for(chat).cloudflare_hostname or ""
        wh = self._config.kick.webhook
        data = _decode_cloudflared_token(wh.cloudflare_token)
        tunnel_id = (data or {}).get("t") or ""
        account_id = (data or {}).get("a") or ""
        if not host or not tunnel_id:
            return False, "\u274c Missing hostname or tunnel token \u2014 start the Named tunnel flow again."
        headers = {"Authorization": f"Bearer {api_token}"}
        target = f"{tunnel_id}.cfargotunnel.com"
        client = self._http
        if client is None:
            return False, "\u274c HTTP client is not ready \u2014 try again in a moment."
        # Account-owned tokens (cfat_ prefix) reject the user-scoped
        # verify endpoint. Fall back to the account-scoped endpoint.
        # Each raising call has its own guard: .json() raises ValueError
        # on a non-JSON body (proxy 502 HTML pages). This handler must
        # never escape with an exception mid-flow.
        try:
            verify = await client.get(f"{_CLOUDFLARE_API}/user/tokens/verify", headers=headers)
            if verify.status_code != 200 and account_id:
                verify = await client.get(f"{_CLOUDFLARE_API}/accounts/{account_id}/tokens/verify", headers=headers)
        except (httpx.HTTPError, ValueError) as e:
            return False, f"\u274c Cloudflare API request failed: {e}"
        try:
            verify_active = verify.status_code == 200 and (verify.json().get("result") or {}).get("status") == "active"
        except ValueError:
            # json() fails on a non-JSON body (proxy 502 HTML
            # pages). Treat the token as unusable instead of
            # escaping mid-flow.
            verify_active = False
        if not verify_active:
            return False, "\u274c That Cloudflare API token is not valid."
        try:
            zones_resp = await client.get(f"{_CLOUDFLARE_API}/zones?per_page=50", headers=headers)
        except (httpx.HTTPError, ValueError) as e:
            return False, f"\u274c Cloudflare API request failed: {e}"
        if zones_resp.status_code != 200:
            return False, (
                "\u274c The token can't list zones \u2014 it needs Zone read (use the 'Edit zone DNS' template)."
            )
        try:
            zones_result: list[dict[str, Any]] = zones_resp.json().get("result") or []
        except ValueError as e:
            return False, f"\u274c Cloudflare API request failed: {e}"
        zone: dict[str, Any] | None = None
        for z in zones_result:
            name = (z.get("name") or "").lower()
            if (host == name or host.endswith("." + name)) and (zone is None or len(name) > len(zone["name"])):
                zone = z
        if zone is None:
            return False, (
                f"\u274c No Cloudflare zone matches {host} \u2014 is the domain "
                "on the Cloudflare account of this API token?"
            )
        zone_id = zone["id"]
        try:
            existing_resp = await client.get(
                f"{_CLOUDFLARE_API}/zones/{zone_id}/dns_records?name={host}&type=CNAME", headers=headers
            )
        except (httpx.HTTPError, ValueError) as e:
            return False, f"\u274c Cloudflare API request failed: {e}"
        try:
            existing: list[dict[str, Any]] = (
                (existing_resp.json().get("result") or []) if existing_resp.status_code == 200 else []
            )
        except ValueError as e:
            return False, f"\u274c Cloudflare API request failed: {e}"
        if existing:
            if existing[0].get("content") == target:
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
            try:
                err = (created.json().get("errors") or [{}])[0].get("message", created.text)
            except ValueError as e:
                return False, f"\u274c Cloudflare API request failed: {e}"
            return False, f"\u274c Could not create the DNS record: {err}"
        return True, "\u2705 DNS record created \u2014 the hostname now points at your tunnel."

    async def _finish_named_setup(self, dns_note: str | None, chat_id: int | None = None) -> tuple[str, Any]:
        """Wire up the named tunnel: local ingress config, run, enable the webhook.

        ``dns_note`` is the DNS success message, or None when the user chose
        'skip'. Then include the manual CNAME instructions instead.
        """
        chat = chat_id if chat_id is not None else self._admin_id
        state = self._state_for(chat)
        host = state.cloudflare_hostname or ""
        state.cloudflare_hostname = None
        if not host:
            return "\u274c No hostname \u2014 start the Named tunnel flow again.", self.reply_keyboard(
                "kick_cloudflare"
            )
        wh = self._config.kick.webhook
        token = wh.cloudflare_token
        cfg = await self._write_cloudflared_config(host)
        ok, hint = await self._cloudflared_named_start(token, config_path=cfg)
        if not ok:
            return f"\u274c cloudflared failed to start:\n{hint}", self.reply_keyboard("kick_cloudflare_dns")
        url = _normalize_webhook_url(f"https://{host}")
        result = await self._apply_webhook_state(
            True, url, "cloudflare", cloudflare_token=token, cloudflare_managed=True, chat_id=chat
        )
        if result.startswith("\u274c"):
            return result, self.reply_keyboard("kick_cloudflare")
        if dns_note is None:
            data = _decode_cloudflared_token(token)
            tunnel_id = (data or {}).get("t") or "your-tunnel"
            dns_note = (
                "\n\nOne last step: add this DNS record in the Cloudflare dashboard "
                "(DNS \u2192 Records \u2192 Add record):\n"
                f"CNAME {host} \u2192 {tunnel_id}.cfargotunnel.com (proxied).\n"
                "The webhook only starts receiving events once the record resolves."
            )
        note = await self._reachability_note(url, "cloudflare")
        state.menu = "kick_webhook"
        return (
            f"{result}\n\n```\n{url}\n```\n" + _KICK_DASHBOARD_HINT + "\n" + dns_note + note,
            self.reply_keyboard("kick_webhook"),
        )

    async def _apply_cloudflare_url(self, text: str, chat_id: int | None = None) -> tuple[str, Any]:
        """Enable the webhook with a pasted URL of the user's own (external) tunnel.

        The app does not manage this tunnel and never restarts it on boot.
        """
        chat = chat_id if chat_id is not None else self._admin_id
        state = self._state_for(chat)
        url = _normalize_webhook_url(text.strip())
        result = await self._apply_webhook_state(True, url, "cloudflare", chat_id=chat)
        if result.startswith("\u274c"):
            return result, self.reply_keyboard(state.menu)
        note = await self._reachability_note(url, "cloudflare")
        state.menu = "kick_webhook"
        return (f"{result}\n\n```\n{url}\n```\n" + _KICK_DASHBOARD_HINT + note, self.reply_keyboard("kick_webhook"))

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
            return "\n\n\u2705 URL is reachable \u2014 save it in Kick and I'll confirm when the first event arrives."
        return (
            "\n\n\u26a0\ufe0f The URL doesn't respond yet \u2014 if you skipped the DNS step, "
            "add the DNS record first; otherwise check the tunnel logs."
        )

    async def _tailscale_funnel_off(self) -> bool:
        """Turn off the app-managed tailscale funnel for the webhook port (best effort).

        Newer tailscale CLIs reject ``--bg <port> off``. The documented form
        is ``tailscale funnel --https=443 off``, because funnels only ever
        listen on 443.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale",
                "funnel",
                "--https=443",
                "off",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=_TAILSCALE_STATUS_TIMEOUT)
        except TimeoutError, FileNotFoundError, OSError:
            return False
        return proc.returncode == 0

    async def _apply_webhook_state(
        self,
        enabled: bool,
        url: str,
        tunnel: str = "",
        cloudflare_token: str = "",
        cloudflare_managed: bool = False,
        chat_id: int | None = None,
    ) -> str:
        """Persist kick.webhook.{enabled,public_url,tunnel,...} and reconcile live state.

        Kick accepts a single webhook URL, so only one tunnel can expose
        the receiver. Enabling a different provider, or disabling the
        webhook, tears down the previously managed tunnel: the tailscale
        funnel for the webhook port, or the cloudflared subprocess.
        ``cloudflare_managed`` marks a cloudflare tunnel that the app
        started itself and restores on boot. A pasted URL with no token is
        the user's own tunnel, and the app never restarts it.
        """
        wh = self._config.kick.webhook
        old_tunnel = wh.tunnel
        old_setup_notified = wh.setup_notified

        def mutate(candidate: AppConfig) -> None:
            cw = candidate.kick.webhook
            if enabled:
                # Set public_url first: the model requires an http(s)
                # URL the moment enabled flips to True.
                cw.public_url = url
                cw.tunnel = cast(Any, tunnel)
                cw.cloudflare_token = cloudflare_token
                cw.cloudflare_managed = cloudflare_managed
                # Re-arm the "webhook is working" confirmation. It fires on
                # the first verified Kick event, so a re-enable with a new
                # tunnel or URL confirms again.
                cw.setup_notified = False
                cw.enabled = True
            else:
                cw.enabled = False
                cw.public_url = url
                cw.tunnel = ""
                cw.cloudflare_token = ""
                cw.cloudflare_managed = False
                cw.setup_notified = old_setup_notified

        result: str = self._apply(
            mutate,
            lambda c: f"Kick webhook {'enabled' if enabled else 'disabled'}",
            chat_id,
        )
        if result.startswith("\u274c"):
            return result
        if self._kick_webhook is not None:
            if enabled:
                await self._kick_webhook.start()  # idempotent
                await self._kick_webhook.sync_channels(self._config.channels)
            else:
                await self._kick_webhook.close()  # idempotent
        if old_tunnel == "tailscale" and tunnel != "tailscale" and not await self._tailscale_funnel_off():
            logger.warning("[telegram] Could not turn off the tailscale funnel for the webhook port")
        if old_tunnel == "cloudflare" and tunnel != "cloudflare":
            self._cloudflared_stop()
        return result
