import asyncio
import contextlib
import json
import logging
from typing import Any

import websockets

from stream_archive.config import AppConfig, bare_name, is_kick_channel

logger = logging.getLogger(__name__)

BASE_WS_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=60"
WELCOME_TIMEOUT = 30


class EventSubClient:
    """EventSub over one conduit WebSocket shard, authenticated with the app token.

    The client calls the monitor's handle_online and handle_offline entry points.
    The Helix poll in scheduler.py stays as the reconciliation and fallback path.
    """

    def __init__(self, twitch_api: Any, monitor: Any, config: AppConfig):
        self._api = twitch_api
        self._monitor = monitor
        self._config = config
        self._conduit_id: str | None = None
        self._session_id: str | None = None
        self._subs: dict[str, dict[str, str]] = {}  # channel -> {"online": sub_id, "offline": sub_id}
        self._user_ids: dict[str, str] = {}  # channel -> helix user id
        self._id_to_channel: dict[str, str] = {}  # helix user id -> channel
        self._reconnect_url: str | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._status_error: str | None = None
        self._subscribed = False
        self._ws: Any = None
        self._task: asyncio.Task[Any] | None = None
        # Hold strong refs to in-flight dispatch tasks. The CPython GC drops
        # unreferenced tasks, which would silently discard a live/offline
        # event mid-flight.
        self._dispatch_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        if not self._config.eventsub.enabled:
            logger.info("[eventsub] disabled, polling only")
            self._ready.set()
            return
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def close(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    def status(self) -> str:
        if not self._config.eventsub.enabled:
            return "EventSub: disabled (polling only)"
        if self._status_error:
            return f"EventSub: unavailable ({self._status_error}) \u2014 polling only"
        if not self._subscribed:
            return "EventSub: connecting\u2026"
        return f"EventSub: connected via conduit ({len(self._subs)} channels subscribed)"

    async def add_channel(self, channel: str) -> None:
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

    async def remove_channel(self, channel: str) -> None:
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

    async def sync_channels(self, channels: list[str]) -> None:
        channels = [c for c in channels if not is_kick_channel(c)]
        for ch in list(self._subs):
            if ch not in channels:
                await self.remove_channel(ch)
        for ch in channels:
            if ch not in self._subs:
                await self.add_channel(ch)

    async def _run(self) -> None:
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

    async def _sleep_or_stop(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _connect_and_listen(self) -> None:
        try:
            if self._conduit_id is None and not await self._ensure_conduit():
                return
            url = self._reconnect_url or BASE_WS_URL
            self._reconnect_url = None
            self._ws = await websockets.connect(url)
            try:
                welcome = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=WELCOME_TIMEOUT))
            except TimeoutError:
                logger.error("[eventsub] timed out waiting for session_welcome, reconnecting")
                return
            if welcome.get("metadata", {}).get("message_type") != "session_welcome":
                logger.error(
                    "[eventsub] expected session_welcome, got %s", welcome.get("metadata", {}).get("message_type")
                )
                return
            session = welcome["payload"]["session"]
            self._session_id = session["id"]
            keepalive = session["keepalive_timeout_seconds"]
            await self._activate_shard()
            if not self._subscribed:
                await self._subscribe_all()
                self._subscribed = True
                self._ready.set()
                twitch_count = len([c for c in self._config.channels if not is_kick_channel(c)])
                logger.info(
                    "[eventsub] session connected, subscribed %d/%d channels",
                    len(self._subs),
                    twitch_count,
                )
            while not self._stop.is_set():
                msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=keepalive + 30))
                if await self._handle_message(msg):
                    return
        except TimeoutError:
            logger.error("[eventsub] keepalive timeout, reconnecting")
            return
        except websockets.ConnectionClosed as e:
            if e.code == 4007:
                # Code 4007 is Twitch's normal server-initiated reconnect.
                # Twitch sends the session_reconnect message before it.
                logger.info("[eventsub] reconnect requested by Twitch (code=4007)")
            elif e.code == 1006:
                # Abnormal closure. Twitch deploys cause this often. The
                # reconnect and backoff loop recovers on its own.
                logger.warning("[eventsub] connection closed abnormally (code=1006), reconnecting")
            else:
                logger.error("[eventsub] connection closed (code=%s), reconnecting", e.code)
            return
        finally:
            ws = self._ws
            self._ws = None
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()

    async def _ensure_conduit(self) -> bool:
        """Delete every existing conduit, then create one.

        Deleting a conduit also deletes its subscriptions.
        """
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

    async def _activate_shard(self) -> None:
        result = await self._api.update_conduit_shards(self._conduit_id, self._session_id)
        if result.get("status") != "enabled":
            logger.warning("[eventsub] shard status: %s", result.get("status"))

    async def _subscribe_all(self) -> None:
        channels = [c for c in self._config.channels if not is_kick_channel(c)]
        try:
            resolved = await self._api.resolve_user_ids([bare_name(c) for c in channels])
        except Exception as e:
            logger.error("[eventsub] resolve_user_ids failed: %s", e)
            resolved = {}
        identity_by_bare = {bare_name(c): c for c in channels}
        user_ids = {identity_by_bare[bare]: uid for bare, uid in resolved.items() if bare in identity_by_bare}
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

    async def _create_channel_subs(self, channel: str, uid: str) -> None:
        def payload(sub_type: str) -> dict[str, Any]:
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
        for kind, (status, body) in zip(("online", "offline"), results, strict=True):
            await self._handle_subscribe_response(channel, kind, status, body)

    async def _handle_subscribe_response(self, channel: str, kind: str, status: int, body: dict[str, Any]) -> None:
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
            except StopIteration, KeyError:
                logger.error("[eventsub] could not resolve existing subscription id for %s", channel)
        elif status in (400, 403):
            logger.error("[eventsub] subscription rejected for %s (%s); channel relies on polling", channel, status)
        else:
            logger.error("[eventsub] unexpected status %s creating %s for %s", status, sub_type, channel)

    async def _handle_message(self, msg: dict[str, Any]) -> bool:
        """Dispatch one WebSocket message. Returns True when the socket must reconnect."""
        mtype = msg.get("metadata", {}).get("message_type")
        if mtype == "notification":
            t = asyncio.create_task(self._dispatch(msg))
            self._dispatch_tasks.add(t)
            t.add_done_callback(self._dispatch_tasks.discard)
        elif mtype == "session_keepalive":
            pass
        elif mtype == "session_reconnect":
            self._reconnect_url = msg["payload"]["session"]["reconnect_url"]
            return True
        elif mtype == "revocation":
            await self._handle_revocation(msg)
        return False

    async def _handle_revocation(self, msg: dict[str, Any]) -> None:
        sub = msg.get("payload", {}).get("subscription", {})
        sub_id = sub.get("id")
        logger.warning("[eventsub] subscription revoked: %s (%s)", sub.get("type"), sub_id)
        for channel, kinds in list(self._subs.items()):
            for kind, sid in list(kinds.items()):
                if sid == sub_id:
                    del self._subs[channel][kind]

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        try:
            sub_type = msg.get("metadata", {}).get("subscription_type")
            event = msg.get("payload", {}).get("event", {})
            user_id = event.get("broadcaster_user_id")
            channel = self._id_to_channel.get(user_id)
            if channel is None or channel not in self._config.channels:
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
