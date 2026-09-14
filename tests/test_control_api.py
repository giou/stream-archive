"""Tests for the /api/v1 control API served on the Kick webhook listener.

The API is a thin adapter over the Telegram command layer, so these tests
drive the real controller and check both the HTTP result and the config
file. The listener never starts here: TestClient drives the aiohttp app
directly, like the webhook tests do.
"""

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from stream_archive.api import ControlAPI
from stream_archive.config import get_config
from stream_archive.kick_webhook import KickWebhook
from stream_archive.telegram import TelegramController

KEY = "test-api-key-1234567890"


class FakeRecorder:
    def __init__(self, recording=()):
        self._recording = set(recording)
        self.stop_calls = []

    def is_recording(self, channel):
        return channel in self._recording

    def active_channels(self):
        return sorted(self._recording)

    def recording_settings(self):
        return {
            ch: {
                "output_mode": "disk",
                "preferred_quality": "best",
                "record_chat": True,
                "kick_record_chat": True,
            }
            for ch in sorted(self._recording)
        }

    async def stop(self, channel):
        self.stop_calls.append(channel)
        self._recording.discard(channel)

    async def restart(self, channel):
        return True

    async def stop_chat(self, channel, platform=None):
        return None


class FakeMonitor:
    def __init__(self):
        self.remove_calls = []

    def remove_channel(self, channel):
        self.remove_calls.append(channel)


class FakeEventSub:
    def __init__(self):
        self.added = []
        self.removed = []

    async def add_channel(self, channel):
        self.added.append(channel)

    async def remove_channel(self, channel):
        self.removed.append(channel)


class FakeKickWebhook:
    """The controller's view of the listener owner."""

    def __init__(self):
        self.applied = []
        self.added = []
        self.removed = []

    async def apply_state(self):
        self.applied.append(1)

    async def add_channel(self, channel):
        self.added.append(channel)

    async def remove_channel(self, channel):
        self.removed.append(channel)


def read_file(tmp_path):
    return json.loads((tmp_path / "config.json").read_text())


def make_api(tmp_path, *, enabled=True, recording=(), channels=("twitch:channel1",)):
    """Build controller + API on a real KickWebhook app, as the scheduler does."""
    data = {
        "telegram_user_id": 12345,
        "bot_telegram_api": "bot_token",
        "twitch_client_id": "client_id",
        "twitch_client_secret": "client_secret",
        "channels": list(channels),
        "proxy_list": ["httpproxy://user:pass@host:port"],
        "monitoring_interval": 60,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "recording_dir": "recordings",
        "api": {"enabled": enabled, "key": KEY},
        "kick": {"client_id": "client_id", "client_secret": "client_secret"},
    }
    (tmp_path / "config.json").write_text(json.dumps(data))
    config = get_config(tmp_path / "config.json")
    recorder = FakeRecorder(recording=recording)
    monitor = FakeMonitor()
    eventsub = FakeEventSub()
    kick_webhook = FakeKickWebhook()
    ctrl = TelegramController(config, recorder, monitor, eventsub, kick_webhook=kick_webhook)
    # No test may reach the Telegram API: record the admin messages instead.
    ctrl._sent = []

    async def send(text):
        ctrl._sent.append(text)

    ctrl._send_admin = send
    # The webhook collaborators stay empty: no test here starts the listener.
    wh = KickWebhook(config, None, None, None, None)
    api = ControlAPI(config, ctrl, recorder)
    api.register_routes(wh)
    return config, ctrl, recorder, eventsub, wh


def auth(token=KEY):
    return {"Authorization": f"Bearer {token}"}


def sent_messages(ctrl):
    """Admin messages that the API sent, in order."""
    return ctrl._sent


def test_disabled_api_answers_404(tmp_path):
    _, _, _, _, wh = make_api(tmp_path, enabled=False)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            for method, path in (
                ("get", "/api/v1/status"),
                ("get", "/api/v1/settings"),
                ("get", "/api/v1/channels"),
            ):
                resp = await getattr(client, method)(path, headers=auth())
                assert resp.status == 404
            return await (await client.patch("/api/v1/settings", json={"retention_days": 3}, headers=auth())).json()

    assert asyncio.run(scenario()) == {"error": "not found"}
    assert read_file(tmp_path).get("retention_days", 0) == 0


def test_missing_and_bad_key_are_401(tmp_path):
    _, _, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            missing = await client.get("/api/v1/settings")
            wrong = await client.get("/api/v1/settings", headers=auth("nope"))
            return missing.status, missing.headers["WWW-Authenticate"], wrong.status, await wrong.json()

    assert asyncio.run(scenario()) == (401, "Bearer", 401, {"error": "unauthorized"})


def test_bearer_and_api_key_headers_authenticate(tmp_path):
    _, _, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            bearer = await client.get("/api/v1/settings", headers=auth())
            header = await client.get("/api/v1/settings", headers={"X-API-Key": KEY})
            return bearer.status, header.status

    assert asyncio.run(scenario()) == (200, 200)


def test_settings_response_holds_no_secrets(tmp_path):
    _, _, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.get("/api/v1/settings", headers=auth())
            return resp.status, await resp.text()

    status, body = asyncio.run(scenario())
    assert status == 200
    settings = json.loads(body)
    assert settings["output_mode"] == "disk"
    assert settings["api"]["enabled"] is True
    for secret in (KEY, "bot_token", "client_secret", "user:pass"):
        assert secret not in body


def test_status_reports_active_recordings(tmp_path):
    _, _, _, _, wh = make_api(tmp_path, recording=["twitch:channel1"])

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            return await (await client.get("/api/v1/status", headers=auth())).json()

    status = asyncio.run(scenario())
    assert status["channels"] == 1
    assert status["recording"] == ["twitch:channel1"]
    assert status["version"]  # installed version or "unknown"


def test_patch_settings_applies_and_persists(tmp_path):
    config, ctrl, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch(
                "/api/v1/settings",
                json={"output_mode": "youtube", "retention_days": 3},
                headers=auth(),
            )
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["applied"]["output_mode"] == "Output mode set to youtube"
    assert body["applied"]["retention_days"] == "Retention set to 3 day(s)"
    assert body["errors"] == {}
    assert body["settings"]["output_mode"] == "youtube"
    assert body["settings"]["retention_days"] == 3
    assert config.output_mode == "youtube"
    assert read_file(tmp_path)["output_mode"] == "youtube"
    assert read_file(tmp_path)["retention_days"] == 3
    assert sent_messages(ctrl) == [
        "\U0001f310 Control API\n\u2022 Output mode set to youtube\n\u2022 Retention set to 3 day(s)"
    ]


def test_patch_settings_keeps_good_keys_when_one_fails(tmp_path):
    _, ctrl, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch(
                "/api/v1/settings",
                json={"output_mode": "youtube", "retention_days": "soon"},
                headers=auth(),
            )
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 400  # one failed key marks the whole request
    assert body["applied"] == {"output_mode": "Output mode set to youtube"}
    assert "retention" in body["errors"]["retention_days"]
    assert read_file(tmp_path)["output_mode"] == "youtube"
    assert read_file(tmp_path)["retention_days"] == 0  # the bad key changed nothing
    assert sent_messages(ctrl) == ["\U0001f310 Control API\n\u2022 Output mode set to youtube"]


def test_patch_settings_rejects_default_as_the_global_quality(tmp_path):
    """'default' clears a per-channel override, so it is not a global quality."""
    _, _, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch("/api/v1/settings", json={"preferred_quality": "default"}, headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "preferred_quality" in body["errors"]["preferred_quality"]
    assert read_file(tmp_path).get("preferred_quality", "best") == "best"  # the bad value changed nothing


def test_patch_settings_accepts_a_real_global_quality(tmp_path):
    config, _, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch("/api/v1/settings", json={"preferred_quality": "720p"}, headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["applied"]["preferred_quality"] == "Quality set to 720p"
    assert config.preferred_quality == "720p"


def test_patch_settings_rejects_unknown_keys(tmp_path):
    _, _, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch("/api/v1/settings", json={"nonsense": 1}, headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "unknown setting(s): nonsense" in body["error"]


def test_patch_settings_rejects_a_non_object_body(tmp_path):
    _, _, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            bad = await client.patch("/api/v1/settings", data=b"not json", headers=auth())
            empty = await client.patch("/api/v1/settings", data=b"[1]", headers=auth())
            return bad.status, empty.status

    assert asyncio.run(scenario()) == (400, 400)


def test_patch_settings_rejects_an_oversized_body(tmp_path):
    _, _, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch(
                "/api/v1/settings", data=b'{"retention_days": 1, "pad": "' + b"x" * 70000 + b'"}', headers=auth()
            )
            return resp.status

    assert asyncio.run(scenario()) == 413


def test_add_channel_subscribes_and_rejects_duplicates(tmp_path):
    config, ctrl, _, _, wh = make_api(tmp_path, channels=("twitch:channel1", "kick:xqc"))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            added = await client.post("/api/v1/channels", json={"channel": "twitch:newch"}, headers=auth())
            dup = await client.post("/api/v1/channels", json={"channel": "twitch:newch"}, headers=auth())
            return added.status, await added.json(), dup.status, await dup.json()

    status, body, dup_status, dup_body = asyncio.run(scenario())
    assert status == 200
    assert body["channel"] == "twitch:newch"
    assert body["channels"] == ["twitch:channel1", "kick:xqc", "twitch:newch"]
    assert "Added twitch:newch" in body["message"]
    assert ctrl._eventsub.added == ["twitch:newch"]
    assert dup_status == 400
    assert "already monitored" in dup_body["error"]
    assert sent_messages(ctrl) == ["\U0001f310 Control API\n\u2022 Added twitch:newch \u2014 3 channel(s) monitored"]
    assert read_file(tmp_path)["channels"] == ["twitch:channel1", "kick:xqc", "twitch:newch"]


def test_add_kick_channel_uses_the_webhook_subscription(tmp_path):
    _, ctrl, _, eventsub, wh = make_api(tmp_path, channels=("kick:xqc",))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            return await client.post("/api/v1/channels", json={"channel": "kick:newkick"}, headers=auth())

    assert asyncio.run(scenario()).status == 200
    assert ctrl._kick_webhook.added == ["kick:newkick"]
    assert eventsub.added == []


def test_add_channel_rejects_a_bad_name(tmp_path):
    _, _, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/api/v1/channels", json={"channel": "bad name!"}, headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "invalid channel name" in body["error"]


def test_remove_channel_stops_recording_and_unsubscribes(tmp_path):
    config, ctrl, recorder, _, wh = make_api(tmp_path, channels=("kick:xqc", "twitch:channel1"), recording=["kick:xqc"])

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            removed = await client.delete("/api/v1/channels/kick:xqc", headers=auth())
            again = await client.delete("/api/v1/channels/kick:xqc", headers=auth())
            return removed.status, await removed.json(), again.status

    status, body, again_status = asyncio.run(scenario())
    assert status == 200
    assert "Removed kick:xqc" in body["message"]
    assert body["channels"] == ["twitch:channel1"]
    assert recorder.stop_calls == ["kick:xqc"]
    assert ctrl._kick_webhook.removed == ["kick:xqc"]
    assert read_file(tmp_path)["channels"] == ["twitch:channel1"]
    assert again_status == 404
    assert sent_messages(ctrl)[0].startswith(
        "\U0001f310 Control API\n\u2022 Removed kick:xqc \u2014 1 channel(s) monitored"
    )


def test_removing_the_last_channel_is_rejected(tmp_path):
    # The config model requires at least one channel, exactly as /remove does.
    _, _, _, _, wh = make_api(tmp_path, channels=("twitch:channel1",))
    before = read_file(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.delete("/api/v1/channels/twitch:channel1", headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "at least 1 item" in body["error"]
    assert read_file(tmp_path) == before


def test_channels_list_shows_effective_settings_and_overrides(tmp_path):
    config, ctrl, recorder, _, wh = make_api(tmp_path, channels=("twitch:channel1",), recording=["twitch:channel1"])
    config.channel_output_modes["twitch:channel1"] = "youtube"

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            listed = await client.get("/api/v1/channels", headers=auth())
            one = await client.get("/api/v1/channels/twitch:channel1", headers=auth())
            missing = await client.get("/api/v1/channels/twitch:nope", headers=auth())
            return await listed.json(), await one.json(), missing.status

    listed, one, missing_status = asyncio.run(scenario())
    assert listed["channels"] == [one]
    assert one["channel"] == "twitch:channel1"
    assert one["recording"] is True
    assert one["output_mode"] == "youtube"
    assert one["output_mode_override"] == "youtube"
    assert one["quality"] == "best"
    assert one["quality_override"] is None
    assert missing_status == 404


def test_patch_channel_sets_and_clears_overrides(tmp_path):
    config, ctrl, _, _, wh = make_api(tmp_path, channels=("twitch:channel1",))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            set_resp = await client.patch(
                "/api/v1/channels/twitch:channel1",
                json={"output_mode": "disk", "quality": "720p", "youtube_hold_seconds": 90},
                headers=auth(),
            )
            set_body = await set_resp.json()
            after_set = (
                dict(config.channel_output_modes),
                dict(config.channel_preferred_qualities),
                dict(config.channel_youtube_hold_seconds),
            )
            cleared = await client.patch(
                "/api/v1/channels/twitch:channel1",
                json={"output_mode": "default", "quality": "default", "youtube_hold_seconds": "default"},
                headers=auth(),
            )
            return set_resp.status, set_body, after_set, cleared.status

    set_status, body, after_set, cleared_status = asyncio.run(scenario())
    assert set_status == 200
    assert body["applied"] == {
        "output_mode": "Output mode for twitch:channel1 set to disk",
        "quality": "Quality for twitch:channel1 set to 720p",
        "youtube_hold_seconds": "Hold delay for twitch:channel1 set to 90s (0 = end immediately)",
    }
    assert after_set == ({"twitch:channel1": "disk"}, {"twitch:channel1": "720p"}, {"twitch:channel1": 90})
    assert cleared_status == 200
    sent = sent_messages(ctrl)
    assert len(sent) == 2  # one message per request, not per key
    assert "Quality for twitch:channel1 set to 720p" in sent[0]
    assert "Quality for twitch:channel1 reset to global (best)" in sent[1]
    assert config.channel_output_modes == {}
    assert config.channel_preferred_qualities == {}
    assert config.channel_youtube_hold_seconds == {}
    assert read_file(tmp_path)["channel_output_modes"] == {}


def test_patch_channel_rejects_an_audio_only_youtube_conflict(tmp_path):
    config, _, _, _, wh = make_api(tmp_path, channels=("twitch:channel1",))
    config.output_mode = "youtube"
    before = read_file(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch(
                "/api/v1/channels/twitch:channel1", json={"quality": "audio_only"}, headers=auth()
            )
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 409  # the bot asks for a confirm press; the API cannot confirm
    assert "audio_only" in body["errors"]["quality"]
    assert read_file(tmp_path) == before  # a refused change writes nothing


def test_rotating_the_key_invalidates_the_old_one(tmp_path):
    _, ctrl, _, _, wh = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            before = await client.get("/api/v1/settings", headers=auth())
            await ctrl._rotate_api_key()
            old = await client.get("/api/v1/settings", headers=auth())
            new_key = read_file(tmp_path)["api"]["key"]
            new = await client.get("/api/v1/settings", headers=auth(new_key))
            return before.status, old.status, new.status, new_key

    before, old, new, new_key = asyncio.run(scenario())
    assert (before, old, new) == (200, 401, 200)
    assert new_key != KEY


def test_webhook_route_still_guards_itself_next_to_the_api(tmp_path):
    _, _, _, _, wh = make_api(tmp_path)
    wh._config.kick.webhook.enabled = True  # the receiver answers only while the feature is on

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            unsigned = await client.post("/kick/webhook", data=b"{}")
            api_call = await client.get("/api/v1/status", headers=auth())
            return unsigned.status, api_call.status

    assert asyncio.run(scenario()) == (401, 200)


def test_a_header_that_is_not_ascii_answers_401(tmp_path):
    """A header can carry bytes that are not UTF-8. The key comparison must
    reject them and never raise."""
    from stream_archive.api import _ApiError

    config, ctrl, recorder, _, _ = make_api(tmp_path)
    api = ControlAPI(config, ctrl, recorder)

    class Request:
        remote = "127.0.0.1"
        # aiohttp decodes header bytes that are not UTF-8 like this.
        headers = {"X-API-Key": "\udcff"}

    with pytest.raises(_ApiError) as err:
        api._check_key(Request())
    assert err.value.status == 401
