import pytest

from src.stream_archive.config import _validate


def valid_config():
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


def test_valid_config_passes_and_sets_defaults():
    config = valid_config()
    _validate(config)
    assert config["output_mode"] == "disk"
    assert config["youtube"] == {
        "privacy_status": "unlisted",
        "client_secrets_file": "client_secret.json",
    }
    assert config["retention_days"] == 0
    assert config["update_check"] == {
        "enabled": True,
        "interval_hours": 24,
        "check_app": True,
        "check_streamlink": True,
        "check_plugin": True,
    }
    assert config["preferred_quality"] == "best"
    assert config["max_concurrent_recordings"] == 0
    assert config["max_concurrent_youtube_streams"] == 0
    assert config["record_chat"] is True
    assert config["chat_dir"] == "chat"
    assert config["disk"] == {
        "min_free_gb": 0,
        "max_total_gb": 0,
        "check_interval_s": 60,
        "min_time_to_full_min": 0,
        "evict_when_over": True,
    }


def test_valid_disk_values_pass():
    config = valid_config()
    config["disk"] = {
        "min_free_gb": 5.5,
        "max_total_gb": 100,
        "check_interval_s": 30,
        "min_time_to_full_min": 15,
        "evict_when_over": False,
    }
    _validate(config)
    assert config["disk"]["min_free_gb"] == 5.5
    assert config["disk"]["evict_when_over"] is False


@pytest.mark.parametrize("mutate", [
    lambda c: c.__setitem__("preferred_quality", ""),
    lambda c: c.__setitem__("max_concurrent_recordings", -1),
    lambda c: c.__setitem__("max_concurrent_youtube_streams", -1),
    lambda c: c.__setitem__("disk", []),
    lambda c: c.__setitem__("disk", {"min_free_gb": -1}),
    lambda c: c.__setitem__("disk", {"max_total_gb": -1}),
    lambda c: c.__setitem__("disk", {"check_interval_s": 0}),
    lambda c: c.__setitem__("disk", {"check_interval_s": -5}),
    lambda c: c.__setitem__("disk", {"min_time_to_full_min": -1}),
    lambda c: c.__setitem__("disk", {"evict_when_over": "yes"}),
    lambda c: c.__setitem__("record_chat", "yes"),
    lambda c: c.__setitem__("chat_dir", ""),
])
def test_invalid_new_settings_raise(mutate):
    config = valid_config()
    mutate(config)
    with pytest.raises(ValueError):
        _validate(config)


@pytest.mark.parametrize("key", [
    "telegram_user_id",
    "bot_telegram_api",
    "twitch_client_id",
    "twitch_client_secret",
    "channels",
    "proxy_list",
    "monitoring_interval",
    "timezone",
    "plugin_dir",
    "recording_dir",
])
def test_missing_required_key_raises(key):
    config = valid_config()
    del config[key]
    with pytest.raises(ValueError):
        _validate(config)


def test_invalid_channel_name_raises():
    config = valid_config()
    config["channels"] = ["bad name!"]
    with pytest.raises(ValueError):
        _validate(config)


def test_invalid_timezone_raises():
    config = valid_config()
    config["timezone"] = "Mars/Olympus"
    with pytest.raises(ValueError):
        _validate(config)


def test_invalid_output_mode_raises():
    config = valid_config()
    config["output_mode"] = "cloud"
    with pytest.raises(ValueError):
        _validate(config)


@pytest.mark.parametrize("interval", [0, -5])
def test_non_positive_monitoring_interval_raises(interval):
    config = valid_config()
    config["monitoring_interval"] = interval
    with pytest.raises(ValueError):
        _validate(config)


@pytest.mark.parametrize("retention_days", [-1, "x"])
def test_invalid_retention_days_raises(retention_days):
    config = valid_config()
    config["retention_days"] = retention_days
    with pytest.raises(ValueError):
        _validate(config)


def test_positive_retention_days_passes():
    config = valid_config()
    config["retention_days"] = 7
    _validate(config)
    assert config["retention_days"] == 7


def test_valid_config_sets_channel_output_modes_default():
    config = valid_config()
    _validate(config)
    assert config["channel_output_modes"] == {}


def test_valid_channel_output_modes_passes():
    config = valid_config()
    config["channel_output_modes"] = {"channel1": "youtube", "other": "both"}
    _validate(config)


def test_invalid_channel_output_mode_value_raises():
    config = valid_config()
    config["channel_output_modes"] = {"channel1": "cloud"}
    with pytest.raises(ValueError):
        _validate(config)


def test_invalid_channel_output_mode_name_raises():
    config = valid_config()
    config["channel_output_modes"] = {"bad name!": "disk"}
    with pytest.raises(ValueError):
        _validate(config)


def test_channel_output_modes_non_dict_raises():
    config = valid_config()
    config["channel_output_modes"] = []
    with pytest.raises(ValueError):
        _validate(config)


@pytest.mark.parametrize("mutate", [
    lambda c: c.__setitem__("update_check", {"enabled": "yes"}),
    lambda c: c.__setitem__("update_check", {"interval_hours": 0}),
    lambda c: c.__setitem__("update_check", {"interval_hours": -1}),
    lambda c: c.__setitem__("update_check", {"check_app": "x"}),
    lambda c: c.__setitem__("update_check", {"check_plugin": 1}),
    lambda c: c.__setitem__("update_check", []),
])
def test_invalid_update_check_raises(mutate):
    config = valid_config()
    mutate(config)
    with pytest.raises(ValueError):
        _validate(config)


def test_valid_update_check_values_pass():
    config = valid_config()
    config["update_check"] = {
        "enabled": False,
        "interval_hours": 6.5,
        "check_app": False,
        "check_streamlink": False,
        "check_plugin": False,
    }
    _validate(config)
    assert config["update_check"]["enabled"] is False
    assert config["update_check"]["interval_hours"] == 6.5
