import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictInt,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)

_CHANNEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_]{0,24}$")
_KICK_CHANNEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,24}$")  # slug: 1-25 chars, alnum start, then alnum/_/-
_PROXY_RE = re.compile(r"^(https?|httpproxy)://")

KICK_PREFIX = "kick:"
TWITCH_PREFIX = "twitch:"

OutputMode = Literal["disk", "youtube", "both"]


def is_kick_channel(channel: str) -> bool:
    return channel.startswith(KICK_PREFIX)


def bare_name(channel: str) -> str:
    """Channel identity without the platform prefix (kick:xqc -> xqc)."""
    for prefix in (KICK_PREFIX, TWITCH_PREFIX):
        if channel.startswith(prefix):
            return channel[len(prefix) :]
    return channel


def kick_bare_name(channel: str) -> str:
    return bare_name(channel)


def channel_url(channel: str) -> str:
    """Public profile URL used in notifications."""
    return (
        f"https://kick.com/{bare_name(channel)}"
        if is_kick_channel(channel)
        else f"https://twitch.tv/{bare_name(channel)}"
    )


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
        bare = name[len(KICK_PREFIX) :]
        return f"kick:{bare.lower()}" if _KICK_CHANNEL_RE.match(bare) else None
    if lower.startswith("twitch:"):
        bare = name[len(TWITCH_PREFIX) :]
        return f"twitch:{bare}" if _CHANNEL_RE.match(bare) else None
    return f"twitch:{name}" if _CHANNEL_RE.match(name) else None


class YouTubeConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    privacy_status: Literal["public", "unlisted", "private"] = "unlisted"
    client_secrets_file: str = Field("client_secret.json", min_length=1)


class UpdateCheckConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    enabled: bool = True
    interval_hours: float = Field(24, gt=0)
    check_app: bool = True
    check_streamlink: bool = True
    check_plugin: bool = True

    @field_validator("enabled", "check_app", "check_streamlink", "check_plugin", mode="before")
    @classmethod
    def _bool_only(cls, v: Any, info: ValidationInfo) -> bool:
        if not isinstance(v, bool):
            raise ValueError(f"update_check.{info.field_name} must be a boolean")
        return v


class DiskConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    max_total_gb: float = Field(0, ge=0)
    check_interval_s: float = Field(60, gt=0)
    delete_oldest: bool = True

    @field_validator("delete_oldest", mode="before")
    @classmethod
    def _bool_only(cls, v: Any) -> bool:
        if not isinstance(v, bool):
            raise ValueError("disk.delete_oldest must be a boolean")
        return v


class EventSubConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    enabled: bool = True

    @field_validator("enabled", mode="before")
    @classmethod
    def _bool_only(cls, v: Any) -> bool:
        if not isinstance(v, bool):
            raise ValueError("eventsub.enabled must be a boolean")
        return v


class KickWebhookConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    enabled: bool = False
    listen_host: str = Field("127.0.0.1", min_length=1)
    listen_port: StrictInt = Field(8787, ge=1, le=65535)
    public_url: StrictStr = ""
    setup_notified: bool = False
    tunnel: Literal["", "cloudflare", "tailscale"] = ""
    cloudflare_token: StrictStr = ""
    cloudflare_managed: bool = False

    @field_validator("enabled", "setup_notified", "cloudflare_managed", mode="before")
    @classmethod
    def _bool_only(cls, v: Any, info: ValidationInfo) -> bool:
        if not isinstance(v, bool):
            raise ValueError(f"kick.webhook.{info.field_name} must be a boolean")
        return v

    @field_validator("tunnel")
    @classmethod
    def _tunnel_only(cls, v: Any) -> str:
        if v not in ("", "cloudflare", "tailscale"):
            raise ValueError("kick.webhook.tunnel must be one of '', 'cloudflare', 'tailscale'")
        return v

    @model_validator(mode="after")
    def _require_public_url_when_enabled(self) -> "KickWebhookConfig":
        if self.enabled and not (self.public_url.startswith("http://") or self.public_url.startswith("https://")):
            raise ValueError("kick.webhook.public_url is required when kick.webhook.enabled is true")
        return self


class KickConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    client_id: str = ""
    client_secret: str = ""
    record_chat: bool = True
    webhook: KickWebhookConfig = KickWebhookConfig()

    @field_validator("record_chat", mode="before")
    @classmethod
    def _bool_only(cls, v: Any) -> bool:
        if not isinstance(v, bool):
            raise ValueError("kick.record_chat must be a boolean")
        return v


class AppConfig(BaseModel):
    """Typed, validated view of config.json.

    All optional settings carry the same defaults the dict-based validator
    applied; required keys missing from the file fail validation with a
    pydantic.ValidationError (a ValueError subclass).
    """

    model_config = ConfigDict(validate_assignment=True)

    # required
    telegram_user_id: StrictInt
    bot_telegram_api: str = Field(min_length=1)
    twitch_client_id: str = Field(min_length=1)
    twitch_client_secret: str = Field(min_length=1)
    channels: list[str] = Field(min_length=1)
    proxy_list: list[str] = Field(min_length=1)
    monitoring_interval: float = Field(gt=0)
    timezone: str = Field(min_length=1)
    plugin_dir: str = Field(min_length=1)
    recording_dir: str = Field(min_length=1)

    # optional with defaults
    retention_days: float = Field(0, ge=0)
    output_mode: OutputMode = "disk"
    channel_output_modes: dict[str, OutputMode] = {}
    youtube: YouTubeConfig = YouTubeConfig()
    update_check: UpdateCheckConfig = UpdateCheckConfig()
    preferred_quality: str = Field("best", min_length=1)
    max_concurrent_recordings: float = Field(0, ge=0)
    max_concurrent_youtube_streams: float = Field(0, ge=0)
    record_chat: bool = True
    chat_dir: str = Field("chat", min_length=1)
    disk: DiskConfig = DiskConfig()
    eventsub: EventSubConfig = EventSubConfig()
    kick: KickConfig = KickConfig()

    _workdir: Path = PrivateAttr()
    _config_path: Path = PrivateAttr()
    _env_placeholders: dict[tuple[Any, ...], str] = PrivateAttr(default_factory=dict)

    @field_validator("channels")
    @classmethod
    def _normalize_channels(cls, v: list[str]) -> list[str]:
        normalized = []
        for ch in v:
            norm = normalize_channel_name(ch)
            if norm is None:
                raise ValueError(f"Invalid channel name: {ch!r}")
            normalized.append(norm)
        return normalized

    @field_validator("proxy_list")
    @classmethod
    def _valid_proxies(cls, v: list[str]) -> list[str]:
        for proxy in v:
            if not _PROXY_RE.match(proxy):
                raise ValueError(f"Invalid proxy URL: {proxy!r}")
        return v

    @field_validator("channel_output_modes")
    @classmethod
    def _normalize_output_mode_keys(cls, v: dict[str, OutputMode]) -> dict[str, OutputMode]:
        normalized: dict[str, OutputMode] = {}
        for ch, mode in v.items():
            norm = normalize_channel_name(ch)
            if norm is None:
                raise ValueError(f"Invalid channel name in channel_output_modes: {ch!r}")
            normalized[norm] = mode
        return normalized

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"Invalid timezone: {v!r}") from None
        return v

    @field_validator("record_chat", mode="before")
    @classmethod
    def _bool_only(cls, v: Any) -> bool:
        if not isinstance(v, bool):
            raise ValueError("record_chat must be a boolean")
        return v

    @model_validator(mode="after")
    def _require_kick_creds(self) -> "AppConfig":
        if any(is_kick_channel(c) for c in self.channels):
            if not self.kick.client_id:
                raise ValueError("kick.client_id is required when kick channels are configured")
            if not self.kick.client_secret:
                raise ValueError("kick.client_secret is required when kick channels are configured")
        return self


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(data: Any, path: tuple[Any, ...] = ()) -> tuple[Any, dict[tuple[Any, ...], str]]:
    """Resolve ``${ENV_VAR}`` in every string value; returns (data, placeholders).

    ``placeholders`` maps each interpolated value's config path to its original
    text, so ``save_config`` can restore it and never write resolved secrets.
    A missing variable raises ValueError naming the variable and config key.
    """

    placeholders: dict[tuple[Any, ...], str] = {}

    def walk(node: Any, p: tuple[Any, ...]) -> Any:
        if isinstance(node, dict):
            return {k: walk(v, p + (k,)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, p + (i,)) for i, v in enumerate(node)]
        if isinstance(node, str) and _ENV_RE.search(node):
            placeholders[p] = node

            def repl(match: re.Match[str]) -> str:
                name = match.group(1)
                try:
                    return os.environ[name]
                except KeyError:
                    raise ValueError(
                        f"Environment variable {name} not set (referenced at config key {'/'.join(map(str, p))})"
                    ) from None

            return _ENV_RE.sub(repl, node)
        return node

    return walk(data, path), placeholders


def _set_at(data: Any, path: tuple[Any, ...], value: str) -> None:
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def get_config(path: Path | None = None) -> AppConfig:
    config_path = path or _find_config()
    try:
        with open(config_path) as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse config.json: %s", e)
        raise
    except FileNotFoundError:
        logger.error("config.json not found in %s", config_path.parent)
        raise

    data, placeholders = _interpolate_env(raw)
    cfg = AppConfig.model_validate(data)
    # Absolutize so relative settings (recordings/, chat/, tokens, state) resolve
    # against the config's directory regardless of the process cwd — in Docker
    # the config lives in the mounted data dir.
    cfg._workdir = config_path.parent.resolve()
    cfg._config_path = config_path.resolve()
    cfg._env_placeholders = placeholders
    return cfg


def save_config(config: AppConfig) -> None:
    """Validate and atomically write config to the file it was loaded from."""
    validated = AppConfig.model_validate(config.model_dump())  # catches invalid in-place mutations
    data = validated.model_dump()
    for key_path, raw in config._env_placeholders.items():
        _set_at(data, key_path, raw)
    config_path = config._config_path
    tmp = Path(str(config_path) + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
        f.write("\n")
    os.replace(tmp, config_path)


def reload_config(config: AppConfig) -> None:
    """Re-read config.json from disk into the live instance; raises ValueError on any failure."""
    try:
        with open(config._config_path) as f:
            json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse config.json: {e}") from e
    except FileNotFoundError:
        raise ValueError("config.json not found") from None
    _replace_in_place(config, get_config(config._config_path))


def _replace_in_place(target: AppConfig, source: AppConfig) -> None:
    """Copy source's state onto target without changing target's object identity.

    Every module holds the same config instance; swaps must keep that identity.
    """
    for name in type(target).model_fields:
        object.__setattr__(target, name, getattr(source, name))
    object.__setattr__(target, "__pydantic_private__", dict(source.__pydantic_private__ or {}))


def _find_config() -> Path:
    for candidate in [
        Path("config.json"),
        Path(__file__).parent.parent.parent / "config.json",
    ]:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("config.json not found")
