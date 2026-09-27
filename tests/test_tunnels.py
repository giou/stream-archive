"""Tests for the proxy config generators (no tunnels are managed)."""

from __future__ import annotations

import base64
import json

import pytest

from stream_archive.tunnels import (
    parse_public_hostname,
    token_from_input,
    valid_token,
    write_ingress_config,
    write_nginx_config,
)


def _token(payload):
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_token_from_input_strips_the_install_command():
    token = _token({"a": "a", "t": "t", "s": "s"})
    assert token_from_input(f"cloudflared service install {token}") == token
    assert token_from_input(token) == token


def test_valid_token_needs_account_tunnel_and_secret():
    assert valid_token(_token({"a": "a", "t": "t", "s": "s"})) is True
    assert valid_token(_token({"a": "a", "t": "t"})) is False
    assert valid_token("not-a-token") is False


def test_parse_public_hostname_accepts_url_and_bare_host():
    assert parse_public_hostname("https://kick.example.com/kick/webhook") == "kick.example.com"
    assert parse_public_hostname("kick.example.com") == "kick.example.com"
    assert parse_public_hostname("KICK.Example.COM.") == "kick.example.com"


def test_parse_public_hostname_rejects_garbage():
    assert parse_public_hostname("no-dot") is None
    assert parse_public_hostname("https://[::1") is None
    assert parse_public_hostname("") is None


def test_write_ingress_config_exact_bytes(tmp_path):
    path = write_ingress_config(
        tmp_path,
        _token({"a": "a", "t": "tid-1", "s": "s"}),
        (("kick.example.com", 8788), ("panel.example.com", 8787)),
    )
    assert path == tmp_path / "cloudflared" / "tid-1.yml"
    assert path.read_text() == (
        "ingress:\n"
        "  - hostname: kick.example.com\n"
        "    service: http://127.0.0.1:8788\n"
        "  - hostname: panel.example.com\n"
        "    service: http://127.0.0.1:8787\n"
        "  - service: http_status:404\n"
    )


def test_write_ingress_config_rejects_bad_input(tmp_path):
    with pytest.raises(ValueError, match="invalid hostname"):
        write_ingress_config(tmp_path, "x", (("no-dot", 8788),))
    with pytest.raises(ValueError, match="invalid port"):
        write_ingress_config(tmp_path, "x", (("kick.example.com", 70000),))
    with pytest.raises(ValueError, match="duplicate hostname"):
        write_ingress_config(tmp_path, "x", (("kick.example.com", 8788), ("kick.example.com", 8787)))
    with pytest.raises(ValueError, match="at least one hostname"):
        write_ingress_config(tmp_path, "x", ())


def test_write_nginx_config_webhook_only(tmp_path):
    path = write_nginx_config(tmp_path, "kick.example.com", 8788)
    assert path == tmp_path / "nginx" / "kick.example.com.conf"
    text = path.read_text()
    assert "server_name kick.example.com;" in text
    assert "location = /kick/webhook" in text
    assert "proxy_pass http://127.0.0.1:8788;" in text
    assert "/api/v1/" not in text
    assert "return 444;" in text
    assert "/etc/letsencrypt/live/kick.example.com/fullchain.pem" in text


def test_write_nginx_config_with_api(tmp_path):
    text = write_nginx_config(tmp_path, "kick.example.com", 8788, 8787).read_text()
    assert "location ^~ /api/v1/" in text
    assert "proxy_pass http://127.0.0.1:8787;" in text


def test_write_nginx_config_rejects_bad_input(tmp_path):
    with pytest.raises(ValueError, match="invalid hostname"):
        write_nginx_config(tmp_path, "../escape", 8788)
    with pytest.raises(ValueError, match="invalid port"):
        write_nginx_config(tmp_path, "kick.example.com", 0)
    with pytest.raises(ValueError, match="invalid port"):
        write_nginx_config(tmp_path, "kick.example.com", 8788, 70000)


def test_write_nginx_config_plain_behind_tunnel(tmp_path):
    """Without TLS the block listens plain HTTP on loopback for the tunnel."""
    text = write_nginx_config(tmp_path, "kick.example.com", 8788, 8787, tls=False).read_text()
    assert "listen 127.0.0.1:8090;" in text
    assert "ssl_certificate" not in text
    assert "location = /kick/webhook" in text
    assert "proxy_pass http://127.0.0.1:8788;" in text
    assert "location ^~ /api/v1/" in text
    assert "return 444;" in text
