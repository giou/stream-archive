"""HTTP control API for channels and settings, served on the webhook listener.

Routes live under ``/api/v1``. The listener belongs to ``KickWebhook``, so
the API is reachable through the same tunnel as the webhook and runs while
either feature is enabled. Every request needs the key from ``api.key``,
sent as ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``. When the
API is disabled, the routes answer 404 as if they did not exist.

The handlers call the Telegram command methods, which are the operation
layer of this app. They validate on a config copy and write config.json
atomically, so an API change takes effect on the next monitoring cycle,
exactly like a Telegram change. Rejected changes change nothing. Every
applied change also sends the admin a Telegram message, so the admin sees
what an API client did.
"""

import functools
import inspect
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web

from stream_archive.config import AppConfig, api_base_url, effective_quality, endpoint_base_url, normalize_channel_name
from stream_archive.updater import _installed_app_version

if TYPE_CHECKING:
    from stream_archive.telegram import TelegramController

logger = logging.getLogger(__name__)

#: Largest accepted request body. Settings payloads are tiny.
_MAX_BODY_BYTES = 64 * 1024

#: Markers of the Telegram command layer: rejected change, pending confirm.
_ERROR_MARK = "\u274c"
_CONFLICT_MARK = "\u26a0\ufe0f"

#: Global settings the API can write. Each maps to one Telegram command.
_SETTING_KEYS = (
    "output_mode",
    "preferred_quality",
    "retention_days",
    "max_concurrent_recordings",
    "max_concurrent_youtube_streams",
    "record_chat",
    "kick_record_chat",
    "disk_max_total_gb",
    "disk_delete_oldest",
)

#: Per-channel settings the API can write. Each maps to one Telegram command.
_CHANNEL_SETTING_KEYS = ("output_mode", "quality", "youtube_hold_seconds")

#: Output modes, plus 'default' to clear a per-channel override.
_OUTPUT_MODES = ("disk", "youtube", "both", "default")


class _ApiError(Exception):
    """One API failure that becomes a JSON error response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _plain(text: str) -> str:
    """Strip a leading status emoji from a command-layer message."""
    for mark in (_ERROR_MARK, _CONFLICT_MARK):
        if text.startswith(mark):
            return text[len(mark) :].lstrip()
    return text


def _request_token(request: web.Request) -> str | None:
    """API key from the Authorization header or X-API-Key, or None."""
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    key = request.headers.get("X-API-Key", "").strip()
    return key or None


def _settings_json(config: AppConfig) -> dict[str, Any]:
    """Public view of the global settings. It never includes a secret."""
    return {
        "output_mode": config.output_mode,
        "preferred_quality": config.preferred_quality,
        "retention_days": config.retention_days,
        "max_concurrent_recordings": config.max_concurrent_recordings,
        "max_concurrent_youtube_streams": config.max_concurrent_youtube_streams,
        "record_chat": config.record_chat,
        "kick_record_chat": config.kick.record_chat,
        "youtube": {
            "privacy_status": config.youtube.privacy_status,
            "hold_seconds": config.youtube.hold_seconds,
        },
        "disk": {
            "max_total_gb": config.disk.max_total_gb,
            "delete_oldest": config.disk.delete_oldest,
        },
        "endpoint": {
            "enabled": config.endpoint.enabled,
            "tunnel": config.endpoint.tunnel,
            "public_url": endpoint_base_url(config),
        },
        "kick_webhook": {"enabled": config.kick.webhook.enabled},
        "api": {"enabled": config.api.enabled, "base_url": api_base_url(config)},
        "monitoring_interval_s": config.monitoring_interval,
    }


def _channel_json(config: AppConfig, channel: str, recording: bool) -> dict[str, Any]:
    """One monitored channel: effective settings plus its overrides."""
    return {
        "channel": channel,
        "recording": recording,
        "output_mode": config.channel_output_modes.get(channel, config.output_mode),
        "output_mode_override": config.channel_output_modes.get(channel),
        "quality": effective_quality(config, channel),
        "quality_override": config.channel_preferred_qualities.get(channel),
        "youtube_hold_seconds": config.channel_youtube_hold_seconds.get(channel, config.youtube.hold_seconds),
        "youtube_hold_seconds_override": config.channel_youtube_hold_seconds.get(channel),
    }


def _scalar(value: Any, key: str) -> str:
    """Text form of a JSON string or number, for a command that parses text."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        msg = f"{key} must be a string or a number"
        raise _ApiError(400, msg)
    return str(value)


def _switch(value: Any, key: str) -> str:
    """Command word for a JSON boolean setting."""
    if not isinstance(value, bool):
        msg = f"{key} must be true or false"
        raise _ApiError(400, msg)
    return "on" if value else "off"


def _mode(value: Any) -> str:
    """Validated output mode, or 'default' to clear a per-channel override."""
    if not isinstance(value, str) or value.lower() not in _OUTPUT_MODES:
        msg = "output_mode must be one of disk, youtube, both, default"
        raise _ApiError(400, msg)
    return value.lower()


def _quality(value: Any) -> str:
    """Validated quality string, or 'default' to clear a per-channel override."""
    if not isinstance(value, str) or not value.strip():
        msg = "quality must be a non-empty string"
        raise _ApiError(400, msg)
    return value.strip()


def _hold_seconds(value: Any) -> str:
    """Validated YouTube hold delay: a whole number of seconds, or 'default'."""
    if isinstance(value, str) and value.strip().lower() == "default":
        return "default"
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0 or int(value) != value:
        msg = "youtube_hold_seconds must be a whole number of seconds >= 0, or 'default'"
        raise _ApiError(400, msg)
    return str(int(value))


class ControlAPI:
    """Serve /api/v1 on the shared listener and apply changes like the bot."""

    def __init__(self, config: AppConfig, controller: TelegramController, recorder: Any) -> None:
        self._config = config
        self._ctrl = controller
        self._recorder = recorder

    def register_routes(self, kick_webhook: Any) -> None:
        """Add the API routes to the Kick webhook listener.

        Call before the listener starts. One application serves the webhook
        and the API.
        """
        kick_webhook.add_routes(self._register)

    def _register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/status", self._guarded(self._status))
        app.router.add_get("/api/v1/settings", self._guarded(self._settings))
        app.router.add_patch("/api/v1/settings", self._guarded(self._patch_settings))
        app.router.add_get("/api/v1/channels", self._guarded(self._channels))
        app.router.add_post("/api/v1/channels", self._guarded(self._add_channel))
        app.router.add_get("/api/v1/channels/{channel}", self._guarded(self._channel))
        app.router.add_patch("/api/v1/channels/{channel}", self._guarded(self._patch_channel))
        app.router.add_delete("/api/v1/channels/{channel}", self._guarded(self._remove_channel))

    def _guarded(self, handler: Any) -> Any:
        """Wrap one handler with key checks and JSON error responses."""

        async def wrapper(request: web.Request) -> web.StreamResponse:
            try:
                self._check_key(request)
                return cast(web.StreamResponse, await handler(request))
            except _ApiError as e:
                headers = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
                return web.json_response({"error": e.message}, status=e.status, headers=headers)

        wrapper.__name__ = handler.__name__
        return wrapper

    def _check_key(self, request: web.Request) -> None:
        """Reject a request without the current API key. A disabled API is absent."""
        cfg = self._config.api
        if not cfg.enabled or not cfg.key:
            msg = "not found"
            raise _ApiError(404, msg)
        token = _request_token(request)
        if token is None or not secrets.compare_digest(token, cfg.key):
            logger.warning("[api] rejected request from %s: bad or missing API key", request.remote)
            msg = "unauthorized"
            raise _ApiError(401, msg)

    async def _read_json(self, request: web.Request) -> dict[str, Any]:
        """Parse the JSON object body, or raise a 400/413."""
        if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:
            msg = "request body too large"
            raise _ApiError(413, msg)
        raw = await request.content.read(_MAX_BODY_BYTES + 1)
        if len(raw) > _MAX_BODY_BYTES:
            msg = "request body too large"
            raise _ApiError(413, msg)
        if not raw:
            msg = "JSON body required"
            raise _ApiError(400, msg)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            msg = f"invalid JSON body: {e}"
            raise _ApiError(400, msg) from e
        if not isinstance(payload, dict):
            msg = "JSON body must be an object"
            raise _ApiError(400, msg)
        return payload

    def _known_channel(self, raw: str) -> str:
        """Normalized monitored channel name, or raise a 400/404."""
        channel = normalize_channel_name(raw)
        if channel is None:
            msg = f"invalid channel name: {raw!r} (use twitch:<name> or kick:<name>)"
            raise _ApiError(400, msg)
        if channel not in self._config.channels:
            msg = f"{channel} is not monitored"
            raise _ApiError(404, msg)
        return channel

    async def _notify(self, messages: Iterable[str]) -> None:
        """Tell the admin about applied API changes. A failure never fails the request."""
        await self._ctrl._notify_api_changes(list(messages))

    async def _run(self, method: Any, args: list[Any]) -> str:
        """Run one Telegram command method and return its message.

        A rejected change becomes a 400. The audio-only prompt of the
        quality commands becomes a 409: it waits for a Telegram confirm
        press, so the API must not report it as applied.
        """
        out: Any = method(args)
        if inspect.isawaitable(out):
            out = await out
        text = str(out)
        if text.startswith(_ERROR_MARK):
            raise _ApiError(400, _plain(text))
        if text.startswith(_CONFLICT_MARK):
            raise _ApiError(409, _plain(text))
        return text

    async def _status(self, request: web.Request) -> web.Response:
        """Service summary: version, channel count, active recordings."""
        return web.json_response(
            {
                "version": _installed_app_version() or "unknown",
                "channels": len(self._config.channels),
                "recording": self._recorder.active_channels(),
                "monitoring_interval_s": self._config.monitoring_interval,
            }
        )

    async def _settings(self, request: web.Request) -> web.Response:
        """Current global settings."""
        return web.json_response(_settings_json(self._config))

    async def _patch_settings(self, request: web.Request) -> web.Response:
        """Apply the given global settings, one command per key."""
        payload = await self._read_json(request)
        unknown = sorted(set(payload) - set(_SETTING_KEYS))
        if unknown:
            msg = "unknown setting(s): " + ", ".join(unknown)
            raise _ApiError(400, msg)
        applied, errors, status = await self._apply_each(payload, self._apply_setting)
        await self._notify(applied.values())
        body = {"applied": applied, "errors": errors, "settings": _settings_json(self._config)}
        return web.json_response(body, status=status)

    async def _apply_setting(self, key: str, value: Any) -> str:
        """Apply one global setting through its Telegram command."""
        ctrl = self._ctrl
        if key == "output_mode":
            return await self._run(ctrl.handle_mode, [_mode(value)])
        if key == "preferred_quality":
            return await self._run(ctrl.handle_quality, [_quality(value)])
        if key == "retention_days":
            return await self._run(ctrl.handle_retention, [_scalar(value, key)])
        if key == "max_concurrent_recordings":
            return await self._run(ctrl.handle_maxrecordings, [_scalar(value, key)])
        if key == "max_concurrent_youtube_streams":
            return await self._run(ctrl.handle_maxyoutube, [_scalar(value, key)])
        if key == "record_chat":
            return await self._run(ctrl.handle_chat, [_switch(value, key), "twitch"])
        if key == "kick_record_chat":
            return await self._run(ctrl.handle_chat, [_switch(value, key), "kick"])
        if key == "disk_max_total_gb":
            return await self._run(ctrl.handle_disk, ["maxsize", _scalar(value, key)])
        if key == "disk_delete_oldest":
            return await self._run(ctrl.handle_disk, ["delete_oldest", _switch(value, key)])
        msg = f"unknown setting: {key}"
        raise _ApiError(400, msg)

    async def _apply_each(
        self, payload: dict[str, Any], apply: Callable[[str, Any], Awaitable[str]]
    ) -> tuple[dict[str, str], dict[str, str], int]:
        """Run one command per key. One failure never blocks the other keys.

        Returns ``(applied, errors, status)``. The status is 200 when every
        key applied, 409 when every failure is the audio-only prompt, and
        400 otherwise.
        """
        applied: dict[str, str] = {}
        errors: dict[str, str] = {}
        conflicts_only = True
        for key, value in payload.items():
            try:
                applied[key] = await apply(key, value)
            except _ApiError as e:
                errors[key] = e.message
                conflicts_only = conflicts_only and e.status == 409
        if not errors:
            return applied, errors, 200
        return applied, errors, 409 if conflicts_only else 400

    async def _channels(self, request: web.Request) -> web.Response:
        """Every monitored channel with its effective settings."""
        return web.json_response({"channels": [self._channel_state(ch) for ch in self._config.channels]})

    async def _channel(self, request: web.Request) -> web.Response:
        """One monitored channel."""
        channel = self._known_channel(request.match_info["channel"])
        return web.json_response(self._channel_state(channel))

    def _channel_state(self, channel: str) -> dict[str, Any]:
        return _channel_json(self._config, channel, self._recorder.is_recording(channel))

    async def _add_channel(self, request: web.Request) -> web.Response:
        """Start monitoring a channel."""
        payload = await self._read_json(request)
        raw = payload.get("channel")
        if not isinstance(raw, str) or not raw.strip():
            msg = "channel is required"
            raise _ApiError(400, msg)
        channel = normalize_channel_name(raw.strip())
        if channel is None:
            msg = f"invalid channel name: {raw!r} (use twitch:<name>, kick:<name>, or a profile URL)"
            raise _ApiError(400, msg)
        message = await self._run(self._ctrl.handle_add, [channel])
        await self._notify([message])
        return web.json_response({"message": message, "channel": channel, "channels": list(self._config.channels)})

    async def _remove_channel(self, request: web.Request) -> web.Response:
        """Stop monitoring a channel. A live recording stops."""
        channel = self._known_channel(request.match_info["channel"])
        message = await self._run(self._ctrl.handle_remove, [channel])
        await self._notify([message])
        return web.json_response({"message": message, "channel": channel, "channels": list(self._config.channels)})

    async def _patch_channel(self, request: web.Request) -> web.Response:
        """Apply the given per-channel settings, one command per key."""
        channel = self._known_channel(request.match_info["channel"])
        payload = await self._read_json(request)
        unknown = sorted(set(payload) - set(_CHANNEL_SETTING_KEYS))
        if unknown:
            msg = "unknown setting(s): " + ", ".join(unknown)
            raise _ApiError(400, msg)
        applied, errors, status = await self._apply_each(
            payload, functools.partial(self._apply_channel_setting, channel)
        )
        await self._notify(applied.values())
        body = {"channel": channel, "applied": applied, "errors": errors}
        return web.json_response(body, status=status)

    async def _apply_channel_setting(self, channel: str, key: str, value: Any) -> str:
        """Apply one per-channel setting through its Telegram command."""
        ctrl = self._ctrl
        if key == "output_mode":
            return await self._run(ctrl.handle_mode, [channel, _mode(value)])
        if key == "quality":
            return await self._run(ctrl.handle_quality, [channel, _quality(value)])
        if key == "youtube_hold_seconds":
            return await self._run(ctrl.handle_channel_hold, [channel, _hold_seconds(value)])
        msg = f"unknown setting: {key}"
        raise _ApiError(400, msg)
