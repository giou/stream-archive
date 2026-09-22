import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from stream_archive.config import AppConfig, bare_name, is_kick_channel

logger = logging.getLogger(__name__)

BASE_WS_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=60"
WELCOME_TIMEOUT = 30
# Dedup window and bound mirror the Kick webhook store: one entry per
# unique Twitch message id, swept on overflow.
_DEDUP_WINDOW_S = 300
_MAX_SEEN_IDS = 50_000
# Cap on concurrent notification dispatches. Slow handlers wait on this
# instead of growing tasks without bound.
_MAX_CONCURRENT_DISPATCH = 16
# A slow dispatch drops only its own event, and the poll path reconciles the
# gap. The socket stays up.
_DISPATCH_TIMEOUT_S = 15
# The two event types this client subscribes to for each channel.
_EVENT_KINDS = ("online", "offline")


class EventSubClient:
    """EventSub over one conduit WebSocket shard, authenticated with the app token.

    The client calls the monitor's handle_online and handle_offline entry points.
    The Helix poll in scheduler.py stays as the reconciliation and fallback path.
    """

    def __init__(self, twitch_api: Any, monitor: Any, config: AppConfig):
        self._api = twitch_api
        self._monitor = monitor
        self._config = config
        self._seen_ids: dict[str, float] = {}  # message_id -> expires (monotonic)
        self._dispatch_sem = asyncio.Semaphore(_MAX_CONCURRENT_DISPATCH)
        self._conduit_id: str | None = None
        self._session_id: str | None = None
        self._subs: dict[str, dict[str, str]] = {}  # channel -> {"online": sub_id, "offline": sub_id}
        self._user_ids: dict[str, str] = {}  # channel -> helix user id
        self._id_to_channel: dict[str, str] = {}  # helix user id -> channel
        # The connection loop and the external callers (Telegram, control API)
        # both mutate the three maps above. This lock serializes them.
        self._subs_lock = asyncio.Lock()
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
        except TimeoutError:
            return False
        else:
            return True

    async def close(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Dispatches already in flight still call the monitor. Cancel and
        # await them, so no handler touches a recording after shutdown starts.
        pending = list(self._dispatch_tasks)
        for dispatch in pending:
            dispatch.cancel()
        if pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(*pending, return_exceptions=True)
        self._dispatch_tasks.clear()
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

    def _subscribed_fully(self, channel: str) -> bool:
        """True when the channel has an id for both event types.

        A channel with one event type must be retried: key presence alone
        would hide the missing kind, and the channel would lose its online
        or offline detection for good.
        """
        kinds = self._subs.get(channel)
        return kinds is not None and all(kind in kinds for kind in _EVENT_KINDS)

    async def add_channel(self, channel: str) -> None:
        await self._add_channels([channel])

    async def _add_channels(self, channels: list[str]) -> None:
        """Subscribe every channel of the list that misses an event type.

        Every network call stays outside ``_subs_lock``. The lock covers the
        merge of one channel only, so add_channel, remove_channel and
        sync_channels never wait for the subscription round trips.
        """
        if self._conduit_id is None or self._session_id is None:
            logger.debug("[eventsub] no live session, not subscribing %s", ", ".join(channels))
            return
        for channel in channels:
            async with self._subs_lock:
                if self._subscribed_fully(channel):
                    continue
            try:
                uid = (await self._api.resolve_user_ids([bare_name(channel)])).get(bare_name(channel))
            except Exception as e:
                logger.error("[eventsub] resolve_user_ids failed for %s: %s", channel, e)
                continue
            if uid is None:
                logger.warning("[eventsub] could not resolve user id for %s, skipping", channel)
                continue
            created = await self._create_channel_subs(channel, uid)
            async with self._subs_lock:
                self._user_ids[channel] = uid
                self._id_to_channel[uid] = channel
                # Record the channel even when no event type landed. An empty
                # entry reads as not subscribed, so a later pass retries it.
                # A skipped entry would hide the channel from every retry.
                self._subs.setdefault(channel, {}).update(created)

    async def remove_channel(self, channel: str) -> None:
        await self._remove_channels([channel])

    async def _remove_channels(self, channels: list[str]) -> None:
        """Delete the tracked subscriptions of the given channels.

        An id stays tracked until its delete succeeds, so a failed call is
        retried by the next sync instead of leaking a live subscription. A
        channel with no tracked ids still gets its empty entry and its user
        id dropped: without that, a channel whose subscribe failed and that
        is then removed from the config stays in _subs and syncs forever.
        """
        if self._conduit_id is None or self._session_id is None:
            logger.debug("[eventsub] no live session, not unsubscribing %s", ", ".join(channels))
            return
        async with self._subs_lock:
            work = [(ch, kind, sid) for ch in channels for kind, sid in self._subs.get(ch, {}).items()]
            empty = [ch for ch in channels if ch in self._subs and not self._subs[ch]]
        for channel, kind, sub_id in work:
            try:
                await self._api.delete_eventsub_subscription(sub_id)
            except Exception as e:
                logger.error("[eventsub] failed to delete subscription for %s: %s", channel, e)
                continue
            async with self._subs_lock:
                self._forget_sub(channel, kind, sub_id)
        for channel in empty:
            async with self._subs_lock:
                if channel in self._subs and not self._subs[channel]:
                    self._forget_empty(channel)

    def _forget_empty(self, channel: str) -> None:
        """Drop an entry that holds no subscription ids. The caller holds the lock."""
        kinds = self._subs.get(channel)
        if kinds is None:
            return
        if kinds:
            # A reconnect added a subscription while the delete ran: the
            # normal delete path owns it now.
            return
        del self._subs[channel]
        uid = self._user_ids.pop(channel, None)
        if uid is not None:
            self._id_to_channel.pop(uid, None)

    def _forget_sub(self, channel: str, kind: str, sub_id: str) -> None:
        """Drop one deleted subscription from the maps. The caller holds the lock."""
        kinds = self._subs.get(channel)
        if kinds is None or kinds.get(kind) != sub_id:
            # A reconnect replaced the entry while the delete ran.
            return
        del kinds[kind]
        if kinds:
            return
        del self._subs[channel]
        uid = self._user_ids.pop(channel, None)
        if uid is not None:
            self._id_to_channel.pop(uid, None)

    async def sync_channels(self, channels: list[str]) -> None:
        """Match the live subscriptions to the given channel list."""
        channels = [c for c in channels if not is_kick_channel(c)]
        # Take the work list under the lock, then run the round trips without
        # it, so a concurrent add_channel or remove_channel is not blocked.
        async with self._subs_lock:
            stale = [ch for ch in self._subs if ch not in channels]
            missing = [ch for ch in channels if not self._subscribed_fully(ch)]
        await self._remove_channels(stale)
        await self._add_channels(missing)

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
            self._ws = await connect(url)
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
            else:
                # A kind that failed in an earlier session is retried here.
                # Twitch redelivers nothing for a subscription that never
                # existed, so this pass is the only second chance.
                await self._retry_partial_subs()
            while not self._stop.is_set():
                msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=keepalive + 30))
                if await self._handle_message(msg):
                    return
        except TimeoutError:
            logger.error("[eventsub] keepalive timeout, reconnecting")
            return
        except ConnectionClosed as e:
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
        except Exception as e:
            logger.error("[eventsub] conduit setup failed: %s", e)
            self._status_error = str(e)[:80] or type(e).__name__
            self._ready.set()
            return False
        else:
            return True

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
        # Every network call stays outside _subs_lock. The lock covers the
        # three maps only, so add_channel, remove_channel, and sync_channels
        # never wait for the subscription round trips of every channel.
        created: dict[str, dict[str, str]] = {}
        for channel in channels:
            uid = user_ids.get(channel)
            if uid is None:
                logger.warning("[eventsub] could not resolve user id for %s, skipping", channel)
                continue
            created[channel] = await self._create_channel_subs(channel, uid)
        async with self._subs_lock:
            # Update, do not replace: a channel that add_channel subscribed
            # while this pass ran keeps its mapping.
            self._user_ids.update(user_ids)
            self._id_to_channel.update({uid: ch for ch, uid in user_ids.items()})
            for channel, kinds in created.items():
                self._subs.setdefault(channel, {}).update(kinds)
        poll_only = [ch for ch in channels if not self._subscribed_fully(ch)]
        if poll_only:
            logger.info("[eventsub] polling only for: %s", ", ".join(poll_only))

    async def _retry_partial_subs(self) -> None:
        """Create the event types that a channel is still missing."""
        async with self._subs_lock:
            partial = [(ch, self._user_ids.get(ch)) for ch in self._subs if not self._subscribed_fully(ch)]
        for channel, uid in partial:
            if uid is None:
                logger.warning("[eventsub] no user id for %s, channel relies on polling", channel)
                continue
            created = await self._create_channel_subs(channel, uid)
            if created:
                async with self._subs_lock:
                    self._subs.setdefault(channel, {}).update(created)

    async def _create_channel_subs(self, channel: str, uid: str) -> dict[str, str]:
        """Create both subscriptions of one channel. Return the ids that landed.

        The method keeps no bookkeeping: the caller merges the result under
        ``_subs_lock``. It never raises for one failed call, so a failure
        cannot hide the result of the other call.
        """

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
            return_exceptions=True,
        )
        created: dict[str, str] = {}
        for kind, result in zip(_EVENT_KINDS, results, strict=True):
            if isinstance(result, BaseException):
                # A status other than 202/400/403/409 raises. The sibling
                # call can still have created its subscription, so its id
                # must land in _subs.
                logger.error("[eventsub] creating stream.%s for %s failed: %s", kind, channel, result)
                continue
            status, body = result
            sub_id = await self._handle_subscribe_response(channel, kind, status, body, uid)
            if sub_id is not None:
                created[kind] = sub_id
        return created

    async def _handle_subscribe_response(
        self, channel: str, kind: str, status: int, body: dict[str, Any], uid: str
    ) -> str | None:
        """Return the subscription id for one response, or None.

        The method does no bookkeeping and never raises, so one bad
        response cannot discard the result of the other one.
        """
        sub_type = f"stream.{kind}"
        if status == 202:
            try:
                return str(body["data"][0]["id"])
            except KeyError, IndexError, TypeError:
                logger.error("[eventsub] malformed 202 body creating %s for %s", sub_type, channel)
                return None
        if status == 409:
            logger.warning("[eventsub] subscription %s already exists for %s, resolving id", sub_type, channel)
            try:
                subs = await self._api.list_eventsub_subscriptions()
                existing = next(
                    s for s in subs if s["type"] == sub_type and s["condition"].get("broadcaster_user_id") == uid
                )
                return str(existing["id"])
            except Exception as e:
                # A malformed entry or a failed call must not escape: this
                # method never raises, and the caller must keep subscribing
                # the remaining channels of the session.
                logger.error("[eventsub] could not resolve existing subscription id for %s: %s", channel, e)
                return None
        if status in (400, 403):
            logger.error("[eventsub] subscription rejected for %s (%s); channel relies on polling", channel, status)
        else:
            logger.error("[eventsub] unexpected status %s creating %s for %s", status, sub_type, channel)
        return None

    async def _handle_message(self, msg: dict[str, Any]) -> bool:
        """Dispatch one WebSocket message. Returns True when the socket must reconnect."""
        if not isinstance(msg, dict):
            # json.loads can return any JSON type, and the single caller
            # passes its result straight in. Reading .get() on a list, a
            # string or None would raise out of the read loop.
            logger.warning("[eventsub] message is not a JSON object, ignoring")
            return False
        metadata = msg.get("metadata")
        if not isinstance(metadata, dict):
            # A frame without an object metadata is not one this client can
            # act on. Raising here would escape the read loop and cost a
            # reconnect, so drop the frame instead.
            logger.warning("[eventsub] message without an object metadata, ignoring")
            return False
        mtype = metadata.get("message_type")
        if mtype == "notification":
            msg_id = metadata.get("message_id")
            # The dedup store is a dict keyed by the id, so a non-string JSON
            # value would raise TypeError out of the read loop.
            if isinstance(msg_id, str) and not self._remember_id(msg_id):
                logger.debug("[eventsub] duplicate event, ignoring")
                return False
            t = asyncio.create_task(self._bounded_dispatch(msg))
            self._dispatch_tasks.add(t)
            t.add_done_callback(self._dispatch_tasks.discard)
        elif mtype == "session_keepalive":
            pass
        elif mtype == "session_reconnect":
            url = self._reconnect_url_of(msg)
            if url is None:
                logger.warning("[eventsub] session_reconnect without a usable websocket URL, ignoring")
                return False
            self._reconnect_url = url
            return True
        elif mtype == "revocation":
            await self._handle_revocation(msg)
        return False

    @staticmethod
    def _reconnect_url_of(msg: dict[str, Any]) -> str | None:
        """Reconnect URL of a session_reconnect frame, or None.

        The URL is dialed verbatim, so it must be a websocket URL. A frame
        that asked for a plaintext ``ws://`` endpoint would downgrade a
        transport this client can otherwise keep encrypted.
        """
        payload = msg.get("payload")
        session = payload.get("session") if isinstance(payload, dict) else None
        url = session.get("reconnect_url") if isinstance(session, dict) else None
        if not isinstance(url, str) or not url.startswith("wss://"):
            return None
        return url

    def _remember_id(self, message_id: str | None) -> bool:
        """True when the message id is new within the dedup window.

        False for a replay. A message without an id has no key to dedup
        on, so it still dispatches. This differs from the Kick webhook,
        where a missing id means an unsigned body that verify rejects.
        """
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
        seen[message_id] = now + _DEDUP_WINDOW_S
        return True

    def _forget_id(self, msg: dict[str, Any]) -> None:
        """Drop the dedup marker after a failed dispatch.

        The marker is set before the dispatch runs. A failed dispatch must
        not suppress a redelivery for the rest of the dedup window.
        """
        message_id = msg.get("metadata", {}).get("message_id")
        if message_id:
            self._seen_ids.pop(message_id, None)

    async def _bounded_dispatch(self, msg: dict[str, Any]) -> None:
        """Run one dispatch under the concurrency bound and a timeout.

        A slow handler drops only its own event. The socket stays up.
        """
        try:
            # Wait for a free slot outside the timeout, so the wait does not
            # consume the budget of the handler itself.
            async with self._dispatch_sem:
                await asyncio.wait_for(self._dispatch(msg), timeout=_DISPATCH_TIMEOUT_S)
        except TimeoutError:
            # The timeout cancels the handler at an arbitrary await point, so
            # it can have applied part of its effect. Keep the dedup marker:
            # a redelivery would re-enter that handler while the abandoned
            # work is still registered. The poll path reconciles the event.
            logger.warning("[eventsub] dispatch timed out, keeping connection")
        except Exception:
            logger.error("[eventsub] bounded dispatch failed", exc_info=True)
            self._forget_id(msg)

    async def _handle_revocation(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload")
        subscription = payload.get("subscription") if isinstance(payload, dict) else None
        sub = subscription if isinstance(subscription, dict) else {}
        sub_id = sub.get("id")
        logger.warning("[eventsub] subscription revoked: %s (%s)", sub.get("type"), sub_id)
        target: tuple[str, str | None] | None = None
        async with self._subs_lock:
            for channel, kinds in list(self._subs.items()):
                for kind, sid in list(kinds.items()):
                    if sid == sub_id:
                        del self._subs[channel][kind]
                        if not self._subs[channel]:
                            # An empty entry means not subscribed, so the
                            # channel is retried on the next sync. It also
                            # hides no subscription id from the delete path.
                            del self._subs[channel]
                        target = (channel, self._user_ids.get(channel))
                        break
                if target is not None:
                    break
        if target is None:
            return
        channel, uid = target
        if uid is None:
            logger.warning("[eventsub] no user id for %s, channel relies on polling", channel)
            return
        # Recreate at once through the normal subscribe path. The surviving
        # kind answers 409 and re-resolves its id. A 400/403 keeps the
        # existing polling fallback log. This call runs outside _subs_lock,
        # so the read loop keeps processing keepalives meanwhile.
        try:
            created = await self._create_channel_subs(channel, uid)
        except Exception as e:
            logger.error("[eventsub] resubscribe failed for %s: %s", channel, e, exc_info=True)
            return
        if created:
            async with self._subs_lock:
                self._subs.setdefault(channel, {}).update(created)

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
                if self._monitor.is_live(channel):
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
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError) as e:
            logger.error("[eventsub] dispatch failed: %s", e, exc_info=True)
            self._forget_id(msg)
