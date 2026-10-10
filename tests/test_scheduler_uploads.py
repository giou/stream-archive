"""Auto-upload submit guard: the monitor hook handler in the scheduler."""

import asyncio
import json

from conftest import make_config as valid_config

from stream_archive import events as events_mod
from stream_archive.scheduler import _auto_upload_file
from stream_archive.uploads import UploadHub


def make_config(tmp_path, **overrides):
    cfg = valid_config(channels=["twitch:ch"], recording_dir=str(tmp_path), **overrides)
    cfg._workdir = tmp_path
    cfg._config_path = tmp_path / "config.json"
    return cfg


def enable_auto_upload(config, tmp_path):
    """Flag the channel on and authenticate YouTube."""
    config.channel_youtube_vod_upload["twitch:ch"] = True
    (tmp_path / "youtube_token.json").write_text(json.dumps({"refresh_token": "rt"}))


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def notify(self, text):
        self.messages.append(text)


async def wait_until(condition, timeout=5.0):
    async def _poll():
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


def test_auto_upload_skips_without_flag_token_or_file(tmp_path, monkeypatch):
    """Off, unauthenticated, gone, and already-uploaded files never submit."""
    from stream_archive.youtube_upload import write_youtube_url

    recorded = []
    monkeypatch.setattr(events_mod, "record", lambda kind, channel, text: recorded.append((kind, channel, text)))

    async def scenario():
        hub = UploadHub()
        notifier = FakeNotifier()
        runner_calls = []

        async def runner(progress):
            runner_calls.append(True)
            return "https://www.youtube.com/watch?v=x"

        factory = lambda channel, path: runner  # noqa: E731

        # Flag off: nothing happens, even for a good file.
        config = make_config(tmp_path)
        target = tmp_path / "show.mp4"
        target.write_bytes(b"x" * 10)
        await _auto_upload_file(
            "twitch:ch", str(target), config=config, hub=hub, runner_factory=factory, notifier=notifier
        )
        # Flag on but no token: quiet skip, no failure notice.
        enable_auto_upload(config, tmp_path)
        (tmp_path / "youtube_token.json").unlink()
        await _auto_upload_file(
            "twitch:ch", str(target), config=config, hub=hub, runner_factory=factory, notifier=notifier
        )
        # Gone file: skip instead of a failing hub task plus a scary notice.
        (tmp_path / "youtube_token.json").write_text(json.dumps({"refresh_token": "rt"}))
        await _auto_upload_file(
            "twitch:ch", str(tmp_path / "gone.mp4"), config=config, hub=hub, runner_factory=factory, notifier=notifier
        )
        # Already uploaded: no second upload.
        write_youtube_url(target, "https://www.youtube.com/watch?v=old")
        await _auto_upload_file(
            "twitch:ch", str(target), config=config, hub=hub, runner_factory=factory, notifier=notifier
        )
        assert runner_calls == []
        assert hub.snapshot() == []
        assert notifier.messages == []
        assert recorded == []

    asyncio.run(scenario())


def test_auto_upload_submits_and_reports(tmp_path, monkeypatch):
    """A flagged finished file uploads, and events plus notices follow it."""
    from stream_archive.youtube_upload import read_youtube_url

    recorded = []
    monkeypatch.setattr(events_mod, "record", lambda kind, channel, text: recorded.append((kind, channel, text)))

    async def scenario():
        hub = UploadHub()
        notifier = FakeNotifier()
        config = make_config(tmp_path)
        enable_auto_upload(config, tmp_path)
        target = tmp_path / "show.mp4"
        target.write_bytes(b"x" * 10)

        async def runner(progress):
            return "https://www.youtube.com/watch?v=auto1"

        await _auto_upload_file(
            "twitch:ch",
            str(target),
            config=config,
            hub=hub,
            runner_factory=lambda channel, path: runner,
            notifier=notifier,
        )
        await wait_until(lambda: not hub._tasks)
        return hub, notifier

    hub, notifier = asyncio.run(scenario())
    snap = {item["id"]: item for item in hub.snapshot()}
    assert len(snap) == 1
    assert next(iter(snap.values()))["status"] == "done"
    assert ("notice", "twitch:ch", "YouTube upload started: show.mp4") in recorded
    assert any("done: show.mp4 https://www.youtube.com/watch?v=auto1" in text for _, _, text in recorded)
    assert any("watch?v=auto1" in message for message in notifier.messages)
    assert read_youtube_url(tmp_path / "show.mp4") == "https://www.youtube.com/watch?v=auto1"


def test_auto_upload_failure_notifies_once(tmp_path, monkeypatch):
    """A failing auto-upload records the failure and tells the operator."""
    recorded = []
    monkeypatch.setattr(events_mod, "record", lambda kind, channel, text: recorded.append((kind, channel, text)))

    async def scenario():
        hub = UploadHub()
        notifier = FakeNotifier()
        config = make_config(tmp_path)
        enable_auto_upload(config, tmp_path)
        target = tmp_path / "show.mp4"
        target.write_bytes(b"x" * 10)

        async def boom(progress):
            msg = "quota exceeded"
            raise RuntimeError(msg)

        await _auto_upload_file(
            "twitch:ch",
            str(target),
            config=config,
            hub=hub,
            runner_factory=lambda channel, path: boom,
            notifier=notifier,
        )
        await wait_until(lambda: not hub._tasks)
        return hub, notifier

    hub, notifier = asyncio.run(scenario())
    snap = {item["id"]: item for item in hub.snapshot()}
    assert next(iter(snap.values()))["status"] == "failed"
    assert any("failed: show.mp4" in text for _, _, text in recorded)
    assert any("YouTube upload failed" in message for message in notifier.messages)
