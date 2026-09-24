"""Tests for the Telegram Web panel menu (Remote access -> Web panel).

The first enable generates the panel password and shows it once, like the
API key flow. Disabling keeps the hash. A new password ends browser
sessions at once through the session password tag.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import types
import unittest.mock

from aiohttp.test_utils import TestClient, TestServer
from conftest import make_config as valid_config

from stream_archive.config import get_config
from stream_archive.kick_webhook import KickWebhook
from stream_archive.telegram import TelegramController
from stream_archive.webui import WebUI, verify_password

ADMIN_ID = 12345


class FakeRecorder:
    def active_channels(self):
        return []

    def is_recording(self, channel):
        return False

    async def disk_snapshot(self):
        return {"usage_ok": True, "free_gb": 10.0, "total_fs_gb": 20.0, "archive_gb": 0.0}

    def recording_info(self):
        return []

    def recording_settings(self):
        return {}

    async def stop(self, channel):
        return None


class FakeMonitor:
    def remove_channel(self, channel):
        pass


class FakeEventSub:
    async def add_channel(self, channel):
        pass

    async def remove_channel(self, channel):
        pass

    async def sync_channels(self, channels):
        return None


class FakeKickWebhook:
    def __init__(self):
        self.applied: list[int] = []

    async def apply_state(self):
        self.applied.append(1)

    async def add_channel(self, channel):
        pass

    async def remove_channel(self, channel):
        pass

    async def sync_channels(self, channels):
        return None


def make_controller(tmp_path):
    data = valid_config(
        channels=["twitch:channel1"],
        kick={"client_id": "client_id", "client_secret": "client_secret"},
    ).model_dump(mode="json", exclude_unset=True)
    (tmp_path / "config.json").write_text(json.dumps(data))
    config = get_config(tmp_path / "config.json")
    ctrl = TelegramController(config, FakeRecorder(), FakeMonitor(), FakeEventSub(), kick_webhook=FakeKickWebhook())
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)
    return config, ctrl, bot


def read_file(tmp_path):
    return json.loads((tmp_path / "config.json").read_text())


def kb_labels(markup):
    data = markup.to_dict()
    rows = data.get("inline_keyboard") or data.get("keyboard")
    return [b["text"] for row in rows for b in row]


def web_labels(enabled):
    return [f"{'Disable' if enabled else 'Enable'} Web panel", "New password", "Back"]


def last_sent(bot):
    kwargs = bot.send_message.await_args.kwargs
    return kwargs["text"], kwargs.get("parse_mode"), kwargs["reply_markup"]


def password_of(sent):
    match = re.search(r"<code>(.*?)</code>", sent)
    assert match is not None, f"no password code span in {sent!r}"
    return html.unescape(match.group(1))


def open_web_menu(ctrl):
    asyncio.run(ctrl.handle_reply_text("Settings"))
    asyncio.run(ctrl.handle_reply_text("Remote access"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Web panel"))
    assert ctrl._state_for(ADMIN_ID).menu == "web"
    return text, markup


def test_web_menu_shows_state_and_keyboard(tmp_path):
    _, ctrl, _ = make_controller(tmp_path)
    text, markup = open_web_menu(ctrl)
    assert "Web panel: off" in text
    assert kb_labels(markup) == web_labels(False)


def test_enable_generates_password_once(tmp_path):
    config, ctrl, bot = make_controller(tmp_path)
    open_web_menu(ctrl)
    assert asyncio.run(ctrl.handle_reply_text("Enable Web panel")) is None
    sent, parse_mode, markup = last_sent(bot)
    assert parse_mode == "HTML"
    assert "Web panel enabled" in html.unescape(sent)
    password = password_of(sent)
    assert len(password) >= 12
    assert read_file(tmp_path)["web"]["enabled"] is True
    assert verify_password(password, read_file(tmp_path)["web"]["password_hash"]) is True
    assert kb_labels(markup) == web_labels(True)
    assert ctrl._kick_webhook.applied == [1]


def test_panel_login_works_with_bot_password(tmp_path):
    config, ctrl, bot = make_controller(tmp_path)
    open_web_menu(ctrl)
    asyncio.run(ctrl.handle_reply_text("Enable Web panel"))
    sent, _, _ = last_sent(bot)
    password = password_of(sent)
    wh = KickWebhook(config, None, None, None, None)
    WebUI(config, ctrl, FakeRecorder()).register_routes(wh)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/api/login", json={"password": password})
            status = await (await client.get("/api/status")).json()
            return resp.status, status["telegram_enabled"]

    login_status, telegram_on = asyncio.run(scenario())
    assert login_status == 200
    assert telegram_on is True


def test_disable_keeps_password_hash(tmp_path):
    config, ctrl, bot = make_controller(tmp_path)
    open_web_menu(ctrl)
    asyncio.run(ctrl.handle_reply_text("Enable Web panel"))
    hashed = read_file(tmp_path)["web"]["password_hash"]
    assert asyncio.run(ctrl.handle_reply_text("Disable Web panel")) is None
    sent, _, markup = last_sent(bot)
    assert "Web panel disabled" in html.unescape(sent)
    assert "<code>" not in sent  # a disable never shows the password
    assert read_file(tmp_path)["web"]["enabled"] is False
    assert read_file(tmp_path)["web"]["password_hash"] == hashed
    assert ctrl._kick_webhook.applied == [1, 1]
    assert kb_labels(markup) == web_labels(False)


def test_new_password_ends_browser_sessions(tmp_path):
    config, ctrl, bot = make_controller(tmp_path)
    open_web_menu(ctrl)
    asyncio.run(ctrl.handle_reply_text("Enable Web panel"))
    first = password_of(last_sent(bot)[0])
    wh = KickWebhook(config, None, None, None, None)
    WebUI(config, ctrl, FakeRecorder()).register_routes(wh)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            login_resp = await client.post("/api/login", json={"password": first})
            assert login_resp.status == 200
            before = (await client.get("/api/status")).status
            rotated = await ctrl._new_web_password()
            second = password_of(rotated)
            assert second != first
            after = (await client.get("/api/status")).status
            old = (await client.post("/api/login", json={"password": first})).status
            new = (await client.post("/api/login", json={"password": second})).status
            return before, after, old, new, second

    before, after, old, new, second = asyncio.run(scenario())
    assert (before, after, old, new) == (200, 401, 401, 200)
    assert verify_password(second, read_file(tmp_path)["web"]["password_hash"]) is True


def test_group_new_password_rotates_nothing(tmp_path):
    config, ctrl, bot = make_controller(tmp_path)
    open_web_menu(ctrl)
    asyncio.run(ctrl.handle_reply_text("Enable Web panel"))
    hashed = read_file(tmp_path)["web"]["password_hash"]
    text = asyncio.run(ctrl._new_web_password(chat_id=99999))
    assert "private chat" in text
    assert read_file(tmp_path)["web"]["password_hash"] == hashed


def test_group_chat_never_sees_the_password(tmp_path):
    _, ctrl, bot = make_controller(tmp_path)
    group_id = 99999
    asyncio.run(ctrl.handle_reply_text("Settings", chat_id=group_id))
    asyncio.run(ctrl.handle_reply_text("Remote access", chat_id=group_id))
    asyncio.run(ctrl.handle_reply_text("Web panel", chat_id=group_id))
    asyncio.run(ctrl.handle_reply_text("Enable Web panel", chat_id=group_id))
    sent, _, _ = last_sent(bot)
    assert "<code>" not in sent
    assert "private chat" in html.unescape(sent)


def test_remote_access_and_status_show_web_state(tmp_path):
    _, ctrl, _ = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Settings"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Remote access"))
    assert "Web panel: off" in text
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert "Web panel" in labels
    status = asyncio.run(ctrl.handle_status())
    assert "Web panel: off" in status


def test_group_enable_leaves_password_ungenerated(tmp_path):
    """A group chat must not burn the one-time password unseen."""
    config, ctrl, bot = make_controller(tmp_path)
    text = asyncio.run(ctrl._set_web_enabled(True, chat_id=99999))
    assert config.web.enabled is True
    assert config.web.password_hash == ""
    assert "private chat" in text
    assert "<code>" not in text
    # The private-chat enable then generates it.
    text = asyncio.run(ctrl._set_web_enabled(True, chat_id=None))
    assert config.web.password_hash != ""
    assert "<code>" in text


def test_disabled_stop_closes_owned_session(tmp_path):
    import json as _json

    from conftest import make_config as valid_config

    from stream_archive.config import get_config
    from stream_archive.telegram import TelegramController

    data = valid_config(
        channels=["twitch:channel1"],
        telegram_user_id=0,
        bot_telegram_api="",
        kick={"client_id": "client_id", "client_secret": "client_secret"},
    ).model_dump(mode="json", exclude_unset=True)
    (tmp_path / "config.json").write_text(_json.dumps(data))
    config = get_config(tmp_path / "config.json")
    ctrl = TelegramController(config, FakeRecorder(), FakeMonitor(), FakeEventSub(), kick_webhook=FakeKickWebhook())
    assert ctrl.enabled is False
    asyncio.run(ctrl.stop())
    assert ctrl._http.is_closed
