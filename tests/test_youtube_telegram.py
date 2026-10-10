"""YouTube upload from the Telegram recordings browser.

The browser tests drive the controller through reply text and callbacks.
A fake runner stands in for the YouTube API: no network, no token refresh.
"""

import asyncio
import json
from types import SimpleNamespace

from conftest import make_config as valid_config

from stream_archive import disk
from stream_archive.config import get_config
from stream_archive.telegram import TelegramController
from stream_archive.uploads import UploadHub


class FakeRecorder:
    def __init__(self):
        self._paths = set()

    def _active_paths(self):
        return set(self._paths)


class FakeMonitor:
    pass


class FakeEventSub:
    pass


class FakeKickWebhook:
    async def apply_state(self):
        pass

    async def add_channel(self, channel):
        pass

    async def remove_channel(self, channel):
        pass

    async def sync_channels(self, channels):
        pass


class FakeNotice:
    def __init__(self):
        self.edits = []
        self.markup = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append(text)
        self.markup.append(reply_markup)


class FakeBot:
    def __init__(self):
        self.sent = []
        self.notices = []

    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        notice = FakeNotice()
        self.notices.append(notice)
        return notice


def make_bot(tmp_path, files=(), youtube=False):
    data = valid_config(channels=["twitch:channel1"]).model_dump(mode="json", exclude_unset=True)
    (tmp_path / "config.json").write_text(json.dumps(data))
    config = get_config(tmp_path / "config.json")
    if youtube:
        (tmp_path / "youtube_token.json").write_text(json.dumps({"refresh_token": "rt"}))
    rec_dir = disk.resolve_recording_dir(config) / "twitch" / "channel1"
    rec_dir.mkdir(parents=True)
    for name, size in files:
        (rec_dir / name).write_bytes(b"x" * size)
    ctrl = TelegramController(config, FakeRecorder(), FakeMonitor(), FakeEventSub(), kick_webhook=FakeKickWebhook())
    ctrl._uploads = UploadHub()
    bot = FakeBot()
    ctrl._app = SimpleNamespace(bot=bot)
    return config, ctrl, bot


def labels_of(markup):
    return [b["text"] for row in markup.to_dict()["keyboard"] for b in row]


async def wait_until(condition, timeout=5.0):
    async def _poll():
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


def open_detail(ctrl, name):
    _, markup = asyncio.run(ctrl.handle_reply_text("Recordings"))
    rows = markup.to_dict()["keyboard"]
    channel_row = next(b["text"] for row in rows for b in row if "twitch:channel1" in b["text"])
    _, markup = asyncio.run(ctrl.handle_reply_text(channel_row))
    rows = markup.to_dict()["keyboard"]
    row = next(b["text"] for row in rows for b in row if name in b["text"])
    return asyncio.run(ctrl.handle_reply_text(row))


async def open_detail_async(ctrl, name):
    """Same path inside one loop, so background upload tasks can run."""
    _, markup = await ctrl.handle_reply_text("Recordings")
    rows = markup.to_dict()["keyboard"]
    channel_row = next(b["text"] for row in rows for b in row if "twitch:channel1" in b["text"])
    _, markup = await ctrl.handle_reply_text(channel_row)
    rows = markup.to_dict()["keyboard"]
    row = next(b["text"] for row in rows for b in row if name in b["text"])
    return await ctrl.handle_reply_text(row)


def test_upload_menu_offers_both_targets_when_configured(tmp_path):
    _, ctrl, _ = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=True)
    open_detail(ctrl, "a.mp4")
    text, markup = asyncio.run(ctrl.handle_reply_text("Upload"))
    assert ctrl._state_for(12345).menu == "rec_upload"
    labels = labels_of(markup)
    assert "Telegram" in labels
    assert "YouTube" in labels
    assert "Back" in labels


def test_upload_menu_hides_youtube_when_not_configured(tmp_path):
    _, ctrl, _ = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=False)
    open_detail(ctrl, "a.mp4")
    _, markup = asyncio.run(ctrl.handle_reply_text("Upload"))
    labels = labels_of(markup)
    assert "Telegram" in labels
    assert "YouTube" not in labels


def test_upload_back_returns_to_detail(tmp_path):
    _, ctrl, _ = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=True)
    open_detail(ctrl, "a.mp4")
    asyncio.run(ctrl.handle_reply_text("Upload"))
    text, _ = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._state_for(12345).menu == "rec_detail"
    assert "a.mp4" in text


def test_youtube_pick_uploads_and_links(tmp_path):
    _, ctrl, bot = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=True)

    async def runner(progress):
        progress(100, 100, "upload")
        return "https://www.youtube.com/watch?v=vid1"

    ctrl._youtube_runner = lambda channel, path: runner

    async def scenario():
        await open_detail_async(ctrl, "a.mp4")
        await ctrl.handle_reply_text("Upload")
        result = await ctrl.handle_reply_text("YouTube")
        assert result is None
        await wait_until(lambda: not ctrl._youtube_tasks and not ctrl._uploads._tasks)

    asyncio.run(scenario())
    notice = bot.notices[-1]
    assert notice.edits, "the progress message must announce the finished upload"
    assert "https://www.youtube.com/watch?v=vid1" in notice.edits[-1]
    snap = ctrl._uploads.snapshot()
    assert snap[0]["status"] == "done"
    assert snap[0]["channel"] == "twitch:channel1"


def test_youtube_stop_cancels_upload(tmp_path):
    _, ctrl, bot = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=True)
    gate = asyncio.Event()

    async def runner(progress):
        await gate.wait()
        return "https://www.youtube.com/watch?v=vid1"

    ctrl._youtube_runner = lambda channel, path: runner

    async def scenario():
        await open_detail_async(ctrl, "a.mp4")
        await ctrl.handle_reply_text("Upload")
        await ctrl.handle_reply_text("YouTube")
        await wait_until(lambda: len(ctrl._uploads._tasks) == 1)
        stop_data = bot.sent[-1][2].to_dict()["inline_keyboard"][0][0]["callback_data"]
        assert stop_data.startswith("youtube_stop:")
        result = await ctrl.handle_callback(stop_data, 12345)
        assert result is None
        gate.set()
        await wait_until(lambda: not ctrl._youtube_tasks and not ctrl._uploads._tasks)

    asyncio.run(scenario())
    notice = bot.notices[-1]
    assert any("Stopped a.mp4" in edit for edit in notice.edits)


def test_youtube_double_start_is_rejected(tmp_path):
    _, ctrl, bot = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=True)
    gate = asyncio.Event()

    async def runner(progress):
        await gate.wait()
        return "url"

    ctrl._youtube_runner = lambda channel, path: runner

    async def scenario():
        await open_detail_async(ctrl, "a.mp4")
        await ctrl.handle_reply_text("Upload")
        await ctrl.handle_reply_text("YouTube")
        await wait_until(lambda: len(ctrl._uploads._tasks) == 1)
        before = len(bot.sent)
        text, _ = await ctrl.handle_reply_text("YouTube")
        assert "already runs" in text
        assert len(bot.sent) == before
        gate.set()
        await wait_until(lambda: not ctrl._youtube_tasks and not ctrl._uploads._tasks)

    asyncio.run(scenario())


def test_youtube_pick_needs_token(tmp_path):
    _, ctrl, bot = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=False)
    ctrl._youtube_runner = lambda channel, path: None
    open_detail(ctrl, "a.mp4")
    asyncio.run(ctrl.handle_reply_text("Upload"))
    text, _ = asyncio.run(ctrl.handle_reply_text("YouTube"))
    assert "not configured" in text
    assert ctrl._uploads.snapshot() == []


def test_youtube_reupload_asks_confirm(tmp_path):
    from stream_archive.youtube_upload import write_youtube_url

    _, ctrl, bot = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=True)

    async def runner(progress):
        progress(100, 100, "upload")
        return "https://www.youtube.com/watch?v=vid2"

    ctrl._youtube_runner = lambda channel, path: runner

    async def scenario():
        await open_detail_async(ctrl, "a.mp4")
        rec_dir = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1"
        write_youtube_url(rec_dir / "a.mp4", "https://www.youtube.com/watch?v=vid1")
        text, _ = await ctrl.handle_reply_text("Upload")
        assert "Already on YouTube: https://www.youtube.com/watch?v=vid1" in text
        text, markup = await ctrl.handle_reply_text("YouTube")
        assert "Already on YouTube: https://www.youtube.com/watch?v=vid1" in text
        assert "replace it" in text
        assert not ctrl._youtube_tasks, "the confirm must precede the upload"
        cb = markup.to_dict()["inline_keyboard"][0][0]["callback_data"]
        assert cb.startswith("confirm_ytupload:")
        result = await ctrl.handle_callback(cb, 12345)
        assert result is None
        await wait_until(lambda: not ctrl._youtube_tasks and not ctrl._uploads._tasks)

    asyncio.run(scenario())
    notice = bot.notices[-1]
    assert "https://www.youtube.com/watch?v=vid2" in notice.edits[-1]


def test_youtube_detail_shows_remembered_url(tmp_path):
    from stream_archive.youtube_upload import write_youtube_url

    _, ctrl, _ = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=True)
    rec_dir = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1"
    write_youtube_url(rec_dir / "a.mp4", "https://www.youtube.com/watch?v=vid1")
    text, _ = open_detail(ctrl, "a.mp4")
    assert "YouTube: https://www.youtube.com/watch?v=vid1" in text
    assert "Uploadable" not in text


def test_youtube_failure_edits_notice(tmp_path):
    """A failed upload edits the progress notice in place and drops Stop."""
    _, ctrl, bot = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=True)

    async def runner(progress):
        msg = "quota exceeded"
        raise RuntimeError(msg)

    ctrl._youtube_runner = lambda channel, path: runner

    async def scenario():
        await open_detail_async(ctrl, "a.mp4")
        await ctrl.handle_reply_text("Upload")
        result = await ctrl.handle_reply_text("YouTube")
        assert result is None
        await wait_until(lambda: not ctrl._youtube_tasks and not ctrl._uploads._tasks)

    asyncio.run(scenario())
    notice = bot.notices[-1]
    assert any("failed" in edit for edit in notice.edits)
    assert notice.markup[-1] is None
    assert len(bot.sent) == 1


def test_youtube_concurrent_starts_submit_once(tmp_path):
    """Two starts racing the notice send still submit a single upload."""
    _, ctrl, bot = make_bot(tmp_path, files=[("a.mp4", 100)], youtube=True)
    gate = asyncio.Event()

    async def runner(progress):
        await gate.wait()
        return "https://www.youtube.com/watch?v=race"

    ctrl._youtube_runner = lambda channel, path: runner

    async def scenario():
        await open_detail_async(ctrl, "a.mp4")
        await ctrl.handle_reply_text("Upload")
        await asyncio.gather(
            ctrl.handle_reply_text("YouTube"),
            ctrl.handle_reply_text("YouTube"),
        )
        await wait_until(lambda: len(ctrl._uploads._tasks) == 1)
        gate.set()
        await wait_until(lambda: not ctrl._youtube_tasks and not ctrl._uploads._tasks)

    asyncio.run(scenario())
    assert len(ctrl._uploads.snapshot()) == 1
    assert any("already runs" in edit for notice in bot.notices for edit in notice.edits)
