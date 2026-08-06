import asyncio
import io
import os
import time
from datetime import datetime, timezone

from streamlink.exceptions import NoStreamsError

from src.twitch_recorder.recorder import Recorder, _sanitize_filename


def make_config(tmp_path):
    return {
        "output_mode": "disk",
        "recording_dir": str(tmp_path / "recordings"),
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "proxy_list": ["httpproxy://u:p@h:1"],
        "_workdir": tmp_path,
        "channels": ["ch"],
    }


class FakeStream:
    def open(self):
        return io.BytesIO()


def test_sanitize_filename_replaces_illegal_chars():
    name = 'a<b>c:d"e/f\\g|h?i*j'
    assert _sanitize_filename(name) == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_filename_truncates_to_200():
    name = "x" * 249 + "/"
    result = _sanitize_filename(name)
    assert len(result) == 200
    assert result == "x" * 200


def test_start_success(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("ch") is True
        assert "ch" in rec._recordings
        assert rec._recordings["ch"]["filepath"].startswith(str(tmp_path / "recordings" / "ch"))
        await rec.stop("ch")

    asyncio.run(scenario())


def test_start_nostreams_returns_false(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    def raise_nostreams(*a):
        raise NoStreamsError()

    monkeypatch.setattr(rec, "_resolve_stream", raise_nostreams)

    assert asyncio.run(rec.start("ch")) is False
    assert "ch" not in rec._recordings


def test_start_duplicate_resolves_once(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    calls = {"n": 0}

    def counting_resolve(*a):
        calls["n"] += 1
        return FakeStream(), "author", "Title", "Game"

    monkeypatch.setattr(rec, "_resolve_stream", counting_resolve)

    async def scenario():
        assert await rec.start("ch") is True
        assert await rec.start("ch") is True
        assert calls["n"] == 1
        await rec.stop("ch")

    asyncio.run(scenario())


def test_start_youtube_without_streamer_returns_false(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["output_mode"] = "youtube"
    rec = Recorder(config)  # youtube_streamer defaults to None
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    assert asyncio.run(rec.start("ch")) is False
    assert "ch" not in rec._recordings


def test_stop_returns_file_info(tmp_path):
    rec = Recorder(make_config(tmp_path))
    filepath = str(tmp_path / "rec.ts")
    with open(filepath, "wb") as f:
        f.write(b"\0" * 1048577)  # exactly 1 MiB + 1 byte
    rec._recordings["ch"] = {
        "tasks": [],
        "process": None,
        "youtube_info": None,
        "filepath": filepath,
    }

    result = asyncio.run(rec.stop("ch"))
    file_info = result["file_info"]
    assert file_info["name"] == "rec.ts"
    assert file_info["size_mb"] == 1.0
    mtime = os.stat(filepath).st_mtime
    expected = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%d-%m-%Y %H:%M")
    assert file_info["date"] == expected


def test_stop_missing_returns_none(tmp_path):
    rec = Recorder(make_config(tmp_path))
    assert asyncio.run(rec.stop("missing")) is None


def seed_recording(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    os.utime(path, (mtime, mtime))


def test_cleanup_removes_old_keeps_new(tmp_path):
    rec = Recorder(make_config(tmp_path))
    old = tmp_path / "recordings" / "ch" / "old.ts"
    new = tmp_path / "recordings" / "ch" / "new.ts"
    t = time.time() - 3 * 86400
    seed_recording(old, t)
    seed_recording(new, time.time())

    removed = asyncio.run(rec.cleanup_old_recordings(2))

    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_cleanup_disabled_with_zero_retention(tmp_path):
    rec = Recorder(make_config(tmp_path))
    old = tmp_path / "recordings" / "ch" / "old.ts"
    seed_recording(old, time.time() - 30 * 86400)

    removed = asyncio.run(rec.cleanup_old_recordings(0))

    assert removed == 0
    assert old.exists()


def test_cleanup_resolves_relative_recording_dir(tmp_path):
    config = make_config(tmp_path)
    config["recording_dir"] = "recordings"
    rec = Recorder(config)
    old = tmp_path / "recordings" / "ch" / "old.ts"
    seed_recording(old, time.time() - 3 * 86400)

    removed = asyncio.run(rec.cleanup_old_recordings(2))

    assert removed == 1
    assert not old.exists()
