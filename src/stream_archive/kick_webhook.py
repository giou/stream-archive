import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

import httpx
from aiohttp import web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from stream_archive.config import (
    KICK_PREFIX,
    AppConfig,
    is_kick_channel,
    kick_bare_name,
    save_config,
    webhook_public_url,
)

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
# Cap on concurrent in-flight webhook requests. This blunts request floods.
_MAX_CONCURRENT = 16
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
    """Token bucket keyed by client IP, with a bounded bucket table."""

    def __init__(self, max_requests: int, window_s: int, max_keys: int = _MAX_RATE_LIMIT_IPS) -> None:
        self._max = max_requests
        self._window = window_s
        self._max_keys = max_keys
        self._buckets: dict[str, list[float]] = {}  # key -> [tokens, last_refill (monotonic)]

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        buckets = self._buckets
        bucket = buckets.get(key)
        if bucket is None:
            if len(buckets) >= self._max_keys:
                for k, (tokens, _) in list(buckets.items()):
                    if tokens >= self._max:  # fully refilled, so eviction loses nothing
                        del buckets[k]
                if len(buckets) >= self._max_keys:
                    buckets.pop(next(iter(buckets)))
            buckets[key] = [self._max, now]
            return True
        tokens, refill = bucket
        tokens = min(self._max, tokens + (now - refill) * (self._max / self._window))
        if tokens < 1:
            bucket[0], bucket[1] = tokens, now
            return False
        bucket[0], bucket[1] = tokens - 1, now
        return True


def _parse_timestamp(value: str) -> float | None:
    """Kick sends ISO-8601. Epoch seconds are accepted too. None when unparseable."""
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return None


class MonitorProtocol(Protocol):
    """The monitor calls that the receiver needs."""

    async def handle_online(
        self, channel: str, title: str | None, game: str | None, user_id: str | None, config: AppConfig
    ) -> None: ...
    async def handle_offline(self, channel: str, config: AppConfig) -> None: ...


class RecorderProtocol(Protocol):
    """The recorder call that the receiver needs."""

    async def add_kick_chat(self, channel: str, payload: dict[str, Any]) -> None: ...


class KickAPIProtocol(Protocol):
    """The Kick API calls that the receiver needs."""

    async def get_public_key(self, force: bool = False) -> str | None: ...
    async def get_channel_statuses(self, slugs: list[str]) -> dict[str, dict[str, Any]]: ...
    async def list_event_subscriptions(self) -> list[dict[str, Any]]: ...
    async def create_event_subscriptions(self, broadcaster_user_id: int, events: list[str]) -> list[dict[str, Any]]: ...
    async def delete_event_subscriptions(self, ids: list[str]) -> None: ...


class NotifierProtocol(Protocol):
    """The notification call that the receiver needs."""

    async def notify(self, message: str) -> None: ...


class KickWebhook:
    EVENT_LIVE = "livestream.status.updated"  # v1
    EVENT_CHAT = "chat.message.sent"  # v1

    def __init__(
        self,
        config: AppConfig,
        monitor: MonitorProtocol,
        recorder: RecorderProtocol,
        kick_api: KickAPIProtocol,
        notifier: NotifierProtocol | None,
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
            await self._stop_sync()
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
        self._sync_failed_notified = True
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

    async def _handle(self, request: Any) -> Any:
        if not self._config.kick.webhook.enabled:
            # The feature is off, so the route behaves as if it did not exist.
            # The listener can stay up because the control API uses it.
            return web.Response(status=404, text="not found")
        client = request.remote or "unknown"
        if not self._rate_limiter.allow(client):
            return web.Response(status=429, text="too many requests")
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=0.1)
        except TimeoutError:
            return web.Response(status=503, text="busy", headers={"Retry-After": "1"})
        try:
            try:
                body = await asyncio.wait_for(request.read(), timeout=5)
            except TimeoutError:
                return web.Response(status=413, text="body too slow")
            event_type = request.headers.get("Kick-Event-Type", "")
            try:
                verified = await self._verify(request, body)
            except UnicodeDecodeError:
                logger.warning("[kick_webhook] event body is not valid UTF-8")
                return web.Response(status=400, text="bad encoding")
            if not verified:
                # Attackers control event_type, so log only known values.
                known = event_type if event_type in (self.EVENT_LIVE, self.EVENT_CHAT) else "unknown"
                logger.warning("[kick_webhook] signature verification failed (event=%s)", known)
                return web.Response(status=401, text="unauthorized")
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

    async def _verify(self, request: Any, body: bytes) -> bool:
        message_id = request.headers.get("Kick-Event-Message-Id")
        timestamp = request.headers.get("Kick-Event-Message-Timestamp")
        signature_b64 = request.headers.get("Kick-Event-Signature")
        if not message_id or not timestamp or not signature_b64:
            return False
        try:
            signature = base64.b64decode(signature_b64)
        except Exception:
            return False
        event_time = _parse_timestamp(timestamp)
        if event_time is None or abs(time.time() - event_time) > _VERIFY_WINDOW_S:
            logger.warning("[kick_webhook] event timestamp outside freshness window")
            return False
        # Strict decode: a non-UTF-8 body is corrupt, not a rotation. It
        # raises UnicodeDecodeError, and the caller answers 400.
        body_text = body.decode("utf-8", errors="strict")
        try:
            message = f"{message_id}.{timestamp}.{body_text}".encode()
            public_key = await self._api.get_public_key()
            self._verify_signature(public_key, message, signature)
        except Exception:
            pass
        else:
            return True
        # The failure can mean that Kick rotated the key. Refetch
        # (rate-limited) and retry once. The refetch bypasses the cache
        # (force), so the code detects rotation reliably. Meanwhile, a flood
        # of bad signatures keeps verifying against the cached key with no
        # outbound calls. The code attempts only one refetch per interval.
        now = time.monotonic()
        if now < self._next_key_refetch:
            return False
        self._next_key_refetch = now + _KEY_REFETCH_INTERVAL_S
        try:
            public_key = await self._api.get_public_key(force=True)
            self._verify_signature(public_key, message, signature)
        except Exception:
            return False
        else:
            return True

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
        channel = f"kick:{slug}"
        if channel not in self._config.channels:
            logger.debug("[kick_webhook] livestream event for unmonitored channel %s, ignoring", channel)
            return
        if event.get("is_live"):
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
        channel = f"kick:{slug}"
        if channel not in self._config.channels:
            logger.debug("[kick_webhook] chat event for unmonitored channel %s, ignoring", channel)
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
            statuses = await self._api.get_channel_statuses([kick_bare_name(c) for c in kick_channels])
            for c in kick_channels:
                bare = kick_bare_name(c)
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
        unresolved = {kick_bare_name(c) for c in kick_channels} - set(desired)
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
        try:
            wh = self._config.kick.webhook
            if not wh.enabled or wh.setup_notified:
                return
            if self._notifier:
                await self._notifier.notify("\u2705 Kick webhook is working \u2014 first event received from Kick.")
            wh.setup_notified = True
            # save_config writes and fsyncs the file, so keep it off the event
            # loop. This handler serves every webhook request.
            await asyncio.to_thread(save_config, self._config)
        except Exception as e:
            logger.error("[kick_webhook] setup confirmation failed: %s", e)

    async def add_channel(self, channel: str) -> None:
        """Subscribe a newly added kick channel to both events. This logs errors."""
        if not is_kick_channel(channel):
            return
        bare = kick_bare_name(channel)
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
        bare = kick_bare_name(channel)
        ids = self._subs.pop(bare, set())
        if not ids:
            return
        try:
            await self._api.delete_event_subscriptions(list(ids))
        except Exception as e:
            logger.error("[kick_webhook] remove_channel failed for %s: %s", channel, e)

    async def sync_channels(self, channels: list[str]) -> None:
        """Run one reconcile now. /reload and the post-enable path call this."""
        try:
            await self._sync_subscriptions(channels)
        except Exception as e:
            logger.error("[kick_webhook] sync_channels failed: %s", e)
