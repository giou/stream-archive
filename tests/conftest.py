"""Shared test helpers for the Stream Archive suite.

Helpers live here so the tests do not copy them into every file.

``make_config`` builds a valid config from one set of test defaults.
``read_file`` reads the config a test just changed. ``kb_labels`` reads
the button labels of a Telegram keyboard.
"""

from __future__ import annotations

import json
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


def read_file(tmp_path: Any) -> dict[str, Any]:
    """config.json in ``tmp_path`` as a dict, read back from disk."""
    return json.loads((tmp_path / "config.json").read_text())


def kb_labels(markup: Any) -> list[str]:
    """Button labels of a Telegram reply keyboard or inline keyboard."""
    data = markup.to_dict()
    rows = data.get("inline_keyboard") or data.get("keyboard")
    return [button["text"] for row in rows for button in row]
