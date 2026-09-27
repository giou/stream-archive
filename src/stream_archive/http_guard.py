"""Shared guards for the private-listener handlers.

The control API and the web panel bound request bodies and per-address
failure budgets the same way. The logic lives here once, so the two
surfaces cannot drift apart: a drift here is a security hole, with one
surface rate-limiting guesses while the other stays open.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from aiohttp import web


class FailBudget:
    """Per-address failure budget over a sliding window.

    A success clears the address. Failures older than the window drop
    out, so a slow trickle never locks an address out.
    """

    def __init__(self, max_fails: int, window_s: float) -> None:
        self._max_fails = max_fails
        self._window_s = window_s
        self._fails: dict[str, deque[float]] = {}

    def allowed(self, key: str) -> bool:
        """True when the address still holds budget."""
        now = time.monotonic()
        fails = self._fails.get(key)
        if fails is None:
            return True
        while fails and now - fails[0] > self._window_s:
            fails.popleft()
        if not fails:
            del self._fails[key]
            return True
        return len(fails) < self._max_fails

    def record(self, key: str) -> None:
        """Spend one unit of budget for the address."""
        fails = self._fails.setdefault(key, deque())
        fails.append(time.monotonic())
        while len(fails) > self._max_fails:
            fails.popleft()

    def clear(self, key: str) -> None:
        """Restore the full budget of the address (after a success)."""
        self._fails.pop(key, None)


async def read_json_object(request: web.Request, limit: int, error: Callable[[int, str], Exception]) -> dict[str, Any]:
    """JSON object body of a bounded request, or raise ``error`` (400/413).

    The caller passes its own error type, so the API and the panel keep
    their own responses while sharing the parse.
    """
    if request.content_length is not None and request.content_length > limit:
        raise error(413, "request body too large")
    raw = await request.content.read(limit + 1)
    if len(raw) > limit:
        raise error(413, "request body too large")
    if not raw:
        raise error(400, "JSON body required")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # json.loads decodes the bytes itself, so invalid UTF-8 raises
        # UnicodeDecodeError, not JSONDecodeError. Both mean a bad body.
        raise error(400, f"invalid JSON body: {e}") from e
    if not isinstance(payload, dict):
        raise error(400, "JSON body must be an object")
    return payload
