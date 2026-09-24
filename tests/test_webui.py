"""Tests for the browser control panel at the domain root.

The panel shares the listener with the Kick webhook, so these tests drive
the real aiohttp app with TestClient, like the webhook and control API
tests do. The Telegram bot stays disabled here: user id 0 and an empty
token prove the panel works without Telegram tokens.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
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
    resp = await client.post("/api/login", json={"password": password})
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
            page = await client.get("/")
            api = await client.get("/api/status")
            return page.status, api.status

    assert asyncio.run(scenario()) == (404, 404)


def test_setup_required_without_password(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path, password=False)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            session = await (await client.get("/api/session")).json()
            login_resp = await client.post("/api/login", json={"password": PW})
            return session, login_resp.status

    session, status = asyncio.run(scenario())
    assert session == {"authenticated": False, "setup_required": True}
    assert status == 503


def test_index_shows_only_login_when_logged_out(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            anon = await client.get("/")
            anon_body = await anon.text()
            await login(client)
            authed = await client.get("/")
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
            resp = await client.get("/login.js")
            return resp.status, resp.headers.get("Content-Type"), await resp.text()

    status, content_type, body = asyncio.run(scenario())
    assert status == 200
    assert "javascript" in content_type
    assert "login-form" in body


def test_root_serves_panel(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.get("/")
            return resp.status, await resp.text()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert "Login" in body


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
            stream = await client.get("/api/recordings/stream?id=twitch/channel1/live.mp4")
            delete = await client.delete("/api/recordings?id=twitch/channel1/live.mp4", headers={"X-CSRF-Token": csrf})
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
            resp = await client.delete("/api/recordings?id=twitch/channel1/stuck.mp4", headers={"X-CSRF-Token": csrf})
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 500
    assert "recording now" not in body["error"]


def test_status_reports_disk_cap(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            return await (await client.get("/api/status")).json()

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
            bad_number = await client.patch("/api/settings", json={"retention_days": "soon"}, headers=headers)
            bad_switch = await client.patch("/api/settings", json={"record_chat": "yes"}, headers=headers)
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
            return await (await client.get("/api/recordings")).json()

    body = asyncio.run(scenario())
    names = [r["name"] for r in body["recordings"]]
    assert names == ["show.mp4"]


def test_plain_http_login_cookie_is_not_secure(tmp_path):
    """Secure on plain HTTP would never come back: LAN logins would loop."""
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/api/login", json={"password": PW}, headers={"Host": "192.168.1.5"})
            cookie = resp.headers.get("Set-Cookie", "")
            status = await (await client.get("/api/status")).json()
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
            return await (await client.get("/api/events")).json()

    body = asyncio.run(api())
    assert [e["kind"] for e in body["events"]] == ["live"]
    assert "\n" not in body["events"][0]["text"]
    events_mod.reset()


def test_clear_events_empties_feed_and_file(tmp_path):
    from stream_archive import events as events_mod

    events_mod.reset()
    events_mod.record("live", "twitch:channel1", "Title")
    config, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            csrf = await login(client)
            headers = {"X-CSRF-Token": csrf}
            bare = await client.delete("/api/events")
            assert bare.status == 403
            gone = await client.delete("/api/events", headers=headers)
            assert gone.status == 200
            return await (await client.get("/api/events")).json()

    body = asyncio.run(scenario())
    assert body["events"] == []
    assert events_mod.list_events() == []
    events_mod.reset()


def test_login_status_logout_flow(tmp_path):
    _, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            before = await (await client.get("/api/status")).json()
            csrf = await login(client)
            assert csrf
            status = await (await client.get("/api/status")).json()
            out = await client.post("/api/logout", headers={"X-CSRF-Token": csrf})
            after = await client.get("/api/status")
            # A second logout without a CSRF token is rejected, not applied.
            await login(client)
            bare = await client.post("/api/logout")
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
            missing = await client.patch("/api/settings", json={"retention_days": 5})
            wrong = await client.patch("/api/settings", json={"retention_days": 5}, headers={"X-CSRF-Token": "nope"})
            ok = await client.patch("/api/settings", json={"retention_days": 5}, headers={"X-CSRF-Token": csrf})
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
                resp = await client.post("/api/login", json={"password": "wrong-password-12"})
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
            listed = await (await client.get("/api/recordings")).json()
            full = await client.get("/api/recordings/stream?id=twitch/channel1/show.mp4")
            full_body = await full.read()
            ranged = await client.get(
                "/api/recordings/stream?id=twitch/channel1/show.mp4", headers={"Range": "bytes=0-15"}
            )
            ranged_body = await ranged.read()
            escape = await client.get("/api/recordings/stream?id=../config.json")
            gone = await client.delete("/api/recordings?id=twitch/channel1/show.mp4", headers=headers)
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
            listed = await (await client.get("/api/recordings")).json()
            stream = await client.get("/api/recordings/stream?id=twitch/channel1/live.mp4")
            download = await client.get("/api/recordings/stream?id=twitch/channel1/live.mp4&download=1")
            delete = await client.delete("/api/recordings?id=twitch/channel1/live.mp4", headers=headers)
            return listed, stream.status, download.status, delete.status

    listed, stream, download, delete = asyncio.run(scenario())
    assert listed["recordings"][0]["live"] is True
    assert (stream, download, delete) == (409, 409, 409)
    assert target.exists()


def test_security_headers_and_no_secrets(tmp_path):
    config, _, _, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/api/login", json={"password": PW})
            body = await resp.text()
            settings = await (await client.get("/api/settings")).text()
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
            added = await client.post("/api/channels", json={"channel": "twitch:newch"}, headers=headers)
            patched = await client.patch("/api/channels/twitch:newch", json={"quality": "720p"}, headers=headers)
            removed = await client.delete("/api/channels/twitch:newch", headers=headers)
            reloaded = await client.post("/api/reload", headers=headers)
            restarted = await client.post("/api/restart", headers=headers)
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
            ok = await client.get("/api/chat?id=twitch/channel1/show.mp4")
            missing = await client.get("/api/chat?id=twitch/channel1/none.mp4")
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


class _StubResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            msg = f"status {self._status}"
            raise RuntimeError(msg)

    def json(self):
        return self._payload


class StubHttp:
    """Fake httpx client: canned JSON per URL, or failure on every call."""

    def __init__(self, payloads=None, fail=False):
        self.payloads = payloads or {}
        self.fail = fail
        self.calls: list[str] = []

    async def get(self, url):
        self.calls.append(url)
        if self.fail:
            msg = "boom"
            raise RuntimeError(msg)
        if url in self.payloads:
            return _StubResp(self.payloads[url])
        return _StubResp(None, status=404)


def _write_chat(config, name, comments, streamer_id=87629696):
    import json as _json

    base = rec_dir(config)
    (base / f"{name}.mp4").write_bytes(b"v" * 10)
    chat_base = config.workdir / "chat" / "twitch" / "channel1"
    chat_base.mkdir(parents=True, exist_ok=True)
    (chat_base / f"{name}.chat.json").write_text(_json.dumps({"streamer": {"id": streamer_id}, "comments": comments}))


def _chat_comment(body, user="alice", fragments=None):
    return {
        "content_offset_seconds": 1.0,
        "channel_id": "87629696",
        "commenter": {"display_name": user},
        "message": {"body": body, "fragments": fragments or [{"text": body}]},
    }


def test_chat_renders_twitch_emotes(tmp_path):
    config, _, _, webui, wh = make_webui(tmp_path)
    webui._http = StubHttp(fail=True)  # Twitch spans need no network
    _write_chat(
        config,
        "emo",
        [
            _chat_comment(
                "smooth305OMG",
                user="Anbaki",
                fragments=[
                    {"text": "smooth305OMG", "emoticon": {"emoticon_id": "emotesv2_272cdedc96e34baf925ddcba142cbf7a"}}
                ],
            )
        ],
    )

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            return await (await client.get("/api/chat?id=twitch/channel1/emo.mp4")).json()

    body = asyncio.run(scenario())
    (msg,) = body["messages"]
    assert msg["text"] == "smooth305OMG"
    assert msg["emotes"] == [
        {
            "start": 0,
            "end": 12,
            "src": "https://static-cdn.jtvnw.net/emoticons/v2/emotesv2_272cdedc96e34baf925ddcba142cbf7a/default/dark/1.0",
        }
    ]


def test_chat_renders_seventv_emote(tmp_path):
    config, _, _, webui, wh = make_webui(tmp_path)
    webui._http = StubHttp(
        payloads={
            "https://7tv.io/v3/users/twitch/87629696": {
                "emote_set": {"emotes": [{"id": "01FP6SPEB00001BCZZ99DVK9W5", "name": "baseg"}]}
            }
        }
    )
    _write_chat(config, "emo", [_chat_comment("baseg", user="StarsTony")])

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            chat = await (await client.get("/api/chat?id=twitch/channel1/emo.mp4")).json()
            index = await client.get("/")
            return chat, index.headers.get("Content-Security-Policy", "")

    body, csp = asyncio.run(scenario())
    (msg,) = body["messages"]
    assert msg["text"] == "baseg"
    assert msg["emotes"] == [
        {"start": 0, "end": 5, "src": "https://cdn.7tv.app/emote/01FP6SPEB00001BCZZ99DVK9W5/1x.webp"}
    ]
    assert "cdn.7tv.app" in csp


def test_chat_emote_lookup_failure_keeps_plain_text(tmp_path):
    config, _, _, webui, wh = make_webui(tmp_path)
    webui._http = StubHttp(fail=True)
    _write_chat(config, "emo", [_chat_comment("baseg", user="StarsTony")])

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            return await (await client.get("/api/chat?id=twitch/channel1/emo.mp4")).json()

    body = asyncio.run(scenario())
    (msg,) = body["messages"]
    assert msg["text"] == "baseg"
    assert "emotes" not in msg


def test_chat_renders_kick_emotes_without_lookup(tmp_path):
    import json as _json

    config, _, _, webui, wh = make_webui(tmp_path)
    webui._http = StubHttp(fail=True)
    base = config.workdir / "recordings" / "kick" / "slug"
    base.mkdir(parents=True, exist_ok=True)
    (base / "show.mp4").write_bytes(b"v" * 10)
    chat_base = config.workdir / "chat" / "kick" / "slug"
    chat_base.mkdir(parents=True, exist_ok=True)
    (chat_base / "show.chat.json").write_text(
        _json.dumps(
            {
                "comments": [
                    {
                        "content_offset_seconds": 1.0,
                        "channel_id": "123",
                        "commenter": {"display_name": "bob"},
                        "message": {
                            "body": "[emote:123:KEKW] hi",
                            "fragments": [
                                {"text": "[emote:123:KEKW]", "emoticon": {"emoticon_id": "123"}},
                                {"text": " hi"},
                            ],
                        },
                    }
                ]
            }
        )
    )

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            return await (await client.get("/api/chat?id=kick/slug/show.mp4")).json()

    body = asyncio.run(scenario())
    (msg,) = body["messages"]
    assert msg["text"] == "[emote:123:KEKW] hi"
    assert msg["emotes"] == [{"start": 0, "end": 16, "src": "https://files.kick.com/emotes/123/fullsize"}]
    assert webui._http.calls == []


def test_chat_emote_with_punctuation_resolves(tmp_path):
    config, _, _, webui, wh = make_webui(tmp_path)
    webui._http = StubHttp(
        payloads={
            "https://7tv.io/v3/users/twitch/87629696": {
                "emote_set": {"emotes": [{"id": "01FP6SPEB00001BCZZ99DVK9W5", "name": "baseg"}]}
            }
        }
    )
    _write_chat(config, "emo", [_chat_comment("baseg! really", user="StarsTony")])

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            return await (await client.get("/api/chat?id=twitch/channel1/emo.mp4")).json()

    body = asyncio.run(scenario())
    (msg,) = body["messages"]
    assert msg["text"] == "baseg! really"
    assert msg["emotes"] == [
        {"start": 0, "end": 5, "src": "https://cdn.7tv.app/emote/01FP6SPEB00001BCZZ99DVK9W5/1x.webp"}
    ]


def test_chat_prefers_embedded_images(tmp_path):
    import base64 as _b64
    import json as _json

    config, _, _, webui, wh = make_webui(tmp_path)
    webui._http = StubHttp(fail=True)  # embedded needs no network
    raw = b"\x89PNG\r\n\x1a\n" + b"\0" * 10
    base = rec_dir(config)
    (base / "emo.mp4").write_bytes(b"v" * 10)
    chat_base = config.workdir / "chat" / "twitch" / "channel1"
    chat_base.mkdir(parents=True, exist_ok=True)
    (chat_base / "emo.chat.json").write_text(
        _json.dumps(
            {
                "comments": [
                    {
                        "content_offset_seconds": 1.0,
                        "channel_id": "87629696",
                        "commenter": {"display_name": "Anbaki"},
                        "message": {
                            "body": "smooth305OMG",
                            "fragments": [
                                {
                                    "text": "smooth305OMG",
                                    "emoticon": {"emoticon_id": "emotesv2_272cdedc96e34baf925ddcba142cbf7a"},
                                }
                            ],
                        },
                    }
                ],
                "embeddedData": {
                    "firstParty": [
                        {
                            "id": "emotesv2_272cdedc96e34baf925ddcba142cbf7a",
                            "imageScale": 2,
                            "data": _b64.b64encode(raw).decode(),
                            "name": "smooth305OMG",
                        }
                    ]
                },
            }
        )
    )

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            return await (await client.get("/api/chat?id=twitch/channel1/emo.mp4")).json()

    body = asyncio.run(scenario())
    (msg,) = body["messages"]
    (emote,) = msg["emotes"]
    assert emote["start"] == 0 and emote["end"] == 12
    assert emote["src"].startswith("data:image/png;base64,")


def test_first_boot_stores_session_secret(tmp_path):
    """The first boot with the panel on writes a lasting session secret."""
    make_webui(tmp_path)
    stored = json.loads((tmp_path / "config.json").read_text())["web"]["session_secret"]
    assert isinstance(stored, str) and len(stored) >= 32


def test_login_survives_restart(tmp_path):
    """A login stays valid when the app restarts: new process, same data dir."""
    _, ctrl, recorder, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/api/login", json={"password": PW})
            assert resp.status == 200
            return resp.headers["Set-Cookie"].split(";", 1)[0], (await resp.json())["csrf"]

    cookie, csrf = asyncio.run(scenario())
    # A restart: a fresh config object and a fresh panel on the same data dir.
    fresh = get_config(tmp_path / "config.json")
    wh2 = KickWebhook(fresh, None, None, None, None)
    WebUI(fresh, ctrl, recorder).register_routes(wh2)

    async def check():
        async with TestClient(TestServer(wh2._app)) as client:
            resp = await client.get("/api/session", headers={"Cookie": cookie})
            return await resp.json()

    assert asyncio.run(check()) == {"authenticated": True, "setup_required": False, "csrf": csrf}


def test_expired_session_stays_logged_out_after_restart(tmp_path):
    """An expired session does not come back after a restart."""
    _, ctrl, recorder, webui, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/api/login", json={"password": PW})
            assert resp.status == 200
            return resp.headers["Set-Cookie"].split(";", 1)[0]

    cookie = asyncio.run(scenario())
    sid = cookie.split("=", 1)[1].split(".", 1)[0]
    entry = webui._sessions[sid]
    stored = {sid: {"csrf": entry.csrf, "expires": time.time() - 1, "pwd": entry.pwd}}
    (tmp_path / "web_sessions.json").write_text(json.dumps(stored))
    fresh = get_config(tmp_path / "config.json")
    wh2 = KickWebhook(fresh, None, None, None, None)
    WebUI(fresh, ctrl, recorder).register_routes(wh2)

    async def check():
        async with TestClient(TestServer(wh2._app)) as client:
            resp = await client.get("/api/session", headers={"Cookie": cookie})
            return await resp.json()

    assert asyncio.run(check()) == {"authenticated": False, "setup_required": False}


def test_logout_ends_session_after_restart(tmp_path):
    """A logout removes the stored session, so a restart stays logged out."""
    _, ctrl, recorder, _, wh = make_webui(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/api/login", json={"password": PW})
            assert resp.status == 200
            cookie = resp.headers["Set-Cookie"].split(";", 1)[0]
            csrf = (await resp.json())["csrf"]
            out = await client.post("/api/logout", headers={"X-CSRF-Token": csrf})
            assert out.status == 200
            return cookie

    cookie = asyncio.run(scenario())
    fresh = get_config(tmp_path / "config.json")
    wh2 = KickWebhook(fresh, None, None, None, None)
    WebUI(fresh, ctrl, recorder).register_routes(wh2)

    async def check():
        async with TestClient(TestServer(wh2._app)) as client:
            resp = await client.get("/api/session", headers={"Cookie": cookie})
            return await resp.json()

    assert asyncio.run(check()) == {"authenticated": False, "setup_required": False}


def test_corrupt_sessions_file_starts_empty(tmp_path):
    """A damaged sessions file logs no one in and breaks no boot."""
    _, ctrl, recorder, _, _ = make_webui(tmp_path)
    (tmp_path / "web_sessions.json").write_text("not json{{{")
    fresh = get_config(tmp_path / "config.json")
    assert WebUI(fresh, ctrl, recorder)._sessions == {}
