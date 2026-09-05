import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urlsplit
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
_KICK_CHANNEL_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,24}$"
)  # slug: 1-25 chars, starts alphanumeric, then alphanumeric/_/-
_PROXY_RE = re.compile(r"^(https?|httpproxy)://")

KICK_PREFIX = "kick:"
TWITCH_PREFIX = "twitch:"
AUDIO_ONLY_QUALITY = "audio_only"

OutputMode = Literal["disk", "youtube", "both"]


_CONFIG_LOCK = threading.Lock()


def is_kick_channel(channel: str) -> bool:
    return channel.startswith(KICK_PREFIX)


def bare_name(channel: str) -> str:
    """Channel identity without the platform prefix, for example kick:xqc -> xqc."""
    for prefix in (KICK_PREFIX, TWITCH_PREFIX):
        if channel.startswith(prefix):
            return channel[len(prefix) :]
    return channel


def kick_bare_name(channel: str) -> str:
    return bare_name(channel)


def channel_url(channel: str) -> str:
    """Return the public profile URL for notifications."""
    return (
        f"https://kick.com/{bare_name(channel)}"
        if is_kick_channel(channel)
        else f"https://twitch.tv/{bare_name(channel)}"
    )


def effective_quality(config: AppConfig, channel: str) -> str:
    """Per-channel quality override, or the global preferred_quality."""
    return config.channel_preferred_qualities.get(channel, config.preferred_quality)


def normalize_channel_name(name: str) -> str | None:
    """Canonical monitored-channel identity, or None when invalid.

    Accepts bare names, platform-prefixed names, and profile URLs. The
    canonical form carries a platform prefix. bare, twitch:<x>, and
    https://twitch.tv/x map to twitch:<x>. kick:<x> and
    https://kick.com/x map to kick:<x.lower()>."""
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


def _require_bool(v: object, label: str) -> bool:
    """Reject non-boolean values with an error that names the setting."""
    if not isinstance(v, bool):
        msg = f"{label} must be a boolean"
        raise ValueError(msg)
    return v


def _normalize_channel_map[V](raw: dict[str, V], setting: str) -> dict[str, V]:
    """Map each key through normalize_channel_name. Reject bad names."""
    normalized: dict[str, V] = {}
    for key, value in raw.items():
        norm = normalize_channel_name(key)
        if norm is None:
            msg = f"Invalid channel name in {setting}: {key!r}"
            raise ValueError(msg)
        if norm in normalized:
            msg = f"Duplicate channel: {norm!r}"
            raise ValueError(msg)
        normalized[norm] = value
    return normalized


class YouTubeConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    privacy_status: Literal["public", "unlisted", "private"] = "unlisted"
    client_secrets_file: str = Field("client_secret.json", min_length=1)
    hold_seconds: float = Field(0, ge=0)


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
        return _require_bool(v, f"update_check.{info.field_name}")


class DiskConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    max_total_gb: float = Field(0, ge=0)
    check_interval_s: float = Field(60, gt=0)
    delete_oldest: bool = True

    @field_validator("delete_oldest", mode="before")
    @classmethod
    def _bool_only(cls, v: Any) -> bool:
        return _require_bool(v, "disk.delete_oldest")


class EventSubConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    enabled: bool = True

    @field_validator("enabled", mode="before")
    @classmethod
    def _bool_only(cls, v: Any) -> bool:
        return _require_bool(v, "eventsub.enabled")


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
        return _require_bool(v, f"kick.webhook.{info.field_name}")

    @field_validator("tunnel")
    @classmethod
    def _tunnel_only(cls, v: Any) -> str:
        if v not in ("", "cloudflare", "tailscale"):
            msg = "kick.webhook.tunnel must be one of '', 'cloudflare', 'tailscale'"
            raise ValueError(msg)
        out: str = v
        return out

    @model_validator(mode="after")
    def _require_public_url_when_enabled(self) -> KickWebhookConfig:
        if self.enabled:
            parts = urlparse(self.public_url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                msg = "kick.webhook.public_url is required when kick.webhook.enabled is true"
                raise ValueError(msg)
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
        return _require_bool(v, "kick.record_chat")


class AppConfig(BaseModel):
    """Typed, validated view of config.json.

    All optional settings carry the same defaults the dict-based validator
    applied. Required keys missing from the file fail validation with a
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
    channel_youtube_hold_seconds: dict[str, float] = {}
    channel_preferred_qualities: dict[str, str] = {}
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

    @property
    def workdir(self) -> Path:
        """Bound working directory. Raise when the config is unbound."""
        with _CONFIG_LOCK:
            return _bound_workdir(self)

    @property
    def config_path(self) -> Path:
        """Bound source path. Raise when the config is unbound."""
        with _CONFIG_LOCK:
            return _bound_config_path(self)

    @field_validator("channels")
    @classmethod
    def _normalize_channels(cls, v: list[str]) -> list[str]:
        normalized = []
        for ch in v:
            norm = normalize_channel_name(ch)
            if norm is None:
                msg = f"Invalid channel name: {ch!r}"
                raise ValueError(msg)
            if norm in normalized:
                msg = f"Duplicate channel: {norm!r}"
                raise ValueError(msg)
            normalized.append(norm)
        return normalized

    @field_validator("proxy_list")
    @classmethod
    def _valid_proxies(cls, v: list[str]) -> list[str]:
        for proxy in v:
            if not _PROXY_RE.match(proxy):
                msg = f"Invalid proxy URL: {proxy!r}"
                raise ValueError(msg)
            if not urlparse(proxy).hostname:
                msg = f"Invalid proxy URL: {proxy!r}"
                raise ValueError(msg)
        return v

    @field_validator("channel_output_modes")
    @classmethod
    def _normalize_output_mode_keys(cls, v: dict[str, OutputMode]) -> dict[str, OutputMode]:
        return _normalize_channel_map(v, "channel_output_modes")

    @field_validator("channel_preferred_qualities")
    @classmethod
    def _normalize_quality_keys(cls, v: dict[str, str]) -> dict[str, str]:
        for ch, quality in v.items():
            if not quality.strip():
                msg = f"channel_preferred_qualities.{ch} must be a non-empty quality string"
                raise ValueError(msg)
        return _normalize_channel_map(v, "channel_preferred_qualities")

    @field_validator("channel_youtube_hold_seconds")
    @classmethod
    def _normalize_hold_keys(cls, v: dict[str, float]) -> dict[str, float]:
        for ch, seconds in v.items():
            if seconds < 0:
                msg = f"channel_youtube_hold_seconds.{ch} must be >= 0"
                raise ValueError(msg)
        return _normalize_channel_map(v, "channel_youtube_hold_seconds")

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError, KeyError:
            msg = f"Invalid timezone: {v!r}"
            raise ValueError(msg) from None
        return v

    @field_validator("record_chat", mode="before")
    @classmethod
    def _bool_only(cls, v: Any) -> bool:
        return _require_bool(v, "record_chat")

    @field_validator("preferred_quality")
    @classmethod
    def _non_blank_preferred_quality(cls, v: str) -> str:
        if not v.strip():
            msg = "preferred_quality must be a non-empty quality string"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _require_kick_creds(self) -> AppConfig:
        if any(is_kick_channel(c) for c in self.channels):
            if not self.kick.client_id.strip():
                msg = "kick.client_id is required when kick channels are configured"
                raise ValueError(msg)
            if not self.kick.client_secret.strip():
                msg = "kick.client_secret is required when kick channels are configured"
                raise ValueError(msg)
        return self


def _bound_workdir(config: AppConfig) -> Path:
    """Return the bound working directory. Callers hold _CONFIG_LOCK."""
    try:
        workdir: Path | None = config._workdir
    except AttributeError:
        workdir = None
    if workdir is None:
        msg = "AppConfig is not bound to a config path"
        raise RuntimeError(msg)
    return workdir


def _bound_config_path(config: AppConfig) -> Path:
    """Return the bound source path. Callers hold _CONFIG_LOCK."""
    try:
        path: Path | None = config._config_path
    except AttributeError:
        path = None
    if path is None:
        msg = "AppConfig is not bound to a config path"
        raise RuntimeError(msg)
    return path


def _bind(
    cfg: AppConfig,
    workdir: Path,
    config_path: Path,
    placeholders: dict[tuple[Any, ...], str],
) -> None:
    """Attach file locations and env placeholders to a validated config."""
    cfg._workdir = workdir
    cfg._config_path = config_path
    cfg._env_placeholders = placeholders


def _load_json_file(config_path: Path) -> Any:
    """Parse a JSON config file. Raise ValueError that names the path."""
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError as e:
        logger.error("config.json not found in %s", config_path.parent)
        msg = f"{config_path}: config file not found"
        raise ValueError(msg) from e
    except PermissionError as e:
        logger.error("Cannot read config.json: %s", e)
        msg = f"{config_path}: permission denied"
        raise ValueError(msg) from e
    except UnicodeDecodeError as e:
        logger.error("Failed to parse config.json: %s", e)
        msg = f"{config_path}: invalid encoding: {e}"
        raise ValueError(msg) from e
    except json.JSONDecodeError as e:
        logger.error("Failed to parse config.json: %s", e)
        msg = f"{config_path}: invalid JSON: {e}"
        raise ValueError(msg) from e
    return raw


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(data: Any, path: tuple[Any, ...] = ()) -> tuple[Any, dict[tuple[Any, ...], str]]:
    """Resolve ``${ENV_VAR}`` references in every string value.

    Returns ``(data, placeholders)``. ``placeholders`` maps each
    interpolated value's config path to its original text, so
    ``save_config`` restores the placeholder and never writes resolved
    secrets. A missing variable raises ValueError naming the variable
    and the config key.
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
                    msg = f"Environment variable {name} not set (referenced at config key {'/'.join(map(str, p))})"
                    raise ValueError(msg) from None

            return _ENV_RE.sub(repl, node)
        return node

    return walk(data, path), placeholders


def _set_at(data: Any, path: tuple[Any, ...], value: str) -> None:
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _get_at(data: Any, path: tuple[Any, ...]) -> Any:
    node = data
    for key in path[:-1]:
        node = node[key]
    return node[path[-1]]


def get_config(path: Path | None = None) -> AppConfig:
    config_path = path or _find_config()
    raw = _load_json_file(config_path)
    data, placeholders = _interpolate_env(raw)
    cfg = AppConfig.model_validate(data)
    # Absolutize so relative settings (recordings/, chat/, tokens, state)
    # resolve against the config directory regardless of the process cwd.
    # In Docker the config lives inside the mounted data dir.
    _bind(cfg, config_path.parent.resolve(), config_path.resolve(), placeholders)
    return cfg


def save_config(config: AppConfig) -> None:
    """Validate the config and atomically write it to its source file."""
    with _CONFIG_LOCK:
        config_path = _bound_config_path(config)
        validated = AppConfig.model_validate(config.model_dump())  # catches invalid in-place mutations
        data = validated.model_dump()
        for key_path, raw in list(config._env_placeholders.items()):
            try:
                current = _get_at(data, key_path)
            except KeyError, IndexError, TypeError:
                # Pydantic dropped the key (extra='ignore'), so the output has
                # no value left to mask. Drop the tracker entry instead of
                # failing this save and every later save.
                del config._env_placeholders[key_path]
                continue
            try:
                resolved: str | None = _ENV_RE.sub(lambda m: os.environ[m.group(1)], raw)
            except ValueError, KeyError, TypeError:
                resolved = None  # env var vanished since load: mask rather than guess
            if resolved is None or current == resolved:
                # Untouched value, or an interpolation we can no longer compute:
                # write the ${VAR} placeholder back so the secret never reaches disk.
                _set_at(data, key_path, raw)
            else:
                # Deliberate bot-persisted literal: write it and drop the tracker
                # so later saves keep this value instead of reverting it to ${VAR}.
                del config._env_placeholders[key_path]
        tmp = Path(str(config_path) + ".tmp")
        try:
            existing_mode: int | None = config_path.stat().st_mode & 0o777
        except FileNotFoundError:
            existing_mode = None  # new file: keep the umask default
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            if existing_mode is not None:
                os.chmod(tmp, existing_mode)
            os.replace(tmp, config_path)
        except (FileNotFoundError, PermissionError) as e:
            msg = f"{config_path}: cannot write config: {e}"
            raise ValueError(msg) from e


def reload_config(config: AppConfig) -> None:
    """Re-read config.json from disk into the live instance.

    Raises ValueError on any failure.
    """
    with _CONFIG_LOCK:
        fresh = get_config(_bound_config_path(config))
        _copy_state(config, fresh)
        _bind(config, fresh._workdir, fresh._config_path, dict(fresh._env_placeholders))


def _replace_in_place(target: AppConfig, source: AppConfig) -> None:
    """Copy source's state onto target without changing target's identity.

    Every module holds the same config instance. A reload must replace
    the state while keeping that object identity.
    """
    with _CONFIG_LOCK:
        _copy_state(target, source)


def _copy_state(target: AppConfig, source: AppConfig) -> None:
    """Copy validated state between configs. Callers hold _CONFIG_LOCK."""
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
    msg = "config.json not found"
    raise FileNotFoundError(msg)
