import asyncio
import base64
import json
import logging

from aiohttp import web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.stream_archive.config import is_kick_channel, kick_bare_name, save_config

logger = logging.getLogger(__name__)


class KickWebhook:
    EVENT_LIVE = "livestream.status.updated"   # v1
    EVENT_CHAT = "chat.message.sent"           # v1

    def __init__(self, config, monitor, recorder, kick_api, notifier):
        self._config = config
        self._monitor = monitor
        self._recorder = recorder
        self._api = kick_api
        self._notifier = notifier
        self._runner = None
        self._site = None
        self._sync_task = None
        self._sync_failed_notified = False
        self._subs = {}   # bare slug -> set(subscription ids)
        self._app = web.Application()
        self._app.router.add_post("/kick/webhook", self._handle)

    async def start(self):
        """Bind the HTTP listener and start the subscription sync loop. Idempotent."""
        if self._runner is not None:
            return
        wh = self._config["kick"]["webhook"]
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, wh["listen_host"], wh["listen_port"])
        await self._site.start()
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info(
            "[kick_webhook] listening on http://%s:%s (public: %s)",
            wh["listen_host"], wh["listen_port"], wh.get("public_url") or "(none)",
        )

    async def close(self):
        """Stop the sync loop and the HTTP listener. Idempotent."""
        task = self._sync_task
        self._sync_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        site = self._site
        self._site = None
        if site is not None:
            try:
                await site.stop()
            except Exception:
                pass
        runner = self._runner
        self._runner = None
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                pass

    async def _sync_loop(self):
        while self._runner is not None:
            try:
                subscribed = await self._sync_subscriptions(self._config["channels"])
            except Exception as e:
                logger.error("[kick_webhook] subscription sync failed: %s", e)
                if not self._sync_failed_notified and self._notifier:
                    self._sync_failed_notified = True
                    await self._notifier.notify(
                        "\u26a0\ufe0f Kick webhook subscriptions out of sync \u2014 is the "
                        "public URL configured in the Kick app (Settings \u2192 Developer \u2192 "
                        "your app \u2192 Enable webhooks)? "
                        f"{self._config['kick']['webhook'].get('public_url', '')}"
                    )
            else:
                self._sync_failed_notified = False
            await asyncio.sleep(self._config["monitoring_interval"])

    async def _handle(self, request):
        body = await request.read()
        event_type = request.headers.get("Kick-Event-Type", "")
        if not await self._verify(request, body):
            logger.warning("[kick_webhook] signature verification failed (event=%s)", event_type)
            return web.Response(status=401, text="unauthorized")
        await self._maybe_confirm_delivery()
        if event_type == self.EVENT_LIVE:
            await self._dispatch_live(body)
        elif event_type == self.EVENT_CHAT:
            await self._dispatch_chat(body)
        else:
            logger.debug("[kick_webhook] unknown event type %r", event_type)
            return web.Response(status=204)
        return web.Response(status=200, text="ok")

    async def _verify(self, request, body) -> bool:
        message_id = request.headers.get("Kick-Event-Message-Id")
        timestamp = request.headers.get("Kick-Event-Message-Timestamp")
        signature_b64 = request.headers.get("Kick-Event-Signature")
        if not message_id or not timestamp or not signature_b64:
            return False
        try:
            signature = base64.b64decode(signature_b64)
        except Exception:
            return False
        message = f"{message_id}.{timestamp}.{body.decode()}".encode()
        try:
            public_key = await self._api.get_public_key()
            self._verify_signature(public_key, message, signature)
            return True
        except Exception:
            pass
        # The key may have rotated: refetch once and retry before failing.
        self._api.clear_public_key_cache()
        try:
            public_key = await self._api.get_public_key()
            self._verify_signature(public_key, message, signature)
            return True
        except Exception:
            return False

    def _verify_signature(self, public_key_pem, message, signature):
        key = serialization.load_pem_public_key(public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem)
        key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())

    async def _dispatch_live(self, body):
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
        if channel not in self._config["channels"]:
            logger.debug("[kick_webhook] livestream event for unmonitored channel %s, ignoring", channel)
            return
        if event.get("is_live"):
            # title/game are None on purpose: the recorder fills them from the
            # streamlink kick plugin's metadata (no extra API call on the hot path).
            await self._monitor.handle_online(channel, None, None, None, self._config)
        else:
            await self._monitor.handle_offline(channel, self._config)

    async def _dispatch_chat(self, body):
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
        badges = [{"text": b.get("text"), "type": b.get("type"), "count": b.get("count")}
                  for b in (identity.get("badges") or [])]
        payload = {
            "message_id": event.get("message_id"),
            "created_at": event.get("created_at"),
            "broadcaster": {"user_id": broadcaster.get("user_id"),
                            "username": broadcaster.get("username"),
                            "profile_picture": broadcaster.get("profile_picture")},
            "sender": {"user_id": sender.get("user_id"), "username": sender.get("username"),
                       "is_verified": sender.get("is_verified"), "is_anonymous": sender.get("is_anonymous"),
                       "profile_picture": sender.get("profile_picture"),
                       "username_color": (identity or {}).get("username_color")},
            "content": event.get("content"),
            "emotes": [{"emote_id": e.get("emote_id"),
                        "positions": [{"s": p.get("s"), "e": p.get("e")} for p in (e.get("positions") or [])]}
                       for e in (event.get("emotes") or [])],
            "badges": badges,
        }
        await self._recorder.add_kick_chat(f"kick:{slug}", payload)

    async def _sync_subscriptions(self, channels):
        """Reconcile webhook subscriptions with the monitored kick channels."""
        desired = {}   # bare slug -> broadcaster_user_id
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
        by_user = {}
        for sub in existing:
            by_user.setdefault(sub.get("broadcaster_user_id"), []).append(sub)

        # Create missing subscriptions for monitored channels.
        for bare, uid in desired.items():
            existing_events = {
                e.get("name")
                for s in by_user.get(uid, [])
                for e in (s.get("events") or [])
            }
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

    async def _maybe_confirm_delivery(self):
        """Working confirmation: fires on the first verified Kick event per enable.

        A signature-verified POST proves Kick has the current URL saved and can
        reach it \u2014 the only real signal that the setup is complete. Fires
        once per enable (``setup_notified`` is re-armed by enabling), then
        persists the flag so it stays silent afterwards.
        """
        try:
            wh = self._config["kick"]["webhook"]
            if not wh.get("enabled") or wh.get("setup_notified"):
                return
            if self._notifier:
                await self._notifier.notify(
                    "\u2705 Kick webhook is working \u2014 first event received from Kick."
                )
            wh["setup_notified"] = True
            save_config(self._config)
        except Exception as e:
            logger.error("[kick_webhook] setup confirmation failed: %s", e)

    async def add_channel(self, channel):
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

    async def remove_channel(self, channel):
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

    async def sync_channels(self, channels):
        """Immediate one-shot reconcile (called by /reload and after enabling)."""
        try:
            await self._sync_subscriptions(channels)
        except Exception as e:
            logger.error("[kick_webhook] sync_channels failed: %s", e)
