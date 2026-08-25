import asyncio
import base64
import json
import threading
import types
import unittest.mock

from stream_archive.config import get_config
from stream_archive.telegram import TelegramController
from stream_archive.telegram.dispatcher import _deferred_affected_channels


class FakeRecorder:
    def __init__(self, active=(), recording=()):
        self._active = list(active)
        self._recording = set(recording)
        self.stop_calls = []
        self.chat_stop_calls = []
        self.restart_calls = []
        self.snapshot = {
            "free_gb": 100.0,
            "total_fs_gb": 500.0,
            "used_fs_gb": 400.0,
            "dir_gb": 0.0,
            "file_count": 0,
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
        return [{"channel": ch, "mode": "disk", "duration_s": 0, "size_mb": None} for ch in self._active]


class FakeMonitor:
    def __init__(self):
        self.remove_calls = []

    def remove_channel(self, channel):
        self.remove_calls.append(channel)


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
        self.started = []
        self.closed = []
        self.added = []
        self.removed = []
        self.synced = []

    async def start(self):
        self.started.append(1)

    async def close(self):
        self.closed.append(1)

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


def base_config(tmp_path):
    return {
        "telegram_user_id": 12345,
        "bot_telegram_api": "bot_token",
        "twitch_client_id": "client_id",
        "twitch_client_secret": "client_secret",
        "channels": ["twitch:channel1"],
        "proxy_list": ["httpproxy://user:pass@host:port"],
        "monitoring_interval": 60,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "recording_dir": "recordings",
        "kick": {
            "client_id": "cid",
            "client_secret": "cs",
            "record_chat": True,
            "webhook": {
                "enabled": False,
                "listen_host": "127.0.0.1",
                "listen_port": 8787,
                "public_url": "",
            },
        },
    }


def make_controller(tmp_path, channels=None, recording=(), active=(), on_restart=None):
    """Write a valid config file, then load it typed, mirroring get_config()."""
    config = base_config(tmp_path)
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
    assert monitor.remove_calls == ["twitch:ch"]
    assert "twitch:ch" not in read_file(tmp_path)["channels"]
    assert "twitch:ch" not in config.channels


def test_remove_not_recording_does_not_stop(tmp_path):
    config, ctrl, recorder, monitor, eventsub = make_controller(tmp_path, channels=["twitch:channel1", "twitch:ch"])
    text = asyncio.run(ctrl.handle_remove(["twitch:ch"]))
    assert text.startswith("Removed")
    assert recorder.stop_calls == []
    assert monitor.remove_calls == []
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
    (tmp_path / "config.json").write_text(json.dumps(base_config(tmp_path), indent=4))
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
    cfg.preferred_quality = "720p"
    rec = _recordings(["twitch:ch", "twitch:channel1"])
    assert _deferred_affected_channels(cfg, rec) == ["twitch:ch", "twitch:channel1"]


def test_deferred_affected_per_channel_quality_override(tmp_path):
    cfg = _load_config(tmp_path)
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
    ctrl._pending_apply["abcd"] = ("Output mode set to youtube", ["twitch:channel1"])
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
    assert ctrl._pending_apply == {"abcd": ("Output mode set to youtube", ["twitch:channel1"])}
    assert ctrl._apply_warnings_sent == {"abcd"}
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
    assert nonce in ctrl._pending_apply  # the tapped nonce must still resolve
    result = asyncio.run(ctrl.handle_callback(data))
    assert result is not None
    text, _ = result
    assert text.startswith("\u2705 Applied: Output mode set to youtube")
    assert "twitch:channel1: restarted with the new settings" in text
    assert recorder.restart_calls == ["twitch:channel1"]
    assert ctrl._pending_apply == {}


def test_apply_now_callback_restarts(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["twitch:channel1"])
    ctrl._pending_apply["abcd"] = ("Output mode set to youtube", ["twitch:channel1"])
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
    # Cancel keeps the current recording and restarts nothing. The entry stays
    # pending, so a later Apply now tap on the same message still works.
    ctrl._pending_apply["wxyz"] = ("Output mode set to youtube", ["twitch:channel1"])
    result = asyncio.run(ctrl.handle_callback("cancel:wxyz"))
    assert result == ("Cancelled \u2014 nothing changed", None)
    assert recorder.restart_calls == ["twitch:channel1"]
    assert ctrl._pending_apply == {"wxyz": ("Output mode set to youtube", ["twitch:channel1"])}


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
        await asyncio.sleep(0.6)
        assert flag.is_set()

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
        "streamlink": {"status": "up_to_date", "current": "8.4.0", "latest": "8.4.0"},
        "plugin": {"status": "up_to_date", "current": "8.3.0-20260701", "latest": "8.3.0-20260701"},
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


def test_update_plugin_only_ships_in_future_image(tmp_path):
    report = {
        "app": {"status": "up_to_date", "current": "1.0.0", "latest": "1.0.0"},
        "streamlink": {"status": "up_to_date", "current": "8.4.0", "latest": "8.4.0"},
        "plugin": {"status": "update", "current": "8.3.0-20260701", "latest": "9.0.0-20260801", "changelog": []},
    }
    fake = FakeUpdater(report)
    _, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "\U0001f4e6 Updates available" in text
        assert "• streamlink-ttvlol: 8.3.0-20260701 → 9.0.0-20260801 (ships in a future image)" in text
        assert "No action needed — plugin/streamlink updates ship in a future image release." in text
        assert "docker compose pull" not in text

    asyncio.run(scenario())


def test_update_up_to_date_lists_current_versions(tmp_path):
    report = {
        "app": {"status": "up_to_date", "current": "1.0.0", "latest": "1.0.0"},
        "streamlink": {"status": "up_to_date", "current": "8.4.0", "latest": "8.4.0"},
        "plugin": {"status": "up_to_date", "current": "8.3.0-20260701", "latest": "8.3.0-20260701"},
    }
    fake = FakeUpdater(report)
    _, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "\u2705 Up to date" in text
        assert "• stream-archive: v1.0.0" in text
        assert "• streamlink: 8.4.0" in text
        assert "• streamlink-ttvlol: 8.3.0-20260701" in text

    asyncio.run(scenario())
    assert fake.check_calls == [False]


def test_update_all_unknown_fails(tmp_path):
    report = {
        "app": {"status": "unknown", "current": None, "latest": None},
        "streamlink": {"status": "unknown", "current": None, "latest": None},
        "plugin": {"status": "unknown", "current": None, "latest": None},
    }
    fake = FakeUpdater(report)
    _, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "\u274c Update check failed — try again later." in text

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
    assert "free of" in text


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


ROOT_LABELS = [
    "Channels",
    "Status",
    "Chat recording",
    "Output mode",
    "Quality",
    "Retention",
    "Max recordings",
    "Max YouTube",
    "Disk",
    "Kick webhook",
]
CHAT_LABELS = ["Twitch", "Kick", "Back"]
CHAT_PLATFORM_LABELS = ["On", "Off", "Back"]
KICK_WEBHOOK_LABELS = ["Off", "Cloudflare tunnel", "Tailscale funnel", "Back"]
KICK_CLOUDFLARE_LABELS = ["Quick tunnel", "Named tunnel", "Back"]
KICK_TOKEN_LABELS = ["Back"]
DISK_LABELS = ["Max total", "Delete oldest", "Back"]


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
        "settings",
    }


def test_reply_keyboard_root_layout(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    d = ctrl.reply_keyboard("root").to_dict()
    assert d["keyboard"] == [
        [{"text": "Channels"}, {"text": "Status"}],
        [{"text": "Chat recording"}, {"text": "Output mode"}],
        [{"text": "Quality"}, {"text": "Retention"}],
        [{"text": "Max recordings"}, {"text": "Max YouTube"}],
        [{"text": "Disk"}, {"text": "Kick webhook"}],
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
        [{"text": "Back"}],
        [{"text": "Mode: disk"}, {"text": "Mode: youtube"}],
        [{"text": "Mode: both"}, {"text": "Mode: default"}],
        [{"text": "Hold delay"}, {"text": "Quality"}],
        [{"text": "Delete channel"}],
    ]


def test_reply_text_navigates_to_channels(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("Channels"))
    assert "Channels (1): twitch:channel1" in text
    assert kb_labels(markup) == ["Back", "Add channel", "\u2022 twitch:channel1"]
    assert ctrl._menu == "channels"


def test_reply_text_status(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("Status"))
    assert "Output mode: disk" in text
    assert kb_labels(markup) == ROOT_LABELS
    assert ctrl._menu == "root"


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
    assert ctrl._menu == "channels"


def test_reply_text_add_channel_invalid_stays(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    asyncio.run(ctrl.handle_reply_text("Add channel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Bad Name!"))
    assert text.startswith("\u274c")
    assert ctrl._menu == "add_channel"
    assert read_file(tmp_path) == before


def test_reply_text_channel_submenu_mode(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    text, markup = asyncio.run(ctrl.handle_reply_text("\u2022 twitch:channel1"))
    assert "Channel: twitch:channel1" in text
    assert "default (global: disk)" in text
    text, markup = asyncio.run(ctrl.handle_reply_text("Mode: youtube"))
    assert read_file(tmp_path)["channel_output_modes"] == {"twitch:channel1": "youtube"}
    assert config.channel_output_modes == {"twitch:channel1": "youtube"}
    assert ctrl._menu_channel == "twitch:channel1"


def test_reply_text_channel_delete_asks_confirm(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    asyncio.run(ctrl.handle_reply_text("\u2022 twitch:channel1"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Delete channel"))
    assert "Remove twitch:channel1 from monitoring?" in text
    assert kb_labels(markup) == ["Confirm", "Cancel"]
    assert read_file(tmp_path) == before
    assert ctrl._menu == "channel"


def test_reply_text_chat_menu_shows_platform_picker(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("Chat recording"))
    assert "Chat recording (Twitch): on" in text
    assert "Kick chat recording: on" in text
    assert kb_labels(markup) == CHAT_LABELS
    assert ctrl._menu == "chat"


def test_reply_text_chat_platform_submenu_twitch(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Chat recording"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Twitch"))
    assert "Twitch chat recording: on" in text
    assert kb_labels(markup) == CHAT_PLATFORM_LABELS
    assert ctrl._menu == "chat_twitch"


def test_reply_text_chat_platform_submenu_kick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Chat recording"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Kick"))
    assert "Kick chat recording: on" in text
    assert kb_labels(markup) == CHAT_PLATFORM_LABELS
    assert ctrl._menu == "chat_kick"


def test_reply_text_chat_twitch_off_stops_only_twitch(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(
        tmp_path, channels=["twitch:channel1", "kick:xqc"], recording=["twitch:channel1", "kick:xqc"]
    )
    asyncio.run(ctrl.handle_reply_text("Chat recording"))
    asyncio.run(ctrl.handle_reply_text("Twitch"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Off"))
    assert text == "Twitch chat recording disabled"
    assert read_file(tmp_path)["record_chat"] is False
    assert read_file(tmp_path)["kick"]["record_chat"] is True
    assert recorder.chat_stop_calls == [("twitch:channel1", "twitch")]
    assert kb_labels(markup) == CHAT_LABELS  # back at the platform picker
    assert ctrl._menu == "chat"


def test_reply_text_chat_kick_on_only_kick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_chat(["off"]))  # both off via command
    asyncio.run(ctrl.handle_reply_text("Chat recording"))
    asyncio.run(ctrl.handle_reply_text("Kick"))
    text, markup = asyncio.run(ctrl.handle_reply_text("On"))
    assert text == "Kick chat recording enabled"
    assert read_file(tmp_path)["kick"]["record_chat"] is True
    assert read_file(tmp_path)["record_chat"] is False
    assert ctrl._menu == "chat"


def test_reply_text_chat_back_navigation(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Chat recording"))
    asyncio.run(ctrl.handle_reply_text("Twitch"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._menu == "chat"
    assert kb_labels(markup) == CHAT_LABELS
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._menu == "root"
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_mode_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Output mode"))
    text, markup = asyncio.run(ctrl.handle_reply_text("youtube"))
    assert read_file(tmp_path)["output_mode"] == "youtube"
    assert config.output_mode == "youtube"
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_quality_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Quality"))
    text, markup = asyncio.run(ctrl.handle_reply_text("1080p"))
    assert read_file(tmp_path)["preferred_quality"] == "1080p"
    assert config.preferred_quality == "1080p"
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_retention_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Retention"))
    text, markup = asyncio.run(ctrl.handle_reply_text("14 days"))
    assert read_file(tmp_path)["retention_days"] == 14
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_retention_off_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Retention"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Off"))
    assert read_file(tmp_path)["retention_days"] == 0
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_retention_custom(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Retention"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Custom"))
    assert "Send the new value in days" in text
    assert kb_labels(markup) == ["Back"]
    text, markup = asyncio.run(ctrl.handle_reply_text("11"))
    assert read_file(tmp_path)["retention_days"] == 11
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_custom_invalid_keeps_state(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Retention"))
    asyncio.run(ctrl.handle_reply_text("Custom"))
    text, markup = asyncio.run(ctrl.handle_reply_text("x"))
    assert text.startswith("\u274c")
    assert ctrl._menu == "custom"
    assert read_file(tmp_path) == before


def test_reply_text_maxrec_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Max recordings"))
    text, markup = asyncio.run(ctrl.handle_reply_text("3"))
    assert read_file(tmp_path)["max_concurrent_recordings"] == 3
    assert config.max_concurrent_recordings == 3
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_disk_quick_returns_disk_menu(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Disk"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Max total"))
    assert "Max total: 0 GB (0 = disabled)" in text
    text, markup = asyncio.run(ctrl.handle_reply_text("50"))
    assert read_file(tmp_path)["disk"]["max_total_gb"] == 50
    assert config.disk.max_total_gb == 50
    assert ctrl._menu == "disk"
    assert kb_labels(markup) == DISK_LABELS


def test_reply_text_disk_submenu_descriptions(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Disk"))
    cases = [
        ("Max total", "Limits total recording size"),
    ]
    for button, desc in cases:
        text, markup = asyncio.run(ctrl.handle_reply_text(button))
        assert desc in text
        asyncio.run(ctrl.handle_reply_text("Back"))  # return to the disk menu


def test_reply_text_disk_delete_oldest_on_confirms(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_disk(["delete_oldest", "off"])
    before = read_file(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Disk"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Delete oldest"))
    assert "oldest recordings will be deleted" in text
    assert kb_labels(markup) == ["Confirm", "Cancel"]
    assert read_file(tmp_path) == before


def test_reply_text_disk_delete_oldest_off_direct(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Disk"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Delete oldest"))
    assert read_file(tmp_path)["disk"]["delete_oldest"] is False
    assert config.disk.delete_oldest is False
    assert kb_labels(markup) == DISK_LABELS


def test_reply_text_back_navigation(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    asyncio.run(ctrl.handle_reply_text("\u2022 twitch:channel1"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert kb_labels(markup) == ["Back", "Add channel", "\u2022 twitch:channel1"]
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._menu == "root"
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
    assert ctrl._menu == "channels"


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
    assert ctrl._menu == "channels"


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
    assert text == "Cancelled \u2014 nothing changed"
    # A second confirm message has a different nonce, so its cancel still works.
    text, markup = asyncio.run(ctrl.handle_callback("cancel:bbbb2222"))
    assert text == "Cancelled \u2014 nothing changed"
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
    assert ctrl._menu == "disk"


def test_callback_cancel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text, markup = asyncio.run(ctrl.handle_callback("cancel:deadbeef"))
    assert text == "Cancelled \u2014 nothing changed"
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
        raise RuntimeError("boom")

    ctrl.handle_callback = boom
    update = _FakeUpdate(12345)
    ctx = _FakeContext()
    update.callback_query.data = "confirm_remove:twitch:channel1:deadbeef"
    asyncio.run(ctrl._on_callback(update, ctx))
    assert update.callback_query.answers == [None]
    assert update.callback_query.edits == ["\u274c Unexpected error \u2014 see logs"]
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
    def fake_exec(*args, **kwargs):
        return _FakeProc(returncode=1, stderr=b"failed to connect to local tailscaled")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "failed to connect to local tailscaled" in hint


def test_tailscale_webhook_url_enables_funnel(tmp_path, monkeypatch):
    calls = []

    def fake_exec(*args, **kwargs):
        calls.append(args)
        if args[1] == "status":
            return _FakeProc(stdout=_status_json())
        return _FakeProc(stdout=b"Funnel already enabled\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url == "https://box.tail1234.ts.net/kick/webhook"
    assert hint is None
    assert calls == [
        ("tailscale", "status", "--json"),
        ("tailscale", "funnel", "--bg", "--yes", "8787"),
    ]


def test_tailscale_webhook_url_funnel_failure(tmp_path, monkeypatch):
    def fake_exec(*args, **kwargs):
        if args[1] == "status":
            return _FakeProc(stdout=_status_json())
        return _FakeProc(returncode=1, stderr=b"Funnel requires HTTPS certificates enabled")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "tailscale funnel 8787 failed" in hint
    assert "HTTPS certificates" in hint


def test_tailscale_webhook_url_funnel_already_enabled(tmp_path, monkeypatch):
    calls = []
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

    def fake_exec(*args, **kwargs):
        calls.append(args)
        if args[1] == "status":
            return _FakeProc(stdout=_status_json())
        if args[1] == "funnel":
            return _FakeProc(
                returncode=1,
                stderr=b"sending serve config: updating config: listener already exists for port 443",
            )
        return _FakeProc(stdout=serve_json)  # serve status --json

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url == "https://box.tail1234.ts.net/kick/webhook"
    assert hint is None
    assert calls == [
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

    def fake_exec(*args, **kwargs):
        if args[1] == "status":
            return _FakeProc(stdout=_status_json())
        if args[1] == "funnel":
            return _FakeProc(returncode=1, stderr=b"listener already exists for port 443")
        return _FakeProc(stdout=serve_json)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "tailscale funnel 8787 failed" in hint


def test_tailscale_webhook_url_funnel_timeout_kills_proc(tmp_path, monkeypatch):
    monkeypatch.setattr("stream_archive.telegram.commands_webhook._TAILSCALE_FUNNEL_TIMEOUT", 0.01)
    procs = []

    def fake_exec(*args, **kwargs):
        if args[1] == "status":
            return _FakeProc(stdout=_status_json())
        p = _FakeProc(hang=True)
        procs.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "timed out" in hint
    assert procs[0].killed


def test_tailscale_webhook_url_no_dns_name(tmp_path, monkeypatch):
    def fake_exec(*args, **kwargs):
        return _FakeProc(stdout=json.dumps({"Self": {}}).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._tailscale_webhook_url())

    assert url is None
    assert "no machine DNS name" in hint


def test_tailscale_funnel_off_uses_documented_syntax(tmp_path, monkeypatch):
    calls = []

    def fake_exec(*args, **kwargs):
        calls.append(args)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    assert asyncio.run(ctrl._tailscale_funnel_off()) is True
    assert calls == [("tailscale", "funnel", "--https=443", "off")]


class _LineStream:
    def __init__(self, lines, hang=False):
        self._lines = list(lines)
        self.hang = hang

    async def readline(self):
        if self.hang:
            await asyncio.sleep(3600)
        return self._lines.pop(0) if self._lines else b""


class _CloudflaredFakeProc:
    def __init__(self, lines, returncode=0, hang=False):
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
    def fake_exec(*args, **kwargs):
        assert args[:3] == ("cloudflared", "--no-autoupdate", "tunnel")
        assert "--url" in args
        return _CloudflaredFakeProc(
            lines=[
                b"2026-08-14T00:00:00Z INF +-----------------------------+\n",
                b"INF |  https://abc123.trycloudflare.com  |\n",
                b"INF +-----------------------------+\n",
            ]
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._cloudflared_quick_start())

    assert url == "https://abc123.trycloudflare.com/kick/webhook"
    assert hint is None
    assert ctrl._cloudflared is not None


def test_cloudflared_quick_start_exit_reports_output(tmp_path, monkeypatch):
    def fake_exec(*args, **kwargs):
        return _CloudflaredFakeProc(lines=[b"error: failed to connect\n"], returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    url, hint = asyncio.run(ctrl._cloudflared_quick_start())

    assert url is None
    assert "exited before publishing a URL" in hint
    assert "failed to connect" in hint


def test_cloudflared_quick_start_timeout_kills_proc(tmp_path, monkeypatch):
    monkeypatch.setattr("stream_archive.telegram.commands_webhook._CLOUDFLARED_QUICK_TIMEOUT", 0.01)
    proc = _CloudflaredFakeProc(lines=[], hang=True)

    def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
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
    def fake_exec(*args, **kwargs):
        assert args[:4] == ("cloudflared", "tunnel", "--no-autoupdate", "run")
        assert "--token" in args
        return _CloudflaredFakeProc(
            lines=[
                b"INF Registered tunnel connection connIndex=0\n",
            ]
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    ok, hint = asyncio.run(ctrl._cloudflared_named_start("tok"))

    assert ok is True
    assert hint is None
    assert ctrl._cloudflared is not None


def test_cloudflared_named_start_failure_reports_output(tmp_path, monkeypatch):
    def fake_exec(*args, **kwargs):
        return _CloudflaredFakeProc(
            lines=[
                b"ERR failed to register tunnel connection: invalid token\n",
            ],
            returncode=1,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    ok, hint = asyncio.run(ctrl._cloudflared_named_start("bad"))

    assert ok is False
    assert "invalid token" in hint
    assert ctrl._cloudflared is None


def test_cloudflared_named_start_timeout_alive_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("stream_archive.telegram.commands_webhook._CLOUDFLARED_RUN_TIMEOUT", 0.01)
    proc = _CloudflaredFakeProc(lines=[], hang=True, returncode=None)

    def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    ok, hint = asyncio.run(ctrl._cloudflared_named_start("tok"))

    assert ok is True
    assert ctrl._cloudflared is proc


def test_cloudflared_token_and_url_helpers():
    from stream_archive.telegram.commands_webhook import (
        _normalize_webhook_url,
        _valid_cloudflare_token,
    )

    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun", "s": "sec"}).encode()).decode()
    assert token.endswith("=")
    assert _valid_cloudflare_token(token)
    assert _valid_cloudflare_token(token.rstrip("="))  # unpadded still decodes
    assert not _valid_cloudflare_token("nope")
    assert not _valid_cloudflare_token(base64.b64encode(b"not json").decode())
    assert not _valid_cloudflare_token(base64.b64encode(json.dumps({"a": "acct"}).encode()).decode())  # missing t/s
    assert _normalize_webhook_url("https://x.example.com") == "https://x.example.com/kick/webhook"
    assert _normalize_webhook_url("https://x.example.com/") == "https://x.example.com/kick/webhook"
    assert _normalize_webhook_url("https://x.example.com/kick/webhook") == "https://x.example.com/kick/webhook"
    assert _normalize_webhook_url("https://x.example.com/custom") == "https://x.example.com/custom"


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
    text, markup = asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    assert "Kick webhook: off" in text
    assert kb_labels(markup) == KICK_WEBHOOK_LABELS
    assert ctrl._menu == "kick_webhook"


def test_reply_text_kick_webhook_cloudflare_prompt(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    assert "Quick tunnel" in text
    assert "Named tunnel" in text
    assert kb_labels(markup) == KICK_CLOUDFLARE_LABELS
    assert ctrl._menu == "kick_cloudflare"


def test_reply_text_kick_webhook_url_applies(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("https://tunnel.trycloudflare.com/kick/webhook"))
    assert "Kick webhook enabled" in text
    assert "https://tunnel.trycloudflare.com/kick/webhook" in text
    assert "Settings \u2192 Developer \u2192 your app \u2192 Enable webhooks" in text
    assert "developer dashboard" not in text
    assert "URL is reachable" in text  # automatic probe, no button
    w = read_file(tmp_path)["kick"]["webhook"]
    assert w["enabled"] is True
    assert w["public_url"] == "https://tunnel.trycloudflare.com/kick/webhook"
    assert w["tunnel"] == "cloudflare"
    assert w["cloudflare_managed"] is False
    assert kb_labels(markup) == KICK_WEBHOOK_LABELS
    assert ctrl._kick_webhook.started == [1]
    assert ctrl._kick_webhook.synced == [["twitch:channel1"]]
    assert ctrl._menu == "kick_webhook"


def test_reply_text_kick_webhook_enable_rearms_setup_notification(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    config.kick.webhook.setup_notified = True  # already confirmed once before
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("https://tunnel.trycloudflare.com/kick/webhook"))
    w = read_file(tmp_path)["kick"]["webhook"]
    assert w["setup_notified"] is False  # re-armed: first event will confirm again


def test_reply_text_kick_webhook_url_normalizes_root_path(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("https://tunnel.trycloudflare.com"))
    assert "https://tunnel.trycloudflare.com/kick/webhook" in text
    assert read_file(tmp_path)["kick"]["webhook"]["public_url"] == "https://tunnel.trycloudflare.com/kick/webhook"
    assert ctrl._menu == "kick_webhook"


def test_reply_text_kick_webhook_quick_tunnel_enables(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)

    async def fake_quick():
        return "https://abc123.trycloudflare.com/kick/webhook", None

    ctrl._cloudflared_quick_start = fake_quick
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Quick tunnel"))
    assert "Kick webhook enabled" in text
    assert "https://abc123.trycloudflare.com/kick/webhook" in text
    assert "cloudflared quick tunnel is running" in text
    assert "URL is reachable" in text
    w = read_file(tmp_path)["kick"]["webhook"]
    assert w["enabled"] is True
    assert w["public_url"] == "https://abc123.trycloudflare.com/kick/webhook"
    assert w["tunnel"] == "cloudflare"
    assert w["cloudflare_managed"] is True
    assert w["cloudflare_token"] == ""
    assert ctrl._kick_webhook.started == [1]
    assert ctrl._menu == "kick_webhook"


def test_reply_text_kick_webhook_quick_tunnel_failure_stays(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    async def failing_quick():
        return None, "cloudflared is not installed in this container."

    ctrl._cloudflared_quick_start = failing_quick
    before = read_file(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Quick tunnel"))
    assert "cloudflared is not installed" in text
    assert ctrl._menu == "kick_cloudflare"
    assert kb_labels(markup) == KICK_CLOUDFLARE_LABELS
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.started == []


def test_reply_text_kick_webhook_named_token_prompt(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    assert "cloudflared service install" in text
    assert kb_labels(markup) == KICK_TOKEN_LABELS
    assert ctrl._menu == "kick_cloudflare_token"


def test_reply_text_kick_webhook_named_token_accepted(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()
    started = []

    async def fake_named(tok, config_path=None):
        started.append((tok, config_path))

    ctrl._cloudflared_named_start = fake_named
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text(f"cloudflared.exe service install {token}"))
    assert "Tunnel token accepted" in text
    assert "kick.example.com" in text
    assert started == []  # cloudflared starts only after the hostname is known
    assert read_file(tmp_path)["kick"]["webhook"]["cloudflare_token"] == token
    assert read_file(tmp_path)["kick"]["webhook"]["enabled"] is False
    assert kb_labels(markup) == KICK_TOKEN_LABELS
    assert ctrl._menu == "kick_cloudflare_hostname"


def test_reply_text_kick_webhook_named_token_invalid_stays(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    text, markup = asyncio.run(ctrl.handle_reply_text("cloudflared service install nope"))
    assert "That doesn't look like a cloudflared tunnel token" in text
    assert ctrl._menu == "kick_cloudflare_token"
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.started == []


def test_reply_text_kick_webhook_named_hostname_invalid_stays(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    asyncio.run(ctrl.handle_reply_text(token))
    before = read_file(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("nope"))
    assert "doesn't look like a public hostname" in text
    assert ctrl._menu == "kick_cloudflare_hostname"
    assert read_file(tmp_path) == before


def test_reply_text_kick_webhook_named_flow_skip_dns(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()
    started = []

    async def fake_named(tok, config_path=None):
        started.append((tok, str(config_path)))
        return True, None

    ctrl._cloudflared_named_start = fake_named
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    asyncio.run(ctrl.handle_reply_text(token))
    text, markup = asyncio.run(ctrl.handle_reply_text("kick.example.com"))
    assert "kick.example.com" in text
    assert kb_labels(markup) == ["Skip", "Back"]
    assert ctrl._menu == "kick_cloudflare_dns"
    text, markup = asyncio.run(ctrl.handle_reply_text("skip"))
    assert "Kick webhook enabled" in text
    assert "https://kick.example.com/kick/webhook" in text
    assert "CNAME kick.example.com \u2192 tun-id.cfargotunnel.com" in text
    w = read_file(tmp_path)["kick"]["webhook"]
    assert w["enabled"] is True
    assert w["public_url"] == "https://kick.example.com/kick/webhook"
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
    assert ctrl._menu == "kick_webhook"


def test_reply_text_kick_webhook_named_flow_with_api_token(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()

    async def fake_named(tok, config_path=None):
        return True, None

    async def fake_dns(api_token):
        return True, "\u2705 DNS record created \u2014 the hostname now points at your tunnel."

    ctrl._cloudflared_named_start = fake_named
    ctrl._create_cloudflare_dns = fake_dns
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    asyncio.run(ctrl.handle_reply_text(token))
    asyncio.run(ctrl.handle_reply_text("kick.example.com"))
    text, markup = asyncio.run(ctrl.handle_reply_text("api-token-123"))
    assert "Kick webhook enabled" in text
    assert "DNS record created" in text
    assert "CNAME kick.example.com" not in text  # no manual step needed
    assert read_file(tmp_path)["kick"]["webhook"]["public_url"] == "https://kick.example.com/kick/webhook"
    assert ctrl._menu == "kick_webhook"


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
        if url == "/user/tokens/verify":
            if self.verify_status == "account-owned":
                return _FakeCfResp(401, {"success": False, "errors": [{"code": 1000, "message": "Invalid API Token"}]})
            return _FakeCfResp(200, {"result": {"status": self.verify_status}})
        if url.endswith("/tokens/verify"):
            return _FakeCfResp(200, {"result": {"status": "active"}})
        if url.startswith("/zones?"):
            return _FakeCfResp(200, {"result": self.zones})
        if "/dns_records?" in url:
            return _FakeCfResp(200, {"result": self.records})
        return _FakeCfResp(404, {})

    async def post(self, url, headers=None, json=None):
        self.calls.append(("post", url, json))
        return _FakeCfResp(200, {"result": json})


def make_cf_ctrl(tmp_path, monkeypatch, client):
    monkeypatch.setattr("stream_archive.telegram.commands_webhook.httpx.AsyncClient", lambda *a, **k: client)
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()
    config.kick.webhook.cloudflare_token = token
    ctrl._cloudflare_hostname = "kick.example.com"
    return config, ctrl, token


def test_create_cloudflare_dns_creates_record(tmp_path, monkeypatch):
    client = _FakeCfClient(zones=[{"id": "z1", "name": "example.com"}])
    config, ctrl, _ = make_cf_ctrl(tmp_path, monkeypatch, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is True
    assert "DNS record created" in message
    assert (
        "post",
        "/zones/z1/dns_records",
        {
            "type": "CNAME",
            "name": "kick.example.com",
            "content": "tun-id.cfargotunnel.com",
            "proxied": True,
        },
    ) in client.calls


def test_create_cloudflare_dns_picks_longest_zone_match(tmp_path, monkeypatch):
    client = _FakeCfClient(
        zones=[
            {"id": "z1", "name": "example.com"},
            {"id": "z2", "name": "sub.example.com"},
        ]
    )
    config, ctrl, _ = make_cf_ctrl(tmp_path, monkeypatch, client)
    ctrl._cloudflare_hostname = "kick.sub.example.com"

    ok, _ = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is True
    assert (
        "post",
        "/zones/z2/dns_records",
        {
            "type": "CNAME",
            "name": "kick.sub.example.com",
            "content": "tun-id.cfargotunnel.com",
            "proxied": True,
        },
    ) in client.calls


def test_create_cloudflare_dns_existing_same_target_ok(tmp_path, monkeypatch):
    client = _FakeCfClient(
        zones=[{"id": "z1", "name": "example.com"}],
        records=[{"content": "tun-id.cfargotunnel.com"}],
    )
    config, ctrl, _ = make_cf_ctrl(tmp_path, monkeypatch, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is True
    assert "already points" in message
    assert not any(c[0] == "post" for c in client.calls)


def test_create_cloudflare_dns_existing_other_target_fails(tmp_path, monkeypatch):
    client = _FakeCfClient(
        zones=[{"id": "z1", "name": "example.com"}],
        records=[{"content": "elsewhere.example.net"}],
    )
    config, ctrl, _ = make_cf_ctrl(tmp_path, monkeypatch, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is False
    assert "already used" in message


def test_create_cloudflare_dns_no_zone_fails(tmp_path, monkeypatch):
    client = _FakeCfClient(zones=[{"id": "z1", "name": "other.org"}])
    config, ctrl, _ = make_cf_ctrl(tmp_path, monkeypatch, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is False
    assert "No Cloudflare zone matches kick.example.com" in message


def test_create_cloudflare_dns_account_owned_token_fallback(tmp_path, monkeypatch):
    # cfat_ account tokens cannot pass /user/tokens/verify. The account-scoped
    # verify endpoint must be used instead.
    client = _FakeCfClient(zones=[{"id": "z1", "name": "example.com"}], verify_status="account-owned")
    config, ctrl, _ = make_cf_ctrl(tmp_path, monkeypatch, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("cfat_..."))

    assert ok is True
    assert ("get", "/accounts/acct/tokens/verify") in client.calls
    assert (
        "post",
        "/zones/z1/dns_records",
        {
            "type": "CNAME",
            "name": "kick.example.com",
            "content": "tun-id.cfargotunnel.com",
            "proxied": True,
        },
    ) in client.calls


def test_create_cloudflare_dns_invalid_token_fails(tmp_path, monkeypatch):
    client = _FakeCfClient(zones=[], verify_status="expired")
    config, ctrl, _ = make_cf_ctrl(tmp_path, monkeypatch, client)

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("bad"))

    assert ok is False
    assert "not valid" in message


def test_restore_named_tunnel_uses_local_config(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()
    config.kick.webhook.public_url = "https://kick.example.com/kick/webhook"
    config.kick.webhook.tunnel = "cloudflare"
    config.kick.webhook.cloudflare_token = token
    config.kick.webhook.cloudflare_managed = True
    config.kick.webhook.enabled = True
    started = []

    async def fake_named(tok, config_path=None):
        started.append((tok, str(config_path) if config_path else None))
        return True, None

    ctrl._cloudflared_named_start = fake_named
    asyncio.run(ctrl._restore_cloudflared())

    cfg_path = tmp_path / "cloudflared" / "tun-id.yml"
    assert started == [(token, str(cfg_path))]
    assert "hostname: kick.example.com" in cfg_path.read_text()


def test_reply_text_kick_webhook_named_dns_failure_stays(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    token = base64.b64encode(json.dumps({"a": "acct", "t": "tun-id", "s": "sec"}).encode()).decode()

    async def fake_named(tok, config_path=None):
        return True, None

    async def fake_dns(api_token):
        return False, "\u274c That Cloudflare API token is not valid."

    ctrl._cloudflared_named_start = fake_named
    ctrl._create_cloudflare_dns = fake_dns
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Named tunnel"))
    asyncio.run(ctrl.handle_reply_text(token))
    asyncio.run(ctrl.handle_reply_text("kick.example.com"))
    before = read_file(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("bad-token"))
    assert "not valid" in text
    assert ctrl._menu == "kick_cloudflare_dns"
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.started == []


def test_reply_text_kick_webhook_off(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Off"))
    assert "Kick webhook disabled" in text
    assert read_file(tmp_path)["kick"]["webhook"]["enabled"] is False
    assert ctrl._kick_webhook.closed == [1]
    assert kb_labels(markup) == ROOT_LABELS
    assert ctrl._menu == "root"


def test_reply_text_kick_webhook_tailscale_detected(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)

    async def fake_tailscale():
        return "https://box.tail1234.ts.net/kick/webhook", None

    ctrl._tailscale_webhook_url = fake_tailscale
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Tailscale funnel"))
    assert "https://box.tail1234.ts.net/kick/webhook" in text
    assert "tailscale funnel 8787 is enabled" in text
    # No reachability probe for tailscale. The funnel is verified against the
    # daemon, and the container cannot reach the host's tailnet IP (hairpin).
    assert "URL is reachable" not in text
    assert "doesn't respond yet" not in text
    assert read_file(tmp_path)["kick"]["webhook"]["enabled"] is True
    assert read_file(tmp_path)["kick"]["webhook"]["public_url"] == "https://box.tail1234.ts.net/kick/webhook"
    assert read_file(tmp_path)["kick"]["webhook"]["tunnel"] == "tailscale"
    assert ctrl._kick_webhook.started == [1]


def test_reply_text_kick_webhook_tailscale_fallback_to_input(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)

    async def no_tailscale():
        return None, "Tailscale is not installed in this container."

    ctrl._tailscale_webhook_url = no_tailscale
    before = read_file(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Tailscale funnel"))
    assert "Tailscale is not installed" in text
    assert "Cloudflare tunnel instead" in text
    assert ctrl._menu == "kick_cloudflare"
    assert kb_labels(markup) == KICK_CLOUDFLARE_LABELS
    assert read_file(tmp_path) == before
    assert ctrl._kick_webhook.started == []


def test_switch_tailscale_to_cloudflare_tears_down_funnel(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    probe_ok(ctrl)
    config.kick.webhook.public_url = "https://box.tail1234.ts.net/kick/webhook"
    config.kick.webhook.tunnel = "tailscale"
    config.kick.webhook.enabled = True
    funnel_off_calls = []

    async def fake_quick():
        return "https://abc123.trycloudflare.com", None

    async def fake_funnel_off():
        funnel_off_calls.append(1)
        return True

    ctrl._cloudflared_quick_start = fake_quick
    ctrl._tailscale_funnel_off = fake_funnel_off
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Cloudflare tunnel"))
    asyncio.run(ctrl.handle_reply_text("Quick tunnel"))
    assert funnel_off_calls == [1]
    w = read_file(tmp_path)["kick"]["webhook"]
    assert w["tunnel"] == "cloudflare"
    assert w["enabled"] is True


def test_switch_cloudflare_to_tailscale_stops_cloudflared(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.kick.webhook.public_url = "https://abc123.trycloudflare.com/kick/webhook"
    config.kick.webhook.tunnel = "cloudflare"
    config.kick.webhook.cloudflare_token = ""
    config.kick.webhook.cloudflare_managed = True
    config.kick.webhook.enabled = True
    stopped = []

    async def fake_tailscale():
        return "https://box.tail1234.ts.net/kick/webhook", None

    ctrl._tailscale_webhook_url = fake_tailscale
    ctrl._cloudflared_stop = lambda: stopped.append(1)
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Tailscale funnel"))
    assert stopped == [1]
    assert "cloudflared tunnel has been stopped" in text
    w = read_file(tmp_path)["kick"]["webhook"]
    assert w["tunnel"] == "tailscale"
    assert w["cloudflare_token"] == ""
    assert w["cloudflare_managed"] is False


def test_off_tears_down_tailscale_funnel(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.kick.webhook.public_url = "https://box.tail1234.ts.net/kick/webhook"
    config.kick.webhook.tunnel = "tailscale"
    config.kick.webhook.enabled = True
    funnel_off_calls = []

    async def fake_funnel_off():
        funnel_off_calls.append(1)
        return True

    ctrl._tailscale_funnel_off = fake_funnel_off
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Off"))
    assert funnel_off_calls == [1]
    w = read_file(tmp_path)["kick"]["webhook"]
    assert w["enabled"] is False
    assert w["tunnel"] == ""


def test_off_tears_down_cloudflared(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.kick.webhook.public_url = "https://abc123.trycloudflare.com/kick/webhook"
    config.kick.webhook.tunnel = "cloudflare"
    config.kick.webhook.cloudflare_managed = True
    config.kick.webhook.enabled = True
    stopped = []
    ctrl._cloudflared_stop = lambda: stopped.append(1)
    asyncio.run(ctrl.handle_reply_text("Kick webhook"))
    asyncio.run(ctrl.handle_reply_text("Off"))
    assert stopped == [1]
    w = read_file(tmp_path)["kick"]["webhook"]
    assert w["enabled"] is False
    assert w["tunnel"] == ""
    assert w["cloudflare_token"] == ""
    assert w["cloudflare_managed"] is False


def test_restore_quick_tunnel_new_url_rearms_confirmation(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.kick.webhook.public_url = "https://old.trycloudflare.com/kick/webhook"
    config.kick.webhook.tunnel = "cloudflare"
    config.kick.webhook.cloudflare_managed = True
    config.kick.webhook.setup_notified = True  # confirmed before the restart
    config.kick.webhook.enabled = True
    sent = []

    async def fake_quick():
        return "https://new.trycloudflare.com/kick/webhook", None

    async def fake_send(text):
        sent.append(text)

    ctrl._cloudflared_quick_start = fake_quick
    ctrl._send_admin = fake_send
    probe_ok(ctrl)
    asyncio.run(ctrl._restore_cloudflared())
    w = read_file(tmp_path)["kick"]["webhook"]
    assert w["public_url"] == "https://new.trycloudflare.com/kick/webhook"
    assert w["setup_notified"] is False  # re-armed: first event confirms again
    assert len(sent) == 1
    assert "new temporary URL" in sent[0]
    assert "URL is reachable" in sent[0]


def test_restore_quick_tunnel_same_url_stays_silent(tmp_path, monkeypatch):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.kick.webhook.public_url = "https://same.trycloudflare.com/kick/webhook"
    config.kick.webhook.tunnel = "cloudflare"
    config.kick.webhook.cloudflare_managed = True
    config.kick.webhook.setup_notified = True
    config.kick.webhook.enabled = True
    sent = []

    async def fake_quick():
        return "https://same.trycloudflare.com/kick/webhook", None

    async def fake_send(text):
        sent.append(text)

    ctrl._cloudflared_quick_start = fake_quick
    ctrl._send_admin = fake_send
    before = read_file(tmp_path)
    asyncio.run(ctrl._restore_cloudflared())
    assert config.kick.webhook.setup_notified is True  # live config untouched
    assert read_file(tmp_path) == before
    assert sent == []


def test_menu_text_kick_webhook_shows_tunnel_mode(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.kick.webhook.public_url = "https://abc123.trycloudflare.com/kick/webhook"
    config.kick.webhook.tunnel = "cloudflare"
    config.kick.webhook.enabled = True
    text = asyncio.run(ctrl.menu_text("kick_webhook"))
    assert "Kick webhook: on (cloudflare \u00b7 https://abc123.trycloudflare.com/kick/webhook)" in text


def test_reply_text_channel_hold_menu(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._menu, ctrl._menu_channel = "channel", "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Hold delay"))
    assert "YouTube hold delay for twitch:channel1" in text
    assert "Global default: 0s" in text
    assert kb_labels(markup) == ["0 (off)", "30s", "60s", "120s", "300s", "600s", "Default", "Custom", "Back"]
    assert ctrl._menu == "channel_hold"


def test_reply_text_channel_hold_set_preset(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._menu, ctrl._menu_channel = "channel", "twitch:channel1"
    asyncio.run(ctrl.handle_reply_text("Hold delay"))
    text, markup = asyncio.run(ctrl.handle_reply_text("60s"))
    assert "Hold delay for twitch:channel1 set to 60s" in text
    assert read_file(tmp_path)["channel_youtube_hold_seconds"] == {"twitch:channel1": 60}
    assert config.channel_youtube_hold_seconds == {"twitch:channel1": 60.0}
    assert ctrl._menu == "channel"
    assert ctrl._menu_channel == "twitch:channel1"


def test_reply_text_channel_hold_default_resets(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    config.channel_youtube_hold_seconds = {"twitch:channel1": 60}
    ctrl._menu, ctrl._menu_channel = "channel_hold", "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Default"))
    assert "reset to global" in text
    assert read_file(tmp_path)["channel_youtube_hold_seconds"] == {}
    assert ctrl._menu == "channel"
    assert ctrl._menu_channel == "twitch:channel1"


def test_reply_text_channel_hold_custom(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._menu, ctrl._menu_channel = "channel", "twitch:channel1"
    asyncio.run(ctrl.handle_reply_text("Hold delay"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Custom"))
    assert "Hold delay for twitch:channel1" in text
    assert ctrl._menu == "custom"
    assert ctrl._custom_setting == "channel_hold"
    assert ctrl._menu_channel == "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("90"))
    assert read_file(tmp_path)["channel_youtube_hold_seconds"] == {"twitch:channel1": 90}
    assert ctrl._menu == "channel"
    assert ctrl._menu_channel == "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._menu == "channels"


def test_back_from_channel_hold_to_channel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl._menu, ctrl._menu_channel = "channel_hold", "twitch:channel1"
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._menu == "channel"
    assert ctrl._menu_channel == "twitch:channel1"
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


def test_create_cloudflare_dns_html_verify_body_reports_invalid(tmp_path, monkeypatch):
    """A proxy 502 HTML page on token verify returns (False, message), with a
    'not valid' message, and never escapes as a JSONDecodeError during the
    setup flow."""

    class _HtmlVerifyResp:
        status_code = 200
        text = "<html><body>502 Bad Gateway</body></html>"

        def json(self):
            raise json.JSONDecodeError("Expecting value", self.text, 0)

    class _HtmlCfClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return _HtmlVerifyResp()

    config, ctrl, _ = make_cf_ctrl(tmp_path, monkeypatch, _HtmlCfClient())

    ok, message = asyncio.run(ctrl._create_cloudflare_dns("apitok"))

    assert ok is False
    assert "not valid" in message
