import asyncio
import json

import httpx
import pytest

from stream_archive.config import AppConfig
from stream_archive.kick_api import _USER_AGENT, KickAPI


def base_config():
    return {
        "telegram_user_id": 12345,
        "bot_telegram_api": "bot_token",
        "twitch_client_id": "client_id",
        "twitch_client_secret": "client_secret",
        "channels": ["ch"],
        "proxy_list": ["httpproxy://user:pass@host:port"],
        "monitoring_interval": 60,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "recording_dir": "recordings",
        "kick": {
            "client_id": "cid",
            "client_secret": "csec",
            "record_chat": True,
            "webhook": {"enabled": False},
        },
    }


def make_api(handler):
    api = KickAPI(AppConfig.model_validate(base_config()))
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"User-Agent": _USER_AGENT})
    return api


def token_handler(request):
    assert request.url.path == "/oauth/token"
    assert request.method == "POST"
    form = request.content.decode()
    assert "grant_type=client_credentials" in form
    assert "client_id=cid" in form
    assert "client_secret=csec" in form
    return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})


def test_token_fetched_and_cached():
    calls = {"tokens": 0}

    def handler(request):
        if request.url.path == "/oauth/token":
            calls["tokens"] += 1
            return token_handler(request)
        return httpx.Response(200, json={"data": []})

    api = make_api(handler)

    async def scenario():
        assert await api._get_token() == "tok-1"
        assert await api._get_token() == "tok-1"
        assert await api._get_token() == "tok-1"

    asyncio.run(scenario())
    assert calls["tokens"] == 1


def test_get_channel_statuses_maps_live_offline_unknown():
    channels = {
        "xqc": {
            "slug": "xqc",
            "stream_title": "Big stream",
            "category": {"name": "Just Chatting"},
            "stream": {"is_live": True},
            "broadcaster_user_id": 111,
        },
        "offline1": {
            "slug": "offline1",
            "stream_title": None,
            "category": None,
            "stream": {"is_live": False},
            "broadcaster_user_id": 222,
        },
    }

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        assert request.url.path == "/public/v1/channels"
        assert request.headers["Authorization"] == "Bearer tok-1"
        slugs = [v for k, v in request.url.params.multi_items() if k == "slug"]
        assert slugs == ["xqc", "offline1", "nope"]
        data = [channels[s] for s in slugs if s in channels]
        return httpx.Response(200, json={"data": data})

    api = make_api(handler)
    result = asyncio.run(api.get_channel_statuses(["xqc", "offline1", "nope"]))

    assert result == {
        "xqc": {"title": "Big stream", "game": "Just Chatting", "is_live": True, "broadcaster_user_id": 111},
        "offline1": {"title": "", "game": "", "is_live": False, "broadcaster_user_id": 222},
    }


def test_get_channel_statuses_empty_list_returns_empty():
    api = make_api(lambda request: pytest.fail("no request expected"))
    assert asyncio.run(api.get_channel_statuses([])) == {}


def test_get_channel_statuses_chunks_over_50_slugs():
    requests = []

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        slugs = [v for k, v in request.url.params.multi_items() if k == "slug"]
        requests.append(slugs)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "slug": s,
                        "stream_title": None,
                        "category": None,
                        "stream": {"is_live": False},
                        "broadcaster_user_id": i,
                    }
                    for i, s in enumerate(slugs)
                ]
            },
        )

    api = make_api(handler)
    slugs = [f"user{i:03d}" for i in range(110)]
    result = asyncio.run(api.get_channel_statuses(slugs))

    assert len(requests) == 3
    assert [len(r) for r in requests] == [50, 50, 10]
    assert len(result) == 110


def test_get_public_key_cached():
    calls = {"n": 0}
    pem = "-----BEGIN PUBLIC KEY-----\nAAA\n-----END PUBLIC KEY-----\n"

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        calls["n"] += 1
        # live API nests the PEM under data.public_key
        return httpx.Response(200, json={"data": {"public_key": pem}})

    api = make_api(handler)

    async def scenario():
        key1 = await api.get_public_key()
        key2 = await api.get_public_key()
        assert key1 == pem
        assert key1 == key2
        api.clear_public_key_cache()
        key3 = await api.get_public_key()
        assert key3 == key1

    asyncio.run(scenario())
    assert calls["n"] == 2


def test_list_event_subscriptions_filters_foreign_app():
    subs = [
        {"id": "ours-1", "app_id": "cid", "broadcaster_user_id": 1},
        {"id": "other-1", "app_id": "other-app", "broadcaster_user_id": 1},
        {"id": "no-app", "broadcaster_user_id": 2},
    ]

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"data": subs})

    api = make_api(handler)
    result = asyncio.run(api.list_event_subscriptions())

    # Fail closed. Return only subscriptions provably owned by this app, and
    # never treat a missing app_id as ours. Reconcile deletes subscriptions
    # for unmonitored broadcasters.
    assert [s["id"] for s in result] == ["ours-1"]


def test_list_event_subscriptions_without_client_id_returns_empty():
    subs = [{"id": "ours-1", "app_id": "cid", "broadcaster_user_id": 1}]

    def handler(request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        return httpx.Response(200, json={"data": subs})

    config = base_config()
    del config["kick"]["client_id"]  # no client_id
    api = KickAPI(AppConfig.model_validate(config))
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert asyncio.run(api.list_event_subscriptions()) == []


def test_create_event_subscriptions_posts_documented_body():
    seen = {}

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        assert request.method == "POST"
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"subscription_id": "sub-1"},
                    {"subscription_id": "sub-2"},
                ]
            },
        )

    api = make_api(handler)
    result = asyncio.run(api.create_event_subscriptions(123, ["livestream.status.updated", "chat.message.sent"]))

    assert seen["body"] == {
        "broadcaster_user_id": 123,
        "events": [
            {"name": "livestream.status.updated", "version": 1},
            {"name": "chat.message.sent", "version": 1},
        ],
        "method": "webhook",
    }
    assert [i["subscription_id"] for i in result] == ["sub-1", "sub-2"]


def test_delete_event_subscriptions_sends_ids():
    seen = {}

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        assert request.method == "DELETE"
        seen["params"] = list(request.url.params.multi_items())
        return httpx.Response(200, json={"data": []})

    api = make_api(handler)
    asyncio.run(api.delete_event_subscriptions(["a", "b"]))

    assert seen["params"] == [("id", "a"), ("id", "b")]


def test_delete_event_subscriptions_empty_is_noop():
    api = make_api(lambda request: pytest.fail("no request expected"))
    asyncio.run(api.delete_event_subscriptions([]))


def test_http_errors_propagate(monkeypatch):
    monkeypatch.setattr("stream_archive.kick_api._RETRY_DELAYS", (0.0, 0.0))

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        return httpx.Response(500, json={"message": "boom"})

    api = make_api(handler)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(api.get_channel_statuses(["xqc"]))


def test_channels_request_sends_user_agent():
    seen = {}

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        seen["ua"] = request.headers.get("User-Agent")
        return httpx.Response(200, json={"data": []})

    api = make_api(handler)
    asyncio.run(api.get_channel_statuses(["xqc"]))

    assert seen["ua"] == "stream-archive"


def test_transient_status_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr("stream_archive.kick_api._RETRY_DELAYS", (0.0, 0.0))
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"data": []})

    api = make_api(handler)
    assert asyncio.run(api.get_channel_statuses(["xqc"])) == {}
    assert calls["n"] == 2


def test_transport_error_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr("stream_archive.kick_api._RETRY_DELAYS", (0.0, 0.0))
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"data": []})

    api = make_api(handler)
    assert asyncio.run(api.get_channel_statuses(["xqc"])) == {}
    assert calls["n"] == 2


def test_retries_exhausted_raises(monkeypatch):
    monkeypatch.setattr("stream_archive.kick_api._RETRY_DELAYS", (0.0, 0.0))
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        calls["n"] += 1
        return httpx.Response(503, request=request)

    api = make_api(handler)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(api.get_channel_statuses(["xqc"]))
    assert calls["n"] == 3


def test_client_error_not_retried():
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        calls["n"] += 1
        return httpx.Response(400, request=request)

    api = make_api(handler)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(api.get_channel_statuses(["xqc"]))
    assert calls["n"] == 1


def test_token_error_propagates():
    def handler(request):
        return httpx.Response(401, json={"message": "bad creds"})

    api = make_api(handler)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(api.get_channel_statuses(["xqc"]))


def test_concurrent_token_refresh_is_single_flight():
    """A burst of callers (webhook verifications run concurrently) must produce
    exactly one client_credentials POST."""
    calls = {"tokens": 0}

    def handler(request):
        calls["tokens"] += 1
        return token_handler(request)

    api = make_api(handler)

    async def scenario():
        return await asyncio.gather(*[api._get_token() for _ in range(5)])

    assert asyncio.run(scenario()) == ["tok-1"] * 5
    assert calls["tokens"] == 1


def test_get_public_key_keeps_cache_on_malformed_response():
    calls = {"n": 0}
    pem = "-----BEGIN PUBLIC KEY-----\nAAA\n-----END PUBLIC KEY-----\n"

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_handler(request)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"data": {"public_key": pem}})
        return httpx.Response(200, json={})  # malformed 200 response without any data

    api = make_api(handler)

    async def scenario():
        first = await api.get_public_key(force=True)
        second = await api.get_public_key(force=True)  # rotation refetch hits the malformed body
        return first, second

    first, second = asyncio.run(scenario())
    assert calls["n"] == 2
    assert first == pem
    assert second == pem  # known-good PEM survives a malformed response
