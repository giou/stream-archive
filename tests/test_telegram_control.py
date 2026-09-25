import asyncio
import base64
import html
import json
import re
import threading
import types
import unittest.mock
from datetime import UTC, datetime

from conftest import make_config as valid_config
from telegram import Chat, Message, Update
from telegram import User as TelegramUser
from telegram.ext import MessageHandler

from stream_archive.config import get_config
from stream_archive.telegram import TelegramController
from stream_archive.telegram.dispatcher import _deferred_affected_channels
from stream_archive.telegram.menus_callbacks import AdminCallbackQueryHandler
from stream_archive.tunnels import tailscale_funnel_off


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

    async def apply_state(self):
        self.applied.append(1)

    async def add_channel(self, channel):
        self.added.append(channel)

    async def remove_channel(self, channel):
        self.removed.append(channel)

    async def sync_channels(self, channels):
        self.synced.append(list(channels))


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


def read_file(tmp_path):
    return json.loads((tmp_path / "config.json").read_text())


def probe_ok(ctrl):
    """Fake the reachability probe so enable flows do not hit the network."""

    async def probe(url):
        return True

    ctrl._probe_webhook_url = probe


def open_settings(ctrl):
    """Open the Settings menu, which owns every global setting."""
    return asyncio.run(ctrl.handle_reply_text("Settings"))


def open_remote_access(ctrl):
    """Open the Remote access menu, which owns the public URL and its tunnels."""
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


def kb_labels(markup):
    data = markup.to_dict()
    rows = data.get("inline_keyboard") or data.get("keyboard")
    return [b["text"] for row in rows for b in row]


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
        "Cloudflare tunnel",
        "Tailscale funnel",
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
    return [f"{toggle_action(enabled)} Kick webhook", "Back"]


def cloudflare_labels(enabled):
    return [f"{toggle_action(enabled)} Cloudflare tunnel", "Quick tunnel", "Named tunnel", "Back"]


def tailscale_labels(enabled):
    return [f"{toggle_action(enabled)} Tailscale funnel", "Back"]


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


def test_tailscale_webhook_url_missing_binary(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "not installed" in hint
    assert "tailscale.com/install.sh" in hint


def test_tailscale_webhook_url_status_failure(tmp_path, monkeypatch):
    scripted_exec(monkeypatch, status=_FakeProc(returncode=1, stderr=b"failed to connect to local tailscaled"))
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "failed to connect to local tailscaled" in hint


def test_tailscale_webhook_url_enables_funnel(tmp_path, monkeypatch):
    calls = scripted_exec(
        monkeypatch,
        status=_FakeProc(stdout=_status_json()),
        funnel=_FakeProc(stdout=b"Funnel already enabled\n"),
    )
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url == "https://box.tail1234.ts.net"
    assert hint is None
    assert [argv for argv, _ in calls] == [
        ("tailscale", "status", "--json"),
        ("tailscale", "funnel", "--bg", "--yes", "8787"),
    ]


def test_tailscale_webhook_url_funnel_failure(tmp_path, monkeypatch):
    scripted_exec(
        monkeypatch,
        status=_FakeProc(stdout=_status_json()),
        funnel=_FakeProc(returncode=1, stderr=b"Funnel requires HTTPS certificates enabled"),
    )
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "tailscale funnel 8787 failed" in hint
    assert "HTTPS certificates" in hint


def test_tailscale_webhook_url_funnel_already_enabled(tmp_path, monkeypatch):
    serve_json = json.dumps(
        {
            "Foreground": {
                "cap1": {
                    "Web": {
                        "box.tail1234.ts.net:443": {
                            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}},
                        }
                    },
                }
            }
        }
    ).encode()
    calls = scripted_exec(
        monkeypatch,
        status=_FakeProc(stdout=_status_json()),
        funnel=_FakeProc(
            returncode=1,
            stderr=b"sending serve config: updating config: listener already exists for port 443",
        ),
        serve=_FakeProc(stdout=serve_json),  # serve status --json
    )
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url == "https://box.tail1234.ts.net"
    assert hint is None
    assert [argv for argv, _ in calls] == [
        ("tailscale", "status", "--json"),
        ("tailscale", "funnel", "--bg", "--yes", "8787"),
        ("tailscale", "serve", "status", "--json"),
    ]


def test_tailscale_webhook_url_listener_conflict_other_port(tmp_path, monkeypatch):
    serve_json = json.dumps(
        {
            "Foreground": {
                "cap1": {
                    "Web": {
                        "other.ts.net:443": {
                            "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}},
                        }
                    },
                }
            }
        }
    ).encode()

    scripted_exec(
        monkeypatch,
        status=_FakeProc(stdout=_status_json()),
        funnel=_FakeProc(returncode=1, stderr=b"listener already exists for port 443"),
        serve=_FakeProc(stdout=serve_json),
    )
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "tailscale funnel 8787 failed" in hint


def test_tailscale_webhook_url_funnel_timeout_kills_proc(tmp_path, monkeypatch):
    monkeypatch.setattr("stream_archive.tunnels._TAILSCALE_FUNNEL_TIMEOUT", 0.01)
    proc = _FakeProc(hang=True)
    scripted_exec(monkeypatch, status=_FakeProc(stdout=_status_json()), funnel=proc)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "timed out" in hint
    assert proc.killed


def test_tailscale_webhook_url_no_dns_name(tmp_path, monkeypatch):
    scripted_exec(monkeypatch, status=_FakeProc(stdout=json.dumps({"Self": {}}).encode()))
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "no machine DNS name" in hint


def test_tailscale_funnel_off_uses_documented_syntax(tmp_path, monkeypatch):
    calls = scripted_exec(monkeypatch, funnel=_FakeProc())
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    assert asyncio.run(tailscale_funnel_off()) is True
    assert [argv for argv, _ in calls] == [("tailscale", "funnel", "--https=443", "off")]


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


def test_cloudflared_quick_start_parses_url(tmp_path, monkeypatch):
    calls = scripted_exec(
        monkeypatch,
        cloudflared=_CloudflaredFakeProc(
            lines=[
                b"2026-08-14T00:00:00Z INF +-----------------------------+\n",
                b"INF |  https://abc123.trycloudflare.com  |\n",
                b"INF +-----------------------------+\n",
            ]
        ),
    )
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._cloudflared_quick_start())

    assert calls[0][0][:3] == ("cloudflared", "--no-autoupdate", "tunnel")
    assert "--url" in calls[0][0]
    assert url == "https://abc123.trycloudflare.com"  # the endpoint stores the base URL
    assert hint is None
    assert ctrl._cloudflared.running is True


def test_cloudflared_quick_start_exit_reports_output(tmp_path, monkeypatch):
    scripted_exec(monkeypatch, cloudflared=_CloudflaredFakeProc(lines=[b"error: failed to connect\n"], returncode=1))
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._cloudflared_quick_start())

    assert url is None
    assert "exited before publishing a URL" in hint
    assert "failed to connect" in hint


def test_cloudflared_quick_start_timeout_kills_proc(tmp_path, monkeypatch):
    monkeypatch.setattr("stream_archive.tunnels._CLOUDFLARED_QUICK_TIMEOUT", 0.01)
    proc = _CloudflaredFakeProc(lines=[], hang=True)
    scripted_exec(monkeypatch, cloudflared=proc)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._cloudflared_quick_start())

    assert url is None
    assert "did not publish" in hint
    assert proc.killed


def test_cloudflared_quick_start_missing_binary(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._cloudflared_quick_start())

    assert url is None
    assert "cloudflared is not installed" in hint


def test_cloudflared_named_start_registered(tmp_path, monkeypatch):
    calls = scripted_exec(
        monkeypatch, cloudflared=_CloudflaredFakeProc(lines=[b"INF Registered tunnel connection connIndex=0\n"])
    )
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    ok, hint = asyncio.run(ctrl._cloudflared_named_start("tok"))

    argv, kwargs = calls[0]
    assert argv[:4] == ("cloudflared", "tunnel", "--no-autoupdate", "run")
    # The install token is a credential: it goes in the child's
    # environment, never on the world-readable command line.
    assert "--token" not in argv
    assert kwargs["env"]["TUNNEL_TOKEN"] == "tok"
    assert ok is True
    assert hint is None
    assert ctrl._cloudflared.running is True


def test_cloudflared_named_start_failure_reports_output(tmp_path, monkeypatch):
    scripted_exec(
        monkeypatch,
        cloudflared=_CloudflaredFakeProc(
            lines=[b"ERR failed to register tunnel connection: invalid token\n"],
            returncode=1,
        ),
    )
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    ok, hint = asyncio.run(ctrl._cloudflared_named_start("bad"))

    assert ok is False
    assert "invalid token" in hint
    assert ctrl._cloudflared.running is False


def test_cloudflared_named_start_timeout_kills_proc(tmp_path, monkeypatch):
    """A silent process has not registered: report a failed start, not a running tunnel."""
    monkeypatch.setattr("stream_archive.tunnels._CLOUDFLARED_RUN_TIMEOUT", 0.01)
    proc = _CloudflaredFakeProc(lines=[], hang=True, returncode=None)
    scripted_exec(monkeypatch, cloudflared=proc)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    ok, hint = asyncio.run(ctrl._cloudflared_named_start("tok"))

    assert ok is False
    assert "did not register" in hint
    assert proc.killed
    assert ctrl._cloudflared.running is False


def test_cloudflared_token_and_url_helpers():
    from stream_archive.config import normalize_endpoint_url
    from stream_archive.tunnels import valid_token

    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun", "s": "sec"}).encode()).decode()
    assert valid_token(token)
    assert valid_token(token.rstrip("="))  # unpadded still decodes
    assert not valid_token("nope")
    assert not valid_token(base64.b64encode(b"not json").decode())
    assert not valid_token(base64.b64encode(json.dumps({"a": "acct"}).encode()).decode())  # missing t/s
    assert normalize_endpoint_url("https://x.example.com") == "https://x.example.com"
    assert normalize_endpoint_url("https://x.example.com/") == "https://x.example.com"
    assert normalize_endpoint_url("https://x.example.com/kick/webhook") == "https://x.example.com"
    assert normalize_endpoint_url("https://x.example.com/custom") == "https://x.example.com/custom"


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
    assert "tunnels are set in Remote access" in text
    assert kb_labels(markup) == webhook_labels(False)  # only the toggle and Back
    assert menu_of(ctrl).menu == "kick_webhook"


def test_reply_text_remote_access_menu(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text, markup = open_remote_access(ctrl)
    assert "Endpoint: off" in text
    assert "Kick webhook: off" in text
    assert "Control API: off" in text
    assert "set the public URL" in text
    assert kb_labels(markup) == remote_labels(False)
    assert menu_of(ctrl).menu == "remote_access"


def test_reply_text_remote_access_back_navigation(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_webhook_menu(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert menu_of(ctrl).menu == "remote_access"
    assert kb_labels(markup) == remote_labels(False)
    text, markup = asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    assert menu_of(ctrl).menu == "kick_cloudflare"
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
    config.endpoint.tunnel = "cloudflare"
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
    config.endpoint.tunnel = "cloudflare"
    config.endpoint.enabled = True
    stopped = []
    ctrl._cloudflared_stop = lambda: stopped.append(1)
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable endpoint"))
    assert "Endpoint disabled" in text
    assert "Your setup is saved" in text
    assert stopped == [1]
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


def test_reply_text_kick_webhook_cloudflare_prompt(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    assert "Quick tunnel" in text
    assert "Named tunnel" in text
    assert kb_labels(markup) == cloudflare_labels(False)
    assert menu_of(ctrl).menu == "kick_cloudflare"


def test_reply_text_kick_webhook_url_applies(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("https://tunnel.trycloudflare.com/kick/webhook"))
    assert "Endpoint enabled" in text
    assert "https://tunnel.trycloudflare.com/kick/webhook" in text
    assert "Settings \u2192 Developer \u2192 your app \u2192 Enable webhooks" in text
    assert "developer dashboard" not in text
    assert "URL is reachable" in text  # automatic probe, no button
    w = read_file(tmp_path)["endpoint"]
    assert w["enabled"] is True
    assert w["public_url"] == "https://tunnel.trycloudflare.com"
    assert w["tunnel"] == "cloudflare"
    assert w["cloudflare_managed"] is False
    assert kb_labels(markup) == cloudflare_labels(True)
    assert ctrl._kick_webhook.applied == [1]
    assert ctrl._kick_webhook.synced == [["twitch:channel1"]]
    assert menu_of(ctrl).menu == "kick_cloudflare"


def test_reply_text_kick_webhook_enable_rearms_setup_notification(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    config.kick.webhook.setup_notified = True  # already confirmed once before
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("https://tunnel.trycloudflare.com/kick/webhook"))
    assert read_file(tmp_path)["kick"]["webhook"]["setup_notified"] is False  # re-armed


def test_reply_text_kick_webhook_url_normalizes_root_path(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("https://tunnel.trycloudflare.com"))
    assert "Endpoint: https://tunnel.trycloudflare.com/" in text
    assert "https://tunnel.trycloudflare.com/kick/webhook" in text
    assert read_file(tmp_path)["endpoint"]["public_url"] == "https://tunnel.trycloudflare.com"
    assert menu_of(ctrl).menu == "kick_cloudflare"


def test_reply_text_kick_webhook_quick_tunnel_enables(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)

    async def fake_quick():
        # The adapter already normalizes the tunnel output to a base URL.
        return "https://abc123.trycloudflare.com", None

    ctrl._cloudflared_quick_start = fake_quick
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Quick tunnel"))
    assert "Endpoint enabled" in text
    assert "https://abc123.trycloudflare.com/kick/webhook" in text
    assert "cloudflared quick tunnel is running" in text
    assert "URL is reachable" in text
    w = read_file(tmp_path)["endpoint"]
    assert w["enabled"] is True
    assert w["public_url"] == "https://abc123.trycloudflare.com"
    assert w["tunnel"] == "cloudflare"
    assert w["cloudflare_managed"] is True
    assert w["cloudflare_token"] == ""
    assert ctrl._kick_webhook.applied == [1]
    assert menu_of(ctrl).menu == "kick_cloudflare"


def test_reply_text_kick_webhook_quick_tunnel_failure_stays(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    async def failing_quick():
        return None, "cloudflared is not installed in this container."

    ctrl._cloudflared_quick_start = failing_quick
    before = read_file(tmp_path)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Quick tunnel"))
    assert "cloudflared is not installed" in text
    assert menu_of(ctrl).menu == "kick_cloudflare"
    assert kb_labels(markup) == cloudflare_labels(False)
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.applied == []


def test_reply_text_kick_webhook_named_token_prompt(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    assert "cloudflared service install" in text
    assert kb_labels(markup) == KICK_TOKEN_LABELS
    assert menu_of(ctrl).menu == "kick_cloudflare_token"


def test_reply_text_kick_webhook_named_token_accepted(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()
    started = []

    async def fake_named(tok, config_path=None):
        started.append((tok, config_path))

    ctrl._cloudflared_named_start = fake_named
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text(f"cloudflared.exe service install {token}"))
    assert "Tunnel token accepted" in text
    assert "kick.example.com" in text
    assert started == []  # cloudflared starts only after the hostname is known
    assert read_file(tmp_path)["endpoint"]["cloudflare_token"] == token
    assert read_file(tmp_path)["endpoint"]["enabled"] is False
    assert kb_labels(markup) == KICK_TOKEN_LABELS
    assert menu_of(ctrl).menu == "kick_cloudflare_hostname"


def test_reply_text_kick_webhook_named_token_invalid_stays(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("cloudflared service install nope"))
    assert "That doesn't look like a cloudflared tunnel token" in text
    assert menu_of(ctrl).menu == "kick_cloudflare_token"
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.applied == []


def test_reply_text_kick_webhook_named_hostname_invalid_stays(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    asyncio.run(ctrl.handle_reply_text(token))
    before = read_file(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("nope"))
    assert "doesn't look like a public hostname" in text
    assert menu_of(ctrl).menu == "kick_cloudflare_hostname"
    assert read_file(tmp_path) == before


def test_reply_text_kick_webhook_named_flow_skip_dns(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()
    started = []

    async def fake_named(tok, config_path=None):
        started.append((tok, str(config_path)))
        return True, None

    ctrl._cloudflared_named_start = fake_named
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    asyncio.run(ctrl.handle_reply_text(token))
    text, markup = asyncio.run(ctrl.handle_reply_text("kick.example.com"))
    assert "kick.example.com" in text
    assert kb_labels(markup) == ["Skip DNS", "Back"]
    assert menu_of(ctrl).menu == "kick_cloudflare_dns"
    text, markup = asyncio.run(ctrl.handle_reply_text("Skip DNS"))
    assert "Endpoint enabled" in text
    assert "https://kick.example.com/kick/webhook" in text
    assert "CNAME kick.example.com \u2192 tun-id.cfargotunnel.com" in text
    w = read_file(tmp_path)["endpoint"]
    assert w["enabled"] is True
    assert w["public_url"] == "https://kick.example.com"
    assert w["tunnel"] == "cloudflare"
    assert w["cloudflare_token"] == token
    assert w["cloudflare_managed"] is True
    assert len(started) == 1
    assert started[0][0] == token
    cfg_path = tmp_path / "cloudflared" / "tun-id.yml"
    assert started[0][1] == str(cfg_path)
    cfg = cfg_path.read_text()
    assert "hostname: kick.example.com" in cfg
    assert "service: http://127.0.0.1:8787" in cfg
    assert menu_of(ctrl).menu == "kick_cloudflare"


def test_reply_text_kick_webhook_named_flow_with_api_token(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()

    async def fake_named(tok, config_path=None):
        return True, None

    async def fake_dns(api_token, chat_id=None):
        return True, "\u2705 DNS record created - the hostname now points at your tunnel."

    ctrl._cloudflared_named_start = fake_named
    ctrl._create_cloudflare_dns = fake_dns
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    asyncio.run(ctrl.handle_reply_text(token))
    asyncio.run(ctrl.handle_reply_text("kick.example.com"))
    text, markup = asyncio.run(ctrl.handle_reply_text("api-token-123"))
    assert "Endpoint enabled" in text
    assert "DNS record created" in text
    assert "CNAME kick.example.com" not in text  # no manual step needed
    assert read_file(tmp_path)["endpoint"]["public_url"] == "https://kick.example.com"
    assert menu_of(ctrl).menu == "kick_cloudflare"


class _FakeCfResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeCfClient:
    def __init__(self, zones, records=None, verify_status="active"):
        self.zones = zones
        self.records = records or []
        self.verify_status = verify_status
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls.append(("get", url))
        if url.endswith("/user/tokens/verify"):
            if self.verify_status == "account-owned":
                return _FakeCfResp(401, {"success": False, "errors": [{"code": 1000, "message": "Invalid API Token"}]})
            return _FakeCfResp(200, {"result": {"status": self.verify_status}})
        if url.endswith("/tokens/verify"):
            return _FakeCfResp(200, {"result": {"status": "active"}})
        if "/zones?per_page=50" in url:
            return _FakeCfResp(200, {"result": self.zones})
        if "/dns_records?" in url:
            return _FakeCfResp(200, {"result": self.records})
        return _FakeCfResp(404, {})

    async def post(self, url, headers=None, json=None):
        self.calls.append(("post", url, json))
        return _FakeCfResp(200, {"result": json})


def make_cf_ctrl(tmp_path, client):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._http = client
    ctrl._owns_http = False
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()
    config.endpoint.cloudflare_token = token
    menu_of(ctrl).cloudflare_hostname = "kick.example.com"
    return config, ctrl, token


def test_create_cloudflare_dns_creates_record(tmp_path):
    client = _FakeCfClient(zones=[{"id": "z1", "name": "example.com"}])
    config, ctrl, _ = make_cf_ctrl(tmp_path, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is True
    assert (
        "post",
        "https://api.cloudflare.com/client/v4/zones/z1/dns_records",
        {
            "type": "CNAME",
            "name": "kick.example.com",
            "content": "tun-id.cfargotunnel.com",
            "proxied": True,
        },
    ) in client.calls


def test_create_cloudflare_dns_picks_longest_zone_match(tmp_path):
    client = _FakeCfClient(
        zones=[
            {"id": "z1", "name": "example.com"},
            {"id": "z2", "name": "sub.example.com"},
        ]
    )
    config, ctrl, _ = make_cf_ctrl(tmp_path, client)
    menu_of(ctrl).cloudflare_hostname = "kick.sub.example.com"

    ok, _ = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert (
        "post",
        "https://api.cloudflare.com/client/v4/zones/z2/dns_records",
        {
            "type": "CNAME",
            "name": "kick.sub.example.com",
            "content": "tun-id.cfargotunnel.com",
            "proxied": True,
        },
    ) in client.calls


def test_create_cloudflare_dns_existing_same_target_ok(tmp_path):
    client = _FakeCfClient(
        zones=[{"id": "z1", "name": "example.com"}],
        records=[{"content": "tun-id.cfargotunnel.com"}],
    )
    config, ctrl, _ = make_cf_ctrl(tmp_path, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is True
    assert "already points" in message
    assert not any(c[0] == "post" for c in client.calls)


def test_create_cloudflare_dns_existing_other_target_fails(tmp_path):
    client = _FakeCfClient(
        zones=[{"id": "z1", "name": "example.com"}],
        records=[{"content": "elsewhere.example.net"}],
    )
    config, ctrl, _ = make_cf_ctrl(tmp_path, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is False
    assert "already used" in message


def test_create_cloudflare_dns_no_zone_fails(tmp_path):
    client = _FakeCfClient(zones=[{"id": "z1", "name": "other.org"}])
    config, ctrl, _ = make_cf_ctrl(tmp_path, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is False
    assert "No Cloudflare zone matches kick.example.com" in message


def test_create_cloudflare_dns_account_owned_token_fallback(tmp_path):
    # cfat_ account tokens cannot pass /user/tokens/verify. The account-scoped
    # verify endpoint must be used instead.
    client = _FakeCfClient(zones=[{"id": "z1", "name": "example.com"}], verify_status="account-owned")
    config, ctrl, _ = make_cf_ctrl(tmp_path, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("cfat_..."))

    assert ("get", "https://api.cloudflare.com/client/v4/accounts/acct/tokens/verify") in client.calls
    assert (
        "post",
        "https://api.cloudflare.com/client/v4/zones/z1/dns_records",
        {
            "type": "CNAME",
            "name": "kick.example.com",
            "content": "tun-id.cfargotunnel.com",
            "proxied": True,
        },
    ) in client.calls


def test_create_cloudflare_dns_invalid_token_fails(tmp_path):
    client = _FakeCfClient(zones=[], verify_status="expired")
    config, ctrl, _ = make_cf_ctrl(tmp_path, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("bad"))

    assert ok is False
    assert "not valid" in message


def test_restore_named_tunnel_uses_local_config(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()
    config.endpoint.public_url = "https://kick.example.com/kick/webhook"
    config.endpoint.tunnel = "cloudflare"
    config.endpoint.cloudflare_token = token
    config.endpoint.cloudflare_managed = True
    config.endpoint.enabled = True
    started = []

    async def fake_named(tok, config_path=None):
        started.append((tok, str(config_path) if config_path else None))
        return True, None

    async def fake_send(text):
        msg = f"unexpected admin message: {text}"
        raise AssertionError(msg)

    ctrl._cloudflared_named_start = fake_named
    ctrl._send_admin = fake_send
    asyncio.run(ctrl._restore_cloudflared())

    cfg_path = tmp_path / "cloudflared" / "tun-id.yml"
    assert started == [(token, str(cfg_path))]
    assert "hostname: kick.example.com" in cfg_path.read_text()


def test_reply_text_kick_webhook_named_dns_failure_stays(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()

    async def fake_named(tok, config_path=None):
        return True, None

    async def fake_dns(api_token, chat_id=None):
        return False, "\u274c That Cloudflare API token is not valid."

    ctrl._cloudflared_named_start = fake_named
    ctrl._create_cloudflare_dns = fake_dns
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    asyncio.run(ctrl.handle_reply_text(token))
    asyncio.run(ctrl.handle_reply_text("kick.example.com"))
    before = read_file(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("bad-token"))
    assert "not valid" in text
    assert menu_of(ctrl).menu == "kick_cloudflare_dns"
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.applied == []


def test_reply_text_kick_webhook_off_keeps_the_setup(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    stopped = []

    async def fake_quick():
        # cloudflared prints a base URL, which the adapter normalizes.
        return "https://abc123.trycloudflare.com", None

    ctrl._cloudflared_quick_start = fake_quick
    ctrl._cloudflared_stop = lambda: stopped.append(1)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Quick tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable Cloudflare tunnel"))
    assert "Endpoint disabled" in text
    assert "Your setup is saved" in text
    assert stopped == [1]  # the managed tunnel stops
    w = read_file(tmp_path)["endpoint"]
    assert w["enabled"] is False
    assert w["public_url"] == "https://abc123.trycloudflare.com"  # kept for the next On
    assert w["tunnel"] == "cloudflare"
    assert w["cloudflare_managed"] is True
    assert ctrl._kick_webhook.applied == [1, 1]  # enable, then off reconciles the listener
    assert menu_of(ctrl).menu == "kick_cloudflare"  # the Off press came from the Cloudflare menu
    assert kb_labels(markup) == cloudflare_labels(False)


def test_reply_text_kick_webhook_off_when_already_off(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    open_webhook_menu(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable Kick webhook"))
    assert "already off" in text
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.applied == []
    assert menu_of(ctrl).menu == "kick_webhook"


def test_reply_text_remote_access_on_restarts_a_managed_quick_tunnel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    config.endpoint.public_url = "https://old.trycloudflare.com"
    config.endpoint.tunnel = "cloudflare"
    config.endpoint.cloudflare_managed = True
    started = []

    async def fake_quick():
        started.append(1)
        return "https://new.trycloudflare.com", None

    ctrl._cloudflared_quick_start = fake_quick
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable endpoint"))
    assert started == [1]
    assert "Endpoint: https://new.trycloudflare.com/" in text
    assert "https://new.trycloudflare.com/kick/webhook" in text
    w = read_file(tmp_path)["endpoint"]
    assert w["enabled"] is True
    assert w["public_url"] == "https://new.trycloudflare.com"  # the quick URL rotates
    assert kb_labels(markup) == remote_labels(True)


def test_reply_text_remote_access_on_without_a_saved_setup(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable endpoint"))
    assert "No saved tunnel yet" in text
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


def test_reply_text_kick_webhook_tailscale_detected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)

    async def fake_tailscale():
        return "https://box.tail1234.ts.net", None

    ctrl._tailscale_webhook_url = fake_tailscale
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Tailscale funnel"))
    assert "Tailscale funnel: off" in text
    assert kb_labels(markup) == tailscale_labels(False)
    assert menu_of(ctrl).menu == "kick_tailscale"
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable Tailscale funnel"))
    assert "https://box.tail1234.ts.net" in text
    assert "tailscale funnel 8787 is enabled" in text
    # No reachability probe for tailscale. The funnel is verified against the
    # daemon, and the container cannot reach the host's tailnet IP (hairpin).
    assert "URL is reachable" not in text
    assert "doesn't respond yet" not in text
    assert read_file(tmp_path)["endpoint"]["enabled"] is True
    assert read_file(tmp_path)["endpoint"]["public_url"] == "https://box.tail1234.ts.net"
    assert read_file(tmp_path)["endpoint"]["tunnel"] == "tailscale"
    assert ctrl._kick_webhook.applied == [1]
    assert menu_of(ctrl).menu == "kick_tailscale"
    assert kb_labels(markup) == tailscale_labels(True)


def test_reply_text_kick_webhook_tailscale_fallback_to_input(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    async def no_tailscale():
        return None, "Tailscale is not installed in this container."

    ctrl._tailscale_webhook_url = no_tailscale
    before = read_file(tmp_path)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Tailscale funnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable Tailscale funnel"))
    assert "Tailscale is not installed" in text
    assert "Cloudflare tunnel instead" in text
    assert menu_of(ctrl).menu == "kick_tailscale"
    assert kb_labels(markup) == tailscale_labels(False)
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.applied == []


def test_reply_text_kick_tailscale_off_turns_off_the_funnel(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.endpoint.public_url = "https://box.tail1234.ts.net"
    config.endpoint.tunnel = "tailscale"
    config.endpoint.enabled = True
    funnel_off_calls = []

    async def fake_funnel_off():
        funnel_off_calls.append(1)
        return True

    monkeypatch.setattr("stream_archive.telegram.commands_webhook.tailscale_funnel_off", fake_funnel_off)
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Tailscale funnel"))
    assert kb_labels(markup) == tailscale_labels(True)
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable Tailscale funnel"))
    assert funnel_off_calls == [1]
    assert "Your setup is saved" in text
    w = read_file(tmp_path)["endpoint"]
    assert w["enabled"] is False
    assert w["tunnel"] == "tailscale"  # saved for the next On press
    assert w["public_url"] == "https://box.tail1234.ts.net"
    assert kb_labels(markup) == tailscale_labels(False)


def test_switch_tailscale_to_cloudflare_tears_down_funnel(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    config.endpoint.public_url = "https://box.tail1234.ts.net"
    config.endpoint.tunnel = "tailscale"
    config.endpoint.enabled = True
    funnel_off_calls = []

    async def fake_quick():
        return "https://abc123.trycloudflare.com", None

    async def fake_funnel_off():
        funnel_off_calls.append(1)
        return True

    ctrl._cloudflared_quick_start = fake_quick
    monkeypatch.setattr("stream_archive.telegram.commands_webhook.tailscale_funnel_off", fake_funnel_off)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Quick tunnel"))
    assert funnel_off_calls == [1]
    w = read_file(tmp_path)["endpoint"]
    assert w["tunnel"] == "cloudflare"
    assert w["enabled"] is True


def test_switch_cloudflare_to_tailscale_stops_cloudflared(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.endpoint.public_url = "https://abc123.trycloudflare.com/kick/webhook"
    config.endpoint.tunnel = "cloudflare"
    config.endpoint.cloudflare_token = ""
    config.endpoint.cloudflare_managed = True
    config.endpoint.enabled = True
    stopped = []

    async def fake_tailscale():
        return "https://box.tail1234.ts.net", None

    ctrl._tailscale_webhook_url = fake_tailscale
    ctrl._cloudflared_stop = lambda: stopped.append(1)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Tailscale funnel"))
    asyncio.run(ctrl.handle_reply_text("Enable Tailscale funnel"))
    assert stopped == [1]
    w = read_file(tmp_path)["endpoint"]
    assert w["tunnel"] == "tailscale"
    assert w["cloudflare_token"] == ""
    assert w["cloudflare_managed"] is False


def test_cloudflare_off_leaves_another_tunnel_alone(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.endpoint.public_url = "https://box.tail1234.ts.net"
    config.endpoint.tunnel = "tailscale"
    config.endpoint.enabled = True
    before = read_file(tmp_path)
    open_remote_access(ctrl)
    text, markup = asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    assert kb_labels(markup) == cloudflare_labels(False)  # cloudflare itself is off
    text, markup = asyncio.run(ctrl.handle_reply_text("Disable Cloudflare tunnel"))
    assert "Cloudflare tunnel is not on" in text
    assert read_file(tmp_path) == before  # the tailscale webhook keeps running


def test_cloudflare_on_without_a_saved_cloudflare_tunnel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.endpoint.public_url = "https://box.tail1234.ts.net"
    config.endpoint.tunnel = "tailscale"
    before = read_file(tmp_path)
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable Cloudflare tunnel"))
    assert "No saved Cloudflare tunnel" in text
    assert read_file(tmp_path) == before
    assert kb_labels(markup) == cloudflare_labels(False)


def test_cloudflare_on_restores_a_saved_cloudflare_tunnel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    config.endpoint.public_url = "https://my-tunnel.example.com/kick/webhook"
    config.endpoint.tunnel = "cloudflare"
    open_remote_access(ctrl)
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Enable Cloudflare tunnel"))
    assert "Endpoint enabled" in text
    assert read_file(tmp_path)["endpoint"]["enabled"] is True
    assert kb_labels(markup) == cloudflare_labels(True)


def test_restore_quick_tunnel_new_url_rearms_confirmation(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.endpoint.public_url = "https://old.trycloudflare.com/kick/webhook"
    config.endpoint.tunnel = "cloudflare"
    config.endpoint.cloudflare_managed = True
    config.kick.webhook.setup_notified = True  # confirmed before the restart
    config.endpoint.enabled = True
    sent = []

    async def fake_quick():
        return "https://new.trycloudflare.com", None  # the adapter returns a base URL

    async def fake_send(text):
        sent.append(text)

    ctrl._cloudflared_quick_start = fake_quick
    ctrl._send_admin = fake_send
    probe_ok(ctrl)
    asyncio.run(ctrl._restore_cloudflared())
    w = read_file(tmp_path)["endpoint"]
    assert w["public_url"] == "https://new.trycloudflare.com"
    assert read_file(tmp_path)["kick"]["webhook"]["setup_notified"] is False  # re-armed
    assert len(sent) == 1
    assert "new temporary URL" in sent[0]
    assert "URL is reachable" in sent[0]


def test_restore_quick_tunnel_same_url_stays_silent(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.endpoint.public_url = "https://same.trycloudflare.com/kick/webhook"
    config.endpoint.tunnel = "cloudflare"
    config.endpoint.cloudflare_managed = True
    config.kick.webhook.setup_notified = True
    config.endpoint.enabled = True
    sent = []

    async def fake_quick():
        return "https://same.trycloudflare.com", None  # the adapter returns a base URL

    async def fake_send(text):
        sent.append(text)

    ctrl._cloudflared_quick_start = fake_quick
    ctrl._send_admin = fake_send
    before = read_file(tmp_path)
    asyncio.run(ctrl._restore_cloudflared())
    assert config.kick.webhook.setup_notified is True  # live config untouched
    assert read_file(tmp_path) == before
    assert sent == []


def test_menu_texts_split_endpoint_and_webhook(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.endpoint.public_url = "https://abc123.trycloudflare.com"
    config.endpoint.tunnel = "cloudflare"
    config.endpoint.enabled = True
    config.kick.webhook.enabled = True
    webhook_text = asyncio.run(ctrl.menu_text("kick_webhook"))
    assert "Kick webhook: on" in webhook_text
    assert "cloudflare" not in webhook_text  # the URL lives with the endpoint
    remote_text = asyncio.run(ctrl.menu_text("remote_access"))
    assert "Endpoint: on (cloudflare \u00b7 https://abc123.trycloudflare.com/)" in remote_text


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


def test_create_cloudflare_dns_html_verify_body_reports_invalid(tmp_path):
    """A proxy 502 HTML page on token verify returns (False, message), with a
    'not valid' message, and never escapes as a JSONDecodeError during the
    setup flow."""

    class _HtmlVerifyResp:
        status_code = 200
        text = "<html><body>502 Bad Gateway</body></html>"

        def json(self):
            msg = "Expecting value"
            raise json.JSONDecodeError(msg, self.text, 0)

    class _HtmlCfClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return _HtmlVerifyResp()

    config, ctrl, _ = make_cf_ctrl(tmp_path, _HtmlCfClient())

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is False
    assert "not valid" in message


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


def test_write_cloudflared_config_keeps_the_file_inside_its_directory(tmp_path):
    """A token can carry a hostile tunnel id, so the file name must not hold a path."""
    _, ctrl, _, _, _ = make_controller(tmp_path)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "../../etc/evil", "s": "sec"}).encode()).decode()
    ctrl._config.endpoint.cloudflare_token = token

    path = asyncio.run(ctrl._write_cloudflared_config("kick.example.com"))

    assert path.parent == tmp_path / "cloudflared"
    assert path.name == "tunnel.yml"
    assert path.exists()


def test_parse_public_hostname_rejects_malformed_input():
    from stream_archive.tunnels import parse_public_hostname

    assert parse_public_hostname("https://[::1") is None  # unclosed IPv6 bracket
    assert parse_public_hostname("not a hostname") is None
    assert parse_public_hostname("https://kick.example.com/path") == "kick.example.com"
    assert parse_public_hostname("kick.example.com.") == "kick.example.com"


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
