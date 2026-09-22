"""Managed public tunnels: cloudflared and the Tailscale funnel.

The Telegram layer starts and stops these processes. This module owns the
process handling, so no other module touches a subprocess. Each call
returns a public URL or a message for the admin.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Seconds to wait for `tailscale status`.
_TAILSCALE_STATUS_TIMEOUT = 5
#: Seconds to wait for `tailscale funnel`. The first enable makes certificates.
_TAILSCALE_FUNNEL_TIMEOUT = 90
#: Seconds to wait for a quick tunnel URL.
_CLOUDFLARED_QUICK_TIMEOUT = 60
#: Seconds to wait for a named tunnel to register.
_CLOUDFLARED_RUN_TIMEOUT = 20

_CLOUDFLARED_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

#: A hostname with at least two labels, for example kick.example.com.
_HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")

#: A cloudflared tunnel id for a file name. A UUID matches this pattern.
_TUNNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: The dashboard command that holds a token: cloudflared service install <TOKEN>.
_CLOUDFLARED_INSTALL_RE = re.compile(r"^cloudflared(?:\.exe)?\s+service\s+install\s+(\S+)\s*$")


def token_from_input(text: str) -> str:
    """Token from a pasted ``cloudflared service install <TOKEN>`` command or a bare token."""
    text = text.strip()
    match = _CLOUDFLARED_INSTALL_RE.match(text)
    return match.group(1) if match else text


def decode_token(token: str) -> dict[str, Any] | None:
    """Decode a cloudflared install token into its JSON payload, or return None."""
    padded = token + "=" * (-len(token) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            payload: Any = json.loads(decoder(padded))
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def valid_token(token: str) -> bool:
    """Return True when the token holds cloudflared install credentials.

    The decoded JSON payload must contain non-empty strings under the keys
    {a: account, t: tunnel, s: secret}.
    """
    data = decode_token(token)
    return bool(isinstance(data, dict) and all(isinstance(data.get(k), str) and data[k] for k in ("a", "t", "s")))


def safe_tunnel_id(raw: Any) -> str:
    """Tunnel id of a token, safe to use in a file name.

    An id from the token can hold path separators, so the function accepts
    only the characters of a real id. Otherwise it returns ``tunnel``.
    """
    value = raw if isinstance(raw, str) else ""
    return value if _TUNNEL_ID_RE.match(value) else "tunnel"


def parse_public_hostname(text: str) -> str | None:
    """Extract a bare hostname with at least one dot from user input, or return None."""
    text = text.strip()
    if re.match(r"^https?://", text):
        try:
            host = urlsplit(text).hostname
        except ValueError:  # malformed input, for example an unclosed IPv6 bracket
            return None
    else:
        host = text
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host if _HOSTNAME_RE.match(host) else None


def write_ingress_config(workdir: Path, host: str, port: int, token: str) -> Path:
    """Write the local ingress config of a named tunnel and return its path.

    The function rejects a host that is not a plain hostname and a port
    outside 1-65535, so neither value can inject text into the ingress file.
    """
    if not _HOSTNAME_RE.match(host):
        msg = f"invalid hostname for ingress config: {host!r}"
        raise ValueError(msg)
    if not 1 <= port <= 65535:
        msg = f"invalid port for ingress config: {port!r}"
        raise ValueError(msg)
    data = decode_token(token)
    tunnel_id = safe_tunnel_id((data or {}).get("t"))
    directory = workdir / "cloudflared"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{tunnel_id}.yml"
    path.write_text(
        f"ingress:\n  - hostname: {host}\n    service: http://127.0.0.1:{port}\n  - service: http_status:404\n",
        encoding="utf-8",
    )
    return path


def _as_object(value: Any) -> dict[str, Any]:
    """A decoded JSON value as an object. Any other shape becomes an empty one.

    The tailscale CLI can answer with a list or a scalar, so every ``.get``
    on its output goes through this guard.
    """
    return value if isinstance(value, dict) else {}


async def _kill_proc(proc: Any) -> None:
    """Stop a subprocess that did not finish in time. Never raises."""
    if proc is None:
        return
    with contextlib.suppress(Exception):
        proc.kill()
        await proc.wait()


class CloudflaredTunnel:
    """The cloudflared process that this app starts and manages.

    One process runs at a time: a start stops the process of the previous
    start. ``stop`` is idempotent, so a teardown never fails on an already
    stopped tunnel.
    """

    def __init__(self) -> None:
        self._proc: Any = None
        self._drain: asyncio.Task[None] | None = None
        self._reapers: set[asyncio.Task[None]] = set()

    @property
    def running(self) -> bool:
        """True while the managed cloudflared process is alive."""
        return self._proc is not None and self._proc.returncode is None

    async def start_quick(self, port: int) -> tuple[str | None, str | None]:
        """Run a cloudflared quick tunnel and return (url, None) or (None, hint).

        The spawned process stays the managed tunnel. The caller enables
        the endpoint with the published trycloudflare URL.
        """
        self.stop()
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
        except OSError as e:
            logger.error("[tunnels] cloudflared is not available: %s", e)
            return None, (
                "cloudflared is not installed in this container.\n"
                "Rebuild the image (docker compose up -d --build) after adding cloudflared."
            )
        self._proc = proc  # publish now: a concurrent stop must reach the process
        try:
            url, tail = await asyncio.wait_for(self._wait_url(proc), timeout=_CLOUDFLARED_QUICK_TIMEOUT)
        except TimeoutError:
            await self._kill(proc)
            logger.warning(
                "[tunnels] cloudflared published no trycloudflare URL within %ss", _CLOUDFLARED_QUICK_TIMEOUT
            )
            return None, (
                "cloudflared did not publish a trycloudflare URL within "
                f"{_CLOUDFLARED_QUICK_TIMEOUT}s \u2014 tap Quick tunnel again."
            )
        if url is None:
            await self._kill(proc)
            logger.warning("[tunnels] cloudflared exited before publishing a URL:\n%s", "\n".join(tail[-8:]))
            return None, "cloudflared exited before publishing a URL:\n" + "\n".join(tail[-8:])
        if not self._adopt(proc):
            return None, "a newer tunnel start replaced this one \u2014 tap Quick tunnel again."
        return url, None

    async def start_named(self, token: str, config_path: Path | None = None) -> tuple[bool, str | None]:
        """Start a named tunnel with the install token in its environment.

        Return (True, None) or (False, hint). With ``config_path``, use the
        local ingress file so no dashboard configuration is needed. Flag
        order matters: ``--no-autoupdate`` and ``--config`` are
        ``tunnel``-command options and must precede ``run``.

        The token goes in ``TUNNEL_TOKEN`` rather than ``--token``. A command
        line is world-readable through ``/proc/<pid>/cmdline``, while the
        environment is not, and every other path treats this value as a
        credential (config.json is 0600, the token is never logged or echoed).
        """
        self.stop()
        cmd = ["cloudflared", "tunnel", "--no-autoupdate"]
        if config_path is not None:
            cmd += ["--config", str(config_path)]
        cmd += ["run"]
        env = {**os.environ, "TUNNEL_TOKEN": token}
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            logger.error("[tunnels] cloudflared is not available: %s", e)
            return False, (
                "cloudflared is not installed in this container.\n"
                "Rebuild the image (docker compose up -d --build) after adding cloudflared."
            )
        self._proc = proc  # publish now: a concurrent stop must reach the process
        try:
            registered, tail = await asyncio.wait_for(self._wait_registered(proc), timeout=_CLOUDFLARED_RUN_TIMEOUT)
        except TimeoutError:
            # A process that is still running has proved nothing: a bad token
            # or a dead network keeps cloudflared retrying without a
            # registration. Report the failed start, so the admin retries.
            await self._kill(proc)
            logger.warning("[tunnels] cloudflared did not register within %ss", _CLOUDFLARED_RUN_TIMEOUT)
            return False, (
                f"cloudflared did not register the tunnel within {_CLOUDFLARED_RUN_TIMEOUT}s \u2014 "
                "check the token and the network, then start it again."
            )
        if not registered:
            await self._kill(proc)
            logger.warning("[tunnels] cloudflared exited before registering:\n%s", "\n".join(tail[-8:]))
            return False, "cloudflared exited:\n" + "\n".join(tail[-8:])
        if not self._adopt(proc):
            return False, "a newer tunnel start replaced this one \u2014 start the named tunnel again."
        return True, None

    async def _kill(self, proc: Any) -> None:
        """Kill a process that failed to start and forget it while it is the managed one."""
        await _kill_proc(proc)
        if self._proc is proc:
            self._proc = None

    def stop(self) -> None:
        """Stop the managed process and its drain task. Idempotent."""
        drain, self._drain = self._drain, None
        proc, self._proc = self._proc, None
        if drain is not None:
            drain.cancel()
        if proc is None:
            return
        self._reap(proc)

    def _reap(self, proc: Any) -> None:
        """Kill a process and reap it, so no zombie and no open pipe stays.

        A kill alone leaves the child unreaped and its stdout pipe open.
        Without a running loop there is nothing to wait on, so the OS keeps
        the child.
        """
        try:
            task = asyncio.get_running_loop().create_task(_kill_proc(proc))
        except RuntimeError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return
        # The loop holds only a weak reference to a task, so keep our own
        # until the reaper finishes.
        self._reapers.add(task)
        task.add_done_callback(self._reapers.discard)

    def _adopt(self, proc: Any) -> bool:
        """Keep a running process as the managed tunnel.

        Return False when a newer start replaced the process while this one
        started. The method kills the older process in that case, and the
        caller must report a failed start.
        """
        if self._proc is not proc:
            logger.warning("[tunnels] a newer start replaced cloudflared (pid %s); killing it", proc.pid)
            self._reap(proc)
            return False
        self._drain = asyncio.create_task(self._drain_output(proc))
        return True

    async def _drain_output(self, proc: Any) -> None:
        """Discard cloudflared output so its pipe never fills and blocks the tunnel."""
        try:
            while await proc.stdout.readline():
                pass
        except Exception as e:
            logger.debug("[tunnels] cloudflared output drain stopped: %s", e, exc_info=True)

    async def _wait_url(self, proc: Any) -> tuple[str | None, list[str]]:
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

    async def _wait_registered(self, proc: Any) -> tuple[bool, list[str]]:
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


async def tailscale_funnel_url(port: int) -> tuple[str | None, str | None]:
    """Enable a tailscale funnel for ``port`` and return its public URL.

    Returns (url, None) on success, or (None, hint) with a user-facing
    explanation when tailscale is missing or unusable. Never raises.
    """
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
    except TimeoutError:
        await _kill_proc(proc)
        logger.warning("[tunnels] tailscale status timed out after %ss", _TAILSCALE_STATUS_TIMEOUT)
        return None, "tailscale status timed out \u2014 is the tailscale daemon running on the host?"
    except FileNotFoundError as exc:
        logger.error("[tunnels] tailscale is not available: %s", exc)
        return None, (
            "Tailscale is not installed in this container.\n"
            "Install it on the host: curl -fsSL https://tailscale.com/install.sh | sh\n"
            "then log in: tailscale up"
        )
    except OSError as exc:
        logger.error("[tunnels] tailscale status could not run: %s", exc)
        return None, f"tailscale status could not run: {exc}"
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
        logger.warning("[tunnels] tailscale status failed: %s", detail)
        return None, "tailscale status failed (daemon not running or not logged in): " + detail
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[tunnels] tailscale status returned unparseable output: %s", exc)
        return None, "tailscale status returned unparseable output"
    if not isinstance(data, dict):
        logger.warning("[tunnels] tailscale status returned a %s, not an object", type(data).__name__)
        return None, "tailscale status returned an unexpected shape"
    dns = _as_object(data.get("Self")).get("DNSName")
    dns_name = dns.rstrip(".").lower() if isinstance(dns, str) else ""
    if not dns_name:
        logger.warning("[tunnels] tailscale status shows no machine DNS name")
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
        logger.error("[tunnels] tailscale is not available for the funnel")
        return None, "Tailscale is not installed in this container."
    except TimeoutError:
        await _kill_proc(proc)
        logger.warning("[tunnels] tailscale funnel timed out after %ss", _TAILSCALE_FUNNEL_TIMEOUT)
        return None, (
            "tailscale funnel timed out (first enable provisions HTTPS certificates and can take "
            "a minute) \u2014 tap Tailscale funnel again in a moment."
        )
    except OSError as exc:
        logger.error("[tunnels] tailscale funnel could not run: %s", exc)
        return None, f"tailscale funnel could not run: {exc}"
    if proc.returncode != 0:
        stderr_text = stderr.decode(errors="replace").strip()
        logger.warning(
            "[tunnels] tailscale funnel %s exited %s: %s", port, proc.returncode, stderr_text or "(no output)"
        )
        # The funnel can already exist: a previous attempt finished after its
        # timeout, or the user tapped the menu again. Make sure that the
        # funnel really serves our port before this call reports success.
        if "listener already exists" not in stderr_text or not await tailscale_funnel_serving(port):
            return None, (f"tailscale funnel {port} failed: " + (stderr_text or f"exit {proc.returncode}"))
    return f"https://{dns_name}", None


async def tailscale_funnel_serving(port: int) -> bool:
    """True when a foreground tailscale funnel proxies / to 127.0.0.1:<port>."""
    proc = None
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
    except TimeoutError:
        await _kill_proc(proc)
        logger.debug("[tunnels] tailscale serve status timed out")
        return False
    except FileNotFoundError, OSError:
        logger.debug("[tunnels] tailscale serve status is not available")
        return False
    if proc.returncode != 0:
        logger.debug("[tunnels] tailscale serve status exited %s", proc.returncode)
        return False
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.debug("[tunnels] tailscale serve status returned unparseable output: %s", exc)
        return False
    if not isinstance(data, dict):
        logger.debug("[tunnels] tailscale serve status returned a %s, not an object", type(data).__name__)
        return False
    target = f"http://127.0.0.1:{port}"
    for fg in _as_object(data.get("Foreground")).values():
        for host in _as_object(_as_object(fg).get("Web")).values():
            for handler in _as_object(_as_object(host).get("Handlers")).values():
                if _as_object(handler).get("Proxy") == target:
                    return True
    return False


async def tailscale_funnel_off() -> bool:
    """Turn off the app-managed tailscale funnel for the endpoint (best effort).

    Newer tailscale CLIs reject ``--bg <port> off``. The documented form
    is ``tailscale funnel --https=443 off``, because funnels listen on 443.
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
        logger.warning("[tunnels] tailscale funnel off could not run")
        return False
    if proc.returncode != 0:
        logger.warning("[tunnels] tailscale funnel off exited %s", proc.returncode)
        return False
    return True
