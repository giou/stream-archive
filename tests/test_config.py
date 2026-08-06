import pytest

from src.twitch_recorder.config import _validate


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
