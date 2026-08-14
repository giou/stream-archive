import asyncio
import base64
import json

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.stream_archive.kick_api import KickAPI
from src.stream_archive.kick_webhook import KickWebhook


def base_config():
    return {
        "channels": ["kick:xqc"],
        "monitoring_interval": 60,
        "kick": {
            "client_id": "cid",
            "client_secret": "csec",
            "record_chat": True,
            "webhook": {
                "enabled": False,
                "listen_host": "127.0.0.1",
                "listen_port": 0,  # ephemeral for tests
                "public_url": "",
            },
        },
    }


class FakeMonitor:
    def __init__(self):
        self.online = []
        self.offline = []

    async def handle_online(self, channel, title, game, user_id, config):
        self.online.append((channel, title, game, user_id, config))

    async def handle_offline(self, channel, config):
        self.offline.append((channel, config))


class FakeRecorder:
    def __init__(self):
        self.chat = []

    async def add_kick_chat(self, channel, payload):
        self.chat.append((channel, payload))


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def notify(self, m):
        self.messages.append(m)


class FakeKickAPI:
    """Minimal stand-in with the webhook-facing surface."""

    def __init__(self, public_key_pem=None):
        self.public_key_pem = public_key_pem
        self.fetch_count = 0

    async def get_public_key(self):
        self.fetch_count += 1
        return self.public_key_pem

    def clear_public_key_cache(self):
        pass

    async def get_channel_statuses(self, slugs):
        return {}

    async def list_event_subscriptions(self):
        return []


def make_webhook(config=None, monitor=None, recorder=None, api=None, notifier=None):
    config = config or base_config()
    return KickWebhook(
        config,
        monitor or FakeMonitor(),
        recorder or FakeRecorder(),
        api or FakeKickAPI(),
        notifier or FakeNotifier(),
    )


def sign(private_key, message_id, timestamp, body):
    message = f"{message_id}.{timestamp}.{body.decode()}".encode()
    signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


def live_event(slug="xqc", is_live=True):
    return json.dumps({
        "is_live": is_live,
        "broadcaster": {"channel_slug": slug, "channel_id": 123},
    }).encode()


def chat_event(slug="xqc"):
    return json.dumps({
        "message_id": "msg-123",
        "created_at": "2026-08-13T10:00:00Z",
        "broadcaster": {
            "channel_slug": slug, "channel_id": 123, "user_id": 123,
            "username": "xqc", "profile_picture": "https://example.com/bc.png",
        },
        "sender": {
            "user_id": 999,
            "username": "viewer1",
            "is_verified": False,
            "is_anonymous": False,
            "profile_picture": "https://example.com/av.png",
            "identity": {
                "username_color": "#FF5733",
                "badges": [{"text": "sub", "type": "sub", "count": 1}],
            },
        },
        "content": "hello kick \U0001f600 [emote:37226:KEKW]",
        "emotes": [
            {"emote_id": "emote-1", "positions": [{"s": 0, "e": 6}]},
            {"emote_id": "37226", "positions": [{"s": 13, "e": 30}]},
        ],
    }).encode()


@pytest.fixture
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, public_pem


def test_start_twice_binds_once_and_close_twice_safe():
    wh = make_webhook()

    async def scenario():
        await wh.start()
        assert wh._runner is not None
        await wh.start()  # second start is a no-op
        assert wh._runner is not None
        await wh.close()
        assert wh._runner is None
        await wh.close()  # double close is safe
        assert wh._runner is None

    asyncio.run(scenario())


def test_sync_loop_runs_on_interval_cadence():
    config = base_config()
    config["monitoring_interval"] = 0.01
    calls = {"n": 0}

    class CountingAPI:
        async def get_channel_statuses(self, slugs):
            calls["n"] += 1
            return {}

        async def list_event_subscriptions(self):
            return []

    wh = make_webhook(config=config, api=CountingAPI())

    async def scenario():
        await wh.start()
        await asyncio.sleep(0.06)
        await wh.close()

    asyncio.run(scenario())
    assert calls["n"] >= 2


def _signed_headers(private_key, message_id, timestamp, body, event_type):
    return {
        "Kick-Event-Type": event_type,
        "Kick-Event-Message-Id": message_id,
        "Kick-Event-Message-Timestamp": timestamp,
        "Kick-Event-Signature": sign(private_key, message_id, timestamp, body),
    }


def test_live_event_dispatches_online(keypair):
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    config = base_config()
    wh = make_webhook(config=config, monitor=monitor, api=FakeKickAPI(public_pem))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await client.post(
                "/kick/webhook",
                data=live_event(is_live=True),
                headers=_signed_headers(private_key, "m1", "1750000000", live_event(is_live=True), wh.EVENT_LIVE),
            )

    asyncio.run(scenario())
    assert len(monitor.online) == 1
    channel, title, game, user_id, cfg = monitor.online[0]
    assert channel == "kick:xqc"
    assert (title, game, user_id) == (None, None, None)
    assert cfg is config


def test_live_event_dispatches_offline(keypair):
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    config = base_config()
    wh = make_webhook(config=config, monitor=monitor, api=FakeKickAPI(public_pem))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            body = live_event(is_live=False)
            await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", "1750000000", body, wh.EVENT_LIVE),
            )

    asyncio.run(scenario())
    assert monitor.online == []
    assert len(monitor.offline) == 1
    assert monitor.offline[0][0] == "kick:xqc"


def test_chat_event_dispatches_normalized_payload(keypair):
    private_key, public_pem = keypair
    recorder = FakeRecorder()
    wh = make_webhook(recorder=recorder, api=FakeKickAPI(public_pem))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            body = chat_event()
            await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", "1750000000", body, wh.EVENT_CHAT),
            )

    asyncio.run(scenario())
    assert len(recorder.chat) == 1
    channel, payload = recorder.chat[0]
    assert channel == "kick:xqc"
    assert payload == {
        "message_id": "msg-123",
        "created_at": "2026-08-13T10:00:00Z",
        "broadcaster": {"user_id": 123, "username": "xqc", "profile_picture": "https://example.com/bc.png"},
        "sender": {
            "user_id": 999, "username": "viewer1", "is_verified": False, "is_anonymous": False,
            "profile_picture": "https://example.com/av.png", "username_color": "#FF5733",
        },
        "content": "hello kick \U0001f600 [emote:37226:KEKW]",
        "emotes": [
            {"emote_id": "emote-1", "positions": [{"s": 0, "e": 6}]},
            {"emote_id": "37226", "positions": [{"s": 13, "e": 30}]},
        ],
        "badges": [{"text": "sub", "type": "sub", "count": 1}],
    }


def test_bad_signature_returns_401_and_no_dispatch(keypair):
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    wh = make_webhook(monitor=monitor, api=FakeKickAPI(public_pem))
    body = live_event(is_live=True)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", "1750000000", b"tampered", wh.EVENT_LIVE),
            )
            assert resp.status == 401

    asyncio.run(scenario())
    assert monitor.online == []


def test_missing_signature_headers_401(keypair):
    _, public_pem = keypair
    wh = make_webhook(api=FakeKickAPI(public_pem))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/kick/webhook", data=live_event())
            assert resp.status == 401

    asyncio.run(scenario())


def test_unknown_event_type_returns_204(keypair):
    private_key, public_pem = keypair
    wh = make_webhook(api=FakeKickAPI(public_pem))
    body = b"{}"

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", "1750000000", body, "some.future.event"),
            )
            assert resp.status == 204

    asyncio.run(scenario())


def test_live_event_unmonitored_channel_ignored(keypair):
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    config = base_config()
    config["channels"] = ["kick:other"]
    wh = make_webhook(config=config, monitor=monitor, api=FakeKickAPI(public_pem))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            body = live_event(is_live=True)
            await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", "1750000000", body, wh.EVENT_LIVE),
            )

    asyncio.run(scenario())
    assert monitor.online == []


def make_mock_api(handler):
    api = KickAPI(base_config())
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return api


def token_response(request):
    assert request.url.path == "/oauth/token"
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})


def test_first_verified_event_confirms_delivery_once(tmp_path, keypair):
    private_key, public_pem = keypair
    config = {
        # save_config validates the whole config, so provide a valid one
        "telegram_user_id": 12345,
        "bot_telegram_api": "bot_token",
        "twitch_client_id": "client_id",
        "twitch_client_secret": "client_secret",
        "channels": ["kick:xqc"],
        "proxy_list": ["httpproxy://user:pass@host:port"],
        "monitoring_interval": 0.01,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "recording_dir": "recordings",
        "kick": {
            "client_id": "cid",
            "client_secret": "csec",
            "record_chat": True,
            "webhook": {
                "enabled": True,
                "listen_host": "127.0.0.1",
                "listen_port": 8799,  # != 8787 (occupied by the docker-published port)
                "public_url": "https://x.example.com/kick/webhook",
            },
        },
    }
    config["_workdir"] = tmp_path
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(
        {k: v for k, v in config.items() if not k.startswith("_")}, indent=4
    ))
    config["_config_path"] = cfg_file

    notifier = FakeNotifier()
    wh = make_webhook(config=config, api=FakeKickAPI(public_pem), notifier=notifier)

    # A signature-verified POST proves Kick saved the URL and can reach it:
    # confirm exactly once, then persist the flag.
    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            body = live_event()
            await client.post(
                "/kick/webhook", data=body,
                headers=_signed_headers(private_key, "m1", "1750000000", body, wh.EVENT_LIVE),
            )
            # A second event must stay silent.
            await client.post(
                "/kick/webhook", data=body,
                headers=_signed_headers(private_key, "m2", "1750000000", body, wh.EVENT_LIVE),
            )

    asyncio.run(scenario())
    assert len(notifier.messages) == 1
    assert "Kick webhook is working" in notifier.messages[0]
    assert "first event received" in notifier.messages[0]
    assert json.loads(cfg_file.read_text())["kick"]["webhook"]["setup_notified"] is True


def test_unverified_event_does_not_confirm(tmp_path, keypair):
    private_key, public_pem = keypair
    config = base_config()
    config.setdefault("kick", {}).setdefault("webhook", {}).update(
        {"enabled": True, "public_url": "https://x.example.com/kick/webhook"}
    )
    notifier = FakeNotifier()
    wh = make_webhook(config=config, api=FakeKickAPI(public_pem), notifier=notifier)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post("/kick/webhook", data=live_event())  # no signature
            assert resp.status == 401

    asyncio.run(scenario())
    assert notifier.messages == []


def test_reconcile_creates_missing_subscriptions():
    seen = {"posts": []}
    channel_data = {
        "slug": "xqc",
        "stream_title": None,
        "category": None,
        "stream": {"is_live": False},
        "broadcaster_user_id": 123,
    }

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_response(request)
        if request.url.path == "/public/v1/channels":
            return httpx.Response(200, json={"data": [channel_data]})
        if request.url.path == "/public/v1/events/subscriptions":
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            if request.method == "POST":
                seen["posts"].append(json.loads(request.content))
                return httpx.Response(200, json={"data": [
                    {"subscription_id": "sub-1"}, {"subscription_id": "sub-2"},
                ]})
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    api = make_mock_api(handler)
    wh = make_webhook(api=api)

    async def scenario():
        await wh._sync_subscriptions(["kick:xqc"])

    asyncio.run(scenario())

    assert seen["posts"] == [{
        "broadcaster_user_id": 123,
        "events": [
            {"name": "livestream.status.updated", "version": 1},
            {"name": "chat.message.sent", "version": 1},
        ],
        "method": "webhook",
    }]
    assert wh._subs == {"xqc": {"sub-1", "sub-2"}}


def test_reconcile_deletes_stale_subscriptions():
    deletes = []

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_response(request)
        if request.url.path == "/public/v1/channels":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/public/v1/events/subscriptions":
            if request.method == "GET":
                return httpx.Response(200, json={"data": [
                    {"id": "stale-1", "app_id": "cid", "broadcaster_user_id": 999,
                     "events": [{"name": "livestream.status.updated"}]},
                    {"id": "stale-2", "app_id": "cid", "broadcaster_user_id": 999,
                     "events": [{"name": "chat.message.sent"}]},
                ]})
            if request.method == "DELETE":
                deletes.append([v for k, v in request.url.params.multi_items() if k == "id"])
                return httpx.Response(200, json={"data": []})
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    api = make_mock_api(handler)
    wh = make_webhook(api=api)
    wh._subs = {"oldch": {"stale-1", "stale-2"}}

    async def scenario():
        await wh._sync_subscriptions(["kick:xqc"])

    asyncio.run(scenario())

    assert deletes == [["stale-1", "stale-2"]]
    assert wh._subs == {}


def test_reconcile_failure_notifies_once_and_clears_on_success():
    config = base_config()
    config["monitoring_interval"] = 0.01
    notifier = FakeNotifier()
    calls = {"n": 0}

    class FlakyAPI:
        async def get_channel_statuses(self, slugs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("boom")
            return {}

        async def list_event_subscriptions(self):
            return []

    wh = make_webhook(config=config, api=FlakyAPI(), notifier=notifier)

    async def scenario():
        await wh.start()
        await asyncio.sleep(0.09)
        await wh.close()

    asyncio.run(scenario())

    assert calls["n"] >= 4
    assert len(notifier.messages) == 1  # only the out-of-sync warning; sync never confirms
    assert "Kick webhook subscriptions out of sync" in notifier.messages[0]
    assert wh._sync_failed_notified is False  # flag cleared after the last success
