"""Proxy config generators: cloudflared ingress and nginx server blocks.

The app never runs a tunnel. The user runs their own reverse proxy, and
these helpers write its fiddly files: validated hostnames and ports, so
neither value can inject text into the generated file. The setup wizard
calls them after the user picks a proxy.
"""

import base64
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: A hostname with at least two labels, for example kick.example.com.
_HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")

#: A cloudflared tunnel id for a file name. A UUID matches this pattern.
_TUNNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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


def _check_host_port(host: str, port: int, what: str) -> None:
    """Reject a hostname or port that must not reach a generated file."""
    if not _HOSTNAME_RE.match(host):
        msg = f"invalid hostname for {what}: {host!r}"
        raise ValueError(msg)
    if not 1 <= port <= 65535:
        msg = f"invalid port for {what}: {port!r}"
        raise ValueError(msg)


def write_ingress_config(workdir: Path, token: str, services: tuple[tuple[str, int], ...]) -> Path:
    """Write the local ingress config of a named tunnel and return its path.

    ``services`` maps each public hostname to its loopback port: the Kick
    entry to the webhook listener, the panel entry to the private
    listener. One hostname may appear once only. Run it with the user's
    own cloudflared: ``cloudflared tunnel --config <path> run``.
    """
    if not services:
        msg = "ingress config needs at least one hostname"
        raise ValueError(msg)
    seen: set[str] = set()
    for host, port in services:
        _check_host_port(host, port, "ingress config")
        if host in seen:
            msg = f"duplicate hostname for ingress config: {host!r}"
            raise ValueError(msg)
        seen.add(host)
    data = decode_token(token)
    tunnel_id = safe_tunnel_id((data or {}).get("t"))
    directory = workdir / "cloudflared"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{tunnel_id}.yml"
    rules = "".join(f"  - hostname: {host}\n    service: http://127.0.0.1:{port}\n" for host, port in services)
    path.write_text(f"ingress:\n{rules}  - service: http_status:404\n", encoding="utf-8")
    return path


def write_nginx_config(
    workdir: Path, host: str, webhook_port: int, api_port: int | None = None, *, tls: bool = True
) -> Path:
    """Write an nginx server block for the public entries and return its path.

    The block proxies POST /kick/webhook to the webhook listener and
    answers 444 (drop) on every other path, so the panel never leaks
    through the public hostname. With ``api_port``, it also proxies
    /api/v1/ to the private listener for remote control. With ``tls``
    (the default) it terminates TLS on 443 with certbot certificates.
    With ``tls=False`` it listens plain HTTP on 127.0.0.1:8090 for use
    behind a tunnel that terminates TLS itself (for example cloudflared):
    point the tunnel ingress at ``http://127.0.0.1:8090``.
    Enable it with a symlink from sites-enabled, then ``nginx -s reload``.
    """
    _check_host_port(host, webhook_port, "nginx config")
    if api_port is not None and not 1 <= api_port <= 65535:
        msg = f"invalid port for nginx config: {api_port!r}"
        raise ValueError(msg)
    if tls:
        head = (
            "    listen 443 ssl;\n"
            f"    server_name {host};\n"
            "\n"
            f"    ssl_certificate /etc/letsencrypt/live/{host}/fullchain.pem;\n"
            f"    ssl_certificate_key /etc/letsencrypt/live/{host}/privkey.pem;\n"
        )
    else:
        head = (
            "    # Plain HTTP behind a TLS-terminating tunnel. The tunnel\n"
            "    # ingress points at http://127.0.0.1:8090 for this hostname.\n"
            "    listen 127.0.0.1:8090;\n"
            f"    server_name {host};\n"
        )
    v1 = ""
    if api_port is not None:
        v1 = (
            "\n    # Remote control. The API key travels in a header, so this\n"
            "    # location is safe to publish. Nothing else under / is served.\n"
            "    location ^~ /api/v1/ {\n"
            f"        proxy_pass http://127.0.0.1:{api_port};\n"
            "        proxy_set_header Host $host;\n"
            "    }\n"
        )
    text = (
        f"# Generated for {host}. The app wrote this file; the user runs nginx.\n"
        f"server {{\n"
        f"{head}"
        f"\n"
        f"    # Kick deliveries only. Every other path drops the connection,\n"
        f"    # so the panel root stays off the public internet.\n"
        f"    location = /kick/webhook {{\n"
        f"        proxy_pass http://127.0.0.1:{webhook_port};\n"
        f"        proxy_set_header Host $host;\n"
        f"    }}\n"
        f"{v1}"
        f"    location / {{\n"
        f"        return 444;\n"
        f"    }}\n"
        f"}}\n"
    )
    directory = workdir / "nginx"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{host}.conf"
    path.write_text(text, encoding="utf-8")
    return path
