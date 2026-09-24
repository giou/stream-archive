"""Operator event feed for the web panel.

The notifier sends every alert to Telegram. This module keeps the same
alerts in a bounded ring buffer, so the web panel can show them without
the bot. Entries are plain operator text, newest last in the buffer.
Each entry also appends to ``events.jsonl`` next to config.json, so the
feed survives restarts. The file holds plain text an admin can read.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

#: Bound of the buffer. Old entries drop off silently.
_MAX_EVENTS = 200

#: Lines past which the file compacts to the newest entries.
_FILE_MAX_LINES = 1000

#: Kept text length of one entry. Titles come from streamers.
_MAX_TEXT_LEN = 300

#: Name of the feed file, next to config.json.
_EVENTS_FILENAME = "events.jsonl"

_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)

#: JSONL file of the feed, set by load(). None keeps memory only (tests).
_path: Path | None = None

#: Lines in the file. load() counts them, record() adds to the count.
_stored_lines = 0


def feed_path(workdir: Path) -> Path:
    """File of the feed inside ``workdir``."""
    return workdir / _EVENTS_FILENAME


def load(path: Path) -> None:
    """Read stored events into memory and compact the file. Never raises."""
    global _path, _stored_lines
    _path = path
    _stored_lines = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeDecodeError:
        return
    found: list[dict[str, Any]] = []
    for line in lines:
        entry = _coerce_line(line)
        if entry is not None:
            found.append(entry)
    _stored_lines = len(lines)
    _events.extend(found[-_MAX_EVENTS:])
    if _stored_lines > _FILE_MAX_LINES:
        _compact()


def _coerce_line(line: str) -> dict[str, Any] | None:
    """One file line as a feed entry, or None when the line is bad."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(entry, dict):
        return None
    if not isinstance(entry.get("text"), str):
        return None
    try:
        ts = float(entry.get("ts", 0.0))
    except TypeError, ValueError:
        ts = 0.0
    return {
        "ts": ts,
        "kind": entry.get("kind"),
        "channel": entry.get("channel"),
        "text": str(entry["text"])[:_MAX_TEXT_LEN],
    }


def record(kind: str, channel: str | None, text: str) -> None:
    """Append one event. Kinds: live, offline, notice, config. Never raises."""
    global _stored_lines
    with contextlib.suppress(Exception):
        entry = {"ts": time.time(), "kind": kind, "channel": channel, "text": text[:_MAX_TEXT_LEN]}
        _events.append(entry)
        if _path is not None:
            with open(_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            _stored_lines += 1
            if _stored_lines > _FILE_MAX_LINES:
                _compact()


def _compact() -> None:
    """Rewrite the file with the newest entries. Never raises."""
    global _stored_lines
    with contextlib.suppress(Exception):
        if _path is None:
            return
        tmp = Path(str(_path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for entry in _events:
                f.write(json.dumps(entry) + "\n")
        os.replace(tmp, _path)
        _stored_lines = len(_events)


def list_events(limit: int = 100) -> list[dict[str, Any]]:
    """Newest-first events, at most ``limit`` entries."""
    limit = min(max(limit, 1), _MAX_EVENTS)
    return list(reversed(list(_events)))[0:limit]


def clear() -> None:
    """Drop every event and truncate the file. Keeps the file bound."""
    global _stored_lines
    _events.clear()
    _stored_lines = 0
    if _path is None:
        return
    with contextlib.suppress(OSError):
        _path.write_text("", encoding="utf-8")


def reset() -> None:
    """Drop every event and unbind the file. Tests only."""
    global _path, _stored_lines
    _events.clear()
    _path = None
    _stored_lines = 0
