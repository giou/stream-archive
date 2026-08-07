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
    ctrl = TelegramController(config, recorder, monitor, on_restart=on_restart)
    return config, ctrl, recorder, monitor


def read_file(tmp_path):
    return json.loads((tmp_path / "config.json").read_text())


def test_status_contains_settings_and_omits_secrets(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path, active=["channel1"])
    text = asyncio.run(ctrl.handle_status())
    assert "channel1" in text
    assert "Output mode: disk" in text
    assert "Retention: disabled" in text
    assert "Monitoring interval: 60s" in text
    assert "Recording now: channel1" in text
    assert "Timezone" not in text
    assert "bot_token" not in text
    assert "client_secret" not in text
    assert "user:pass" not in text


def test_status_retention_days_and_singular(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    ctrl.handle_retention(["7"])
    assert "Retention: 7 days" in asyncio.run(ctrl.handle_status())
    ctrl.handle_retention(["1"])
    assert "Retention: 1 day" in asyncio.run(ctrl.handle_status())


def test_add_persists_and_updates_live(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    text = ctrl.handle_add(["newch"])
    assert text.startswith("Added")
    assert "newch" in text
    assert "newch" in read_file(tmp_path)["channels"]
    assert "newch" in config["channels"]


def test_add_duplicate_rejected(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    ctrl.handle_add(["newch"])
    before = read_file(tmp_path)
    text = ctrl.handle_add(["newch"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_add_invalid_name_rejected(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_add(["bad name!"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_add_usage_rejected(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    before = read_file(tmp_path)
    assert ctrl.handle_add([]) == "Usage: /add <channel>"
    assert ctrl.handle_add(["a", "b"]) == "Usage: /add <channel>"
    assert read_file(tmp_path) == before


def test_remove_stops_live_recording(tmp_path):
    config, ctrl, recorder, monitor = make_controller(
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
    config, ctrl, recorder, monitor = make_controller(
        tmp_path, channels=["channel1", "ch"]
    )
    text = asyncio.run(ctrl.handle_remove(["ch"]))
    assert text.startswith("Removed")
    assert recorder.stop_calls == []
    assert monitor.remove_calls == []
    assert "ch" not in read_file(tmp_path)["channels"]


def test_remove_unknown_channel_rejected(tmp_path):
    config, ctrl, recorder, _ = make_controller(tmp_path, channels=["channel1"])
    before = read_file(tmp_path)
    text = asyncio.run(ctrl.handle_remove(["nope"]))
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before
    assert recorder.stop_calls == []


def test_retention_set(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    text = ctrl.handle_retention(["7"])
    assert text == "Retention set to 7 day(s)"
    assert read_file(tmp_path)["retention_days"] == 7
    assert config["retention_days"] == 7


def test_retention_invalid_rejected(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    for arg in ["-1", "x"]:
        before = read_file(tmp_path)
        text = ctrl.handle_retention([arg])
        assert text.startswith("\u274c")
        assert read_file(tmp_path) == before


def test_mode_set(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    text = ctrl.handle_mode(["youtube"])
    assert text == "Output mode set to youtube"
    assert read_file(tmp_path)["output_mode"] == "youtube"
    assert config["output_mode"] == "youtube"


def test_mode_invalid_rejected(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["cloud"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_mode_per_channel_sets_override(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    text = ctrl.handle_mode(["channel1", "youtube"])
    assert text == "Output mode for channel1 set to youtube"
    assert read_file(tmp_path)["channel_output_modes"] == {"channel1": "youtube"}
    assert config["channel_output_modes"] == {"channel1": "youtube"}


def test_mode_per_channel_reset(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    ctrl.handle_mode(["channel1", "youtube"])
    text = ctrl.handle_mode(["channel1", "default"])
    assert text == "Output mode for channel1 reset to global (disk)"
    assert read_file(tmp_path)["channel_output_modes"] == {}
    assert config["output_mode"] == "disk"


def test_mode_per_channel_invalid_mode_rejected(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["channel1", "cloud"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_mode_per_channel_invalid_name_rejected(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["bad name!", "disk"])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_mode_usage_too_many_args(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_mode(["a", "b", "c"])
    assert text == "Usage: /mode <disk|youtube|both> or /mode <channel> <disk|youtube|both|default>"
    assert read_file(tmp_path) == before


def test_remove_clears_override(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path, channels=["channel1", "ch"])
    ctrl.handle_mode(["channel1", "youtube"])
    asyncio.run(ctrl.handle_remove(["channel1"]))
    assert read_file(tmp_path)["channel_output_modes"] == {}
    assert "channel1" not in read_file(tmp_path)["channels"]


def test_status_shows_per_channel_modes(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    ctrl.handle_mode(["channel1", "youtube"])
    assert "Per-channel modes: channel1=youtube" in asyncio.run(ctrl.handle_status())


def test_reload_picks_up_disk_edits(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    file_config = read_file(tmp_path)
    file_config["channels"].append("hand_edit")
    file_config["retention_days"] = 3
    (tmp_path / "config.json").write_text(json.dumps(file_config, indent=4))
    text = ctrl.handle_reload()
    assert text == "\u2705 Config reloaded from config.json"
    assert "hand_edit" in config["channels"]
    assert config["retention_days"] == 3


def test_reload_corrupt_file_leaves_live_unchanged(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    (tmp_path / "config.json").write_text("{ not json")
    text = ctrl.handle_reload()
    assert text.startswith("\u274c")
    assert config["channels"] == ["channel1"]


def test_restart_schedules_callback(tmp_path):
    flag = threading.Event()
    _, ctrl, _, _ = make_controller(tmp_path, on_restart=flag.set)

    async def scenario():
        text = ctrl.handle_restart()
        assert "\U0001f504 Restarting..." in text
        await asyncio.sleep(0.6)
        assert flag.is_set()

    asyncio.run(scenario())


def test_restart_without_callback(tmp_path):
    _, ctrl, _, _ = make_controller(tmp_path)
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
    _, ctrl, _, _ = make_controller(tmp_path, on_restart=flag.set)
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
    _, ctrl, _, _ = make_controller(tmp_path, on_restart=flag.set)
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
    _, ctrl, _, _ = make_controller(tmp_path, on_restart=flag.set)
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
    _, ctrl, _, _ = make_controller(tmp_path, on_restart=flag.set)
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
    _, ctrl, _, _ = make_controller(tmp_path, on_restart=flag.set)
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
    _, ctrl, _, _ = make_controller(tmp_path)
    assert asyncio.run(ctrl.handle_update()) == "Update checks are not configured"


def test_status_contains_update_check_line(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    assert "Update check: enabled (every 24h)" in asyncio.run(ctrl.handle_status())


def test_status_contains_quality_and_disk_lines(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    text = asyncio.run(ctrl.handle_status())
    assert "Quality: best" in text
    assert "Concurrent limit: 0 recording(s), 0 YouTube re-stream(s)" in text
    assert "free of" in text
    assert "Disk limits: min free 0 GB" in text


def test_quality_show_set_invalid(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    assert "Quality: best" in ctrl.handle_quality([])
    text = ctrl.handle_quality(["720p"])
    assert text == "Quality set to 720p"
    assert read_file(tmp_path)["preferred_quality"] == "720p"
    assert config["preferred_quality"] == "720p"
    assert ctrl.handle_quality(["a", "b"]) == "Usage: /quality <best|1080p|720p|...>"


def test_quality_empty_rejected(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    before = read_file(tmp_path)
    text = ctrl.handle_quality([""])
    assert text.startswith("\u274c")
    assert read_file(tmp_path) == before


def test_maxrecordings_show_set_invalid(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
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
    config, ctrl, _, _ = make_controller(tmp_path)
    assert ctrl.handle_maxrecordings(["1", "2"]) == "Usage: /maxrecordings <n> (0 = unlimited)"


def test_maxyoutube_set(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
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
    config, ctrl, _, _ = make_controller(tmp_path)
    text = ctrl.handle_disk(["minfree", "5"])
    assert text == "Disk min free set to 5 GB"
    assert read_file(tmp_path)["disk"]["min_free_gb"] == 5
    assert config["disk"]["min_free_gb"] == 5

    text = ctrl.handle_disk(["maxsize", "20"])
    assert text == "Disk max total set to 20 GB"
    assert read_file(tmp_path)["disk"]["max_total_gb"] == 20

    text = ctrl.handle_disk(["fill", "10"])
    assert text == "Disk fill guard set to 10 min"
    assert read_file(tmp_path)["disk"]["min_time_to_full_min"] == 10

    text = ctrl.handle_disk(["interval", "30"])
    assert text == "Disk check interval set to 30s"
    assert read_file(tmp_path)["disk"]["check_interval_s"] == 30

    text = ctrl.handle_disk(["evict", "off"])
    assert text == "Disk eviction disabled"
    assert read_file(tmp_path)["disk"]["evict_when_over"] is False
    text = ctrl.handle_disk(["evict", "on"])
    assert text == "Disk eviction enabled"
    assert read_file(tmp_path)["disk"]["evict_when_over"] is True

    assert ctrl.handle_disk(["bogus", "1"]) == "Usage: /disk <minfree|maxsize|fill|interval|evict> <value>"
    before = read_file(tmp_path)
    assert ctrl.handle_disk(["minfree", "x"]).startswith("\u274c")
    assert read_file(tmp_path) == before


def test_disk_show_block(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    text = ctrl.handle_disk([])
    assert "Disk limits:" in text
    assert "min free: 0 GB (0 = disabled)" in text
    assert "max total: 0 GB (0 = disabled, evict: on)" in text
    assert "stop if full in < 0 min (0 = disabled)" in text
    assert "check every 60s" in text


def test_help_lists_new_commands(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    text = ctrl.handle_help()
    assert "/quality [value]" in text
    assert "/maxrecordings <n>" in text
    assert "/maxyoutube <n>" in text
    assert "/disk <minfree|maxsize|fill|interval|evict> <value>" in text
    assert "/chat [on|off]" in text


def test_chat_show_state(tmp_path):
    config, ctrl, _, _ = make_controller(tmp_path)
    assert "Chat recording: enabled" in asyncio.run(ctrl.handle_chat([]))
    asyncio.run(ctrl.handle_chat(["off"]))
    assert "Chat recording: disabled" in asyncio.run(ctrl.handle_chat([]))
    assert "Chat recording: disabled" in asyncio.run(ctrl.handle_status())


def test_chat_on_persists(tmp_path):
    config, ctrl, recorder, _ = make_controller(tmp_path, recording=["channel1"])
    asyncio.run(ctrl.handle_chat(["off"]))
    text = asyncio.run(ctrl.handle_chat(["on"]))
    assert text == "Chat recording enabled"
    assert read_file(tmp_path)["record_chat"] is True
    assert config["record_chat"] is True
    assert recorder.chat_stop_calls == ["channel1"]  # from the earlier /chat off, not re-triggered by on


def test_chat_off_persists_and_stops_inflight(tmp_path):
    config, ctrl, recorder, _ = make_controller(tmp_path, recording=["channel1", "ch"])
    text = asyncio.run(ctrl.handle_chat(["off"]))
    assert text == "Chat recording disabled"
    assert read_file(tmp_path)["record_chat"] is False
    assert config["record_chat"] is False
    assert recorder.chat_stop_calls == ["ch", "channel1"]
    assert recorder.stop_calls == []


def test_chat_invalid_rejected(tmp_path):
    config, ctrl, recorder, _ = make_controller(tmp_path, recording=["channel1"])
    before = read_file(tmp_path)
    assert asyncio.run(ctrl.handle_chat([])) == "Chat recording: enabled"
    assert asyncio.run(ctrl.handle_chat(["maybe"])) == "Usage: /chat <on|off>"
    assert asyncio.run(ctrl.handle_chat(["on", "off"])) == "Usage: /chat <on|off>"
    assert read_file(tmp_path) == before
    assert recorder.chat_stop_calls == []
