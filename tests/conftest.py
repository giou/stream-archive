"""Shared test helpers for the Stream Archive suite.

``make_config`` builds a valid config from one set of test defaults, so the
tests do not copy those defaults into every file.
"""

from __future__ import annotations

from typing import Any

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
    for key, value in overrides.items():
        default = data.get(key)
        if isinstance(default, dict) and isinstance(value, dict):
            # Merge nested mappings, so a partial override keeps the defaults.
            data[key] = {**default, **value}
        else:
            data[key] = value
    return AppConfig.model_validate(data)
