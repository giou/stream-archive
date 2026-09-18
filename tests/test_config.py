import json
import os
import stat
from pathlib import Path

import pytest

from stream_archive.config import (
    AppConfig,
    api_base_url,
    apply_config_change,
    effective_quality,
    endpoint_base_url,
    get_config,
    normalize_channel_name,
    save_config,
    webhook_public_url,
)


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
    assert config.channel_preferred_qualities == {}
    assert config.update_check.enabled is True
    assert config.update_check.interval_hours == 24
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
    "mutate,match",
    [
        (lambda c: c.__setitem__("preferred_quality", ""), "preferred_quality"),
        (lambda c: c.__setitem__("max_concurrent_recordings", -1), "max_concurrent_recordings"),
        (lambda c: c.__setitem__("max_concurrent_youtube_streams", -1), "max_concurrent_youtube_streams"),
        (lambda c: c.__setitem__("disk", []), "disk"),
        (lambda c: c.__setitem__("disk", {"max_total_gb": -1}), r"disk\.max_total_gb"),
        (lambda c: c.__setitem__("disk", {"check_interval_s": 0}), r"disk\.check_interval_s"),
        (lambda c: c.__setitem__("disk", {"check_interval_s": -5}), r"disk\.check_interval_s"),
        (lambda c: c.__setitem__("disk", {"delete_oldest": "yes"}), r"disk\.delete_oldest"),
        (lambda c: c.__setitem__("record_chat", "yes"), "record_chat"),
        (lambda c: c.__setitem__("chat_dir", ""), "chat_dir"),
        (lambda c: c.__setitem__("eventsub", {"enabled": "yes"}), r"eventsub\.enabled"),
        (lambda c: c.__setitem__("eventsub", 5), "eventsub"),
        (
            lambda c: c.__setitem__("channel_preferred_qualities", {"bad!name": "720p"}),
            "Invalid channel name in channel_preferred_qualities",
        ),
        (
            lambda c: c.__setitem__("channel_preferred_qualities", {"kick:x": ""}),
            r"channel_preferred_qualities\.kick:x must be a non-empty quality string",
        ),
    ],
)
def test_invalid_new_settings_raise(mutate, match):
    """Each case pins its own rule, not just some ValueError."""
    config = valid_config()
    mutate(config)
    with pytest.raises(ValueError, match=match):
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


def test_channel_quality_keys_normalized_and_effective_quality():
    config = build(
        channel_preferred_qualities={"channel1": "720p"},
        preferred_quality="audio_only",
    )
    assert config.channel_preferred_qualities == {"twitch:channel1": "720p"}
    assert effective_quality(config, "twitch:channel1") == "720p"
    assert effective_quality(config, "twitch:other") == "audio_only"


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
        lambda c: c.__setitem__("update_check", []),
    ],
)
def test_invalid_update_check_raises(mutate):
    config = valid_config()
    mutate(config)
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_valid_update_check_values_pass():
    config = build(update_check={"enabled": False, "interval_hours": 6.5})
    assert config.update_check.enabled is False
    assert config.update_check.interval_hours == 6.5


def test_legacy_update_check_keys_still_load():
    """Config files from older releases hold the removed check_* keys."""
    config = build(
        update_check={
            "enabled": True,
            "interval_hours": 12,
            "check_app": True,
            "check_streamlink": True,
            "check_plugin": True,
        }
    )
    assert config.update_check.interval_hours == 12


def test_eventsub_disabled_passes():
    config = build(eventsub={"enabled": False})
    assert config.eventsub.enabled is False


def kick_config(channels=None):
    config = valid_config()
    # An explicit None check keeps an empty list an empty list.
    config["channels"] = ["kick:xqc"] if channels is None else channels
    config["endpoint"] = {
        "enabled": False,
        "listen_host": "127.0.0.1",
        "listen_port": 8787,
        "public_url": "",
    }
    config["kick"] = {
        "client_id": "cid",
        "client_secret": "csec",
        "record_chat": True,
        "webhook": {"enabled": False},
    }
    return config


def test_kick_channel_with_creds_passes_and_sets_defaults():
    config = AppConfig.model_validate(kick_config())
    assert config.channels == ["kick:xqc"]
    assert config.kick.record_chat is True
    assert config.kick.webhook.enabled is False
    assert config.endpoint.listen_host == "127.0.0.1"
    assert config.endpoint.listen_port == 8787


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


@pytest.mark.parametrize("ch", ["kick:", "kick:bad name", "kick:.dot", "kick:" + "a" * 26])
def test_invalid_kick_channel_raises(ch):
    config = kick_config([ch])
    with pytest.raises(ValueError):
        AppConfig.model_validate(config)


def test_twitch_prefix_is_preserved():
    config = build(channels=["twitch:foo"])
    assert config.channels == ["twitch:foo"]


def test_two_spellings_of_one_twitch_channel_raise():
    """Names are lowercased first, so both spellings are the same channel."""
    config = valid_config()
    config["channels"] = ["twitch:Foo", "FOO"]
    with pytest.raises(ValueError, match="Duplicate channel: 'twitch:foo'"):
        AppConfig.model_validate(config)


def test_kick_channel_output_modes_key_passes():
    config = kick_config()
    config["channel_output_modes"] = {"kick:xqc": "youtube"}
    config = AppConfig.model_validate(config)
    assert config.channel_output_modes == {"kick:xqc": "youtube"}


def test_endpoint_enabled_requires_public_url():
    config = kick_config()
    config["endpoint"]["enabled"] = True
    with pytest.raises(ValueError, match="endpoint.public_url is required"):
        AppConfig.model_validate(config)

    config = kick_config()
    config["endpoint"]["enabled"] = True
    config["endpoint"]["public_url"] = "ftp://nope"
    with pytest.raises(ValueError, match="endpoint.public_url is required"):
        AppConfig.model_validate(config)

    config = kick_config()
    config["endpoint"]["enabled"] = True
    config["endpoint"]["public_url"] = "https://host.ts.net"
    AppConfig.model_validate(config)


def test_kick_record_chat_non_bool_raises():
    config = kick_config()
    config["kick"]["record_chat"] = "yes"
    with pytest.raises(ValueError, match="kick.record_chat must be a boolean"):
        AppConfig.model_validate(config)


def test_endpoint_invalid_values_raise():
    config = kick_config()
    config["endpoint"]["listen_port"] = 0
    with pytest.raises(ValueError, match=r"endpoint\.listen_port"):
        AppConfig.model_validate(config)
    config = kick_config()
    config["endpoint"]["listen_port"] = 70000
    with pytest.raises(ValueError, match=r"endpoint\.listen_port"):
        AppConfig.model_validate(config)
    config = kick_config()
    config["endpoint"]["listen_port"] = "8787"
    with pytest.raises(ValueError, match=r"endpoint\.listen_port"):
        AppConfig.model_validate(config)
    config = kick_config()
    config["endpoint"]["listen_host"] = ""
    with pytest.raises(ValueError, match=r"endpoint\.listen_host"):
        AppConfig.model_validate(config)
    config = kick_config()
    config["endpoint"]["enabled"] = "yes"
    with pytest.raises(ValueError, match=r"endpoint\.enabled"):
        AppConfig.model_validate(config)
    config = kick_config()
    config["endpoint"]["tunnel"] = "wireguard"
    with pytest.raises(ValueError, match="endpoint.tunnel"):
        AppConfig.model_validate(config)
    config = kick_config()
    config["endpoint"]["cloudflare_token"] = 42
    with pytest.raises(ValueError, match="endpoint.cloudflare_token"):
        AppConfig.model_validate(config)
    config = kick_config()
    config["endpoint"]["cloudflare_managed"] = "yes"
    with pytest.raises(ValueError, match="endpoint.cloudflare_managed"):
        AppConfig.model_validate(config)


def test_webhook_feature_invalid_values_raise():
    config = kick_config()
    config["kick"]["webhook"]["enabled"] = "yes"
    with pytest.raises(ValueError, match=r"kick\.webhook\.enabled"):
        AppConfig.model_validate(config)
    config = kick_config()
    config["kick"]["webhook"]["setup_notified"] = "yes"
    with pytest.raises(ValueError, match=r"kick\.webhook\.setup_notified"):
        AppConfig.model_validate(config)


def test_legacy_webhook_config_migrates_to_the_endpoint():
    config = kick_config()
    del config["endpoint"]  # older files had no endpoint section
    config["kick"]["webhook"] = {
        "enabled": True,
        "listen_host": "0.0.0.0",
        "listen_port": 9000,
        "public_url": "https://kick.example.com/kick/webhook",
        "setup_notified": True,
        "tunnel": "cloudflare",
        "cloudflare_token": "tok",
        "cloudflare_managed": True,
    }
    parsed = AppConfig.model_validate(config)

    assert parsed.endpoint.enabled is True
    assert parsed.endpoint.listen_host == "0.0.0.0"
    assert parsed.endpoint.listen_port == 9000
    assert parsed.endpoint.public_url == "https://kick.example.com/kick/webhook"
    assert parsed.endpoint.tunnel == "cloudflare"
    assert parsed.endpoint.cloudflare_token == "tok"
    assert parsed.endpoint.cloudflare_managed is True
    # The old file used one flag for both features: keep both on.
    assert parsed.kick.webhook.enabled is True
    assert parsed.kick.webhook.setup_notified is True


def test_endpoint_section_wins_over_a_legacy_webhook():
    """A file with an endpoint section must keep its own endpoint values."""
    config = kick_config()
    config["kick"]["webhook"] = {
        "enabled": True,
        "listen_host": "0.0.0.0",
        "listen_port": 9000,
        "public_url": "https://kick.example.com/kick/webhook",
        "tunnel": "cloudflare",
        "cloudflare_token": "tok",
    }
    config["endpoint"] = {
        "enabled": False,
        "listen_host": "127.0.0.1",
        "listen_port": 8787,
        "public_url": "",
    }

    parsed = AppConfig.model_validate(config)

    # Nothing may be copied from the legacy webhook over the endpoint section.
    assert parsed.endpoint.enabled is False
    assert parsed.endpoint.listen_host == "127.0.0.1"
    assert parsed.endpoint.listen_port == 8787
    assert parsed.endpoint.public_url == ""
    assert parsed.endpoint.tunnel == ""
    assert parsed.kick.webhook.enabled is True


def test_bare_channels_valid_without_kick_section():
    config = build()
    assert config.kick.record_chat is True
    assert config.kick.webhook.enabled is False
    assert config.kick.webhook.setup_notified is False
    assert config.endpoint.enabled is False
    assert config.endpoint.listen_host == "127.0.0.1"
    assert config.endpoint.listen_port == 8787
    assert config.endpoint.public_url == ""
    assert config.endpoint.tunnel == ""
    assert config.endpoint.cloudflare_token == ""
    assert config.endpoint.cloudflare_managed is False


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


def test_env_interpolation_missing_var_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_BOT_TOKEN", raising=False)
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


def test_api_defaults_and_validation():
    config = build()
    assert config.api.enabled is False
    assert config.api.key == ""
    with pytest.raises(ValueError, match="api.enabled must be a boolean"):
        build(api={"enabled": "yes"})


def test_public_url_helpers():
    config = build()
    assert api_base_url(config) == ""  # no public URL yet
    assert webhook_public_url(config) == ""
    config.endpoint.public_url = "https://kick.example.com"
    assert endpoint_base_url(config) == "https://kick.example.com"
    assert api_base_url(config) == "https://kick.example.com/api/v1/"
    assert webhook_public_url(config) == "https://kick.example.com/kick/webhook"
    # The old layout stored the webhook URL: strip that path.
    config.endpoint.public_url = "https://kick.example.com/kick/webhook/"
    assert endpoint_base_url(config) == "https://kick.example.com"
    assert api_base_url(config) == "https://kick.example.com/api/v1/"
    assert webhook_public_url(config) == "https://kick.example.com/kick/webhook"


def test_apply_config_change_writes_and_adopts_the_change(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(valid_config()))
    cfg = get_config(path)

    apply_config_change(cfg, lambda c: setattr(c, "output_mode", "youtube"))

    assert cfg.output_mode == "youtube"  # the live instance adopted the change
    assert json.loads(path.read_text())["output_mode"] == "youtube"
    assert not (tmp_path / "config.json.tmp").exists()  # atomic replace, no leftover


def test_apply_config_change_rejects_a_bad_change_and_writes_nothing(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(valid_config()))
    cfg = get_config(path)
    before = path.read_text()

    with pytest.raises(ValueError):
        apply_config_change(cfg, lambda c: setattr(c, "output_mode", "nope"))

    assert cfg.output_mode == "disk"
    assert path.read_text() == before


def test_save_tightens_a_mode_other_than_the_default(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(valid_config()))
    path.chmod(0o644)  # differs from the 0o600 every save writes
    cfg = get_config(path)

    save_config(cfg)

    assert (path.stat().st_mode & 0o777) == 0o600


def test_rotated_env_secret_keeps_placeholder_and_the_new_value_applies(monkeypatch, tmp_path):
    """A rotated variable must not become a plaintext literal of the old value."""
    monkeypatch.setenv("MY_TOK", "old-secret")
    data = valid_config()
    data["bot_telegram_api"] = "${MY_TOK}"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    cfg = get_config(path)

    monkeypatch.setenv("MY_TOK", "new-secret")  # the operator rotates the value
    save_config(cfg)  # any later save, for example a Telegram change

    raw = path.read_text()
    assert "${MY_TOK}" in raw
    assert "old-secret" not in raw
    assert "new-secret" not in raw
    # The rotated value takes effect on the next start, not the stale one.
    assert get_config(path).bot_telegram_api == "new-secret"


def test_save_creates_a_new_file_with_a_private_mode(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(valid_config()))
    cfg = get_config(path)
    path.unlink()  # an editor or a cleanup step removed the file

    save_config(cfg)

    assert (path.stat().st_mode & 0o777) == 0o600


def test_save_removes_the_tmp_copy_when_the_write_fails(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(valid_config()))
    cfg = get_config(path)
    real_replace = os.replace

    def no_space(src, dst, *args, **kwargs):
        # Fail only for the config file. Every other caller of os.replace
        # keeps working while this test runs.
        if str(dst) != str(path):
            return real_replace(src, dst, *args, **kwargs)
        # The temporary copy holds the same secrets, so it must never become
        # readable by another user, not even when the write fails.
        assert stat.S_IMODE(os.stat(src).st_mode) == 0o600
        msg = "No space left on device"
        raise OSError(28, msg)

    monkeypatch.setattr("stream_archive.config.os.replace", no_space)

    with pytest.raises(ValueError, match="cannot write config"):
        save_config(cfg)

    # The plaintext copy must not stay behind.
    assert not (tmp_path / "config.json.tmp").exists()
    assert json.loads(path.read_text())["bot_telegram_api"] == valid_config()["bot_telegram_api"]


def test_orphaned_env_placeholder_does_not_break_saves(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFINITELY_UNSET_VAR_12345", "resolved-at-load")
    data = valid_config()
    # An unknown key still gets interpolated, then pydantic drops it
    # (extra='ignore') and leaves an orphaned placeholder behind.
    data["bogus"] = "${DEFINITELY_UNSET_VAR_12345}"
    (tmp_path / "config.json").write_text(json.dumps(data))
    cfg = get_config(tmp_path / "config.json")
    monkeypatch.delenv("DEFINITELY_UNSET_VAR_12345", raising=False)

    save_config(cfg)  # must not raise

    rewritten = json.loads((tmp_path / "config.json").read_text())
    assert rewritten["bot_telegram_api"] == data["bot_telegram_api"]
    assert "bogus" not in rewritten
    save_config(cfg)  # later saves keep working
    assert json.loads((tmp_path / "config.json").read_text())["bot_telegram_api"] == data["bot_telegram_api"]


def legacy_config():
    """A file in the pre-endpoint-split layout: listener keys under kick.webhook."""
    data = valid_config()
    data["kick"] = {
        "client_id": "cid",
        "client_secret": "csec",
        "webhook": {
            "enabled": True,
            "listen_host": "0.0.0.0",
            "listen_port": 8787,
            "public_url": "https://x.example.com",
            "cloudflare_token": "",
        },
    }
    return data


def test_legacy_migration_keeps_the_env_mask_on_a_moved_key(monkeypatch, tmp_path):
    """A moved key must not unmask its environment placeholder.

    The placeholder tracker keys each masked value by its path in the file.
    When the layout moves afterwards, the recorded path is gone, and the
    resolved secret used to be written out as a literal.
    """
    monkeypatch.setenv("CF_TOKEN", "cf-secret-123")
    data = legacy_config()
    data["kick"]["webhook"]["cloudflare_token"] = "${CF_TOKEN}"
    (tmp_path / "config.json").write_text(json.dumps(data))

    cfg = get_config(tmp_path / "config.json")
    assert cfg.endpoint.cloudflare_token == "cf-secret-123"
    save_config(cfg)

    raw = (tmp_path / "config.json").read_text()
    assert json.loads(raw)["endpoint"]["cloudflare_token"] == "${CF_TOKEN}"
    assert "cf-secret-123" not in raw


def test_legacy_migration_still_moves_the_listener_keys(tmp_path):
    """The migration itself is unchanged by the masking fix."""
    (tmp_path / "config.json").write_text(json.dumps(legacy_config()))

    cfg = get_config(tmp_path / "config.json")

    assert cfg.endpoint.enabled is True
    assert cfg.endpoint.listen_port == 8787
    assert cfg.kick.webhook.enabled is True


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_disk_cap_is_rejected(value):
    """No non-finite cap may reach the file.

    ``ge=0`` already rejects NaN and -inf (both fail the comparison), but
    +inf satisfies it, so only the finite check catches that one: it would
    be stored as the non-standard Infinity token and the cap would never
    apply.
    """
    with pytest.raises(ValueError):
        build(disk={"max_total_gb": value})
    with pytest.raises(ValueError, match="finite"):
        build(disk={"max_total_gb": float("inf")})


def test_non_finite_hold_seconds_are_rejected():
    """The per-channel bound is hand-written, so NaN slipped past it."""
    with pytest.raises(ValueError, match="finite"):
        build(channel_youtube_hold_seconds={"twitch:channel1": float("nan")})
    with pytest.raises(ValueError, match="finite"):
        build(channel_youtube_hold_seconds={"twitch:channel1": float("inf")})


def test_save_does_not_follow_a_symlink_at_the_temp_path(tmp_path):
    """A planted entry at config.json.tmp must not receive the secret copy."""
    target = tmp_path / "victim.txt"
    target.write_text("original")
    data = valid_config()
    (tmp_path / "config.json").write_text(json.dumps(data))
    (tmp_path / "config.json.tmp").symlink_to(target)

    cfg = get_config(tmp_path / "config.json")
    save_config(cfg)

    assert target.read_text() == "original"
    assert not (tmp_path / "config.json.tmp").exists()
    assert json.loads((tmp_path / "config.json").read_text())["bot_telegram_api"] == "bot_token"
