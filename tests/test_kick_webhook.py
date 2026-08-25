import asyncio
import base64
import json
import time

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from stream_archive.config import AppConfig
from stream_archive.kick_api import KickAPI
from stream_archive.kick_webhook import KickWebhook, _RateLimiter


def _fresh_ts():
    """Current epoch-second timestamp string. Webhook events must be fresh."""
    return str(int(time.time()))


def base_config():
    return {
        "telegram_user_id": 12345,
        "bot_telegram_api": "bot_token",
        "twitch_client_id": "client_id",
        "twitch_client_secret": "client_secret",
        "channels": ["kick:xqc"],
        "proxy_list": ["httpproxy://user:pass@host:port"],
        "monitoring_interval": 60,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "recording_dir": "recordings",
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

    async def get_public_key(self, force=False):
        self.fetch_count += 1
        return self.public_key_pem

    def clear_public_key_cache(self):
        pass

    async def get_channel_statuses(self, slugs):
        return {}

    async def list_event_subscriptions(self):
        return []


def make_webhook(config=None, monitor=None, recorder=None, api=None, notifier=None):
    raw = config or base_config()
    # base_config uses listen_port 0 for an ephemeral port so bind tests never
    # collide. The config model allows only ports 1-65535, so make_webhook
    # validates with a placeholder port and re-applies 0 afterwards.
    if isinstance(raw, AppConfig):
        ephemeral = raw.kick.webhook.listen_port == 0
        config = raw
    else:
        ephemeral = raw.get("kick", {}).get("webhook", {}).get("listen_port") == 0
        if ephemeral:
            raw["kick"]["webhook"]["listen_port"] = 8787
        config = AppConfig.model_validate(raw)
    if ephemeral:
        object.__setattr__(config.kick.webhook, "listen_port", 0)
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
    return json.dumps(
        {
            "is_live": is_live,
            "broadcaster": {"channel_slug": slug, "channel_id": 123},
        }
    ).encode()


def chat_event(slug="xqc"):
    return json.dumps(
        {
            "message_id": "msg-123",
            "created_at": "2026-08-13T10:00:00Z",
            "broadcaster": {
                "channel_slug": slug,
                "channel_id": 123,
                "user_id": 123,
                "username": "xqc",
                "profile_picture": "https://example.com/bc.png",
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
        }
    ).encode()


@pytest.fixture
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
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
                headers=_signed_headers(private_key, "m1", _fresh_ts(), live_event(is_live=True), wh.EVENT_LIVE),
            )

    asyncio.run(scenario())
    assert len(monitor.online) == 1
    channel, title, game, user_id, cfg = monitor.online[0]
    assert channel == "kick:xqc"
    assert (title, game, user_id) == (None, None, None)
    assert cfg is wh._config


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
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_LIVE),
            )

    asyncio.run(scenario())
    assert monitor.online == []
    assert len(monitor.offline) == 1
    assert monitor.offline[0][0] == "kick:xqc"


def test_failed_dispatch_not_marked_seen(keypair):
    """A crashing handler must answer 500 and unmark the message id. Kick then
    retries the event, and the retry dispatches instead of counting as a
    duplicate."""
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    wh = make_webhook(config=base_config(), monitor=monitor, api=FakeKickAPI(public_pem))
    calls = {"n": 0}
    original_online = monitor.handle_online

    async def flaky_online(channel, title, game, user_id, config):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient handler boom")
        await original_online(channel, title, game, user_id, config)

    monitor.handle_online = flaky_online

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            body = live_event(is_live=True)
            headers = _signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_LIVE)
            first = await client.post("/kick/webhook", data=body, headers=headers)
            assert first.status == 500
            second = await client.post("/kick/webhook", data=body, headers=headers)  # Kick's retry
            assert second.status == 200

    asyncio.run(scenario())
    assert calls["n"] == 2
    assert len(monitor.online) == 1


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
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_CHAT),
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
            "user_id": 999,
            "username": "viewer1",
            "is_verified": False,
            "is_anonymous": False,
            "profile_picture": "https://example.com/av.png",
            "username_color": "#FF5733",
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
                headers=_signed_headers(private_key, "m1", _fresh_ts(), b"tampered", wh.EVENT_LIVE),
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
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, "some.future.event"),
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
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_LIVE),
            )

    asyncio.run(scenario())
    assert monitor.online == []


def make_mock_api(handler):
    config = base_config()
    # The config model needs a real port, although KickAPI itself never binds.
    config["kick"]["webhook"]["listen_port"] = 8787
    api = KickAPI(AppConfig.model_validate(config))
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return api


def token_response(request):
    assert request.url.path == "/oauth/token"
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})


def test_first_verified_event_confirms_delivery_once(tmp_path, keypair):
    private_key, public_pem = keypair
    config = {
        # save_config validates the whole config, so provide a valid one.
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
    cfg_file.write_text(json.dumps({k: v for k, v in config.items() if not k.startswith("_")}, indent=4))
    config = AppConfig.model_validate(config)
    config._workdir = tmp_path
    config._config_path = cfg_file

    notifier = FakeNotifier()
    wh = make_webhook(config=config, api=FakeKickAPI(public_pem), notifier=notifier)

    # A signature-verified POST proves that Kick saved the URL and can reach
    # it. The webhook confirms setup exactly once and then persists the flag.
    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            body = live_event()
            await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_LIVE),
            )
            # A second event must stay silent.
            await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m2", _fresh_ts(), body, wh.EVENT_LIVE),
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
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"subscription_id": "sub-1"},
                            {"subscription_id": "sub-2"},
                        ]
                    },
                )
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    api = make_mock_api(handler)
    wh = make_webhook(api=api)

    async def scenario():
        await wh._sync_subscriptions(["kick:xqc"])

    asyncio.run(scenario())

    assert seen["posts"] == [
        {
            "broadcaster_user_id": 123,
            "events": [
                {"name": "livestream.status.updated", "version": 1},
                {"name": "chat.message.sent", "version": 1},
            ],
            "method": "webhook",
        }
    ]
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
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "stale-1",
                                "app_id": "cid",
                                "broadcaster_user_id": 999,
                                "events": [{"name": "livestream.status.updated"}],
                            },
                            {
                                "id": "stale-2",
                                "app_id": "cid",
                                "broadcaster_user_id": 999,
                                "events": [{"name": "chat.message.sent"}],
                            },
                        ]
                    },
                )
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
    assert len(notifier.messages) == 1  # sync failure notified once, no setup confirm
    assert "Kick webhook subscriptions out of sync" in notifier.messages[0]
    assert wh._sync_failed_notified is False  # flag cleared after the last success


def test_sync_failure_logged_once_per_episode(caplog):
    config = base_config()
    config["monitoring_interval"] = 0.01
    notifier = FakeNotifier()

    class AlwaysFails:
        async def get_channel_statuses(self, slugs):
            raise httpx.ConnectError("boom")

        async def list_event_subscriptions(self):
            return []

    wh = make_webhook(config=config, api=AlwaysFails(), notifier=notifier)

    async def scenario():
        await wh.start()
        await asyncio.sleep(0.05)
        await wh.close()

    with caplog.at_level("DEBUG", logger="stream_archive.kick_webhook"):
        asyncio.run(scenario())

    errors = [r for r in caplog.records if "subscription sync failed" in r.getMessage()]
    debugs = [r for r in caplog.records if "subscription sync still failing" in r.getMessage()]
    assert len(errors) == 1
    assert len(debugs) >= 1


def _server_error_500():
    request = httpx.Request("GET", "https://api.kick.com/public/v1/events/subscriptions")
    response = httpx.Response(500, request=request)
    return httpx.HTTPStatusError("Server error '500 Internal Server Error'", request=request, response=response)


class ScriptedAPI:
    """Fails with a 500 for the first ``failures`` entries, then succeeds."""

    def __init__(self, failures):
        self.failures = failures
        self.n = 0

    async def get_channel_statuses(self, slugs):
        self.n += 1
        if self.n <= len(self.failures) and self.failures[self.n - 1]:
            raise _server_error_500()
        return {}

    async def list_event_subscriptions(self):
        return []


def test_sync_failure_5xx_stays_silent_until_delay_elapses(monkeypatch):
    # A Kick-side 500 must not notify while it is shorter than the delay.
    monkeypatch.setattr("stream_archive.kick_webhook._SYNC_SERVER_ERROR_DELAY_S", 3600)
    config = base_config()
    config["monitoring_interval"] = 0.01
    notifier = FakeNotifier()
    wh = make_webhook(config=config, api=ScriptedAPI([True] * 100), notifier=notifier)

    async def scenario():
        await wh.start()
        await asyncio.sleep(0.08)
        await wh.close()

    asyncio.run(scenario())
    assert notifier.messages == []
    assert wh._sync_failed_notified is False
    assert wh._sync_failing_since is not None  # episode timer armed


def test_sync_failure_5xx_notifies_after_delay_and_once_per_episode(monkeypatch):
    monkeypatch.setattr("stream_archive.kick_webhook._SYNC_SERVER_ERROR_DELAY_S", 0.02)
    config = base_config()
    config["monitoring_interval"] = 0.01
    notifier = FakeNotifier()
    # Two 500-error episodes, separated by one recovery, each exceed the
    # delay and notify once.
    wh = make_webhook(config=config, api=ScriptedAPI([True] * 6 + [False] + [True] * 6), notifier=notifier)

    async def scenario():
        await wh.start()
        await asyncio.sleep(0.22)
        await wh.close()

    asyncio.run(scenario())
    assert len(notifier.messages) == 2
    assert "Kick webhook subscriptions out of sync" in notifier.messages[0]
    assert "500 Internal Server Error" in notifier.messages[0]


def test_sync_failure_5xx_short_episodes_never_notify(monkeypatch):
    # Recovery resets the episode timer. Brief blips stay silent when each
    # failing run is shorter than the delay, even across several episodes.
    monkeypatch.setattr("stream_archive.kick_webhook._SYNC_SERVER_ERROR_DELAY_S", 0.05)
    config = base_config()
    config["monitoring_interval"] = 0.01
    notifier = FakeNotifier()
    wh = make_webhook(config=config, api=ScriptedAPI([True] * 3 + [False] + [True] * 3), notifier=notifier)

    async def scenario():
        await wh.start()
        await asyncio.sleep(0.15)
        await wh.close()

    asyncio.run(scenario())
    assert notifier.messages == []
    assert wh._sync_failed_notified is False


# ---- replay protection & flood hardening -----------------------------------


def test_stale_timestamp_rejected_401(keypair):
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    wh = make_webhook(monitor=monitor, api=FakeKickAPI(public_pem))
    body = live_event(is_live=True)
    stale = str(int(time.time()) - 600)  # outside the 5-minute window

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", stale, body, wh.EVENT_LIVE),
            )
            assert resp.status == 401

    asyncio.run(scenario())
    assert monitor.online == []


def test_future_timestamp_rejected_401(keypair):
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    wh = make_webhook(monitor=monitor, api=FakeKickAPI(public_pem))
    body = live_event(is_live=True)
    future = str(int(time.time()) + 600)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", future, body, wh.EVENT_LIVE),
            )
            assert resp.status == 401

    asyncio.run(scenario())
    assert monitor.online == []


def test_unparseable_timestamp_rejected_401(keypair):
    private_key, public_pem = keypair
    wh = make_webhook(api=FakeKickAPI(public_pem))
    body = live_event()

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", "not-a-date", body, wh.EVENT_LIVE),
            )
            assert resp.status == 401

    asyncio.run(scenario())


def test_duplicate_message_id_dropped(keypair):
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    wh = make_webhook(monitor=monitor, api=FakeKickAPI(public_pem))
    body = live_event(is_live=True)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            headers = _signed_headers(private_key, "dup-1", _fresh_ts(), body, wh.EVENT_LIVE)
            first = await client.post("/kick/webhook", data=body, headers=headers)
            replay = await client.post("/kick/webhook", data=body, headers=headers)
            assert first.status == 200
            assert replay.status == 200  # replayed event is acknowledged, not dispatched

    asyncio.run(scenario())
    assert len(monitor.online) == 1


class CachingFakeKickAPI(FakeKickAPI):
    """Mimics KickAPI's in-memory public-key cache so fetch counts are real."""

    def __init__(self, public_key_pem=None):
        super().__init__(public_key_pem)
        self._cached = None

    async def get_public_key(self, force=False):
        if force or self._cached is None:
            self._cached = self.public_key_pem
            self.fetch_count += 1
        return self._cached

    def clear_public_key_cache(self):
        self._cached = None


def test_bad_signature_key_refetch_rate_limited(keypair):
    private_key, public_pem = keypair
    api = CachingFakeKickAPI(public_pem)
    wh = make_webhook(api=api)
    body = live_event(is_live=True)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            headers = _signed_headers(private_key, "m1", _fresh_ts(), b"tampered", wh.EVENT_LIVE)
            for _ in range(5):
                resp = await client.post("/kick/webhook", data=body, headers=headers)
                assert resp.status == 401
            # The first request fetches the key once and refetches once.
            # The 60s negative cache keeps the other 4 requests purely local.
            assert api.fetch_count == 2
            # After the refetch window elapses, one more refetch is allowed.
            wh._next_key_refetch = 0.0
            resp = await client.post("/kick/webhook", data=body, headers=headers)
            assert resp.status == 401
            assert api.fetch_count == 3

    asyncio.run(scenario())


def test_rate_limit_returns_429(keypair):
    _, public_pem = keypair
    wh = make_webhook(api=FakeKickAPI(public_pem))
    wh._rate_limiter = _RateLimiter(2, 60)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            statuses = []
            for _ in range(4):
                resp = await client.post("/kick/webhook", data=b"{}")
                statuses.append(resp.status)
            # The bucket starts full, so max=2 admits a burst of 2. Refill
            # drift lets the 3rd request through, and the 4th is blocked.
            assert statuses == [401, 401, 401, 429]

    asyncio.run(scenario())
