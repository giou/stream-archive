import asyncio

import httpx
import pytest

from stream_archive.config import AppConfig
from stream_archive.twitch_api import _MAX_QUERY_ITEMS, TwitchAPI


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
    }


class YieldingTransport(httpx.AsyncBaseTransport):
    """A transport that turns the event loop before it answers.

    MockTransport calls its handler straight from the await, so a burst of
    callers can never overlap and the single-flight test would pass without a
    lock. The yields give the other callers a turn while the request is in
    flight.
    """

    def __init__(self, handler, yields=20):
        self._handler = handler
        self._yields = yields

    async def handle_async_request(self, request):
        for _ in range(self._yields):
            await asyncio.sleep(0)
        return self._handler(request)


def make_api(handler, transport=None):
    # Inject the mock client through the constructor: TwitchAPI owns the
    # client only when it creates it, so the tests close this one themselves.
    transport = httpx.MockTransport(handler) if transport is None else transport(handler)
    client = httpx.AsyncClient(transport=transport)
    return TwitchAPI(AppConfig.model_validate(base_config()), http=client)


def token_handler(request):
    assert request.url.path == "/oauth2/token"
    assert request.method == "POST"
    form = request.content.decode()
    assert "grant_type=client_credentials" in form
    assert "client_id=client_id" in form
    assert "client_secret=client_secret" in form
    return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})


def assert_auth_headers(request):
    """The Helix calls must carry the token and the client id of the app."""
    assert request.headers["Authorization"] == "Bearer tok-1"
    assert request.headers["Client-Id"] == "client_id"


def test_token_fetched_and_cached():
    calls = {"tokens": 0}

    def handler(request):
        # token_handler asserts the path, so any other request fails clearly.
        calls["tokens"] += 1
        return token_handler(request)

    api = make_api(handler)

    async def scenario():
        try:
            assert await api._get_token() == "tok-1"
            assert await api._get_token() == "tok-1"
        finally:
            await api.client.aclose()

    asyncio.run(scenario())
    assert calls["tokens"] == 1


def test_concurrent_token_refresh_is_single_flight():
    """A burst of callers must produce exactly one client_credentials POST."""
    calls = {"tokens": 0}

    def handler(request):
        calls["tokens"] += 1
        return token_handler(request)

    api = make_api(handler, transport=YieldingTransport)

    async def scenario():
        try:
            return await asyncio.gather(*[api._get_token() for _ in range(5)])
        finally:
            await api.client.aclose()

    assert asyncio.run(scenario()) == ["tok-1"] * 5
    assert calls["tokens"] == 1


def test_token_request_error_is_not_cached():
    """A failed token POST must raise and must not poison the cache."""
    calls = {"tokens": 0}

    def handler(request):
        calls["tokens"] += 1
        return httpx.Response(400, json={"message": "invalid client"})

    api = make_api(handler)

    async def scenario():
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await api._get_token()
            assert api._token is None
            assert calls["tokens"] == 1
            with pytest.raises(httpx.HTTPStatusError):
                await api._get_token()  # the failure is not cached: it retries
            assert calls["tokens"] == 2
        finally:
            await api.client.aclose()

    asyncio.run(scenario())


def test_short_lived_token_is_refetched_after_expiry():
    """A token inside the 60s safety margin must trigger a second POST."""
    tokens = []

    def handler(request):
        tokens.append("tok")
        return httpx.Response(200, json={"access_token": f"tok-{len(tokens)}", "expires_in": 30})

    api = make_api(handler)

    async def scenario():
        try:
            return [await api._get_token(), await api._get_token()]
        finally:
            await api.client.aclose()

    # expires_in=30 keeps the token inside the `now < expiry - 60` margin, so
    # every call refetches instead of serving the stale cached value.
    assert asyncio.run(scenario()) == ["tok-1", "tok-2"]


def test_resolve_user_ids_keeps_max_items_in_one_request():
    """Exactly _MAX_QUERY_ITEMS names still fit in one GET."""
    logins = [f"user{i}" for i in range(_MAX_QUERY_ITEMS)]
    chunks = []

    def handler(request):
        if request.url.path == "/oauth2/token":
            return token_handler(request)
        assert request.url.path == "/helix/users"
        assert_auth_headers(request)
        chunk = request.url.params.get_list("login")
        chunks.append(chunk)
        return httpx.Response(200, json={"data": [{"login": n, "id": f"id-{n}"} for n in chunk]})

    api = make_api(handler)

    async def scenario():
        try:
            return await api.resolve_user_ids(logins)
        finally:
            await api.client.aclose()

    resolved = asyncio.run(scenario())

    assert [len(chunk) for chunk in chunks] == [_MAX_QUERY_ITEMS]
    assert resolved == {name: f"id-{name}" for name in logins}


def test_resolve_user_ids_splits_over_max_items_into_chunks():
    """The names past the limit need a second GET. The map keeps the caller's names."""
    logins = [f"user{i}" for i in range(_MAX_QUERY_ITEMS + 4)] + ["MixedCase"]
    chunks = []

    def handler(request):
        if request.url.path == "/oauth2/token":
            return token_handler(request)
        assert request.url.path == "/helix/users"
        assert_auth_headers(request)
        chunk = request.url.params.get_list("login")
        chunks.append(chunk)
        # Twitch answers with the canonical lowercase login.
        return httpx.Response(200, json={"data": [{"login": n.lower(), "id": f"id-{n.lower()}"} for n in chunk]})

    api = make_api(handler)

    async def scenario():
        try:
            return await api.resolve_user_ids(logins)
        finally:
            await api.client.aclose()

    resolved = asyncio.run(scenario())

    # One GET per chunk: the first _MAX_QUERY_ITEMS names, then the rest.
    assert [len(chunk) for chunk in chunks] == [_MAX_QUERY_ITEMS, 5]
    assert [name for chunk in chunks for name in chunk] == logins
    assert resolved == {name: f"id-{name.lower()}" for name in logins}
    # A mixed-case configured name is a key of the result, not the canonical
    # lowercase login.
    assert resolved["MixedCase"] == "id-mixedcase"


def test_get_live_streams_splits_over_max_items_into_chunks():
    """The ids past the limit need a second GET. The map holds every live stream."""
    user_ids = {f"ch{i}": f"u{i}" for i in range(_MAX_QUERY_ITEMS + 5)}
    chunks = []

    def handler(request):
        if request.url.path == "/oauth2/token":
            return token_handler(request)
        assert request.url.path == "/helix/streams"
        assert_auth_headers(request)
        chunk = request.url.params.get_list("user_id")
        chunks.append(chunk)
        return httpx.Response(200, json={"data": [{"user_id": uid, "title": f"T-{uid}"} for uid in chunk]})

    api = make_api(handler)

    async def scenario():
        try:
            return await api.get_live_streams(user_ids)
        finally:
            await api.client.aclose()

    streams = asyncio.run(scenario())

    assert [len(chunk) for chunk in chunks] == [_MAX_QUERY_ITEMS, 5]
    assert set(streams) == {f"u{i}" for i in range(_MAX_QUERY_ITEMS + 5)}
    # The stream of the trailing chunk is keyed by its user id.
    assert streams[f"u{_MAX_QUERY_ITEMS + 4}"]["title"] == f"T-u{_MAX_QUERY_ITEMS + 4}"


def test_empty_input_asks_the_api_for_nothing():
    """An empty input needs no token and no request."""
    requests = []

    def handler(request):
        requests.append(request.url.path)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    api = make_api(handler)

    async def scenario():
        try:
            assert await api.resolve_user_ids([]) == {}
            assert await api.get_live_streams({}) == {}
        finally:
            await api.client.aclose()

    asyncio.run(scenario())
    assert requests == []


def test_create_eventsub_subscription_answers_a_non_json_body_with_an_empty_dict():
    """A handled status with a non-JSON body must not raise a parse error."""

    def handler(request):
        if request.url.path == "/oauth2/token":
            return token_handler(request)
        assert request.url.path == "/helix/eventsub/subscriptions"
        assert_auth_headers(request)
        # A proxy or a WAF can answer a handled status with an HTML page.
        return httpx.Response(409, content=b"<html>conflict</html>")

    api = make_api(handler)
    payload = {"type": "channel.follow", "version": "2", "condition": {"broadcaster_user_id": "1"}}

    async def scenario():
        try:
            return await api.create_eventsub_subscription(payload)
        finally:
            await api.client.aclose()

    assert asyncio.run(scenario()) == (409, {})


def test_create_eventsub_subscription_raises_for_an_unhandled_status():
    """A status that the callers do not handle must raise."""

    def handler(request):
        if request.url.path == "/oauth2/token":
            return token_handler(request)
        assert request.url.path == "/helix/eventsub/subscriptions"
        return httpx.Response(401, json={"message": "invalid token"})

    api = make_api(handler)
    payload = {"type": "channel.follow", "version": "2", "condition": {"broadcaster_user_id": "1"}}

    async def scenario():
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await api.create_eventsub_subscription(payload)
        finally:
            await api.client.aclose()

    asyncio.run(scenario())
