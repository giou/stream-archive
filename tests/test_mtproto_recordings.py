"""MTProto uploader and recordings browser tests.

The uploader tests use a fake Telethon client: no network, no session
file. The browser tests write real files under tmp_path and drive the
controller through its reply-text and callback entries.
"""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_config as valid_config

from stream_archive import disk
from stream_archive.config import get_config
from stream_archive.mtproto_upload import BOT_API_BYTES, MAX_UPLOAD_BYTES, MtprotoUploader, check_sendable
from stream_archive.telegram import TelegramController


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = None
        self.sent = []
        self.disconnected = False

    async def start(self, bot_token=None):
        self.started = bot_token

    def is_connected(self):
        return not self.disconnected

    async def disconnect(self):
        self.disconnected = True

    async def send_file(
        self, chat_id, path, caption=None, supports_streaming=None, progress_callback=None, thumb=None, attributes=None
    ):
        self.sent.append((chat_id, str(path), caption, supports_streaming))
        self.thumb = thumb
        self.attributes = attributes
        if progress_callback:
            progress_callback(1, 1)

    async def upload_file(self, path, part_size_kb=None, progress_callback=None):
        data = Path(path).read_bytes()
        self.uploaded_parts = getattr(self, "uploaded_parts", [])
        self.uploaded_parts.append((str(path), part_size_kb, len(data)))
        if progress_callback:
            progress_callback(len(data), len(data))
        return SimpleNamespace(id=1, parts=1, name=Path(path).name)


class RawFakeClient(FakeClient):
    """Double with Telethon raw-call support for the parallel path."""

    def __init__(self, *args, part_delay=0.05, **kwargs):
        super().__init__(*args, **kwargs)
        self.part_delay = part_delay
        self.max_inflight = 0
        self._inflight = 0
        self.saved_parts: list[tuple[int, int]] = []
        self._lock = asyncio.Lock()

    async def __call__(self, request):
        async with self._lock:
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            await asyncio.sleep(self.part_delay)
            self.saved_parts.append((request.file_id, request.file_part))
            return True
        finally:
            async with self._lock:
                self._inflight -= 1

    async def send_file(self, chat_id, path, **kwargs):
        # The parallel path passes an InputFileBig handle, not a path.
        self.sent.append((chat_id, repr(path), kwargs.get("caption"), kwargs.get("supports_streaming")))
        self.thumb = kwargs.get("thumb")
        self.attributes = kwargs.get("attributes")


def factory_holder():
    made: list[FakeClient] = []

    def factory(*args, **kwargs):
        client = FakeClient(*args, **kwargs)
        made.append(client)
        return client

    return factory, made


def uploader_config(tmp_path, **overrides):
    (tmp_path / "config.json").write_text(
        json.dumps(valid_config(channels=["twitch:channel1"]).model_dump(mode="json", exclude_unset=True))
    )
    config = get_config(tmp_path / "config.json")
    for key, value in overrides.items():
        setattr(config.mtproto, key, value)
    return config


def test_check_sendable_caps(tmp_path):
    small = tmp_path / "a.ts"
    small.write_bytes(b"x" * 10)
    ok, _ = check_sendable(small)
    assert ok is True
    ok, note = check_sendable(tmp_path / "missing.ts")
    assert ok is False
    assert "gone" in note
    empty = tmp_path / "empty.ts"
    empty.write_bytes(b"")
    ok, note = check_sendable(empty)
    assert ok is False
    assert "empty" in note
    assert BOT_API_BYTES == 50 * 1024 * 1024
    assert MAX_UPLOAD_BYTES == 4000 * 512 * 1024


def test_uploader_connect_and_send(tmp_path):
    config = uploader_config(tmp_path)
    config.mtproto.api_id = 1
    config.mtproto.api_hash = "hash"
    config.mtproto.enabled = True
    factory, made = factory_holder()
    up = MtprotoUploader(config, client_factory=factory)
    assert up.enabled is True
    assert up.connected is False
    asyncio.run(up.connect())
    assert up.connected is True
    assert made[0].started == "bot_token"
    target = tmp_path / "rec.ts"
    target.write_bytes(b"x" * 10)
    asyncio.run(up.send_video(target, 12345, caption="rec.ts"))
    assert made[0].sent[0][0] == 12345
    assert made[0].sent[0][2] == "rec.ts"
    assert made[0].sent[0][3] is True
    assert made[0].uploaded_parts, "fallback upload_file must run first"
    asyncio.run(up.disconnect())
    assert up.connected is False
    assert made[0].disconnected is True


def test_send_marks_mp4_streamable(tmp_path):
    import subprocess

    gen = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(tmp_path / "cap.ts"),
        ],
        capture_output=True,
    )
    if gen.returncode != 0:
        import pytest as _pytest

        _pytest.skip("cannot generate a TS fixture here")
    config = uploader_config(tmp_path)
    config.mtproto.api_id = 1
    config.mtproto.api_hash = "hash"
    config.mtproto.enabled = True
    factory, made = factory_holder()
    up = MtprotoUploader(config, client_factory=factory)
    asyncio.run(up.connect())
    asyncio.run(up.send_video(tmp_path / "cap.ts", 99, caption="cap"))
    assert made[0].sent[0][0] == 99
    assert "cap.mp4" in made[0].sent[0][1]
    assert made[0].attributes is not None and len(made[0].attributes) == 1
    assert not (tmp_path / "cap.ts").exists()


def test_uploader_refuses_when_off_or_missing(tmp_path):
    config = uploader_config(tmp_path)
    factory, _ = factory_holder()
    up = MtprotoUploader(config, client_factory=factory)
    assert up.enabled is False
    target = tmp_path / "rec.ts"
    target.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="off"):
        asyncio.run(up.send_video(target, 1))


def test_uploader_refuses_large_file(tmp_path):
    from unittest import mock

    config = uploader_config(tmp_path)
    config.mtproto.api_id = 1
    config.mtproto.api_hash = "hash"
    config.mtproto.enabled = True
    factory, made = factory_holder()
    up = MtprotoUploader(config, client_factory=factory)
    asyncio.run(up.connect())
    target = tmp_path / "big.ts"
    target.write_bytes(b"x")
    with (
        mock.patch("stream_archive.mtproto_upload.check_sendable", return_value=(False, "too big")),
        pytest.raises(ValueError, match="too big"),
    ):
        asyncio.run(up.send_video(target, 1))
    assert made[0].sent == []


class FakeRecorder:
    def __init__(self):
        self.snapshot = {
            "free_gb": 100.0,
            "total_fs_gb": 500.0,
            "used_fs_gb": 400.0,
            "dir_gb": 0.0,
            "file_count": 0,
            "chat_gb": 0.0,
            "chat_count": 0,
            "archive_gb": 0.0,
            "dir": "recordings",
        }

    def recording_settings(self):
        return {}

    def active_channels(self):
        return []

    async def stop_chat(self, channel, platform=None):
        pass

    async def disk_snapshot(self):
        return self.snapshot

    def recording_info(self):
        return []

    def _active_paths(self):
        return set()

    def _remove_if_inactive(self, path, active):
        if os.path.realpath(path) in active:
            return None
        size = Path(path).stat().st_size
        Path(path).unlink(missing_ok=True)
        return size


class FakeMonitor:
    def remove_channel(self, channel):
        pass


class FakeEventSub:
    async def add_channel(self, channel):
        pass

    async def remove_channel(self, channel):
        pass

    async def sync_channels(self, channels):
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


class FakeMtproto:
    def __init__(self):
        self.connected = True
        self.enabled = True
        self.sent: list[tuple[int, str]] = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def send_video(self, path, chat_id, caption=None, progress=None):
        self.sent.append((chat_id, str(path)))


def make_bot(tmp_path, files=(), mtproto=None):
    data = valid_config(channels=["twitch:channel1"]).model_dump(mode="json", exclude_unset=True)
    (tmp_path / "config.json").write_text(json.dumps(data))
    config = get_config(tmp_path / "config.json")
    rec_dir = disk.resolve_recording_dir(config) / "twitch" / "channel1"
    rec_dir.mkdir(parents=True)
    for name, size in files:
        (rec_dir / name).write_bytes(b"x" * size)
    ctrl = TelegramController(config, FakeRecorder(), FakeMonitor(), FakeEventSub(), kick_webhook=FakeKickWebhook())
    ctrl._mtproto = mtproto if mtproto is not None else FakeMtproto()
    ctrl._app = SimpleNamespace(bot=SimpleNamespace(send_message=_noop_send()))
    return config, ctrl


def _noop_send():
    async def _send(*args, **kwargs):
        pass

    return _send


def test_recordings_empty(tmp_path):
    _, ctrl = make_bot(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("Recordings"))
    assert "No recordings" in text
    assert ctrl._state_for(12345).menu == "recordings"


def test_same_truncated_name_different_size_opens(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)])
    asyncio.run(ctrl.handle_reply_text("Recordings"))
    rec_dir = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1"
    (rec_dir / "a.ts").write_bytes(b"x" * 10)
    long_one = "a_very_long_name_that_truncates_past_forty_chars_1111.ts"
    long_two = "a_very_long_name_that_truncates_past_forty_chars_2222.ts"
    (rec_dir / long_one).write_bytes(b"x" * 10)
    (rec_dir / long_two).write_bytes(b"x" * 20)
    asyncio.run(ctrl.handle_reply_text("Back"))
    text, markup = _open_only_channel(ctrl)
    rows = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    dup = next(label for label in rows if long_one[:37] in label)
    text, _ = asyncio.run(ctrl.handle_reply_text(dup))
    assert ctrl._state_for(12345).menu == "rec_detail"
    assert ctrl._state_for(12345).rec_path is not None


def test_live_pick_ignores_size_drift(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("live.ts", 10)])
    text, markup = _open_only_channel(ctrl)
    row = next(b["text"] for row in markup.to_dict()["keyboard"] for b in row if "live.ts" in b["text"])
    rec_dir = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1"
    with open(rec_dir / "live.ts", "ab") as f:
        f.write(b"y" * 5000)
    text, _ = asyncio.run(ctrl.handle_reply_text(row))
    assert ctrl._state_for(12345).menu == "rec_detail"
    assert "live.ts" in text


def test_detail_back_returns_to_channel_page(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)])
    _, markup = _open_only_channel(ctrl)
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    first = next(label for label in labels if "a.ts" in label)
    asyncio.run(ctrl.handle_reply_text(first))
    assert ctrl._state_for(12345).menu == "rec_detail"
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._state_for(12345).menu == "rec_channel"
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert any("a.ts" in label for label in labels)
    assert "twitch:channel1" in text
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._state_for(12345).menu == "recordings"
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert any("twitch:channel1" in label for label in labels)


def test_detail_hides_send_over_cap(tmp_path):
    from unittest import mock

    _, ctrl = make_bot(tmp_path, files=[("big.ts", 10)])
    _, markup = _open_only_channel(ctrl)
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    first = next(label for label in labels if "big.ts" in label)
    with mock.patch("stream_archive.telegram.menus_recordings.check_sendable", return_value=(False, "too big")):
        text, markup = asyncio.run(ctrl.handle_reply_text(first))
        labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
        assert "\U0001f4e4 Send" not in labels
        assert "\U0001f5d1 Delete" in labels
        assert "Cannot send" in text
        # A typed Send press is refused too, not just hidden.
        text, _ = asyncio.run(ctrl.handle_reply_text("\U0001f4e4 Send"))
        assert "Cannot send" in text


def _open_only_channel(ctrl):
    """Open Recordings, then its single channel. Returns the file keyboard."""
    import asyncio

    if ctrl._state_for(12345).menu != "root":
        asyncio.run(ctrl.handle_reply_text("Back"))
        if ctrl._state_for(12345).menu != "root":
            asyncio.run(ctrl.handle_reply_text("Back"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Recordings"))
    assert "Tap a channel" in text
    rows = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    channel_row = next(label for label in rows if "twitch:channel1" in label)
    text, markup = asyncio.run(ctrl.handle_reply_text(channel_row))
    assert ctrl._state_for(12345).menu == "rec_channel"
    return text, markup


def test_recordings_list_and_pick(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("b.ts", 20), ("a.ts", 10)])
    text, markup = asyncio.run(ctrl.handle_reply_text("Recordings"))
    assert "Recordings (2 files" in text
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert any("twitch:channel1" in label for label in labels)
    assert not any("b.ts" in label for label in labels)
    text, markup = _open_only_channel(ctrl)
    assert "twitch:channel1" in text
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert any("b.ts" in label for label in labels)
    assert any("a.ts" in label for label in labels)
    # Pick the first file row whatever it is; the detail is a reply submenu.
    first = [b["text"] for b in markup.to_dict()["keyboard"][0]][0]
    text, markup = asyncio.run(ctrl.handle_reply_text(first))
    assert ctrl._state_for(12345).menu == "rec_detail"
    assert ctrl._state_for(12345).rec_path is not None
    assert "Sendable over MTProto" in text
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert "\U0001f4e4 Send" in labels
    assert "\U0001f5d1 Delete" in labels
    assert "Back" in labels


def test_recordings_channel_list_and_paging(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[(f"f{i}.ts", 10) for i in range(7)])
    text, markup = asyncio.run(ctrl.handle_reply_text("Recordings"))
    assert "Recordings (7 files" in text
    assert "Tap a channel" in text
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert any("twitch:channel1" in label for label in labels)
    assert "7 files" in next(label for label in labels if "twitch:channel1" in label)
    text, markup = _open_only_channel(ctrl)
    assert "twitch:channel1" in text
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert "Next ▶" in labels
    text2, markup2 = asyncio.run(ctrl.handle_reply_text("Next ▶"))
    assert "twitch:channel1" in text2
    labels2 = [b["text"] for row in markup2.to_dict()["keyboard"] for b in row]
    assert "◀ Prev" in labels2


def test_channel_grouping_two_channels(tmp_path):
    import json

    data = valid_config(channels=["twitch:channel1"]).model_dump(mode="json", exclude_unset=True)
    (tmp_path / "config.json").write_text(json.dumps(data))
    config = get_config(tmp_path / "config.json")
    rec_dir = disk.resolve_recording_dir(config) / "twitch" / "channel1"
    rec_dir.mkdir(parents=True)
    (rec_dir / "a.ts").write_bytes(b"x" * 10)
    other = disk.resolve_recording_dir(config) / "kick" / "example"
    other.mkdir(parents=True)
    (other / "b.ts").write_bytes(b"x" * 10)
    from stream_archive.telegram import TelegramController

    ctrl = TelegramController(config, FakeRecorder(), FakeMonitor(), FakeEventSub(), kick_webhook=FakeKickWebhook())
    ctrl._mtproto = FakeMtproto()
    ctrl._app = SimpleNamespace(bot=SimpleNamespace(send_message=_noop_send()))
    text, markup = asyncio.run(ctrl.handle_reply_text("Recordings"))
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert any("twitch:channel1" in label for label in labels)
    assert any("kick:example" in label for label in labels)
    assert not any("a.ts" in label for label in labels)
    kick_row = next(label for label in labels if "kick:example" in label)
    text, markup = asyncio.run(ctrl.handle_reply_text(kick_row))
    assert ctrl._state_for(12345).menu == "rec_channel"
    assert "kick:example" in text
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert any("b.ts" in label for label in labels)
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._state_for(12345).menu == "recordings"
    labels = [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert any("twitch:channel1" in label for label in labels)


def test_recordings_send_requires_mtproto(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)], mtproto=None)
    ctrl._mtproto = None
    asyncio.run(ctrl.handle_reply_text("Recordings"))
    state = ctrl._state_for(12345)
    state.rec_path = str(disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1" / "a.ts")
    ctrl._enter_menu(12345, "rec_detail")
    text, _ = asyncio.run(ctrl.handle_reply_text("\U0001f4e4 Send"))
    assert "MTProto upload is off" in text


def test_recordings_send_starts_upload(tmp_path):
    mt = FakeMtproto()
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)], mtproto=mt)
    asyncio.run(ctrl.handle_reply_text("Recordings"))
    state = ctrl._state_for(12345)
    target = str(disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1" / "a.ts")
    state.rec_path = target
    ctrl._enter_menu(12345, "rec_detail")
    started = []
    orig = ctrl._start_mtproto_send

    async def _fake(chat_id, path):
        started.append((chat_id, path))

    ctrl._start_mtproto_send = _fake  # type: ignore[method-assign]
    try:
        result = asyncio.run(ctrl.handle_reply_text("\U0001f4e4 Send"))
    finally:
        ctrl._start_mtproto_send = orig
    assert result is None
    assert started == [(12345, target)]


def test_recordings_delete_flow(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)])
    asyncio.run(ctrl.handle_reply_text("Recordings"))
    state = ctrl._state_for(12345)
    target = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1" / "a.ts"
    state.rec_path = str(target)
    ctrl._enter_menu(12345, "rec_detail")
    # Delete asks for confirm through the shared inline confirm buttons.
    text, markup = asyncio.run(ctrl.handle_reply_text("\U0001f5d1 Delete"))
    assert text is not None and "Delete a.ts" in text
    assert "confirm_recdel" in markup.to_dict()["inline_keyboard"][0][0]["callback_data"]
    # Cancel leaves the file alone.
    result = asyncio.run(ctrl.handle_callback("cancel:abcd1234", 12345))
    assert result is not None and "Cancelled" in result[0]
    assert target.exists()
    # Confirm deletes and returns the list keyboard. Re-ask: cancel consumed
    # the first prompt's nonce, so take the fresh callback_data.
    text, markup = asyncio.run(ctrl.handle_reply_text("\U0001f5d1 Delete"))
    cb = markup.to_dict()["inline_keyboard"][0][0]["callback_data"]
    assert len(cb) <= 64, f"callback overflow: {cb!r}"
    text, markup = asyncio.run(ctrl.handle_callback(cb, 12345))
    assert text is not None and "Deleted a.ts" in text
    assert "Back" in [b["text"] for row in markup.to_dict()["keyboard"] for b in row]
    assert not target.exists()
    assert disk._snapshot_cache == {}
    assert ctrl._state_for(12345).menu == "recordings"


def test_recordings_delete_blocked_when_live(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)])
    asyncio.run(ctrl.handle_reply_text("Recordings"))
    state = ctrl._state_for(12345)
    target = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1" / "a.ts"
    state.rec_path = str(target)
    ctrl._recorder._active_paths = lambda: {os.path.realpath(target)}  # type: ignore[method-assign]
    ctrl._enter_menu(12345, "rec_detail")
    text, markup = asyncio.run(ctrl.handle_reply_text("\U0001f5d1 Delete"))
    cb = markup.to_dict()["inline_keyboard"][0][0]["callback_data"]
    result = asyncio.run(ctrl.handle_callback(cb, 12345))
    assert result is not None and "recording now" in result[0]
    assert target.exists()


def test_mtproto_toggle_needs_creds(tmp_path):
    _, ctrl = make_bot(tmp_path)
    text = asyncio.run(ctrl._set_mtproto_enabled(True))
    assert text.startswith("\u274c")
    assert "api_id" in text


def test_mtproto_enable_connects(tmp_path):
    mt = FakeMtproto()
    mt.connected = False
    _, ctrl = make_bot(tmp_path, mtproto=mt)
    ctrl._config.mtproto.api_id = 1
    ctrl._config.mtproto.api_hash = "hash"
    text = asyncio.run(ctrl._set_mtproto_enabled(True))
    assert "MTProto upload enabled" in text
    assert mt.connected is True


def test_settings_shows_mtproto(tmp_path):
    _, ctrl = make_bot(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Settings"))
    text = asyncio.run(ctrl.menu_text("settings"))
    assert "MTProto upload:" in text
    labels = [b["text"] for row in ctrl.reply_keyboard("settings").to_dict()["keyboard"] for b in row]
    assert "MTProto upload" in labels
    asyncio.run(ctrl.handle_reply_text("MTProto upload"))
    text = asyncio.run(ctrl.menu_text("mtproto"))
    assert "MTProto upload:" in text
    labels = [b["text"] for row in ctrl.reply_keyboard("mtproto").to_dict()["keyboard"] for b in row]
    assert "Enable MTProto upload" in labels


def test_status_hides_mtproto_secret(tmp_path):
    config, ctrl = make_bot(tmp_path)
    config.mtproto.api_id = 12345678
    config.mtproto.api_hash = "0123456789abcdef0123456789abcdef"
    config.mtproto.enabled = True
    text = asyncio.run(ctrl.handle_status())
    assert "MTProto upload:" in text
    assert "0123456789abcdef0123456789abcdef" not in text
    assert "12345678" not in text


def test_double_send_is_rejected(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)])
    asyncio.run(ctrl.handle_reply_text("Recordings"))
    state = ctrl._state_for(12345)
    target = str(disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1" / "a.ts")
    state.rec_path = target
    ctrl._enter_menu(12345, "rec_detail")
    ctrl._sending_paths.add(target)
    text, _ = asyncio.run(ctrl.handle_reply_text("\U0001f4e4 Send"))
    assert "already runs" in text


def test_rebind_closes_session(tmp_path):
    config = uploader_config(tmp_path)
    config.mtproto.api_id = 1
    config.mtproto.api_hash = "hash"
    config.mtproto.enabled = True
    factory, made = factory_holder()
    up = MtprotoUploader(config, client_factory=factory)
    asyncio.run(up.connect())
    assert up.connected is True
    config.mtproto.enabled = False
    asyncio.run(up.rebind())
    assert up.connected is False
    assert made[0].disconnected is True


def test_next_clamps_to_page_start(tmp_path):
    from stream_archive.telegram.menus_recordings import last_page_start

    assert last_page_start(12) == 10
    assert last_page_start(10) == 5
    assert last_page_start(5) == 0
    assert last_page_start(0) == 0


def test_reload_notes_missing_mtproto_client(tmp_path):
    import json

    _, ctrl = make_bot(tmp_path)
    ctrl._mtproto = None
    file_config = json.loads((tmp_path / "config.json").read_text())
    file_config["mtproto"] = {"enabled": True, "api_id": 1, "api_hash": "hash", "session": "mtproto.session"}
    (tmp_path / "config.json").write_text(json.dumps(file_config))
    text = asyncio.run(ctrl.handle_reload())
    assert "MTProto" in text
    assert "restart" in text


def test_repick_between_delete_and_confirm_cannot_retarget(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10), ("b.ts", 10)])
    rec_dir = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1"
    state = ctrl._state_for(12345)
    # Delete asked for a.ts; the chat re-picked b.ts before Confirm.
    state.rec_path = str(rec_dir / "b.ts")
    ctrl._enter_menu(12345, "rec_detail")
    state.rec_path = str(rec_dir / "a.ts")
    text, _ = asyncio.run(ctrl.handle_reply_text("\U0001f5d1 Delete"))
    assert "Delete a.ts" in text
    state.rec_path = str(rec_dir / "b.ts")
    text, markup = asyncio.run(ctrl.handle_reply_text("\U0001f5d1 Delete"))
    cb = markup.to_dict()["inline_keyboard"][0][0]["callback_data"]
    state.rec_path = str(rec_dir / "a.ts")  # stale prompt targets a.ts, chat shows b.ts
    result = asyncio.run(ctrl.handle_callback(cb, 12345))
    assert result is not None and "expired" in result[0]
    assert (rec_dir / "a.ts").exists()
    assert (rec_dir / "b.ts").exists()


def test_confirm_markup_survives_callback_edit(tmp_path):
    import types

    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)])
    rec_dir = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1"
    state = ctrl._state_for(12345)
    state.rec_path = str(rec_dir / "a.ts")
    ctrl._enter_menu(12345, "rec_detail")
    edited = []

    async def _edit(text, reply_markup=None):
        edited.append((text, reply_markup))

    async def _answer():
        pass

    text, markup = asyncio.run(ctrl.handle_reply_text("\U0001f5d1 Delete"))
    cb = markup.to_dict()["inline_keyboard"][0][0]["callback_data"]
    assert len(cb) <= 64, f"callback overflow: {cb!r}"
    query = types.SimpleNamespace(data=cb, answer=_answer, edit_message_text=_edit)
    ctx = types.SimpleNamespace(bot=SimpleNamespace(send_message=_noop_send()))
    asyncio.run(ctrl._on_callback(types.SimpleNamespace(callback_query=query), ctx))
    assert edited and "Deleted a.ts" in edited[0][0]
    assert not (rec_dir / "a.ts").exists()


def test_progress_line_shows_bar_speed_eta():
    from stream_archive.telegram.commands_mtproto import _format_eta, _progress_bar, _progress_line

    assert _progress_bar(0.0) == "\U00002b1c" * 10
    assert _progress_bar(1.0) == "\U0001f7e9" * 10
    assert _progress_bar(0.55) == "\U0001f7e9" * 5 + "\U00002b1c" * 5
    assert _format_eta(3661) == "1:01:01"
    assert _format_eta(90) == "1:30"
    line = _progress_line("cap.mp4", 1000, 500, 1000, 0.5, 10.0)
    assert "50%" in line and "Mbps" in line and "ETA" in line
    line = _progress_line("cap.mp4", 1000, 1000, 1000, 1.0, 10.0)
    assert "100%" in line and "ETA" not in line


def test_send_reports_progress_edits(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)])
    state = ctrl._state_for(12345)
    target = str(disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1" / "a.ts")
    state.rec_path = target
    ctrl._enter_menu(12345, "rec_detail")
    notices = []

    class FakeMsg:
        def __init__(self):
            self.edits: list[str] = []
            self.markups: list[object] = []

        async def edit_text(self, line, reply_markup=None):
            self.edits.append(line)
            self.markups.append(reply_markup)

    async def _send_notice(chat_id=None, text=None, **kwargs):
        msg = FakeMsg()
        msg.markups.append(kwargs.get("reply_markup"))
        notices.append(msg)
        return msg

    async def _send_result(chat_id=None, text=None, **kwargs):
        notices.append(text)
        return None

    calls = [_send_notice, _send_result]
    ctrl._app = SimpleNamespace(
        bot=SimpleNamespace(send_message=lambda *a, **k: calls.pop(0)(*a, **k) if calls else None)
    )

    async def _fake_send(path, chat_id, caption=None, progress=None):
        total = 100
        for sent in (5, 10, 60, 100):
            progress(sent, total)

    ctrl._mtproto.send_video = _fake_send  # type: ignore[method-assign]

    async def _drive() -> None:
        await ctrl._start_mtproto_send(12345, target)
        tasks = list(ctrl._mtproto_tasks)
        assert len(tasks) == 1
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

    asyncio.run(_drive())
    assert notices[0].edits, "expected at least one progress edit"
    assert any("Mbps" in edit for edit in notices[0].edits)
    assert any("Sent a.ts" in edit for edit in notices[0].edits), "notice edits into the done state"
    assert notices[0].markups, "progress edits must carry the stop button"
    progress_markups = notices[0].markups[:-1]  # last edit is the done line: button gone
    assert progress_markups and all(m is not None for m in progress_markups), (
        "every progress edit must keep the stop button"
    )
    assert notices[0].markups[-1] is None, "the done edit must drop the stop button"
    stop_data = notices[0].markups[0].to_dict()["inline_keyboard"][0][0]["callback_data"]
    assert stop_data.startswith("mtproto_stop:") and len(stop_data) <= 64


def test_stop_button_cancels_upload(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def _hanging_send(path, chat_id, caption=None, progress=None):
        started.set()
        await release.wait()

    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)])
    target = str(disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1" / "a.ts")

    class FakeMsg:
        def __init__(self):
            self.edits: list[str] = []

        async def edit_text(self, line, reply_markup=None):
            self.edits.append(line)

    notices: list[FakeMsg] = []

    async def _send_notice(chat_id=None, text=None, **kwargs):
        msg = FakeMsg()
        notices.append(msg)
        return msg

    ctrl._app = SimpleNamespace(bot=SimpleNamespace(send_message=_send_notice))
    ctrl._mtproto.send_video = _hanging_send  # type: ignore[method-assign]

    async def _drive() -> None:
        await ctrl._start_mtproto_send(12345, target)
        (task,) = ctrl._mtproto_tasks
        await asyncio.wait_for(started.wait(), timeout=10)
        (key, registered) = next(iter(ctrl._mtproto_sends.items()))
        assert registered is task
        chat_id, nonce = key
        assert chat_id == 12345
        result = await ctrl.handle_callback(f"mtproto_stop:{nonce}", 12345)
        assert result is None, "stop is silent: the task rewrites the notice itself"
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=10)

    asyncio.run(_drive())
    assert any("Stopped a.ts" in edit for edit in notices[0].edits), "notice must show the stop"
    assert not ctrl._mtproto_sends, "stop must unregister the send"
    assert target not in ctrl._sending_paths, "stop must free the path for a retry"


def test_stop_button_after_finish_reports_done(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)])
    target = str(disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1" / "a.ts")

    async def _send_notice(chat_id=None, text=None, **kwargs):
        return SimpleNamespace(edit_text=lambda *a, **k: asyncio.sleep(0))

    ctrl._app = SimpleNamespace(bot=SimpleNamespace(send_message=_send_notice))

    async def _quick_send(path, chat_id, caption=None, progress=None):
        return None

    ctrl._mtproto.send_video = _quick_send  # type: ignore[method-assign]

    async def _drive() -> object:
        await ctrl._start_mtproto_send(12345, target)
        (task,) = ctrl._mtproto_tasks
        await asyncio.wait_for(asyncio.gather(task), timeout=10)
        return await ctrl.handle_callback("mtproto_stop:deadbeef", 12345)

    result = asyncio.run(_drive())
    assert result is not None and "No upload runs" in result[0]


def test_parallel_upload_overlaps_parts(tmp_path):
    from stream_archive.mtproto_upload import PART_SIZE, _upload_handle, _upload_parallel

    target = tmp_path / "cap.bin"
    target.write_bytes(b"x" * (3 * PART_SIZE + 100))
    client = RawFakeClient("s", 1, "h", part_delay=0.05)
    client._call = object()
    seen: list[tuple[int, int]] = []

    async def _main():
        return await _upload_parallel(
            [client], target, target.stat().st_size, lambda s, t: seen.append((s, t)), workers=8
        )

    parts = asyncio.run(_main())
    assert parts["parts"] == 4
    assert client.max_inflight > 1, "parts must overlap"
    assert sorted(i for _, i in client.saved_parts) == [0, 1, 2, 3]
    assert seen and seen[-1][0] == seen[-1][1]

    # Small files stay on the serial path with max-size parts.
    small = tmp_path / "small.bin"
    small.write_bytes(b"x" * 10)
    plain = FakeClient("s", 1, "h")
    asyncio.run(_upload_handle(plain, small, progress=None))
    assert plain.uploaded_parts and plain.uploaded_parts[0][1] == 512


def test_fast_ige_patch_roundtrips():
    import os

    from stream_archive.mtproto_upload import _patch_fast_ige

    _patch_fast_ige()
    _patch_fast_ige()
    from telethon.crypto import libssl

    assert libssl.encrypt_ige is not None
    plain = os.urandom(1024)
    key = os.urandom(32)
    iv = os.urandom(32)
    cipher = libssl.encrypt_ige(plain, key, iv)
    assert cipher != plain
    assert libssl.decrypt_ige(cipher, key, iv) == plain


def test_gzip_bypass_skips_large_parts():
    import os

    from telethon.tl.core.gzippacked import GzipPacked

    from stream_archive.mtproto_upload import _patch_fast_ige

    _patch_fast_ige()
    big = os.urandom(300 * 1024)
    assert GzipPacked.gzip_if_smaller(True, big) is big
    small = b"hello world, hello world, hello world, " * 30
    out = GzipPacked.gzip_if_smaller(True, small)
    assert isinstance(out, bytes) and len(out) > 0


def test_delete_returns_page_keyboard_not_bare_back(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10), ("b.ts", 10)])
    asyncio.run(ctrl.handle_reply_text("Recordings"))
    state = ctrl._state_for(12345)
    rec_dir = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1"
    state.rec_path = str(rec_dir / "a.ts")
    ctrl._enter_menu(12345, "rec_detail")
    text, markup = asyncio.run(ctrl.handle_reply_text("\U0001f5d1 Delete"))
    cb = markup.to_dict()["inline_keyboard"][0][0]["callback_data"]
    assert len(cb) <= 64, f"callback overflow: {cb!r}"
    text, markup = asyncio.run(ctrl.handle_callback(cb, 12345))
    assert "Deleted a.ts" in text
    assert markup is not None
    labels = [b["text"] for row in markup.to_dict().get("keyboard", []) for b in row]
    assert any("twitch:channel1" in label for label in labels), "channel list must survive the delete"
    # Same flow with the channel set (real UI path): file page with b.ts row.
    state.rec_path = str(rec_dir / "b.ts")
    from stream_archive.telegram import menus_recordings as rec

    rec._channel_files(ctrl, "twitch:channel1")
    state.rec_channel = "twitch:channel1"
    ctrl._enter_menu(12345, "rec_detail")
    text, markup = asyncio.run(ctrl.handle_reply_text("\U0001f5d1 Delete"))
    cb = markup.to_dict()["inline_keyboard"][0][0]["callback_data"]
    text, markup = asyncio.run(ctrl.handle_callback(cb, 12345))
    assert "Deleted b.ts" in text
    labels = [b["text"] for row in markup.to_dict().get("keyboard", []) for b in row]
    assert labels == ["Back"], "emptied channel shows bare Back with no files"


def test_shrunk_archive_clamps_offset(tmp_path):
    from stream_archive.telegram.menus_recordings import _clamp_offset, last_page_start

    _, ctrl = make_bot(tmp_path, files=[(f"f{i}.ts", 10) for i in range(11)])
    asyncio.run(ctrl.handle_reply_text("Recordings"))
    state = ctrl._state_for(12345)
    state.rec_offset = 10
    rec_dir = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1"
    for name in ["f0.ts", "f1.ts", "f2.ts", "f3.ts", "f4.ts", "f5.ts"]:
        (rec_dir / name).unlink()
    assert last_page_start(5) == 0
    assert _clamp_offset(ctrl, 12345, 5) == 0
    assert state.rec_offset == 0


def test_stable_pick_survives_size_change(tmp_path):
    _, ctrl = make_bot(tmp_path, files=[("live.ts", 10)])
    text, markup = _open_only_channel(ctrl)
    row = next(b["text"] for row in markup.to_dict()["keyboard"] for b in row if "live.ts" in b["text"])
    rec_dir = disk.resolve_recording_dir(ctrl._config) / "twitch" / "channel1"
    with open(rec_dir / "live.ts", "ab") as f:
        f.write(b"y" * 5000)
    text, markup = asyncio.run(ctrl.handle_reply_text(row))
    assert ctrl._state_for(12345).menu == "rec_detail"
    assert "live.ts" in text


def test_progress_sentinel_needs_no_fake_full_bar(tmp_path):
    # The None wake-up must not edit: drive the watcher sentinel directly.
    _, ctrl = make_bot(tmp_path, files=[("a.ts", 10)])
    assert ctrl._state_for(12345).menu == "root"


def test_send_splits_over_cap_file(tmp_path):
    from stream_archive.mtproto_upload import MAX_UPLOAD_BYTES

    big = tmp_path / "big.mp4"
    big.write_bytes(b"v")

    with open(big, "ab") as f:
        f.truncate(MAX_UPLOAD_BYTES + 1024)
    config = uploader_config(tmp_path)
    config.mtproto.api_id = 1
    config.mtproto.api_hash = "hash"
    config.mtproto.enabled = True
    factory, made = factory_holder()
    up = MtprotoUploader(config, client_factory=factory)
    asyncio.run(up.connect())
    parts = [tmp_path / "big.part01of02.mp4", tmp_path / "big.part02of02.mp4"]
    for part in parts:
        part.write_bytes(b"v" * 100)
    import unittest.mock as mock

    async def _fake_stream(path, on_chunk=None, cancel=None):
        for index, part in enumerate(parts, 1):
            if on_chunk is not None:
                await on_chunk(part, index, len(parts))
        return list(parts)

    seen_notes: list[str | None] = []

    def _noting_progress(sent, total, note=None):
        seen_notes.append(note)

    with (
        mock.patch("stream_archive.recorder.remux.split_parts_streaming", side_effect=_fake_stream),
        mock.patch("stream_archive.recorder.remux.cleanup_split") as cleanup,
    ):
        asyncio.run(up.send_video(big, 99, caption="big", progress=_noting_progress))
        assert made[0].sent, "each chunk must send"
        assert len(made[0].sent) == 2
        assert "part01of02" in made[0].sent[0][1]
        assert "(1/2)" in made[0].sent[0][2]
        assert "(2/2)" in made[0].sent[1][2]
        cleanup.assert_called_once()
        assert "split" in seen_notes, "split phase must report progress"
        assert "part 1/2" in seen_notes, "part 1 must report its own bar"


def test_split_streams_first_chunk_before_cutter_finishes(tmp_path):
    import asyncio as _asyncio

    from stream_archive.recorder import remux

    order: list[str] = []

    def _fake_run_split(src, out, start, length):
        order.append(f"cut:{out.name}")
        Path(out).write_bytes(b"v" * 10)
        return True

    async def _report(chunk, index, total):
        order.append(f"report:{index}/{total}")

    import unittest.mock as mock

    big = tmp_path / "big.mp4"
    big.write_bytes(b"v" * 100)
    with (
        mock.patch.object(remux, "_run_split", side_effect=_fake_run_split),
        mock.patch.object(remux, "_probe_ok", return_value=True),
        mock.patch.object(remux, "_media_duration", return_value=4.0),
        mock.patch("stream_archive.mtproto_upload.MAX_UPLOAD_BYTES", 10),
        mock.patch("stream_archive.mtproto_upload.SPLIT_BYTES", 10),
    ):
        chunks = _asyncio.run(remux.split_parts_streaming(big, chunk_bytes=40, on_chunk=_report))
    assert chunks is not None and len(chunks) == 3
    assert order.index("report:1/3") < order.index("cut:big.part03of03.mp4"), (
        "chunk 1 must report while chunk 3 still cuts"
    )


def test_split_progress_shows_phase_per_part(tmp_path):
    """Each part gets its own labeled bar: (split), (part 1/2), (part 2/2)."""
    from stream_archive.telegram.commands_mtproto import _progress_line

    assert "(split)" in _progress_line("big.mp4", 100, 1, 2, 0.5, 1.0, "split")
    first = _progress_line("big.mp4", 100, 100, 100, 1.0, 1.0, "part 1/2")
    second = _progress_line("big.mp4", 100, 10, 100, 0.1, 2.0, "part 2/2")
    assert "(part 1/2)" in first and "(part 2/2)" in second
    # A restarted part bar re-renders even at a lower fraction: phase change wins.
    assert "10%" in second


def test_help_lists_recordings(tmp_path):
    _, ctrl = make_bot(tmp_path)
    assert "/recordings" in ctrl.handle_help()
    assert "recordings" in {c.command for c in ctrl.command_list()}
