"""Tests for the guided setup wizard.

Each test owns one contract: fresh-file creation, re-run secret
preservation, the control-surface choice, and the YouTube delegation.
The OAuth flow itself is covered by test_setup_youtube.py.
"""

from __future__ import annotations

import getpass

import pytest
from conftest import make_config

from stream_archive import setup_wizard as wizard
from stream_archive.config import get_config, save_config, telegram_enabled
from stream_archive.webui import verify_password


def _script(monkeypatch: pytest.MonkeyPatch, inputs: list[str], secrets: list[str]) -> None:
    """Feed scripted answers to input() and getpass.getpass() in order."""
    answers = iter(inputs)
    hidden = iter(secrets)

    def answer(*_args: object, **_kwargs: object) -> str:
        try:
            return next(answers)
        except StopIteration:
            msg = "wizard asked for more input than scripted"
            raise AssertionError(msg) from None

    def hidden_answer(*_args: object, **_kwargs: object) -> str:
        try:
            return next(hidden)
        except StopIteration:
            msg = "wizard asked for more secrets than scripted"
            raise AssertionError(msg) from None

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(getpass, "getpass", hidden_answer)


def _write_config(tmp_path, **overrides):  # type: ignore[no-untyped-def]
    """Persist a config file in tmp_path for a re-run test."""
    from pathlib import Path

    config = make_config(**overrides)
    config._workdir = tmp_path
    config._config_path = Path(tmp_path) / "config.json"
    save_config(config)
    return config


def test_fresh_run_creates_valid_config(monkeypatch, tmp_path):
    """A first run with no config.json writes a file the app accepts.

    Channels stay out of the first run: they arrive later from the panel,
    the bot, or the Channels step.
    """
    monkeypatch.chdir(tmp_path)
    _script(
        monkeypatch,
        inputs=["tid123", "1", "8"],
        secrets=["tsecret123", "long-enough-password", "long-enough-password"],
    )
    wizard.main()
    assert (tmp_path / "config.json").exists()
    config = get_config(tmp_path / "config.json")
    assert config.channels == []
    assert config.twitch_client_id == "tid123"
    assert config.twitch_client_secret == "tsecret123"
    assert config.web.enabled is True
    assert verify_password("long-enough-password", config.web.password_hash) is True
    assert telegram_enabled(config) is False
    assert len(config.proxy_list) == 5


def test_kick_step_asks_creds_then_tunnel(monkeypatch, tmp_path):
    """The kick step takes credentials, then the chat question, then the entry pick."""
    _write_config(tmp_path, kick={"client_id": "", "client_secret": ""})
    monkeypatch.chdir(tmp_path)
    _script(
        monkeypatch,
        inputs=["4", "kid123", "y", "3", "kick.example.com", "n", "8"],
        secrets=["ksecret123"],
    )
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.kick.client_id == "kid123"
    assert config.kick.client_secret == "ksecret123"
    assert config.kick.webhook.public_url == "https://kick.example.com"
    assert config.kick.webhook.enabled is True


def test_rerun_enables_mtproto_and_keeps_stored_secrets(monkeypatch, tmp_path):
    """A re-run that adds MTProto must not touch the stored secrets."""
    from stream_archive.webui import hash_password

    old_hash = hash_password("old-password-123")
    _write_config(tmp_path, web={"enabled": True, "password_hash": old_hash})
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["6", "y", "123456", "8"], secrets=["abcdef123456"])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.mtproto.enabled is True
    assert config.mtproto.api_id == 123456
    assert config.mtproto.api_hash == "abcdef123456"
    assert config.web.password_hash == old_hash
    assert config.bot_telegram_api == "bot_token"


def test_control_choice_telegram_enables_bot_only(monkeypatch, tmp_path):
    """The Telegram choice enables the bot and leaves the panel off."""
    _write_config(tmp_path, telegram_user_id=0, bot_telegram_api="")
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["2", "2", "42", "8"], secrets=["bottoken123"])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert telegram_enabled(config) is True
    assert config.telegram_user_id == 42
    assert config.web.enabled is False


def test_youtube_step_saves_mode_and_runs_oauth(monkeypatch, tmp_path):
    """The YouTube step stores the mode first, then delegates the OAuth flow."""
    _write_config(tmp_path)
    (tmp_path / "client_secret.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(wizard, "youtube_main", lambda: calls.append("oauth"))
    _script(monkeypatch, inputs=["5", "2", "8"], secrets=[])
    wizard.main()
    assert calls == ["oauth"]
    assert get_config(tmp_path / "config.json").output_mode == "youtube"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "https://example.com"),
        ("https://example.com/", "https://example.com"),
        ("http://example.com/kick/webhook", "http://example.com"),
        ("https://example.com/kick/webhook", "https://example.com"),
        ("", None),
        ("not a url at all", None),
        ("ftp://example.com", None),
    ],
)
def test_normalize_public_url_accepts_bare_hostnames(raw, expected):
    """A bare hostname gets https://, and unusable text reads as None."""
    assert wizard._normalize_public_url(raw) == expected


def test_local_timezone_prefers_tz_then_localtime(monkeypatch):
    """The machine zone needs no prompt: TZ wins, then /etc/localtime."""
    import os

    monkeypatch.setenv("TZ", "America/New_York")
    assert wizard._local_timezone() == "America/New_York"
    monkeypatch.setenv("TZ", "not-a-zone")
    monkeypatch.setattr(os, "readlink", lambda _path: "/usr/share/zoneinfo/Europe/Berlin")
    assert wizard._local_timezone() == "Europe/Berlin"
    monkeypatch.delenv("TZ")
    monkeypatch.setattr(os, "readlink", lambda _path: "/etc/localtime")
    assert wizard._local_timezone() == "UTC"
    monkeypatch.setattr(os, "readlink", _raise_missing_link)
    assert wizard._local_timezone() == "UTC"


def _raise_missing_link(_path):  # type: ignore[no-untyped-def]
    msg = "no such file"
    raise OSError(msg)


@pytest.mark.parametrize(
    ("host", "docker", "expected"),
    [
        ("127.0.0.1", True, "0.0.0.0"),
        ("127.0.0.1", False, "127.0.0.1"),
        ("0.0.0.0", True, "0.0.0.0"),
    ],
)
def test_listen_host_default_suggests_wildcard_in_containers(monkeypatch, host, docker, expected):
    """A container that still binds loopback gets no proxy traffic: suggest 0.0.0.0, never forced."""
    import os

    monkeypatch.setattr(os.path, "exists", lambda _path: docker)
    assert wizard._listen_host_default(host) == expected


def test_remote_step_tailnet_saves_url(monkeypatch, tmp_path):
    """The tailnet pick prints the serve command and saves the pasted address."""
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["3", "y", "1", "", "", "https://box.tailnet.ts.net", "8"], secrets=[])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.endpoint.enabled is True
    assert config.endpoint.public_url == "https://box.tailnet.ts.net"


def test_remote_step_internet_saves_url(monkeypatch, tmp_path):
    """The internet pick saves a bare hostname as https."""
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["3", "y", "2", "", "", "test.com", "8"], secrets=[])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.endpoint.enabled is True
    assert config.endpoint.public_url == "https://test.com"


def test_remote_step_blank_host_defaults_to_wildcard_in_containers(monkeypatch, tmp_path):
    """A blank host in a container stores 0.0.0.0."""
    import os

    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os.path, "exists", lambda _path: True)
    _script(monkeypatch, inputs=["3", "y", "2", "", "", "test.com", "8"], secrets=[])
    wizard.main()
    assert get_config(tmp_path / "config.json").endpoint.listen_host == "0.0.0.0"


def test_kick_step_stores_separate_url(monkeypatch, tmp_path, capsys):
    """Kick channels get their own public entry, independent of the endpoint."""
    _write_config(tmp_path, channels=["kick:slug"])
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["4", "y", "3", "kick.example.com", "n", "8"], secrets=[])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.kick.webhook.public_url == "https://kick.example.com"
    assert config.endpoint.public_url == ""
    assert config.kick.webhook.setup_notified is False
    assert config.kick.webhook.enabled is True
    out = capsys.readouterr().out
    assert "Kick webhook URL: https://kick.example.com/kick/webhook" in out
    assert "Enable webhooks" in out


def test_kick_step_follow_endpoint_clears_override(monkeypatch, tmp_path):
    """Following the endpoint clears the override and re-arms the confirmation."""
    _write_config(
        tmp_path,
        channels=["kick:slug"],
        kick={"webhook": {"enabled": False, "setup_notified": True, "public_url": "https://kick.example.com"}},
    )
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["4", "4", "8"], secrets=[])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.kick.webhook.public_url == ""
    assert config.kick.webhook.setup_notified is False
    assert config.kick.webhook.enabled is False


def test_remote_step_accepts_bare_hostname(monkeypatch, tmp_path):
    """The reported validation failure is gone: test.com saves as https."""
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["3", "y", "1", "", "", "test.com", "8"], secrets=[])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.endpoint.enabled is True
    assert config.endpoint.public_url == "https://test.com"


def test_remote_step_skips_public_entry_without_kick(monkeypatch, tmp_path):
    """Twitch-only setups stay local: no URL or tunnel prompts follow a no."""
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["3", "n", "8"], secrets=[])
    wizard.main()
    assert get_config(tmp_path / "config.json").endpoint.enabled is False


def test_kick_step_declined_leaves_config_alone(monkeypatch, tmp_path, capsys):
    """Ctrl+C abandons the step and shows the menu instead of exiting."""
    before = _write_config(tmp_path, kick={"client_id": "", "client_secret": ""})
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def answer(prompt=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return "4"
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return "8"

    monkeypatch.setattr("builtins.input", answer)
    wizard.main()
    after = get_config(tmp_path / "config.json")
    assert after.channels == before.channels
    assert after.kick.client_id == ""
    assert after.kick.webhook.public_url == ""
    assert "Back to the menu." in capsys.readouterr().out


def test_ctrl_c_at_menu_closes_setup(monkeypatch, tmp_path, capsys):
    """Ctrl+C at the menu prompt closes the program instead of looping."""
    before = _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def answer(prompt=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        msg = "menu asked again after Ctrl+C"
        raise AssertionError(msg)

    monkeypatch.setattr("builtins.input", answer)
    wizard.main()
    after = get_config(tmp_path / "config.json")
    assert after.channels == before.channels
    assert "Setup closed." in capsys.readouterr().out


def test_kick_step_without_entry_stays_on_polling(monkeypatch, tmp_path):
    """Declining the public entry still saves credentials for polling.

    Without an entry Kick works through polling (slower signals, no
    chat), so the decline must not drop the credentials.
    """
    _write_config(tmp_path, kick={"client_id": "", "client_secret": ""})
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["4", "kid123", "n", "8"], secrets=["ksecret123"])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.kick.client_id == "kid123"
    assert config.kick.client_secret == "ksecret123"
    assert config.kick.webhook.public_url == ""
    assert config.kick.webhook.enabled is False


def test_kick_step_follow_without_panel_entry_warns(capsys, monkeypatch, tmp_path):
    """Following a panel with no public address says Kick stays on polling.

    The choice is still saved (it clears any override), but the step
    must not imply that deliveries work.
    """
    _write_config(tmp_path, kick={"client_id": "", "client_secret": ""})
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["4", "kid123", "y", "4", "8"], secrets=["ksecret123"])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.kick.webhook.public_url == ""
    assert config.kick.client_id == "kid123"
    assert config.kick.webhook.enabled is False
    assert "stays on polling" in capsys.readouterr().out


def test_kick_step_rerun_enables_saved_entry(monkeypatch, tmp_path):
    """Rerunning the step repairs an entry saved while the step left it off.

    Older runs stored the entry without enabling deliveries, so keeping
    the same choice must flip the webhook on, not no-op on no diff.
    """
    _write_config(
        tmp_path,
        channels=["kick:slug"],
        kick={"webhook": {"enabled": False, "public_url": "https://kick.example.com"}},
    )
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["4", "3", "", "n", "8"], secrets=[])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.kick.webhook.public_url == "https://kick.example.com"
    assert config.kick.webhook.enabled is True


def test_kick_step_cloudflared_generates_ingress(monkeypatch, tmp_path):
    """The cloudflared pick writes an ingress file and saves the hostname."""
    _write_config(tmp_path, kick={"client_id": "cid", "client_secret": "csec"})
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["4", "y", "1", "kick.example.com", "n", "8"], secrets=[""])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.kick.webhook.public_url == "https://kick.example.com"
    assert config.kick.webhook.enabled is True
    ingress = tmp_path / "cloudflared" / "tunnel.yml"
    assert ingress.exists()
    text = ingress.read_text()
    assert "hostname: kick.example.com" in text
    assert "http://127.0.0.1:8788" in text


def test_kick_step_nginx_generates_config(monkeypatch, tmp_path):
    """The nginx pick writes a server block with the webhook and API locations."""
    _write_config(tmp_path, kick={"client_id": "cid", "client_secret": "csec"})
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["4", "y", "2", "kick.example.com", "y", "n", "8"], secrets=[])
    wizard.main()
    config = get_config(tmp_path / "config.json")
    assert config.kick.webhook.public_url == "https://kick.example.com"
    assert config.kick.webhook.enabled is True
    text = (tmp_path / "nginx" / "kick.example.com.conf").read_text()
    assert "location = /kick/webhook" in text
    assert "location ^~ /api/v1/" in text


def test_reset_wipes_config_and_starts_over(monkeypatch, tmp_path):
    """Reset keeps a backup and runs the first-time flow again."""
    old = _write_config(tmp_path, channels=["twitch:old"])
    monkeypatch.chdir(tmp_path)
    _script(
        monkeypatch,
        inputs=["7", "y", "tid123", "1", "8"],
        secrets=["tsecret123", "long-enough-password", "long-enough-password"],
    )
    wizard.main()
    assert (tmp_path / "config.json.bak").exists()
    config = get_config(tmp_path / "config.json")
    assert config.channels == []
    assert old.channels == ["twitch:old"]


def test_fresh_run_refuses_unwritable_dir(monkeypatch, tmp_path):
    """A root-owned data dir fails before the first prompt, with the fix."""
    work = tmp_path / "data"
    work.mkdir()
    work.chmod(0o555)
    monkeypatch.chdir(work)
    _script(monkeypatch, inputs=[], secrets=[])
    with pytest.raises(SystemExit):
        wizard.main()
    assert not (work / "config.json").exists()


def test_kick_entry_test_needs_the_app_running(monkeypatch, tmp_path, capsys):
    """The test prompt without a listener says to start the app, not hanging."""
    _write_config(tmp_path, endpoint={"listen_port": 47999})
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, inputs=["4", "y", "3", "kick.example.com", "y", "8"], secrets=[])
    wizard.main()
    assert "Start the app first" in capsys.readouterr().out


def test_kick_entry_test_reports_delivery(monkeypatch, tmp_path, capsys):
    """A running app answers the test call: the wizard prints its verdict."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.server.requests.append((self.path, self.headers.get("Authorization"), length))
            body = _json.dumps({"ok": True, "message": "first delivery in 3s"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        _write_config(tmp_path, endpoint={"listen_port": port}, api={"enabled": True, "key": "k"})
        monkeypatch.chdir(tmp_path)
        _script(monkeypatch, inputs=["4", "y", "3", "kick.example.com", "y", "8"], secrets=[])
        wizard.main()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    out = capsys.readouterr().out
    assert "Delivery works: first delivery in 3s" in out
    assert server.requests == [("/api/v1/kick/webhook/test", "Bearer k", 2)]


def test_verify_needs_credentials_first(tmp_path, capsys):
    """Without Kick credentials the direct call stops before any network."""
    config = _write_config(tmp_path, kick={"client_id": "", "client_secret": ""})
    wizard._verify_kick_delivery(config)
    assert "Save Kick credentials first" in capsys.readouterr().out
