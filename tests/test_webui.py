"""Tests for the /web/ browser control panel.

The panel shares the listener with the Kick webhook, so these tests drive
the real aiohttp app with TestClient, like the webhook and control API
tests do. The Telegram bot stays disabled here: user id 0 and an empty
token prove the panel works without Telegram tokens.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from conftest import make_config as valid_config

from stream_archive.config import get_config
from stream_archive.kick_webhook import KickWebhook
from stream_archive.telegram import TelegramController
from stream_archive.webui import WebUI, hash_password, verify_password

PW = "correct-horse-battery-12"


class FakeRecorder:
    def __init__(self, recording=()):
        self._recording = set(recording)
        self.stop_calls: list[str] = []

    def active_channels(self):
        return sorted(self._recording)

    def is_recording(self, channel):
        return channel in self._recording

    async def disk_snapshot(self):
        return {"usage_ok": True, "free_gb": 10.0, "total_fs_gb": 20.0, "archive_gb": 1.0}

    def recording_info(self):
        return []

    def recording_settings(self):
        return {}

    def _active_paths(self):
        return set()

    def _remove_if_inactive(self, path, active):
        if os.path.realpath(path) in active:
            return None
        size = Path(path).stat().st_size
        Path(path).unlink(missing_ok=True)
        return size

    async def stop(self, channel):
        self.stop_calls.append(channel)
        self._recording.discard(channel)


class FakeMonitor:
    def __init__(self):
        self.remove_calls: list[str] = []

    def remove_channel(self, channel):
        self.remove_calls.append(channel)


class FakeEventSub:
    def __init__(self):
        self.added: list[str] = []
        self.removed: list[str] = []

    async def add_channel(self, channel):
        self.added.append(channel)

    async def remove_channel(self, channel):
        self.removed.append(channel)

    async def sync_channels(self, channels):
        return None


class FakeKickWebhook:
    def __init__(self):
        self.added: list[str] = []
        self.removed: list[str] = []

    async def apply_state(self):
        return None

    async def add_channel(self, channel):
        self.added.append(channel)

    async def remove_channel(self, channel):
        self.removed.append(channel)

    async def sync_channels(self, channels):
        return None


def make_webui(tmp_path, *, web_enabled=True, password=True, channels=("twitch:channel1",)):
    """Controller (Telegram disabled) plus panel on a real listener app."""
    data = valid_config(
        channels=list(channels),
        telegram_user_id=0,
        bot_telegram_api="",
        web={"enabled": web_enabled, "password_hash": hash_password(PW) if password else ""},
        kick={"client_id": "client_id", "client_secret": "client_secret"},
    ).model_dump(mode="json", exclude_unset=True)
    (tmp_path / "config.json").write_text(json.dumps(data))
    config = get_config(tmp_path / "config.json")
    recorder = FakeRecorder()
    monitor = FakeMonitor()
    eventsub = FakeEventSub()
    ctrl = TelegramController(config, recorder, monitor, eventsub, kick_webhook=FakeKickWebhook())
    assert ctrl.enabled is False
    wh = KickWebhook(config, None, None, None, None)
    webui = WebUI(config, ctrl, recorder)
    webui.register_routes(wh)
    return config, ctrl, recorder, webui, wh


def rec_dir(config):
    base = config.workdir / "recordings" / "twitch" / "channel1"
    base.mkdir(parents=True, exist_ok=True)
    return base


async def login(client, password=PW):
    resp = await client.post("/web/api/login", json={"password": password})
    body = await resp.json()
    assert resp.status == 200, body
    return body["csrf"]


def test_password_hash_roundtrip():
    hashed = hash_password(PW)
    assert "$" in hashed
    assert verify_password(PW, hashed) is True
    assert verify_password("wrong-password-12", hashed) is False
    assert verify_password(PW, "garbage") is False
    assert verify_password(PW, "pbkdf2-sha256$bad$salt$hash") is False


def test_disabled_web_answers_404(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path, web_enabled=False)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            page = await client.get("/web/")
            api = await client.get("/web/api/status")
            return page.status, api.status

    assert asyncio.run(scenario()) == (404, 404)


def test_setup_required_without_password(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path, password=False)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            session = await (await client.get("/web/api/session")).json()
            login_resp = await client.post("/web/api/login", json={"password": PW})
            return session, login_resp.status

    session, status = asyncio.run(scenario())
    assert session == {"authenticated": False, "setup_required": True}
    assert status == 503


def test_index_shows_only_login_when_logged_out(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            anon = await client.get("/web/")
            anon_body = await anon.text()
            await login(client)
            authed = await client.get("/web/")
            authed_body = await authed.text()
            return anon.status, anon_body, authed.status, authed_body

    anon_status, anon_body, authed_status, authed_body = asyncio.run(scenario())
    assert anon_status == 200
    assert "Login" in anon_body
    for marker in ("Reload config", "New password", "Recordings", 'id="nav"'):
        assert marker not in anon_body
    assert authed_status == 200
    assert "Reload config" in authed_body
    assert "login.js" not in authed_body


def test_login_js_served_publicly(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.get("/web/login.js")
            return resp.status, resp.headers.get("Content-Type"), await resp.text()

    status, content_type, body = asyncio.run(scenario())
    assert status == 200
    assert "javascript" in content_type
    assert "login-form" in body


def test_bare_web_path_redirects_to_slash(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.get("/web", allow_redirects=False)
            return resp.status, resp.headers.get("Location")

    status, location = asyncio.run(scenario())
    assert status in (301, 308)
    assert location == "/web/"


def test_live_probe_failure_blocks_stream_and_delete(tmp_path):
    """A recorder hiccup must fail closed, never serve a live file as done."""

    class ExplodingRecorder(FakeRecorder):
        def _active_paths(self):
            msg = "boom"
            raise RuntimeError(msg)

    data = valid_config(
        channels=["twitch:channel1"],
        telegram_user_id=0,
        bot_telegram_api="",
        web={"enabled": True, "password_hash": hash_password(PW)},
        kick={"client_id": "client_id", "client_secret": "client_secret"},
    ).model_dump(mode="json", exclude_unset=True)
    (tmp_path / "config.json").write_text(json.dumps(data))
    config = get_config(tmp_path / "config.json")
    recorder = ExplodingRecorder()
    ctrl = TelegramController(config, recorder, FakeMonitor(), FakeEventSub(), kick_webhook=FakeKickWebhook())
    wh = KickWebhook(config, None, None, None, None)
    WebUI(config, ctrl, recorder).register_routes(wh)
    base = rec_dir(config)
    (base / "live.mp4").write_bytes(b"x" * 100)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            csrf = await login(client)
            stream = await client.get("/web/api/recordings/stream?id=twitch/channel1/live.mp4")
            delete = await client.delete(
                "/web/api/recordings?id=twitch/channel1/live.mp4", headers={"X-CSRF-Token": csrf}
            )
            return stream.status, delete.status

    assert asyncio.run(scenario()) == (503, 503)


def test_delete_failure_is_not_reported_as_live(tmp_path):
    """An unreadable file answers 500, never 'is recording now'."""

    class StuckRecorder(FakeRecorder):
        def _remove_if_inactive(self, path, active):
            return None

    data = valid_config(
        channels=["twitch:channel1"],
        telegram_user_id=0,
        bot_telegram_api="",
        web={"enabled": True, "password_hash": hash_password(PW)},
        kick={"client_id": "client_id", "client_secret": "client_secret"},
    ).model_dump(mode="json", exclude_unset=True)
    (tmp_path / "config.json").write_text(json.dumps(data))
    config = get_config(tmp_path / "config.json")
    recorder = StuckRecorder()
    ctrl = TelegramController(config, recorder, FakeMonitor(), FakeEventSub(), kick_webhook=FakeKickWebhook())
    wh = KickWebhook(config, None, None, None, None)
    WebUI(config, ctrl, recorder).register_routes(wh)
    base = rec_dir(config)
    (base / "stuck.mp4").write_bytes(b"x" * 100)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            csrf = await login(client)
            resp = await client.delete(
                "/web/api/recordings?id=twitch/channel1/stuck.mp4", headers={"X-CSRF-Token": csrf}
            )
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 500
    assert "recording now" not in body["error"]


def test_status_reports_disk_cap(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            return await (await client.get("/web/api/status")).json()

    status = asyncio.run(scenario())
    assert status["disk"]["cap_gb"] == 0
    assert status["disk"]["archive_gb"] == 1.0


def test_patch_settings_bad_value_is_400_not_500(tmp_path):
    """Shared validators must answer 400 through the panel, like the API."""
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            csrf = await login(client)
            headers = {"X-CSRF-Token": csrf}
            bad_number = await client.patch("/web/api/settings", json={"retention_days": "soon"}, headers=headers)
            bad_switch = await client.patch("/web/api/settings", json={"record_chat": "yes"}, headers=headers)
            return bad_number.status, await bad_number.json(), bad_switch.status

    number_status, body, switch_status = asyncio.run(scenario())
    assert number_status == 400
    assert "retention_days" in body["errors"]
    assert switch_status == 400


def test_listing_hides_unserved_suffixes(tmp_path):
    config, _, _, _, wh = make_webui(tmp_path)
    base = rec_dir(config)
    (base / "hand.mkv").write_bytes(b"x" * 10)
    (base / "show.mp4").write_bytes(b"y" * 10)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            return await (await client.get("/web/api/recordings")).json()

    body = asyncio.run(scenario())
    names = [r["name"] for r in body["recordings"]]
    assert names == ["show.mp4"]


def test_plain_http_login_cookie_is_not_secure(tmp_path):
    """Secure on plain HTTP would never come back: LAN logins would loop."""
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/web/api/login", json={"password": PW}, headers={"Host": "192.168.1.5"})
            cookie = resp.headers.get("Set-Cookie", "")
            status = await (await client.get("/web/api/status")).json()
            return resp.status, cookie, status["version"]

    login_status, cookie, version = asyncio.run(scenario())
    assert login_status == 200
    assert "Secure" not in cookie
    assert version


def test_events_feed_records_notifier_alerts(tmp_path):
    from stream_archive import events as events_mod
    from stream_archive.notifier import NullNotifier

    events_mod.reset()
    notifier = NullNotifier()

    async def scenario():
        await notifier.notify_live("twitch:channel1", "Title", "Game", "https://twitch.tv/channel1")
        await notifier.notify_offline("twitch:channel1")
        await notifier.notify("disk is getting full")

    asyncio.run(scenario())
    listed = events_mod.list_events()
    assert [e["kind"] for e in listed] == ["notice", "offline", "live"]
    assert listed[0]["channel"] is None
    assert listed[1]["channel"] == "twitch:channel1"

    events_mod.reset()

    async def forged():
        await notifier.notify_live(
            "twitch:channel1", "Win\nOffline: twitch:other", "Game", "https://twitch.tv/channel1"
        )

    asyncio.run(forged())
    entry = events_mod.list_events()[0]
    assert "\n" not in entry["text"]  # one event renders as one visual line
    assert "Offline: twitch:other" in entry["text"]

    _, _, _, _, wh = make_webui(tmp_path)

    async def api():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            return await (await client.get("/web/api/events")).json()

    body = asyncio.run(api())
    assert [e["kind"] for e in body["events"]] == ["live"]
    assert "\n" not in body["events"][0]["text"]
    events_mod.reset()


def test_login_status_logout_flow(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            before = await (await client.get("/web/api/status")).json()
            csrf = await login(client)
            assert csrf
            status = await (await client.get("/web/api/status")).json()
            out = await client.post("/web/api/logout", headers={"X-CSRF-Token": csrf})
            after = await client.get("/web/api/status")
            # A second logout without a CSRF token is rejected, not applied.
            await login(client)
            bare = await client.post("/web/api/logout")
            return before, status["version"], out.status, after.status, bare.status

    before, version, logout_status, after, bare = asyncio.run(scenario())
    assert before == {"error": "unauthorized"}
    assert version
    assert logout_status == 200
    assert after == 401
    assert bare == 403


def test_write_needs_csrf(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            csrf = await login(client)
            missing = await client.patch("/web/api/settings", json={"retention_days": 5})
            wrong = await client.patch(
                "/web/api/settings", json={"retention_days": 5}, headers={"X-CSRF-Token": "nope"}
            )
            ok = await client.patch("/web/api/settings", json={"retention_days": 5}, headers={"X-CSRF-Token": csrf})
            return missing.status, wrong.status, ok.status, await ok.json()

    missing, wrong, ok, body = asyncio.run(scenario())
    assert (missing, wrong, ok) == (403, 403, 200)
    assert body["applied"]["retention_days"] == "Retention set to 5 day(s)"


def test_login_rate_limit(tmp_path, monkeypatch):
    import stream_archive.webui as webui_mod

    monkeypatch.setattr(webui_mod, "_LOGIN_FAIL_DELAY_S", 0)
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            codes = []
            for _ in range(11):
                resp = await client.post("/web/api/login", json={"password": "wrong-password-12"})
                codes.append(resp.status)
            return codes

    codes = asyncio.run(scenario())
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


def test_recordings_list_stream_range_delete(tmp_path):
    config, _, _, _, wh = make_webui(tmp_path)
    base = rec_dir(config)
    target = base / "show.mp4"
    target.write_bytes(b"0123456789abcdef" * 64)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            csrf = await login(client)
            headers = {"X-CSRF-Token": csrf}
            listed = await (await client.get("/web/api/recordings")).json()
            full = await client.get("/web/api/recordings/stream?id=twitch/channel1/show.mp4")
            full_body = await full.read()
            ranged = await client.get(
                "/web/api/recordings/stream?id=twitch/channel1/show.mp4", headers={"Range": "bytes=0-15"}
            )
            ranged_body = await ranged.read()
            escape = await client.get("/web/api/recordings/stream?id=../config.json")
            gone = await client.delete("/web/api/recordings?id=twitch/channel1/show.mp4", headers=headers)
            return listed, full, full_body, ranged, ranged_body, escape.status, gone.status

    listed, full, full_body, ranged, ranged_body, escape, gone = asyncio.run(scenario())
    assert listed["total"] == 1
    assert listed["recordings"][0]["name"] == "show.mp4"
    assert listed["recordings"][0]["playable"] is True
    assert full.status == 200
    assert full.headers["Content-Type"] == "video/mp4"
    assert full.headers["Accept-Ranges"] == "bytes"
    assert full_body == target.read_bytes() if target.exists() else full_body == b"0123456789abcdef" * 64
    assert ranged.status == 206
    assert ranged.headers["Content-Range"] == f"bytes 0-15/{16 * 64}"
    assert ranged_body == b"0123456789abcdef"
    assert escape == 400
    assert gone == 200
    assert not target.exists()


def test_live_stream_and_download_blocked(tmp_path):
    """A capture in flight is incomplete: no play, no download, no delete."""
    import os as _os

    config, _, recorder, _, wh = make_webui(tmp_path)
    base = rec_dir(config)
    target = base / "live.mp4"
    target.write_bytes(b"x" * 100)
    live = {_os.path.realpath(target)}
    recorder._active_paths = lambda: set(live)  # type: ignore[method-assign]

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            csrf = await login(client)
            headers = {"X-CSRF-Token": csrf}
            listed = await (await client.get("/web/api/recordings")).json()
            stream = await client.get("/web/api/recordings/stream?id=twitch/channel1/live.mp4")
            download = await client.get("/web/api/recordings/stream?id=twitch/channel1/live.mp4&download=1")
            delete = await client.delete("/web/api/recordings?id=twitch/channel1/live.mp4", headers=headers)
            return listed, stream.status, download.status, delete.status

    listed, stream, download, delete = asyncio.run(scenario())
    assert listed["recordings"][0]["live"] is True
    assert (stream, download, delete) == (409, 409, 409)
    assert target.exists()


def test_security_headers_and_no_secrets(tmp_path):
    config, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/web/api/login", json={"password": PW})
            body = await resp.text()
            settings = await (await client.get("/web/api/settings")).text()
            return resp.headers, body, settings

    headers, body, settings = asyncio.run(scenario())
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    for secret in (PW, "bot_token", "client_secret"):
        assert secret not in body
        assert secret not in settings


def test_patch_channels_and_reload_restart(tmp_path):
    config, ctrl, _, _, wh = make_webui(tmp_path, channels=("twitch:channel1",))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            csrf = await login(client)
            headers = {"X-CSRF-Token": csrf}
            added = await client.post("/web/api/channels", json={"channel": "twitch:newch"}, headers=headers)
            patched = await client.patch("/web/api/channels/twitch:newch", json={"quality": "720p"}, headers=headers)
            removed = await client.delete("/web/api/channels/twitch:newch", headers=headers)
            reloaded = await client.post("/web/api/reload", headers=headers)
            restarted = await client.post("/web/api/restart", headers=headers)
            return (
                added.status,
                patched.status,
                removed.status,
                (await reloaded.json())["message"],
                (await restarted.json())["message"],
            )

    added, patched, removed, reloaded, restarted = asyncio.run(scenario())
    assert (added, patched, removed) == (200, 200, 200)
    assert "reloaded" in reloaded.lower()
    # No shutdown callback in tests, so restart reports unavailability.
    assert "not available" in restarted
    assert list(config.channels) == ["twitch:channel1"]


def test_telegram_disabled_but_both_can_run(tmp_path):
    config, ctrl, _, _, _ = make_webui(tmp_path)
    assert ctrl.enabled is False
    assert config.telegram_user_id == 0
    asyncio.run(ctrl.start())  # no-op, must not raise
    asyncio.run(ctrl.stop())


def test_chat_endpoint_serves_recording_chat(tmp_path):
    import json as _json

    config, _, _, _, wh = make_webui(tmp_path)
    base = rec_dir(config)
    (base / "show.mp4").write_bytes(b"v" * 10)
    chat_base = config.workdir / "chat" / "twitch" / "channel1"
    chat_base.mkdir(parents=True, exist_ok=True)
    (chat_base / "show.chat.json").write_text(
        _json.dumps(
            {
                "comments": [
                    {
                        "content_offset_seconds": 12.5,
                        "commenter": {"display_name": "alice"},
                        "message": {"body": "hello"},
                    },
                    {
                        "content_offset_seconds": 3.0,
                        "commenter": {"name": "bob"},
                        "message": {"body": "first"},
                    },
                    {"content_offset_seconds": -1, "commenter": {}, "message": {}},
                ]
            }
        )
    )

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            ok = await client.get("/web/api/chat?id=twitch/channel1/show.mp4")
            missing = await client.get("/web/api/chat?id=twitch/channel1/none.mp4")
            return await ok.json(), ok.status, missing.status

    body, status, missing = asyncio.run(scenario())
    assert status == 200
    assert [(m["t"], m["user"], m["text"]) for m in body["messages"]] == [
        (3.0, "bob", "first"),
        (12.5, "alice", "hello"),
    ]
    assert body["truncated"] is False
    assert body["missing"] is False
    assert missing == 200
