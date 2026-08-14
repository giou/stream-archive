import asyncio
import json
import logging

import websockets

from src.stream_archive.config import bare_name, is_kick_channel

logger = logging.getLogger(__name__)

BASE_WS_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=60"
WELCOME_TIMEOUT = 30


class EventSubClient:
    """EventSub over a single conduit WebSocket shard, authenticated with the app token.

    Drives the monitor's handle_online/handle_offline entry points; the Helix poll
    in scheduler.py remains the reconciliation/fallback path.
    """

    def __init__(self, twitch_api, monitor, config):
        self._api = twitch_api
        self._monitor = monitor
        self._config = config
        self._conduit_id = None
        self._session_id = None
        self._subs = {}            # channel -> {"online": sub_id, "offline": sub_id}
        self._user_ids = {}        # channel -> helix user id
        self._id_to_channel = {}   # helix user id -> channel
        self._reconnect_url = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._status_error = None
        self._subscribed = False
        self._ws = None
        self._task = None

    async def start(self):
        if not self._config.get("eventsub", {}).get("enabled", True):
            logger.info("[eventsub] disabled, polling only")
            self._ready.set()
            return
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, timeout=15.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self):
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def status(self):
        if not self._config.get("eventsub", {}).get("enabled", True):
            return "EventSub: disabled (polling only)"
        if self._status_error:
            return f"EventSub: unavailable ({self._status_error}) \u2014 polling only"
        if not self._subscribed:
            return "EventSub: connecting\u2026"
        return f"EventSub: connected via conduit ({len(self._subs)} channels subscribed)"

    async def add_channel(self, channel):
        if self._conduit_id is None or self._session_id is None:
            logger.debug("[eventsub] no live session, not subscribing %s", channel)
            return
        if channel in self._subs:
            return
        try:
            uid = (await self._api.resolve_user_ids([bare_name(channel)])).get(bare_name(channel))
        except Exception as e:
            logger.error("[eventsub] resolve_user_ids failed for %s: %s", channel, e)
            return
        if uid is None:
            logger.warning("[eventsub] could not resolve user id for %s, skipping", channel)
            return
        self._user_ids[channel] = uid
        self._id_to_channel[uid] = channel
        await self._create_channel_subs(channel, uid)

    async def remove_channel(self, channel):
        if self._conduit_id is None or self._session_id is None:
            logger.debug("[eventsub] no live session, not unsubscribing %s", channel)
            return
        for sub_id in self._subs.pop(channel, {}).values():
            try:
                await self._api.delete_eventsub_subscription(sub_id)
            except Exception as e:
                logger.error("[eventsub] failed to delete subscription for %s: %s", channel, e)
        uid = self._user_ids.pop(channel, None)
        if uid is not None:
            self._id_to_channel.pop(uid, None)

    async def sync_channels(self, channels):
        channels = [c for c in channels if not is_kick_channel(c)]
        for ch in list(self._subs):
            if ch not in channels:
                await self.remove_channel(ch)
        for ch in channels:
            if ch not in self._subs:
                await self.add_channel(ch)

    async def _run(self):
        backoff = 5.0
        while not self._stop.is_set():
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.error("[eventsub] connection lost: %s", e)
            if self._reconnect_url:
                backoff = 5.0
            await self._sleep_or_stop(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _sleep_or_stop(self, seconds):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _connect_and_listen(self):
        try:
            if self._conduit_id is None:
                if not await self._ensure_conduit():
                    return
            url = self._reconnect_url or BASE_WS_URL
            self._reconnect_url = None
            self._ws = await websockets.connect(url)
            try:
                welcome = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=WELCOME_TIMEOUT))
            except asyncio.TimeoutError:
                logger.error("[eventsub] timed out waiting for session_welcome, reconnecting")
                return
            if welcome.get("metadata", {}).get("message_type") != "session_welcome":
                logger.error("[eventsub] expected session_welcome, got %s", welcome.get("metadata", {}).get("message_type"))
                return
            session = welcome["payload"]["session"]
            self._session_id = session["id"]
            keepalive = session["keepalive_timeout_seconds"]
            await self._activate_shard()
            if not self._subscribed:
                await self._subscribe_all()
                self._subscribed = True
                self._ready.set()
                twitch_count = len([c for c in self._config["channels"] if not is_kick_channel(c)])
                logger.info(
                    "[eventsub] session connected, subscribed %d/%d channels",
                    len(self._subs),
                    twitch_count,
                )
            while not self._stop.is_set():
                msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=keepalive + 30))
                if await self._handle_message(msg):
                    return
        except asyncio.TimeoutError:
            logger.error("[eventsub] keepalive timeout, reconnecting")
            return
        except websockets.ConnectionClosed as e:
            logger.error("[eventsub] connection closed (code=%s), reconnecting", e.code)
            return
        finally:
            ws = self._ws
            self._ws = None
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

    async def _ensure_conduit(self):
        """Delete existing conduits (cascade removes their subscriptions) and create one."""
        try:
            for conduit in await self._api.list_conduits():
                await self._api.delete_conduit(conduit["id"])
            created = await self._api.create_conduit(1)
            self._conduit_id = created["id"]
            self._status_error = None
            logger.info("[eventsub] conduit created: %s", self._conduit_id)
            return True
        except Exception as e:
            logger.error("[eventsub] conduit setup failed: %s", e)
            self._status_error = str(e)[:80] or type(e).__name__
            self._ready.set()
            return False

    async def _activate_shard(self):
        result = await self._api.update_conduit_shards(self._conduit_id, self._session_id)
        if result.get("status") != "enabled":
            logger.warning("[eventsub] shard status: %s", result.get("status"))

    async def _subscribe_all(self):
        channels = [c for c in self._config["channels"] if not is_kick_channel(c)]
        try:
            resolved = await self._api.resolve_user_ids([bare_name(c) for c in channels])
        except Exception as e:
            logger.error("[eventsub] resolve_user_ids failed: %s", e)
            resolved = {}
        identity_by_bare = {bare_name(c): c for c in channels}
        user_ids = {
            identity_by_bare[bare]: uid
            for bare, uid in resolved.items()
            if bare in identity_by_bare
        }
        self._user_ids = user_ids
        self._id_to_channel = {uid: ch for ch, uid in user_ids.items()}
        for channel in channels:
            uid = user_ids.get(channel)
            if uid is None:
                logger.warning("[eventsub] could not resolve user id for %s, skipping", channel)
                continue
            await self._create_channel_subs(channel, uid)
        if len(self._subs) < len(channels):
            poll_only = [ch for ch in channels if ch not in self._subs]
            logger.info("[eventsub] polling only for: %s", ", ".join(poll_only))

    async def _create_channel_subs(self, channel, uid):
        def payload(sub_type):
            return {
                "type": sub_type,
                "version": "1",
                "condition": {"broadcaster_user_id": uid},
                "transport": {"method": "conduit", "conduit_id": self._conduit_id},
            }

        results = await asyncio.gather(
            self._api.create_eventsub_subscription(payload("stream.online")),
            self._api.create_eventsub_subscription(payload("stream.offline")),
        )
        for kind, (status, body) in zip(("online", "offline"), results):
            await self._handle_subscribe_response(channel, kind, status, body)

    async def _handle_subscribe_response(self, channel, kind, status, body):
        sub_type = f"stream.{kind}"
        if status == 202:
            self._subs.setdefault(channel, {})[kind] = body["data"][0]["id"]
        elif status == 409:
            logger.warning("[eventsub] subscription %s already exists for %s, resolving id", sub_type, channel)
            try:
                subs = await self._api.list_eventsub_subscriptions()
                existing = next(
                    s
                    for s in subs
                    if s["type"] == sub_type
                    and s["condition"].get("broadcaster_user_id") == self._user_ids.get(channel)
                )
                self._subs.setdefault(channel, {})[kind] = existing["id"]
            except (StopIteration, KeyError):
                logger.error("[eventsub] could not resolve existing subscription id for %s", channel)
        elif status in (400, 403):
            logger.error("[eventsub] subscription rejected for %s (%s); channel relies on polling", channel, status)
        else:
            logger.error("[eventsub] unexpected status %s creating %s for %s", status, sub_type, channel)

    async def _handle_message(self, msg):
        """Dispatch one WebSocket message. Returns True when the socket must reconnect."""
        mtype = msg.get("metadata", {}).get("message_type")
        if mtype == "notification":
            asyncio.create_task(self._dispatch(msg))
        elif mtype == "session_keepalive":
            pass
        elif mtype == "session_reconnect":
            self._reconnect_url = msg["payload"]["session"]["reconnect_url"]
            return True
        elif mtype == "revocation":
            await self._handle_revocation(msg)
        return False

    async def _handle_revocation(self, msg):
        sub = msg.get("payload", {}).get("subscription", {})
        sub_id = sub.get("id")
        logger.warning("[eventsub] subscription revoked: %s (%s)", sub.get("type"), sub_id)
        for channel, kinds in list(self._subs.items()):
            for kind, sid in list(kinds.items()):
                if sid == sub_id:
                    del self._subs[channel][kind]

    async def _dispatch(self, msg):
        try:
            sub_type = msg.get("metadata", {}).get("subscription_type")
            event = msg.get("payload", {}).get("event", {})
            user_id = event.get("broadcaster_user_id")
            channel = self._id_to_channel.get(user_id)
            if channel is None or channel not in self._config["channels"]:
                logger.debug("[eventsub] event for unknown channel %s, ignoring", user_id)
                return
            if sub_type == "stream.online":
                if channel in self._monitor._live_channels:
                    logger.debug("[eventsub] %s already handled as live, ignoring", channel)
                    return
                stream = await self._api.get_stream(user_id)
                if stream is None:
                    logger.debug("[eventsub] %s already offline, ignoring", channel)
                    return
                await self._monitor.handle_online(
                    channel, stream.get("title"), stream.get("game_name"), user_id, self._config
                )
            elif sub_type == "stream.offline":
                await self._monitor.handle_offline(channel, self._config)
            else:
                logger.debug("[eventsub] ignoring event type %s", sub_type)
        except Exception as e:
            logger.error("[eventsub] dispatch failed: %s", e)
