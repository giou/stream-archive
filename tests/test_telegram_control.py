import asyncio
import json
import threading

from src.twitch_recorder.config import _validate
from src.twitch_recorder.telegram_control import TelegramController


class FakeRecorder:
    def __init__(self, active=(), recording=()):
        self._active = list(active)
        self._recording = set(recording)
        self.stop_calls = []

    def is_recording(self, channel):
        return channel in self._recording

    async def stop(self, channel):
        self.stop_calls.append(channel)
        self._recording.discard(channel)

    def active_channels(self):
        return sorted(self._active)


class FakeMonitor:
    def __init__(self):
        self.remove_calls = []

    def remove_channel(self, channel):
        self.remove_calls.append(channel)


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
    text = ctrl.handle_status()
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
    assert "Retention: 7 days" in ctrl.handle_status()
    ctrl.handle_retention(["1"])
    assert "Retention: 1 day" in ctrl.handle_status()


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
    assert "Per-channel modes: channel1=youtube" in ctrl.handle_status()


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
