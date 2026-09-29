"""Tests for the /api/v1 control API served on the private listener.

The API is a thin adapter over the Telegram command layer, so these tests
drive the real controller and check both the HTTP result and the config
file. The listener never starts here: TestClient drives the aiohttp app
directly, like the webhook tests do.
"""

import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer
from conftest import make_config as valid_config
from conftest import read_file

from stream_archive.api import ControlAPI
from stream_archive.config import get_config
from stream_archive.kick_webhook import KickWebhook
from stream_archive.telegram import TelegramController
from stream_archive.webui import WebUI

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
        self.added = []
        self.removed = []
        self.verified = []
        self.state_calls = []
        self.synced = []

    async def apply_state(self) -> None:
        # The listener never starts in these tests, so a reconcile is a no-op.
        self.state_calls.append(1)
        return None

    async def sync_channels(self, channels) -> None:
        # Subscriptions belong to the webhook tests: here the stub only
        # proves the reconcile calls through after an endpoint change.
        self.synced.append(list(channels))
        return None

    async def add_channel(self, channel):
        self.added.append(channel)

    async def remove_channel(self, channel):
        self.removed.append(channel)

    async def verify_delivery(self, timeout=180.0):
        # The delivery proof itself belongs to the webhook tests: here the
        # stub only proves the route calls through and passes the result on.
        self.verified.append(timeout)
        return True, "first delivery in 3s"


def make_api(tmp_path, *, enabled=True, recording=(), channels=("twitch:channel1",)):
    """Build controller + API on a real KickWebhook app, as the scheduler does.

    The shared defaults differ here: the API key and the kick credentials are
    the local placeholders. Only the keys the helper sets go in the file: a
    key left out keeps its model default.
    """
    data = valid_config(
        channels=list(channels),
        api={"enabled": enabled, "key": KEY},
        kick={"client_id": "client_id", "client_secret": "client_secret"},
    ).model_dump(mode="json", exclude_unset=True)
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
    # Production serves the panel and /api/v1 on one listener with shared
    # sessions, so the fixture wires the same pairing.
    webui = WebUI(config, ctrl, recorder)
    api.set_session_checker(webui._session_of)
    return config, ctrl, recorder, eventsub, wh, monitor, webui


def auth(token=KEY):
    return {"Authorization": f"Bearer {token}"}


def sent_messages(ctrl):
    """Admin messages that the API sent, in order."""
    return ctrl._sent


def test_disabled_api_answers_404(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path, enabled=False)
    before = read_file(tmp_path)

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
    assert read_file(tmp_path) == before  # a disabled API writes nothing at all


def test_missing_and_bad_key_are_401(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            missing = await client.get("/api/v1/settings")
            wrong = await client.get("/api/v1/settings", headers=auth("nope"))
            return missing.status, missing.headers["WWW-Authenticate"], wrong.status, await wrong.json()

    assert asyncio.run(scenario()) == (401, "Bearer", 401, {"error": "unauthorized"})


def test_bearer_and_api_key_headers_authenticate(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            bearer = await client.get("/api/v1/settings", headers=auth())
            header = await client.get("/api/v1/settings", headers={"X-API-Key": KEY})
            return bearer.status, header.status

    assert asyncio.run(scenario()) == (200, 200)


def test_settings_response_holds_no_secrets(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.get("/api/v1/settings", headers=auth())
            return resp.status, await resp.text()

    status, body = asyncio.run(scenario())
    assert status == 200
    settings = json.loads(body)
    # Every key is reviewed, so a new field cannot leak a secret silently.
    assert set(settings) == {
        "output_mode",
        "preferred_quality",
        "retention_days",
        "max_concurrent_recordings",
        "max_concurrent_youtube_streams",
        "record_chat",
        "kick_record_chat",
        "youtube",
        "disk",
        "endpoint",
        "kick_webhook",
        "api",
        "monitoring_interval_s",
    }
    assert settings["output_mode"] == "disk"
    assert settings["api"]["enabled"] is True
    for secret in (KEY, "bot_token", "client_secret", "user:pass"):
        assert secret not in body


def test_status_reports_active_recordings(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path, recording=["twitch:channel1"])

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            return await (await client.get("/api/v1/status", headers=auth())).json()

    status = asyncio.run(scenario())
    assert status["channels"] == 1
    assert status["recording"] == ["twitch:channel1"]
    assert status["version"]  # installed version or "unknown"


def test_patch_global_hold_applies_and_rejects(tmp_path):
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            good = await client.patch("/api/v1/settings", json={"youtube_hold_seconds": 90}, headers=auth())
            bad = await client.patch("/api/v1/settings", json={"youtube_hold_seconds": -1}, headers=auth())
            text = await client.patch("/api/v1/settings", json={"youtube_hold_seconds": "soon"}, headers=auth())
            return await good.json(), good.status, await bad.json(), bad.status, await text.json(), text.status

    good, good_status, bad, bad_status, text, text_status = asyncio.run(scenario())
    assert good_status == 200
    assert good["applied"]["youtube_hold_seconds"] == "Hold delay set to 90s (0 = end immediately)"
    assert config.youtube.hold_seconds == 90
    assert read_file(tmp_path)["youtube"]["hold_seconds"] == 90
    assert bad_status == 400 and text_status == 400
    assert read_file(tmp_path)["youtube"]["hold_seconds"] == 90  # bad keys change nothing


def test_patch_endpoint_url_saves_without_enabling(tmp_path):
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            saved = await client.patch(
                "/api/v1/settings", json={"endpoint_public_url": "panel.example.com"}, headers=auth()
            )
            bad = await client.patch("/api/v1/settings", json={"endpoint_public_url": "not a url"}, headers=auth())
            return await saved.json(), saved.status, await bad.json(), bad.status

    saved, saved_status, bad, bad_status = asyncio.run(scenario())
    assert saved_status == 200
    assert config.endpoint.enabled is False  # a URL alone never flips the toggle
    assert config.endpoint.public_url == "https://panel.example.com"
    assert bad_status == 400
    assert config.endpoint.public_url == "https://panel.example.com"


def test_patch_endpoint_toggle_needs_a_url(tmp_path):
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            missing = await client.patch("/api/v1/settings", json={"endpoint_enabled": True}, headers=auth())
            await client.patch(
                "/api/v1/settings", json={"endpoint_public_url": "https://panel.example.com"}, headers=auth()
            )
            on = await client.patch("/api/v1/settings", json={"endpoint_enabled": True}, headers=auth())
            off = await client.patch("/api/v1/settings", json={"endpoint_enabled": False}, headers=auth())
            return (
                await missing.json(),
                missing.status,
                await on.json(),
                on.status,
                await off.json(),
                off.status,
            )

    missing, missing_status, on, on_status, off, off_status = asyncio.run(scenario())
    assert missing_status == 400  # no saved URL, so enabling fails
    assert missing["errors"]["endpoint_enabled"] != ""
    assert on_status == 200
    assert on["applied"]["endpoint_enabled"] == "Endpoint enabled"
    assert on["settings"]["endpoint"]["public_url"] == "https://panel.example.com"  # the toggle keeps the URL
    assert off_status == 200
    assert config.endpoint.enabled is False
    assert read_file(tmp_path)["endpoint"]["public_url"] == "https://panel.example.com"


def test_patch_kick_url_follows_when_cleared(tmp_path):
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            bad = await client.patch("/api/v1/settings", json={"kick_webhook_public_url": "a"}, headers=auth())
            saved = await client.patch(
                "/api/v1/settings", json={"kick_webhook_public_url": "https://kick.example.com"}, headers=auth()
            )
            cleared = await client.patch("/api/v1/settings", json={"kick_webhook_public_url": ""}, headers=auth())
            enabled = await client.patch("/api/v1/settings", json={"kick_webhook_enabled": True}, headers=auth())
            return (
                await bad.json(),
                bad.status,
                await saved.json(),
                saved.status,
                await cleared.json(),
                cleared.status,
                await enabled.json(),
                enabled.status,
            )

    bad, bad_status, saved, saved_status, cleared, cleared_status, enabled, enabled_status = asyncio.run(scenario())
    assert bad_status == 400  # a single label never saves as a Kick entry
    assert saved_status == 200
    assert saved["applied"]["kick_webhook_public_url"] == "Kick URL saved: https://kick.example.com"
    assert cleared_status == 200
    assert cleared["applied"]["kick_webhook_public_url"] == "Kick URL cleared, follows the endpoint"
    assert enabled_status == 200
    assert config.kick.webhook.public_url == ""
    assert config.kick.webhook.enabled is True


def test_patch_api_disable_keeps_the_key(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            off = await client.patch("/api/v1/settings", json={"api_enabled": False}, headers=auth())
            return await off.json(), off.status

    # Disabling answers 404 from here on (key auth is gone with the toggle).
    off, off_status = asyncio.run(scenario())
    assert off_status == 200
    assert off["applied"]["api_enabled"] == "Control API disabled"
    assert read_file(tmp_path)["api"]["key"] == KEY  # disabling keeps the key


def test_api_enable_makes_a_key_when_missing(tmp_path):
    from stream_archive.api import ControlAPI

    config, ctrl, recorder, _, _, _, _ = make_api(tmp_path)
    api = ControlAPI(config, ctrl, recorder)
    config.api.key = ""  # no key saved yet: the next enable must make one
    text = asyncio.run(api._apply_api_enabled(True))
    assert "New API key" in text
    assert read_file(tmp_path)["api"]["key"] not in ("", KEY)
    assert read_file(tmp_path)["api"]["enabled"] is True


def test_patch_delivery_reconcile_failure_restores(tmp_path, monkeypatch):
    from stream_archive import events as _events

    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)
    recorded: list[str] = []
    monkeypatch.setattr(_events, "record", lambda kind, channel, text: recorded.append(text))

    class FailingWebhook:
        def __init__(self):
            self.calls = 0

        async def apply_state(self):
            self.calls += 1
            msg = "bind conflict"
            raise OSError(msg)

        async def sync_channels(self, channels):
            msg = "must not sync after a failed reconcile"
            raise AssertionError(msg)

    failing = FailingWebhook()
    ctrl._kick_webhook = failing

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch(
                "/api/v1/settings", json={"endpoint_public_url": "https://panel.example.com"}, headers=auth()
            )
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 400  # the batch layer reports per-key failures as 400
    assert "endpoint_public_url" not in body["applied"]
    assert "not served, restored" in body["errors"]["endpoint_public_url"]
    assert read_file(tmp_path)["endpoint"]["public_url"] == ""  # the file never claims an unserved URL
    assert failing.calls == 2  # the failed attempt plus the re-converge on the restored state
    assert all("Endpoint URL saved" not in text for text in recorded)  # no false success in the feed


def test_patch_settings_applies_and_persists(tmp_path):
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)

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
    _, ctrl, _, _, wh, _, _ = make_api(tmp_path)

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
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch("/api/v1/settings", json={"preferred_quality": "default"}, headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "preferred_quality" in body["errors"]["preferred_quality"]
    assert read_file(tmp_path).get("preferred_quality", "best") == "best"  # the bad value changed nothing


def test_patch_settings_accepts_a_real_global_quality(tmp_path):
    config, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch("/api/v1/settings", json={"preferred_quality": "720p"}, headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["applied"]["preferred_quality"] == "Quality set to 720p"
    assert config.preferred_quality == "720p"


def test_patch_settings_rejects_unknown_keys(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch("/api/v1/settings", json={"nonsense": 1}, headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "unknown setting(s): nonsense" in body["error"]


def test_patch_settings_rejects_a_non_object_body(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            bad = await client.patch("/api/v1/settings", data=b"not json", headers=auth())
            empty = await client.patch("/api/v1/settings", data=b"[1]", headers=auth())
            return bad.status, empty.status

    assert asyncio.run(scenario()) == (400, 400)


def test_patch_settings_rejects_an_oversized_body(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch(
                "/api/v1/settings", data=b'{"retention_days": 1, "pad": "' + b"x" * 70000 + b'"}', headers=auth()
            )
            return resp.status

    assert asyncio.run(scenario()) == 413


def test_patch_settings_rejects_a_non_finite_number(tmp_path):
    """inf and nan reach the API as a JSON literal or as a string."""
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            # A hand-written body can carry the Infinity literal.
            literal = await client.patch("/api/v1/settings", data=b'{"retention_days": Infinity}', headers=auth())
            text = await client.patch("/api/v1/settings", data=b'{"retention_days": "nan"}', headers=auth())
            return literal.status, await literal.json(), text.status, await text.json()

    status, body, text_status, text_body = asyncio.run(scenario())
    assert status == 400
    assert "must be a finite number" in body["errors"]["retention_days"]
    assert text_status == 400
    assert "must be a finite number" in text_body["errors"]["retention_days"]
    assert text_body["settings"]["retention_days"] == 0  # nothing was written


def test_patch_settings_rejects_a_body_that_is_not_utf8(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch("/api/v1/settings", data=b'{"retention_days": \xff}', headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "invalid JSON body" in body["error"]


def test_add_channel_subscribes_and_rejects_duplicates(tmp_path):
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path, channels=("twitch:channel1", "kick:xqc"))

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
    assert sent_messages(ctrl) == ["\U0001f310 Control API\n\u2022 Added twitch:newch - 3 channel(s) monitored"]
    assert read_file(tmp_path)["channels"] == ["twitch:channel1", "kick:xqc", "twitch:newch"]


def test_add_kick_channel_uses_the_webhook_subscription(tmp_path):
    _, ctrl, _, eventsub, wh, _, _ = make_api(tmp_path, channels=("kick:xqc",))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            return await client.post("/api/v1/channels", json={"channel": "kick:newkick"}, headers=auth())

    assert asyncio.run(scenario()).status == 200
    assert ctrl._kick_webhook.added == ["kick:newkick"]
    assert eventsub.added == []


def test_add_channel_rejects_a_bad_name(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/api/v1/channels", json={"channel": "bad name!"}, headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "invalid channel name" in body["error"]


def test_remove_channel_stops_recording_and_unsubscribes(tmp_path):
    config, ctrl, recorder, _, wh, monitor, _ = make_api(
        tmp_path, channels=("kick:xqc", "twitch:channel1"), recording=["kick:xqc"]
    )

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
    assert monitor.remove_calls == ["kick:xqc"]  # no stale monitor state
    assert ctrl._kick_webhook.removed == ["kick:xqc"]
    assert read_file(tmp_path)["channels"] == ["twitch:channel1"]
    assert again_status == 404
    assert sent_messages(ctrl)[0].startswith("\U0001f310 Control API\n\u2022 Removed kick:xqc - 1 channel(s) monitored")


def test_remove_twitch_channel_unsubscribes_eventsub(tmp_path):
    _, ctrl, _, eventsub, wh, _, _ = make_api(tmp_path, channels=("twitch:channel1", "kick:xqc"))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            return await client.delete("/api/v1/channels/twitch:channel1", headers=auth())

    resp = asyncio.run(scenario())
    assert resp.status == 200
    # A Twitch channel is left through EventSub, never through the Kick listener.
    assert eventsub.removed == ["twitch:channel1"]
    assert ctrl._kick_webhook.removed == []


def test_removing_the_last_channel_is_allowed(tmp_path):
    # An empty list is valid, so the last monitored channel can go too.
    _, _, _, _, wh, _, _ = make_api(tmp_path, channels=("twitch:channel1",))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.delete("/api/v1/channels/twitch:channel1", headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["channels"] == []
    assert read_file(tmp_path)["channels"] == []


def test_channels_list_shows_effective_settings_and_overrides(tmp_path):
    config, ctrl, recorder, _, wh, _, _ = make_api(
        tmp_path, channels=("twitch:channel1",), recording=["twitch:channel1"]
    )
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
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path, channels=("twitch:channel1",))

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
    config, _, _, _, wh, _, _ = make_api(tmp_path, channels=("twitch:channel1",))
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
    _, ctrl, _, _, wh, _, _ = make_api(tmp_path)

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
    _, _, _, _, wh, _, _ = make_api(tmp_path)
    wh._config.kick.webhook.enabled = True  # the receiver answers only while the feature is on

    async def scenario():
        async with TestClient(TestServer(wh._app)) as private:
            api_call = await private.get("/api/v1/status", headers=auth())
        async with TestClient(TestServer(wh._webhook_app)) as public:
            unsigned = await public.post("/kick/webhook", data=b"{}")
            return unsigned.status, api_call.status

    assert asyncio.run(scenario()) == (401, 200)


def test_non_utf8_header_bytes_answer_401(tmp_path):
    """The real listener decodes header bytes that are not UTF-8. The key
    comparison must reject them and answer 401, not raise."""
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            reader, writer = await asyncio.open_connection("127.0.0.1", client.server.port)
            # 0xFF is not valid UTF-8. aiohttp passes such bytes through.
            writer.write(
                b"GET /api/v1/status HTTP/1.1\r\nHost: 127.0.0.1\r\nX-API-Key: \xff\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), timeout=5)
            writer.close()
            return status_line.split(b"\r\n", 1)[0]

    assert asyncio.run(scenario()) == b"HTTP/1.1 401 Unauthorized"


def session_auth(webui):
    """Cookie header plus CSRF token for a fresh panel session, no HTTP login."""
    sid, session = webui._new_session()
    return {"Cookie": f"sa_session={webui._cookie_value(sid)}"}, session.csrf


def test_v1_accepts_panel_session_without_key(tmp_path):
    """The panel and scripts share one API: a live session needs no key."""
    _, _, _, _, wh, _, webui = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            cookie, _ = session_auth(webui)
            resp = await client.get("/api/v1/settings", headers=cookie)
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["output_mode"] == "disk"


def test_v1_session_write_needs_csrf(tmp_path):
    """Session writes on /api/v1 need the CSRF token, like panel writes."""
    _, _, _, _, wh, _, webui = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            cookie, csrf = session_auth(webui)
            missing = await client.patch("/api/v1/settings", json={"retention_days": 5}, headers=cookie)
            wrong = await client.patch(
                "/api/v1/settings", json={"retention_days": 5}, headers=cookie | {"X-CSRF-Token": "nope"}
            )
            ok = await client.patch(
                "/api/v1/settings", json={"retention_days": 5}, headers=cookie | {"X-CSRF-Token": csrf}
            )
            return missing.status, wrong.status, ok.status, await ok.json()

    missing, wrong, ok, body = asyncio.run(scenario())
    assert (missing, wrong, ok) == (403, 403, 200)
    assert body["applied"]["retention_days"] == "Retention set to 5 day(s)"


def test_v1_session_write_reports_web_panel_origin(tmp_path):
    """Admin messages name the surface that changed the setting."""
    _, ctrl, _, _, wh, _, webui = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            cookie, csrf = session_auth(webui)
            await client.patch("/api/v1/settings", json={"retention_days": 5}, headers=cookie | {"X-CSRF-Token": csrf})
            await client.patch("/api/v1/settings", json={"retention_days": 6}, headers=auth())

    asyncio.run(scenario())
    assert sent_messages(ctrl)[0].startswith("🌐 Web panel\n")
    assert sent_messages(ctrl)[1].startswith("🌐 Control API\n")


def test_v1_session_works_while_api_disabled(tmp_path):
    """The panel works with the key API off: sessions ignore the key gate."""
    _, _, _, _, wh, _, webui = make_api(tmp_path, enabled=False)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            cookie, _ = session_auth(webui)
            resp = await client.get("/api/v1/settings", headers=cookie)
            return resp.status

    assert asyncio.run(scenario()) == 200


def test_v1_key_guessing_ends_in_429(tmp_path):
    _, _, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            codes = []
            for _ in range(11):
                resp = await client.get("/api/v1/settings", headers=auth("nope"))
                codes.append(resp.status)
            return codes

    codes = asyncio.run(scenario())
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


def test_kick_delivery_test_route_calls_through(tmp_path):
    """POST /api/v1/kick/webhook/test runs the listener's proof and passes it on."""
    _, ctrl, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/api/v1/kick/webhook/test", headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body == {"ok": True, "message": "first delivery in 3s"}
    assert ctrl._kick_webhook.verified == [180.0]


def test_kick_delivery_test_route_needs_the_listener(tmp_path):
    """Without a delivery owner the route answers 503, not 500."""
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)
    ctrl._kick_webhook = None

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/api/v1/kick/webhook/test", headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert body == {"error": "Kick delivery test unavailable"}


def test_disabled_api_never_spends_budget(tmp_path):
    """A disabled API answers 404 on every attempt, never 429."""
    _, _, _, _, wh, _, _ = make_api(tmp_path, enabled=False)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            codes = []
            for _ in range(11):
                resp = await client.get("/api/v1/settings", headers=auth("nope"))
                codes.append(resp.status)
            return codes

    assert asyncio.run(scenario()) == [404] * 11


def test_status_reports_degraded_problems(tmp_path):
    from stream_archive.health import clear_degraded, set_degraded

    _, _, _, _, wh, _, _ = make_api(tmp_path)
    set_degraded("twitch_auth", "Twitch rejected the app credentials (HTTP 401)")
    try:

        async def scenario():
            async with TestClient(TestServer(wh._app)) as client:
                return await (await client.get("/api/v1/status", headers=auth())).json()

        assert asyncio.run(scenario())["degraded"] == ["twitch_auth"]
    finally:
        clear_degraded("twitch_auth")


def test_kick_enable_without_endpoint_skips_sync(tmp_path):
    """Sync needs somewhere to deliver: endpoint off means no re-subscribe."""
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch("/api/v1/settings", json={"kick_webhook_enabled": True}, headers=auth())
            return resp.status, await resp.json()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert config.kick.webhook.enabled is True
    assert ctrl._kick_webhook.synced == []


def test_endpoint_and_kick_on_syncs_subscriptions(tmp_path):
    """With both toggles on, the reconcile re-subscribes the channels."""
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await client.patch(
                "/api/v1/settings", json={"endpoint_public_url": "https://panel.example.com"}, headers=auth()
            )
            await client.patch("/api/v1/settings", json={"endpoint_enabled": True}, headers=auth())
            resp = await client.patch("/api/v1/settings", json={"kick_webhook_enabled": True}, headers=auth())
            return resp.status, await resp.json()

    status, _ = asyncio.run(scenario())
    assert status == 200
    assert ctrl._kick_webhook.synced[-1] == ["twitch:channel1"]


def test_api_toggle_reconciles_the_listener(tmp_path):
    """Disabling the API rebinds: the listener must not serve a dead toggle."""
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.patch("/api/v1/settings", json={"api_enabled": False}, headers=auth())
            return resp.status, await resp.json()

    status, _ = asyncio.run(scenario())
    assert status == 200
    assert len(ctrl._kick_webhook.state_calls) == 1


def test_endpoint_url_with_bad_port_rejected(tmp_path):
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            big = await client.patch(
                "/api/v1/settings", json={"endpoint_public_url": "https://host.example.com:99999"}, headers=auth()
            )
            text = await client.patch(
                "/api/v1/settings", json={"endpoint_public_url": "https://host.example.com:abc"}, headers=auth()
            )
            return big.status, text.status

    big_status, text_status = asyncio.run(scenario())
    assert big_status == 400
    assert text_status == 400
    assert read_file(tmp_path).get("endpoint", {}).get("public_url", "") == ""


def test_endpoint_url_clears_only_while_off(tmp_path):
    config, ctrl, _, _, wh, _, _ = make_api(tmp_path)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await client.patch(
                "/api/v1/settings", json={"endpoint_public_url": "https://panel.example.com"}, headers=auth()
            )
            cleared = await client.patch("/api/v1/settings", json={"endpoint_public_url": ""}, headers=auth())
            await client.patch(
                "/api/v1/settings", json={"endpoint_public_url": "https://panel.example.com"}, headers=auth()
            )
            await client.patch("/api/v1/settings", json={"endpoint_enabled": True}, headers=auth())
            locked = await client.patch("/api/v1/settings", json={"endpoint_public_url": ""}, headers=auth())
            return await cleared.json(), cleared.status, await locked.json(), locked.status

    cleared, cleared_status, locked, locked_status = asyncio.run(scenario())
    assert cleared_status == 200
    assert cleared["applied"]["endpoint_public_url"] == "Endpoint URL cleared"
    assert locked_status == 400
    assert config.endpoint.public_url == "https://panel.example.com"
