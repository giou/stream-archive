import asyncio

import httpx

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
    api = TwitchAPI(AppConfig.model_validate(base_config()))
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return api


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
        assert await api._get_token() == "tok-1"
        assert await api._get_token() == "tok-1"

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
        return await asyncio.gather(*[api._get_token() for _ in range(5)])

    assert asyncio.run(scenario()) == ["tok-1"] * 5
    assert calls["tokens"] == 1
