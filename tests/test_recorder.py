import asyncio
import io
import os
import time
import types
from datetime import datetime, timezone

import pytest
from streamlink.exceptions import NoStreamsError, PluginError

from src.stream_archive.recorder import Recorder, _sanitize_filename


def make_config(tmp_path):
    return {
        "output_mode": "disk",
        "recording_dir": str(tmp_path / "recordings"),
        "record_chat": False,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "proxy_list": ["httpproxy://u:p@h:1"],
        "_workdir": tmp_path,
        "channels": ["ch"],
    }


class FakeChatRecorder:
    instances = []

    def __init__(self, channel, chat_path, title, game, author=None, user_id=None):
        self.channel = channel
        self.chat_path = chat_path
        self.title = title
        self.game = game
        self.author = author
        self.user_id = user_id
        self.started = False
        self.stopped = False
        FakeChatRecorder.instances.append(self)

    def start(self):
        self.started = True
        return object()  # task-like sentinel

    async def stop(self):
        self.stopped = True
        return 0


class FakeStream:
    def open(self):
        return io.BytesIO()


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def notify(self, m):
        self.messages.append(m)

    async def notify_live(self, *a, **k):
        pass

    async def notify_offline(self, *a, **k):
        pass


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


def test_start_uses_per_channel_override(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["channel_output_modes"] = {"ch": "youtube"}
    rec = Recorder(config, youtube_streamer=FakeYouTubeStreamer())
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("ch") is True
        assert rec._recordings["ch"]["filepath"] is None
        await rec.stop("ch")

        assert await rec.start("other") is True
        assert rec._recordings["other"]["filepath"] is not None
        await rec.stop("other")

    asyncio.run(scenario())


def test_start_nostreams_returns_false(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    def raise_nostreams(*a):
        raise NoStreamsError()

    monkeypatch.setattr(rec, "_resolve_stream", raise_nostreams)

    assert asyncio.run(rec.start("ch")) is False
    assert "ch" not in rec._recordings


class FakePlugin:
    author = "author"
    title = "Title"
    category = "Game"

    def __init__(self, session, url, options):
        self.session = session
        self.url = url
        self.options = options

    def streams(self):
        raise AssertionError("streams() not scripted")


def _make_proxy_config(tmp_path, proxies):
    config = make_config(tmp_path)
    config["proxy_list"] = proxies
    return config


def test_resolve_stream_tries_next_proxy_on_plugin_error(tmp_path, monkeypatch):
    rec = Recorder(_make_proxy_config(
        tmp_path, ["httpproxy://u:p@h:1", "https://proxy2.example.com"]))
    calls = []
    stream = FakeStream()

    monkeypatch.setattr(
        rec._session, "resolve_url",
        lambda url: ("twitch", FakePlugin, url))

    def scripted_streams(self):
        calls.append(self.options["proxy-playlist"])
        if len(calls) == 1:
            raise PluginError("proxy boom")
        return {"best": stream}

    monkeypatch.setattr(FakePlugin, "streams", scripted_streams)

    best, author, title, game = rec._resolve_stream("ch", None, None)

    assert best is stream
    assert calls == [
        ["httpproxy://u:p@h:1", "https://proxy2.example.com"],
        ["https://proxy2.example.com"],
    ]


def test_resolve_stream_all_proxies_fail_raises_nostreams(tmp_path, monkeypatch):
    rec = Recorder(_make_proxy_config(
        tmp_path, ["httpproxy://u:p@h:1", "https://proxy2.example.com"]))
    calls = []

    monkeypatch.setattr(
        rec._session, "resolve_url",
        lambda url: ("twitch", FakePlugin, url))

    def scripted_streams(self):
        calls.append(self.options["proxy-playlist"])
        raise PluginError("proxy boom")

    monkeypatch.setattr(FakePlugin, "streams", scripted_streams)

    with pytest.raises(NoStreamsError):
        rec._resolve_stream("ch", None, None)

    assert calls == [
        ["httpproxy://u:p@h:1", "https://proxy2.example.com"],
        ["https://proxy2.example.com"],
    ]


def test_resolve_stream_nostreams_not_retried(tmp_path, monkeypatch):
    rec = Recorder(_make_proxy_config(
        tmp_path, ["httpproxy://u:p@h:1", "https://proxy2.example.com"]))
    calls = []

    monkeypatch.setattr(
        rec._session, "resolve_url",
        lambda url: ("twitch", FakePlugin, url))

    def scripted_streams(self):
        calls.append(self.options["proxy-playlist"])
        raise NoStreamsError()

    monkeypatch.setattr(FakePlugin, "streams", scripted_streams)

    with pytest.raises(NoStreamsError):
        rec._resolve_stream("ch", None, None)

    assert len(calls) == 1


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


class RaisingReadStream:
    def open(self):
        class _Fd:
            def read(self, n):
                raise RuntimeError("read boom")

        return _Fd()


class FakeFailingStream:
    def open(self):
        raise RuntimeError("disk boom")


class FakeYouTubeStreamer:
    def __init__(self, create_error=None):
        self.create_error = create_error

    async def create_stream(self, author, title, channel, game):
        if self.create_error:
            raise self.create_error
        return {"youtube_url": "https://youtu.be/x", "rtmp_url": "rtmp://x", "broadcast_id": "b1"}


def test_pipe_stream_clean_eof_returns_true(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    process = types.SimpleNamespace(stdin=None)
    result = asyncio.run(rec._pipe_stream("ch", FakeStream(), process, None))
    assert result is True


def test_pipe_stream_read_error_returns_false(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    process = types.SimpleNamespace(stdin=None)
    result = asyncio.run(rec._pipe_stream("ch", RaisingReadStream(), process, None))
    assert result is False


def test_recording_task_failure_removes_entry(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeFailingStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("ch") is True
        await asyncio.sleep(0.05)
        assert not rec.is_recording("ch")
        assert "ch" not in rec._recordings

    asyncio.run(scenario())


def test_youtube_create_failure_propagates_and_removes_entry(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["output_mode"] = "youtube"
    rec = Recorder(config, youtube_streamer=FakeYouTubeStreamer(create_error=RuntimeError("boom")))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("ch") is True
        await asyncio.sleep(0.05)
        assert not rec.is_recording("ch")

    asyncio.run(scenario())


def test_youtube_quota_error_falls_back_to_disk(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["output_mode"] = "youtube"
    rec = Recorder(
        config,
        youtube_streamer=FakeYouTubeStreamer(create_error=RuntimeError("The user has exceeded their quota")),
    )
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("ch") is True
        await asyncio.sleep(0.05)
        assert rec.is_recording("ch")
        assert rec._recordings["ch"]["filepath"].startswith(str(tmp_path / "recordings" / "ch"))

    asyncio.run(scenario())


def test_start_records_chat_when_enabled(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["record_chat"] = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("src.stream_archive.recorder.ChatRecorder", FakeChatRecorder)
    FakeChatRecorder.instances.clear()

    async def scenario():
        assert await rec.start("ch") is True
        entry = rec._recordings["ch"]
        assert entry["chat_task"] is not None
        assert len(FakeChatRecorder.instances) == 1
        cr = FakeChatRecorder.instances[0]
        assert cr.channel == "ch"
        assert cr.started
        assert cr.title == "Title"
        assert cr.game == "Game"
        assert cr.author == "author"
        assert cr.user_id is None
        assert cr.chat_path.startswith(str(tmp_path / "chat" / "ch"))
        assert "Title-" in cr.chat_path
        assert cr.chat_path.endswith(".chat.json")
        await rec.stop("ch")
        assert cr.stopped

    asyncio.run(scenario())


def test_start_chat_disabled(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))  # record_chat defaults to False in make_config
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("src.stream_archive.recorder.ChatRecorder", FakeChatRecorder)
    FakeChatRecorder.instances.clear()

    async def scenario():
        assert await rec.start("ch") is True
        assert "chat_task" not in rec._recordings["ch"]
        assert FakeChatRecorder.instances == []
        await rec.stop("ch")

    asyncio.run(scenario())


def test_recording_failure_stops_chat(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["record_chat"] = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeFailingStream(), "author", "Title", "Game"))
    monkeypatch.setattr("src.stream_archive.recorder.ChatRecorder", FakeChatRecorder)
    FakeChatRecorder.instances.clear()

    async def scenario():
        assert await rec.start("ch") is True
        await asyncio.sleep(0.1)
        assert not rec.is_recording("ch")
        assert len(FakeChatRecorder.instances) == 1
        assert FakeChatRecorder.instances[0].stopped

    asyncio.run(scenario())


def test_stop_all_finalizes_chat_for_every_channel(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["record_chat"] = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("src.stream_archive.recorder.ChatRecorder", FakeChatRecorder)
    FakeChatRecorder.instances.clear()

    async def scenario():
        assert await rec.start("ch1") is True
        assert await rec.start("ch2") is True
        assert len(FakeChatRecorder.instances) == 2
        await rec.stop_all()
        assert all(cr.stopped for cr in FakeChatRecorder.instances)

    asyncio.run(scenario())


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


def test_resolve_stream_preferred_quality(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    s1, s2 = FakeStream(), FakeStream()

    monkeypatch.setattr(
        rec._session, "resolve_url",
        lambda url: ("twitch", FakePlugin, url))
    monkeypatch.setattr(FakePlugin, "streams", lambda self: {"best": s1, "720p": s2})

    rec._config["preferred_quality"] = "720p"
    best, _, _, _ = rec._resolve_stream("ch", None, None)
    assert best is s2

    rec._config["preferred_quality"] = "1080p"
    best, _, _, _ = rec._resolve_stream("ch", None, None)
    assert best is s1


def test_start_records_mode_and_started_at(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("ch") is True
        assert rec._recordings["ch"]["mode"] == "disk"
        assert isinstance(rec._recordings["ch"]["started_at"], float)
        await rec.stop("ch")

    asyncio.run(scenario())


def test_recording_info_reports_duration_and_size(tmp_path):
    rec = Recorder(make_config(tmp_path))
    filepath = tmp_path / "recordings" / "ch" / "rec.ts"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_bytes(b"\0" * (3 * 1024 * 1024))
    rec._recordings["ch"] = {
        "tasks": [],
        "process": None,
        "youtube_info": None,
        "filepath": str(filepath),
        "started_at": time.monotonic() - 125,
        "mode": "disk",
    }

    info = rec.recording_info()

    assert len(info) == 1
    assert info[0]["channel"] == "ch"
    assert abs(info[0]["duration_s"] - 125) <= 1
    assert info[0]["size_mb"] == pytest.approx(3.0, abs=0.1)


def test_watchdog_aborts_on_low_free_space(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["disk"] = {"min_free_gb": 2, "check_interval_s": 0.01}
    notifier = FakeNotifier()
    rec = Recorder(config, notifier=notifier)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    async def fake_snapshot(config):
        return {"free_gb": 0.5, "dir_gb": 0.0, "file_count": 0}

    monkeypatch.setattr("src.stream_archive.disk.disk_snapshot", fake_snapshot)

    async def scenario():
        assert await rec.start("ch") is True
        await asyncio.sleep(0.05)
        assert "ch" not in rec._recordings

    asyncio.run(scenario())
    assert any("Stopped recording ch" in m for m in notifier.messages)
    assert any("free space dropped below 2" in m for m in notifier.messages)


def test_watchdog_aborts_on_fill_rate(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["disk"] = {"min_time_to_full_min": 10, "check_interval_s": 0.01}
    notifier = FakeNotifier()
    rec = Recorder(config, notifier=notifier)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    async def fake_snapshot(config):
        return {"free_gb": 0.01, "dir_gb": 0.0, "file_count": 0}

    monkeypatch.setattr("src.stream_archive.disk.disk_snapshot", fake_snapshot)
    sizes = {"n": 0}

    def growing_getsize(path):
        sizes["n"] += 1
        return sizes["n"] * 50 * 1024 * 1024

    monkeypatch.setattr(os.path, "getsize", growing_getsize)

    async def scenario():
        assert await rec.start("ch") is True
        await asyncio.sleep(0.05)
        assert "ch" not in rec._recordings

    asyncio.run(scenario())
    assert any("Stopped recording ch" in m for m in notifier.messages)
    assert any("disk filling too fast" in m for m in notifier.messages)


def test_evict_to_cap_deletes_oldest(tmp_path):
    config = make_config(tmp_path)
    config["disk"] = {"max_total_gb": 2.5e-6}  # ~2.6 KB cap
    rec = Recorder(config)
    base = tmp_path / "recordings" / "ch"
    base.mkdir(parents=True, exist_ok=True)
    t0 = time.time() - 100
    for i, name in enumerate(["old.ts", "mid.ts", "new.ts"]):
        p = base / name
        p.write_bytes(b"x" * 1024)
        os.utime(p, (t0 + i, t0 + i))

    removed, freed = asyncio.run(rec.evict_to_cap())

    assert removed == 1
    assert freed == 1024
    assert not (base / "old.ts").exists()
    assert (base / "mid.ts").exists()
    assert (base / "new.ts").exists()
