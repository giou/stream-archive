"""HTTP control API for channels and settings, served on the private listener.

Routes live under ``/api/v1``. The listener belongs to ``KickWebhook``, so
the API runs while either feature is enabled. Every request needs the key from ``api.key``,
sent as ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``. When the
API is disabled, the routes answer 404 as if they did not exist.

The handlers call the Telegram command methods, which are the operation
layer of this app. They validate on a config copy and write config.json
atomically, so an API change takes effect on the next monitoring cycle,
exactly like a Telegram change. Rejected changes change nothing. Every
applied change also sends the admin a Telegram message, so the admin sees
what an API client did.
"""

import contextlib
import functools
import hmac
import inspect
import logging
import math
import secrets
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from aiohttp import web

from stream_archive.config import (
    AppConfig,
    api_base_url,
    apply_config_change,
    effective_quality,
    endpoint_base_url,
    normalize_channel_name,
    normalize_endpoint_url,
)
from stream_archive.http_guard import FailBudget, read_json_object
from stream_archive.telegram.menu_state import is_error
from stream_archive.updater import installed_app_version

if TYPE_CHECKING:
    from stream_archive.recorder import Recorder
    from stream_archive.telegram import TelegramController

logger = logging.getLogger(__name__)

#: Largest accepted request body. Settings payloads are tiny.
_MAX_BODY_BYTES = 64 * 1024

#: Failed key attempts per address before the API answers 429.
_KEY_MAX_FAILS = 10

#: Window of the key budget, in seconds.
_KEY_WINDOW_S = 600

#: Request slot for the authenticated origin ("Control API" or "Web panel").
_ORIGIN_KEY: web.RequestKey[str] = web.RequestKey("api_origin", str)

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
    "youtube_hold_seconds",
    "endpoint_enabled",
    "endpoint_public_url",
    "kick_webhook_enabled",
    "kick_webhook_public_url",
    "api_enabled",
)

#: Per-channel settings the API can write. Each maps to one Telegram command.
_CHANNEL_SETTING_KEYS = ("output_mode", "quality", "youtube_hold_seconds", "categories")

#: Output modes, plus 'default' to clear a per-channel override.
_OUTPUT_MODES = ("disk", "youtube", "both", "default")

#: Output modes for the global setting. 'default' clears a per-channel
#: override, so it has no meaning here.
_GLOBAL_OUTPUT_MODES = ("disk", "youtube", "both")


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
            "public_url": endpoint_base_url(config),
        },
        "kick_webhook": {
            "enabled": config.kick.webhook.enabled,
            "public_url": normalize_endpoint_url(config.kick.webhook.public_url),
        },
        "api": {"enabled": config.api.enabled, "base_url": api_base_url(config)},
        "monitoring_interval_s": config.monitoring_interval,
    }


def _channel_json(config: AppConfig, channel: str, recording: bool) -> dict[str, Any]:
    """One monitored channel: effective settings plus its overrides."""
    cats = config.channel_categories.get(channel)
    return {
        "channel": channel,
        "recording": recording,
        "output_mode": config.channel_output_modes.get(channel, config.output_mode),
        "output_mode_override": config.channel_output_modes.get(channel),
        "quality": effective_quality(config, channel),
        "quality_override": config.channel_preferred_qualities.get(channel),
        "youtube_hold_seconds": config.channel_youtube_hold_seconds.get(channel, config.youtube.hold_seconds),
        "youtube_hold_seconds_override": config.channel_youtube_hold_seconds.get(channel),
        "categories": list(cats) if cats is not None else None,
        "categories_override": list(cats) if cats is not None else None,
    }


def _scalar(value: Any, key: str) -> str:
    """Text form of a JSON string or number, for a command that parses text."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        msg = f"{key} must be a string or a number"
        raise _ApiError(400, msg)
    text = str(value)
    # The command layer parses the text again. JSON accepts the NaN and
    # Infinity literals, and the perfect number 1e999 parses to inf. The
    # setting would then persist as the non-standard Infinity token, which
    # strict JSON clients cannot read.
    try:
        number = float(text)
    except ValueError:
        return text  # not a number at all, so the command layer decides
    if not math.isfinite(number):
        msg = f"{key} must be a finite number"
        raise _ApiError(400, msg)
    return text


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


def _global_mode(value: Any) -> str:
    """Validated output mode for every channel without an override.

    The word ``default`` clears a per-channel override. It has no meaning
    as the global mode, so this method rejects it.
    """
    if not isinstance(value, str) or value.lower() not in _GLOBAL_OUTPUT_MODES:
        msg = "output_mode must be one of disk, youtube, both"
        raise _ApiError(400, msg)
    return value.lower()


def _quality(value: Any) -> str:
    """Validated quality string, or 'default' to clear a per-channel override."""
    if not isinstance(value, str) or not value.strip():
        msg = "quality must be a non-empty string"
        raise _ApiError(400, msg)
    return value.strip()


def _global_quality(value: Any) -> str:
    """Validated quality for every channel without an override.

    The word ``default`` clears a per-channel override. It has no meaning
    as the value of every channel, so this method rejects it.
    """
    quality = _quality(value)
    if quality.lower() == "default":
        msg = "preferred_quality must be a quality name, not 'default'"
        raise _ApiError(400, msg)
    return quality


def _hold_seconds(value: Any) -> str:
    """Validated YouTube hold delay: a whole number of seconds, or 'default'."""
    if isinstance(value, str) and value.strip().lower() == "default":
        return "default"
    # JSON accepts NaN and Infinity, and int() raises on both. Reject them here,
    # so the client gets a 400 instead of a 500.
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or (isinstance(value, float) and not math.isfinite(value))
        or value < 0
        or int(value) != value
    ):
        msg = "youtube_hold_seconds must be a whole number of seconds >= 0, or 'default'"
        raise _ApiError(400, msg)
    return str(int(value))


def _global_hold_seconds(value: Any) -> str:
    """Validated global YouTube hold delay: a whole number of seconds >= 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = "youtube_hold_seconds must be a whole number of seconds >= 0"
        raise _ApiError(400, msg)
    if isinstance(value, float) and (not math.isfinite(value) or int(value) != value):
        msg = "youtube_hold_seconds must be a whole number of seconds >= 0"
        raise _ApiError(400, msg)
    if value < 0:
        msg = "youtube_hold_seconds must be a whole number of seconds >= 0"
        raise _ApiError(400, msg)
    return str(int(value))


def _categories(value: Any) -> list[str] | str:
    """Validated category filter, or 'default' to clear it.

    The API takes a JSON list of names. A blank entry, a name longer
    than 100 characters, more than 50 names, or the word 'default' as a
    name all fail with 400. The word 'default' alone clears the filter.
    """
    from stream_archive.config import MAX_CATEGORIES_PER_CHANNEL, MAX_CATEGORY_LEN

    if isinstance(value, str):
        if value.strip().lower() != "default":
            msg = "categories must be a list of names, or 'default'"
            raise _ApiError(400, msg)
        return "default"
    if not isinstance(value, list):
        msg = "categories must be a list of names, or 'default'"
        raise _ApiError(400, msg)
    seen: set[str] = set()
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            msg = "categories must hold strings only"
            raise _ApiError(400, msg)
        name = entry.strip()
        if not name:
            msg = "categories must hold non-empty names"
            raise _ApiError(400, msg)
        if len(name) > MAX_CATEGORY_LEN:
            msg = f"{name!r} is longer than 100 characters"
            raise _ApiError(400, msg)
        if name.lower() == "default":
            msg = "'default' clears a filter, so it is not a value"
            raise _ApiError(400, msg)
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    if not out:
        msg = "categories must hold at least one name (use 'default' to clear it)"
        raise _ApiError(400, msg)
    if len(out) > MAX_CATEGORIES_PER_CHANNEL:
        msg = "categories must hold at most 50 names"
        raise _ApiError(400, msg)
    return out


def _public_url(value: Any, key: str, *, allow_empty: bool = False, require_dot: bool = False) -> str:
    """Validated public URL: http(s) with a hostname, without a path tail.

    With ``require_dot`` a single label never saves as a Kick entry, like
    the setup wizard enforces.
    """
    if not isinstance(value, str):
        msg = f"{key} must be a string"
        raise _ApiError(400, msg)
    if allow_empty and not value.strip():
        return ""
    text = value.strip()
    if "://" not in text:
        text = "https://" + text
    parts = urlparse(text)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        msg = f"{key} must be an http(s) URL with a hostname"
        raise _ApiError(400, msg)
    try:
        port = parts.port
    except ValueError:
        msg = f"{key} must be an http(s) URL with a usable port"
        raise _ApiError(400, msg) from None
    if port is not None and not 1 <= port <= 65535:
        msg = f"{key} must be an http(s) URL with a usable port"
        raise _ApiError(400, msg)
    if not all(char.isalnum() or char in ".-" for char in host) or not host.strip(".-"):
        msg = f"{key} must be an http(s) URL with a hostname"
        raise _ApiError(400, msg)
    if require_dot and "." not in host:
        msg = f"{key} must be a public hostname with a dot"
        raise _ApiError(400, msg)
    base = normalize_endpoint_url(text)
    bare = urlparse(base)
    if bare.path not in ("", "/") or bare.params or bare.query or bare.fragment or bare.username or bare.password:
        # A path, query, fragment, or userinfo would save verbatim and break
        # the derived URLs (webhook, API base). The known webhook path is
        # already stripped above, so any rest here is user data.
        msg = f"{key} must be a host URL without a path, query, or fragment"
        raise _ApiError(400, msg)
    return base


class ControlAPI:
    """Serve /api/v1 on the private listener and apply changes like the bot."""

    def __init__(self, config: AppConfig, controller: TelegramController, recorder: Recorder) -> None:
        self._config = config
        self._ctrl = controller
        self._recorder = recorder
        self._session_of: Callable[[web.Request], Any] | None = None
        self._key_budget = FailBudget(_KEY_MAX_FAILS, _KEY_WINDOW_S)

    def set_session_checker(self, checker: Callable[[web.Request], Any]) -> None:
        """Panel sessions that /api/v1 also accepts. Set once before serving.

        The checker returns the live session or None. The panel passes its
        own session lookup, so the panel and scripts share one API.
        """
        self._session_of = checker

    def register_routes(self, kick_webhook: Any) -> None:
        """Add the API routes to the private listener.

        Call before the listener starts. One application serves the panel
        and the API; the webhook listener carries POST /kick/webhook alone.
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
        app.router.add_post("/api/v1/kick/webhook/test", self._guarded(self._test_kick_delivery))
        app.router.add_get("/api/v1/api-key", self._guarded(self._show_api_key))
        app.router.add_post("/api/v1/api-key/rotate", self._guarded(self._rotate_api_key))

    def _guarded(self, handler: Any) -> Any:
        """Wrap one handler with key-or-session checks and JSON error responses."""

        async def wrapper(request: web.Request) -> web.StreamResponse:
            try:
                request[_ORIGIN_KEY] = self._authenticate(request)
                return cast(web.StreamResponse, await handler(request))
            except _ApiError as e:
                headers = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
                return web.json_response({"error": e.message}, status=e.status, headers=headers)
            except web.HTTPException:
                raise
            except Exception:
                # A handler awaits the command layer, which touches the
                # network and the disk. Such a failure must answer in JSON
                # like every other failure of this API, not as an HTML 500.
                logger.exception("[api] %s failed", handler.__name__)
                return web.json_response({"error": "internal error"}, status=500)

        wrapper.__name__ = handler.__name__
        return wrapper

    def _key_valid(self, request: web.Request) -> bool:
        """True when the request carries the current API key."""
        cfg = self._config.api
        if not cfg.enabled or not cfg.key:
            return False
        token = _request_token(request)
        if token is None:
            return False
        # Compare bytes. A header can carry bytes that are not UTF-8, and
        # secrets.compare_digest rejects a str that is not ASCII. The
        # encoding maps such bytes back, so they fail the comparison.
        return secrets.compare_digest(
            token.encode("utf-8", "surrogateescape"), cfg.key.encode("utf-8", "surrogateescape")
        )

    def _key_allowed(self, request: web.Request) -> bool:
        """True when the address still holds key budget."""
        return self._key_budget.allowed(request.remote or "unknown")

    def _record_key_fail(self, request: web.Request) -> None:
        self._key_budget.record(request.remote or "unknown")

    def _authenticate(self, request: web.Request) -> str:
        """Origin of the request for admin messages: key or panel session.

        A valid API key returns "Control API". A live panel session returns
        "Web panel" (state-changing calls still need its CSRF token, like on
        the panel routes). Anything else fails like before: a disabled API
        reads 404, a bad key reads 401. Failures spend the per-address
        budget, so key guessing ends in 429.
        """
        if self._key_valid(request):
            self._key_budget.clear(request.remote or "unknown")
            return "Control API"
        if self._session_of is not None:
            session = self._session_of(request)
            if session is not None:
                csrf = getattr(session, "csrf", "")
                if request.method != "GET":
                    token = request.headers.get("X-CSRF-Token", "")
                    if not token or not csrf or not hmac.compare_digest(token, csrf):
                        msg = "bad CSRF token"
                        raise _ApiError(403, msg)
                return "Web panel"
        cfg = self._config.api
        if not cfg.enabled or not cfg.key:
            # Absent means absent: no budget is spent, so a disabled API
            # never answers 429 and leaks nothing through it.
            msg = "not found"
            raise _ApiError(404, msg)
        if not self._key_allowed(request):
            msg = "too many attempts, try again later"
            raise _ApiError(429, msg)
        self._record_key_fail(request)
        logger.warning("[api] rejected request from %s: bad or missing API key", request.remote)
        msg = "unauthorized"
        raise _ApiError(401, msg)

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

    async def _notify(self, messages: Iterable[str], origin: str = "Control API") -> None:
        """Tell the admin about applied API changes. A failure never fails the request."""
        await self._ctrl.notify_api_changes(list(messages), origin=origin)

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
        if is_error(text):
            raise _ApiError(400, _plain(text))
        if text.startswith(_CONFLICT_MARK):
            raise _ApiError(409, _plain(text))
        return text

    async def _status(self, request: web.Request) -> web.Response:
        """Service summary: version, channel count, active recordings."""
        from stream_archive.health import degraded as _degraded

        return web.json_response(
            {
                "version": installed_app_version() or "unknown",
                "channels": len(self._config.channels),
                "recording": self._recorder.active_channels(),
                "monitoring_interval_s": self._config.monitoring_interval,
                "degraded": sorted(_degraded()),
            }
        )

    async def _settings(self, request: web.Request) -> web.Response:
        """Current global settings."""
        return web.json_response(_settings_json(self._config))

    async def _patch_settings(self, request: web.Request) -> web.Response:
        """Apply the given global settings, one command per key."""
        payload = await read_json_object(request, _MAX_BODY_BYTES, _ApiError)
        unknown = sorted(set(payload) - set(_SETTING_KEYS))
        if unknown:
            msg = "unknown setting(s): " + ", ".join(unknown)
            raise _ApiError(400, msg)
        applied, errors, status = await self._apply_each(payload, self._apply_setting)
        await self._notify(applied.values(), request[_ORIGIN_KEY])
        body = {"applied": applied, "errors": errors, "settings": _settings_json(self._config)}
        return web.json_response(body, status=status)

    async def _apply_setting(self, key: str, value: Any) -> str:
        """Apply one global setting through its Telegram command."""
        ctrl = self._ctrl
        if key == "output_mode":
            return await self._run(ctrl.handle_mode, [_global_mode(value)])
        if key == "preferred_quality":
            return await self._run(ctrl.handle_quality, [_global_quality(value)])
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
        if key == "youtube_hold_seconds":
            return await self._run(ctrl.handle_global_hold, [_global_hold_seconds(value)])
        if key == "endpoint_enabled":
            return await self._apply_endpoint_enabled(_switch(value, key) == "on")
        if key == "endpoint_public_url":
            return await self._apply_endpoint_url(_public_url(value, key, allow_empty=True))
        if key == "kick_webhook_enabled":
            return await self._apply_kick_enabled(_switch(value, key) == "on")
        if key == "kick_webhook_public_url":
            return await self._apply_kick_url(_public_url(value, key, allow_empty=True, require_dot=True))
        if key == "api_enabled":
            return await self._apply_api_enabled(_switch(value, key) == "on")
        msg = f"unknown setting: {key}"
        raise _ApiError(400, msg)

    async def _reconcile_delivery(self) -> None:
        """Restart listeners and re-subscribe Kick after an endpoint change.

        The config write already passed validation. A bind failure must not
        leave the file claiming an endpoint nothing serves, so the caller
        restores the snapshot on any error here. Subscriptions need the
        endpoint and the webhook both on: with nowhere to deliver, the
        app drops them instead of recreating them.
        """
        target = self._ctrl._kick_webhook
        if target is None:
            return
        await target.apply_state()
        if self._config.kick.webhook.enabled and self._config.endpoint.enabled:
            await target.sync_channels(self._config.channels)

    def _delivery_snapshot(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Endpoint, Kick, and API state, to restore after a failed reconcile."""
        return (
            self._config.endpoint.model_dump(),
            self._config.kick.webhook.model_dump(),
            self._config.api.model_dump(),
        )

    def _restore_delivery(self, snapshot: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]) -> None:
        """Write a delivery snapshot back without extra event noise."""
        endpoint, webhook, api = snapshot

        def restore(candidate: AppConfig) -> None:
            for name, value in endpoint.items():
                setattr(candidate.endpoint, name, value)
            for name, value in webhook.items():
                setattr(candidate.kick.webhook, name, value)
            for name, value in api.items():
                setattr(candidate.api, name, value)

        apply_config_change(self._config, restore)

    async def _apply_delivery_change(self, mutate: Any, ok_text: str) -> str:
        """Apply one endpoint, Kick, or API change, reconcile, and report it.

        The success event lands only after the reconcile: a rolled-back
        change must not leave a false success in the feed. These keys
        never touch recording settings, so no apply-warning applies.
        """
        from stream_archive import events as _events

        before = self._delivery_snapshot()
        try:
            apply_config_change(self._config, mutate)
        except ValueError as e:
            raise _ApiError(400, _plain(f"\u274c {e}")) from None
        try:
            await self._reconcile_delivery()
        except Exception as e:
            self._restore_delivery(before)
            # The failed attempt unbound the listeners on its way out.
            # Re-converge them on the restored state, so runtime matches
            # the file again.
            with contextlib.suppress(Exception):
                if self._ctrl._kick_webhook is not None:
                    await self._ctrl._kick_webhook.apply_state()
            msg = f"change saved but not served, restored: {e}"
            raise _ApiError(500, msg) from e
        _events.record("config", None, ok_text)
        return ok_text

    async def _apply_endpoint_enabled(self, enabled: bool) -> str:
        """Turn the endpoint on with the saved URL, or off keeping it."""

        def mutate(candidate: AppConfig) -> None:
            if enabled:
                # The model requires the URL the moment enabled flips on.
                # An empty saved URL fails here as a 400, like in the bot.
                candidate.endpoint.enabled = True
            else:
                candidate.endpoint.enabled = False

        state = "enabled" if enabled else "disabled"
        return await self._apply_delivery_change(mutate, f"Endpoint {state}")

    async def _apply_endpoint_url(self, url: str) -> str:
        """Save the endpoint public URL, keeping the enabled state.

        Empty clears a saved URL while the endpoint stays off. It never
        clears the URL of a running endpoint: the model would reject
        the state at once.
        """
        if not url and self._config.endpoint.enabled:
            msg = "cannot clear the URL while the endpoint is on"
            raise _ApiError(400, msg)

        def mutate(candidate: AppConfig) -> None:
            # The URL goes first: the model requires an http(s) URL the
            # moment enabled flips to True, and assignment validates
            # every set.
            candidate.endpoint.public_url = url
            if not candidate.kick.webhook.public_url.strip():
                candidate.kick.webhook.setup_notified = False

        if not url:
            return await self._apply_delivery_change(mutate, "Endpoint URL cleared")
        return await self._apply_delivery_change(mutate, f"Endpoint URL saved: {url}")

    async def _apply_kick_enabled(self, enabled: bool) -> str:
        """Turn Kick deliveries on or off. The entry is untouched."""

        def mutate(candidate: AppConfig) -> None:
            candidate.kick.webhook.enabled = enabled
            if enabled:
                candidate.kick.webhook.setup_notified = False

        state = "enabled" if enabled else "disabled"
        return await self._apply_delivery_change(mutate, f"Kick webhook {state}")

    async def _apply_kick_url(self, url: str) -> str:
        """Save the Kick entry. Empty follows the endpoint URL."""

        def mutate(candidate: AppConfig) -> None:
            candidate.kick.webhook.public_url = url
            candidate.kick.webhook.setup_notified = False

        if not url:
            return await self._apply_delivery_change(mutate, "Kick URL cleared, follows the endpoint")
        return await self._apply_delivery_change(mutate, f"Kick URL saved: {url}")

    async def _apply_api_enabled(self, enabled: bool) -> str:
        """Turn the control API on or off. A first enable makes a key.

        The key itself never reaches the event feed: the reconcile path
        records the toggle message, and the key travels only in the
        API answer, shown once.
        """
        key = self._config.api.key
        created = enabled and not key.strip()
        if created:
            key = secrets.token_urlsafe(32)

        def mutate(candidate: AppConfig) -> None:
            candidate.api.enabled = enabled
            if created:
                candidate.api.key = key

        text = await self._apply_delivery_change(mutate, f"Control API {'enabled' if enabled else 'disabled'}")
        if created:
            return f"{text}. New API key: {key} (shown once)"
        return text

    async def _apply_each(
        self, payload: dict[str, Any], apply: Callable[[str, Any], Awaitable[str]]
    ) -> tuple[dict[str, str], dict[str, str], int]:
        """Run one command per key. One failure never blocks the other keys.

        Returns ``(applied, errors, status)``. The status is 200 when every
        key applied, 500 when a key failed on its own error, 409 when every
        failure is the audio-only prompt, and 400 otherwise.
        """
        applied: dict[str, str] = {}
        errors: dict[str, str] = {}
        conflicts_only = True
        internal = False
        for key, value in payload.items():
            try:
                applied[key] = await apply(key, value)
            except _ApiError as e:
                errors[key] = e.message
                conflicts_only = conflicts_only and e.status == 409
            except Exception:
                # A command method touches the network and the disk after it
                # writes the config. Report that key and run the rest, so one
                # failure never blocks the other keys.
                logger.exception("[api] applying %r failed", key)
                errors[key] = "internal error"
                internal = True
        if internal:
            return applied, errors, 500
        if not errors:
            return applied, errors, 200
        return applied, errors, 409 if conflicts_only else 400

    async def _test_kick_delivery(self, request: web.Request) -> web.Response:
        """Run the Kick delivery test and report its timed result.

        The wait follows the test timeout (minutes on a cold setup): the
        request stays open until the first delivery or the timeout.
        """
        kick_webhook = self._ctrl._kick_webhook
        if kick_webhook is None:
            msg = "Kick delivery test unavailable"
            raise _ApiError(503, msg)
        ok, message = await kick_webhook.verify_delivery()
        return web.json_response({"ok": ok, "message": message})

    async def _show_api_key(self, request: web.Request) -> web.Response:
        """Return the API key. The panel shows it on demand, like the bot."""
        key = self._config.api.key
        if not key.strip():
            msg = "no API key yet - enable the API to generate one"
            raise _ApiError(404, msg)
        return web.json_response({"key": key})

    async def _rotate_api_key(self, request: web.Request) -> web.Response:
        """Replace the API key and return the new one. The old key dies at once."""
        key = secrets.token_urlsafe(32)
        existed = bool(self._config.api.key.strip())

        def mutate(candidate: AppConfig) -> None:
            candidate.api.key = key

        summary = "API key rotated - the old key stopped working" if existed else "API key generated"
        text = self._ctrl._apply(mutate, lambda _candidate: summary)
        if is_error(text):
            raise _ApiError(400, _plain(text))
        await self._notify([summary], request[_ORIGIN_KEY])
        return web.json_response({"key": key, "message": summary})

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
        payload = await read_json_object(request, _MAX_BODY_BYTES, _ApiError)
        raw = payload.get("channel")
        if not isinstance(raw, str) or not raw.strip():
            msg = "channel is required"
            raise _ApiError(400, msg)
        channel = normalize_channel_name(raw.strip())
        if channel is None:
            msg = f"invalid channel name: {raw!r} (use twitch:<name>, kick:<name>, or a profile URL)"
            raise _ApiError(400, msg)
        message = await self._run(self._ctrl.handle_add, [channel])
        await self._notify([message], request[_ORIGIN_KEY])
        return web.json_response({"message": message, "channel": channel, "channels": list(self._config.channels)})

    async def _remove_channel(self, request: web.Request) -> web.Response:
        """Stop monitoring a channel. A live recording stops."""
        channel = self._known_channel(request.match_info["channel"])
        message = await self._run(self._ctrl.handle_remove, [channel])
        await self._notify([message], request[_ORIGIN_KEY])
        return web.json_response({"message": message, "channel": channel, "channels": list(self._config.channels)})

    async def _patch_channel(self, request: web.Request) -> web.Response:
        """Apply the given per-channel settings, one command per key."""
        channel = self._known_channel(request.match_info["channel"])
        payload = await read_json_object(request, _MAX_BODY_BYTES, _ApiError)
        unknown = sorted(set(payload) - set(_CHANNEL_SETTING_KEYS))
        if unknown:
            msg = "unknown setting(s): " + ", ".join(unknown)
            raise _ApiError(400, msg)
        applied, errors, status = await self._apply_each(
            payload, functools.partial(self._apply_channel_setting, channel)
        )
        await self._notify(applied.values(), request[_ORIGIN_KEY])
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
        if key == "categories":
            parsed = _categories(value)
            if parsed == "default":
                return await self._run(ctrl.handle_category, [channel, "default"])
            names = cast(list[str], parsed)
            return await self._run(ctrl.handle_category, [channel, ", ".join(names)])
        msg = f"unknown setting: {key}"
        raise _ApiError(400, msg)
