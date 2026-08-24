import json
from pathlib import Path

import pytest

from stream_archive.config import AppConfig, get_config, normalize_channel_name, save_config


def valid_config():
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
    }


def build(**overrides):
    data = valid_config()
    data.update(overrides)
    return AppConfig.model_validate(data)


def test_valid_config_passes_and_sets_defaults():
    config = build()
    assert config.output_mode == "disk"
    assert config.youtube.privacy_status == "unlisted"
    assert config.youtube.client_secrets_file == "client_secret.json"
    assert config.youtube.hold_seconds == 0
    assert config.channel_youtube_hold_seconds == {}
    assert config.retention_days == 0
    assert config.update_check.enabled is True
    assert config.update_check.interval_hours == 24
    assert config.update_check.check_app is True
    assert config.update_check.check_streamlink is True
    assert config.update_check.check_plugin is True
    assert config.preferred_quality == "best"
    assert config.max_concurrent_recordings == 0
    assert config.max_concurrent_youtube_streams == 0
    assert config.record_chat is True
    assert config.chat_dir == "chat"
    assert config.disk.max_total_gb == 0
    assert config.disk.check_interval_s == 60
    assert config.disk.delete_oldest is True
    assert config.eventsub.enabled is True


def test_valid_disk_values_pass():
    config = build(
        disk={
            "max_total_gb": 100,
            "check_interval_s": 30,
            "delete_oldest": False,
        }
    )
    assert config.disk.max_total_gb == 100
    assert config.disk.delete_oldest is False


def test_obsolete_disk_keys_are_dropped():
    config = build(
        disk={
            "min_free_gb": 5,
            "min_time_to_full_min": 15,
            "max_total_gb": 100,
        }
    )
    dumped = config.disk.model_dump()
    assert "min_free_gb" not in dumped
    assert "min_time_to_full_min" not in dumped
    assert config.disk.max_total_gb == 100


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.__setitem__("preferred_quality", ""),
        lambda c: c.__setitem__("max_concurrent_recordings", -1),
        lambda c: c.__setitem__("max_concurrent_youtube_streams", -1),
        lambda c: c.__setitem__("disk", []),
        lambda c: c.__setitem__("disk", {"max_total_gb": -1}),
        lambda c: c.__setitem__("disk", {"check_interval_s": 0}),
        lambda c: c.__setitem__("disk", {"check_interval_s": -5}),
        lambda c: c.__setitem__("disk", {"delete_oldest": "yes"}),
        lambda c: c.__setitem__("record_chat", "yes"),
        lambda c: c.__setitem__("chat_dir", ""),
        lambda c: c.__setitem__("eventsub", {"enabled": "yes"}),
        lambda c: c.__setitem__("eventsub", 5),
    ],
)
def test_invalid_new_settings_raise(mutate):
    config = valid_config()
    mutate(config)
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


@pytest.mark.parametrize(
    "key",
    [
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
    ],
)
def test_missing_required_key_raises(key):
    config = valid_config()
    del config[key]
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_invalid_channel_name_raises():
    config = valid_config()
    config["channels"] = ["bad name!"]
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_invalid_timezone_raises():
    config = valid_config()
    config["timezone"] = "Mars/Olympus"
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_invalid_output_mode_raises():
    config = valid_config()
    config["output_mode"] = "cloud"
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


@pytest.mark.parametrize("interval", [0, -5])
def test_non_positive_monitoring_interval_raises(interval):
    config = valid_config()
    config["monitoring_interval"] = interval
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


@pytest.mark.parametrize("retention_days", [-1, "x"])
def test_invalid_retention_days_raises(retention_days):
    config = valid_config()
    config["retention_days"] = retention_days
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_positive_retention_days_passes():
    config = build(retention_days=7)
    assert config.retention_days == 7


def test_valid_config_sets_channel_output_modes_default():
    config = build()
    assert config.channel_output_modes == {}


def test_valid_channel_output_modes_passes():
    config = build(channel_output_modes={"channel1": "youtube", "other": "both"})
    assert config.channel_output_modes == {"twitch:channel1": "youtube", "twitch:other": "both"}


def test_invalid_channel_output_mode_value_raises():
    config = valid_config()
    config["channel_output_modes"] = {"channel1": "cloud"}
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_invalid_channel_output_mode_name_raises():
    config = valid_config()
    config["channel_output_modes"] = {"bad name!": "disk"}
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_channel_output_modes_non_dict_raises():
    config = valid_config()
    config["channel_output_modes"] = []
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.__setitem__("update_check", {"enabled": "yes"}),
        lambda c: c.__setitem__("update_check", {"interval_hours": 0}),
        lambda c: c.__setitem__("update_check", {"interval_hours": -1}),
        lambda c: c.__setitem__("update_check", {"check_app": "x"}),
        lambda c: c.__setitem__("update_check", {"check_plugin": 1}),
        lambda c: c.__setitem__("update_check", []),
    ],
)
def test_invalid_update_check_raises(mutate):
    config = valid_config()
    mutate(config)
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_valid_update_check_values_pass():
    config = build(
        update_check={
            "enabled": False,
            "interval_hours": 6.5,
            "check_app": False,
            "check_streamlink": False,
            "check_plugin": False,
        }
    )
    assert config.update_check.enabled is False
    assert config.update_check.interval_hours == 6.5


def test_eventsub_disabled_passes():
    config = build(eventsub={"enabled": False})
    assert config.eventsub.enabled is False


def kick_config(channels=None):
    config = valid_config()
    config["channels"] = channels or ["kick:xqc"]
    config["kick"] = {
        "client_id": "cid",
        "client_secret": "csec",
        "record_chat": True,
        "webhook": {
            "enabled": False,
            "listen_host": "127.0.0.1",
            "listen_port": 8787,
            "public_url": "",
        },
    }
    return config


def test_kick_channel_with_creds_passes_and_sets_defaults():
    config = AppConfig.model_validate(kick_config())
    assert config.channels == ["kick:xqc"]
    assert config.kick.record_chat is True
    assert config.kick.webhook.enabled is False
    assert config.kick.webhook.listen_host == "127.0.0.1"
    assert config.kick.webhook.listen_port == 8787


def test_kick_channel_normalized_lowercase():
    config = AppConfig.model_validate(kick_config(["kick:XQC"]))
    assert config.channels == ["kick:xqc"]


def test_kick_channel_without_creds_raises():
    config = kick_config()
    del config["kick"]["client_id"]
    with pytest.raises(ValueError, match="kick.client_id is required"):
        AppConfig.model_validate(config)

    config = kick_config()
    del config["kick"]["client_secret"]
    with pytest.raises(ValueError, match="kick.client_secret is required"):
        AppConfig.model_validate(config)


@pytest.mark.parametrize("ch", ["kick:", "kick:bad name", "kick:.dot", "kick:", "kick:a" * 26])
def test_invalid_kick_channel_raises(ch):
    config = kick_config([ch])
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_twitch_prefix_normalized_to_bare():
    config = build(channels=["twitch:foo"])
    assert config.channels == ["twitch:foo"]


def test_kick_channel_output_modes_key_passes():
    config = kick_config()
    config["channel_output_modes"] = {"kick:xqc": "youtube"}
    config = AppConfig.model_validate(config)
    assert config.channel_output_modes == {"kick:xqc": "youtube"}


def test_kick_webhook_enabled_requires_public_url():
    config = kick_config()
    config["kick"]["webhook"]["enabled"] = True
    with pytest.raises(ValueError, match="kick.webhook.public_url is required"):
        AppConfig.model_validate(config)

    config = kick_config()
    config["kick"]["webhook"]["enabled"] = True
    config["kick"]["webhook"]["public_url"] = "ftp://nope"
    with pytest.raises(ValueError, match="kick.webhook.public_url is required"):
        AppConfig.model_validate(config)

    config = kick_config()
    config["kick"]["webhook"]["enabled"] = True
    config["kick"]["webhook"]["public_url"] = "https://host.ts.net/kick/webhook"
    AppConfig.model_validate(config)


def test_kick_record_chat_non_bool_raises():
    config = kick_config()
    config["kick"]["record_chat"] = "yes"
    with pytest.raises(ValueError, match="kick.record_chat must be a boolean"):
        AppConfig.model_validate(config)


def test_kick_webhook_invalid_values_raise():
    config = kick_config()
    config["kick"]["webhook"]["listen_port"] = 0
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)
    config = kick_config()
    config["kick"]["webhook"]["listen_port"] = 70000
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)
    config = kick_config()
    config["kick"]["webhook"]["listen_port"] = "8787"
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)
    config = kick_config()
    config["kick"]["webhook"]["listen_host"] = ""
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)
    config = kick_config()
    config["kick"]["webhook"]["enabled"] = "yes"
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)
    config = kick_config()
    config["kick"]["webhook"]["setup_notified"] = "yes"
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)
    config = kick_config()
    config["kick"]["webhook"]["tunnel"] = "wireguard"
    with pytest.raises(ValueError, match="kick.webhook.tunnel"):
        AppConfig.model_validate(config)
    config = kick_config()
    config["kick"]["webhook"]["cloudflare_token"] = 42
    with pytest.raises(ValueError, match="kick.webhook.cloudflare_token"):
        AppConfig.model_validate(config)
    config = kick_config()
    config["kick"]["webhook"]["cloudflare_managed"] = "yes"
    with pytest.raises(ValueError, match="kick.webhook.cloudflare_managed"):
        AppConfig.model_validate(config)


def test_bare_channels_valid_without_kick_section():
    config = build()
    assert config.kick.record_chat is True
    assert config.kick.webhook.enabled is False
    assert config.kick.webhook.listen_host == "127.0.0.1"
    assert config.kick.webhook.listen_port == 8787
    assert config.kick.webhook.public_url == ""
    assert config.kick.webhook.setup_notified is False
    assert config.kick.webhook.tunnel == ""
    assert config.kick.webhook.cloudflare_token == ""
    assert config.kick.webhook.cloudflare_managed is False


def test_bare_name_and_channel_url_helpers():
    from stream_archive.config import bare_name, channel_url

    assert bare_name("kick:xqc") == "xqc"
    assert bare_name("twitch:streamer1") == "streamer1"
    assert bare_name("streamer1") == "streamer1"
    assert channel_url("kick:xqc") == "https://kick.com/xqc"
    assert channel_url("twitch:streamer1") == "https://twitch.tv/streamer1"
    assert channel_url("streamer1") == "https://twitch.tv/streamer1"


def test_kick_url_normalized_to_slug():
    config = AppConfig.model_validate(kick_config(["https://kick.com/xqc"]))
    assert config.channels == ["kick:xqc"]


def test_twitch_url_normalized_to_bare():
    config = build(channels=["https://www.twitch.tv/foo/"])
    assert config.channels == ["twitch:foo"]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("https://kick.com/xqc", "kick:xqc"),
        ("https://KICK.com/XQC", "kick:xqc"),
        ("https://kick.com/xqc/", "kick:xqc"),
        ("https://kick.com/x?ref=1", "kick:x"),
        ("https://twitch.tv/foo", "twitch:foo"),
        ("https://www.twitch.tv/foo/", "twitch:foo"),
        ("https://twitch.tv/foo?ref=1", "twitch:foo"),
        ("  https://twitch.tv/foo  ", "twitch:foo"),
    ],
)
def test_normalize_channel_name_urls(name, expected):
    assert normalize_channel_name(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "https://kick.com/",
        "https://kick.com/bad name",
        "https://kick.com/foo/bar",
        "https://other.com/x",
        "https://twitch.tv/",
        "https://twitch.tv/bad name!",
        "https://twitch.tv/foo/bar",
        "kick.com/x",
        "http://",
        "https://",
    ],
)
def test_normalize_channel_name_invalid_urls(name):
    assert normalize_channel_name(name) is None


def test_env_interpolation_and_placeholder_preservation(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_BOT_TOKEN", "s3cr3t")
    data = valid_config()
    data["bot_telegram_api"] = "${TEST_BOT_TOKEN}"
    (tmp_path / "config.json").write_text(json.dumps(data))
    cfg = get_config(tmp_path / "config.json")
    assert cfg.bot_telegram_api == "s3cr3t"
    save_config(cfg)
    raw = (tmp_path / "config.json").read_text()
    assert "${TEST_BOT_TOKEN}" in raw
    assert "s3cr3t" not in raw


def test_save_persists_bot_written_literal_over_placeholder(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOK", "abc")
    data = valid_config()
    data["bot_telegram_api"] = "${MY_TOK}"
    (tmp_path / "config.json").write_text(json.dumps(data))
    cfg = get_config(tmp_path / "config.json")

    cfg.bot_telegram_api = "literal"  # bot persisted an explicit value
    save_config(cfg)

    raw = (tmp_path / "config.json").read_text()
    assert '"literal"' in raw
    assert "${MY_TOK}" not in raw
    save_config(cfg)  # a second save keeps the literal (placeholder dropped)
    assert json.loads((tmp_path / "config.json").read_text())["bot_telegram_api"] == "literal"


def test_untouched_placeholder_still_round_trips_masked(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOK", "abc")
    data = valid_config()
    data["bot_telegram_api"] = "${MY_TOK}"
    (tmp_path / "config.json").write_text(json.dumps(data))
    cfg = get_config(tmp_path / "config.json")

    save_config(cfg)

    raw = (tmp_path / "config.json").read_text()
    assert "${MY_TOK}" in raw
    assert "abc" not in raw


def test_missing_env_var_at_save_restores_placeholder(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOK", "abc")
    data = valid_config()
    data["bot_telegram_api"] = "${MY_TOK}"
    (tmp_path / "config.json").write_text(json.dumps(data))
    cfg = get_config(tmp_path / "config.json")

    monkeypatch.delenv("MY_TOK")  # env vanished before the next save
    save_config(cfg)

    raw = (tmp_path / "config.json").read_text()
    assert "${MY_TOK}" in raw


def test_env_interpolation_missing_var_raises(tmp_path):
    data = valid_config()
    data["bot_telegram_api"] = "${TEST_BOT_TOKEN}"
    (tmp_path / "config.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="TEST_BOT_TOKEN"):
        get_config(tmp_path / "config.json")


def test_channel_hold_override_normalized():
    config = build(channel_youtube_hold_seconds={"channel1": 60})
    assert config.channel_youtube_hold_seconds == {"twitch:channel1": 60}


def test_invalid_channel_hold_key_raises():
    config = valid_config()
    config["channel_youtube_hold_seconds"] = {"bad name!": 60}
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_negative_channel_hold_raises():
    config = valid_config()
    config["channel_youtube_hold_seconds"] = {"channel1": -1}
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_negative_global_hold_raises():
    config = valid_config()
    config["youtube"] = {"hold_seconds": -1}
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_config_example_is_valid_json_and_appconfig():
    data = json.loads((Path(__file__).resolve().parent.parent / "config.json.example").read_text())
    data["telegram_user_id"] = 12345  # placeholder string fails StrictInt by design
    config = AppConfig.model_validate(data)
    assert config.youtube.hold_seconds == 0
    assert config.output_mode == "disk"
