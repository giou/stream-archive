import asyncio
import io
import json
import os
import subprocess
import time
import types
from datetime import UTC, datetime

import pytest
from streamlink.exceptions import NoStreamsError, PluginError

from stream_archive.config import AppConfig
from stream_archive.recorder import Recorder, _sanitize_filename
from stream_archive.recorder.streamlink_source import _AudioOnlyStream


@pytest.fixture(autouse=True)
def _no_network_emote_embed(monkeypatch):
    """Keep kick chat finalize offline in recorder tests (embedding is covered in test_kick_chat)."""

    async def noop(root, client=None):
        return None

    monkeypatch.setattr("stream_archive.recorder.chat_output.embed_kick_emotes", noop)


def make_config(tmp_path):
    d = {
        "telegram_user_id": 12345,
        "bot_telegram_api": "bot_token",
        "twitch_client_id": "client_id",
        "twitch_client_secret": "client_secret",
        "channels": ["ch"],
        "proxy_list": ["httpproxy://u:p@h:1"],
        "monitoring_interval": 60,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "recording_dir": str(tmp_path / "recordings"),
        "output_mode": "disk",
        "record_chat": False,
    }
    cfg = AppConfig.model_validate(d)
    cfg._workdir = tmp_path
    cfg._config_path = tmp_path / "config.json"
    return cfg


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
    """Feed that keeps returning data until closed.

    FakeStream ends instantly with a clean EOF. Tests use this stream so
    recording tasks stay alive until cancellation.
    """

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
        assert rec._recordings["ch"]["filepath"].endswith(".ts")
        await rec.stop("ch")

    asyncio.run(scenario())


def test_start_uses_per_channel_override(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.channel_output_modes = {"twitch:ch": "youtube"}
    rec = Recorder(config, youtube_streamer=FakeYouTubeStreamer())
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("twitch:ch") is True
        assert rec._recordings["twitch:ch"]["filepath"] is None
        await rec.stop("twitch:ch")

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
    config.proxy_list = proxies
    return config


def test_resolve_stream_tries_next_proxy_on_plugin_error(tmp_path, monkeypatch):
    rec = Recorder(_make_proxy_config(tmp_path, ["httpproxy://u:p@h:1", "https://proxy2.example.com"]))
    calls = []
    stream = FakeStream()

    monkeypatch.setattr(rec._session, "resolve_url", lambda url: ("twitch", FakePlugin, url))

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
    rec = Recorder(_make_proxy_config(tmp_path, ["httpproxy://u:p@h:1", "https://proxy2.example.com"]))
    calls = []

    monkeypatch.setattr(rec._session, "resolve_url", lambda url: ("twitch", FakePlugin, url))

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
    rec = Recorder(_make_proxy_config(tmp_path, ["httpproxy://u:p@h:1", "https://proxy2.example.com"]))
    calls = []

    monkeypatch.setattr(rec._session, "resolve_url", lambda url: ("twitch", FakePlugin, url))

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
    config.output_mode = "youtube"
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
        self.create_count = 0

    async def create_stream(self, author, title, channel, game):
        self.create_count += 1
        return await super().create_stream(author, title, channel, game)

    async def end_stream(self, broadcast_id):
        self.ended.append(broadcast_id)


def test_clean_task_end_removes_entry_and_ends_broadcast(tmp_path, monkeypatch):
    """A clean stream end releases the entry and completes the broadcast.

    When a feed stall ends in a streamlink EOF, the recorder releases the
    entry and flags the end as clean. The monitor then skips restart attempts
    until the offline event arrives. The YouTube broadcast transitions to
    complete instead of lingering.
    """
    config = make_config(tmp_path)
    config.output_mode = "youtube"
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
        assert rec.ended_clean("ch")
        assert yt.ended == ["b1"]

    asyncio.run(scenario())


def test_failed_task_end_not_flagged_clean(tmp_path, monkeypatch):
    """A task that ends with an exception must not look like a clean end.

    The monitor has to restart the channel after such a task death.
    """
    rec = Recorder(make_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    async def scenario():
        async def boom():
            raise RuntimeError("stream interrupted")

        task = asyncio.create_task(boom())
        rec._recordings["ch"] = {
            "tasks": [task],
            "process": None,
            "youtube_info": None,
            "kick_chat": None,
        }
        task.add_done_callback(lambda t: rec._on_task_finished("ch", t))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.05)
        assert "ch" not in rec._recordings
        assert not rec.ended_clean("ch")

    asyncio.run(scenario())


def test_clean_end_flag_cleared_on_restart(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "disk"
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (SustainedStream(), "author", "Title", "Game"))

    async def scenario():
        rec._ended_clean["ch"] = time.monotonic()
        assert await rec.start("ch") is True
        assert not rec.ended_clean("ch")
        await rec.stop("ch")

    asyncio.run(scenario())


def test_clean_end_latch_expires_after_grace(tmp_path):
    rec = Recorder(make_config(tmp_path))
    rec._ended_clean["ch"] = time.monotonic() - 601
    assert not rec.ended_clean("ch")  # expired -> monitor restarts instead of suppressing
    assert "ch" not in rec._ended_clean  # expired entries are lazily popped


def test_reserve_start_blocks_when_capacity_taken(tmp_path):
    """reserve_start holds a capacity slot atomically.

    Two simultaneous go-lives cannot both pass max_concurrent_recordings.
    """
    config = make_config(tmp_path)
    config.max_concurrent_recordings = 1
    rec = Recorder(config)
    reason = (
        f"concurrent recording limit reached ({config.max_concurrent_recordings}/{config.max_concurrent_recordings})"
    )

    async def scenario():
        assert await rec.reserve_start("ch") is None
        assert await rec.reserve_start("other") == reason
        rec.release_start("ch")
        assert await rec.reserve_start("other") is None

    asyncio.run(scenario())


def test_session_rides_through_playlist_stalls(tmp_path):
    rec = Recorder(make_config(tmp_path))
    assert rec._session.get_option("stream-segmented-queue-deadline") == 10


def test_youtube_daily_budget_blocks_and_releases(tmp_path):
    config = make_config(tmp_path)
    config.output_mode = "youtube"
    rec = Recorder(config, youtube_streamer=FakeYouTubeStreamer())
    # 10 creations inside the rolling 24h window: shared across channels.
    rec._youtube_starts = [time.time() - i * 60 for i in range(10)]
    reason = rec.youtube_restart_blocked_reason("kick:a")
    assert reason is not None
    assert "daily broadcast limit" in reason
    assert "10/10" in reason
    assert rec.youtube_restart_blocked_reason("kick:b") is not None  # global, not per channel
    # One slot ages out of the window: recording is allowed again.
    rec._youtube_starts[0] = time.time() - 90000
    assert rec.youtube_restart_blocked_reason("kick:a") is None


def test_youtube_daily_budget_ignores_disk_mode(tmp_path):
    rec = Recorder(make_config(tmp_path))  # output_mode disk
    rec._youtube_starts = [time.time() - i * 60 for i in range(10)]
    assert rec.youtube_restart_blocked_reason("ch") is None


def test_quick_youtube_end_sets_restart_backoff(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "youtube"
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
    first = rec._backoff_until["ch"]
    assert "restarting in" in rec.youtube_restart_blocked_reason("ch")

    asyncio.run(finish_with(10))
    second = rec._backoff_until["ch"]
    assert second > first  # exponential growth

    asyncio.run(finish_with(300))
    assert rec.youtube_restart_blocked_reason("ch") is None  # long recording resets


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
    assert rec.youtube_restart_blocked_reason("ch") is None


def test_youtube_create_failure_propagates_and_removes_entry(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "youtube"
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
    config.output_mode = "youtube"
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
    config.record_chat = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("stream_archive.recorder.core.ChatRecorder", FakeChatRecorder)
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
    monkeypatch.setattr("stream_archive.recorder.core.ChatRecorder", FakeChatRecorder)
    FakeChatRecorder.instances.clear()

    async def scenario():
        assert await rec.start("ch") is True
        assert "chat_task" not in rec._recordings["ch"]
        assert FakeChatRecorder.instances == []
        await rec.stop("ch")

    asyncio.run(scenario())


def test_recording_failure_stops_chat(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.record_chat = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeFailingStream(), "author", "Title", "Game"))
    monkeypatch.setattr("stream_archive.recorder.core.ChatRecorder", FakeChatRecorder)
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
    config.record_chat = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("stream_archive.recorder.core.ChatRecorder", FakeChatRecorder)
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
    config.record_chat = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("stream_archive.recorder.core.ChatRecorder", FakeChatRecorder)
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
    config.record_chat = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("stream_archive.recorder.core.ChatRecorder", FakeChatRecorder)
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
    expected = datetime.fromtimestamp(mtime, tz=UTC).strftime("%d-%m-%Y %H:%M")
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
    old_audio = tmp_path / "recordings" / "ch" / "old.m4a"
    new = tmp_path / "recordings" / "ch" / "new.ts"
    t = time.time() - 3 * 86400
    seed_recording(old, t)
    seed_recording(old_audio, t)
    seed_recording(new, time.time())

    removed = asyncio.run(rec.cleanup_old_recordings(2))

    assert removed == 2
    assert not old.exists()
    assert not old_audio.exists()
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
    config.recording_dir = "recordings"
    rec = Recorder(config)
    old = tmp_path / "recordings" / "ch" / "old.ts"
    seed_recording(old, time.time() - 3 * 86400)

    removed = asyncio.run(rec.cleanup_old_recordings(2))

    assert removed == 1
    assert not old.exists()


def test_resolve_stream_preferred_quality(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    s1, s2 = FakeStream(), FakeStream()

    monkeypatch.setattr(rec._session, "resolve_url", lambda url: ("twitch", FakePlugin, url))
    monkeypatch.setattr(FakePlugin, "streams", lambda self: {"best": s1, "720p": s2})

    rec._config.preferred_quality = "720p"
    best, _, _, _ = rec._resolve_stream("ch", None, None)
    assert best is s2

    rec._config.preferred_quality = "1080p"
    best, _, _, _ = rec._resolve_stream("ch", None, None)
    assert best is s1


def test_resolve_stream_per_channel_quality(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    s1, s2 = FakeStream(), FakeStream()

    monkeypatch.setattr(rec._session, "resolve_url", lambda url: ("twitch", FakePlugin, url))
    monkeypatch.setattr(FakePlugin, "streams", lambda self: {"best": s1, "480p": s2})
    rec._config.channel_preferred_qualities = {"twitch:ch": "480p"}
    best, _, _, _ = rec._resolve_stream("twitch:ch", None, None)
    assert best is s2


def test_resolve_stream_audio_only_twitch_native(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    s_best, s_audio = FakeStream(), FakeStream()
    rec._config.preferred_quality = "audio_only"

    monkeypatch.setattr(rec._session, "resolve_url", lambda url: ("twitch", FakePlugin, url))
    monkeypatch.setattr(FakePlugin, "streams", lambda self: {"best": s_best, "audio_only": s_audio})
    best, _, _, _ = rec._resolve_stream("ch", None, None)
    assert isinstance(best, _AudioOnlyStream)
    assert best._inner is s_audio


def test_resolve_stream_audio_only_kick_falls_back_to_best(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    s_best = FakeStream()
    rec._config.preferred_quality = "audio_only"

    monkeypatch.setattr(rec._session, "resolve_url", lambda url: ("kick", FakePlugin, url))
    monkeypatch.setattr(FakePlugin, "streams", lambda self: {"best": s_best})
    best, _, _, _ = rec._resolve_stream("kick:xqc", None, None)
    assert isinstance(best, _AudioOnlyStream)
    assert best._inner is s_best  # best keeps the full audio bitrate; never worst


def test_resolve_stream_audio_only_kick_demux_uses_480p(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    s_best, s_480p = FakeStream(), FakeStream()
    rec._config.preferred_quality = "audio_only"

    monkeypatch.setattr(rec._session, "resolve_url", lambda url: ("kick", FakePlugin, url))
    monkeypatch.setattr(FakePlugin, "streams", lambda self: {"best": s_best, "480p": s_480p})
    best, _, _, _ = rec._resolve_stream("kick:xqc", None, None)
    assert isinstance(best, _AudioOnlyStream)
    assert best._inner is s_480p


def test_audio_only_stream_remuxes_to_fragmented_mp4(tmp_path):
    gen = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", "-f", "adts", "pipe:1"],
        check=True,
        capture_output=True,
    )

    class AdtsSource:
        def open(self):
            return io.BytesIO(gen.stdout)

    fd = _AudioOnlyStream(AdtsSource()).open()
    chunks = []
    while True:
        chunk = fd.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    fd.close()
    data = b"".join(chunks)
    assert data  # ffmpeg produced audio-only output
    assert data[4:8] == b"ftyp"  # MP4 container, not raw TS/ADTS
    assert b"moof" in data  # fragmented: survives truncation mid-recording

    out = tmp_path / "out.m4a"
    out.write_bytes(data)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    assert probe.stdout.strip() == b"aac"

    # A crash-truncated file must stay playable: empty_moov puts the header
    # first and frag_duration keeps cutting fragments even without video
    # keyframes.
    half = tmp_path / "half.m4a"
    half.write_bytes(data[: len(data) // 2])
    probe_half = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(half),
        ],
        check=True,
        capture_output=True,
    )
    assert float(probe_half.stdout.strip()) > 0
    assert fd._proc.poll() is not None  # close() reaped the process


def test_start_audio_only_forces_disk_mode(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "youtube"
    config.channel_preferred_qualities = {"twitch:ch": "audio_only"}
    rec = Recorder(config, youtube_streamer=FakeYouTubeStreamer())
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    assert rec._effective_mode("twitch:ch") == "disk"

    async def scenario():
        assert await rec.start("twitch:ch") is True
        entry = rec._recordings["twitch:ch"]
        assert entry["mode"] == "disk"
        assert entry["quality"] == "audio_only"
        assert entry["filepath"].endswith(".m4a")

    asyncio.run(scenario())


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


def test_restart_applies_new_mode_preserves_metadata(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "disk"
    rec = Recorder(config, youtube_streamer=FakeYouTubeStreamer())
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    def resolve(*a):
        return (SustainedStream(), "author", "Title", "Game")

    monkeypatch.setattr(rec, "_resolve_stream", resolve)

    async def scenario():
        assert await rec.start("ch", title="T", game="G") is True
        assert rec._recordings["ch"]["title"] == "T"
        config.output_mode = "youtube"
        assert await rec.restart("ch") is True
        assert rec._recordings["ch"]["mode"] == "youtube"
        assert rec._recordings["ch"]["title"] == "T"
        settings = rec.recording_settings()["ch"]
        assert settings["output_mode"] == "youtube"
        assert settings["preferred_quality"] == "best"
        await rec.stop("ch")

    asyncio.run(scenario())


def test_restart_youtube_sends_youtube_live_notification(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "disk"
    notifier = FakeNotifier()
    rec = Recorder(config, youtube_streamer=FakeYouTubeStreamer(), notifier=notifier)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    def resolve(*a):
        return (SustainedStream(), "author", "Title", "Game")

    monkeypatch.setattr(rec, "_resolve_stream", resolve)

    async def scenario():
        assert await rec.start("ch") is True
        assert len(notifier.live) == 1
        assert len(notifier.live[0][0]) == 4  # disk-mode start: twitch link only
        config.output_mode = "youtube"
        assert await rec.restart("ch") is True
        await asyncio.sleep(0.05)  # let the tracked youtube task create the broadcast
        assert len(notifier.live) == 2
        args, _ = notifier.live[1]
        assert args[0] == "ch"
        assert len(args) == 5
        assert args[4] == "https://youtu.be/x"  # new broadcast link
        await rec.stop("ch")

    asyncio.run(scenario())


def test_restart_disk_suppresses_live_notification(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "disk"
    notifier = FakeNotifier()
    rec = Recorder(config, notifier=notifier)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (SustainedStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("ch") is True
        assert len(notifier.live) == 1  # from the initial start
        assert await rec.restart("ch") is True
        await asyncio.sleep(0.05)
        assert len(notifier.live) == 1  # apply-now restart: no extra live notification
        await rec.stop("ch")

    asyncio.run(scenario())


def test_recording_settings_chat_state_twitch(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.record_chat = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (SustainedStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("ch") is True
        assert rec.recording_settings()["ch"]["record_chat"] is True
        await rec.stop_chat("ch")
        assert rec.recording_settings()["ch"]["record_chat"] is False
        await rec.stop("ch")

    asyncio.run(scenario())


def test_restart_not_recording_returns_false(tmp_path, monkeypatch):
    rec = Recorder(make_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (SustainedStream(), "author", "Title", "Game"))
    assert asyncio.run(rec.restart("ch")) is False


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
    config.disk = {"max_total_gb": 5, "delete_oldest": False, "check_interval_s": 0.01}
    notifier = FakeNotifier()
    rec = Recorder(config, notifier=notifier)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (SustainedStream(), "author", "Title", "Game"))

    async def fake_snapshot(config):
        return {"free_gb": 100.0, "dir_gb": 6.0, "file_count": 1}

    monkeypatch.setattr("stream_archive.disk.disk_snapshot", fake_snapshot)

    async def scenario():
        assert await rec.start("ch") is True
        await asyncio.sleep(0.05)
        assert "ch" not in rec._recordings

    asyncio.run(scenario())
    assert any("Stopped recording ch" in m for m in notifier.messages)
    assert any("archive at 5 GB cap" in m for m in notifier.messages)


def test_abort_serializes_with_stop(tmp_path):
    """Watchdog _abort racing monitor stop() on the same channel must tear
    down the recording exactly once, never double-pop the entry."""
    rec = Recorder(make_config(tmp_path))

    async def scenario():
        released = asyncio.Event()

        async def feed():
            await released.wait()  # recording task runs until torn down

        task = asyncio.create_task(feed())
        rec._recordings["ch"] = {"tasks": [task], "process": None, "youtube_info": None, "kick_chat": None}

        results = await asyncio.gather(rec._abort("ch", "cap"), rec.stop("ch"))
        released.set()

        assert "ch" not in rec._recordings  # removed exactly once
        assert task.cancelled()  # whichever path won the lock tore it down
        # The loser sees no entry. stop() returns None when abort wins, or a
        # result with no file info when stop wins. Either way no RuntimeError
        # surfaces.
        assert results[1] is None or results[1] == {"file_info": None, "youtube_info": None}

    asyncio.run(scenario())


def test_delete_oldest_to_cap_deletes_oldest(tmp_path):
    config = make_config(tmp_path)
    config.disk = {"max_total_gb": 2.5e-6}  # ~2.6 KB cap
    rec = Recorder(config)
    base = tmp_path / "recordings" / "ch"
    base.mkdir(parents=True, exist_ok=True)
    t0 = time.time() - 100
    for i, name in enumerate(["old.m4a", "mid.ts", "new.ts"]):
        p = base / name
        p.write_bytes(b"x" * 1024)
        os.utime(p, (t0 + i, t0 + i))

    removed, freed = asyncio.run(rec.delete_oldest_to_cap())

    assert removed == 1
    assert freed == 1024
    assert not (base / "old.m4a").exists()
    assert (base / "mid.ts").exists()
    assert (base / "new.ts").exists()


def test_delete_oldest_spares_active_recording(tmp_path):
    """Cap deletion must skip the file currently being written.

    Deletion falls through to the next-oldest candidate instead.
    """
    config = make_config(tmp_path)
    config.disk = {"max_total_gb": 2.5e-6}  # ~2.6 KB cap
    rec = Recorder(config)
    base = tmp_path / "recordings" / "ch"
    base.mkdir(parents=True, exist_ok=True)
    t0 = time.time() - 100
    for i, name in enumerate(["old.ts", "mid.ts", "new.ts"]):
        p = base / name
        p.write_bytes(b"x" * 1024)
        os.utime(p, (t0 + i, t0 + i))
    rec._recordings["ch"] = {"filepath": str(base / "old.ts")}

    removed, freed = asyncio.run(rec.delete_oldest_to_cap())

    assert (removed, freed) == (1, 1024)
    assert (base / "old.ts").exists()  # active recording is never a deletion candidate
    assert not (base / "mid.ts").exists()  # next-oldest takes the hit instead
    assert (base / "new.ts").exists()


def make_kick_config(tmp_path, record_chat=True):
    config = make_config(tmp_path)
    config.kick = {"client_id": "cid", "client_secret": "cs", "record_chat": record_chat}
    config.channels = ["kick:xqc"]
    config.record_chat = True
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

    # Without plugin metadata, the bare slug is the author fallback.
    CapturingFakePlugin.author = None
    best, author, title, game = rec._resolve_stream("kick:xqc", None, None)
    assert author == "xqc"


def test_start_twitch_prefixed_uses_twitch_dir(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.channels = ["twitch:streamer1"]
    config.record_chat = True
    rec = Recorder(config)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    monkeypatch.setattr("stream_archive.recorder.core.ChatRecorder", FakeChatRecorder)
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
    monkeypatch.setattr("stream_archive.recorder.core.ChatRecorder", FakeChatRecorder)
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


class FakeKeepaliveProc:
    """Keep-alive process stand-in.

    wait() blocks until terminated. If exit_immediately is set, the process
    exits immediately to cover the early-death path.
    """

    def __init__(self, exit_immediately=False):
        self._rc = 1 if exit_immediately else None
        self.terminated = False
        self.killed = False

    @property
    def returncode(self):
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = 1

    def kill(self):
        self.killed = True
        self._rc = 1

    async def wait(self):
        while self._rc is None:
            await asyncio.sleep(0.01)
        return self._rc


def test_hold_delays_end_and_reuses_broadcast(tmp_path, monkeypatch):
    """A clean source end defers the broadcast end.

    A return within the hold delay reuses the same broadcast without a
    second create_stream call.
    """
    config = make_config(tmp_path)
    config.output_mode = "youtube"
    config.youtube.hold_seconds = 60
    yt = EndingYouTubeStreamer()
    rec = Recorder(config, youtube_streamer=yt)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))

    async def no_keepalive(rtmp_url):
        return None

    monkeypatch.setattr(rec, "_start_keepalive", no_keepalive)

    async def scenario():
        # Feed ends cleanly on its own -> broadcast held, not ended
        assert await rec.start("ch") is True
        for _ in range(200):  # real ffmpeg spawn/EOF takes a few ticks
            if "ch" in rec._held:
                break
            await asyncio.sleep(0.01)
        assert yt.ended == []
        assert "ch" in rec._held
        assert rec._held["ch"]["youtube_info"]["broadcast_id"] == "b1"
        assert rec.ended_clean("ch")
        assert yt.create_count == 1  # the fresh create above

        # Streamer returns within the hold -> same broadcast, no new create
        monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (SustainedStream(), "author", "Title", "Game"))
        assert await rec.start("ch") is True
        await asyncio.sleep(0.05)
        assert yt.create_count == 1  # no new create: held broadcast reused
        assert rec._recordings["ch"]["youtube_info"]["broadcast_id"] == "b1"
        assert rec._held == {}  # hold consumed by the reuse

        # Stop path also holds instead of ending
        await rec.stop("ch")
        assert yt.ended == []
        assert "ch" in rec._held
        await rec.close()  # flush the hold

    asyncio.run(scenario())
    assert yt.ended == ["b1"]
    assert rec._held == {}


def test_hold_expiry_ends_broadcast(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "youtube"
    config.youtube.hold_seconds = 0.05
    yt = EndingYouTubeStreamer()
    rec = Recorder(config, youtube_streamer=yt)

    async def no_keepalive(rtmp_url):
        return None

    monkeypatch.setattr(rec, "_start_keepalive", no_keepalive)

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(0))
        rec._recordings["ch"] = {
            "tasks": [task],
            "process": None,
            "youtube_info": {"broadcast_id": "b1", "rtmp_url": "rtmp://x"},
            "kick_chat": None,
        }
        task.add_done_callback(lambda t: rec._on_task_finished("ch", t))
        await task
        await asyncio.sleep(0.15)  # beyond the 0.05s hold
        assert yt.ended == ["b1"]
        assert rec._held == {}

    asyncio.run(scenario())


def test_stop_schedules_hold(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "youtube"
    config.youtube.hold_seconds = 60
    yt = EndingYouTubeStreamer()
    rec = Recorder(config, youtube_streamer=yt)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    async def no_keepalive(rtmp_url):
        return None

    monkeypatch.setattr(rec, "_start_keepalive", no_keepalive)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (SustainedStream(), "author", "Title", "Game"))

    async def scenario():
        assert await rec.start("ch") is True
        await asyncio.sleep(0.05)
        await rec.stop("ch")
        assert yt.ended == []  # deferred
        assert "ch" in rec._held
        await rec.close()  # clean shutdown flushes the hold

    asyncio.run(scenario())
    assert yt.ended == ["b1"]
    assert rec._held == {}


def test_failed_reused_task_ends_immediately(tmp_path, monkeypatch):
    """A reuse whose feed dies again is a dead broadcast: end now, no hold loop."""
    config = make_config(tmp_path)
    config.output_mode = "youtube"
    config.youtube.hold_seconds = 60
    yt = EndingYouTubeStreamer()
    rec = Recorder(config, youtube_streamer=yt)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    async def no_keepalive(rtmp_url):
        return None

    monkeypatch.setattr(rec, "_start_keepalive", no_keepalive)

    async def scenario():
        async def boom():
            raise RuntimeError("stream interrupted")

        task = asyncio.create_task(boom())
        rec._recordings["ch"] = {
            "tasks": [task],
            "process": None,
            "youtube_info": {"broadcast_id": "b1"},
            "kick_chat": None,
            "mode": "youtube",
            "started_at": time.monotonic() - 300,  # avoid quick-end backoff noise
            "reused": True,
        }
        task.add_done_callback(lambda t: rec._on_task_finished("ch", t))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.05)
        assert yt.ended == ["b1"]
        assert rec._held == {}

    asyncio.run(scenario())


def test_hold_bypasses_restart_gates(tmp_path, monkeypatch):
    """A pending hold is free to reuse: no new broadcast, so no backoff/budget gate."""
    config = make_config(tmp_path)
    config.output_mode = "youtube"
    rec = Recorder(config, youtube_streamer=FakeYouTubeStreamer())

    async def scenario():
        rec._held["ch"] = {
            "youtube_info": {"broadcast_id": "b1", "rtmp_url": "rtmp://x"},
            "end_task": asyncio.create_task(asyncio.sleep(60)),
            "keepalive": None,
        }
        rec._backoff_until["ch"] = time.monotonic() + 999
        rec._youtube_starts = [time.time() - i * 60 for i in range(10)]
        assert rec.youtube_restart_blocked_reason("ch") is None

    asyncio.run(scenario())


def test_per_channel_hold_override(tmp_path):
    config = make_config(tmp_path)
    rec = Recorder(config)
    config.channel_youtube_hold_seconds = {"twitch:ch": 60}  # keys normalize on assignment
    assert rec._hold_seconds("twitch:ch") == 60
    assert rec._hold_seconds("kick:a") == 0  # no override: global default 0
    config.youtube.hold_seconds = 120
    config.channel_youtube_hold_seconds["twitch:ch"] = 0  # explicit off beats global
    assert rec._hold_seconds("twitch:ch") == 0


def test_bundled_reconnect_clip_present():
    from stream_archive.recorder.youtube_output import _RECONNECT_CLIP

    assert _RECONNECT_CLIP.is_file()
    assert _RECONNECT_CLIP.stat().st_size > 0


def test_start_keepalive_uses_bundled_clip(tmp_path, monkeypatch):
    from stream_archive.recorder.youtube_output import _RECONNECT_CLIP

    rec = Recorder(make_config(tmp_path))
    recorded = []
    fake_proc = object()

    async def fake_exec(*args, **kwargs):
        recorded.append(args)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(rec._start_keepalive("rtmp://x"))
    assert result is fake_proc
    assert recorded[0] == (
        "ffmpeg",
        "-loglevel",
        "warning",
        "-re",
        "-stream_loop",
        "-1",
        "-i",
        str(_RECONNECT_CLIP),
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-f",
        "flv",
        "-flvflags",
        "no_duration_filesize",
        "rtmp://x",
    )

    async def failing_exec(*args, **kwargs):
        raise FileNotFoundError("ffmpeg missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_exec)
    assert asyncio.run(rec._start_keepalive("rtmp://x")) is None  # spawn failure -> None, no exception


def test_hold_spawns_keepalive_and_stops_on_expiry(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "youtube"
    config.youtube.hold_seconds = 0.05
    yt = EndingYouTubeStreamer()
    rec = Recorder(config, youtube_streamer=yt)
    proc = FakeKeepaliveProc()

    async def fake_start(rtmp_url):
        return proc

    monkeypatch.setattr(rec, "_start_keepalive", fake_start)

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(0))
        rec._recordings["ch"] = {
            "tasks": [task],
            "process": None,
            "youtube_info": {"broadcast_id": "b1", "rtmp_url": "rtmp://x"},
            "kick_chat": None,
        }
        task.add_done_callback(lambda t: rec._on_task_finished("ch", t))
        await task
        await asyncio.sleep(0.15)
        assert yt.ended == ["b1"]
        assert rec._held == {}
        assert proc.terminated

    asyncio.run(scenario())


def test_reuse_stops_keepalive(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.output_mode = "youtube"
    config.youtube.hold_seconds = 60
    yt = EndingYouTubeStreamer()
    rec = Recorder(config, youtube_streamer=yt)
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)

    async def scenario():
        proc = FakeKeepaliveProc()
        rec._held["ch"] = {
            "youtube_info": {"broadcast_id": "b1", "rtmp_url": "rtmp://x"},
            "end_task": asyncio.create_task(asyncio.sleep(60)),
            "keepalive": proc,
        }
        monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (SustainedStream(), "author", "Title", "Game"))
        assert await rec.start("ch") is True
        await asyncio.sleep(0.05)
        assert yt.create_count == 0  # reuse: no new broadcast created
        assert proc.terminated
        assert rec._recordings["ch"]["youtube_info"]["broadcast_id"] == "b1"
        assert rec._recordings["ch"]["reused"] is True
        await rec.stop("ch")
        await rec.close()

    asyncio.run(scenario())


def test_keepalive_early_death_ends_broadcast(tmp_path, monkeypatch):
    """If the keep-alive feed dies early, end now.

    An example is a rejected broadcast. Do not wait out the full delay.
    """
    config = make_config(tmp_path)
    config.output_mode = "youtube"
    config.youtube.hold_seconds = 60
    yt = EndingYouTubeStreamer()
    rec = Recorder(config, youtube_streamer=yt)

    async def dying_keepalive(rtmp_url):
        return FakeKeepaliveProc(exit_immediately=True)

    monkeypatch.setattr(rec, "_start_keepalive", dying_keepalive)

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(0))
        rec._recordings["ch"] = {
            "tasks": [task],
            "process": None,
            "youtube_info": {"broadcast_id": "b1", "rtmp_url": "rtmp://x"},
            "kick_chat": None,
        }
        task.add_done_callback(lambda t: rec._on_task_finished("ch", t))
        await task
        await asyncio.sleep(0.1)
        assert yt.ended == ["b1"]
        assert rec._held == {}

    asyncio.run(scenario())


def test_start_setup_failure_cleans_registered_entry(tmp_path, monkeypatch):
    """An OSError after registration must not strand a taskless zombie entry.

    If the OS denies makedirs after registration, start returns False and
    nothing stays registered. A retry succeeds once the cause is gone.
    """
    rec = Recorder(make_config(tmp_path))
    monkeypatch.setattr(rec, "_load_plugin", lambda: None)
    monkeypatch.setattr(rec, "_resolve_stream", lambda *a: (FakeStream(), "author", "Title", "Game"))
    real_makedirs = os.makedirs
    denied = {"on": True}

    def flaky_makedirs(path, *a, **k):
        if denied["on"]:
            raise PermissionError(f"denied: {path}")
        return real_makedirs(path, *a, **k)

    monkeypatch.setattr(os, "makedirs", flaky_makedirs)

    async def scenario():
        assert await rec.start("ch") is False
        assert "ch" not in rec._recordings

        denied["on"] = False  # cause removed: the next cycle retries successfully
        assert await rec.start("ch") is True
        assert "ch" in rec._recordings
        await rec.stop("ch")

    asyncio.run(scenario())


def test_cleanup_spares_active_recording(tmp_path):
    """Retention cleanup must not unlink an in-flight .ts file.

    Even when its mtime aged past retention_days, a stalled feed keeps
    writing through its open fd.
    """
    rec = Recorder(make_config(tmp_path))
    old_active = tmp_path / "recordings" / "ch" / "active.ts"
    old_idle = tmp_path / "recordings" / "ch" / "idle.ts"
    t = time.time() - 30 * 86400
    seed_recording(old_active, t)
    seed_recording(old_idle, t)
    rec._recordings["ch"] = {"tasks": [], "process": None, "youtube_info": None, "filepath": str(old_active)}

    removed = asyncio.run(rec.cleanup_old_recordings(7))

    assert removed == 1
    assert old_active.exists()
    assert not old_idle.exists()
