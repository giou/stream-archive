"""Shared test fakes for the Stream Archive suite.

Helpers build valid configs and in-memory API doubles.
Tests import these fakes instead of copying local copies.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from stream_archive.config import AppConfig


def make_config(**overrides: Any) -> AppConfig:
    """Build a valid AppConfig with test defaults."""
    data: dict[str, Any] = {
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
        "kick": {"client_id": "cid", "client_secret": "cs"},
    }
    data.update(overrides)
    return AppConfig.model_validate(data)


class FakeTwitchAPI:
    """In-memory double for TwitchAPI."""

    def __init__(
        self,
        streams: Mapping[str, Any] | None = None,
        error: Exception | None = None,
        user_ids: dict[str, str] | None = None,
    ) -> None:
        self.streams = streams
        self.error = error
        self.user_ids = user_ids
        self.resolve_calls: list[list[str]] = []

    async def resolve_user_ids(self, channels: list[str]) -> dict[str, str]:
        self.resolve_calls.append(list(channels))
        return self.user_ids or {c: c for c in channels}

    async def get_live_streams(self, user_ids: Mapping[str, str]) -> dict[str, Any]:
        if self.error:
            raise self.error
        return dict(self.streams or {})

    async def get_stream(self, user_id: str) -> Any:
        streams = self.streams or {}
        return streams.get(user_id)

    async def aclose(self) -> None:
        return None


class FakeKickAPI:
    """In-memory double for KickAPI."""

    def __init__(
        self,
        statuses: Mapping[str, Any] | None = None,
        error: Exception | None = None,
        public_key: str | None = None,
    ) -> None:
        self.statuses = statuses
        self.error = error
        self.public_key = public_key

    async def get_channel_statuses(self, slugs: list[str]) -> dict[str, Any]:
        if self.error:
            raise self.error
        return dict(self.statuses or {})

    async def get_public_key(self) -> str:
        return self.public_key or "test-public-key"

    async def aclose(self) -> None:
        return None


def make_mock_http_client(handler: Any) -> httpx.AsyncClient:
    """Build an AsyncClient backed by an in-memory MockTransport."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
