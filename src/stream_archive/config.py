import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_CHANNEL_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_]{0,24}$')
_KICK_CHANNEL_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,24}$')  # slug: 1-25 chars, alnum start, then alnum/_/-
_PROXY_RE = re.compile(r'^(https?|httpproxy)://')

KICK_PREFIX = "kick:"
TWITCH_PREFIX = "twitch:"


def is_kick_channel(channel: str) -> bool:
    return channel.startswith(KICK_PREFIX)


def bare_name(channel: str) -> str:
    """Channel identity without the platform prefix (kick:xqc -> xqc)."""
    for prefix in (KICK_PREFIX, TWITCH_PREFIX):
        if channel.startswith(prefix):
            return channel[len(prefix):]
    return channel


def kick_bare_name(channel: str) -> str:
    return bare_name(channel)


def channel_url(channel: str) -> str:
    """Public profile URL used in notifications."""
    return f"https://kick.com/{bare_name(channel)}" if is_kick_channel(channel) else f"https://twitch.tv/{bare_name(channel)}"


def normalize_channel_name(name: str) -> str | None:
    """Canonical monitored-channel identity; None when invalid.

    Accepts bare names, platform-prefixed names, and profile URLs. Canonical
    form is prefixed: bare/twitch:<x>/https://twitch.tv/x -> twitch:<x>;
    kick:<x>/https://kick.com/x -> kick:<x.lower()>."""
    name = name.strip()
    if name.startswith(("http://", "https://")):
        try:
            parts = urlsplit(name)
        except ValueError:
            return None
        host = (parts.hostname or "").lower()
        path = parts.path.strip("/")
        if host in ("twitch.tv", "www.twitch.tv"):
            return f"twitch:{path}" if _CHANNEL_RE.match(path) else None
        if host in ("kick.com", "www.kick.com"):
            return f"kick:{path.lower()}" if _KICK_CHANNEL_RE.match(path) else None
        return None
    lower = name.lower()
    if lower.startswith("kick:"):
        bare = name[len(KICK_PREFIX):]
        return f"kick:{bare.lower()}" if _KICK_CHANNEL_RE.match(bare) else None
    if lower.startswith("twitch:"):
        bare = name[len(TWITCH_PREFIX):]
        return f"twitch:{bare}" if _CHANNEL_RE.match(bare) else None
    return f"twitch:{name}" if _CHANNEL_RE.match(name) else None


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
    normalized = []
    for ch in config["channels"]:
        if not isinstance(ch, str):
            raise ValueError(f"Invalid channel name: {ch!r}")
        norm = normalize_channel_name(ch)
        if norm is None:
            raise ValueError(f"Invalid channel name: {ch!r}")
        normalized.append(norm)
    config["channels"] = normalized

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
    _validate_eventsub(config)
    _validate_kick(config)


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
    normalized_modes = {}
    for ch, mode in modes.items():
        if not isinstance(ch, str):
            raise ValueError(f"Invalid channel name in channel_output_modes: {ch!r}")
        norm = normalize_channel_name(ch)
        if norm is None:
            raise ValueError(f"Invalid channel name in channel_output_modes: {ch!r}")
        if not isinstance(mode, str) or mode not in valid_modes:
            raise ValueError(f"output_mode for {ch} must be one of {valid_modes}, got {mode!r}")
        normalized_modes[norm] = mode
    config["channel_output_modes"] = normalized_modes


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
    d.pop("min_free_gb", None)
    d.pop("min_time_to_full_min", None)
    d.setdefault("max_total_gb", 0)
    d.setdefault("check_interval_s", 60)
    d.setdefault("delete_oldest", True)
    if not isinstance(d["max_total_gb"], (int, float)) or d["max_total_gb"] < 0:
        raise ValueError("disk.max_total_gb must be a number >= 0 (0 = disabled)")
    if not isinstance(d["check_interval_s"], (int, float)) or d["check_interval_s"] <= 0:
        raise ValueError("disk.check_interval_s must be a number > 0")
    if not isinstance(d["delete_oldest"], bool):
        raise ValueError("disk.delete_oldest must be a boolean")


def _validate_chat(config):
    config.setdefault("record_chat", True)
    if not isinstance(config["record_chat"], bool):
        raise ValueError("record_chat must be a boolean")
    config.setdefault("chat_dir", "chat")
    if not isinstance(config["chat_dir"], str) or not config["chat_dir"]:
        raise ValueError("chat_dir must be a non-empty string")


def _validate_eventsub(config):
    config.setdefault("eventsub", {})
    es = config["eventsub"]
    if not isinstance(es, dict):
        raise ValueError("eventsub must be an object")
    es.setdefault("enabled", True)
    if not isinstance(es["enabled"], bool):
        raise ValueError("eventsub.enabled must be a boolean")


def _validate_kick(config):
    config.setdefault("kick", {})
    kick = config["kick"]
    if not isinstance(kick, dict):
        raise ValueError("kick config must be an object")

    if any(is_kick_channel(ch) for ch in config["channels"]):
        cid = kick.get("client_id")
        if not isinstance(cid, str) or not cid:
            raise ValueError("kick.client_id is required when kick channels are configured")
        csec = kick.get("client_secret")
        if not isinstance(csec, str) or not csec:
            raise ValueError("kick.client_secret is required when kick channels are configured")

    kick.setdefault("record_chat", True)
    if not isinstance(kick["record_chat"], bool):
        raise ValueError("kick.record_chat must be a boolean")

    kick.setdefault("webhook", {})
    wh = kick["webhook"]
    if not isinstance(wh, dict):
        raise ValueError("kick.webhook must be an object")
    wh.setdefault("enabled", False)
    if not isinstance(wh["enabled"], bool):
        raise ValueError("kick.webhook.enabled must be a boolean")
    wh.setdefault("listen_host", "127.0.0.1")
    if not isinstance(wh["listen_host"], str) or not wh["listen_host"]:
        raise ValueError("kick.webhook.listen_host must be a non-empty string")
    wh.setdefault("listen_port", 8787)
    if (
        not isinstance(wh["listen_port"], int)
        or isinstance(wh["listen_port"], bool)
        or not 1 <= wh["listen_port"] <= 65535
    ):
        raise ValueError("kick.webhook.listen_port must be an integer in 1-65535")
    wh.setdefault("public_url", "")
    if not isinstance(wh["public_url"], str):
        raise ValueError("kick.webhook.public_url must be a string")
    if wh["enabled"] and not (
        wh["public_url"].startswith("http://") or wh["public_url"].startswith("https://")
    ):
        raise ValueError("kick.webhook.public_url is required when kick.webhook.enabled is true")
    wh.setdefault("setup_notified", False)
    if not isinstance(wh["setup_notified"], bool):
        raise ValueError("kick.webhook.setup_notified must be a boolean")
    wh.setdefault("tunnel", "")
    if wh["tunnel"] not in ("", "cloudflare", "tailscale"):
        raise ValueError("kick.webhook.tunnel must be one of '', 'cloudflare', 'tailscale'")
    wh.setdefault("cloudflare_token", "")
    if not isinstance(wh["cloudflare_token"], str):
        raise ValueError("kick.webhook.cloudflare_token must be a string")
    wh.setdefault("cloudflare_managed", False)
    if not isinstance(wh["cloudflare_managed"], bool):
        raise ValueError("kick.webhook.cloudflare_managed must be a boolean")
