"""In-memory operator event feed for the web panel.

The notifier sends every alert to Telegram. This module keeps the same
alerts in a bounded ring buffer, so the web panel can show them without
the bot. Entries are plain operator text, newest last in the buffer.
"""

from __future__ import annotations

import contextlib
import time
from collections import deque
from typing import Any

#: Bound of the buffer. Old entries drop off silently.
_MAX_EVENTS = 200

#: Kept text length of one entry. Titles come from streamers.
_MAX_TEXT_LEN = 300

_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)


def record(kind: str, channel: str | None, text: str) -> None:
    """Append one event. Kinds: live, offline, notice. Never raises."""
    with contextlib.suppress(Exception):
        _events.append({"ts": time.time(), "kind": kind, "channel": channel, "text": text[:_MAX_TEXT_LEN]})


def list_events(limit: int = 100) -> list[dict[str, Any]]:
    """Newest-first events, at most ``limit`` entries."""
    limit = min(max(limit, 1), _MAX_EVENTS)
    return list(reversed(list(_events)))[0:limit]


def reset() -> None:
    """Drop every event. Tests only."""
    _events.clear()
