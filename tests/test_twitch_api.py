import asyncio

import httpx
import pytest

from stream_archive.config import AppConfig
from stream_archive.twitch_api import TwitchAPI


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


def make_api(handler):
    # Inject the mock client through the constructor: TwitchAPI owns the
    # client only when it creates it, so the tests close this one themselves.
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TwitchAPI(AppConfig.model_validate(base_config()), http=client)


def token_handler(request):
    assert request.url.path == "/oauth2/token"
    assert request.method == "POST"
    form = request.content.decode()
    assert "grant_type=client_credentials" in form
    assert "client_id=client_id" in form
    assert "client_secret=client_secret" in form
    return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})


def test_token_fetched_and_cached():
    calls = {"tokens": 0}

    def handler(request):
        if request.url.path == "/oauth2/token":
            calls["tokens"] += 1
            return token_handler(request)
        return httpx.Response(200, json={"data": []})

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

    api = make_api(handler)

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


def test_resolve_user_ids_splits_over_100_names_into_chunks():
    """105 names need two GETs. The merged map keeps the caller's own names."""
    logins = [f"user{i}" for i in range(104)] + ["MixedCase"]
    chunks = []

    def handler(request):
        if request.url.path == "/oauth2/token":
            return token_handler(request)
        assert request.url.path == "/helix/users"
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

    # One GET per chunk: 100 names, then the trailing 5.
    assert [len(chunk) for chunk in chunks] == [100, 5]
    assert [name for chunk in chunks for name in chunk] == logins
    assert resolved == {name: f"id-{name.lower()}" for name in logins}
    # A mixed-case configured name is a key of the result, not the canonical
    # lowercase login.
    assert resolved["MixedCase"] == "id-mixedcase"


def test_get_live_streams_splits_over_100_ids_into_chunks():
    """105 user ids need two GETs. The merged map holds every live stream."""
    user_ids = {f"ch{i}": f"u{i}" for i in range(105)}
    chunks = []

    def handler(request):
        if request.url.path == "/oauth2/token":
            return token_handler(request)
        assert request.url.path == "/helix/streams"
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

    assert [len(chunk) for chunk in chunks] == [100, 5]
    assert set(streams) == {f"u{i}" for i in range(105)}
    # The stream of the trailing chunk is keyed by its user id.
    assert streams["u104"]["title"] == "T-u104"
