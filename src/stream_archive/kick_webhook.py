import asyncio
import base64
import contextlib
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx
from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from stream_archive.config import (
    KICK_PREFIX,
    AppConfig,
    bare_name,
    is_kick_channel,
    save_config,
    webhook_public_url,
)

if TYPE_CHECKING:
    from stream_archive.kick_api import KickAPI
    from stream_archive.monitor import Monitor
    from stream_archive.notifier import Notifier
    from stream_archive.recorder import Recorder

logger = logging.getLogger(__name__)

# Signed events are accepted only within this freshness window. The signature
# covers the header timestamp, so an attacker cannot move it. This is what
# stops replay of captured events (a recording kill or a forged chat).
_VERIFY_WINDOW_S = 300
# This store holds one dedup entry per unique signed event, for the
# freshness window. Its bound stops a flood from growing memory.
_MAX_SEEN_IDS = 50_000
# A failed signature triggers a public-key refetch (key-rotation retry).
# This negative cache bounds that refetch. Without it, an unauthenticated
# flood forces one outbound Kick API call per request and exhausts the Kick
# rate limit (self-DoS).
_KEY_REFETCH_INTERVAL_S = 60
# Token bucket keyed by client IP. It adds coarse backstop protection behind
# signature verification. Behind a tunnel, every request shares the tunnel's
# origin IP, so the budget covers aggregate legit chat volume, not per-event
# precision.
_RATE_LIMIT_PER_IP = 1200  # requests per window
_RATE_LIMIT_WINDOW_S = 60
_MAX_RATE_LIMIT_IPS = 10_000
# Cap on unseen client addresses that enter the bucket table in one window.
# Every request that arrives through the tunnel shares the tunnel address,
# so normal traffic needs one key. The cap limits a flood that uses many
# distinct addresses, which the table size alone does not.
_MAX_NEW_RATE_LIMIT_IPS = 100
#: Result of one signature check. "unavailable" is not a verdict: the public
#: key could not be fetched, so the sender must be asked to redeliver rather
#: than told that its signature is bad.
VerifyResult = Literal["ok", "bad", "unavailable"]

# Cap on concurrent in-flight webhook dispatches. The permit is taken only
# after the signature check, so an unauthenticated caller cannot occupy a
# slot that a genuine delivery needs.
_MAX_CONCURRENT = 16
# Largest accepted event body. Kick events are small, and the cap is applied
# before the signature check so an unauthenticated caller cannot make the
# receiver buffer an arbitrary payload.
_MAX_BODY_BYTES = 64 * 1024
# Deadline for reading the body of one event. It bounds the work an
# unverified request can hold open without occupying a dispatch permit.
_BODY_READ_TIMEOUT_S = 5.0
# Wait for a free dispatch permit before answering "busy". Short, because
# Kick retries a refused delivery.
_DISPATCH_WAIT_S = 0.1
# Kick-side sync failures (5xx) alert only after they persist this long, so
# a transient API outage does not page the admin. Other failures notify at
# once.
_SYNC_SERVER_ERROR_DELAY_S = 600


def _is_server_error(exc: Exception) -> bool:
    """True when Kick's API itself failed (5xx): an outage on their side.

    Config or auth problems (4xx), connectivity errors, and timeouts still
    notify immediately. Those failures can require action on our side.
    """
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


class _RateLimiter:
    """Token bucket keyed by client IP, with a bounded bucket table.

    The table holds the least recently used key only up to its size, so a
    flood of new addresses cannot push out a busy client. A new key also
    pays for its first request, and the number of unseen keys in one window
    is bounded.
    """

    def __init__(
        self,
        max_requests: int,
        window_s: int,
        max_keys: int = _MAX_RATE_LIMIT_IPS,
        max_new_keys: int = _MAX_NEW_RATE_LIMIT_IPS,
    ) -> None:
        self._max = max_requests
        self._window = window_s
        self._max_keys = max_keys
        self._max_new_keys = max_new_keys
        self._new_keys = 0
        self._new_keys_since = 0.0
        # Ordered by last access, oldest first.
        self._buckets: OrderedDict[str, list[float]] = OrderedDict()  # key -> [tokens, last_refill (monotonic)]

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        buckets = self._buckets
        bucket = buckets.get(key)
        if bucket is None:
            if now - self._new_keys_since >= self._window:
                self._new_keys_since = now
                self._new_keys = 0
            if self._new_keys >= self._max_new_keys:
                return False
            self._new_keys += 1
            if len(buckets) >= self._max_keys:
                buckets.popitem(last=False)  # evict by last access, not by insertion age
            # The first request spends one token. A free first request would
            # give a flood of distinct addresses an unbounded budget.
            buckets[key] = [self._max - 1, now]
            return True
        buckets.move_to_end(key)
        tokens, refill = bucket
        tokens = min(self._max, tokens + (now - refill) * (self._max / self._window))
        if tokens < 1:
            bucket[0], bucket[1] = tokens, now
            return False
        bucket[0], bucket[1] = tokens - 1, now
        return True


def _parse_timestamp(value: str) -> float | None:
    """Kick sends ISO-8601. Epoch seconds are accepted too. None when unparseable.

    A non-finite value is rejected: ``float('nan')`` parses, and every
    comparison against it is false, which would make the freshness check
    pass instead of fail. An offset-less ISO value is UTC: ``timestamp()``
    would otherwise read it in the local zone of the host.
    """
    value = value.strip()
    try:
        parsed_dt = datetime.fromisoformat(value)
    except ValueError:
        pass
    else:
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=UTC)
        parsed = parsed_dt.timestamp()
        return parsed if math.isfinite(parsed) else None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None


class KickWebhook:
    EVENT_LIVE = "livestream.status.updated"  # v1
    EVENT_CHAT = "chat.message.sent"  # v1

    def __init__(
        self,
        config: AppConfig,
        monitor: Monitor,
        recorder: Recorder,
        kick_api: KickAPI,
        notifier: Notifier | None,
    ):
        self._config = config
        self._monitor = monitor
        self._recorder = recorder
        self._api = kick_api
        self._notifier = notifier
        self._runner: Any = None
        self._site: Any = None
        self._bound: tuple[str, int] | None = None  # (host, port) of the live listener
        self._sync_task: asyncio.Task[Any] | None = None
        self._sync_failed_notified = False
        self._sync_failing_since: float | None = None  # monotonic start of current failure episode
        self._sync_error_logged = False
        self._subs: dict[str, set[str]] = {}  # bare slug -> set(subscription ids)
        self._seen_ids: dict[str, float] = {}  # message_id -> expires (monotonic)
        self._rate_limiter = _RateLimiter(_RATE_LIMIT_PER_IP, _RATE_LIMIT_WINDOW_S)
        self._sem = asyncio.Semaphore(_MAX_CONCURRENT)
        self._next_key_refetch = 0.0  # monotonic time. Gates the rotation refetch.
        self._next_key_fetch = 0.0  # monotonic time. Gates a repeat of a failed key fetch.
        self._app = web.Application()
        self._app.router.add_post("/kick/webhook", self._handle)

    def add_routes(self, register: Callable[[web.Application], None]) -> None:
        """Register extra routes on the shared listener (the control API uses this).

        Call before the first ``apply_state``. One listener serves the
        webhook and every extra route.
        """
        register(self._app)

    def listening_needed(self) -> bool:
        """True when the listener must run: the endpoint or the control API is on."""
        return self._config.endpoint.enabled or self._config.api.enabled

    async def apply_state(self) -> None:
        """Match the live listener and sync loop to the config. Idempotent.

        The listener serves the public endpoint, which carries the Kick
        webhook and the control API, so it runs while the endpoint or the
        API is enabled. The subscription sync loop needs the webhook and a
        reachable endpoint, and it deletes subscriptions for unmonitored
        channels. When the webhook goes off, the loop stops and the app
        deletes the subscriptions it created, so Kick stops the deliveries.
        """
        if not self.listening_needed():
            # The listener is off, so Kick cannot deliver anything. Stop the
            # sync loop, then delete the subscriptions it created. Kick then
            # stops the deliveries to a dead URL.
            await self._stop_sync()
            await self._drop_subscriptions()
            await self._unbind()
            return
        ep = self._config.endpoint
        if self._runner is not None and self._bound != (ep.listen_host, ep.listen_port):
            # A reload can change the listen address. Rebind, so the listener
            # and the webhook URL point at the same address. Stop the sync
            # loop first: it exits once the runner is gone, and a stopped loop
            # is recreated below.
            await self._stop_sync()
            await self._unbind()
        if self._runner is None:
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            try:
                self._site = web.TCPSite(self._runner, ep.listen_host, ep.listen_port)
                await self._site.start()
            except OSError:
                await self._unbind()  # a failed bind leaves no half-built runner
                raise
            self._bound = (ep.listen_host, ep.listen_port)
            logger.info(
                "[kick_webhook] listening on http://%s:%s (public: %s)",
                ep.listen_host,
                ep.listen_port,
                ep.public_url or "(none)",
            )
        if not self._sync_needed():
            await self._stop_sync()
            await self._drop_subscriptions()
            return
        if self._sync_task is None:
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def _drop_subscriptions(self) -> None:
        """Delete the subscriptions of every tracked channel.

        The webhook disable path uses this method. Idempotent.
        """
        for bare in list(self._subs):
            await self.remove_channel(f"{KICK_PREFIX}{bare}")

    def _sync_needed(self) -> bool:
        """True when Kick delivers events here: endpoint and webhook both on."""
        return self._config.endpoint.enabled and self._config.kick.webhook.enabled

    async def close(self) -> None:
        """Stop the sync loop and the HTTP listener. Idempotent."""
        await self._stop_sync()
        await self._unbind()

    async def _stop_sync(self) -> None:
        task = self._sync_task
        self._sync_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _unbind(self) -> None:
        site = self._site
        self._site = None
        self._bound = None
        if site is not None:
            with contextlib.suppress(Exception):
                await site.stop()
        runner = self._runner
        self._runner = None
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()

    async def _sync_loop(self) -> None:
        while self._runner is not None:
            try:
                await self._sync_subscriptions(self._config.channels)
            except Exception as e:
                # Log one error per failure episode. The loop retries every
                # interval, so an error per cycle is only noise.
                if not self._sync_error_logged:
                    self._sync_error_logged = True
                    logger.error("[kick_webhook] subscription sync failed: %s", e)
                else:
                    logger.debug("[kick_webhook] subscription sync still failing: %s", e)
                await self._notify_sync_failure(e)
            else:
                self._sync_failed_notified = False
                self._sync_failing_since = None
                self._sync_error_logged = False
            await asyncio.sleep(self._config.monitoring_interval)

    async def _notify_sync_failure(self, e: Exception) -> None:
        """Alert on a sync failure. Send one notification per failure episode.

        A Kick-side (5xx) failure alerts only after it persists for
        ``_SYNC_SERVER_ERROR_DELAY_S``, so a transient API blip stays quiet.
        """
        if not self._notifier or self._sync_failed_notified:
            return
        if _is_server_error(e):
            now = time.monotonic()
            if self._sync_failing_since is None:
                self._sync_failing_since = now
            if now - self._sync_failing_since < _SYNC_SERVER_ERROR_DELAY_S:
                return
        detail = str(e).strip() or e.__class__.__name__
        try:
            await self._notifier.notify(
                "\u26a0\ufe0f Kick webhook subscriptions out of sync \u2014 is the "
                "public URL configured in the Kick app (Settings \u2192 Developer \u2192 "
                "your app \u2192 Enable webhooks)? "
                f"{webhook_public_url(self._config)}\n"
                f"Error: {detail}"
            )
        except Exception:
            logger.error("[kick_webhook] sync-failure notification failed", exc_info=True)
            return
        # Only a sent notification marks the episode as notified. An earlier
        # flag would silence every later failure of the same episode.
        self._sync_failed_notified = True

    async def _handle(self, request: Any) -> Any:
        if not self._config.kick.webhook.enabled:
            # The feature is off, so the route behaves as if it did not exist.
            # The listener can stay up because the control API uses it.
            return web.Response(status=404, text="not found")
        client = request.remote or "unknown"
        if not self._rate_limiter.allow(client):
            return web.Response(status=429, text="too many requests")
        # Read the body and verify the signature before taking a dispatch
        # permit. An unauthenticated caller must not be able to hold the
        # shared budget that a genuine delivery needs: the read is bounded
        # by size and by time here, and the per-address bucket above bounds
        # the request rate.
        if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:
            return web.Response(status=413, text="body too large")
        chunks: list[bytes] = []
        total = 0
        try:
            # ``content.read(n)`` returns as soon as any data is buffered, so
            # drain the body to EOF: a delivery split across TCP segments is
            # otherwise verified truncated, fails the signature check, and is
            # answered 401, which a webhook sender treats as permanent. One
            # deadline covers the whole read.
            async with asyncio.timeout(_BODY_READ_TIMEOUT_S):
                while total <= _MAX_BODY_BYTES:
                    chunk = await request.content.read(_MAX_BODY_BYTES + 1 - total)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
        except TimeoutError:
            return web.Response(status=413, text="body too slow")
        if total > _MAX_BODY_BYTES:
            return web.Response(status=413, text="body too large")
        body = b"".join(chunks)
        event_type = request.headers.get("Kick-Event-Type", "")
        try:
            verified = await self._verify(request, body)
        except UnicodeDecodeError:
            logger.warning("[kick_webhook] event body is not valid UTF-8")
            return web.Response(status=400, text="bad encoding")
        if verified == "unavailable":
            # The public key could not be fetched, so this request cannot be
            # judged either way. A retryable status makes Kick redeliver,
            # instead of the permanent 401 that a signature failure earns.
            logger.warning("[kick_webhook] public key unavailable, asking for a redelivery")
            return web.Response(status=503, text="key unavailable", headers={"Retry-After": "5"})
        if verified != "ok":
            # Attackers control event_type, so log only known values.
            known = event_type if event_type in (self.EVENT_LIVE, self.EVENT_CHAT) else "unknown"
            logger.warning("[kick_webhook] signature verification failed (event=%s)", known)
            return web.Response(status=401, text="unauthorized")
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=_DISPATCH_WAIT_S)
        except TimeoutError:
            return web.Response(status=503, text="busy", headers={"Retry-After": "1"})
        try:
            msg_id = request.headers.get("Kick-Event-Message-Id")
            if not self._remember_id(msg_id):
                logger.debug("[kick_webhook] duplicate event, ignoring")
                return web.Response(status=200, text="ok")
            await self._maybe_confirm_delivery()
            try:
                if event_type == self.EVENT_LIVE:
                    await self._dispatch_live(body)
                elif event_type == self.EVENT_CHAT:
                    await self._dispatch_chat(body)
                else:
                    logger.debug("[kick_webhook] unknown event type %r", event_type)
                    return web.Response(status=204)
            except json.JSONDecodeError:
                # Truncated body. Answer 400 so Kick retries, and roll back
                # the dedup mark so the retry dispatches instead of reading
                # as a replay.
                if msg_id:
                    self._seen_ids.pop(msg_id, None)
                return web.Response(status=400, text="bad body")
            except Exception:
                # Roll back the dedup mark, so the code processes Kick's
                # retry instead of answering 200 as if it were a replay.
                # Aiohttp answers the raised exception with 500.
                if msg_id:
                    self._seen_ids.pop(msg_id, None)
                raise
            return web.Response(status=200, text="ok")
        finally:
            self._sem.release()

    def _remember_id(self, message_id: str | None) -> bool:
        """True when the message id is new within the freshness window.

        False for replays, including a missing id. An unsigned replay
        carries no id, so it must never pass as new.
        """
        if not message_id:
            return False
        now = time.monotonic()
        seen = self._seen_ids
        expires = seen.get(message_id)
        if expires is not None and expires > now:
            return False
        if len(seen) >= _MAX_SEEN_IDS:
            for mid, exp in list(seen.items()):
                if exp <= now:
                    del seen[mid]
        if len(seen) >= _MAX_SEEN_IDS:
            seen.pop(next(iter(seen)))
        seen[message_id] = now + _VERIFY_WINDOW_S
        return True

    async def _verify(self, request: Any, body: bytes) -> VerifyResult:
        message_id = request.headers.get("Kick-Event-Message-Id")
        timestamp = request.headers.get("Kick-Event-Message-Timestamp")
        signature_b64 = request.headers.get("Kick-Event-Signature")
        if not message_id or not timestamp or not signature_b64:
            return "bad"
        try:
            signature = base64.b64decode(signature_b64)
        except Exception:
            return "bad"
        event_time = _parse_timestamp(timestamp)
        if event_time is None or abs(time.time() - event_time) > _VERIFY_WINDOW_S:
            logger.warning("[kick_webhook] event timestamp outside freshness window")
            return "bad"
        # Strict decode: a non-UTF-8 body is corrupt, not a rotation. It
        # raises UnicodeDecodeError, and the caller answers 400.
        body_text = body.decode("utf-8", errors="strict")
        message = f"{message_id}.{timestamp}.{body_text}".encode()
        # A warm cache verifies without any outbound call. A cold cache needs
        # one, and the window for it is claimed before the await: otherwise a
        # concurrent burst would all pass the check and each issue a call,
        # spending the Kick rate limit one request at a time.
        if not self._api.has_public_key():
            now = time.monotonic()
            if now < self._next_key_fetch:
                return "unavailable"
            self._next_key_fetch = now + _KEY_REFETCH_INTERVAL_S
        try:
            public_key = await self._api.get_public_key()
        except Exception:
            # The window stays claimed, so the failure is not repeated for
            # every request for the rest of the interval.
            return "unavailable"
        if not public_key:
            # A 200 without a key leaves the cache cold, so this delivery
            # cannot be judged either: ask for a redelivery instead of
            # telling the sender that its signature is bad.
            return "unavailable"
        # A key is cached now, so the cold-start window is no longer needed.
        self._next_key_fetch = 0.0
        try:
            self._verify_signature(public_key, message, signature)
        except InvalidSignature:
            # A wrong key and a rotated key look the same here, so the
            # refetch below decides between them.
            pass
        except Exception as e:
            # A bad PEM or a non-RSA key is a misconfiguration, not a
            # rotation. Take the log line, and keep the delivery retryable.
            logger.error("[kick_webhook] signature check failed: %s", e)
            return "unavailable"
        else:
            return "ok"
        # The failure can mean that Kick rotated the key. Refetch
        # (rate-limited) and retry once. The refetch bypasses the cache
        # (force), so the code detects rotation reliably. Meanwhile, a flood
        # of bad signatures keeps verifying against the cached key with no
        # outbound calls. The code attempts only one refetch per interval.
        now = time.monotonic()
        if now < self._next_key_refetch:
            # The refetch is already rate-limited; this delivery still cannot
            # be judged, so it stays retryable rather than permanent.
            return "unavailable"
        self._next_key_refetch = now + _KEY_REFETCH_INTERVAL_S
        try:
            public_key = await self._api.get_public_key(force=True)
        except Exception:
            # The refetch is a fetch: its failure is not a verdict on the
            # signature, so the sender must redeliver rather than treat a
            # genuine event as forged.
            return "unavailable"
        if not public_key:
            return "unavailable"
        try:
            self._verify_signature(public_key, message, signature)
        except InvalidSignature:
            # The key was fetched and the signature still does not match, so
            # this delivery is not authentic.
            return "bad"
        except Exception as e:
            # The check itself failed, so there is no verdict on the
            # signature. Do not tell the sender that it is forged.
            logger.error("[kick_webhook] signature check failed: %s", e)
            return "unavailable"
        return "ok"

    def _verify_signature(self, public_key_pem: Any, message: bytes, signature: bytes) -> None:
        key = serialization.load_pem_public_key(
            public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem
        )
        if not isinstance(key, rsa.RSAPublicKey):
            msg = "webhook public key is not an RSA key"
            raise ValueError(msg)
        key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())

    async def _dispatch_live(self, body: bytes) -> None:
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("[kick_webhook] invalid livestream event body")
            raise
        broadcaster = event.get("broadcaster") or {}
        slug = broadcaster.get("channel_slug")
        if not slug:
            logger.warning("[kick_webhook] livestream event without channel_slug, ignoring")
            return
        channel = f"{KICK_PREFIX}{slug}"
        if channel not in self._config.channels:
            logger.debug("[kick_webhook] livestream event for unmonitored channel %s, ignoring", channel)
            return
        # The operation is chosen from the Kick-Event-Type header, which the
        # signature does not cover (it signs message-id, timestamp and body).
        # So the signed body has to confirm the action instead of merely
        # failing to contradict it: a body without a boolean state field is
        # not a livestream event and must never read as "stream ended".
        is_live = event.get("is_live")
        if not isinstance(is_live, bool):
            logger.warning("[kick_webhook] livestream event without a boolean is_live, ignoring")
            return
        if is_live:
            # title/game are None on purpose: the recorder fills them from the
            # streamlink kick plugin's metadata (no extra API call on the hot path).
            await self._monitor.handle_online(channel, None, None, None, self._config)
        else:
            await self._monitor.handle_offline(channel, self._config)

    async def _dispatch_chat(self, body: bytes) -> None:
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("[kick_webhook] invalid chat event body")
            raise
        broadcaster = event.get("broadcaster") or {}
        slug = broadcaster.get("channel_slug")
        if not slug:
            return
        channel = f"{KICK_PREFIX}{slug}"
        if channel not in self._config.channels:
            logger.debug("[kick_webhook] chat event for unmonitored channel %s, ignoring", channel)
            return
        # Same binding as above, in the other direction: a chat event must
        # carry the chat fields, so a message-shaped body cannot be written
        # into the archive under a different event type.
        if not isinstance(event.get("message_id"), str) or not isinstance(event.get("content"), str):
            logger.warning("[kick_webhook] chat event without a message_id and content, ignoring")
            return
        sender = event.get("sender") or {}
        identity = sender.get("identity") or {}
        badges = [
            {"text": b.get("text"), "type": b.get("type"), "count": b.get("count")}
            for b in (identity.get("badges") or [])
        ]
        payload = {
            "message_id": event.get("message_id"),
            "created_at": event.get("created_at"),
            "broadcaster": {
                "user_id": broadcaster.get("user_id"),
                "username": broadcaster.get("username"),
                "profile_picture": broadcaster.get("profile_picture"),
            },
            "sender": {
                "user_id": sender.get("user_id"),
                "username": sender.get("username"),
                "is_verified": sender.get("is_verified"),
                "is_anonymous": sender.get("is_anonymous"),
                "profile_picture": sender.get("profile_picture"),
                "username_color": (identity or {}).get("username_color"),
            },
            "content": event.get("content"),
            "emotes": [
                {
                    "emote_id": e.get("emote_id"),
                    "positions": [{"s": p.get("s"), "e": p.get("e")} for p in (e.get("positions") or [])],
                }
                for e in (event.get("emotes") or [])
            ],
            "badges": badges,
        }
        await self._recorder.add_kick_chat(channel, payload)

    async def _sync_subscriptions(self, channels: list[str]) -> int:
        """Reconcile webhook subscriptions with the monitored kick channels."""
        desired = {}  # bare slug -> broadcaster_user_id
        kick_channels = [c for c in channels if is_kick_channel(c)]
        if kick_channels:
            statuses = await self._api.get_channel_statuses([bare_name(c) for c in kick_channels])
            for c in kick_channels:
                bare = bare_name(c)
                status = statuses.get(bare)
                if status is None:
                    logger.warning("[kick_webhook] channel not found for webhook subs: %s", c)
                    continue
                uid = status.get("broadcaster_user_id")
                if uid is not None:
                    desired[bare] = uid

        existing = await self._api.list_event_subscriptions()
        by_user: dict[Any, list[Any]] = {}
        for sub in existing:
            by_user.setdefault(sub.get("broadcaster_user_id"), []).append(sub)

        # Create missing subscriptions for monitored channels.
        for bare, uid in desired.items():
            subs_for_user = by_user.get(uid, [])
            existing_events = {e.get("name") for s in subs_for_user for e in (s.get("events") or [])}
            # Record the ids of the subscriptions that already exist at Kick.
            # A restart starts with an empty _subs, and the cleanup paths
            # delete subscriptions by id.
            known_ids = {s.get("id") for s in subs_for_user if s.get("id")}
            if known_ids:
                self._subs.setdefault(bare, set()).update(known_ids)
            missing = [ev for ev in (self.EVENT_LIVE, self.EVENT_CHAT) if ev not in existing_events]
            if missing:
                created = await self._api.create_event_subscriptions(uid, missing)
                self._subs.setdefault(bare, set()).update(
                    item["subscription_id"] for item in created if item.get("subscription_id")
                )

        # Delete subscriptions for broadcasters no longer monitored. A slug
        # that did not resolve above has no uid, so this pass cannot tell its
        # channel apart from an unmonitored one. Skip the whole pass then: a
        # missed deletion is harmless, a wrong deletion kills event delivery.
        unresolved = {bare_name(c) for c in kick_channels} - set(desired)
        if unresolved:
            logger.warning(
                "[kick_webhook] %d kick channel(s) unresolved, skipping subscription cleanup", len(unresolved)
            )
            return len(desired)
        desired_ids = set(desired.values())
        for uid, subs in by_user.items():
            if uid in desired_ids:
                continue
            ids = [s.get("id") for s in subs if s.get("id")]
            if ids:
                await self._api.delete_event_subscriptions(ids)
                for sub in subs:
                    for bare_set in self._subs.values():
                        bare_set.discard(sub.get("id"))

        # Prune bookkeeping for channels no longer monitored.
        for bare in list(self._subs):
            if bare not in desired:
                self._subs.pop(bare, None)

        return len(desired)

    async def _maybe_confirm_delivery(self) -> None:
        """Confirm the setup works on the first verified Kick event after enable.

        A signature-verified POST proves that Kick saved the current URL and
        can reach it. That is the only real signal that the setup is complete.
        This notifies once per enable (enabling re-arms ``setup_notified``)
        and then persists the flag, so it stays silent afterwards.
        """
        wh = self._config.kick.webhook
        if not wh.enabled or wh.setup_notified:
            return
        # Set the flag before the await. Two first events can arrive
        # together, and both would pass the check above otherwise.
        wh.setup_notified = True
        try:
            if self._notifier:
                await self._notifier.notify("\u2705 Kick webhook is working \u2014 first event received from Kick.")
            # save_config writes and fsyncs the file, so keep it off the event
            # loop. This handler serves every webhook request.
            await asyncio.to_thread(save_config, self._config)
        except Exception as e:
            # Nothing was announced and nothing was persisted, so clear the
            # flag: the next event retries the confirmation.
            wh.setup_notified = False
            logger.error("[kick_webhook] setup confirmation failed: %s", e)

    async def add_channel(self, channel: str) -> None:
        """Subscribe a newly added kick channel to both events. This logs errors."""
        if not is_kick_channel(channel):
            return
        bare = bare_name(channel)
        try:
            statuses = await self._api.get_channel_statuses([bare])
            uid = (statuses.get(bare) or {}).get("broadcaster_user_id")
            if uid is None:
                logger.warning("[kick_webhook] channel not found for webhook subs: %s", channel)
                return
            created = await self._api.create_event_subscriptions(uid, [self.EVENT_LIVE, self.EVENT_CHAT])
            self._subs.setdefault(bare, set()).update(
                item["subscription_id"] for item in created if item.get("subscription_id")
            )
        except Exception as e:
            logger.error("[kick_webhook] add_channel failed for %s: %s", channel, e)

    async def remove_channel(self, channel: str) -> None:
        """Delete all recorded subscriptions for a removed kick channel."""
        if not is_kick_channel(channel):
            return
        bare = bare_name(channel)
        ids = self._subs.get(bare)
        if not ids:
            self._subs.pop(bare, None)
            return
        try:
            await self._api.delete_event_subscriptions(list(ids))
        except Exception as e:
            # The ids stay tracked, so a later sync retries the delete. A pop
            # before the call would lose the only record of live subs.
            logger.error("[kick_webhook] remove_channel failed for %s: %s", channel, e)
            return
        self._subs.pop(bare, None)

    async def sync_channels(self, channels: list[str]) -> None:
        """Run one reconcile now. /reload and the post-enable path call this."""
        try:
            await self._sync_subscriptions(channels)
        except Exception as e:
            logger.error("[kick_webhook] sync_channels failed: %s", e)
