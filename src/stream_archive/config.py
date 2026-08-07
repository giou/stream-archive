import json
import logging
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_CHANNEL_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_]{0,24}$')
_PROXY_RE = re.compile(r'^(https?|httpproxy)://')


def get_config():
    config_path = _find_config()
    try:
        with open(config_path) as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse config.json: %s", e)
        raise
    except FileNotFoundError:
        logger.error("config.json not found in %s", config_path.parent)
        raise

    _validate(config)
    config["_workdir"] = config_path.parent
    config["_config_path"] = config_path
    return config


def save_config(config):
    """Validate and atomically write config to the file it was loaded from."""
    _validate(config)
    path = config["_config_path"]
    data = {k: v for k, v in config.items() if not k.startswith("_")}
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
        f.write("\n")
    os.replace(tmp, path)


def reload_config(config):
    """Re-read config.json from disk into the live dict; raises ValueError on any failure."""
    try:
        with open(config["_config_path"]) as f:
            new_config = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse config.json: {e}")
    except FileNotFoundError:
        raise ValueError("config.json not found")
    _validate(new_config)
    new_config["_workdir"] = config["_workdir"]
    new_config["_config_path"] = config["_config_path"]
    config.clear()
    config.update(new_config)


def _find_config():
    for candidate in [
        Path("config.json"),
        Path(__file__).parent.parent.parent / "config.json",
    ]:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("config.json not found")


def _validate(config):
    required = [
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
    ]
    for key in required:
        if key not in config:
            raise ValueError(f"Missing config key: {key}")

    if not isinstance(config["telegram_user_id"], int):
        raise ValueError("telegram_user_id must be an integer")
    if not isinstance(config["monitoring_interval"], (int, float)):
        raise ValueError("monitoring_interval must be a number")
    if config["monitoring_interval"] <= 0:
        raise ValueError("monitoring_interval must be greater than 0")
    if not isinstance(config["timezone"], str) or not config["timezone"]:
        raise ValueError("timezone must be a non-empty string")
    try:
        ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, KeyError):
        raise ValueError(f"Invalid timezone: {config['timezone']!r}")
    if not isinstance(config["bot_telegram_api"], str) or not config["bot_telegram_api"]:
        raise ValueError("bot_telegram_api must be a non-empty string")
    if not isinstance(config["twitch_client_id"], str) or not config["twitch_client_id"]:
        raise ValueError("twitch_client_id must be a non-empty string")
    if not isinstance(config["twitch_client_secret"], str) or not config["twitch_client_secret"]:
        raise ValueError("twitch_client_secret must be a non-empty string")
    if not isinstance(config["plugin_dir"], str) or not config["plugin_dir"]:
        raise ValueError("plugin_dir must be a non-empty string")
    if not isinstance(config["recording_dir"], str) or not config["recording_dir"]:
        raise ValueError("recording_dir must be a non-empty string")

    if not isinstance(config["channels"], list) or not config["channels"]:
        raise ValueError("channels must be a non-empty list")
    for ch in config["channels"]:
        if not isinstance(ch, str) or not _CHANNEL_RE.match(ch):
            raise ValueError(f"Invalid channel name: {ch!r}")

    if not isinstance(config["proxy_list"], list) or not config["proxy_list"]:
        raise ValueError("proxy_list must be a non-empty list")
    for proxy in config["proxy_list"]:
        if not isinstance(proxy, str) or not _PROXY_RE.match(proxy):
            raise ValueError(f"Invalid proxy URL: {proxy!r}")

    config.setdefault("retention_days", 0)
    if not isinstance(config["retention_days"], (int, float)) or config["retention_days"] < 0:
        raise ValueError("retention_days must be a non-negative number (0 disables cleanup)")

    _validate_output_mode(config)
    _validate_channel_output_modes(config)
    _validate_youtube(config)
    _validate_update_check(config)
    _validate_quality(config)
    _validate_concurrency(config)
    _validate_disk(config)
    _validate_chat(config)


def _validate_output_mode(config):
    config.setdefault("output_mode", "disk")
    valid_modes = {"disk", "youtube", "both"}
    if config["output_mode"] not in valid_modes:
        raise ValueError(f"output_mode must be one of {valid_modes}, got {config['output_mode']!r}")


def _validate_channel_output_modes(config):
    config.setdefault("channel_output_modes", {})
    modes = config["channel_output_modes"]
    if not isinstance(modes, dict):
        raise ValueError("channel_output_modes must be an object")
    valid_modes = {"disk", "youtube", "both"}
    for ch, mode in modes.items():
        if not isinstance(ch, str) or not _CHANNEL_RE.match(ch):
            raise ValueError(f"Invalid channel name in channel_output_modes: {ch!r}")
        if not isinstance(mode, str) or mode not in valid_modes:
            raise ValueError(f"output_mode for {ch} must be one of {valid_modes}, got {mode!r}")


def _validate_youtube(config):
    if config.get("youtube") is None:
        config["youtube"] = {}
    yt = config["youtube"]
    valid_privacy = {"public", "unlisted", "private"}
    if not isinstance(yt, dict):
        raise ValueError("youtube config must be an object")
    yt.setdefault("privacy_status", "unlisted")
    if yt["privacy_status"] not in valid_privacy:
        raise ValueError(f"youtube.privacy_status must be one of {valid_privacy}, got {yt['privacy_status']!r}")
    yt.setdefault("client_secrets_file", "client_secret.json")
    if not isinstance(yt["client_secrets_file"], str) or not yt["client_secrets_file"]:
        raise ValueError("youtube.client_secrets_file must be a non-empty string")


def _validate_update_check(config):
    config.setdefault("update_check", {})
    uc = config["update_check"]
    if not isinstance(uc, dict):
        raise ValueError("update_check must be an object")
    uc.setdefault("enabled", True)
    uc.setdefault("interval_hours", 24)
    uc.setdefault("check_app", True)
    uc.setdefault("check_streamlink", True)
    uc.setdefault("check_plugin", True)
    if not isinstance(uc["enabled"], bool):
        raise ValueError("update_check.enabled must be a boolean")
    if not isinstance(uc["interval_hours"], (int, float)) or uc["interval_hours"] <= 0:
        raise ValueError("update_check.interval_hours must be a number greater than 0")
    for key in ("check_app", "check_streamlink", "check_plugin"):
        if not isinstance(uc[key], bool):
            raise ValueError(f"update_check.{key} must be a boolean")


def _validate_quality(config):
    config.setdefault("preferred_quality", "best")
    if not isinstance(config["preferred_quality"], str) or not config["preferred_quality"]:
        raise ValueError("preferred_quality must be a non-empty string")


def _validate_concurrency(config):
    config.setdefault("max_concurrent_recordings", 0)
    config.setdefault("max_concurrent_youtube_streams", 0)
    for key in ("max_concurrent_recordings", "max_concurrent_youtube_streams"):
        if not isinstance(config[key], (int, float)) or config[key] < 0:
            raise ValueError(f"{key} must be a number >= 0 (0 = unlimited)")


def _validate_disk(config):
    config.setdefault("disk", {})
    d = config["disk"]
    if not isinstance(d, dict):
        raise ValueError("disk must be an object")
    d.setdefault("min_free_gb", 0)
    d.setdefault("max_total_gb", 0)
    d.setdefault("check_interval_s", 60)
    d.setdefault("min_time_to_full_min", 0)
    d.setdefault("evict_when_over", True)
    if not isinstance(d["min_free_gb"], (int, float)) or d["min_free_gb"] < 0:
        raise ValueError("disk.min_free_gb must be a number >= 0 (0 = disabled)")
    if not isinstance(d["max_total_gb"], (int, float)) or d["max_total_gb"] < 0:
        raise ValueError("disk.max_total_gb must be a number >= 0 (0 = disabled)")
    if not isinstance(d["check_interval_s"], (int, float)) or d["check_interval_s"] <= 0:
        raise ValueError("disk.check_interval_s must be a number > 0")
    if not isinstance(d["min_time_to_full_min"], (int, float)) or d["min_time_to_full_min"] < 0:
        raise ValueError("disk.min_time_to_full_min must be a number >= 0 (0 = disabled)")
    if not isinstance(d["evict_when_over"], bool):
        raise ValueError("disk.evict_when_over must be a boolean")


def _validate_chat(config):
    config.setdefault("record_chat", True)
    if not isinstance(config["record_chat"], bool):
        raise ValueError("record_chat must be a boolean")
    config.setdefault("chat_dir", "chat")
    if not isinstance(config["chat_dir"], str) or not config["chat_dir"]:
        raise ValueError("chat_dir must be a non-empty string")
