"""Browser control panel served on the shared listener at the domain root.

The panel replaces the Telegram bot: it needs no Telegram token. The bot
and the panel can run at once. The panel shares the listener with the
Kick webhook and the control API, so it runs while any of them is on.

Security model (the panel is exposed through a tunnel):

* Password login only. The config stores a PBKDF2 hash, never the
  password. ``stream-archive-setup-web`` writes the hash.
* Sessions live server-side. The cookie carries an id plus an HMAC
  signature, and it is HttpOnly and SameSite=Lax (Secure outside
  local access). Each session owns a CSRF token, required on every
  state-changing call.
* Failed logins are rate limited per address and answered slowly.
* Every panel response carries hardening headers. No response
  carries a secret. Recording paths stay inside the archive dir.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web

from stream_archive import disk
from stream_archive.api import (
    _CHANNEL_SETTING_KEYS,
    _SETTING_KEYS,
    _ApiError,
    _channel_json,
    _global_mode,
    _global_quality,
    _hold_seconds,
    _mode,
    _quality,
    _scalar,
    _settings_json,
    _switch,
)
from stream_archive.config import (
    AppConfig,
    apply_config_change,
    normalize_channel_name,
    telegram_enabled,
)
from stream_archive.emotes import (
    TWITCH_EMOTE_URL as _TWITCH_EMOTE_URL,
)
from stream_archive.emotes import (
    fetch_channel_emotes,
    fetch_global_emotes,
    sniff_mime,
)
from stream_archive.http import build_http_client
from stream_archive.kick_chat import EMOTE_URL as _KICK_EMOTE_URL
from stream_archive.updater import installed_app_version

if TYPE_CHECKING:
    from stream_archive.recorder import Recorder
    from stream_archive.telegram import TelegramController

logger = logging.getLogger(__name__)

#: Largest accepted JSON body. Settings payloads are tiny.
_MAX_BODY_BYTES = 64 * 1024

#: Largest accepted login body. It holds one password only.
_MAX_LOGIN_BYTES = 4 * 1024

#: Session lifetime, in seconds. Twelve hours covers a long admin day.
_SESSION_TTL_S = 12 * 3600

#: Failed logins per address before the panel answers 429.
_LOGIN_MAX_FAILS = 10

#: Window of the login budget, in seconds.
_LOGIN_WINDOW_S = 600

#: Extra wait on a failed login, in seconds. It slows guessing.
_LOGIN_FAIL_DELAY_S = 1.0

#: Password hash work factor. The login rate limit caps online guessing,
#: so this factor targets offline guessing of a stolen config file.
_HASH_ITERATIONS = 210_000

#: Salt size of the password hash, in bytes.
_HASH_SALT_BYTES = 16

#: Shortest accepted panel password. The panel is exposed, so short
#: passwords are rejected at set time.
MIN_PASSWORD_LEN = 12

#: Cookie of the panel session.
_COOKIE_NAME = "sa_session"

#: File of the panel sessions, next to config.json. The data dir is a
#: persistent volume, so logins survive restarts of the app and the container.
_SESSIONS_FILENAME = "web_sessions.json"

#: Cap of live and stored sessions. Login past it drops the oldest one.
_SESSIONS_MAX = 512

#: Video and audio suffixes the browser plays inline. Finished recordings
#: are MP4 (M4A for audio-only): the recorder remuxes .ts at stop.
_PLAYABLE_SUFFIXES = (".mp4", ".m4a")

#: Suffixes the recordings endpoints serve. MKV never occurs: the recorder
#: writes .ts/.m4a and remuxes finished .ts captures to .mp4.
_RECORDING_SUFFIXES = (".mp4", ".ts", ".m4a")

#: Media type per suffix for the stream endpoint.
_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".ts": "video/mp2t",
    ".m4a": "audio/mp4",
}

#: Read size of one stream chunk, in bytes.
_STREAM_CHUNK_BYTES = 256 * 1024

#: Cap of one recordings listing. The browser pages past it.
_RECORDINGS_LIMIT_MAX = 1000

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: https://static-cdn.jtvnw.net https://cdn.7tv.app "
    "https://cdn.betterttv.net https://cdn.frankerfacez.com https://files.kick.com; "
    "media-src 'self' blob:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)

#: Cache lifetime of one channel emote set, in seconds.
_EMOTE_SET_TTL_S = 3600

#: Cache lifetime of the global emote sets, in seconds.
_EMOTE_GLOBAL_TTL_S = 86400

#: Cache lifetime of a failed emote lookup, in seconds. A short wait keeps
#: a chat reopen from hammering a down provider.
_EMOTE_FAIL_TTL_S = 300


class _WebError(Exception):
    """One panel failure that becomes a JSON error response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def hash_password(password: str) -> str:
    """PBKDF2 hash of ``password`` for the config file. Never logs it."""
    salt = secrets.token_bytes(_HASH_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _HASH_ITERATIONS)
    return f"pbkdf2-sha256${_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """True when ``password`` matches the stored hash. Constant time."""
    try:
        alg, iterations, salt_hex, hash_hex = stored.split("$", 3)
    except ValueError:
        return False
    if alg != "pbkdf2-sha256":
        return False
    try:
        count = int(iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    if count <= 0 or not salt or not expected:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, count)
    return hmac.compare_digest(digest, expected)


@dataclass
class _Session:
    """One login: its CSRF token, its expiry (wall-clock epoch seconds), and its password tag."""

    csrf: str
    expires: float
    pwd: str


def _pwd_tag(password_hash: str) -> str:
    """Short fingerprint of the current password hash for session binding."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()


#: Largest served chat payload: comment count cap, not bytes. Chat files
#: grow with the stream; the panel renders at most this many messages.
_CHAT_MAX_MESSAGES = 5000


def _embedded_map(payload: dict[str, Any]) -> dict[str, str]:
    """Emote id to data-URI image of the embeddedData block, or empty.

    Entries with undecodable data or an unknown image type are skipped:
    the caller falls back to the CDN and live lookups for those ids.
    """
    import base64

    out: dict[str, str] = {}
    block = payload.get("embeddedData")
    entries = block.get("firstParty") if isinstance(block, dict) else None
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        eid, data = entry.get("id"), entry.get("data")
        if not isinstance(eid, str) or not eid or not isinstance(data, str) or not data:
            continue
        try:
            raw = base64.b64decode(data)
        except Exception:
            continue
        mime = sniff_mime(raw)
        if mime is None:
            continue
        out[eid] = f"data:{mime};base64,{data}"
    return out


def _capture_spans(
    fragments: Any, text: str, kick: bool, embedded: dict[str, str] | None = None
) -> list[tuple[int, int, str]]:
    """(start, end, image URL) of the captured emotes in ``text``.

    Twitch fragments carry Twitch emote ids, Kick fragments carry Kick
    ids, so the artwork URL follows the platform of the recording. Ids
    with an embedded image use its data URI first: the replay then shows
    the emotes of the recording, even offline. The capture stores
    fragments in order, so positions follow the running offset. A
    fragment that does not match stops the walk: the rest stays plain
    words instead of pointing at the wrong slice.
    """
    template = _KICK_EMOTE_URL if kick else _TWITCH_EMOTE_URL
    spans: list[tuple[int, int, str]] = []
    if not isinstance(fragments, list):
        return spans
    pos = 0
    for frag in fragments:
        if not isinstance(frag, dict):
            continue
        piece = frag.get("text")
        if not isinstance(piece, str) or not piece:
            continue
        emo = frag.get("emoticon")
        eid = emo.get("emoticon_id") if isinstance(emo, dict) else None
        if not isinstance(eid, str) or not eid:
            pos += len(piece)
            continue
        if not text.startswith(piece, pos) or pos + len(piece) > len(text):
            break
        src = embedded.get(eid) if embedded else None
        spans.append((pos, pos + len(piece), src or template.format(id=eid)))
        pos += len(piece)
    return spans


#: Punctuation stripped around an emote word. Exact names match first,
#: so wrapped codes like ``:tf:`` still resolve before stripping.
_WORD_PUNCT = "!\"#$%&'()*+,-./:;=?@[\\]^_`{|}~"


def _word_spans(text: str, names: dict[str, str], taken: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """(start, end, image URL) of third-party emotes in ``text``.

    Whole whitespace-separated words, outside the captured spans. A word
    with attached punctuation (``baseg!``) still resolves, and the span
    covers the emote part only.
    """
    if not names:
        return []
    out: list[tuple[int, int, str]] = []
    for match in re.finditer(r"\S+", text):
        token = match.group(0)
        url = names.get(token)
        word = token
        if url is None:
            word = token.strip(_WORD_PUNCT)
            url = names.get(word) if word and word != token else None
        if url is None:
            continue
        start = match.start() + (token.find(word) if word != token else 0)
        end = start + len(word)
        if any(s < end and start < e for s, e, _ in taken):
            continue
        out.append((start, end, url))
    return out


def _chat_channel_id(payload: dict[str, Any], comments: list[Any]) -> str:
    """Twitch channel id of a chat file: first comment, else the trailer."""
    for comment in comments:
        if isinstance(comment, dict):
            cid = comment.get("channel_id")
            if cid:
                return str(cid)
    streamer = payload.get("streamer")
    if isinstance(streamer, dict) and streamer.get("id"):
        return str(streamer["id"])
    return ""


class WebUI:
    """Serve the browser panel on the shared listener."""

    def __init__(
        self,
        config: AppConfig,
        controller: TelegramController,
        recorder: Recorder,
        http: Any = None,
    ) -> None:
        self._config = config
        self._ctrl = controller
        self._recorder = recorder
        self._http = http if http is not None else build_http_client()
        self._owns_http = http is None
        self._emote_sets: dict[str, tuple[float, dict[str, str]]] = {}
        self._emote_globals: tuple[float, dict[str, str]] | None = None
        # A fresh config carries no secret. The panel stores a generated
        # secret in config.json on first boot (see register_routes), so
        # logins survive restarts. Until then an ephemeral secret signs
        # sessions. Stored sessions return here, minus the expired ones.
        self._ephemeral = secrets.token_hex(32)
        self._sessions: dict[str, _Session] = {}
        self._load_sessions()
        self._login_fails: dict[str, deque[float]] = {}
        self._assets = _load_assets()

    @property
    def _secret(self) -> str:
        configured = self._config.web.session_secret.strip()
        return configured or self._ephemeral

    def _ensure_secret(self) -> None:
        """Store a lasting session secret in config.json, once.

        Fresh configs carry an empty secret. Without this step every
        restart signs cookies with a new random secret and ends all
        logins. A read-only config keeps the old behavior: logins work
        until the next restart.
        """
        if self._config.web.session_secret.strip() or not self._config.web.enabled:
            return
        try:
            secret = secrets.token_hex(32)

            def mutate(candidate: AppConfig) -> None:
                candidate.web.session_secret = secret

            apply_config_change(self._config, mutate)
        except Exception:
            logger.warning("[web] Cannot store the session secret, sessions end on restart", exc_info=True)
            return
        logger.info("[web] Stored a session secret: logins survive restarts")

    def _sessions_path(self) -> Path | None:
        """File of the stored sessions, or None when the config is unbound."""
        try:
            return self._config.workdir / _SESSIONS_FILENAME
        except RuntimeError:
            return None

    def _load_sessions(self) -> None:
        """Read stored sessions into memory. A bad file starts empty."""
        path = self._sessions_path()
        if path is None:
            return
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("[web] Cannot read %s, sessions start empty", path)
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError, UnicodeDecodeError:
            logger.warning("[web] %s is not valid JSON, sessions start empty", path)
            return
        if not isinstance(payload, dict):
            logger.warning("[web] %s holds no session map, sessions start empty", path)
            return
        now = time.time()
        loaded: dict[str, _Session] = {}
        for sid, entry in payload.items():
            if not isinstance(sid, str) or not isinstance(entry, dict):
                continue
            csrf = entry.get("csrf")
            expires = entry.get("expires")
            pwd = entry.get("pwd")
            if not isinstance(csrf, str) or not csrf:
                continue
            if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= now:
                continue
            if not isinstance(pwd, str) or not pwd:
                continue
            if len(loaded) >= _SESSIONS_MAX:
                break
            loaded[sid] = _Session(csrf=csrf, expires=float(expires), pwd=pwd)
        self._sessions = loaded
        if loaded:
            logger.info("[web] Restored %d session(s) after a restart", len(loaded))

    def _save_sessions(self) -> None:
        """Write the live sessions next to config.json. Never fails a request."""
        path = self._sessions_path()
        if path is None:
            return
        payload = {sid: {"csrf": s.csrf, "expires": s.expires, "pwd": s.pwd} for sid, s in self._sessions.items()}
        try:
            _write_private_json(path, payload)
        except OSError:
            logger.warning("[web] Cannot store sessions in %s", path)

    def register_routes(self, kick_webhook: Any) -> None:
        """Add the panel routes to the shared listener.

        Call before the listener starts. One application serves the
        webhook, the control API, and the panel.
        """
        self._ensure_secret()
        kick_webhook.add_routes(self._register)

    def _register(self, app: web.Application) -> None:
        if self._owns_http:
            app.on_cleanup.append(self._close_http)
        app.router.add_get("/", self._index)
        app.router.add_get("/app.js", self._asset_js)
        app.router.add_get("/login.js", self._asset_login_js)
        app.router.add_get("/manifest.webmanifest", self._asset_manifest)
        app.router.add_get("/icon.svg", self._asset_icon)
        app.router.add_get("/styles.css", self._asset_css)
        app.router.add_get("/api/session", self._session)
        app.router.add_post("/api/login", self._login)
        app.router.add_post("/api/logout", self._guarded_csrf(self._logout))
        app.router.add_get("/api/status", self._guarded(self._status))
        app.router.add_get("/api/settings", self._guarded(self._settings))
        app.router.add_patch("/api/settings", self._guarded_csrf(self._patch_settings))
        app.router.add_get("/api/channels", self._guarded(self._channels))
        app.router.add_post("/api/channels", self._guarded_csrf(self._add_channel))
        app.router.add_get("/api/channels/{channel}", self._guarded(self._channel))
        app.router.add_patch("/api/channels/{channel}", self._guarded_csrf(self._patch_channel))
        app.router.add_delete("/api/channels/{channel}", self._guarded_csrf(self._remove_channel))
        app.router.add_get("/api/recordings", self._guarded(self._recordings))
        app.router.add_get("/api/recordings/stream", self._guarded(self._stream))
        app.router.add_get("/api/recordings/thumb", self._guarded(self._thumb))
        app.router.add_get("/api/chat", self._guarded(self._chat))
        app.router.add_delete("/api/recordings", self._guarded_csrf(self._delete_recording))
        app.router.add_post("/api/reload", self._guarded_csrf(self._reload))
        app.router.add_post("/api/restart", self._guarded_csrf(self._restart))
        app.router.add_get("/api/update", self._guarded(self._update))
        app.router.add_get("/api/events", self._guarded(self._events))
        app.router.add_delete("/api/events", self._guarded_csrf(self._clear_events))
        app.router.add_post("/api/password", self._guarded_csrf(self._password))

    # ---- page and assets -------------------------------------------------

    def _page(self, body: str, content_type: str, request: web.Request) -> web.Response:
        resp = web.Response(text=body, content_type=content_type)
        resp.headers["Cache-Control"] = "no-store"
        self._secure_headers(resp, request)
        return resp

    async def _index(self, request: web.Request) -> web.Response:
        if not self._config.web.enabled:
            return self._public_json(request, {"error": "not found"}, status=404)
        if self._session_of(request) is None:
            return self._page(self._assets["login.html"], "text/html", request)
        return self._page(self._assets["index.html"], "text/html", request)

    async def _asset_js(self, request: web.Request) -> web.Response:
        if not self._config.web.enabled:
            return self._public_json(request, {"error": "not found"}, status=404)
        return self._page(self._assets["app.js"], "application/javascript", request)

    async def _asset_login_js(self, request: web.Request) -> web.Response:
        if not self._config.web.enabled:
            return self._public_json(request, {"error": "not found"}, status=404)
        return self._page(self._assets["login.js"], "application/javascript", request)

    async def _asset_manifest(self, request: web.Request) -> web.Response:
        if not self._config.web.enabled:
            return self._public_json(request, {"error": "not found"}, status=404)
        resp = web.Response(text=self._assets["manifest.webmanifest"], content_type="application/manifest+json")
        self._secure_headers(resp, request)
        return resp

    async def _asset_icon(self, request: web.Request) -> web.Response:
        if not self._config.web.enabled:
            return self._public_json(request, {"error": "not found"}, status=404)
        return self._page(self._assets["icon.svg"], "image/svg+xml", request)

    async def _asset_css(self, request: web.Request) -> web.Response:
        if not self._config.web.enabled:
            return self._public_json(request, {"error": "not found"}, status=404)
        return self._page(self._assets["styles.css"], "text/css", request)

    # ---- auth ------------------------------------------------------------

    def _check_ready(self) -> None:
        """Raise when the panel is off or has no password yet."""
        if not self._config.web.enabled:
            msg = "not found"
            raise _WebError(404, msg)
        if not self._config.web.password_hash:
            msg = "web password is not set - run stream-archive-setup-web"
            raise _WebError(503, msg)

    def _session_of(self, request: web.Request) -> _Session | None:
        """Live session of the request cookie, or None."""
        raw = request.cookies.get(_COOKIE_NAME, "")
        sid, _, sig = raw.partition(".")
        if not sid or not sig:
            return None
        want = hmac.new(self._secret.encode(), sid.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return None
        session = self._sessions.get(sid)
        if session is None:
            return None
        if session.expires <= time.time():
            self._sessions.pop(sid, None)
            self._save_sessions()
            return None
        if not hmac.compare_digest(session.pwd, _pwd_tag(self._config.web.password_hash)):
            # The password changed (here or over the bot): end the session.
            self._sessions.pop(sid, None)
            self._save_sessions()
            return None
        session.expires = time.time() + _SESSION_TTL_S
        return session

    def _new_session(self) -> tuple[str, _Session]:
        sid = secrets.token_hex(32)
        session = _Session(
            csrf=secrets.token_hex(16),
            expires=time.time() + _SESSION_TTL_S,
            pwd=_pwd_tag(self._config.web.password_hash),
        )
        self._sessions[sid] = session
        if len(self._sessions) > _SESSIONS_MAX:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].expires)
            self._sessions.pop(oldest, None)
        self._save_sessions()
        return sid, session

    def _cookie_value(self, sid: str) -> str:
        sig = hmac.new(self._secret.encode(), sid.encode(), hashlib.sha256).hexdigest()
        return f"{sid}.{sig}"

    def _secure_cookie(self, request: web.Request) -> bool:
        """True when the browser reached us over HTTPS.

        The tunnel ends TLS in front of the app, so the forwarded proto
        counts. A Secure marker on plain HTTP buys nothing (the cookie
        already travels in the clear) and only breaks logins, so plain
        HTTP never sets it, whatever the host.
        """
        if request.headers.get("X-Forwarded-Proto", "").lower() == "https":
            return True
        return request.scheme == "https"

    def _set_cookie(self, response: web.Response, request: web.Request, sid: str) -> None:
        parts = [f"{_COOKIE_NAME}={self._cookie_value(sid)}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if self._secure_cookie(request):
            parts.append("Secure")
        parts.append(f"Max-Age={_SESSION_TTL_S}")
        response.headers["Set-Cookie"] = "; ".join(parts)

    def _clear_cookie(self, response: web.Response, request: web.Request) -> None:
        """Clear the session cookie with the attributes it was set with.

        A Secure cookie needs Secure on the clearing response too, or the
        browser keeps the old session after logout or a password change.
        """
        parts = [f"{_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax", "Max-Age=0"]
        if self._secure_cookie(request):
            parts.append("Secure")
        response.headers["Set-Cookie"] = "; ".join(parts)

    def _login_allowed(self, request: web.Request) -> bool:
        """True when the address still holds login budget."""
        now = time.monotonic()
        key = request.remote or "unknown"
        fails = self._login_fails.get(key)
        if fails is None:
            return True
        while fails and now - fails[0] > _LOGIN_WINDOW_S:
            fails.popleft()
        if not fails:
            del self._login_fails[key]
            return True
        return len(fails) < _LOGIN_MAX_FAILS

    def _record_login_fail(self, request: web.Request) -> None:
        fails = self._login_fails.setdefault(request.remote or "unknown", deque())
        fails.append(time.monotonic())
        while len(fails) > _LOGIN_MAX_FAILS:
            fails.popleft()

    def _public_json(self, request: web.Request, payload: dict[str, Any], status: int = 200) -> web.Response:
        """Hardened JSON for the public auth endpoints (no session yet)."""
        resp = web.json_response(payload, status=status)
        self._secure_headers(resp, request)
        return resp

    async def _session(self, request: web.Request) -> web.Response:
        try:
            self._check_ready()
        except _WebError as e:
            if e.status == 404:
                return self._public_json(request, {"error": e.message}, status=404)
            return self._public_json(request, {"authenticated": False, "setup_required": True}, status=200)
        session = self._session_of(request)
        if session is None:
            return self._public_json(request, {"authenticated": False, "setup_required": False}, status=200)
        return self._public_json(
            request, {"authenticated": True, "setup_required": False, "csrf": session.csrf}, status=200
        )

    async def _login(self, request: web.Request) -> web.Response:
        try:
            self._check_ready()
        except _WebError as e:
            return self._public_json(request, {"error": e.message}, status=e.status)
        if not self._login_allowed(request):
            logger.warning("[web] login rate limited for %s", request.remote)
            return self._public_json(request, {"error": "too many attempts, try again later"}, status=429)
        try:
            payload = await self._read_json(request, limit=_MAX_LOGIN_BYTES)
        except _WebError as e:
            return self._public_json(request, {"error": e.message}, status=e.status)
        password = payload.get("password")
        if not isinstance(password, str) or not password:
            msg = "password is required"
            return self._public_json(request, {"error": msg}, status=400)
        loop = asyncio.get_running_loop()
        # PBKDF2 burns ~100 ms of CPU: keep it off the event loop.
        ok = await loop.run_in_executor(None, verify_password, password, self._config.web.password_hash)
        if not ok:
            self._record_login_fail(request)
            logger.warning("[web] failed login from %s", request.remote)
            await asyncio.sleep(_LOGIN_FAIL_DELAY_S)
            return self._public_json(request, {"error": "invalid password"}, status=401)
        self._login_fails.pop(request.remote or "unknown", None)
        sid, session = self._new_session()
        logger.info("[web] login from %s", request.remote)
        resp = web.json_response({"ok": True, "csrf": session.csrf}, status=200)
        self._set_cookie(resp, request, sid)
        self._secure_headers(resp, request)
        return resp

    def _require_session(self, request: web.Request) -> _Session:
        """Live session of the request, or raise 401."""
        self._check_ready()
        session = self._session_of(request)
        if session is None:
            msg = "unauthorized"
            raise _WebError(401, msg)
        return session

    def _require_csrf(self, request: web.Request, session: _Session) -> None:
        """Matching CSRF token of the session, or raise 403."""
        token = request.headers.get("X-CSRF-Token", "")
        if not token or not hmac.compare_digest(token, session.csrf):
            msg = "bad CSRF token"
            raise _WebError(403, msg)

    def _guarded(self, handler: Any) -> Any:
        """Wrap one read handler with the enabled, setup, and session checks."""

        async def wrapper(request: web.Request) -> web.StreamResponse:
            try:
                session = self._require_session(request)
                return cast(web.StreamResponse, await handler(request, session))
            except _WebError as e:
                resp = web.json_response({"error": e.message}, status=e.status)
                self._secure_headers(resp, request)
                return resp
            except web.HTTPException:
                raise
            except Exception:
                logger.exception("[web] %s failed", handler.__name__)
                resp = web.json_response({"error": "internal error"}, status=500)
                self._secure_headers(resp, request)
                return resp

        wrapper.__name__ = handler.__name__
        return wrapper

    def _guarded_csrf(self, handler: Any) -> Any:
        """Wrap one write handler with the session check plus a CSRF check."""

        async def wrapper(request: web.Request) -> web.StreamResponse:
            try:
                session = self._require_session(request)
                self._require_csrf(request, session)
                return cast(web.StreamResponse, await handler(request, session))
            except _WebError as e:
                resp = web.json_response({"error": e.message}, status=e.status)
                self._secure_headers(resp, request)
                return resp
            except web.HTTPException:
                raise
            except Exception:
                logger.exception("[web] %s failed", handler.__name__)
                resp = web.json_response({"error": "internal error"}, status=500)
                self._secure_headers(resp, request)
                return resp

        wrapper.__name__ = handler.__name__
        return wrapper

    async def _read_json(self, request: web.Request, limit: int = _MAX_BODY_BYTES) -> dict[str, Any]:
        """Parse the JSON object body, or raise a 400/413."""
        if request.content_length is not None and request.content_length > limit:
            msg = "request body too large"
            raise _WebError(413, msg)
        raw = await request.content.read(limit + 1)
        if len(raw) > limit:
            msg = "request body too large"
            raise _WebError(413, msg)
        if not raw:
            msg = "JSON body required"
            raise _WebError(400, msg)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            msg = f"invalid JSON body: {e}"
            raise _WebError(400, msg) from e
        if not isinstance(payload, dict):
            msg = "JSON body must be an object"
            raise _WebError(400, msg)
        return payload

    # ---- responses --------------------------------------------------------

    def _secure_headers(self, response: Any, request: web.Request) -> None:
        """Harden one panel response. The shared app forbids global middleware."""
        headers = response.headers
        headers["Content-Security-Policy"] = _CSP
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        headers["Cache-Control"] = "no-store"
        if self._secure_cookie(request):
            headers["Strict-Transport-Security"] = "max-age=63072000"

    def _json(self, request: web.Request, payload: dict[str, Any], status: int = 200) -> web.Response:
        resp = web.json_response(payload, status=status)
        self._secure_headers(resp, request)
        return resp

    # ---- auth endpoints ----------------------------------------------------

    async def _logout(self, request: web.Request, session: _Session) -> web.Response:
        raw = request.cookies.get(_COOKIE_NAME, "")
        sid, _, _ = raw.partition(".")
        self._sessions.pop(sid, None)
        self._save_sessions()
        logger.info("[web] logout from %s", request.remote)
        resp = web.json_response({"ok": True}, status=200)
        self._clear_cookie(resp, request)
        self._secure_headers(resp, request)
        return resp

    async def _password(self, request: web.Request, session: _Session) -> web.Response:
        payload = await self._read_json(request, limit=_MAX_LOGIN_BYTES)
        current = payload.get("current")
        new = payload.get("new")
        if not isinstance(current, str) or not isinstance(new, str):
            msg = "current and new must be strings"
            raise _WebError(400, msg)
        loop = asyncio.get_running_loop()
        current_ok = await loop.run_in_executor(None, verify_password, current, self._config.web.password_hash)
        if not current_ok:
            self._record_login_fail(request)
            await asyncio.sleep(_LOGIN_FAIL_DELAY_S)
            msg = "invalid password"
            raise _WebError(401, msg)
        if len(new) < MIN_PASSWORD_LEN:
            msg = f"new password must hold at least {MIN_PASSWORD_LEN} characters"
            raise _WebError(400, msg)
        hashed = await loop.run_in_executor(None, hash_password, new)

        def mutate(candidate: AppConfig) -> None:
            candidate.web.password_hash = hashed

        try:
            apply_config_change(self._config, mutate)
        except ValueError as e:
            raise _WebError(400, str(e)) from e
        self._sessions.clear()
        self._save_sessions()
        logger.info("[web] password changed from %s, all sessions ended", request.remote)
        resp = web.json_response({"ok": True}, status=200)
        self._clear_cookie(resp, request)
        self._secure_headers(resp, request)
        return resp

    # ---- status and settings -------------------------------------------------

    async def _status(self, request: web.Request, session: _Session) -> web.Response:
        try:
            snap = await self._recorder.disk_snapshot()
        except Exception:
            logger.warning("[web] disk snapshot failed", exc_info=True)
            snap = {"usage_ok": False, "archive_gb": 0.0}
        try:
            now = self._recorder.recording_info()
        except Exception:
            now = []
        payload = {
            "version": installed_app_version() or "unknown",
            "channels": list(self._config.channels),
            "recording": self._recorder.active_channels(),
            "monitoring_interval_s": self._config.monitoring_interval,
            "telegram_enabled": telegram_enabled(self._config),
            "endpoint": {
                "enabled": self._config.endpoint.enabled,
                "tunnel": self._config.endpoint.tunnel,
                "public_url": self._config.endpoint.public_url,
            },
            "kick_webhook": {"enabled": self._config.kick.webhook.enabled},
            "mtproto": {"enabled": self._config.mtproto.enabled},
            "update_check": {
                "enabled": self._config.update_check.enabled,
                "interval_hours": self._config.update_check.interval_hours,
            },
            "recordings_now": now,
            "disk": {**snap, "cap_gb": self._config.disk.max_total_gb},
        }
        return self._json(request, payload)

    async def _settings(self, request: web.Request, session: _Session) -> web.Response:
        return self._json(request, _settings_json(self._config))

    async def _patch_settings(self, request: web.Request, session: _Session) -> web.Response:
        payload = await self._read_json(request)
        unknown = sorted(set(payload) - set(_SETTING_KEYS))
        if unknown:
            msg = "unknown setting(s): " + ", ".join(unknown)
            raise _WebError(400, msg)
        helper = _ApiHelper(self._ctrl)
        applied, errors, status = await helper.apply_each(payload, helper.apply_setting)
        await self._notify([f"{v}" for v in applied.values()], origin="Web panel")
        body: dict[str, Any] = {"applied": applied, "errors": errors, "settings": _settings_json(self._config)}
        return self._json(request, body, status=status)

    # ---- channels ------------------------------------------------------------

    def _channel_state(self, channel: str) -> dict[str, Any]:
        return _channel_json(self._config, channel, self._recorder.is_recording(channel))

    def _known_channel(self, raw: str) -> str:
        channel = normalize_channel_name(raw)
        if channel is None:
            msg = f"invalid channel name: {raw!r} (use twitch:<name> or kick:<name>)"
            raise _WebError(400, msg)
        if channel not in self._config.channels:
            msg = f"{channel} is not monitored"
            raise _WebError(404, msg)
        return channel

    async def _channels(self, request: web.Request, session: _Session) -> web.Response:
        return self._json(request, {"channels": [self._channel_state(ch) for ch in self._config.channels]})

    async def _channel(self, request: web.Request, session: _Session) -> web.Response:
        channel = self._known_channel(request.match_info["channel"])
        return self._json(request, self._channel_state(channel))

    async def _add_channel(self, request: web.Request, session: _Session) -> web.Response:
        payload = await self._read_json(request)
        raw = payload.get("channel")
        if not isinstance(raw, str) or not raw.strip():
            msg = "channel is required"
            raise _WebError(400, msg)
        channel = normalize_channel_name(raw.strip())
        if channel is None:
            msg = f"invalid channel name: {raw!r} (use twitch:<name>, kick:<name>, or a profile URL)"
            raise _WebError(400, msg)
        helper = _ApiHelper(self._ctrl)
        message = await helper.run(self._ctrl.handle_add, [channel])
        await self._notify([message], origin="Web panel")
        return self._json(request, {"message": message, "channel": channel, "channels": list(self._config.channels)})

    async def _remove_channel(self, request: web.Request, session: _Session) -> web.Response:
        channel = self._known_channel(request.match_info["channel"])
        helper = _ApiHelper(self._ctrl)
        message = await helper.run(self._ctrl.handle_remove, [channel])
        await self._notify([message], origin="Web panel")
        return self._json(request, {"message": message, "channel": channel, "channels": list(self._config.channels)})

    async def _patch_channel(self, request: web.Request, session: _Session) -> web.Response:
        channel = self._known_channel(request.match_info["channel"])
        payload = await self._read_json(request)
        unknown = sorted(set(payload) - set(_CHANNEL_SETTING_KEYS))
        if unknown:
            msg = "unknown setting(s): " + ", ".join(unknown)
            raise _WebError(400, msg)
        helper = _ApiHelper(self._ctrl)
        applied, errors, status = await helper.apply_each(
            payload, lambda key, value, _ch=channel: helper.apply_channel_setting(_ch, key, value)
        )
        await self._notify([f"{v}" for v in applied.values()], origin="Web panel")
        return self._json(request, {"channel": channel, "applied": applied, "errors": errors}, status=status)

    # ---- chat emotes --------------------------------------------------------

    async def _close_http(self, app: web.Application) -> None:
        """Close the owned HTTP client when the listener stops."""
        if self._owns_http:
            with contextlib.suppress(Exception):
                await self._http.aclose()

    async def _third_party_map(self, channel_id: str) -> dict[str, str]:
        """Emote name to image URL for one channel: set first, then globals."""
        now = time.monotonic()
        cached = self._emote_sets.get(channel_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        merged = {
            name: url for name, (_pid, url) in (await fetch_channel_emotes(self._http, "twitch", channel_id)).items()
        }
        if self._emote_globals is None or self._emote_globals[0] <= now:
            glob = {name: url for name, (_pid, url) in (await fetch_global_emotes(self._http)).items()}
            self._emote_globals = (now + _EMOTE_GLOBAL_TTL_S, glob)
        for name, url in self._emote_globals[1].items():
            merged.setdefault(name, url)
        ttl = _EMOTE_SET_TTL_S if merged else _EMOTE_FAIL_TTL_S
        self._emote_sets[channel_id] = (now + ttl, merged)
        return merged

    # ---- recordings ------------------------------------------------------------

    def _scan_recordings(self, base: Path) -> list[tuple[float, int, Path]]:
        """Newest-first archive scan for the recordings listing.

        Runs in an executor: the walk stats every file. Served suffixes
        only: the archive can hold a hand-placed .mkv the panel hides
        instead of listing as a dead entry.
        """
        found: list[tuple[float, int, Path]] = []
        if base.exists():
            for path in disk.iter_recordings(base):
                if path.suffix.lower() not in _RECORDING_SUFFIXES:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                found.append((st.st_mtime, st.st_size, path))
        found.sort(key=lambda t: t[0], reverse=True)
        return found

    def _is_live(self, path: Path) -> bool:
        """True when ``path`` is an in-flight capture. Raises 503 when unknown.

        Destructive and streaming paths use this strict check: a recorder
        hiccup must block the action, never silently treat a live file as
        finished. The listing uses best-effort flags instead.
        """
        return os.path.realpath(path) in self._active_set()

    def _active_set(self) -> set[str]:
        """Live capture paths, or raise 503 when the recorder errors."""
        recorder = self._recorder
        if not hasattr(recorder, "_active_paths"):
            return set()
        try:
            return set(recorder._active_paths())
        except Exception as e:
            msg = "recording state unavailable, try again"
            raise _WebError(503, msg) from e

    def _live_paths(self) -> set[str]:
        recorder = self._recorder
        if hasattr(recorder, "_active_paths"):
            try:
                return set(recorder._active_paths())
            except Exception:
                return set()
        return set()

    def _recording_id(self, base: Path, path: Path) -> str:
        return path.relative_to(base).as_posix()

    def _recording_path(self, rel: str) -> Path:
        """Archive path of a browser-supplied id. Rejects escapes."""
        base = disk.resolve_recording_dir(self._config).resolve()
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            msg = "invalid recording id"
            raise _WebError(400, msg)
        candidate = (base / rel).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            msg = "invalid recording id"
            raise _WebError(400, msg) from None
        if candidate.suffix.lower() not in _RECORDING_SUFFIXES:
            msg = "not a recording"
            raise _WebError(404, msg)
        return candidate

    async def _recordings(self, request: web.Request, session: _Session) -> web.Response:
        base = disk.resolve_recording_dir(self._config)
        channel = request.query.get("channel", "").strip() or None
        try:
            limit = int(request.query.get("limit", "200"))
        except ValueError:
            msg = "limit must be a number"
            raise _WebError(400, msg) from None
        try:
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            msg = "offset must be a number"
            raise _WebError(400, msg) from None
        limit = min(max(limit, 1), _RECORDINGS_LIMIT_MAX)
        offset = max(offset, 0)
        loop = asyncio.get_running_loop()
        # The walk touches every file: keep it off the event loop.
        found = await loop.run_in_executor(None, self._scan_recordings, base)
        live = self._live_paths()
        items: list[dict[str, Any]] = []
        for mtime, size, path in found:
            rel = self._recording_id(base, path)
            if channel is not None and not rel.startswith(channel.replace(":", "/") + "/"):
                continue
            items.append(
                {
                    "id": rel,
                    "name": path.name,
                    "size": size,
                    "mtime": mtime,
                    "live": os.path.realpath(path) in live,
                    "playable": path.suffix.lower() in _PLAYABLE_SUFFIXES,
                }
            )
        total = len(items)
        return self._json(request, {"total": total, "recordings": items[offset : offset + limit]})

    async def _stream(self, request: web.Request, session: _Session) -> web.StreamResponse:
        rel = request.query.get("id", "")
        download = request.query.get("download", "") == "1"
        path = self._recording_path(rel)
        if self._is_live(path):
            # A capture in flight is incomplete: its container is not
            # finalized, so it can neither play nor download yet.
            msg = f"{path.name} is recording now"
            raise _WebError(409, msg)
        try:
            size = path.stat().st_size
        except OSError:
            missing = web.json_response({"error": "not found"}, status=404)
            self._secure_headers(missing, request)
            return missing
        media = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
        start, end = _parse_range(request.headers.get("Range"), size)
        status = 206 if start is not None else 200
        headers = {"Accept-Ranges": "bytes", "Content-Type": media}
        if download:
            # Recorder names cannot hold quotes or line breaks, but a file
            # placed by hand can: strip them so the name cannot break out
            # of the quoted-string.
            safe_name = path.name.replace('"', "").replace("\r", "").replace("\n", "")
            headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        if start is None:
            headers["Content-Length"] = str(size)
            resp = web.StreamResponse(status=200, headers=headers)
            self._secure_headers(resp, request)
            await resp.prepare(request)
            await _send_file_range(path, 0, size - 1, resp)
            return resp
        assert end is not None
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)
        resp = web.StreamResponse(status=status, headers=headers)
        self._secure_headers(resp, request)
        await resp.prepare(request)
        await _send_file_range(path, start, end, resp)
        return resp

    async def _thumb(self, request: web.Request, session: _Session) -> web.Response:
        from stream_archive import disk as disk_mod
        from stream_archive.recorder.remux import capture_thumbnail

        rel = request.query.get("id", "")
        path = self._recording_path(rel)
        if self._is_live(path):
            msg = f"{path.name} is recording now"
            raise _WebError(409, msg)
        target = disk_mod.thumbnail_path(self._config, path)
        if target is None:
            msg = "not a recording"
            raise _WebError(404, msg)
        if not target.exists():
            # The cache fills on demand: old recordings never captured one.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, capture_thumbnail, path, target)
        try:
            body = target.read_bytes()
        except OSError:
            missing = web.json_response({"error": "not found"}, status=404)
            self._secure_headers(missing, request)
            return missing
        resp = web.Response(body=body, content_type="image/jpeg")
        self._secure_headers(resp, request)
        # Thumbnails are immutable for one recording id (dated filenames),
        # so the browser may cache them: without this the 30s list refresh
        # re-downloads every image and the cards flicker.
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    async def _chat(self, request: web.Request, session: _Session) -> web.Response:
        """Chat messages of one recording for the side panel.

        The chat file shares the recording stem: ``<stem>.chat.json`` in
        the mirrored chat dir. Comments already carry video-relative
        ``content_offset_seconds``, so the panel highlights by player time
        with no clock math. Live captures answer 409 like the stream.
        """
        rel = request.query.get("id", "")
        path = self._recording_path(rel)
        if self._is_live(path):
            msg = f"{path.name} is recording now"
            raise _WebError(409, msg)
        chat_file = self._chat_file(path)
        try:
            raw = await asyncio.get_running_loop().run_in_executor(None, chat_file.read_bytes)
        except OSError:
            return self._json(request, {"messages": [], "truncated": False, "missing": True})
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError, UnicodeDecodeError:
            msg = "chat file is not valid JSON"
            raise _WebError(502, msg) from None
        comments = payload.get("comments") if isinstance(payload, dict) else None
        if not isinstance(comments, list):
            return self._json(request, {"messages": [], "truncated": False, "missing": True})
        messages: list[dict[str, Any]] = []
        kick = rel.startswith("kick/")
        embedded = _embedded_map(payload)
        channel_id = "" if kick else _chat_channel_id(payload, comments)
        names = await self._third_party_map(channel_id) if channel_id else {}
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            try:
                offset = float(comment.get("content_offset_seconds", -1))
            except TypeError, ValueError:
                continue
            if offset < 0:
                continue
            commenter = comment.get("commenter") or {}
            message = comment.get("message") or {}
            body = message.get("body")
            if not isinstance(body, str) or not body:
                continue
            name = commenter.get("display_name") or commenter.get("name") or "?"
            if not isinstance(name, str):
                name = "?"
            text = body[:500]
            spans = _capture_spans(message.get("fragments"), text, kick, embedded)
            spans.extend(_word_spans(text, names, spans))
            entry: dict[str, Any] = {"t": offset, "user": name[:64], "text": text}
            if spans:
                spans.sort(key=lambda s: (s[0], s[1]))
                entry["emotes"] = [{"start": s, "end": e, "src": u} for s, e, u in spans]
            messages.append(entry)
        messages.sort(key=lambda m: m["t"])
        truncated = len(messages) > _CHAT_MAX_MESSAGES
        return self._json(
            request,
            {"messages": messages[:_CHAT_MAX_MESSAGES], "truncated": truncated, "missing": False},
        )

    def _chat_file(self, recording: Path) -> Path:
        """Chat file of a recording: same stem, .chat.json, mirrored chat dir."""
        rel = recording.name
        stem = rel[: -len(recording.suffix)] if recording.suffix else rel
        channel_dir = recording.parent.relative_to(disk.resolve_recording_dir(self._config).resolve())
        return disk.chat_dir_path(self._config) / channel_dir / f"{stem}.chat.json"

    async def _delete_recording(self, request: web.Request, session: _Session) -> web.Response:
        rel = request.query.get("id", "")
        path = self._recording_path(rel)
        try:
            exists = path.exists()
        except OSError:
            exists = False
        if not exists:
            msg = f"{path.name} is already gone"
            raise _WebError(404, msg)
        live = self._is_live(path)
        recorder = self._recorder
        if hasattr(recorder, "_remove_if_inactive"):
            try:
                freed = recorder._remove_if_inactive(path, self._active_set())
            except Exception as e:
                raise _WebError(500, "delete failed") from e
            if freed is None:
                if not path.exists():
                    msg = f"{path.name} is already gone"
                    raise _WebError(404, msg)
                if live:
                    msg = f"{path.name} is recording now"
                    raise _WebError(409, msg)
                msg = "delete failed"
                raise _WebError(500, msg)
        else:
            if live:
                msg = f"{path.name} is recording now"
                raise _WebError(409, msg)
            try:
                freed = path.stat().st_size
                path.unlink(missing_ok=True)
            except OSError as e:
                raise _WebError(500, "delete failed") from e
        disk.drop_thumbnail(self._config, path)
        disk.invalidate_snapshot()
        logger.info("[web] deleted %s from %s", path.name, request.remote)
        return self._json(request, {"message": f"Deleted {path.name}", "freed": freed})

    # ---- ops ---------------------------------------------------------------------

    async def _notify(self, messages: list[str], origin: str = "Web panel") -> None:
        """Tell the admin what the panel changed. Never fails the request."""
        await self._ctrl.notify_api_changes(list(messages), origin=origin)

    async def _reload(self, request: web.Request, session: _Session) -> web.Response:
        text = await self._ctrl.handle_reload()
        if text.startswith("❌"):
            return self._json(request, {"message": text}, status=400)
        logger.info("[web] config reloaded from %s", request.remote)
        return self._json(request, {"message": text})

    async def _restart(self, request: web.Request, session: _Session) -> web.Response:
        text: str = self._ctrl.handle_restart()
        if text.startswith("Restart is not available"):
            return self._json(request, {"message": text}, status=400)
        logger.info("[web] restart requested from %s", request.remote)
        return self._json(request, {"message": text})

    async def _update(self, request: web.Request, session: _Session) -> web.Response:
        text = await self._ctrl.handle_update()
        if text.startswith("❌"):
            return self._json(request, {"message": text}, status=502)
        return self._json(request, {"message": text})

    async def _events(self, request: web.Request, session: _Session) -> web.Response:
        from stream_archive import events as _events_mod

        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            msg = "limit must be a number"
            raise _WebError(400, msg) from None
        return self._json(request, {"events": _events_mod.list_events(limit)})

    async def _clear_events(self, request: web.Request, session: _Session) -> web.Response:
        from stream_archive import events as _events_mod

        _events_mod.clear()
        logger.info("[web] events cleared from %s", request.remote)
        return self._json(request, {"message": "Events cleared."})


class _ApiHelper:
    """Run Telegram command methods like the control API does.

    The web panel shares the command layer with the bot and the API, so
    one validation path serves every control surface. Rejected changes
    become 400, and the audio-only prompt becomes 409.
    """

    _ERROR_MARK = "❌"
    _CONFLICT_MARK = "⚠️"

    def __init__(self, ctrl: Any) -> None:
        self._ctrl = ctrl

    async def run(self, method: Any, args: list[Any]) -> str:
        import inspect as _inspect

        out: Any = method(args)
        if _inspect.isawaitable(out):
            out = await out
        text = str(out)
        if text.startswith(self._ERROR_MARK):
            msg = text[len(self._ERROR_MARK) :].lstrip()
            raise _WebError(400, msg)
        if text.startswith(self._CONFLICT_MARK):
            msg = text[len(self._CONFLICT_MARK) :].lstrip()
            raise _WebError(409, msg)
        return text

    async def apply_setting(self, key: str, value: Any) -> str:
        ctrl = self._ctrl
        if key == "output_mode":
            return await self.run(ctrl.handle_mode, [_global_mode(value)])
        if key == "preferred_quality":
            return await self.run(ctrl.handle_quality, [_global_quality(value)])
        if key == "retention_days":
            return await self.run(ctrl.handle_retention, [_scalar(value, key)])
        if key == "max_concurrent_recordings":
            return await self.run(ctrl.handle_maxrecordings, [_scalar(value, key)])
        if key == "max_concurrent_youtube_streams":
            return await self.run(ctrl.handle_maxyoutube, [_scalar(value, key)])
        if key == "record_chat":
            return await self.run(ctrl.handle_chat, [_switch(value, key), "twitch"])
        if key == "kick_record_chat":
            return await self.run(ctrl.handle_chat, [_switch(value, key), "kick"])
        if key == "disk_max_total_gb":
            return await self.run(ctrl.handle_disk, ["maxsize", _scalar(value, key)])
        if key == "disk_delete_oldest":
            return await self.run(ctrl.handle_disk, ["delete_oldest", _switch(value, key)])
        msg = f"unknown setting: {key}"
        raise _WebError(400, msg)

    async def apply_channel_setting(self, channel: str, key: str, value: Any) -> str:
        ctrl = self._ctrl
        if key == "output_mode":
            return await self.run(ctrl.handle_mode, [channel, _mode(value)])
        if key == "quality":
            return await self.run(ctrl.handle_quality, [channel, _quality(value)])
        if key == "youtube_hold_seconds":
            return await self.run(ctrl.handle_channel_hold, [channel, _hold_seconds(value)])
        msg = f"unknown setting: {key}"
        raise _WebError(400, msg)

    async def apply_each(self, payload: dict[str, Any], apply: Any) -> tuple[dict[str, str], dict[str, str], int]:
        """Run one command per key. One failure never blocks the other keys."""
        import inspect as _inspect

        applied: dict[str, str] = {}
        errors: dict[str, str] = {}
        conflicts_only = True
        internal = False
        for key, value in payload.items():
            try:
                out = apply(key, value)
                if _inspect.isawaitable(out):
                    out = await out
                applied[key] = out
            except _WebError as e:
                errors[key] = e.message
                conflicts_only = conflicts_only and e.status == 409
            except _ApiError as e:
                # The shared validators raise the API error type. Translate
                # it so a bad value answers 400, not 500.
                errors[key] = e.message
                conflicts_only = conflicts_only and e.status == 409
            except Exception:
                logger.exception("[web] applying %r failed", key)
                errors[key] = "internal error"
                internal = True
        if internal:
            return applied, errors, 500
        if not errors:
            return applied, errors, 200
        return applied, errors, 409 if conflicts_only else 400


def _parse_range(header: str | None, size: int) -> tuple[int | None, int | None]:
    """Single-range ``Range`` header as ``(start, end)``, or ``(None, None)``.

    Full responses and malformed ranges stream from the start: the player
    retries with a valid range.
    """
    if size <= 0 or not header or not header.lower().startswith("bytes="):
        return None, None
    spec = header[6:].strip().split(",", 1)[0].strip()
    first, _, last = spec.partition("-")
    try:
        if not first:
            suffix = int(last)
            if suffix <= 0:
                return None, None
            start = max(size - suffix, 0)
            return start, size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None, None
    if start >= size or end < start:
        return None, None
    return start, min(end, size - 1)


async def _send_file_range(path: Path, start: int, end: int, resp: web.StreamResponse) -> None:
    """Write bytes ``[start, end]`` of ``path`` into a prepared response."""
    loop = asyncio.get_running_loop()
    remaining = end - start + 1

    def _read_block(f: Any, count: int) -> bytes:
        data: bytes = f.read(count)
        return data

    try:
        with open(path, "rb") as f:
            f.seek(start)
            while remaining > 0:
                block = await loop.run_in_executor(None, _read_block, f, min(_STREAM_CHUNK_BYTES, remaining))
                if not block:
                    break
                remaining -= len(block)
                try:
                    await resp.write(block)
                except ConnectionError:
                    # The player went away mid-stream (closed tab, next
                    # track): stop quietly instead of logging a 500.
                    # CancelledError keeps propagating: a cancelled task
                    # must not report success.
                    break
    except OSError:
        logger.warning("[web] stream read failed for %s", path.name)


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON with mode 0600. It holds bearer secrets."""
    tmp = Path(str(path) + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        # A file left by a crashed run. Remove the entry itself (never
        # a symlink target) and create it exclusively.
        tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    # os.open applies its mode only when it creates the file, and the
    # umask clears bits of that mode.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_assets() -> dict[str, str]:
    """Read the panel page, script, and style next to this module."""
    base = Path(__file__).resolve().parent / "assets" / "web"
    out: dict[str, str] = {}
    for name in ("index.html", "login.html", "app.js", "login.js", "manifest.webmanifest", "icon.svg", "styles.css"):
        try:
            out[name] = (base / name).read_text(encoding="utf-8")
        except OSError:
            logger.warning("[web] asset missing: %s", base / name)
            out[name] = ""
    return out
