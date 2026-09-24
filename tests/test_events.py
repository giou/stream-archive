"""Tests for the persistent operator event feed.

The feed keeps the newest entries in memory and appends each one to
events.jsonl, so a restart restores them.
"""

from __future__ import annotations

import json

from stream_archive import events
from stream_archive.events import _FILE_MAX_LINES, _MAX_EVENTS


def _entry(n: int) -> dict[str, object]:
    return {"ts": 1000.0 + n, "kind": "notice", "channel": None, "text": f"event {n}"}


def test_record_appends_jsonl_and_truncates(tmp_path):
    events.reset()
    events.load(tmp_path / "events.jsonl")
    events.record("live", "twitch:channel1", "Title · Game")
    events.record("notice", None, "x" * 500)
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["kind"] == "live"
    assert first["channel"] == "twitch:channel1"
    assert len(second["text"]) == 300
    events.reset()


def test_load_restores_entries_newest_first(tmp_path):
    events.reset()
    path = tmp_path / "events.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for n in range(3):
            f.write(json.dumps(_entry(n)) + "\n")
    events.load(path)
    try:
        listed = events.list_events()
        assert [e["text"] for e in listed] == ["event 2", "event 1", "event 0"]
    finally:
        events.reset()


def test_load_skips_bad_lines(tmp_path):
    events.reset()
    path = tmp_path / "events.jsonl"
    path.write_text(
        "not json{{{\n"
        + json.dumps(["a", "list"])
        + "\n"
        + json.dumps({"kind": "notice"})
        + "\n"
        + json.dumps(_entry(1))
        + "\n",
        encoding="utf-8",
    )
    events.load(path)
    try:
        assert [e["text"] for e in events.list_events()] == ["event 1"]
    finally:
        events.reset()


def test_load_compacts_long_file(tmp_path):
    events.reset()
    path = tmp_path / "events.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for n in range(_FILE_MAX_LINES + 50):
            f.write(json.dumps(_entry(n)) + "\n")
    events.load(path)
    try:
        kept = path.read_text(encoding="utf-8").splitlines()
        assert len(kept) == _MAX_EVENTS
        assert json.loads(kept[-1])["text"] == f"event {_FILE_MAX_LINES + 49}"
        assert len(events.list_events(limit=_MAX_EVENTS)) == _MAX_EVENTS
    finally:
        events.reset()


def test_record_without_file_stays_memory_only(tmp_path):
    events.reset()
    try:
        events.record("notice", None, "hello")
        assert [e["text"] for e in events.list_events()] == ["hello"]
        assert not (tmp_path / "events.jsonl").exists()
    finally:
        events.reset()


def test_entries_survive_restart(tmp_path):
    """Record, drop all memory state, load again: the feed returns."""
    events.reset()
    path = tmp_path / "events.jsonl"
    events.load(path)
    events.record("offline", "kick:channel1", "stream ended")
    events.reset()
    events.load(path)
    try:
        listed = events.list_events()
        assert [(e["kind"], e["channel"]) for e in listed] == [("offline", "kick:channel1")]
    finally:
        events.reset()
