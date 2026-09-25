"""Scratch tmp folder and boot repair for the .ts remux."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest import mock

from conftest import make_config as valid_config

from stream_archive import disk
from stream_archive.recorder import Recorder
from stream_archive.recorder import remux as remux_mod


def _bind(tmp_path: Path, **overrides):
    """Config with tmp_path as the data dir."""
    cfg = valid_config(**overrides)
    cfg._workdir = tmp_path
    cfg._config_path = tmp_path / "config.json"
    return cfg


def _ts(parent: Path, name: str = "cap.ts") -> Path:
    """One fake capture under parent."""
    parent.mkdir(parents=True, exist_ok=True)
    src = parent / name
    src.write_bytes(b"v" * 100)
    return src


def test_remux_scratch_lives_in_data_tmp(tmp_path):
    """The scratch file mirrors the source under the data tmp folder."""
    src = _ts(tmp_path / "recordings" / "twitch" / "ch", "show.ts")
    seen: dict = {}

    def fake_ffmpeg(source, tmp):
        seen["tmp"] = Path(tmp)
        Path(tmp).write_bytes(b"v" * 100)
        return True

    with (
        mock.patch.object(remux_mod, "_run_ffmpeg", side_effect=fake_ffmpeg),
        mock.patch.object(remux_mod, "_probe_ok", return_value=True),
    ):
        out = remux_mod.remux_ts_to_mp4(src, tmp_path)
    assert out == src.with_suffix(".mp4")
    assert seen["tmp"] == tmp_path / ".tmp" / "recordings" / "twitch" / "ch" / "show.mp4"
    assert not seen["tmp"].exists()
    assert not src.exists()
    assert list(src.parent.glob("*.tmp.mp4")) == []


def test_find_pending_remuxes_queues_orphans(tmp_path):
    """Every orphan evidence kind queues its .ts: legacy scratch, tmp-folder leftover."""
    base = tmp_path / "recordings"
    legacy_src = _ts(base, "legacy.ts")
    legacy_tmp = base / "legacy.remux.tmp.mp4"
    legacy_tmp.write_bytes(b"junk")
    folder_src = _ts(base / "twitch" / "ch", "cap.ts")
    folder_tmp = tmp_path / ".tmp" / "recordings" / "twitch" / "ch" / "cap.mp4"
    folder_tmp.parent.mkdir(parents=True, exist_ok=True)
    folder_tmp.write_bytes(b"junk")
    out = remux_mod.find_pending_remuxes(base, tmp_path)
    assert sorted(out) == sorted([legacy_src, folder_src])
    assert legacy_tmp.exists()


def test_find_pending_remuxes_clears_garbage(tmp_path):
    """Scratch with no live .ts goes at once and queues nothing."""
    base = tmp_path / "recordings"
    base.mkdir()
    orphan = base / "gone.remux.tmp.mp4"
    orphan.write_bytes(b"junk")
    stray = tmp_path / ".tmp" / "recordings" / "gone.mp4"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"junk")
    assert remux_mod.find_pending_remuxes(base, tmp_path) == []
    assert not orphan.exists()
    assert not stray.exists()


def test_find_pending_remuxes_skips_finished(tmp_path):
    """A .ts whose .mp4 probes clean goes like a normal run, not a repair."""
    base = tmp_path / "recordings"
    src = _ts(base)
    (base / "cap.remux.tmp.mp4").write_bytes(b"junk")
    done = base / "cap.mp4"
    done.write_bytes(b"v" * 100)
    with mock.patch.object(remux_mod, "_probe_ok", return_value=True):
        assert remux_mod.find_pending_remuxes(base, tmp_path) == []
    assert done.exists()
    assert not src.exists()


def test_shutdown_handoff_to_boot_repair(tmp_path):
    """Shutdown lists the kept .ts, boot consumes the list and queues it."""
    cfg = _bind(tmp_path)
    rec = Recorder(cfg)
    src = _ts(tmp_path / "recordings")
    rec._recordings["twitch:x"] = {"filepath": str(src), "tasks": []}
    asyncio.run(rec.close())
    assert src.exists()
    pending_file = tmp_path / ".tmp" / "pending.json"
    assert json.loads(pending_file.read_text()) == [str(src)]
    out = remux_mod.find_pending_remuxes(tmp_path / "recordings", tmp_path)
    assert out == [src]
    assert not pending_file.exists()


def test_iter_recordings_skips_scratch(tmp_path):
    """Neither the tmp folder nor legacy scratch counts as a recording."""
    base = tmp_path / "recordings"
    _ts(base)
    (base / "good.mp4").write_bytes(b"x" * 10)
    (base / "cap.remux.tmp.mp4").write_bytes(b"x" * 10)
    scratch = base / ".tmp" / "x.mp4"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(b"x" * 10)
    names = {p.name for p in disk.iter_recordings(base)}
    assert names == {"good.mp4", "cap.ts"}


def test_finalize_finish_media_flag(tmp_path):
    """Shutdown finalize skips the remux, normal stop keeps it."""
    cfg = _bind(tmp_path)
    rec = Recorder(cfg)
    src = _ts(tmp_path / "recordings")
    entry = {"filepath": str(src), "tasks": [], "youtube_info": None}
    with (
        mock.patch("stream_archive.recorder.core.ffmpeg_available", return_value=True),
        mock.patch(
            "stream_archive.recorder.core.remux_ts_to_mp4_async", return_value=src.with_suffix(".mp4")
        ) as remux_mock,
    ):
        asyncio.run(rec._finalize_entry("twitch:x", dict(entry), None, finish_media=False))
        remux_mock.assert_not_called()
        asyncio.run(rec._finalize_entry("twitch:x", dict(entry), None, finish_media=True))
        remux_mock.assert_called_once()


def test_repair_skips_live_captures(tmp_path):
    """The boot repair never remuxes a file a capture still holds."""
    cfg = _bind(tmp_path)
    rec = Recorder(cfg)
    base = tmp_path / "recordings"
    live = _ts(base, "live.ts")
    (base / "live.remux.tmp.mp4").write_bytes(b"junk")
    stale = _ts(base, "old.ts")
    (base / "old.remux.tmp.mp4").write_bytes(b"junk")
    rec._recordings["twitch:live"] = {"filepath": str(live), "tasks": []}
    calls: list = []

    async def fake_remux(source, workdir):
        calls.append(Path(source))
        return Path(source).with_suffix(".mp4")

    with mock.patch("stream_archive.recorder.core.remux_ts_to_mp4_async", side_effect=fake_remux):
        ok, failed = asyncio.run(rec.repair_pending_remuxes())
    assert calls == [stale]
    assert (ok, failed) == (1, 1)
