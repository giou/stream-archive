import asyncio
import io
import json
import os
import time
import types
from datetime import datetime, timezone

import pytest
from streamlink.exceptions import NoStreamsError, PluginError

from src.stream_archive.recorder import Recorder, _sanitize_filename


@pytest.fixture(autouse=True)
def _no_network_emote_embed(monkeypatch):
    """Keep kick chat finalize offline in recorder tests (embedding is covered in test_kick_chat)."""
    async def noop(root, client=None):
        return None

    monkeypatch.setattr("src.stream_archive.recorder.embed_kick_emotes", noop)


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


class SustainedStream:
    """Stream that keeps returning data until closed, so recording tasks stay
    alive until cancelled (FakeStream ends instantly with a clean EOF)."""

    def __init__(self, chunk=b"\x00" * 1024):
        self._chunk = chunk
        self._closed = False

    def open(self):
        return self

    def read(self, n):
        return b"" if self._closed else self._chunk

    def close(self):
        self._closed = True


class FakeNotifier:
    def __init__(self):
        self.messages = []
        self.live = []

    async def notify(self, m):
        self.messages.append(m)

    async def notify_live(self, *a, **k):
        self.live.append((a, k))

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


class EndingYouTubeStreamer(FakeYouTubeStreamer):
    def __init__(self):
        super().__init__()
        self.ended = []

    async def end_stream(self, broadcast_id):
        self.ended.append(broadcast_id)


def test_clean_task_end_removes_entry_and_ends_broadcast(tmp_path, monkeypatch):
    """A stream that ends cleanly (feed stall -> streamlink EOF) must release the
    entry so the monitor can restart on the next poll, and transition the
    YouTube broadcast to complete instead of leaving it lingering."""
    config = make_config(tmp_path)
    config["output_mode"] = "youtube"
    yt = EndingYouTubeStreamer()
    rec = Recorder(config, youtube_streamer=yt)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(0))
        rec._recordings["ch"] = {
            "tasks": [task],
            "process": None,
            "youtube_info": {"broadcast_id": "b1"},
            "kick_chat": None,
        }
        task.add_done_callback(lambda t: rec._on_task_finished("ch", t))
        await task
        await asyncio.sleep(0.05)  # let the fire-and-forget finalizers run
        assert "ch" not in rec._recordings
        assert yt.ended == ["b1"]

    asyncio.run(scenario())


def test_session_rides_through_playlist_stalls(tmp_path):
    rec = Recorder(make_config(tmp_path))
    assert rec._session.get_option("stream-segmented-queue-deadline") == 10


def test_quick_youtube_end_sets_restart_backoff(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["output_mode"] = "youtube"
    rec = Recorder(config, youtube_streamer=FakeYouTubeStreamer())
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    async def finish_with(lifetime_s):
        task = asyncio.create_task(asyncio.sleep(0))
        rec._recordings["ch"] = {
            "tasks": [task],
            "process": None,
            "youtube_info": {"broadcast_id": "b1"},
            "kick_chat": None,
            "mode": "youtube",
            "started_at": time.monotonic() - lifetime_s,
        }
        task.add_done_callback(lambda t: rec._on_task_finished("ch", t))
        await task

    asyncio.run(finish_with(10))
    first = rec.restart_blocked_until("ch")
    assert first > time.monotonic()            # 60s backoff after a quick end

    asyncio.run(finish_with(10))
    second = rec.restart_blocked_until("ch")
    assert second > first                      # exponential: 120s on the second

    asyncio.run(finish_with(300))
    assert rec.restart_blocked_until("ch") == 0.0   # a long recording resets


def test_disk_end_no_backoff(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))  # output_mode disk
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(0))
        rec._recordings["ch"] = {
            "tasks": [task],
            "process": None,
            "youtube_info": None,
            "kick_chat": None,
            "mode": "disk",
            "started_at": time.monotonic() - 10,
        }
        task.add_done_callback(lambda t: rec._on_task_finished("ch", t))
        await task

    asyncio.run(scenario())
    assert rec.restart_blocked_until("ch") == 0.0


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
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (SustainedStream(), "author", "Title", "Game"))

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


def test_stop_chat_stops_only_chat(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["record_chat"] = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("src.stream_archive.recorder.ChatRecorder", FakeChatRecorder)
    FakeChatRecorder.instances.clear()

    async def scenario():
        assert await rec.start("ch") is True
        assert rec.is_recording("ch")
        cr = FakeChatRecorder.instances[0]
        assert not cr.stopped
        await rec.stop_chat("ch")
        assert cr.stopped
        assert "chat_recorder" not in rec._recordings["ch"]
        assert rec.is_recording("ch")  # video continues
        await rec.stop("ch")
        assert cr.stopped

    asyncio.run(scenario())


def test_stop_chat_unknown_channel_is_noop(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    async def scenario():
        await rec.stop_chat("nope")  # must not raise

    asyncio.run(scenario())


def test_stop_chat_platform_kick_keeps_twitch_irc(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["record_chat"] = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("src.stream_archive.recorder.ChatRecorder", FakeChatRecorder)
    FakeChatRecorder.instances.clear()

    async def scenario():
        assert await rec.start("ch") is True
        cr = FakeChatRecorder.instances[0]
        await rec.stop_chat("ch", "kick")  # kick-only stop must not touch the IRC recorder
        assert not cr.stopped
        assert "chat_recorder" in rec._recordings["ch"]
        await rec.stop("ch")
        assert cr.stopped

    asyncio.run(scenario())


def test_stop_chat_platform_twitch_keeps_kick_buffer(tmp_path, monkeypatch):
    config = make_kick_config(tmp_path)
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("kick:xqc") is True
        await rec.add_kick_chat("kick:xqc", {"content": "kept"})
        await rec.stop_chat("kick:xqc", "twitch")  # twitch-only stop must not finalize kick chat
        assert "kick_chat" in rec._recordings["kick:xqc"]
        assert rec._recordings["kick:xqc"]["kick_chat"]["messages"] == [{"content": "kept"}]
        chat_path = rec._recordings["kick:xqc"]["kick_chat"]["path"]
        assert not os.path.exists(chat_path)
        await rec.stop("kick:xqc")
        with open(chat_path) as f:
            comments = json.load(f)["comments"]
        assert [c["message"]["body"] for c in comments] == ["kept"]

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


def test_watchdog_aborts_at_cap_without_delete_oldest(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["disk"] = {"max_total_gb": 5, "delete_oldest": False, "check_interval_s": 0.01}
    notifier = FakeNotifier()
    rec = Recorder(config, notifier=notifier)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (SustainedStream(), "author", "Title", "Game"))
    async def fake_snapshot(config):
        return {"free_gb": 100.0, "dir_gb": 6.0, "file_count": 1}

    monkeypatch.setattr("src.stream_archive.disk.disk_snapshot", fake_snapshot)

    async def scenario():
        assert await rec.start("ch") is True
        await asyncio.sleep(0.05)
        assert "ch" not in rec._recordings

    asyncio.run(scenario())
    assert any("Stopped recording ch" in m for m in notifier.messages)
    assert any("archive at 5 GB cap" in m for m in notifier.messages)


def test_delete_oldest_to_cap_deletes_oldest(tmp_path):
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

    removed, freed = asyncio.run(rec.delete_oldest_to_cap())

    assert removed == 1
    assert freed == 1024
    assert not (base / "old.ts").exists()
    assert (base / "mid.ts").exists()
    assert (base / "new.ts").exists()


def make_kick_config(tmp_path, record_chat=True):
    config = make_config(tmp_path)
    config["channels"] = ["kick:xqc"]
    config["record_chat"] = True
    config["kick"] = {"record_chat": record_chat}
    return config


class CapturingFakePlugin(FakePlugin):
    instances = []

    def __init__(self, session, url, options):
        super().__init__(session, url, options)
        CapturingFakePlugin.instances.append((url, options))


def test_resolve_stream_kick_uses_plugin_directly(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    stream = FakeStream()
    seen = {}

    def resolve_url(url):
        seen["url"] = url
        return ("kick", CapturingFakePlugin, url)

    monkeypatch.setattr(rec._session, "resolve_url", resolve_url)
    monkeypatch.setattr(CapturingFakePlugin, "streams", lambda self: {"best": stream})
    CapturingFakePlugin.instances.clear()

    best, author, title, game = rec._resolve_stream("kick:xqc", None, None)

    assert best is stream
    assert seen["url"] == "https://kick.com/xqc"
    assert CapturingFakePlugin.instances == [("https://kick.com/xqc", {})]
    assert author == "author"  # plugin metadata wins
    assert title == "Title"
    assert game == "Game"

    # without plugin metadata, the bare slug is the author fallback
    CapturingFakePlugin.author = None
    best, author, title, game = rec._resolve_stream("kick:xqc", None, None)
    assert author == "xqc"


def test_start_twitch_prefixed_uses_twitch_dir(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["channels"] = ["twitch:streamer1"]
    config["record_chat"] = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("src.stream_archive.recorder.ChatRecorder", FakeChatRecorder)
    FakeChatRecorder.instances.clear()

    async def scenario():
        assert await rec.start("twitch:streamer1") is True
        entry = rec._recordings["twitch:streamer1"]
        assert entry["filepath"].startswith(str(tmp_path / "recordings" / "twitch" / "streamer1"))
        cr = FakeChatRecorder.instances[0]
        assert cr.channel == "streamer1"  # bare login for IRC JOIN
        assert cr.chat_path.startswith(str(tmp_path / "chat" / "twitch" / "streamer1"))
        assert cr.chat_path.endswith(".chat.json")
        await rec.stop("twitch:streamer1")

    asyncio.run(scenario())


def test_start_kick_uses_kick_dir_and_chat(tmp_path, monkeypatch):
    config = make_kick_config(tmp_path, record_chat=True)
    rec = Recorder(config, notifier=FakeNotifier())
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("src.stream_archive.recorder.ChatRecorder", FakeChatRecorder)
    FakeChatRecorder.instances.clear()

    async def scenario():
        assert await rec.start("kick:xqc") is True
        entry = rec._recordings["kick:xqc"]
        assert entry["filepath"].startswith(str(tmp_path / "recordings" / "kick" / "xqc"))
        assert "Title-" in os.path.basename(entry["filepath"])
        # no Twitch IRC chat recorder for kick channels
        assert FakeChatRecorder.instances == []
        assert "chat_task" not in entry
        # kick chat buffer prepared under chat/kick/xqc/
        assert entry["kick_chat"]["path"].startswith(str(tmp_path / "chat" / "kick" / "xqc"))
        assert entry["kick_chat"]["path"].endswith(".chat.json")
        assert entry["kick_chat"]["channel"] == "kick:xqc"
        assert rec._notifier.live == [(("kick:xqc", "Title", "Game", "https://kick.com/xqc"), {})]
        await rec.stop("kick:xqc")

    asyncio.run(scenario())


def test_start_kick_record_chat_false_skips_buffer(tmp_path, monkeypatch):
    config = make_kick_config(tmp_path, record_chat=False)
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("kick:xqc") is True
        assert "kick_chat" not in rec._recordings["kick:xqc"]
        await rec.stop("kick:xqc")

    asyncio.run(scenario())


def test_start_kick_403_block_warns_once_per_30min(tmp_path, monkeypatch):
    config = make_kick_config(tmp_path)
    notifier = FakeNotifier()
    rec = Recorder(config, notifier=notifier)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    def blocked(*a):
        raise PluginError("Error while querying Kick API: 403 Forbidden")

    monkeypatch.setattr(rec, "_resolve_stream", blocked)

    async def scenario():
        assert await rec.start("kick:xqc") is False
        assert await rec.start("kick:xqc") is False

    asyncio.run(scenario())

    assert notifier.messages == [
        "\u26a0\ufe0f Kick is blocking requests from this server (anti-bot challenge). "
        "Recording kick:xqc failed: Error while querying Kick API: 403 Forbidden. Will retry automatically. "
        "Install a browser on this host (streamlink then solves the challenge automatically) "
        "or run from a non-blocked IP."
    ]


def test_start_kick_plugin_error_without_403_no_warning(tmp_path, monkeypatch):
    config = make_kick_config(tmp_path)
    notifier = FakeNotifier()
    rec = Recorder(config, notifier=notifier)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    def other_error(*a):
        raise PluginError("other error")

    monkeypatch.setattr(rec, "_resolve_stream", other_error)

    async def scenario():
        assert await rec.start("kick:xqc") is False

    asyncio.run(scenario())

    assert notifier.messages == []


def test_add_kick_chat_appends_and_stop_writes_file(tmp_path, monkeypatch):
    config = make_kick_config(tmp_path)
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    payload = {"created_at": "2026-08-13T10:00:00Z", "content": "hello"}

    async def scenario():
        assert await rec.start("kick:xqc") is True
        await rec.add_kick_chat("kick:xqc", payload)
        await rec.add_kick_chat("kick:xqc", {"content": "world"})
        entry = rec._recordings["kick:xqc"]
        chat_path = entry["kick_chat"]["path"]
        await rec.stop("kick:xqc")
        assert os.path.exists(chat_path)
        with open(chat_path) as f:
            data = json.load(f)
        # TwitchDownloader ChatRoot shape
        assert data["FileInfo"]["Version"] == {"Major": 1, "Minor": 4, "Patch": 0}
        assert data["streamer"]["login"] == "xqc"
        assert data["video"]["title"] == "Title"
        assert data["video"]["id"].startswith("kick-xqc-")
        assert len(data["comments"]) == 2
        assert data["comments"][0]["message"]["body"] == "hello"
        assert data["comments"][0]["content_offset_seconds"] >= 0
        assert data["comments"][1]["message"]["body"] == "world"
        assert "embeddedData" not in data  # embed patched off in tests
        assert not os.path.exists(chat_path + ".tmp")

    asyncio.run(scenario())


def test_add_kick_chat_unrecorded_channel_noop(tmp_path, monkeypatch):
    rec = Recorder(make_kick_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    async def scenario():
        await rec.add_kick_chat("kick:other", {"content": "x"})  # must not raise
        assert rec._recordings == {}

    asyncio.run(scenario())


def test_stop_with_empty_kick_chat_writes_no_file(tmp_path, monkeypatch):
    config = make_kick_config(tmp_path)
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("kick:xqc") is True
        chat_path = rec._recordings["kick:xqc"]["kick_chat"]["path"]
        await rec.stop("kick:xqc")
        assert not os.path.exists(chat_path)

    asyncio.run(scenario())


def test_stop_chat_finalizes_kick_chat_midstream(tmp_path, monkeypatch):
    config = make_kick_config(tmp_path)
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("kick:xqc") is True
        chat_path = rec._recordings["kick:xqc"]["kick_chat"]["path"]
        await rec.add_kick_chat("kick:xqc", {"content": "saved"})
        await rec.stop_chat("kick:xqc")
        assert "kick_chat" not in rec._recordings["kick:xqc"]
        assert rec.is_recording("kick:xqc")  # video continues
        with open(chat_path) as f:
            comments = json.load(f)["comments"]
        assert [c["message"]["body"] for c in comments] == ["saved"]
        await rec.stop("kick:xqc")

    asyncio.run(scenario())
