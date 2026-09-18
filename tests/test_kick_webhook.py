import asyncio
import base64
import copy
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from stream_archive.config import AppConfig
from stream_archive.kick_api import KickAPI
from stream_archive.kick_webhook import _VERIFY_WINDOW_S, KickWebhook, _RateLimiter


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
        "endpoint": {
            "enabled": False,
            "listen_host": "127.0.0.1",
            "listen_port": 0,  # ephemeral for tests
            "public_url": "",
        },
        "kick": {
            "client_id": "cid",
            "client_secret": "csec",
            "record_chat": True,
            # The receiver accepts deliveries only while the feature is on, so
            # the normal test config has it on. Tests of the off state set it
            # to false themselves.
            "webhook": {"enabled": True},
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

    def has_public_key(self):
        return self.public_key_pem is not None

    def clear_public_key_cache(self):
        pass

    async def get_channel_statuses(self, slugs):
        return {}

    async def list_event_subscriptions(self):
        return []


def enabled_config(**overrides):
    """base_config with endpoint and webhook on: the listener and sync loop need both."""
    config = base_config()
    config["endpoint"]["enabled"] = True
    config["endpoint"]["public_url"] = "https://x.example.com"
    config["kick"]["webhook"]["enabled"] = True
    config.update(overrides)
    return config


def make_webhook(config=None, monitor=None, recorder=None, api=None, notifier=None):
    # Validate a copy. The placeholder port below must not leak into the
    # caller's dict, which other helpers hand to more than one webhook.
    raw = copy.deepcopy(config) if isinstance(config, dict) else config
    if raw is None:
        raw = base_config()
    # base_config uses listen_port 0 for an ephemeral port so bind tests never
    # collide. The config model allows only ports 1-65535, so make_webhook
    # validates with a placeholder port and re-applies 0 afterwards.
    if isinstance(raw, AppConfig):
        ephemeral = raw.endpoint.listen_port == 0
        config = raw
    else:
        ephemeral = raw.get("endpoint", {}).get("listen_port") == 0
        if ephemeral:
            raw["endpoint"]["listen_port"] = 8787
        config = AppConfig.model_validate(raw)
    if ephemeral:
        object.__setattr__(config.endpoint, "listen_port", 0)
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


def test_apply_state_is_idempotent_and_close_twice_safe():
    wh = make_webhook(config=enabled_config())

    async def scenario():
        await wh.apply_state()
        assert wh._runner is not None
        assert wh._sync_task is not None
        await wh.apply_state()  # second call is a no-op
        assert wh._runner is not None
        await wh.close()
        assert wh._runner is None
        assert wh._sync_task is None
        await wh.close()  # double close is safe
        assert wh._runner is None

    asyncio.run(scenario())


def test_apply_state_keeps_the_listener_for_the_api_alone():
    """The API needs the listener but never the webhook subscription sync."""
    config = base_config()
    config["api"] = {"enabled": True, "key": "k"}
    config["kick"]["webhook"]["enabled"] = False
    calls = {"n": 0}

    class CountingAPI:
        async def get_channel_statuses(self, slugs):
            calls["n"] += 1
            return {}

        async def list_event_subscriptions(self):
            return []

    wh = make_webhook(config=config, api=CountingAPI())

    async def scenario():
        await wh.apply_state()
        assert wh._runner is not None
        assert wh._sync_task is None  # no webhook: no reconcile that deletes subs
        # A few loop turns give a stray sync task the chance to run.
        for _ in range(3):
            await asyncio.sleep(0)
        await wh.apply_state()  # a keep-listener reconcile changes nothing
        assert wh._runner is not None
        assert wh._sync_task is None
        await wh.close()
        assert wh._runner is None

    asyncio.run(scenario())
    assert calls["n"] == 0


def test_apply_state_stops_the_listener_when_nothing_is_enabled():
    config = base_config()
    config["api"] = {"enabled": True, "key": "k"}
    wh = make_webhook(config=config)

    async def scenario():
        await wh.apply_state()
        assert wh._runner is not None
        wh._config.api.enabled = False
        await wh.apply_state()
        assert wh._runner is None

    asyncio.run(scenario())


def test_apply_state_serves_webhook_and_api_together():
    config = enabled_config()
    config["api"] = {"enabled": True, "key": "k"}
    wh = make_webhook(config=config)

    async def scenario():
        await wh.apply_state()
        assert wh._runner is not None
        assert wh._sync_task is not None  # webhook on: sync runs
        await wh.close()

    asyncio.run(scenario())


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
            msg = "transient handler boom"
            raise RuntimeError(msg)
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
    # Pass the mock client through the constructor, so KickAPI does not build
    # and leak an httpx client of its own. The caller closes this one.
    config = base_config()
    # The config model needs a real port, although KickAPI itself never binds.
    config["endpoint"]["listen_port"] = 8787
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return KickAPI(AppConfig.model_validate(config), http=client)


def token_response(request):
    assert request.url.path == "/oauth/token"
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})


def writable_config(tmp_path):
    """enabled_config bound to a real file, so a flow that persists a flag can save it."""
    raw = enabled_config()
    raw["endpoint"]["listen_port"] = 8799  # a real port, which the model requires
    raw["_workdir"] = tmp_path
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({k: v for k, v in raw.items() if not k.startswith("_")}, indent=4))
    config = AppConfig.model_validate(raw)
    config._workdir = tmp_path
    config._config_path = cfg_file
    return config, cfg_file


def test_first_verified_event_confirms_delivery_once(tmp_path, keypair):
    private_key, public_pem = keypair
    config, cfg_file = writable_config(tmp_path)
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


def test_two_first_events_confirm_the_delivery_once(tmp_path, keypair):
    """The flag is set before the send, so a second event cannot confirm too."""
    private_key, public_pem = keypair
    config, _ = writable_config(tmp_path)
    messages = []
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingNotifier:
        """Holds the send open until the test releases it."""

        async def notify(self, message):
            messages.append(message)
            started.set()
            await release.wait()

    wh = make_webhook(config=config, api=FakeKickAPI(public_pem), notifier=BlockingNotifier())

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            body = live_event()
            first = asyncio.create_task(
                client.post(
                    "/kick/webhook",
                    data=body,
                    headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_LIVE),
                )
            )
            await asyncio.wait_for(started.wait(), 5)
            # The first send is still in flight, and the flag is already set.
            assert wh._config.kick.webhook.setup_notified is True
            second = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m2", _fresh_ts(), body, wh.EVENT_LIVE),
            )
            assert second.status == 200
            release.set()
            assert (await first).status == 200

    asyncio.run(scenario())
    assert len(messages) == 1


def test_unverified_event_does_not_confirm(tmp_path, keypair):
    private_key, public_pem = keypair
    config = base_config()
    config["kick"]["webhook"]["enabled"] = True
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
                # The documented create response is endpoints.PostEventSubscription:
                # name, version, subscription_id, error. The list endpoint below
                # answers with the id of endpoints.GetEventSubscription instead.
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"name": "livestream.status.updated", "version": 1, "subscription_id": "sub-1"},
                            {"name": "chat.message.sent", "version": 1, "subscription_id": "sub-2"},
                        ]
                    },
                )
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    api = make_mock_api(handler)
    wh = make_webhook(api=api)

    async def scenario():
        try:
            await wh._sync_subscriptions(["kick:xqc"])
        finally:
            await api.client.aclose()

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
            # The monitored channel must resolve. An unresolved slug makes the
            # reconcile skip the cleanup pass (fail safe).
            return httpx.Response(200, json={"data": [channel_data]})
        if request.url.path == "/public/v1/events/subscriptions":
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            # The monitored broadcaster keeps both events, so the
                            # reconcile creates nothing.
                            {
                                "id": "keep-1",
                                "app_id": "cid",
                                "broadcaster_user_id": 123,
                                "events": [{"name": "livestream.status.updated"}],
                            },
                            {
                                "id": "keep-2",
                                "app_id": "cid",
                                "broadcaster_user_id": 123,
                                "events": [{"name": "chat.message.sent"}],
                            },
                            # A broadcaster nobody monitors: both rows must go.
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
        try:
            await wh._sync_subscriptions(["kick:xqc"])
        finally:
            await api.client.aclose()

    asyncio.run(scenario())

    assert deletes == [["stale-1", "stale-2"]]
    # The stale channel is gone, and the surviving ids of xqc are kept.
    assert wh._subs == {"xqc": {"keep-1", "keep-2"}}


def test_disabled_webhook_ignores_deliveries(keypair):
    """Off means off: the receiver answers as if the route did not exist."""
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    recorder = FakeRecorder()
    config = base_config()
    config["kick"]["webhook"]["enabled"] = False
    wh = make_webhook(config=config, monitor=monitor, recorder=recorder, api=FakeKickAPI(public_pem))

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            body = live_event(is_live=True)
            live = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_LIVE),
            )
            chat = await client.post(
                "/kick/webhook",
                data=chat_event(),
                headers=_signed_headers(private_key, "m2", _fresh_ts(), chat_event(), wh.EVENT_CHAT),
            )
            return live.status, chat.status

    assert asyncio.run(scenario()) == (404, 404)
    assert monitor.online == []
    assert recorder.chat == []


def test_apply_state_stops_the_sync_and_drops_subscriptions_when_the_webhook_goes_off():
    """Off must stop the reconcile and delete the subscriptions it created."""
    deletes = []

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_response(request)
        if request.url.path == "/public/v1/events/subscriptions" and request.method == "DELETE":
            deletes.append([v for k, v in request.url.params.multi_items() if k == "id"])
            return httpx.Response(200, json={"data": []})
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    config = enabled_config()
    api = make_mock_api(handler)
    wh = make_webhook(config=config, api=api)
    wh._subs = {"xqc": {"sub-1", "sub-2"}}

    async def scenario():
        try:
            await wh.apply_state()
            assert wh._sync_task is not None
            wh._config.kick.webhook.enabled = False
            await wh.apply_state()
            assert wh._sync_task is None
            # apply_state awaits the deletes, so they are done when it returns.
            assert deletes
        finally:
            await wh.close()
            await api.client.aclose()

    asyncio.run(scenario())
    assert [sorted(ids) for ids in deletes] == [["sub-1", "sub-2"]]  # a set, so order varies
    assert wh._subs == {}
    assert wh.listening_needed()  # the endpoint stays on for the control API


class CountingAPI:
    """Counts the sync runs, and signals the test at the requested run count."""

    def __init__(self, until=2):
        self.n = 0
        self.until = until
        self.ran = asyncio.Event()

    async def get_channel_statuses(self, slugs):
        self.n += 1
        if self.n >= self.until:
            self.ran.set()
        return {}

    async def list_event_subscriptions(self):
        return []


def _drive_sync_loop(wh, api):
    """Run the sync loop until the API served ``api.until`` runs, then close."""

    async def scenario():
        await wh.apply_state()
        await asyncio.wait_for(api.ran.wait(), timeout=5)
        await wh.close()

    asyncio.run(scenario())


def test_sync_loop_runs_on_interval_cadence():
    config = enabled_config(monitoring_interval=0.01)
    api = CountingAPI(until=3)
    wh = make_webhook(config=config, api=api)

    _drive_sync_loop(wh, api)

    assert api.n >= 3


def test_reconcile_failure_notifies_once_and_clears_on_success():
    config = enabled_config(monitoring_interval=0.01)
    notifier = FakeNotifier()
    api = CountingAPI(until=5)

    async def failing_statuses(slugs):
        api.n += 1
        if api.n >= api.until:
            api.ran.set()
        if api.n < 3:
            msg = "boom"
            raise httpx.ConnectError(msg)
        return {}

    api.get_channel_statuses = failing_statuses
    wh = make_webhook(config=config, api=api, notifier=notifier)

    _drive_sync_loop(wh, api)

    # The fourth run succeeded, and run five proves the flag was cleared.
    assert api.n >= 5
    assert len(notifier.messages) == 1  # sync failure notified once, no setup confirm
    assert "Kick webhook subscriptions out of sync" in notifier.messages[0]
    assert wh._sync_failed_notified is False  # flag cleared after the last success


def test_sync_failure_logged_once_per_episode(caplog):
    config = enabled_config(monitoring_interval=0.01)
    notifier = FakeNotifier()
    api = CountingAPI(until=4)

    async def always_fails(slugs):
        api.n += 1
        if api.n >= api.until:
            api.ran.set()
        msg = "boom"
        raise httpx.ConnectError(msg)

    api.get_channel_statuses = always_fails
    wh = make_webhook(config=config, api=api, notifier=notifier)

    with caplog.at_level("DEBUG", logger="stream_archive.kick_webhook"):
        _drive_sync_loop(wh, api)

    errors = [r for r in caplog.records if "subscription sync failed" in r.getMessage()]
    debugs = [r for r in caplog.records if "subscription sync still failing" in r.getMessage()]
    assert len(errors) == 1
    assert len(debugs) >= 1


def _server_error_500():
    request = httpx.Request("GET", "https://api.kick.com/public/v1/events/subscriptions")
    response = httpx.Response(500, request=request)
    return httpx.HTTPStatusError("Server error '500 Internal Server Error'", request=request, response=response)


class FakeClock:
    """A monotonic clock that only the test moves.

    The sync-failure delay is measured in seconds. A stepped clock keeps the
    delay tests independent of the speed of the machine that runs them.
    """

    def __init__(self, start=1000.0):
        self.value = start

    def time(self):
        return self.value

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ScriptedAPI:
    """Fails with a 500 for the scripted entries, then succeeds.

    Each call steps the test clock, so a scripted number of runs means a
    known number of elapsed seconds.
    """

    def __init__(self, failures, clock, step, until):
        self.failures = failures
        self.clock = clock
        self.step = step
        self.until = until
        self.n = 0
        self.ran = asyncio.Event()

    async def get_channel_statuses(self, slugs):
        self.n += 1
        self.clock.advance(self.step)
        if self.n >= self.until:
            self.ran.set()
        if self.n <= len(self.failures) and self.failures[self.n - 1]:
            raise _server_error_500()
        return {}

    async def list_event_subscriptions(self):
        return []


def test_sync_failure_5xx_stays_silent_until_delay_elapses(monkeypatch):
    # A Kick-side 500 must not notify while it is shorter than the delay.
    monkeypatch.setattr("stream_archive.kick_webhook._SYNC_SERVER_ERROR_DELAY_S", 3600)
    config = enabled_config(monitoring_interval=0.01)
    notifier = FakeNotifier()
    clock = FakeClock()
    monkeypatch.setattr("stream_archive.kick_webhook.time", clock)
    # Each run moves the clock by a tenth of a second: 20 failing runs stay far
    # below the delay.
    api = ScriptedAPI([True] * 100, clock=clock, step=0.1, until=20)
    wh = make_webhook(config=config, api=api, notifier=notifier)

    _drive_sync_loop(wh, api)

    assert api.n >= 20
    assert notifier.messages == []
    assert wh._sync_failed_notified is False
    assert wh._sync_failing_since is not None  # episode timer armed


def test_sync_failure_5xx_notifies_after_delay_and_once_per_episode(monkeypatch):
    monkeypatch.setattr("stream_archive.kick_webhook._SYNC_SERVER_ERROR_DELAY_S", 0.02)
    config = enabled_config(monitoring_interval=0.01)
    notifier = FakeNotifier()
    clock = FakeClock()
    monkeypatch.setattr("stream_archive.kick_webhook.time", clock)
    # Each run moves the clock by 0.05s, so the second failing run of an
    # episode outlives the 0.02s delay and notifies once. The successful run
    # in the middle resets the timer, so the second episode notifies again.
    api = ScriptedAPI([True] * 3 + [False] + [True] * 3, clock=clock, step=0.05, until=8)
    wh = make_webhook(config=config, api=api, notifier=notifier)

    _drive_sync_loop(wh, api)

    assert len(notifier.messages) == 2
    assert "Kick webhook subscriptions out of sync" in notifier.messages[0]
    assert "500 Internal Server Error" in notifier.messages[0]


def test_sync_failure_5xx_short_episodes_never_notify(monkeypatch):
    # Recovery resets the episode timer. Brief blips stay silent when each
    # failing run is shorter than the delay, even across several episodes.
    monkeypatch.setattr("stream_archive.kick_webhook._SYNC_SERVER_ERROR_DELAY_S", 0.5)
    config = enabled_config(monitoring_interval=0.01)
    notifier = FakeNotifier()
    clock = FakeClock()
    monkeypatch.setattr("stream_archive.kick_webhook.time", clock)
    # Three failing runs move the clock by 0.15s, below the 0.5s delay.
    api = ScriptedAPI([True] * 3 + [False] + [True] * 3, clock=clock, step=0.05, until=8)
    wh = make_webhook(config=config, api=api, notifier=notifier)

    _drive_sync_loop(wh, api)

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


@pytest.mark.parametrize(
    "offset",
    [
        -_VERIFY_WINDOW_S - 1,
        -_VERIFY_WINDOW_S,
        -_VERIFY_WINDOW_S + 1,
        _VERIFY_WINDOW_S - 1,
        _VERIFY_WINDOW_S,
        _VERIFY_WINDOW_S + 1,
    ],
)
def test_timestamp_freshness_boundary(keypair, monkeypatch, offset):
    """The window admits |now - event_time| <= _VERIFY_WINDOW_S and rejects the rest.

    The clock is frozen, so the boundary is exact. A live clock advances
    between the request and the check and makes the boundary cases flaky.
    """
    private_key, public_pem = keypair
    fixed_now = 1_700_000_000.0
    monkeypatch.setattr(
        "stream_archive.kick_webhook.time",
        SimpleNamespace(time=lambda: fixed_now, monotonic=time.monotonic),
    )
    monitor = FakeMonitor()
    wh = make_webhook(monitor=monitor, api=FakeKickAPI(public_pem))
    body = live_event(is_live=True)
    timestamp = str(int(fixed_now) + offset)
    expected = 200 if abs(offset) <= _VERIFY_WINDOW_S else 401

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", timestamp, body, wh.EVENT_LIVE),
            )
            assert resp.status == expected

    asyncio.run(scenario())
    assert len(monitor.online) == (1 if expected == 200 else 0)


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
            # The first request fetches the key, refetches, verifies against the
            # fresh key and rejects the signature: a fetched key that does not
            # verify means the delivery is not authentic.
            resp = await client.post("/kick/webhook", data=body, headers=headers)
            assert resp.status == 401
            for _ in range(4):
                # The refetch window is closed, so the receiver cannot tell a
                # forged signature from a rotated key and must stay retryable.
                resp = await client.post("/kick/webhook", data=body, headers=headers)
                assert resp.status == 503
            # The first request fetches the key once and refetches once.
            # The 60s negative cache keeps the other 4 requests purely local.
            assert api.fetch_count == 2
            # After the refetch window elapses, one more refetch is allowed and
            # the answer is a verdict again.
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
            # The limiter charges the first request of an unseen key, so a
            # capacity-2 bucket admits two requests and rejects the rest.
            assert statuses == [401, 401, 429, 429]

    asyncio.run(scenario())


def test_rate_limiter_caps_unseen_keys_per_window(monkeypatch):
    """A flood of distinct addresses must not enter the bucket table for ever."""
    clock = FakeClock()
    monkeypatch.setattr("stream_archive.kick_webhook.time", clock)
    limiter = _RateLimiter(max_requests=2, window_s=60, max_new_keys=2)

    assert limiter.allow("a") is True
    assert limiter.allow("b") is True
    assert limiter.allow("c") is False  # over the cap for this window
    # The next window admits new keys again.
    clock.advance(60)
    assert limiter.allow("c") is True


def test_rate_limiter_evicts_the_least_recently_used_key(monkeypatch):
    """A full table drops the key that was idle the longest, not the newest."""
    clock = FakeClock()
    monkeypatch.setattr("stream_archive.kick_webhook.time", clock)
    limiter = _RateLimiter(max_requests=3, window_s=60, max_keys=2, max_new_keys=10)

    assert limiter.allow("a") is True
    assert limiter.allow("b") is True
    assert limiter.allow("a") is True  # touching "a" makes "b" the oldest
    assert limiter.allow("c") is True  # the full table drops "b"

    assert list(limiter._buckets) == ["a", "c"]


class _StalledRequest:
    """A request whose body never arrives: the slow-body flood shape.

    Nothing but the attributes ``_handle`` reads is defined, so the request
    can be driven directly without a socket. Both the streaming read and the
    whole-body read are provided, so the same stub stalls either shape.
    """

    def __init__(self, remote="10.0.0.1"):
        self.headers = {}
        self.content_length = 4096
        self.remote = remote
        self.released = asyncio.Event()
        self.content = SimpleNamespace(read=self._read)

    async def _read(self, _size):
        await self.released.wait()
        return b""

    async def read(self):
        await self.released.wait()
        return b""


class FailingKeyAPI(FakeKickAPI):
    """A Kick API whose public-key fetch always fails."""

    def __init__(self):
        super().__init__(public_key_pem=None)
        self.failures = 0

    async def get_public_key(self, force=False):
        self.failures += 1
        msg = "kick public key unavailable"
        raise RuntimeError(msg)


def test_relabelled_chat_event_does_not_change_recording_state(keypair):
    """The unsigned type header must not turn a chat body into a stop.

    The signature covers message-id, timestamp and body only, so a body must
    confirm the action its header claims. A chat body has no is_live field.
    """
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    recorder = FakeRecorder()
    wh = make_webhook(config=base_config(), monitor=monitor, recorder=recorder, api=FakeKickAPI(public_pem))
    body = chat_event()

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_LIVE),
            )
            assert resp.status == 200

    asyncio.run(scenario())
    assert monitor.offline == []
    assert monitor.online == []
    assert recorder.chat == []


def test_relabelled_live_event_is_not_written_to_chat(keypair):
    """The reverse direction: a livestream body must not become a chat entry."""
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    recorder = FakeRecorder()
    wh = make_webhook(config=base_config(), monitor=monitor, recorder=recorder, api=FakeKickAPI(public_pem))
    body = live_event(is_live=False)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_CHAT),
            )
            assert resp.status == 200

    asyncio.run(scenario())
    assert recorder.chat == []
    assert monitor.offline == []


def test_livestream_event_without_a_boolean_state_is_ignored(keypair):
    """A livestream body with no is_live field must not read as 'stream ended'."""
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    wh = make_webhook(config=base_config(), monitor=monitor, api=FakeKickAPI(public_pem))
    body = json.dumps({"broadcaster": {"channel_slug": "xqc"}}).encode()

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_LIVE),
            )
            assert resp.status == 200

    asyncio.run(scenario())
    assert monitor.offline == []
    assert monitor.online == []


def test_non_finite_timestamp_is_rejected(keypair):
    """'nan' parses as a float, and every comparison against it is false."""
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    recorder = FakeRecorder()
    wh = make_webhook(config=base_config(), monitor=monitor, recorder=recorder, api=FakeKickAPI(public_pem))
    body = chat_event()

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", "nan", body, wh.EVENT_CHAT),
            )
            assert resp.status == 401

    asyncio.run(scenario())
    assert recorder.chat == []


def test_failed_key_fetch_is_not_repeated_for_every_request(keypair):
    """A cold key cache plus a failing Kick API must not cost one call per request.

    The answer must stay retryable: the request was not judged either way, so
    a permanent 401 would make the sender drop a delivery it should repeat.
    """
    private_key, _ = keypair
    api = FailingKeyAPI()
    wh = make_webhook(config=base_config(), monitor=FakeMonitor(), api=api)
    body = chat_event()

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            for index in range(5):
                resp = await client.post(
                    "/kick/webhook",
                    data=body,
                    headers=_signed_headers(private_key, f"m{index}", _fresh_ts(), body, wh.EVENT_CHAT),
                )
                assert resp.status == 503
                assert resp.headers["Retry-After"]

    asyncio.run(scenario())
    assert api.failures == 1


def test_unverified_requests_do_not_consume_the_dispatch_budget(keypair):
    """Slow unauthenticated bodies must not refuse a genuine signed delivery."""
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    recorder = FakeRecorder()
    wh = make_webhook(config=base_config(), monitor=monitor, recorder=recorder, api=FakeKickAPI(public_pem))
    body = chat_event()

    async def scenario():
        stalls = [asyncio.create_task(wh._handle(_StalledRequest())) for _ in range(16)]
        await asyncio.sleep(0.05)  # let every stall reach the body read
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_CHAT),
            )
            assert resp.status == 200
        for stall in stalls:
            stall.cancel()
        await asyncio.gather(*stalls, return_exceptions=True)

    asyncio.run(scenario())
    assert len(recorder.chat) == 1


class _SegmentedRequest:
    """A request whose body arrives in several pieces, like a real delivery.

    ``request.content.read(n)`` returns as soon as any data is buffered, so a
    receiver that reads once and verifies sees a truncated body.
    """

    def __init__(self, pieces, headers, remote="10.0.0.2"):
        self._pieces = list(pieces)
        self.headers = headers
        self.content_length = sum(len(p) for p in pieces)
        self.remote = remote
        self.content = SimpleNamespace(read=self._read)

    async def _read(self, size):
        if not self._pieces:
            return b""
        return self._pieces.pop(0)[:size]


def test_body_split_across_segments_is_verified_whole(keypair):
    """A delivery that arrives in several pieces must not be read truncated.

    Reading once returned only the first buffered chunk, so the signature was
    checked against a partial body and a genuine event was answered 401.
    """
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    recorder = FakeRecorder()
    wh = make_webhook(config=base_config(), monitor=monitor, recorder=recorder, api=FakeKickAPI(public_pem))
    body = chat_event()
    mid = len(body) // 2
    pieces = [body[:mid], body[mid:]]
    headers = _signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_CHAT)

    async def scenario():
        response = await wh._handle(_SegmentedRequest(pieces, headers))
        assert response.status == 200

    asyncio.run(scenario())
    assert len(recorder.chat) == 1


def test_missing_public_key_asks_for_a_redelivery(keypair):
    """A 200 without a key is not a verdict on the signature.

    The cache stays cold, so every genuine delivery would otherwise be
    answered 401, which a sender treats as permanent.
    """
    private_key, _ = keypair
    api = FakeKickAPI(public_key_pem=None)  # a fetch that returns no key
    wh = make_webhook(config=base_config(), monitor=FakeMonitor(), api=api)
    body = chat_event()

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_CHAT),
            )
            assert resp.status == 503
            assert resp.headers["Retry-After"]

    asyncio.run(scenario())


def test_key_rotation_refetch_failure_stays_retryable(keypair):
    """The forced rotation refetch is a fetch too: its failure is not a verdict."""
    private_key, public_pem = keypair
    monitor = FakeMonitor()
    recorder = FakeRecorder()
    wh = make_webhook(config=base_config(), monitor=monitor, recorder=recorder, api=FakeKickAPI(public_pem))
    body = chat_event()

    async def failing_force(force=False):
        if force:
            msg = "kick api unreachable"
            raise RuntimeError(msg)
        return "not-the-signing-key"  # the cached key cannot verify this delivery

    wh._api.get_public_key = failing_force
    wh._api.has_public_key = lambda: True

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            resp = await client.post(
                "/kick/webhook",
                data=body,
                headers=_signed_headers(private_key, "m1", _fresh_ts(), body, wh.EVENT_CHAT),
            )
            assert resp.status == 503

    asyncio.run(scenario())
    assert recorder.chat == []


def test_a_concurrent_cold_cache_burst_makes_one_key_fetch(keypair):
    """The cold-cache window is claimed before the await, not after it.

    Verification now runs before the dispatch permit, so nothing else bounds
    concurrent fetches: a check-then-act window would let a burst each issue
    its own outbound Kick API call.
    """
    private_key, _ = keypair
    calls = []

    class CountingAPI(FakeKickAPI):
        async def get_public_key(self, force=False):
            calls.append(force)
            await asyncio.sleep(0.05)  # a real fetch takes time
            return None  # no key: the cache stays cold

    api = CountingAPI(public_key_pem=None)
    wh = make_webhook(config=base_config(), monitor=FakeMonitor(), api=api)
    body = chat_event()

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/kick/webhook",
                        data=body,
                        headers=_signed_headers(private_key, f"m{i}", _fresh_ts(), body, wh.EVENT_CHAT),
                    )
                    for i in range(5)
                ]
            )
            assert {r.status for r in responses} == {503}

    asyncio.run(scenario())
    assert len(calls) == 1, f"the burst must share one fetch, got {len(calls)}"


def test_empty_key_body_is_rejected_as_too_large(keypair):
    """The drain still enforces the size cap on a body that never ends."""
    private_key, public_pem = keypair
    wh = make_webhook(config=base_config(), monitor=FakeMonitor(), api=FakeKickAPI(public_pem))
    headers = {
        "Kick-Event-Type": wh.EVENT_CHAT,
        "Kick-Event-Message-Id": "m1",
        "Kick-Event-Message-Timestamp": _fresh_ts(),
        "Kick-Event-Signature": "AAAA",
    }
    big = _SegmentedRequest([b"x" * 4096] * 40, headers)  # 160 KiB, over the 64 KiB cap

    async def scenario():
        response = await wh._handle(big)
        assert response.status == 413

    asyncio.run(scenario())
