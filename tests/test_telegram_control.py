import asyncio
import html
import json
import re
import threading
import types
import unittest.mock
from datetime import UTC, datetime

from conftest import kb_labels, read_file
from conftest import make_config as valid_config
from telegram import Chat, Message, Update
from telegram import User as TelegramUser
from telegram.ext import MessageHandler

from stream_archive.config import get_config
from stream_archive.telegram import TelegramController
from stream_archive.telegram.dispatcher import _deferred_affected_channels
from stream_archive.telegram.menus_callbacks import AdminCallbackQueryHandler


class FakeRecorder:
    def __init__(self, active=(), recording=()):
        # One source of truth: recording_info(), is_recording(), stop(),
        # restart() and active_channels() must all agree.
        self._recording = set(active) | set(recording)
        self.stop_calls = []
        self.chat_stop_calls = []
        self.restart_calls = []
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

    def is_recording(self, channel):
        return channel in self._recording

    async def stop(self, channel):
        self.stop_calls.append(channel)
        self._recording.discard(channel)

    async def restart(self, channel):
        self.restart_calls.append(channel)
        self._recording.discard(channel)
        return True

    def active_channels(self):
        return sorted(self._recording)

    def recording_settings(self):
        return {
            ch: {
                "output_mode": "disk",
                "preferred_quality": "best",
                "record_chat": True,
                "kick_record_chat": True,
            }
            for ch in sorted(self._recording)
        }

    async def stop_chat(self, channel, platform=None):
        self.chat_stop_calls.append((channel, platform))

    async def disk_snapshot(self):
        return self.snapshot

    def recording_info(self):
        return [{"channel": ch, "mode": "disk", "duration_s": 0, "size_mb": None} for ch in sorted(self._recording)]


class FakeMonitor:
    def __init__(self):
        self.remove_calls = []
        self.sweeps = []

    def remove_channel(self, channel):
        self.remove_calls.append(channel)

    async def check_channels(self, twitch_api, kick_api, config):
        self.sweeps.append(1)


class FakeEventSub:
    def __init__(self):
        self.added = []
        self.removed = []
        self.synced = []

    async def add_channel(self, channel):
        self.added.append(channel)

    async def remove_channel(self, channel):
        self.removed.append(channel)

    async def sync_channels(self, channels):
        self.synced.append(list(channels))

    def status(self):
        return "EventSub: TEST STATUS"


class FakeKickWebhook:
    def __init__(self):
        self.applied = []
        self.added = []
        self.removed = []
        self.synced = []
        self.verified = []

    async def apply_state(self):
        self.applied.append(1)

    async def add_channel(self, channel):
        self.added.append(channel)

    async def remove_channel(self, channel):
        self.removed.append(channel)

    async def sync_channels(self, channels):
        self.synced.append(list(channels))

    async def verify_delivery(self, timeout=180.0):
        # The delivery proof itself belongs to the webhook tests: here the
        # stub only proves the button calls through and reports the result.
        self.verified.append(timeout)
        return True, "first delivery in 3s"


class FakeUpdater:
    def __init__(self, report):
        self.report = report
        self.check_calls = []

    async def check(self, notify):
        self.check_calls.append(notify)
        return self.report


def base_config():
    """Build the on-disk config the controller tests load.

    The shared defaults differ here: the only channel is twitch:channel1, the
    kick chat capture is on, and the webhook listener is off. Only the keys
    the helper sets go in the file: a key left out keeps its model default,
    and the settings text renders those defaults as they are.
    """
    return valid_config(
        channels=["twitch:channel1"],
        kick={"record_chat": True, "webhook": {"enabled": False}},
    ).model_dump(mode="json", exclude_unset=True)


#: Admin chat id of the test config, and the key of its pending prompts.
ADMIN_ID = 12345


def make_controller(tmp_path, channels=None, recording=(), active=(), on_restart=None):
    """Write a valid config file, then load it typed, mirroring get_config()."""
    config = base_config()
    if channels is not None:
        config["channels"] = channels
    (tmp_path / "config.json").write_text(json.dumps(config, indent=4))
    config = get_config(tmp_path / "config.json")
    recorder = FakeRecorder(active=active, recording=recording)
    monitor = FakeMonitor()
    eventsub = FakeEventSub()
    kick_webhook = FakeKickWebhook()
    ctrl = TelegramController(
        config,
        recorder,
        monitor,
        eventsub,
        on_restart=on_restart,
        kick_webhook=kick_webhook,
    )
    return config, ctrl, recorder, monitor, eventsub


def probe_ok(ctrl):
    """Fake the reachability probe so enable flows do not hit the network."""

    async def probe(url):
        return True

    ctrl._probe_webhook_url = probe


def open_settings(ctrl):
    """Open the Settings menu, which owns every global setting."""
    return asyncio.run(ctrl.handle_reply_text("Settings"))


def open_remote_access(ctrl):
    """Open the Remote access menu, which owns the public URL."""
    open_settings(ctrl)
    return asyncio.run(ctrl.handle_reply_text("Remote access"))


def open_webhook_menu(ctrl):
    """Open Remote access, then its Kick webhook submenu."""
    open_remote_access(ctrl)
    return asyncio.run(ctrl.handle_reply_text("Kick webhook"))


def open_storage(ctrl):
    """Open Storage & limits, which owns retention, disk, and the two limits."""
    open_settings(ctrl)
    return asyncio.run(ctrl.handle_reply_text("Storage & limits"))


def menu_of(ctrl):
    """Menu state of the admin chat, the only chat these tests drive."""
    return ctrl._state_for(ADMIN_ID)


def test_status_contains_settings_and_omits_secrets(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, active=["twitch:channel1"])
    text = asyncio.run(ctrl.handle_status())
    assert "twitch:channel1" in text
    assert "Output mode: disk" in text
    assert "Retention: disabled" in text
    assert "Monitoring interval" not in text
    assert "Recording now: twitch:channel1" in text
    assert "Timezone" not in text
    assert "bot_token" not in text
    assert "client_secret" not in text
    assert "user:pass" not in text


def test_status_retention_days_and_singular(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_retention(["7"])
    assert "Retention: 7 days" in asyncio.run(ctrl.handle_status())
    ctrl.handle_retention(["1"])
    assert "Retention: 1 day" in asyncio.run(ctrl.handle_status())


def test_add_persists_and_updates_live(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_add(["twitch:newch"]))
    assert text.startswith("Added")
    assert "twitch:newch" in text
    assert "twitch:newch" in read_file(tmp_path)["channels"]
    assert "twitch:newch" in config.channels


def test_add_duplicate_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_add(["twitch:newch"]))
    before = read_file(tmp_path)
    text = asyncio.run(ctrl.handle_add(["twitch:newch"]))
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_add_invalid_name_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = asyncio.run(ctrl.handle_add(["bad name!"]))
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_add_usage_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    assert asyncio.run(ctrl.handle_add([])) == "Usage: /add <channel>"
    assert asyncio.run(ctrl.handle_add(["a", "b"])) == "Usage: /add <channel>"
    assert read_file(tmp_path) == before


def test_remove_stops_live_recording(tmp_path):
    config, ctrl, recorder, monitor, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "twitch:ch"], recording=["twitch:ch"]
    )
    text = asyncio.run(ctrl.handle_remove(["twitch:ch"]))
    assert text.startswith("Removed")
    assert "twitch:ch" in text
    assert recorder.stop_calls == ["twitch:ch"]
    assert "Recording stopped." in text
    assert monitor.remove_calls == ["twitch:ch"]
    assert "twitch:ch" not in read_file(tmp_path)["channels"]
    assert "twitch:ch" not in config.channels


def test_remove_recording_channel_sends_no_apply_warning(tmp_path):
    # Regression: removing a live channel whose per-channel override differs
    # from the global default used to stash a bogus "recording in progress"
    # apply warning. The remove path stops the recording itself, so there is
    # nothing to apply or keep.
    config, ctrl, recorder, monitor, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "twitch:ch"], recording=["twitch:ch"]
    )
    # A real settings change first: it legitimately warns while the channel
    # records. The admin declines, so the prompt stays pending.
    ctrl.handle_mode(["twitch:ch", "youtube"])
    assert len(ctrl._pending_apply) == 1
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)

    text = asyncio.run(ctrl.handle_remove(["twitch:ch"]))
    assert text.startswith("Removed twitch:ch")
    assert "Recording stopped." in text
    assert recorder.stop_calls == ["twitch:ch"]
    assert recorder.restart_calls == []

    # The removal must not stash a second warning for the removed channel,
    # and the send pass drops the leftover prompt: its recording has ended.
    asyncio.run(ctrl._maybe_send_apply_warnings())
    assert bot.send_message.await_count == 0
    assert ctrl._pending_apply == {}


def test_remove_not_recording_does_not_stop(tmp_path):
    config, ctrl, recorder, monitor, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    text = asyncio.run(ctrl.handle_remove(["twitch:ch"]))
    assert text.startswith("Removed")
    assert recorder.stop_calls == []
    # The monitor live state is dropped for every removed channel, also when
    # no recording runs.
    assert monitor.remove_calls == ["twitch:ch"]
    assert "twitch:ch" not in read_file(tmp_path)["channels"]


def test_remove_unknown_channel_rejected(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1"])
    before = read_file(tmp_path)
    text = asyncio.run(ctrl.handle_remove(["nope"]))
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before
    assert recorder.stop_calls == []


def test_retention_set(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_retention(["7"])
    assert text == "Retention set to 7 day(s)"
    assert read_file(tmp_path)["retention_days"] == 7
    assert config.retention_days == 7


def test_retention_invalid_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    for arg in ["-1", "x"]:
        before = read_file(tmp_path)
        text = ctrl.handle_retention([arg])
        assert text.startswith("\u274c")
        assert read_file(tmp_path) == before


def test_mode_set(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_mode(["youtube"])
    assert text == "Output mode set to youtube"
    assert read_file(tmp_path)["output_mode"] == "youtube"
    assert config.output_mode == "youtube"


def test_mode_invalid_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["cloud"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_mode_per_channel_sets_override(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_mode(["twitch:channel1", "youtube"])
    assert text == "Output mode for twitch:channel1 set to youtube"
    assert read_file(tmp_path)["channel_output_modes"] == {"twitch:channel1": "youtube"}
    assert config.channel_output_modes == {"twitch:channel1": "youtube"}


def test_mode_per_channel_reset(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_mode(["twitch:channel1", "youtube"])
    text = ctrl.handle_mode(["twitch:channel1", "default"])
    assert text == "Output mode for twitch:channel1 reset to global (disk)"
    assert read_file(tmp_path)["channel_output_modes"] == {}
    assert config.output_mode == "disk"


def test_mode_per_channel_invalid_mode_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["twitch:channel1", "cloud"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_mode_per_channel_invalid_name_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["bad name!", "disk"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_mode_usage_too_many_args(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["a", "b", "c"])
    assert text == "Usage: /mode <disk|youtube|both> or /mode <channel> <disk|youtube|both|default>"
    assert read_file(tmp_path) == before


def test_remove_clears_override(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    ctrl.handle_mode(["twitch:channel1", "youtube"])
    asyncio.run(ctrl.handle_remove(["twitch:channel1"]))
    assert read_file(tmp_path)["channel_output_modes"] == {}
    assert "twitch:channel1" not in read_file(tmp_path)["channels"]


def test_status_shows_per_channel_modes(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_mode(["twitch:channel1", "youtube"])
    assert "Per-channel output: twitch:channel1 \u2192 youtube" in asyncio.run(ctrl.handle_status())


def _load_config(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(base_config(), indent=4))
    return get_config(tmp_path / "config.json")


def _recordings(channels, mode="disk", quality="best", chat=True, kick_chat=True):
    return {
        ch: {
            "output_mode": mode,
            "preferred_quality": quality,
            "record_chat": chat,
            "kick_record_chat": kick_chat,
        }
        for ch in channels
    }


def test_deferred_affected_global_mode_change(tmp_path):
    cfg = _load_config(tmp_path)
    cfg.output_mode = "youtube"
    assert _deferred_affected_channels(cfg, _recordings(["twitch:channel1"])) == ["twitch:channel1"]


def test_deferred_affected_override_immune_to_global_change(tmp_path):
    cfg = _load_config(tmp_path)
    cfg.output_mode = "youtube"
    cfg.channel_output_modes = {"twitch:channel1": "youtube"}
    rec = _recordings(["twitch:channel1"], mode="youtube")  # started with the override
    assert _deferred_affected_channels(cfg, rec) == []


def test_deferred_affected_per_channel_override_change(tmp_path):
    cfg = _load_config(tmp_path)
    cfg.channel_output_modes = {"twitch:channel1": "youtube"}
    rec = _recordings(["twitch:channel1", "twitch:ch"])
    assert _deferred_affected_channels(cfg, rec) == ["twitch:channel1"]


def test_deferred_affected_repeat_change_after_decline(tmp_path):
    # Config already holds the change because the first prompt was declined.
    # The recording still runs the old mode, so the same change must warn again.
    cfg = _load_config(tmp_path)
    cfg.channel_output_modes = {"twitch:channel1": "disk"}
    rec = _recordings(["twitch:channel1"], mode="youtube")
    assert _deferred_affected_channels(cfg, rec) == ["twitch:channel1"]


def test_deferred_affected_same_value_noop(tmp_path):
    cfg = _load_config(tmp_path)
    rec = _recordings(["twitch:channel1"])  # disk recording, disk config
    assert _deferred_affected_channels(cfg, rec) == []


def test_deferred_affected_quality_change(tmp_path):
    cfg = _load_config(tmp_path)
    cfg.channels = ["twitch:ch", "twitch:channel1"]
    cfg.preferred_quality = "720p"
    rec = _recordings(["twitch:ch", "twitch:channel1"])
    assert _deferred_affected_channels(cfg, rec) == ["twitch:ch", "twitch:channel1"]


def test_deferred_affected_per_channel_quality_override(tmp_path):
    cfg = _load_config(tmp_path)
    cfg.channels = ["twitch:ch", "twitch:channel1"]
    cfg.channel_preferred_qualities = {"twitch:ch": "1080p"}
    rec = _recordings(["twitch:ch", "twitch:channel1"])  # both snapshots report quality best
    assert _deferred_affected_channels(cfg, rec) == ["twitch:ch"]


def test_deferred_affected_chat_twitch_enable(tmp_path):
    cfg = _load_config(tmp_path)
    cfg.record_chat = True
    rec = _recordings(["twitch:channel1", "kick:xqc"], chat=False, kick_chat=True)
    assert _deferred_affected_channels(cfg, rec) == ["twitch:channel1"]


def test_deferred_affected_chat_kick_enable(tmp_path):
    cfg = _load_config(tmp_path)
    cfg.channels = ["twitch:channel1", "kick:xqc"]
    cfg.kick.record_chat = True
    rec = _recordings(["twitch:channel1", "kick:xqc"], chat=True, kick_chat=False)
    assert _deferred_affected_channels(cfg, rec) == ["kick:xqc"]


def test_deferred_affected_chat_disable_noop(tmp_path):
    cfg = _load_config(tmp_path)
    cfg.record_chat = False
    rec = _recordings(["twitch:channel1"], chat=True)  # disable stops capture immediately
    assert _deferred_affected_channels(cfg, rec) == []


def test_deferred_affected_no_active_recordings(tmp_path):
    cfg = _load_config(tmp_path)
    cfg.output_mode = "youtube"
    assert _deferred_affected_channels(cfg, {}) == []


def test_deferred_affected_ignores_removed_channel(tmp_path):
    # Removing a channel stops its recording right away. Its snapshot may
    # differ from the global fallback settings, but that never warrants a
    # deferred-apply warning.
    cfg = _load_config(tmp_path)
    cfg.output_mode = "youtube"
    cfg.channels = ["twitch:channel1"]  # twitch:gone was just removed
    rec = _recordings(["twitch:channel1", "twitch:gone"])
    assert _deferred_affected_channels(cfg, rec) == ["twitch:channel1"]


def test_mode_change_sets_pending_apply(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1"])
    text = ctrl.handle_mode(["youtube"])
    assert text == "Output mode set to youtube"
    assert len(ctrl._pending_apply) == 1
    summary, channels = next(iter(ctrl._pending_apply.values()))
    assert summary == "Output mode set to youtube"
    assert channels == ["twitch:channel1"]


def test_apply_warning_sent_with_inline_keyboard(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1"])
    ctrl._pending_apply[(ADMIN_ID, "abcd")] = ("Output mode set to youtube", ["twitch:channel1"])
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)
    asyncio.run(ctrl._maybe_send_apply_warnings())
    assert bot.send_message.await_count == 1
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert "twitch:channel1" in kwargs["text"]
    buttons = kwargs["reply_markup"].to_dict()["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "apply_now:abcd"
    assert buttons[1]["callback_data"] == "cancel:abcd"
    # The entry stays pending so the button's nonce still resolves on tap.
    assert ctrl._pending_apply == {(ADMIN_ID, "abcd"): ("Output mode set to youtube", ["twitch:channel1"])}
    assert ctrl._apply_warnings_sent == {(ADMIN_ID, "abcd")}
    # A second trigger does not resend the same warning.
    asyncio.run(ctrl._maybe_send_apply_warnings())
    assert bot.send_message.await_count == 1


def test_apply_warning_round_trip_restarts(tmp_path):
    """A change sends a warning, and an Apply now tap restarts the recording."""
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1"])
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)
    ctrl.handle_mode(["youtube"])
    asyncio.run(ctrl._maybe_send_apply_warnings())
    assert bot.send_message.await_count == 1
    data = bot.send_message.await_args.kwargs["reply_markup"].to_dict()["inline_keyboard"][0][0]["callback_data"]
    nonce = data.split(":")[1]
    assert (12345, nonce) in ctrl._pending_apply  # the tapped nonce must still resolve
    result = asyncio.run(ctrl.handle_callback(data))
    assert result is not None
    text, _ = result
    assert text.startswith("\u2705 Applied: Output mode set to youtube")
    assert "twitch:channel1: restarted with the new settings" in text
    assert recorder.restart_calls == ["twitch:channel1"]
    assert ctrl._pending_apply == {}


def test_apply_warning_dropped_when_recording_ends(tmp_path):
    # A pending prompt whose recording ended before the admin answered is
    # dropped: there is no running recording left to apply settings to.
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1"])
    ctrl._pending_apply[(ADMIN_ID, "abcd")] = ("Output mode set to youtube", ["twitch:channel1"])
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)
    asyncio.run(recorder.stop("twitch:channel1"))
    asyncio.run(ctrl._maybe_send_apply_warnings())
    assert bot.send_message.await_count == 0
    assert ctrl._pending_apply == {}
    assert ctrl._apply_warnings_sent == set()


def test_apply_now_callback_restarts(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1"])
    ctrl._pending_apply[(ADMIN_ID, "abcd")] = ("Output mode set to youtube", ["twitch:channel1"])
    result = asyncio.run(ctrl.handle_callback("apply_now:abcd"))
    assert result is not None
    text, _ = result
    assert text.startswith("\u2705 Applied: Output mode set to youtube")
    assert "twitch:channel1: restarted with the new settings" in text
    assert recorder.restart_calls == ["twitch:channel1"]
    assert ctrl._pending_apply == {}
    assert ctrl._apply_warnings_sent == set()
    # A double tap on the same message is a silent no-op.
    assert asyncio.run(ctrl.handle_callback("apply_now:abcd")) is None
    assert recorder.restart_calls == ["twitch:channel1"]
    # A stale or unknown nonce is a silent no-op.
    assert asyncio.run(ctrl.handle_callback("apply_now:zzzz")) is None
    assert recorder.restart_calls == ["twitch:channel1"]
    # Cancel keeps the current recording and restarts nothing. The caller drops
    # the inline keyboard, so the entry and its warning marker must go too.
    ctrl._pending_apply[(ADMIN_ID, "wxyz")] = ("Output mode set to youtube", ["twitch:channel1"])
    ctrl._apply_warnings_sent.add((ADMIN_ID, "wxyz"))
    result = asyncio.run(ctrl.handle_callback("cancel:wxyz"))
    assert result == ("Cancelled - nothing changed", None)
    assert recorder.restart_calls == ["twitch:channel1"]
    assert ctrl._pending_apply == {}
    assert ctrl._apply_warnings_sent == set()


def test_reload_picks_up_disk_edits(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    file_config = read_file(tmp_path)
    file_config["channels"].append("twitch:hand_edit")
    file_config["retention_days"] = 3
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))
    text = asyncio.run(ctrl.handle_reload())
    assert text == "\u2705 Config reloaded from config.json"
    assert "twitch:hand_edit" in config.channels
    assert config.retention_days == 3


def test_reload_corrupt_file_leaves_live_unchanged(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    (tmp_path / "config.json").write_text("{ not json")
    text = asyncio.run(ctrl.handle_reload())
    assert text.startswith("\u274c")
    assert config.channels == ["twitch:channel1"]


def test_restart_schedules_callback(tmp_path):
    flag = threading.Event()
    _, ctrl, _, _, eventsub = make_controller(tmp_path, on_restart=flag.set)

    async def scenario():
        text = ctrl.handle_restart()
        assert "\U0001f504 Restarting..." in text
        # handle_restart schedules the restart about 0.5s out, so wait on the
        # flag itself instead of sleeping for a fixed margin.
        assert await asyncio.to_thread(flag.wait, 5.0)

    asyncio.run(scenario())


def test_restart_without_callback(tmp_path):
    _, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_restart()
    assert text == "Restart is not available (no shutdown callback configured)"


def test_update_app_available_shows_pull_command(tmp_path):
    report = {
        "app": {
            "status": "update",
            "current": "1.0.0",
            "latest": "1.1.0",
            "changelog": ["Add retention cleanup", "Fix proxy retry loop"],
        },
    }
    fake = FakeUpdater(report)
    _, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "\U0001f4e6 Updates available" in text
        assert "• stream-archive: v1.0.0 → v1.1.0" in text
        assert "  Changelog:" in text
        assert "  • Add retention cleanup" in text
        assert "  • Fix proxy retry loop" in text
        assert "Apply by running:\ndocker compose pull && docker compose up -d" in text

    asyncio.run(scenario())
    assert fake.check_calls == [False]


def test_update_up_to_date_lists_current_versions(tmp_path):
    report = {"app": {"status": "up_to_date", "current": "1.0.0", "latest": "1.0.0"}}
    fake = FakeUpdater(report)
    _, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "\u2705 Up to date" in text
        assert "• stream-archive: v1.0.0" in text
        assert "streamlink" not in text

    asyncio.run(scenario())
    assert fake.check_calls == [False]


def test_update_all_unknown_fails(tmp_path):
    report = {"app": {"status": "unknown", "current": None, "latest": None}}
    fake = FakeUpdater(report)
    _, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "\u274c Update check failed - try again later." in text

    asyncio.run(scenario())


def test_update_not_configured(tmp_path):
    _, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert asyncio.run(ctrl.handle_update()) == "Update checks are not configured"


def test_status_contains_update_check_line(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert "Update check: enabled (every 24h)" in asyncio.run(ctrl.handle_status())


def test_status_contains_quality_and_disk_lines(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_status())
    assert "Quality: best" in text
    assert "Simultaneous recordings: unlimited" in text
    assert "YouTube re-streams: unlimited" in text
    assert "Disk limits: disabled" in text
    assert "EventSub" not in text
    assert "GB free of" in text
    assert "archive:" in text


def test_quality_show_set_invalid(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert "Quality: best" in ctrl.handle_quality([])
    text = ctrl.handle_quality(["720p"])
    assert text == "Quality set to 720p"
    assert read_file(tmp_path)["preferred_quality"] == "720p"
    assert config.preferred_quality == "720p"
    assert "\u274c Invalid channel name" in ctrl.handle_quality(["bad name!", "720p"])
    assert (
        ctrl.handle_quality(["twitch:ch", "720p", "extra"])
        == "Usage: /quality <best|1080p|720p|...> or /quality <channel> <quality|default>"
    )


def test_quality_empty_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_quality([""])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_quality_per_channel_persists_and_resets(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_quality(["twitch:channel1", "720p"])
    assert text == "Quality for twitch:channel1 set to 720p"
    assert read_file(tmp_path)["channel_preferred_qualities"] == {"twitch:channel1": "720p"}
    text = ctrl.handle_quality(["twitch:channel1", "default"])
    assert text == "Quality for twitch:channel1 reset to global (best)"
    assert read_file(tmp_path)["channel_preferred_qualities"] == {}


def test_quality_audio_only_conflict_confirm_applies_both_overrides(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.output_mode = "youtube"
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)
    before = read_file(tmp_path)
    text = ctrl.handle_quality(["twitch:channel1", "audio_only"])
    assert "output mode to disk" in text
    # Nothing is saved while the choice is pending.
    assert read_file(tmp_path) == before
    asyncio.run(ctrl._maybe_send_apply_warnings())
    assert bot.send_message.await_count == 1
    kwargs = bot.send_message.await_args.kwargs
    assert "audio-only" in kwargs["text"].lower()
    buttons = kwargs["reply_markup"].to_dict()["inline_keyboard"][0]
    nonce = buttons[0]["callback_data"].split(":")[1]
    assert buttons[0]["callback_data"] == f"audio_confirm:{nonce}"
    assert buttons[1]["callback_data"] == f"cancel:{nonce}"
    result = asyncio.run(ctrl.handle_callback(f"audio_confirm:{nonce}"))
    assert result is not None
    saved = read_file(tmp_path)
    assert saved["channel_preferred_qualities"]["twitch:channel1"] == "audio_only"
    assert saved["channel_output_modes"]["twitch:channel1"] == "disk"
    # Double-tap guard: a second identical press does nothing.
    assert asyncio.run(ctrl.handle_callback(f"audio_confirm:{nonce}")) is None


def test_quality_audio_only_conflict_cancel_changes_nothing(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    bot = unittest.mock.AsyncMock()
    config.output_mode = "youtube"
    ctrl._app = types.SimpleNamespace(bot=bot)
    before = read_file(tmp_path)
    text = ctrl.handle_quality(["twitch:channel1", "audio_only"])
    assert "output mode to disk" in text
    nonce = next(iter(ctrl._pending_audio_switch))
    result = asyncio.run(ctrl.handle_callback(f"cancel:{nonce}"))
    assert result is not None
    assert read_file(tmp_path) == before
    # The bot never confirms a prompt that the admin cancelled.
    assert asyncio.run(ctrl.handle_callback(f"audio_confirm:{nonce}")) is None
    assert read_file(tmp_path) == before


def test_maxrecordings_show_set_invalid(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert "Max recordings: 0 (0 = unlimited)" in ctrl.handle_maxrecordings([])
    text = ctrl.handle_maxrecordings(["3"])
    assert text == "Max recordings set to 3"
    assert read_file(tmp_path)["max_concurrent_recordings"] == 3
    assert config.max_concurrent_recordings == 3
    before = read_file(tmp_path)
    text = ctrl.handle_maxrecordings(["x"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before
    text = ctrl.handle_maxrecordings(["-1"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_maxrecordings_usage(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert ctrl.handle_maxrecordings(["1", "2"]) == "Usage: /maxrecordings <n> (0 = unlimited)"


def test_maxyoutube_set(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert "Max YouTube re-streams: 0 (0 = unlimited)" in ctrl.handle_maxyoutube([])
    text = ctrl.handle_maxyoutube(["2"])
    assert text == "Max YouTube re-streams set to 2"
    assert read_file(tmp_path)["max_concurrent_youtube_streams"] == 2
    assert config.max_concurrent_youtube_streams == 2
    before = read_file(tmp_path)
    assert ctrl.handle_maxyoutube(["x"]).startswith("\u274c")
    assert read_file(tmp_path) == before
    assert ctrl.handle_maxyoutube(["1", "2"]) == "Usage: /maxyoutube <n> (0 = unlimited)"


def test_disk_subcommands(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_disk(["maxsize", "20"])
    assert text == "Disk max total set to 20 GB"
    assert read_file(tmp_path)["disk"]["max_total_gb"] == 20
    assert config.disk.max_total_gb == 20

    text = ctrl.handle_disk(["delete_oldest", "off"])
    assert text == "Delete oldest disabled"
    assert read_file(tmp_path)["disk"]["delete_oldest"] is False
    text = ctrl.handle_disk(["delete_oldest", "on"])
    assert text == "Delete oldest enabled"
    assert read_file(tmp_path)["disk"]["delete_oldest"] is True

    assert ctrl.handle_disk(["bogus", "1"]) == "Usage: /disk <maxsize|delete_oldest> <value>"
    before = read_file(tmp_path)
    assert ctrl.handle_disk(["interval", "30"]) == "Usage: /disk <maxsize|delete_oldest> <value>"
    assert read_file(tmp_path)["disk"].get("check_interval_s", 60) == 60  # untouched
    assert read_file(tmp_path) == before
    before = read_file(tmp_path)
    assert ctrl.handle_disk(["maxsize", "x"]).startswith("\u274c")
    assert read_file(tmp_path) == before


def test_disk_show_block(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_disk([])
    assert "Disk limits:" in text
    assert "max total: 0 GB (0 = disabled, delete oldest: on)" in text
    assert "check every" not in text


def test_help_lists_new_commands(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_help()
    assert "/quality [channel] <value|default>" in text
    assert "/maxrecordings <n>" in text
    assert "/maxyoutube <n>" in text
    assert "/disk <maxsize|delete_oldest> <value>" in text
    assert "/chat [on|off]" in text
    assert "/recordings" in text
    assert "/settings" in text
    assert "/start" in text


def test_start_resends_settings_keyboard(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    sent = []

    class FakeBot:
        async def set_my_commands(self, *args, **kwargs):
            pass

        async def send_message(self, chat_id, text, reply_markup=None):
            sent.append((chat_id, text, reply_markup))

    class FakeUpdater:
        async def start_polling(self, **kwargs):
            pass

    class FakeApp:
        bot = FakeBot()
        updater = FakeUpdater()

        def add_handlers(self, handlers):
            pass

        async def initialize(self):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

        async def shutdown(self):
            pass

    ctrl._app = FakeApp()
    asyncio.run(ctrl.start())
    assert len(sent) == 1
    chat_id, text, markup = sent[0]
    assert chat_id == config.telegram_user_id
    assert "Channels" in text  # root menu text = status block
    assert kb_labels(markup) == ROOT_LABELS


def test_chat_show_state(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert "Chat recording: enabled" in asyncio.run(ctrl.handle_chat([]))
    asyncio.run(ctrl.handle_chat(["off"]))
    assert "Chat recording: disabled" in asyncio.run(ctrl.handle_chat([]))
    assert "Chat recording: disabled" in asyncio.run(ctrl.handle_status())


def test_chat_on_persists(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1"])
    asyncio.run(ctrl.handle_chat(["off"]))
    text = asyncio.run(ctrl.handle_chat(["on"]))
    assert text == "Chat recording enabled"
    assert read_file(tmp_path)["record_chat"] is True
    assert config.record_chat is True
    assert recorder.chat_stop_calls == [("twitch:channel1", None)]  # from the earlier /chat off, not re-triggered by on


def test_chat_off_persists_and_stops_inflight(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1", "twitch:ch"])
    text = asyncio.run(ctrl.handle_chat(["off"]))
    assert text == "Chat recording disabled"
    assert read_file(tmp_path)["record_chat"] is False
    assert config.record_chat is False
    assert recorder.chat_stop_calls == [("twitch:ch", None), ("twitch:channel1", None)]
    assert recorder.stop_calls == []


def test_chat_invalid_rejected(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1"])
    before = read_file(tmp_path)
    assert "Chat recording: enabled" in asyncio.run(ctrl.handle_chat([]))
    assert asyncio.run(ctrl.handle_chat(["maybe"])) == "Usage: /chat <on|off> [twitch|kick]"
    assert read_file(tmp_path) == before
    assert recorder.chat_stop_calls == []


def test_add_calls_eventsub_add_channel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_add(["twitch:newch"]))
    assert text.startswith("Added")
    assert eventsub.added == ["twitch:newch"]


def test_add_rejected_does_not_call_eventsub(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_add(["twitch:newch"]))
    asyncio.run(ctrl.handle_add(["twitch:newch"]))
    assert eventsub.added == ["twitch:newch"]


def test_remove_calls_eventsub_remove_channel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    asyncio.run(ctrl.handle_remove(["twitch:ch"]))
    assert eventsub.removed == ["twitch:ch"]


def test_remove_rejected_does_not_call_eventsub(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1"])
    asyncio.run(ctrl.handle_remove(["nope"]))
    assert eventsub.removed == []


def test_reload_calls_eventsub_sync(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    file_config = read_file(tmp_path)
    file_config["channels"].append("twitch:hand_edit")
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))
    text = asyncio.run(ctrl.handle_reload())
    assert text == "\u2705 Config reloaded from config.json"
    assert eventsub.synced == [["twitch:channel1", "twitch:hand_edit"]]


def test_status_limits_in_plain_words_when_set(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_maxrecordings(["3"])
    ctrl.handle_maxyoutube(["2"])
    text = asyncio.run(ctrl.handle_status())
    assert "Simultaneous recordings: 3" in text
    assert "YouTube re-streams: 2" in text


def test_status_disk_limits_in_plain_words(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_disk(["maxsize", "100"])
    text = asyncio.run(ctrl.handle_status())
    assert "max 100 GB (delete oldest when over)" in text
    ctrl.handle_disk(["delete_oldest", "off"])
    text = asyncio.run(ctrl.handle_status())
    assert "max 100 GB (stop recording when over)" in text


def api_sent(bot):
    """The text, parse mode and keyboard of the last message the bot sent."""
    assert bot.send_message.await_count >= 1
    kwargs = bot.send_message.await_args.kwargs
    return kwargs["text"], kwargs.get("parse_mode"), kwargs["reply_markup"]


def visible_text(text):
    """The text Telegram shows, with the HTML entities decoded."""
    return html.unescape(text)


def assert_valid_html(text):
    """Only the code tags and HTML entities may hold '<' or '&'."""
    for match in re.finditer(r"<(?!/?code>)|&(?!(?:amp|lt|gt|quot|#x27);)", text):
        msg = f"unescaped {match.group()!r} at index {match.start()}: {text!r}"
        raise AssertionError(msg)


ROOT_LABELS = ["Channels", "Recordings", "Settings"]
SETTINGS_LABELS = [
    "Output mode",
    "Quality",
    "Chat recording",
    "Storage & limits",
    "Remote access",
    "MTProto upload",
    "Back",
]
STORAGE_LABELS = ["Retention", "Disk limits", "Max recordings", "Max restreams", "Back"]
KICK_TOKEN_LABELS = ["Back"]


def toggle_action(enabled):
    """The one toggle button shows the action that the current state allows."""
    return "Disable" if enabled else "Enable"


def chat_labels(twitch, kick):
    return [f"{toggle_action(twitch)} Twitch chat", f"{toggle_action(kick)} Kick chat", "Back"]


def disk_labels(delete_oldest):
    return ["Max total size", f"{toggle_action(delete_oldest)} delete oldest", "Back"]


def api_labels(enabled):
    return [f"{toggle_action(enabled)} API", "Show key", "Rotate key", "Back"]


def remote_labels(enabled):
    return [
        f"{toggle_action(enabled)} endpoint",
        "Kick webhook",
        "API",
        "Web panel",
        "Back",
    ]


def web_labels(enabled):
    return [f"{toggle_action(enabled)} Web panel", "New password", "Back"]


def mtproto_labels(enabled):
    return [f"{toggle_action(enabled)} MTProto upload", "Back"]


def webhook_labels(enabled):
    return [f"{toggle_action(enabled)} Kick webhook", "Set Kick URL", "Test delivery", "Back"]


def test_command_list_covers_all_handlers(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    commands = {c.command for c in ctrl.command_list()}
    assert commands >= {
        "start",
        "help",
        "status",
        "channels",
        "add",
        "remove",
        "retention",
        "mode",
        "reload",
        "restart",
        "update",
        "quality",
        "maxrecordings",
        "maxyoutube",
        "disk",
        "chat",
        "recordings",
        "settings",
    }


def test_reply_keyboard_root_layout(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    d = ctrl.reply_keyboard("root").to_dict()
    assert d["keyboard"] == [
        [{"text": "Channels"}, {"text": "Recordings"}],
        [{"text": "Settings"}],
    ]
    assert d["resize_keyboard"] is True


def test_reply_keyboard_channels_layout(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    assert ctrl.reply_keyboard("channels").to_dict()["keyboard"] == [
        [{"text": "Back"}],
        [{"text": "Add channel"}],
        [{"text": "\u2022 twitch:channel1"}],
        [{"text": "\u2022 twitch:ch"}],
    ]


def test_reply_keyboard_channel_layout(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert ctrl.reply_keyboard("channel", "twitch:channel1").to_dict()["keyboard"] == [
        [{"text": "Mode"}, {"text": "Quality"}],
        [{"text": "Hold delay"}, {"text": "Remove channel"}],
        [{"text": "Back"}],
    ]
    assert ctrl.reply_keyboard("channel_mode", "twitch:channel1").to_dict()["keyboard"] == [
        [{"text": "Disk"}, {"text": "YouTube"}, {"text": "Both"}],
        [{"text": "\u2713 Global"}],
        [{"text": "Back"}],
    ]
    config.channel_output_modes["twitch:channel1"] = "youtube"
    assert ctrl.reply_keyboard("channel_mode", "twitch:channel1").to_dict()["keyboard"][0] == [
        {"text": "Disk"},
        {"text": "\u2713 YouTube"},
        {"text": "Both"},
    ]


def test_reply_text_navigates_to_channels(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("Channels"))
    assert "Channels (1): twitch:channel1" in text
    assert kb_labels(markup) == ["Back", "Add channel", "\u2022 twitch:channel1"]
    assert menu_of(ctrl).menu == "channels"


def test_root_menu_text_shows_status(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.menu_text("root"))
    assert "Output mode: disk" in text
    assert kb_labels(ctrl.reply_keyboard("root")) == ROOT_LABELS
    assert menu_of(ctrl).menu == "root"


def test_reply_text_add_channel_flow(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Add channel"))
    assert "Send the channel name" in text
    assert kb_labels(markup) == ["Back"]
    text, markup = asyncio.run(ctrl.handle_reply_text("twitch:newch"))
    assert text.startswith("Added twitch:newch")
    assert eventsub.added == ["twitch:newch"]
    assert "twitch:newch" in config.channels
    assert menu_of(ctrl).menu == "channels"


def test_reply_text_add_channel_invalid_stays(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    asyncio.run(ctrl.handle_reply_text("Add channel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Bad Name!"))
    assert text.startswith("\u274c")
    assert menu_of(ctrl).menu == "add_channel"
    assert read_file(tmp_path) == before


def test_add_checks_live_status_at_once(tmp_path):
    from stream_archive import events as events_mod

    config, ctrl, recorder, monitor, eventsub = make_controller(tmp_path)
    events_mod.reset()
    ctrl.bind_live_check(object(), object())

    async def live_sweep(twitch_api, kick_api, config):
        monitor.sweeps.append(1)
        recorder._recording.add("twitch:newch")

    monitor.check_channels = live_sweep  # type: ignore[method-assign]
    text = asyncio.run(ctrl.handle_add(["twitch:newch"]))
    assert text.startswith("Added twitch:newch")
    assert "is live - recording started." in text
    assert monitor.sweeps == [1]
    kinds = [e["kind"] for e in events_mod.list_events()]
    assert "config" in kinds  # the add itself lands in the event feed
    events_mod.reset()


def test_reply_text_channel_submenu_mode(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    text, markup = asyncio.run(ctrl.handle_reply_text("\u2022 twitch:channel1"))
    assert "Channel: twitch:channel1" in text
    assert "global (disk)" in text
    text, markup = asyncio.run(ctrl.handle_reply_text("Mode"))
    assert "Output mode for twitch:channel1: global (disk)" in text
    assert kb_labels(markup) == ["Disk", "YouTube", "Both", "\u2713 Global", "Back"]
    text, markup = asyncio.run(ctrl.handle_reply_text("YouTube"))
    assert read_file(tmp_path)["channel_output_modes"] == {"twitch:channel1": "youtube"}
    assert config.channel_output_modes == {"twitch:channel1": "youtube"}
    assert menu_of(ctrl).menu == "channel"
    assert menu_of(ctrl).channel == "twitch:channel1"


def test_channel_mode_submenu_global_resets(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.channel_output_modes = {"twitch:channel1": "youtube"}
    menu_of(ctrl).menu, menu_of(ctrl).channel = "channel_mode", "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Global"))
    assert "reset to global" in text
    assert read_file(tmp_path)["channel_output_modes"] == {}
    assert menu_of(ctrl).menu == "channel"
    assert menu_of(ctrl).channel == "twitch:channel1"


def test_back_from_channel_mode_to_channel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    menu_of(ctrl).menu, menu_of(ctrl).channel = "channel_mode", "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "channel"
    assert menu_of(ctrl).channel == "twitch:channel1"
    assert "Output mode" in text


def test_reply_text_channel_delete_asks_confirm(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    asyncio.run(ctrl.handle_reply_text("\u2022 twitch:channel1"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Remove channel"))
    assert "Remove twitch:channel1 from monitoring?" in text
    assert kb_labels(markup) == ["Confirm", "Cancel"]
    assert read_file(tmp_path) == before
    assert menu_of(ctrl).menu == "channel"


def test_reply_text_chat_menu_shows_both_toggles(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("Settings"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Chat recording"))
    assert "Chat recording (Twitch): on" in text
    assert "Kick chat recording: on" in text
    assert kb_labels(markup) == chat_labels(True, True)
    assert menu_of(ctrl).menu == "chat"


def test_reply_text_chat_disable_twitch_only(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "kick:xqc"], recording=["twitch:channel1", "kick:xqc"]
    )
    asyncio.run(ctrl.handle_reply_text("Settings"))
    asyncio.run(ctrl.handle_reply_text("Chat recording"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable Twitch chat"))
    assert text == "Twitch chat recording disabled"
    assert read_file(tmp_path)["record_chat"] is False
    assert read_file(tmp_path)["kick"]["record_chat"] is True
    assert recorder.chat_stop_calls == [("twitch:channel1", "twitch")]
    assert kb_labels(markup) == chat_labels(False, True)
    assert menu_of(ctrl).menu == "chat"


def test_reply_text_chat_enable_kick_only(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_chat(["off"]))  # both off via command
    asyncio.run(ctrl.handle_reply_text("Settings"))
    asyncio.run(ctrl.handle_reply_text("Chat recording"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable Kick chat"))
    assert text == "Kick chat recording enabled"
    assert read_file(tmp_path)["kick"]["record_chat"] is True
    assert read_file(tmp_path)["record_chat"] is False
    assert kb_labels(markup) == chat_labels(False, True)
    assert menu_of(ctrl).menu == "chat"


def test_reply_text_chat_back_navigation(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Settings"))
    asyncio.run(ctrl.handle_reply_text("Chat recording"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "settings"
    assert kb_labels(markup) == SETTINGS_LABELS
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "root"
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_mode_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Settings"))
    asyncio.run(ctrl.handle_reply_text("Output mode"))
    text, markup = asyncio.run(ctrl.handle_reply_text("YouTube"))
    assert read_file(tmp_path)["output_mode"] == "youtube"
    assert config.output_mode == "youtube"
    assert kb_labels(markup) == SETTINGS_LABELS


def test_reply_text_quality_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Settings"))
    asyncio.run(ctrl.handle_reply_text("Quality"))
    text, markup = asyncio.run(ctrl.handle_reply_text("1080p"))
    assert read_file(tmp_path)["preferred_quality"] == "1080p"
    assert config.preferred_quality == "1080p"
    assert kb_labels(markup) == SETTINGS_LABELS


def test_reply_text_retention_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Retention"))
    text, markup = asyncio.run(ctrl.handle_reply_text("14 days"))
    assert read_file(tmp_path)["retention_days"] == 14
    assert kb_labels(markup) == STORAGE_LABELS


def test_reply_text_retention_off_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Retention"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Off"))
    assert read_file(tmp_path)["retention_days"] == 0
    assert kb_labels(markup) == STORAGE_LABELS


def test_reply_text_retention_custom(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Retention"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Custom"))
    assert "Send the new value in days" in text
    assert kb_labels(markup) == ["Back"]
    text, markup = asyncio.run(ctrl.handle_reply_text("11"))
    assert read_file(tmp_path)["retention_days"] == 11
    assert kb_labels(markup) == STORAGE_LABELS


def test_menu_marks_the_current_value(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.retention_days = 7
    assert kb_labels(ctrl.reply_keyboard("retention")) == [
        "Off",
        "1 day",
        "3 days",
        "\u2713 7 days",
        "14 days",
        "30 days",
        "Custom",
        "Back",
    ]
    assert "\u2713 Disk" in kb_labels(ctrl.reply_keyboard("mode"))
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Retention"))
    text, markup = asyncio.run(ctrl.handle_reply_text("\u2713 14 days"))
    assert read_file(tmp_path)["retention_days"] == 14
    assert kb_labels(markup) == STORAGE_LABELS


def test_menu_quality_audio_only_maps_to_value(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Settings"))
    asyncio.run(ctrl.handle_reply_text("Quality"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Audio only"))
    assert read_file(tmp_path)["preferred_quality"] == "audio_only"
    assert config.preferred_quality == "audio_only"


def test_menu_limits_unlimited_maps_to_zero(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Max restreams"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Unlimited"))
    assert read_file(tmp_path)["max_concurrent_youtube_streams"] == 0
    assert kb_labels(markup) == STORAGE_LABELS


def test_reply_text_custom_invalid_keeps_state(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Retention"))
    asyncio.run(ctrl.handle_reply_text("Custom"))
    text, markup = asyncio.run(ctrl.handle_reply_text("x"))
    assert text.startswith("\u274c")
    assert menu_of(ctrl).menu == "custom"
    assert read_file(tmp_path) == before


def test_reply_text_maxrec_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Max recordings"))
    text, markup = asyncio.run(ctrl.handle_reply_text("3"))
    assert read_file(tmp_path)["max_concurrent_recordings"] == 3
    assert config.max_concurrent_recordings == 3
    assert kb_labels(markup) == STORAGE_LABELS


def test_reply_text_storage_menu(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text, markup = open_storage(ctrl)
    assert "Retention: off (0 = disabled)" in text
    assert "Delete oldest: on" in text
    assert "Max YouTube re-streams: 0 (0 = unlimited)" in text
    assert kb_labels(markup) == STORAGE_LABELS
    assert menu_of(ctrl).menu == "storage"


def test_reply_text_disk_quick_returns_disk_menu(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Disk limits"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Max total size"))
    assert "Max total size: 0 GB (0 = disabled)" in text
    text, markup = asyncio.run(ctrl.handle_reply_text("50"))
    assert read_file(tmp_path)["disk"]["max_total_gb"] == 50
    assert config.disk.max_total_gb == 50
    assert menu_of(ctrl).menu == "disk"
    assert kb_labels(markup) == disk_labels(True)


def test_reply_text_disk_submenu_descriptions(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Disk limits"))
    cases = [
        ("Max total size", "Limits the total archive size"),
    ]
    for button, desc in cases:
        text, markup = asyncio.run(ctrl.handle_reply_text(button))
        assert desc in text
        asyncio.run(ctrl.handle_reply_text("Back"))  # return to the disk menu


def test_reply_text_disk_delete_oldest_on_confirms(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_disk(["delete_oldest", "off"])
    before = read_file(tmp_path)
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Disk limits"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable delete oldest"))
    assert "oldest recordings will be deleted" in text
    assert kb_labels(markup) == ["Confirm", "Cancel"]
    assert read_file(tmp_path) == before


def test_reply_text_disk_delete_oldest_off_direct(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_storage(ctrl)
    asyncio.run(ctrl.handle_reply_text("Disk limits"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable delete oldest"))
    assert read_file(tmp_path)["disk"]["delete_oldest"] is False
    assert config.disk.delete_oldest is False
    assert kb_labels(markup) == disk_labels(False)


def test_reply_text_back_navigation(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    asyncio.run(ctrl.handle_reply_text("\u2022 twitch:channel1"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert kb_labels(markup) == ["Back", "Add channel", "\u2022 twitch:channel1"]
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "root"
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_unknown_ignored(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    assert asyncio.run(ctrl.handle_reply_text("hello")) is None
    assert read_file(tmp_path) == before


def test_callback_confirm_remove_applies(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    text, markup = asyncio.run(ctrl.handle_callback("confirm_remove:twitch:channel1:deadbeef"))
    assert text.startswith("Removed twitch:channel1")
    assert "twitch:channel1" not in config.channels
    assert "twitch:channel1" not in read_file(tmp_path)["channels"]
    assert eventsub.removed == ["twitch:channel1"]
    assert menu_of(ctrl).menu == "channels"


def test_callback_confirm_remove_kick_channel(tmp_path):
    config, ctrl, recorder, monitor, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "kick:xqc"], recording=["kick:xqc"]
    )
    text, markup = asyncio.run(ctrl.handle_callback("confirm_remove:kick:xqc:deadbeef"))
    assert text.startswith("Removed kick:xqc")
    assert "kick:xqc" not in config.channels
    assert "kick:xqc" not in read_file(tmp_path)["channels"]
    assert recorder.stop_calls == ["kick:xqc"]
    assert monitor.remove_calls == ["kick:xqc"]
    assert eventsub.removed == []
    assert ctrl._kick_webhook.removed == ["kick:xqc"]
    assert menu_of(ctrl).menu == "channels"


def test_confirm_keyboard_roundtrip_kick_channel_applies(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "kick:xqc"])
    kb = ctrl._confirm_keyboard("confirm_remove", "kick:xqc").to_dict()
    data = kb["inline_keyboard"][0][0]["callback_data"]
    assert data.startswith("confirm_remove:kick:xqc:")
    text, markup = asyncio.run(ctrl.handle_callback(data))
    assert text.startswith("Removed kick:xqc")
    assert "kick:xqc" not in config.channels
    assert ctrl._kick_webhook.removed == ["kick:xqc"]


def test_callback_confirm_remove_dedup(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    asyncio.run(ctrl.handle_callback("confirm_remove:twitch:channel1:deadbeef"))
    assert asyncio.run(ctrl.handle_callback("confirm_remove:twitch:channel1:deadbeef")) is None
    assert eventsub.removed == ["twitch:channel1"]


def test_callback_confirm_remove_stale_channel_feedback(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1"])
    before = read_file(tmp_path)
    text, markup = asyncio.run(ctrl.handle_callback("confirm_remove:ghost:deadbeef"))
    assert text == "ghost is no longer monitored"
    assert read_file(tmp_path) == before
    assert eventsub.removed == []


def test_callback_cancel_works_per_confirm_message(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text, markup = asyncio.run(ctrl.handle_callback("cancel:aaaa1111"))
    assert text == "Cancelled - nothing changed"
    # A second confirm message has a different nonce, so its cancel still works.
    text, markup = asyncio.run(ctrl.handle_callback("cancel:bbbb2222"))
    assert text == "Cancelled - nothing changed"
    assert read_file(tmp_path) == before


def test_confirm_keyboard_roundtrip_applies(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    kb = ctrl._confirm_keyboard("confirm_remove", "twitch:channel1").to_dict()
    data = kb["inline_keyboard"][0][0]["callback_data"]
    text, markup = asyncio.run(ctrl.handle_callback(data))
    assert text.startswith("Removed twitch:channel1")
    assert "twitch:channel1" not in config.channels
    assert eventsub.removed == ["twitch:channel1"]


def test_confirm_keyboard_data_unique_per_message(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    kb1 = ctrl._confirm_keyboard("confirm_remove", "twitch:channel1").to_dict()
    kb2 = ctrl._confirm_keyboard("confirm_remove", "twitch:channel1").to_dict()
    data1 = [b["callback_data"] for row in kb1["inline_keyboard"] for b in row]
    data2 = [b["callback_data"] for row in kb2["inline_keyboard"] for b in row]
    assert data1[0].startswith("confirm_remove:twitch:channel1:")
    assert data1[1].startswith("cancel:")
    assert data1 != data2  # nonce differs per message


def test_callback_old_format_ignored(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    assert asyncio.run(ctrl.handle_callback("confirm_remove:channel1")) is None
    assert asyncio.run(ctrl.handle_callback("cancel")) is None
    assert read_file(tmp_path) == before


def test_callback_confirm_delete_oldest_applies(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_disk(["delete_oldest", "off"])
    text, markup = asyncio.run(ctrl.handle_callback("confirm_delete_oldest:on:deadbeef"))
    assert read_file(tmp_path)["disk"]["delete_oldest"] is True
    assert config.disk.delete_oldest is True
    assert menu_of(ctrl).menu == "disk"


def test_callback_cancel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text, markup = asyncio.run(ctrl.handle_callback("cancel:deadbeef"))
    assert text == "Cancelled - nothing changed"
    assert markup is None
    assert read_file(tmp_path) == before


def test_callback_unknown_ignored(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    assert asyncio.run(ctrl.handle_callback("bogus:data")) is None
    assert read_file(tmp_path) == before


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class _FakeQuery:
    def __init__(self):
        self.answers = []
        self.edits = []
        self.data = None

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(text)


class _FakeUpdate:
    def __init__(self, user_id):
        self.effective_user = type("U", (), {"id": user_id})()
        self.callback_query = _FakeQuery()


class _FakeContext:
    def __init__(self):
        self.bot = _FakeBot()


def test_callback_double_tap_silent_no_toast(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    update = _FakeUpdate(12345)
    ctx = _FakeContext()
    update.callback_query.data = "confirm_remove:twitch:channel1:deadbeef"
    asyncio.run(ctrl._on_callback(update, ctx))
    asyncio.run(ctrl._on_callback(update, ctx))
    assert update.callback_query.answers == [None, None]  # no "Already processed" toast
    assert len(update.callback_query.edits) == 1  # second tap does not re-edit
    assert "Removed twitch:channel1" in update.callback_query.edits[0]
    assert eventsub.removed == ["twitch:channel1"]
    assert len(ctx.bot.sent) == 1  # menu re-rendered once, after the first tap


def test_callback_error_surfaces_instead_of_silent_failure(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1"])

    async def boom(data):
        msg = "boom"
        raise RuntimeError(msg)

    ctrl.handle_callback = boom
    update = _FakeUpdate(12345)
    ctx = _FakeContext()
    update.callback_query.data = "confirm_remove:twitch:channel1:deadbeef"
    asyncio.run(ctrl._on_callback(update, ctx))
    assert update.callback_query.answers == [None]
    assert update.callback_query.edits == ["\u274c Unexpected error - see logs"]
    assert ctx.bot.sent == []  # failed tap does not re-render the menu


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b"", hang=False):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.hang = hang
        self.killed = False

    def __await__(self):  # create_subprocess_exec is awaited before the process is used
        async def _resolve():
            return self

        return _resolve().__await__()

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _status_json(dns_name="box.tail1234.ts.net."):
    return json.dumps({"Self": {"DNSName": dns_name}}).encode()


def scripted_exec(monkeypatch, **procs):
    """Patch create_subprocess_exec with one process per scripted call.

    Each keyword names the call it answers: a tailscale call matches its
    subcommand (``status``, ``funnel``, ``serve``), and a cloudflared call
    matches the binary name. The helper returns every (argv, kwargs) pair the
    controller passed, in order.
    """
    seen: list[tuple[tuple[str, ...], dict]] = []

    def fake_exec(*args, **kwargs):
        seen.append((args, kwargs))
        key = args[1] if args[0] == "tailscale" else args[0]
        proc = procs.get(key)
        if proc is None:
            msg = f"no process scripted for {args!r}"
            raise AssertionError(msg)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


class _LineStream:
    def __init__(self, lines, hang=False):
        self._lines = list(lines)
        self.hang = hang

    async def readline(self):
        if self.hang:
            await asyncio.sleep(3600)
        return self._lines.pop(0) if self._lines else b""


class _CloudflaredFakeProc:
    def __init__(self, lines, returncode=None, hang=False):
        # A live child reports returncode None, exactly like a real process.
        self.stdout = _LineStream(lines, hang=hang)
        self.returncode = returncode
        self.killed = False

    def __await__(self):  # create_subprocess_exec is awaited before use
        async def _resolve():
            return self

        return _resolve().__await__()

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def test_callback_unknown_data_silent(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    update = _FakeUpdate(12345)
    ctx = _FakeContext()
    update.callback_query.data = "bogus:data"
    asyncio.run(ctrl._on_callback(update, ctx))
    assert update.callback_query.answers == [None]
    assert update.callback_query.edits == []
    assert ctx.bot.sent == []


def test_add_kick_channel_stores_and_skips_eventsub(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_add(["kick:xqc"]))
    assert text.startswith("Added kick:xqc")
    assert "kick:xqc" in read_file(tmp_path)["channels"]
    assert "kick:xqc" in config.channels
    assert eventsub.added == []
    assert ctrl._kick_webhook.added == ["kick:xqc"]


def test_add_kick_channel_normalizes_case(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_add(["kick:XQC"]))
    assert text.startswith("Added kick:xqc")
    assert config.channels == ["twitch:channel1", "kick:xqc"]


def test_add_kick_url_stores_canonical(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_add(["https://kick.com/xqc"]))
    assert text.startswith("Added kick:xqc")
    assert config.channels == ["twitch:channel1", "kick:xqc"]
    assert read_file(tmp_path)["channels"] == ["twitch:channel1", "kick:xqc"]
    assert eventsub.added == []
    assert ctrl._kick_webhook.added == ["kick:xqc"]


def test_add_twitch_url_stores_bare(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_add(["https://www.twitch.tv/newch/"]))
    assert text.startswith("Added twitch:newch")
    assert config.channels == ["twitch:channel1", "twitch:newch"]
    assert eventsub.added == ["twitch:newch"]
    assert ctrl._kick_webhook.added == []


def test_add_invalid_url_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = asyncio.run(ctrl.handle_add(["https://other.com/x"]))
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before
    assert eventsub.added == []
    assert ctrl._kick_webhook.added == []


def test_remove_kick_url(tmp_path):
    config, ctrl, recorder, monitor, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "kick:xqc"], recording=["kick:xqc"]
    )
    text = asyncio.run(ctrl.handle_remove(["https://kick.com/xqc"]))
    assert text.startswith("Removed kick:xqc")
    assert "kick:xqc" not in read_file(tmp_path)["channels"]
    assert recorder.stop_calls == ["kick:xqc"]
    assert eventsub.removed == []
    assert ctrl._kick_webhook.removed == ["kick:xqc"]


def test_remove_twitch_url(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    text = asyncio.run(ctrl.handle_remove(["https://twitch.tv/ch"]))
    assert text.startswith("Removed twitch:ch")
    assert "twitch:ch" not in read_file(tmp_path)["channels"]


def test_remove_invalid_url_rejected(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1"])
    before = read_file(tmp_path)
    text = asyncio.run(ctrl.handle_remove(["https://other.com/x"]))
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before
    assert recorder.stop_calls == []


def test_mode_kick_url_override(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "kick:xqc"])
    text = ctrl.handle_mode(["https://kick.com/xqc", "youtube"])
    assert text == "Output mode for kick:xqc set to youtube"
    assert read_file(tmp_path)["channel_output_modes"] == {"kick:xqc": "youtube"}


def test_add_invalid_kick_channel_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = asyncio.run(ctrl.handle_add(["kick:"]))
    assert text.startswith("\u274c")
    assert "use twitch:<name> for Twitch or kick:<name>" in text
    assert read_file(tmp_path) == before
    assert eventsub.added == []
    assert ctrl._kick_webhook.added == []


def test_remove_kick_channel_calls_webhook_not_eventsub(tmp_path):
    config, ctrl, recorder, monitor, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "kick:xqc"], recording=["kick:xqc"]
    )
    text = asyncio.run(ctrl.handle_remove(["kick:xqc"]))
    assert text.startswith("Removed kick:xqc")
    assert "kick:xqc" not in read_file(tmp_path)["channels"]
    assert recorder.stop_calls == ["kick:xqc"]
    assert monitor.remove_calls == ["kick:xqc"]
    assert eventsub.removed == []
    assert ctrl._kick_webhook.removed == ["kick:xqc"]


def test_mode_per_channel_kick_override(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "kick:xqc"])
    text = ctrl.handle_mode(["kick:xqc", "youtube"])
    assert text == "Output mode for kick:xqc set to youtube"
    assert read_file(tmp_path)["channel_output_modes"] == {"kick:xqc": "youtube"}
    assert config.channel_output_modes == {"kick:xqc": "youtube"}


def test_mode_per_channel_kick_invalid_name_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["kick:", "disk"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_chat_off_toggles_both_platform_flags(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1"])
    text = asyncio.run(ctrl.handle_chat(["off"]))
    assert text == "Chat recording disabled"
    assert read_file(tmp_path)["record_chat"] is False
    assert read_file(tmp_path)["kick"]["record_chat"] is False
    assert config.record_chat is False
    assert config.kick.record_chat is False
    assert recorder.chat_stop_calls == [("twitch:channel1", None)]

    text = asyncio.run(ctrl.handle_chat(["on"]))
    assert text == "Chat recording enabled"
    assert read_file(tmp_path)["kick"]["record_chat"] is True


def test_chat_off_twitch_only(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "kick:xqc"], recording=["twitch:channel1", "kick:xqc"]
    )
    text = asyncio.run(ctrl.handle_chat(["off", "twitch"]))
    assert text == "Twitch chat recording disabled"
    assert read_file(tmp_path)["record_chat"] is False
    assert read_file(tmp_path)["kick"]["record_chat"] is True
    assert config.record_chat is False
    assert config.kick.record_chat is True
    # Only the twitch channel's chat stops. The kick buffer keeps collecting.
    assert recorder.chat_stop_calls == [("twitch:channel1", "twitch")]


def test_chat_off_kick_only(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "kick:xqc"], recording=["twitch:channel1", "kick:xqc"]
    )
    text = asyncio.run(ctrl.handle_chat(["off", "kick"]))
    assert text == "Kick chat recording disabled"
    assert read_file(tmp_path)["record_chat"] is True
    assert read_file(tmp_path)["kick"]["record_chat"] is False
    assert config.record_chat is True
    assert config.kick.record_chat is False
    # Only the kick channel's chat is finalized. The twitch IRC recorder keeps running.
    assert recorder.chat_stop_calls == [("kick:xqc", "kick")]


def test_chat_on_per_platform(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_chat(["off"]))
    text = asyncio.run(ctrl.handle_chat(["on", "twitch"]))
    assert text == "Twitch chat recording enabled"
    assert read_file(tmp_path)["record_chat"] is True
    assert read_file(tmp_path)["kick"]["record_chat"] is False

    text = asyncio.run(ctrl.handle_chat(["on", "kick"]))
    assert text == "Kick chat recording enabled"
    assert read_file(tmp_path)["kick"]["record_chat"] is True


def test_chat_show_state_per_platform(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_chat([]))
    assert "Chat recording: enabled" in text
    assert "Kick chat recording: enabled" in text
    asyncio.run(ctrl.handle_chat(["off", "kick"]))
    text = asyncio.run(ctrl.handle_chat([]))
    assert "Kick chat recording: disabled" in text


def test_chat_invalid_platform_rejected(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1"])
    before = read_file(tmp_path)
    assert asyncio.run(ctrl.handle_chat(["off", "youtube"])) == "Usage: /chat <on|off> [twitch|kick]"
    assert asyncio.run(ctrl.handle_chat(["maybe", "twitch"])) == "Usage: /chat <on|off> [twitch|kick]"
    assert read_file(tmp_path) == before
    assert recorder.chat_stop_calls == []


def test_status_contains_kick_lines(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_status())
    assert "Kick chat recording: enabled" in text
    assert "Kick webhook: off" in text


def test_reload_calls_kick_webhook_sync(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    file_config = read_file(tmp_path)
    file_config["channels"].append("kick:xqc")
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))
    text = asyncio.run(ctrl.handle_reload())
    assert text == "\u2705 Config reloaded from config.json"
    assert ctrl._kick_webhook.synced == [["twitch:channel1", "kick:xqc"]]


def test_help_mentions_kick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_help()
    assert "kick:" in text


def test_reply_text_kick_webhook_menu_flow(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text, markup = open_webhook_menu(ctrl)
    assert "Kick webhook: off" in text
    assert "Set Kick URL saves your own public entry" in text
    assert kb_labels(markup) == webhook_labels(False)  # only the toggle and Back
    assert menu_of(ctrl).menu == "kick_webhook"


def test_reply_text_remote_access_menu(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text, markup = open_remote_access(ctrl)
    assert "Endpoint: off" in text
    assert "Kick webhook: off" in text
    assert "Control API: off" in text
    assert "Paste a URL here to enable it with that address" in text
    assert kb_labels(markup) == remote_labels(False)
    assert menu_of(ctrl).menu == "remote_access"


def test_reply_text_remote_access_back_navigation(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_webhook_menu(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "remote_access"
    assert kb_labels(markup) == remote_labels(False)
    text, markup = asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    assert menu_of(ctrl).menu == "kick_webhook"
    text, markup = asyncio.run(ctrl.handle_reply_text("Set Kick URL"))
    assert menu_of(ctrl).menu == "kick_webhook_url"
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "kick_webhook"
    assert kb_labels(markup) == webhook_labels(False)
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "remote_access"
    assert kb_labels(markup) == remote_labels(False)
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "settings"
    assert kb_labels(markup) == SETTINGS_LABELS
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "root"
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_remote_access_toggle_restores_the_saved_url(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    config.endpoint.public_url = "https://my-tunnel.example.com/kick/webhook"
    text, markup = open_remote_access(ctrl)
    assert kb_labels(markup) == remote_labels(False)
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable endpoint"))
    assert "Endpoint enabled" in text
    assert "https://my-tunnel.example.com/kick/webhook" in text
    assert ctrl._kick_webhook.applied == [1]
    assert menu_of(ctrl).menu == "remote_access"
    assert kb_labels(markup) == remote_labels(True)


def test_reply_text_remote_access_toggle_off_keeps_the_setup(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.endpoint.public_url = "https://my-tunnel.example.com/kick/webhook"
    config.endpoint.enabled = True
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable endpoint"))
    assert "Endpoint disabled" in text
    assert "Your setup is saved" in text
    assert menu_of(ctrl).menu == "remote_access"
    assert kb_labels(markup) == remote_labels(False)
    assert read_file(tmp_path)["endpoint"]["public_url"] == "https://my-tunnel.example.com/kick/webhook"
    # The stored URL keeps its path until the endpoint is re-applied


def test_reply_text_api_enable_shows_generated_key(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("API"))
    assert "Control API: off" in text
    assert kb_labels(markup) == api_labels(False)
    assert menu_of(ctrl).menu == "api"
    assert config.api.key == ""  # no key before the first enable
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)  # the handler sends the reply itself
    assert asyncio.run(ctrl.handle_reply_text("Enable API")) is None
    sent, parse_mode, markup = api_sent(bot)
    assert_valid_html(sent)
    assert "Control API enabled" in visible_text(sent)
    assert "No public URL yet" in visible_text(sent)
    key = read_file(tmp_path)["api"]["key"]
    assert key
    assert f"<code>{key}</code>" in sent  # the key is a code span, so one tap copies it
    assert parse_mode == "HTML"
    assert kb_labels(markup) == api_labels(True)
    assert read_file(tmp_path)["api"]["enabled"] is True
    assert config.api.enabled is True
    assert ctrl._kick_webhook.applied == [1]


def test_reply_text_api_shows_base_url_and_key(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.endpoint.public_url = "https://kick.example.com/kick/webhook"
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("API"))
    asyncio.run(ctrl.handle_reply_text("Enable API"))
    sent, _, _ = api_sent(bot)
    assert "https://kick.example.com/api/v1/" in visible_text(sent)
    asyncio.run(ctrl.handle_reply_text("Back"))  # remote_access
    text, _ = asyncio.run(ctrl.handle_reply_text("API"))
    assert "Control API: on" in text
    assert "https://kick.example.com/api/v1/" in text
    assert asyncio.run(ctrl.handle_reply_text("Show key")) is None
    sent, parse_mode, markup = api_sent(bot)
    assert_valid_html(sent)
    key = read_file(tmp_path)["api"]["key"]
    assert f"<code>{key}</code>" in sent  # one tap on the key copies it
    assert parse_mode == "HTML"
    assert kb_labels(markup) == api_labels(True)


def test_reply_text_api_hides_the_key_in_a_group(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    group_id = -1001234567890  # a group chat that holds the bot
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)
    asyncio.run(ctrl.handle_reply_text("Settings", chat_id=group_id))
    asyncio.run(ctrl.handle_reply_text("Remote access", chat_id=group_id))
    asyncio.run(ctrl.handle_reply_text("API", chat_id=group_id))
    asyncio.run(ctrl.handle_reply_text("Enable API", chat_id=group_id))
    enabled, _, _ = api_sent(bot)
    key = read_file(tmp_path)["api"]["key"]
    assert key  # the first enable generated the key
    assert key not in enabled  # a group chat never sees the secret
    assert "<code>" not in enabled  # and no code span either
    asyncio.run(ctrl.handle_reply_text("Show key", chat_id=group_id))
    sent, parse_mode, markup = api_sent(bot)
    assert key not in sent  # a group chat never sees the secret
    assert "<code>" not in sent
    assert "private chat" in visible_text(sent)
    assert parse_mode == "HTML"
    assert kb_labels(markup) == api_labels(True)


def test_reply_text_api_disable_keeps_key(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("API"))
    asyncio.run(ctrl.handle_reply_text("Enable API"))
    key = read_file(tmp_path)["api"]["key"]
    assert asyncio.run(ctrl.handle_reply_text("Disable API")) is None
    sent, parse_mode, markup = api_sent(bot)
    assert_valid_html(sent)
    assert "Control API disabled" in visible_text(sent)
    assert f"<code>{key}</code>" not in sent  # a disable never shows the key
    assert parse_mode == "HTML"
    assert read_file(tmp_path)["api"]["enabled"] is False
    assert read_file(tmp_path)["api"]["key"] == key
    assert ctrl._kick_webhook.applied == [1, 1]
    assert kb_labels(markup) == api_labels(False)


def test_reply_text_api_rotate_key_replaces_it(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    bot = unittest.mock.AsyncMock()
    ctrl._app = types.SimpleNamespace(bot=bot)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("API"))
    assert asyncio.run(ctrl.handle_reply_text("Rotate key")) is None
    sent, parse_mode, _ = api_sent(bot)
    assert_valid_html(sent)
    assert "API key generated" in visible_text(sent)  # no key existed yet
    first_key = read_file(tmp_path)["api"]["key"]
    assert f"<code>{first_key}</code>" in sent
    assert parse_mode == "HTML"
    asyncio.run(ctrl.handle_reply_text("Enable API"))
    assert asyncio.run(ctrl.handle_reply_text("Rotate key")) is None
    sent, _, markup = api_sent(bot)
    new_key = read_file(tmp_path)["api"]["key"]
    assert new_key != first_key
    assert f"<code>{new_key}</code>" in sent  # the code span carries the new key
    assert f"<code>{first_key}</code>" not in sent
    assert "old key stopped working" in visible_text(sent)
    assert ctrl._kick_webhook.applied == [1]  # rotation never touches the listener
    assert kb_labels(markup) == api_labels(True)


def test_reply_text_kick_webhook_url_applies(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("https://tunnel.trycloudflare.com/kick/webhook"))
    assert "Endpoint enabled" in text
    assert "https://tunnel.trycloudflare.com/kick/webhook" in text
    assert "Settings \u2192 Developer \u2192 your app \u2192 Enable webhooks" in text
    assert "developer dashboard" not in text
    assert "URL is reachable" in text  # automatic probe, no button
    w = read_file(tmp_path)["endpoint"]
    assert w["enabled"] is True
    assert w["public_url"] == "https://tunnel.trycloudflare.com"
    assert ctrl._kick_webhook.applied == [1]
    assert ctrl._kick_webhook.synced == [["twitch:channel1"]]
    assert menu_of(ctrl).menu == "remote_access"


def test_reply_text_kick_webhook_enable_rearms_setup_notification(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    config.kick.webhook.setup_notified = True  # already confirmed once before
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("https://tunnel.trycloudflare.com/kick/webhook"))
    assert read_file(tmp_path)["kick"]["webhook"]["setup_notified"] is False  # re-armed


def test_reply_text_kick_webhook_url_normalizes_root_path(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("https://tunnel.trycloudflare.com"))
    assert "Endpoint: https://tunnel.trycloudflare.com/" in text
    assert "https://tunnel.trycloudflare.com/kick/webhook" in text
    assert read_file(tmp_path)["endpoint"]["public_url"] == "https://tunnel.trycloudflare.com"
    assert menu_of(ctrl).menu == "remote_access"


def test_reply_text_kick_webhook_off_keeps_the_setup(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("https://abc123.trycloudflare.com"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable endpoint"))
    assert "Endpoint disabled" in text
    assert "Your setup is saved" in text
    w = read_file(tmp_path)["endpoint"]
    assert w["enabled"] is False
    assert w["public_url"] == "https://abc123.trycloudflare.com"  # kept for the next On
    assert ctrl._kick_webhook.applied == [1, 1]  # enable, then off reconciles the listener
    assert menu_of(ctrl).menu == "remote_access"  # the Off press came from the Remote access menu
    assert kb_labels(markup) == remote_labels(False)


def test_reply_text_kick_webhook_off_when_already_off(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    open_webhook_menu(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable Kick webhook"))
    assert "already off" in text
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.applied == []
    assert menu_of(ctrl).menu == "kick_webhook"


def test_reply_text_remote_access_on_without_a_saved_setup(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable endpoint"))
    assert "No saved public URL yet" in text
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.applied == []
    assert kb_labels(markup) == remote_labels(False)


def test_reply_text_webhook_toggle_on_and_off(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_webhook_menu(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable Kick webhook"))
    assert "Kick webhook enabled" in text
    assert "The endpoint is off" in text  # deliveries need the endpoint
    assert config.kick.webhook.enabled is True
    assert kb_labels(markup) == webhook_labels(True)
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable Kick webhook"))
    assert "Kick webhook disabled" in text
    assert config.kick.webhook.enabled is False
    assert kb_labels(markup) == webhook_labels(False)


def test_webhook_enable_on_tailnet_endpoint_warns(tmp_path):
    """A tailnet panel address cannot take Kick events, but the enable is no
    longer refused: the reply warns and the probe note says unreachable."""
    config = base_config()
    config["endpoint"] = {
        "enabled": True,
        "listen_host": "0.0.0.0",
        "listen_port": 8787,
        "public_url": "https://box.tailnet.ts.net",
    }
    (tmp_path / "config.json").write_text(json.dumps(config, indent=4))
    loaded = get_config(tmp_path / "config.json")
    ctrl = TelegramController(
        loaded,
        FakeRecorder(),
        FakeMonitor(),
        FakeEventSub(),
        on_restart=None,
        kick_webhook=FakeKickWebhook(),
    )
    open_webhook_menu(ctrl)
    text, _ = asyncio.run(ctrl.handle_reply_text("Enable Kick webhook"))
    assert "tailnet" in text
    assert loaded.kick.webhook.enabled is True
    assert read_file(tmp_path)["kick"]["webhook"]["enabled"] is True


def test_webhook_enable_allowed_with_separate_kick_url(tmp_path):
    """A separate public Kick entry allows the enable on a serve endpoint."""
    config = base_config()
    config["endpoint"] = {
        "enabled": True,
        "listen_host": "0.0.0.0",
        "listen_port": 8787,
        "public_url": "https://box.tailnet.ts.net",
    }
    config["kick"] = {
        "record_chat": True,
        "webhook": {"enabled": False, "public_url": "https://kick.example.com"},
    }
    (tmp_path / "config.json").write_text(json.dumps(config, indent=4))
    loaded = get_config(tmp_path / "config.json")
    ctrl = TelegramController(
        loaded,
        FakeRecorder(),
        FakeMonitor(),
        FakeEventSub(),
        on_restart=None,
        kick_webhook=FakeKickWebhook(),
    )
    menu_text, _ = open_webhook_menu(ctrl)
    assert "https://kick.example.com/kick/webhook (own entry)" in menu_text
    text, _ = asyncio.run(ctrl.handle_reply_text("Enable Kick webhook"))
    assert "Kick webhook enabled" in text
    assert loaded.kick.webhook.enabled is True
    assert read_file(tmp_path)["kick"]["webhook"]["enabled"] is True


def test_menu_texts_split_endpoint_and_webhook(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.endpoint.public_url = "https://abc123.trycloudflare.com"
    config.endpoint.enabled = True
    config.kick.webhook.enabled = True
    webhook_text = asyncio.run(ctrl.menu_text("kick_webhook"))
    assert "Kick webhook: on" in webhook_text
    remote_text = asyncio.run(ctrl.menu_text("remote_access"))
    assert "Endpoint: on (https://abc123.trycloudflare.com/)" in remote_text


def test_reply_text_channel_hold_menu(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    menu_of(ctrl).menu, menu_of(ctrl).channel = "channel", "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Hold delay"))
    assert "YouTube hold delay for twitch:channel1" in text
    assert "Global: 0s" in text
    assert kb_labels(markup) == ["Off", "30s", "60s", "120s", "300s", "600s", "\u2713 Global", "Custom", "Back"]
    assert menu_of(ctrl).menu == "channel_hold"


def test_channel_hold_keyboard_marks_channel_override(tmp_path):
    """The keyboard uses the chat's channel.

    A bug built it from an empty channel, so it always marked Global even
    when the channel had an override.
    """
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.channel_youtube_hold_seconds = {"twitch:channel1": 600}
    menu_of(ctrl).menu, menu_of(ctrl).channel = "channel", "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Hold delay"))
    assert "YouTube hold delay for twitch:channel1: 600s" in text
    labels = kb_labels(markup)
    assert "\u2713 600s" in labels
    assert "\u2713 Global" not in labels


def test_keyboard_state_is_per_chat(tmp_path):
    """Each chat's keyboard renders from that chat's own state.

    A second chat must not see the admin chat's selected channel.
    """
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    config.channel_youtube_hold_seconds = {"twitch:ch": 600}
    other = ADMIN_ID + 1
    asyncio.run(ctrl.handle_reply_text("Channels", chat_id=other))
    asyncio.run(ctrl.handle_reply_text("\u2022 twitch:ch", chat_id=other))
    text, markup = asyncio.run(ctrl.handle_reply_text("Hold delay", chat_id=other))
    assert "YouTube hold delay for twitch:ch: 600s" in text
    assert "\u2713 600s" in kb_labels(markup)

    # The admin chat keeps its own menu: no channel selected yet.
    asyncio.run(ctrl.handle_reply_text("Channels"))
    asyncio.run(ctrl.handle_reply_text("\u2022 twitch:channel1"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Hold delay"))
    assert "YouTube hold delay for twitch:channel1: 0s" in text
    assert "\u2713 Global" in kb_labels(markup)


def test_off_preset_values_mark_custom(tmp_path):
    """A value outside the presets marks Custom alone.

    The presets and the global value stay unmarked.
    """
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.channel_youtube_hold_seconds = {"twitch:channel1": 90}
    config.retention_days = 45
    config.max_concurrent_recordings = 4
    config.disk.max_total_gb = 60
    for menu, channel_name in (
        ("channel_hold", "twitch:channel1"),
        ("retention", None),
        ("maxrec", None),
        ("disk_maxsize", None),
    ):
        labels = kb_labels(ctrl.reply_keyboard(menu, channel_name))
        assert [label for label in labels if label.startswith("\u2713")] == ["\u2713 Custom"], menu


def test_reply_text_channel_hold_set_preset(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    menu_of(ctrl).menu, menu_of(ctrl).channel = "channel", "twitch:channel1"
    asyncio.run(ctrl.handle_reply_text("Hold delay"))
    text, markup = asyncio.run(ctrl.handle_reply_text("60s"))
    assert "Hold delay for twitch:channel1 set to 60s" in text
    assert read_file(tmp_path)["channel_youtube_hold_seconds"] == {"twitch:channel1": 60}
    assert config.channel_youtube_hold_seconds == {"twitch:channel1": 60.0}
    assert menu_of(ctrl).menu == "channel"
    assert menu_of(ctrl).channel == "twitch:channel1"


def test_reply_text_channel_hold_default_resets(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.channel_youtube_hold_seconds = {"twitch:channel1": 60}
    menu_of(ctrl).menu, menu_of(ctrl).channel = "channel_hold", "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Global"))
    assert "reset to global" in text
    assert read_file(tmp_path)["channel_youtube_hold_seconds"] == {}
    assert menu_of(ctrl).menu == "channel"
    assert menu_of(ctrl).channel == "twitch:channel1"


def test_reply_text_channel_hold_custom(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    menu_of(ctrl).menu, menu_of(ctrl).channel = "channel", "twitch:channel1"
    asyncio.run(ctrl.handle_reply_text("Hold delay"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Custom"))
    assert "Hold delay for twitch:channel1" in text
    assert menu_of(ctrl).menu == "custom"
    assert menu_of(ctrl).custom == "channel_hold"
    assert menu_of(ctrl).channel == "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("90"))
    assert read_file(tmp_path)["channel_youtube_hold_seconds"] == {"twitch:channel1": 90}
    assert menu_of(ctrl).menu == "channel"
    assert menu_of(ctrl).channel == "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "channels"


def test_back_from_channel_hold_to_channel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    menu_of(ctrl).menu, menu_of(ctrl).channel = "channel_hold", "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "channel"
    assert menu_of(ctrl).channel == "twitch:channel1"
    assert "Output mode" in text


def test_handle_channel_hold_invalid(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    for args in (["twitch:channel1", "-5"], ["twitch:channel1", "abc"]):
        text = ctrl.handle_channel_hold(args)
        assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_remove_clears_hold_override(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    ctrl.handle_channel_hold(["twitch:channel1", "60"])
    assert read_file(tmp_path)["channel_youtube_hold_seconds"] == {"twitch:channel1": 60}
    asyncio.run(ctrl.handle_remove(["twitch:channel1"]))
    assert read_file(tmp_path).get("channel_youtube_hold_seconds", {}) == {}
    assert "twitch:channel1" not in read_file(tmp_path)["channels"]


def test_every_advertised_command_has_a_handler(tmp_path):
    """The help text and the Telegram /-menu must not name a command without a handler."""
    from telegram.ext import CommandHandler

    _, ctrl, _, _, _ = make_controller(tmp_path)

    registered = {name for h in ctrl.command_handlers() if isinstance(h, CommandHandler) for name in h.commands}
    advertised = {c.command for c in ctrl.command_list()}
    assert registered == advertised


def test_webhook_toggle_reconciles_the_listener(tmp_path):
    """Turning the webhook on must start its reconcile, and off must stop it."""
    config, ctrl, _, _, _ = make_controller(tmp_path)
    config.endpoint.public_url = "https://x.example.com"
    config.endpoint.enabled = True  # subscriptions need a reachable endpoint

    text = asyncio.run(ctrl._set_webhook_enabled(True))
    assert text.startswith("Kick webhook enabled")
    assert ctrl._kick_webhook.applied == [1]
    assert ctrl._kick_webhook.synced == [config.channels]

    text = asyncio.run(ctrl._set_webhook_enabled(False))
    assert text == "Kick webhook disabled"
    assert ctrl._kick_webhook.applied == [1, 1]
    assert ctrl._kick_webhook.synced == [config.channels]  # no reconcile when it goes off
    assert read_file(tmp_path)["kick"]["webhook"]["enabled"] is False


def test_webhook_toggle_on_without_endpoint_skips_the_reconcile(tmp_path):
    """Subscriptions need a reachable endpoint, so this path only saves the flag."""
    config, ctrl, _, _, _ = make_controller(tmp_path)
    config.endpoint.enabled = False

    asyncio.run(ctrl._set_webhook_enabled(True))

    assert ctrl._kick_webhook.applied == [1]
    assert ctrl._kick_webhook.synced == []


def test_reload_reconciles_the_listener(tmp_path):
    """A hand-edited endpoint or API state must reach the live listener."""
    _, ctrl, _, _, _ = make_controller(tmp_path)
    file_config = read_file(tmp_path)
    file_config["endpoint"] = {
        "enabled": True,
        "listen_host": "127.0.0.1",
        "listen_port": 8787,
        "public_url": "https://new.example.com",
    }
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))

    text = asyncio.run(ctrl.handle_reload())

    assert text == "\u2705 Config reloaded from config.json"
    assert ctrl._kick_webhook.applied == [1]
    assert ctrl._kick_webhook.synced == [ctrl._config.channels]


def _admin_update(user_id, text="hi"):
    """A real PTB update from one user, for the admin gate behaviour check."""
    user = TelegramUser(id=user_id, first_name="admin", is_bot=False)
    chat = Chat(id=user_id, type=Chat.PRIVATE)
    message = Message(message_id=1, date=datetime.now(UTC), chat=chat, from_user=user, text=text)
    return Update(update_id=1, message=message)


def _registered_callback_handler(handlers):
    """The callback handler the handler table actually returns."""
    return next(h for h in handlers if isinstance(h, AdminCallbackQueryHandler))


def test_reload_moves_the_admin_gate_to_the_new_identity(tmp_path):
    """A reloaded telegram_user_id must replace the previous admin everywhere.

    The handler filters, the callback gate and the controller's own admin id
    are built once, so a removal or a handover used to leave the previous
    identity authorized and the new one authorized nowhere.
    """
    config, ctrl, _, _, _ = make_controller(tmp_path)
    handlers = ctrl.command_handlers()
    old_admin, new_admin = 12345, 22222
    assert ctrl._admin_filter.check_update(_admin_update(old_admin))
    registered = _registered_callback_handler(handlers)
    assert registered is ctrl._callback_handler, "the table must carry the handler the rebind reaches"
    assert registered._admin_id == old_admin
    text_handler = next(h for h in handlers if isinstance(h, MessageHandler))
    assert text_handler.filters.check_update(_admin_update(old_admin, "menu"))

    file_config = read_file(tmp_path)
    file_config["telegram_user_id"] = new_admin
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))
    asyncio.run(ctrl.handle_reload())

    assert config.telegram_user_id == new_admin
    assert ctrl._admin_id == new_admin
    # The one filter object every handler shares now admits only the new id.
    assert ctrl._admin_filter.check_update(_admin_update(new_admin))
    assert not ctrl._admin_filter.check_update(_admin_update(old_admin))
    # The gate the dispatcher runs is the one the reload re-pointed.
    assert _registered_callback_handler(ctrl.command_handlers())._admin_id == new_admin
    assert not text_handler.filters.check_update(_admin_update(old_admin, "menu"))
    assert text_handler.filters.check_update(_admin_update(new_admin, "menu"))


def test_reload_reports_the_keys_that_need_a_restart(tmp_path):
    """A rotated bot token or client secret must not read as applied."""
    _, ctrl, _, _, _ = make_controller(tmp_path)
    file_config = read_file(tmp_path)
    file_config["twitch_client_secret"] = "rotated"
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))

    text = asyncio.run(ctrl.handle_reload())

    assert text.startswith("\u26a0\ufe0f")
    assert "twitch_client_secret" in text


def test_reload_stops_a_channel_that_left_the_file(tmp_path):
    """A hand edit plus /reload must release the channel it removed.

    /remove stops the capture, drops the monitor's live state and deletes the
    subscriptions. A reload bypassed all of it, and no later command could
    stop the capture, because /remove refuses a channel that is not listed.
    """
    config, ctrl, recorder, monitor, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "twitch:channel2"], recording=["twitch:channel2"]
    )
    file_config = read_file(tmp_path)
    file_config["channels"] = ["twitch:channel1"]
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))

    text = asyncio.run(ctrl.handle_reload())

    assert recorder.stop_calls == ["twitch:channel2"]
    assert not recorder.is_recording("twitch:channel2")
    assert "twitch:channel2" in monitor.remove_calls
    assert "twitch:channel2" in eventsub.removed
    assert "Recording stopped." in text


def test_reload_reports_a_released_channel_even_when_applying_fails(tmp_path):
    """A release that already happened must be reported, not swallowed.

    The channel is gone from the file, so a retry cannot report it again: the
    reply that carries the apply failure must still name it.
    """
    config, ctrl, recorder, monitor, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "twitch:channel2"], recording=["twitch:channel2"]
    )
    file_config = read_file(tmp_path)
    file_config["channels"] = ["twitch:channel1"]
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))

    async def failing_apply_state():
        msg = "listener rebind failed"
        raise RuntimeError(msg)

    ctrl._kick_webhook.apply_state = failing_apply_state

    text = asyncio.run(ctrl.handle_reload())

    assert text.startswith("\u26a0\ufe0f")
    assert "listener rebind failed" in text
    assert "twitch:channel2" in text
    assert recorder.stop_calls == ["twitch:channel2"]


def test_reply_text_kick_webhook_test_delivery_reports_result(tmp_path):
    """The Test delivery button runs the listener's proof and reports it."""
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_webhook_menu(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Test delivery"))
    assert text.startswith("\u2705 ")
    assert "first delivery in 3s" in text
    assert ctrl._kick_webhook.verified == [180.0]
    assert menu_of(ctrl).menu == "kick_webhook"
    assert kb_labels(markup) == webhook_labels(False)


def test_hold_sets_the_global_delay(tmp_path):
    """The /hold slash sets the global delay every channel falls back to."""
    config, ctrl, _, _, _ = make_controller(tmp_path)
    assert ctrl.handle_global_hold(["90"]) == "Hold delay set to 90s (0 = end immediately)"
    assert read_file(tmp_path)["youtube"]["hold_seconds"] == 90
    assert ctrl.handle_global_hold(["soon"]).startswith("❌")
    assert read_file(tmp_path)["youtube"]["hold_seconds"] == 90


def test_status_names_degraded_problems(tmp_path):
    """The bot status names present problems instead of reading all-green."""
    from stream_archive.health import clear_degraded, set_degraded

    config, ctrl, _, _, _ = make_controller(tmp_path)
    set_degraded("disk_full", "only 0.2 GB free on the archive disk")
    try:
        assert "Degraded: disk_full" in asyncio.run(ctrl.handle_status())
    finally:
        clear_degraded("disk_full")
