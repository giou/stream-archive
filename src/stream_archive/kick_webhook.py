import asyncio
import base64
import contextlib
import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx
from aiohttp import web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from stream_archive.config import AppConfig, is_kick_channel, kick_bare_name, save_config

logger = logging.getLogger(__name__)

# Signed events are accepted only within this freshness window. The header
# timestamp is covered by Kick's signature, so an attacker cannot move it —
# this is what stops replay of captured events (recording kill / forged chat).
_VERIFY_WINDOW_S = 300
# message_id dedup store: one entry per unique signed event, kept for the
# freshness window. Bounded so a flood cannot grow memory.
_MAX_SEEN_IDS = 50_000
# A failed signature triggers a public-key refetch (key-rotation retry). This
# negative cache bounds that refetch so unauthenticated floods cannot force an
# outbound Kick API call per request (rate-limit self-DoS).
_KEY_REFETCH_INTERVAL_S = 60
# Per-client-IP token bucket; a coarse backstop behind signature verification.
# Behind a tunnel every request shares the tunnel's origin IP, so the budget
# is sized for aggregate legit chat volume, not per-event precision.
_RATE_LIMIT_PER_IP = 1200  # requests per window
_RATE_LIMIT_WINDOW_S = 60
_MAX_RATE_LIMIT_IPS = 10_000
# Cap on concurrent in-flight webhook requests; flood protection.
_MAX_CONCURRENT = 16
# Kick-side sync failures (5xx) alert only after persisting this long, so a
# transient API outage doesn't page the admin. Other failures notify at once.
_SYNC_SERVER_ERROR_DELAY_S = 600


def _is_server_error(exc: Exception) -> bool:
    """True when Kick's API itself failed (5xx): an outage on their side.

    Config/auth problems (4xx), connectivity errors, and timeouts still
    notify immediately — they may need action on our side.
    """
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


class _RateLimiter:
    """Per-key token bucket (key = client IP), with a bounded bucket table."""

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
                    if tokens >= self._max:  # fully refilled: safe to evict
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
    """Kick sends ISO-8601; epoch seconds are accepted too. None when unparseable."""
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return None


class KickWebhook:
    EVENT_LIVE = "livestream.status.updated"  # v1
    EVENT_CHAT = "chat.message.sent"  # v1

    def __init__(self, config: AppConfig, monitor: Any, recorder: Any, kick_api: Any, notifier: Any):
        self._config = config
        self._monitor = monitor
        self._recorder = recorder
        self._api = kick_api
        self._notifier = notifier
        self._runner: Any = None
        self._site: Any = None
        self._sync_task: asyncio.Task[Any] | None = None
        self._sync_failed_notified = False
        self._sync_failing_since: float | None = None  # monotonic start of current failure episode
        self._sync_error_logged = False
        self._subs: dict[str, set[str]] = {}  # bare slug -> set(subscription ids)
        self._seen_ids: dict[str, float] = {}  # message_id -> expires (monotonic)
        self._rate_limiter = _RateLimiter(_RATE_LIMIT_PER_IP, _RATE_LIMIT_WINDOW_S)
        self._sem = asyncio.Semaphore(_MAX_CONCURRENT)
        self._next_key_refetch = 0.0  # monotonic; gates the rotation refetch
        self._app = web.Application()
        self._app.router.add_post("/kick/webhook", self._handle)

    async def start(self) -> None:
        """Bind the HTTP listener and start the subscription sync loop. Idempotent."""
        if self._runner is not None:
            return
        wh = self._config.kick.webhook
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, wh.listen_host, wh.listen_port)
        await self._site.start()
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info(
            "[kick_webhook] listening on http://%s:%s (public: %s)",
            wh.listen_host,
            wh.listen_port,
            wh.public_url or "(none)",
        )

    async def close(self) -> None:
        """Stop the sync loop and the HTTP listener. Idempotent."""
        task = self._sync_task
        self._sync_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        site = self._site
        self._site = None
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
                # Log once per failure episode; the loop retries every
                # interval anyway, so per-cycle error lines are just spam.
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
        """Alert on a sync failure; one notification per failure episode.

        A Kick-side (5xx) failure only alerts once it has persisted for
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
        await self._notifier.notify(
            "\u26a0\ufe0f Kick webhook subscriptions out of sync \u2014 is the "
            "public URL configured in the Kick app (Settings \u2192 Developer \u2192 "
            "your app \u2192 Enable webhooks)? "
            f"{self._config.kick.webhook.public_url}\n"
            f"Error: {detail}"
        )

    async def _handle(self, request: Any) -> Any:
        client = request.remote or "unknown"
        if not self._rate_limiter.allow(client):
            return web.Response(status=429, text="too many requests")
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return web.Response(status=503, text="busy")
        try:
            body = await request.read()
            event_type = request.headers.get("Kick-Event-Type", "")
            if not await self._verify(request, body):
                # event_type is attacker-controlled: only log known values.
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
            except Exception:
                # Roll back the dedup mark so Kick's retry is processed instead
                # of answered 200 as a replay (aiohttp answers the raise 500).
                if msg_id:
                    self._seen_ids.pop(msg_id, None)
                raise
            return web.Response(status=200, text="ok")
        finally:
            self._sem.release()

    def _remember_id(self, message_id: str | None) -> bool:
        """True when the message id is new within the freshness window; False for replays."""
        if not message_id:
            return True
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
        try:
            message = f"{message_id}.{timestamp}.{body.decode()}".encode()
            public_key = await self._api.get_public_key()
            self._verify_signature(public_key, message, signature)
            return True
        except Exception:
            pass
        # The key may have rotated: refetch (rate-limited) and retry once.
        # The refetch bypasses the cache (force), so rotation is actually
        # detected; a flood of bad signatures keeps verifying against the
        # cached key with no outbound calls, and only one refetch per
        # interval is ever attempted.
        now = time.monotonic()
        if now < self._next_key_refetch:
            return False
        self._next_key_refetch = now + _KEY_REFETCH_INTERVAL_S
        try:
            public_key = await self._api.get_public_key(force=True)
            self._verify_signature(public_key, message, signature)
            return True
        except Exception:
            return False

    def _verify_signature(self, public_key_pem: Any, message: bytes, signature: bytes) -> None:
        key = serialization.load_pem_public_key(
            public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem
        )
        if not isinstance(key, rsa.RSAPublicKey):
            raise ValueError("webhook public key is not an RSA key")
        key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())

    async def _dispatch_live(self, body: bytes) -> None:
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("[kick_webhook] invalid livestream event body")
            return
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
            return
        broadcaster = event.get("broadcaster") or {}
        slug = broadcaster.get("channel_slug")
        if not slug:
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
        await self._recorder.add_kick_chat(f"kick:{slug}", payload)

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
            existing_events = {e.get("name") for s in by_user.get(uid, []) for e in (s.get("events") or [])}
            missing = [ev for ev in (self.EVENT_LIVE, self.EVENT_CHAT) if ev not in existing_events]
            if missing:
                created = await self._api.create_event_subscriptions(uid, missing)
                self._subs.setdefault(bare, set()).update(
                    item["subscription_id"] for item in created if item.get("subscription_id")
                )

        # Delete subscriptions for broadcasters no longer monitored.
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
        """Working confirmation: fires on the first verified Kick event per enable.

        A signature-verified POST proves Kick has the current URL saved and can
        reach it \u2014 the only real signal that the setup is complete. Fires
        once per enable (``setup_notified`` is re-armed by enabling), then
        persists the flag so it stays silent afterwards.
        """
        try:
            wh = self._config.kick.webhook
            if not wh.enabled or wh.setup_notified:
                return
            if self._notifier:
                await self._notifier.notify("\u2705 Kick webhook is working \u2014 first event received from Kick.")
            wh.setup_notified = True
            save_config(self._config)
        except Exception as e:
            logger.error("[kick_webhook] setup confirmation failed: %s", e)

    async def add_channel(self, channel: str) -> None:
        """Subscribe a newly added kick channel to both events; errors logged."""
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
        """Immediate one-shot reconcile (called by /reload and after enabling)."""
        try:
            await self._sync_subscriptions(channels)
        except Exception as e:
            logger.error("[kick_webhook] sync_channels failed: %s", e)
