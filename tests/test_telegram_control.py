import asyncio
import json
import threading

from src.stream_archive.config import _validate
from src.stream_archive.telegram_control import TelegramController


class FakeRecorder:
    def __init__(self, active=(), recording=()):
        self._active = list(active)
        self._recording = set(recording)
        self.stop_calls = []
        self.chat_stop_calls = []
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

    def active_channels(self):
        return sorted(self._recording)

    async def stop_chat(self, channel):
        self.chat_stop_calls.append(channel)

    async def disk_snapshot(self):
        return self.snapshot

    def recording_info(self):
        return [
            {"channel": ch, "mode": "disk", "duration_s": 0, "size_mb": None}
            for ch in self._active
        ]


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


class FakeUpdater:
    def __init__(self, report, results=None):
        self.report = report
        self.results = results
        self.check_calls = []
        self.apply_calls = []

    async def check(self, notify):
        self.check_calls.append(notify)
        return self.report

    async def apply(self, report):
        self.apply_calls.append(report)
        return self.results


def base_config(tmp_path):
    return {
        "telegram_user_id": 12345,
        "bot_telegram_api": "bot_token",
        "twitch_client_id": "client_id",
        "twitch_client_secret": "client_secret",
        "channels": ["channel1"],
        "proxy_list": ["httpproxy://user:pass@host:port"],
        "monitoring_interval": 60,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "recording_dir": "recordings",
    }


def make_controller(tmp_path, channels=None, recording=(), active=(), on_restart=None):
    """Mirror get_config(): validate (applies defaults), write file, add internal keys."""
    config = base_config(tmp_path)
    if channels is not None:
        config["channels"] = channels
    _validate(config)
    (tmp_path / "config.json").write_text(json.dumps(config, indent=4))
    config["_workdir"] = tmp_path
    config["_config_path"] = tmp_path / "config.json"
    recorder = FakeRecorder(active=active, recording=recording)
    monitor = FakeMonitor()
    eventsub = FakeEventSub()
    ctrl = TelegramController(config, recorder, monitor, eventsub, on_restart=on_restart)
    return config, ctrl, recorder, monitor, eventsub


def read_file(tmp_path):
    return json.loads((tmp_path / "config.json").read_text())


def test_status_contains_settings_and_omits_secrets(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, active=["channel1"])
    text = asyncio.run(ctrl.handle_status())
    assert "channel1" in text
    assert "Output mode: disk" in text
    assert "Retention: disabled" in text
    assert "Monitoring interval" not in text
    assert "Recording now: channel1" in text
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
    text = asyncio.run(ctrl.handle_add(["newch"]))
    assert text.startswith("Added")
    assert "newch" in text
    assert "newch" in read_file(tmp_path)["channels"]
    assert "newch" in config["channels"]


def test_add_duplicate_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_add(["newch"]))
    before = read_file(tmp_path)
    text = asyncio.run(ctrl.handle_add(["newch"]))
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
        tmp_path, channels=["channel1", "ch"], recording=["ch"]
    )
    text = asyncio.run(ctrl.handle_remove(["ch"]))
    assert text.startswith("Removed")
    assert "ch" in text
    assert recorder.stop_calls == ["ch"]
    assert monitor.remove_calls == ["ch"]
    assert "ch" not in read_file(tmp_path)["channels"]
    assert "ch" not in config["channels"]


def test_remove_not_recording_does_not_stop(tmp_path):
    config, ctrl, recorder, monitor, eventsub = make_controller(
        tmp_path, channels=["channel1", "ch"]
    )
    text = asyncio.run(ctrl.handle_remove(["ch"]))
    assert text.startswith("Removed")
    assert recorder.stop_calls == []
    assert monitor.remove_calls == []
    assert "ch" not in read_file(tmp_path)["channels"]


def test_remove_unknown_channel_rejected(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, channels=["channel1"])
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
    assert config["retention_days"] == 7


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
    assert config["output_mode"] == "youtube"


def test_mode_invalid_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["cloud"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_mode_per_channel_sets_override(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_mode(["channel1", "youtube"])
    assert text == "Output mode for channel1 set to youtube"
    assert read_file(tmp_path)["channel_output_modes"] == {"channel1": "youtube"}
    assert config["channel_output_modes"] == {"channel1": "youtube"}


def test_mode_per_channel_reset(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_mode(["channel1", "youtube"])
    text = ctrl.handle_mode(["channel1", "default"])
    assert text == "Output mode for channel1 reset to global (disk)"
    assert read_file(tmp_path)["channel_output_modes"] == {}
    assert config["output_mode"] == "disk"


def test_mode_per_channel_invalid_mode_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["channel1", "cloud"])
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
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["channel1", "ch"])
    ctrl.handle_mode(["channel1", "youtube"])
    asyncio.run(ctrl.handle_remove(["channel1"]))
    assert read_file(tmp_path)["channel_output_modes"] == {}
    assert "channel1" not in read_file(tmp_path)["channels"]


def test_status_shows_per_channel_modes(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_mode(["channel1", "youtube"])
    assert "Per-channel output: channel1 \u2192 youtube" in asyncio.run(ctrl.handle_status())


def test_reload_picks_up_disk_edits(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    file_config = read_file(tmp_path)
    file_config["channels"].append("hand_edit")
    file_config["retention_days"] = 3
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))
    text = asyncio.run(ctrl.handle_reload())
    assert text == "\u2705 Config reloaded from config.json"
    assert "hand_edit" in config["channels"]
    assert config["retention_days"] == 3


def test_reload_corrupt_file_leaves_live_unchanged(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    (tmp_path / "config.json").write_text("{ not json")
    text = asyncio.run(ctrl.handle_reload())
    assert text.startswith("\u274c")
    assert config["channels"] == ["channel1"]


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


UPDATE_REPORT = {
    "app": {"status": "update", "behind": 2, "subject": "Fix retention"},
    "plugin": {"status": "update", "current": "8.3.0-20260701", "latest": "9.0.0-20260801", "digest": None},
    "streamlink": {"status": "update", "current": "8.4.0", "latest": "8.5.0"},
}

UPDATE_RESULTS = {
    "app": ("applied", "pulled 2 commit(s) — Fix retention"),
    "plugin": ("applied", "plugins/twitch.py replaced (8.3.0-20260701 → 9.0.0-20260801)"),
    "streamlink": ("applied", "uv.lock updated — now active"),
}


def test_update_applies_and_schedules_restart(tmp_path):
    flag = threading.Event()
    _, ctrl, _, _, eventsub = make_controller(tmp_path, on_restart=flag.set)
    fake = FakeUpdater(UPDATE_REPORT, UPDATE_RESULTS)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "\U0001f504 Updates applied" in text
        assert 'stream-archive: pulled 2 commit(s) — "Fix retention"' in text
        assert "streamlink: 8.4.0 → 8.5.0 (uv.lock updated — now active)" in text
        assert "streamlink-ttvlol: 8.3.0-20260701 → 9.0.0-20260801 (plugins/twitch.py replaced)" in text
        assert "Run on the host:" not in text
        assert "Restarting the service..." in text
        await asyncio.sleep(0.6)
        assert flag.is_set()

    asyncio.run(scenario())
    assert fake.check_calls == [False]
    assert fake.apply_calls == [UPDATE_REPORT]


def test_update_no_updates_no_restart(tmp_path):
    flag = threading.Event()
    _, ctrl, _, _, eventsub = make_controller(tmp_path, on_restart=flag.set)
    report = {
        "app": {"status": "up_to_date", "local": "abc1234def5678", "remote": "abc1234def5678", "behind": 0, "subject": "Latest"},
        "streamlink": {"status": "up_to_date", "current": "8.4.0", "latest": "8.4.0"},
        "plugin": {"status": "up_to_date", "current": "8.3.0-20260701", "latest": "8.3.0-20260701"},
    }
    fake = FakeUpdater(report)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "\u2705 Up to date" in text
        assert "stream-archive: abc1234 (main)" in text
        assert "streamlink: 8.4.0" in text
        assert "streamlink-ttvlol: 8.3.0-20260701" in text
        await asyncio.sleep(0.6)
        assert not flag.is_set()

    asyncio.run(scenario())
    assert fake.apply_calls == []


def test_update_apply_failure_no_restart(tmp_path):
    flag = threading.Event()
    _, ctrl, _, _, eventsub = make_controller(tmp_path, on_restart=flag.set)
    results = {
        "app": ("failed", "git pull: fatal: Could not resolve host"),
        "plugin": ("failed", "sha256 mismatch — download rejected"),
        "streamlink": ("failed", "uv lock: command not found"),
    }
    fake = FakeUpdater(UPDATE_REPORT, results)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "\u274c Update failed" in text
        assert "• stream-archive: git pull: fatal: Could not resolve host" in text
        assert "• streamlink-ttvlol: sha256 mismatch — download rejected" in text
        assert "No restart triggered." in text
        await asyncio.sleep(0.6)
        assert not flag.is_set()

    asyncio.run(scenario())


DOCKER_LOCK_ONLY_REPORT = {
    "app": {"status": "up_to_date", "local": "abc1234", "remote": "abc1234", "behind": 0, "subject": None},
    "plugin": {"status": "up_to_date", "current": "8.3.0-20260701", "latest": "8.3.0-20260701"},
    "streamlink": {"status": "update", "current": "8.4.0", "latest": "8.5.0"},
}


def test_update_docker_streamlink_lock_requires_rebuild(tmp_path):
    flag = threading.Event()
    _, ctrl, _, _, eventsub = make_controller(tmp_path, on_restart=flag.set)
    results = {
        "app": ("skipped", "no update available"),
        "plugin": ("skipped", "no update available"),
        "streamlink": ("applied_rebuild", "uv.lock updated — rebuild required"),
    }
    fake = FakeUpdater(DOCKER_LOCK_ONLY_REPORT, results)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "\U0001f504 Updates applied" in text
        assert "• streamlink: 8.4.0 → 8.5.0 (uv.lock updated — rebuild required)" in text
        assert "Run on the host:" in text
        assert "docker compose up -d --build" in text
        assert "Restarting the service..." not in text
        await asyncio.sleep(0.6)
        assert not flag.is_set()

    asyncio.run(scenario())


def test_update_docker_app_and_streamlink_restarts_and_rebuilds(tmp_path):
    flag = threading.Event()
    _, ctrl, _, _, eventsub = make_controller(tmp_path, on_restart=flag.set)
    results = {
        "app": ("applied", "pulled 2 commit(s) — Fix retention"),
        "plugin": ("skipped", "no update available"),
        "streamlink": ("applied_rebuild", "uv.lock updated — rebuild required"),
    }
    fake = FakeUpdater(DOCKER_LOCK_ONLY_REPORT, results)
    ctrl._updater = fake

    async def scenario():
        text = await ctrl.handle_update()
        assert "Restarting the service..." in text
        assert "docker compose up -d --build" in text
        assert "• streamlink: 8.4.0 → 8.5.0 (uv.lock updated — rebuild required)" in text
        await asyncio.sleep(0.6)
        assert flag.is_set()

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
    assert config["preferred_quality"] == "720p"
    assert ctrl.handle_quality(["a", "b"]) == "Usage: /quality <best|1080p|720p|...>"


def test_quality_empty_rejected(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_quality([""])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_maxrecordings_show_set_invalid(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert "Max recordings: 0 (0 = unlimited)" in ctrl.handle_maxrecordings([])
    text = ctrl.handle_maxrecordings(["3"])
    assert text == "Max recordings set to 3"
    assert read_file(tmp_path)["max_concurrent_recordings"] == 3
    assert config["max_concurrent_recordings"] == 3
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
    assert config["max_concurrent_youtube_streams"] == 2
    before = read_file(tmp_path)
    assert ctrl.handle_maxyoutube(["x"]).startswith("\u274c")
    assert read_file(tmp_path) == before
    assert ctrl.handle_maxyoutube(["1", "2"]) == "Usage: /maxyoutube <n> (0 = unlimited)"


def test_disk_subcommands(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_disk(["maxsize", "20"])
    assert text == "Disk max total set to 20 GB"
    assert read_file(tmp_path)["disk"]["max_total_gb"] == 20
    assert config["disk"]["max_total_gb"] == 20

    text = ctrl.handle_disk(["interval", "30"])
    assert text == "Disk check interval set to 30s"
    assert read_file(tmp_path)["disk"]["check_interval_s"] == 30

    text = ctrl.handle_disk(["delete_oldest", "off"])
    assert text == "Delete oldest disabled"
    assert read_file(tmp_path)["disk"]["delete_oldest"] is False
    text = ctrl.handle_disk(["delete_oldest", "on"])
    assert text == "Delete oldest enabled"
    assert read_file(tmp_path)["disk"]["delete_oldest"] is True

    assert ctrl.handle_disk(["bogus", "1"]) == "Usage: /disk <maxsize|interval|delete_oldest> <value>"
    before = read_file(tmp_path)
    assert ctrl.handle_disk(["maxsize", "x"]).startswith("\u274c")
    assert read_file(tmp_path) == before


def test_disk_show_block(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_disk([])
    assert "Disk limits:" in text
    assert "max total: 0 GB (0 = disabled, delete oldest: on)" in text
    assert "check every 60s" in text


def test_help_lists_new_commands(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = ctrl.handle_help()
    assert "/quality [value]" in text
    assert "/maxrecordings <n>" in text
    assert "/maxyoutube <n>" in text
    assert "/disk <maxsize|interval|delete_oldest> <value>" in text
    assert "/chat [on|off]" in text
    assert "/settings" in text
    assert "/start" in text


def test_chat_show_state(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert "Chat recording: enabled" in asyncio.run(ctrl.handle_chat([]))
    asyncio.run(ctrl.handle_chat(["off"]))
    assert "Chat recording: disabled" in asyncio.run(ctrl.handle_chat([]))
    assert "Chat recording: disabled" in asyncio.run(ctrl.handle_status())


def test_chat_on_persists(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["channel1"])
    asyncio.run(ctrl.handle_chat(["off"]))
    text = asyncio.run(ctrl.handle_chat(["on"]))
    assert text == "Chat recording enabled"
    assert read_file(tmp_path)["record_chat"] is True
    assert config["record_chat"] is True
    assert recorder.chat_stop_calls == ["channel1"]  # from the earlier /chat off, not re-triggered by on


def test_chat_off_persists_and_stops_inflight(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["channel1", "ch"])
    text = asyncio.run(ctrl.handle_chat(["off"]))
    assert text == "Chat recording disabled"
    assert read_file(tmp_path)["record_chat"] is False
    assert config["record_chat"] is False
    assert recorder.chat_stop_calls == ["ch", "channel1"]
    assert recorder.stop_calls == []


def test_chat_invalid_rejected(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["channel1"])
    before = read_file(tmp_path)
    assert asyncio.run(ctrl.handle_chat([])) == "Chat recording: enabled"
    assert asyncio.run(ctrl.handle_chat(["maybe"])) == "Usage: /chat <on|off>"
    assert read_file(tmp_path) == before
    assert recorder.chat_stop_calls == []


def test_add_calls_eventsub_add_channel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_add(["newch"]))
    assert text.startswith("Added")
    assert eventsub.added == ["newch"]


def test_add_rejected_does_not_call_eventsub(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_add(["newch"]))
    asyncio.run(ctrl.handle_add(["newch"]))
    assert eventsub.added == ["newch"]


def test_remove_calls_eventsub_remove_channel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["channel1", "ch"])
    asyncio.run(ctrl.handle_remove(["ch"]))
    assert eventsub.removed == ["ch"]


def test_remove_rejected_does_not_call_eventsub(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["channel1"])
    asyncio.run(ctrl.handle_remove(["nope"]))
    assert eventsub.removed == []


def test_reload_calls_eventsub_sync(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    file_config = read_file(tmp_path)
    file_config["channels"].append("hand_edit")
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))
    text = asyncio.run(ctrl.handle_reload())
    assert text == "\u2705 Config reloaded from config.json"
    assert eventsub.synced == [["channel1", "hand_edit"]]


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


ROOT_LABELS = ["Channels", "Status", "Chat recording", "Output mode",
               "Quality", "Retention", "Max recordings", "Max YouTube", "Disk"]
DISK_LABELS = ["Max total", "Check interval", "Delete oldest", "Back"]


def test_command_list_covers_all_handlers(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    commands = {c.command for c in ctrl.command_list()}
    assert commands >= {
        "start", "help", "status", "channels", "add", "remove", "retention",
        "mode", "reload", "restart", "update", "quality", "maxrecordings",
        "maxyoutube", "disk", "chat", "settings",
    }


def test_reply_keyboard_root_layout(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    d = ctrl.reply_keyboard("root").to_dict()
    assert d["keyboard"] == [
        [{"text": "Channels"}, {"text": "Status"}],
        [{"text": "Chat recording"}, {"text": "Output mode"}],
        [{"text": "Quality"}, {"text": "Retention"}],
        [{"text": "Max recordings"}, {"text": "Max YouTube"}],
        [{"text": "Disk"}],
    ]
    assert d["resize_keyboard"] is True


def test_reply_keyboard_channels_layout(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["channel1", "ch"])
    assert ctrl.reply_keyboard("channels").to_dict()["keyboard"] == [
        [{"text": "Add channel"}],
        [{"text": "\u2022 channel1"}],
        [{"text": "\u2022 ch"}],
        [{"text": "Back"}],
    ]


def test_reply_keyboard_channel_layout(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    assert ctrl.reply_keyboard("channel", "channel1").to_dict()["keyboard"] == [
        [{"text": "Delete channel"}],
        [{"text": "Mode: disk"}, {"text": "Mode: youtube"}],
        [{"text": "Mode: both"}, {"text": "Mode: default"}],
        [{"text": "Back"}],
    ]


def test_reply_text_navigates_to_channels(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    text, markup = asyncio.run(ctrl.handle_reply_text("Channels"))
    assert "Channels (1): channel1" in text
    assert kb_labels(markup) == ["Add channel", "\u2022 channel1", "Back"]
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
    text, markup = asyncio.run(ctrl.handle_reply_text("newch"))
    assert text.startswith("Added newch")
    assert eventsub.added == ["newch"]
    assert "newch" in config["channels"]
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
    text, markup = asyncio.run(ctrl.handle_reply_text("\u2022 channel1"))
    assert "Channel: channel1" in text
    assert "default (global: disk)" in text
    text, markup = asyncio.run(ctrl.handle_reply_text("Mode: youtube"))
    assert read_file(tmp_path)["channel_output_modes"] == {"channel1": "youtube"}
    assert config["channel_output_modes"] == {"channel1": "youtube"}
    assert ctrl._menu_channel == "channel1"


def test_reply_text_channel_delete_asks_confirm(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    asyncio.run(ctrl.handle_reply_text("\u2022 channel1"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Delete channel"))
    assert "Remove channel1 from monitoring?" in text
    assert kb_labels(markup) == ["Confirm", "Cancel"]
    assert read_file(tmp_path) == before
    assert ctrl._menu == "channel"


def test_reply_text_chat_quick(tmp_path):
    config, ctrl, recorder, _, eventsub = make_controller(tmp_path, recording=["channel1"])
    asyncio.run(ctrl.handle_reply_text("Chat recording"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Off"))
    assert read_file(tmp_path)["record_chat"] is False
    assert recorder.chat_stop_calls == ["channel1"]
    assert kb_labels(markup) == ROOT_LABELS
    assert ctrl._menu == "root"


def test_reply_text_mode_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Output mode"))
    text, markup = asyncio.run(ctrl.handle_reply_text("youtube"))
    assert read_file(tmp_path)["output_mode"] == "youtube"
    assert config["output_mode"] == "youtube"
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_quality_quick(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Quality"))
    text, markup = asyncio.run(ctrl.handle_reply_text("1080p"))
    assert read_file(tmp_path)["preferred_quality"] == "1080p"
    assert config["preferred_quality"] == "1080p"
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
    assert config["max_concurrent_recordings"] == 3
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_disk_quick_returns_disk_menu(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Disk"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Max total"))
    assert "Max total: 0 GB (0 = disabled)" in text
    text, markup = asyncio.run(ctrl.handle_reply_text("50"))
    assert read_file(tmp_path)["disk"]["max_total_gb"] == 50
    assert config["disk"]["max_total_gb"] == 50
    assert ctrl._menu == "disk"
    assert kb_labels(markup) == DISK_LABELS


def test_reply_text_disk_submenu_descriptions(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Disk"))
    cases = [
        ("Max total", "Limits total recording size"),
        ("Check interval", "How often the disk limits are checked"),
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
    assert config["disk"]["delete_oldest"] is False
    assert kb_labels(markup) == DISK_LABELS


def test_reply_text_back_navigation(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    asyncio.run(ctrl.handle_reply_text("Channels"))
    asyncio.run(ctrl.handle_reply_text("\u2022 channel1"))
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._menu == "channels"
    assert kb_labels(markup) == ["Add channel", "\u2022 channel1", "Back"]
    text, markup = asyncio.run(ctrl.handle_reply_text("Back"))
    assert ctrl._menu == "root"
    assert kb_labels(markup) == ROOT_LABELS


def test_reply_text_unknown_ignored(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    assert asyncio.run(ctrl.handle_reply_text("hello")) is None
    assert read_file(tmp_path) == before


def test_callback_confirm_remove_applies(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["channel1", "ch"])
    text, markup = asyncio.run(ctrl.handle_callback("confirm_remove:channel1"))
    assert text.startswith("Removed channel1")
    assert "channel1" not in config["channels"]
    assert "channel1" not in read_file(tmp_path)["channels"]
    assert eventsub.removed == ["channel1"]
    assert ctrl._menu == "channels"


def test_callback_confirm_remove_dedup(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["channel1", "ch"])
    asyncio.run(ctrl.handle_callback("confirm_remove:channel1"))
    assert asyncio.run(ctrl.handle_callback("confirm_remove:channel1")) is None
    assert eventsub.removed == ["channel1"]


def test_callback_confirm_delete_oldest_applies(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    ctrl.handle_disk(["delete_oldest", "off"])
    text, markup = asyncio.run(ctrl.handle_callback("confirm_delete_oldest:on"))
    assert read_file(tmp_path)["disk"]["delete_oldest"] is True
    assert config["disk"]["delete_oldest"] is True
    assert ctrl._menu == "disk"


def test_callback_cancel(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    before = read_file(tmp_path)
    text, markup = asyncio.run(ctrl.handle_callback("cancel"))
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
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["channel1", "ch"])
    update = _FakeUpdate(12345)
    ctx = _FakeContext()
    update.callback_query.data = "confirm_remove:channel1"
    asyncio.run(ctrl._on_callback(update, ctx))
    asyncio.run(ctrl._on_callback(update, ctx))
    assert update.callback_query.answers == [None, None]  # no "Already processed" toast
    assert len(update.callback_query.edits) == 1  # second tap does not re-edit
    assert "Removed channel1" in update.callback_query.edits[0]
    assert eventsub.removed == ["channel1"]
    assert len(ctx.bot.sent) == 1  # menu re-rendered once, after the first tap


def test_callback_error_surfaces_instead_of_silent_failure(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path, channels=["channel1"])
    async def boom(data):
        raise RuntimeError("boom")
    ctrl.handle_callback = boom
    update = _FakeUpdate(12345)
    ctx = _FakeContext()
    update.callback_query.data = "confirm_remove:channel1"
    asyncio.run(ctrl._on_callback(update, ctx))
    assert update.callback_query.answers == [None]
    assert update.callback_query.edits == ["\u274c Unexpected error \u2014 see logs"]
    assert ctx.bot.sent == []  # failed tap does not re-render the menu


def test_callback_unknown_data_silent(tmp_path):
    config, ctrl, _, _, eventsub = make_controller(tmp_path)
    update = _FakeUpdate(12345)
    ctx = _FakeContext()
    update.callback_query.data = "bogus:data"
    asyncio.run(ctrl._on_callback(update, ctx))
    assert update.callback_query.answers == [None]
    assert update.callback_query.edits == []
    assert ctx.bot.sent == []
