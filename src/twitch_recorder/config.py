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
    _validate_youtube(config)


def _validate_output_mode(config):
    config.setdefault("output_mode", "disk")
    valid_modes = {"disk", "youtube", "both"}
    if config["output_mode"] not in valid_modes:
        raise ValueError(f"output_mode must be one of {valid_modes}, got {config['output_mode']!r}")


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
