import asyncio
import json
import logging

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from stream_archive.config import AppConfig
from stream_archive.eventsub import EventSubClient


def make_config(**overrides):
    data = {
        "telegram_user_id": 12345,
        "bot_telegram_api": "bot_token",
        "twitch_client_id": "client_id",
        "twitch_client_secret": "client_secret",
        "channels": ["ch"],
        "proxy_list": ["httpproxy://user:pass@host:port"],
        "monitoring_interval": 60,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "recording_dir": "recordings",
    }
    data.update(overrides)
    return AppConfig.model_validate(data)


class FakeTwitchAPI:
    def __init__(self, user_ids=None, streams=None):
        self.user_ids = user_ids or {}
        self.streams = streams or {}
        self.conduits = []
        self.created = []
        self.deleted = []
        self.shard_updates = []
        self.subscription_creates = []
        self.subscription_deletes = []
        self.subscription_list = []
        self.create_status = 202

    async def resolve_user_ids(self, channels):
        return {c: self.user_ids.get(c, "u" + c) for c in channels}

    async def list_conduits(self):
        return self.conduits

    async def create_conduit(self, shard_count=1):
        conduit = {"id": f"conduit-{len(self.created)}", "shard_count": shard_count}
        self.created.append(conduit)
        return conduit

    async def delete_conduit(self, conduit_id):
        self.deleted.append(conduit_id)

    async def update_conduit_shards(self, conduit_id, session_id):
        self.shard_updates.append((conduit_id, session_id))
        return {"status": "enabled", "id": "0"}

    async def create_eventsub_subscription(self, payload):
        self.subscription_creates.append(payload)
        if self.create_status == 202:
            return 202, {"data": [{"id": f"sub-{len(self.subscription_creates)}"}]}
        return self.create_status, {}

    async def delete_eventsub_subscription(self, sub_id):
        self.subscription_deletes.append(sub_id)

    async def list_eventsub_subscriptions(self):
        return self.subscription_list

    async def get_stream(self, user_id):
        return self.streams.get(user_id)


class StubMonitor:
    def __init__(self):
        self.online_calls = []
        self.offline_calls = []
        self._live_channels = set()

    async def handle_online(self, channel, title, game, user_id, config):
        self.online_calls.append((channel, title, game, user_id))

    async def handle_offline(self, channel, config):
        self.offline_calls.append(channel)

    def is_live(self, channel):
        return channel in self._live_channels


def make_client(api=None, monitor=None, config=None):
    return EventSubClient(
        api or FakeTwitchAPI(),
        monitor or StubMonitor(),
        config or make_config(),
    )


async def handle_message(client, msg):
    """Dispatch a message and wait for the tasks it spawned."""
    await client._handle_message(msg)
    await asyncio.gather(*client._dispatch_tasks)


def notification(user_id, sub_type, message_id=None):
    metadata = {"message_type": "notification", "subscription_type": sub_type}
    if message_id is not None:
        metadata["message_id"] = message_id
    return {
        "metadata": metadata,
        "payload": {"event": {"broadcaster_user_id": user_id}},
    }


def test_online_notification_dispatches_to_monitor():
    api = FakeTwitchAPI(user_ids={"ch": "u1"}, streams={"u1": {"title": "T", "game_name": "G"}})
    mon = StubMonitor()
    client = make_client(api=api, monitor=mon)
    client._id_to_channel = {"u1": "twitch:ch"}

    asyncio.run(handle_message(client, notification("u1", "stream.online")))

    assert mon.online_calls == [("twitch:ch", "T", "G", "u1")]


def test_online_when_stream_already_ended():
    api = FakeTwitchAPI(user_ids={"ch": "u1"})
    mon = StubMonitor()
    client = make_client(api=api, monitor=mon)
    client._id_to_channel = {"u1": "twitch:ch"}

    asyncio.run(handle_message(client, notification("u1", "stream.online")))

    assert mon.online_calls == []


def test_offline_notification_dispatches():
    mon = StubMonitor()
    mon._live_channels.add("twitch:ch")
    client = make_client(monitor=mon)
    client._id_to_channel = {"u1": "twitch:ch"}

    asyncio.run(handle_message(client, notification("u1", "stream.offline")))

    assert mon.offline_calls == ["twitch:ch"]


def test_online_event_ignored_when_already_live():
    mon = StubMonitor()
    mon._live_channels.add("twitch:ch")
    client = make_client(monitor=mon)
    client._id_to_channel = {"u1": "twitch:ch"}

    asyncio.run(handle_message(client, notification("u1", "stream.online")))

    assert mon.online_calls == []


def test_ensure_conduit_deletes_then_creates():
    api = FakeTwitchAPI()
    api.conduits = [{"id": "old1", "shard_count": 1}, {"id": "old2", "shard_count": 1}]
    client = make_client(api=api)

    assert asyncio.run(client._ensure_conduit()) is True

    assert api.deleted == ["old1", "old2"]
    assert len(api.created) == 1
    assert client._conduit_id == api.created[0]["id"]
    assert client._status_error is None


def test_ensure_conduit_failure_sets_ready_and_status_error():
    class BoomAPI(FakeTwitchAPI):
        async def list_conduits(self):
            msg = "invalid client id"
            raise RuntimeError(msg)

    client = make_client(api=BoomAPI())

    assert asyncio.run(client._ensure_conduit()) is False

    assert client._status_error is not None
    assert client._ready.is_set()
    assert "unavailable" in client.status()


def test_activate_shard_uses_current_session():
    api = FakeTwitchAPI()
    client = make_client(api=api)
    client._conduit_id = "c1"
    client._session_id = "sess-abc"

    asyncio.run(client._activate_shard())

    assert api.shard_updates == [("c1", "sess-abc")]


def test_subscribe_creates_online_and_offline_per_channel():
    channels = [f"twitch:ch{i}" for i in range(1, 8)]
    user_ids = {f"ch{i}": f"u{i}" for i in range(1, 8)}
    api = FakeTwitchAPI(user_ids=user_ids)
    client = make_client(api=api, config=make_config(channels=channels))
    client._conduit_id = "c1"

    asyncio.run(client._subscribe_all())

    assert len(api.subscription_creates) == 14
    assert [p["type"] for p in api.subscription_creates].count("stream.online") == 7
    assert [p["type"] for p in api.subscription_creates].count("stream.offline") == 7
    for p in api.subscription_creates:
        assert p["version"] == "1"
        assert p["transport"] == {"method": "conduit", "conduit_id": "c1"}
    assert set(client._subs) == set(channels)
    for kind in ("online", "offline"):
        assert all(kind in subs for subs in client._subs.values())


def test_409_resolves_existing_subscription_id():
    api = FakeTwitchAPI(user_ids={"ch": "u1"})
    api.create_status = 409
    api.subscription_list = [
        {"id": "existing-online", "type": "stream.online", "condition": {"broadcaster_user_id": "u1"}},
        {"id": "existing-offline", "type": "stream.offline", "condition": {"broadcaster_user_id": "u1"}},
    ]
    client = make_client(api=api)
    client._conduit_id = "c1"

    asyncio.run(client._subscribe_all())

    assert client._subs["twitch:ch"] == {"online": "existing-online", "offline": "existing-offline"}


def _make_closing_ws(code):
    """Fake connect(): delivers a welcome, then closes with ``code``.

    The socket raises a real ``ConnectionClosed``, because the client catches
    that type. The returned callable replaces ``eventsub.connect``.
    """

    class FakeWS:
        def __init__(self):
            self.n = 0

        async def recv(self):
            self.n += 1
            if self.n == 1:
                return json.dumps(
                    {
                        "metadata": {"message_type": "session_welcome"},
                        "payload": {"session": {"id": "s1", "keepalive_timeout_seconds": 60}},
                    }
                )
            raise ConnectionClosed(Close(code, ""), None)

        async def close(self):
            pass

    async def fake_connect(url):
        return FakeWS()

    return fake_connect


def _run_close_code(client):
    """Run one connect/listen pass under a deadline.

    The fake socket raises the close code again on every read. Without the
    deadline a regression that retries inside the method spins forever.
    """
    return asyncio.run(asyncio.wait_for(client._connect_and_listen(), timeout=5))


def test_close_code_4007_logged_as_info(caplog, monkeypatch):
    # 4007 is Twitch's normal server-initiated reconnect, not an error.
    client = make_client()
    client._conduit_id = "c1"
    client._subscribed = True
    monkeypatch.setattr("stream_archive.eventsub.connect", _make_closing_ws(4007))

    with caplog.at_level("INFO", logger="stream_archive.eventsub"):
        _run_close_code(client)

    assert any("reconnect requested by Twitch" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_close_code_1006_logged_as_warning(caplog, monkeypatch):
    client = make_client()
    client._conduit_id = "c1"
    client._subscribed = True
    monkeypatch.setattr("stream_archive.eventsub.connect", _make_closing_ws(1006))

    with caplog.at_level("WARNING", logger="stream_archive.eventsub"):
        _run_close_code(client)

    assert any("abnormally" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_close_code_other_logged_as_error(caplog, monkeypatch):
    client = make_client()
    client._conduit_id = "c1"
    client._subscribed = True
    monkeypatch.setattr("stream_archive.eventsub.connect", _make_closing_ws(1011))

    with caplog.at_level("ERROR", logger="stream_archive.eventsub"):
        _run_close_code(client)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "code=1011" in errors[0].getMessage()


def test_session_reconnect_message_sets_reconnect_url():
    client = make_client()
    msg = {
        "metadata": {"message_type": "session_reconnect"},
        "payload": {"session": {"reconnect_url": "wss://reconnect.example/ws"}},
    }

    assert asyncio.run(client._handle_message(msg)) is True
    assert client._reconnect_url == "wss://reconnect.example/ws"


def test_add_channel_creates_subs_and_maps():
    api = FakeTwitchAPI()
    client = make_client(api=api, config=make_config(channels=["ch"]))
    client._conduit_id = "c1"
    client._session_id = "s1"

    asyncio.run(client.add_channel("twitch:ch"))

    assert len(api.subscription_creates) == 2
    assert client._subs["twitch:ch"] == {"online": "sub-1", "offline": "sub-2"}
    assert client._user_ids == {"twitch:ch": "uch"}
    assert client._id_to_channel == {"uch": "twitch:ch"}


def test_remove_channel_deletes_subs():
    api = FakeTwitchAPI()
    client = make_client(api=api, config=make_config(channels=["ch"]))
    client._conduit_id = "c1"
    client._session_id = "s1"
    # AppConfig normalizes channels, so production keys the maps by the
    # prefixed name. The seed mirrors that shape.
    client._subs = {"twitch:ch": {"online": "s1", "offline": "s2"}}
    client._user_ids = {"twitch:ch": "u1"}
    client._id_to_channel = {"u1": "twitch:ch"}

    asyncio.run(client.remove_channel("twitch:ch"))

    assert sorted(api.subscription_deletes) == ["s1", "s2"]
    assert client._subs == {}
    assert client._user_ids == {}
    assert client._id_to_channel == {}


def test_subscribe_twitch_prefixed_channel_resolves_bare():
    api = FakeTwitchAPI(user_ids={"streamer1": "u1"})
    client = make_client(api=api, config=make_config(channels=["twitch:streamer1"]))
    client._conduit_id = "c1"

    asyncio.run(client._subscribe_all())

    assert client._user_ids == {"twitch:streamer1": "u1"}
    assert set(client._subs) == {"twitch:streamer1"}
    assert all(p["condition"]["broadcaster_user_id"] == "u1" for p in api.subscription_creates)


def test_sync_channels_removes_stale_and_adds_new():
    api = FakeTwitchAPI()
    client = make_client(api=api, config=make_config(channels=["ch1", "ch2"]))
    client._conduit_id = "c1"
    client._session_id = "s1"
    client._subs = {"twitch:ch1": {"online": "s1"}, "twitch:stale": {"online": "s2"}}
    client._user_ids = {"twitch:ch1": "u1", "twitch:stale": "u9"}
    client._id_to_channel = {"u1": "twitch:ch1", "u9": "twitch:stale"}

    asyncio.run(client.sync_channels(["twitch:ch1", "twitch:ch2"]))

    assert api.subscription_deletes == ["s2"]
    assert "twitch:ch2" in client._subs
    assert "twitch:stale" not in client._subs


class FlakyStreamAPI(FakeTwitchAPI):
    """Fails the stream lookup until ``healthy`` is set."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.healthy = False

    async def get_stream(self, user_id):
        if not self.healthy:
            msg = "stream lookup failed"
            raise httpx.HTTPError(msg)
        return await super().get_stream(user_id)


class BlockingStreamAPI(FakeTwitchAPI):
    """Holds the stream lookup until ``release`` is set."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lookup_started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_stream(self, user_id):
        self.lookup_started.set()
        await self.release.wait()
        return await super().get_stream(user_id)


def test_duplicate_message_id_dispatches_once():
    """The same message id within the dedup window reaches the monitor once."""
    api = FakeTwitchAPI(user_ids={"ch": "u1"}, streams={"u1": {"title": "T", "game_name": "G"}})
    mon = StubMonitor()
    client = make_client(api=api, monitor=mon)
    client._id_to_channel = {"u1": "twitch:ch"}
    msg = notification("u1", "stream.online", message_id="m1")

    asyncio.run(handle_message(client, msg))
    asyncio.run(handle_message(client, msg))

    assert mon.online_calls == [("twitch:ch", "T", "G", "u1")]


def test_expired_message_id_dispatches_again(monkeypatch):
    """A marker that aged past the dedup window must not suppress a redelivery."""
    api = FakeTwitchAPI(user_ids={"ch": "u1"}, streams={"u1": {"title": "T", "game_name": "G"}})
    mon = StubMonitor()
    client = make_client(api=api, monitor=mon)
    client._id_to_channel = {"u1": "twitch:ch"}
    msg = notification("u1", "stream.online", message_id="m1")
    # A zero window makes every marker expire at once, so the second delivery
    # is a redelivery after the window instead of a replay inside it.
    monkeypatch.setattr("stream_archive.eventsub._DEDUP_WINDOW_S", 0)

    asyncio.run(handle_message(client, msg))
    asyncio.run(handle_message(client, msg))

    assert mon.online_calls == [
        ("twitch:ch", "T", "G", "u1"),
        ("twitch:ch", "T", "G", "u1"),
    ]


def test_failed_dispatch_forgets_the_message_id():
    """A dispatch that fails must let a redelivery of the message dispatch again."""
    api = FlakyStreamAPI(user_ids={"ch": "u1"}, streams={"u1": {"title": "T", "game_name": "G"}})
    mon = StubMonitor()
    client = make_client(api=api, monitor=mon)
    client._id_to_channel = {"u1": "twitch:ch"}
    msg = notification("u1", "stream.online", message_id="m1")

    asyncio.run(handle_message(client, msg))

    assert "m1" not in client._seen_ids
    assert mon.online_calls == []

    api.healthy = True
    asyncio.run(handle_message(client, msg))

    assert mon.online_calls == [("twitch:ch", "T", "G", "u1")]


def test_close_cancels_an_in_flight_dispatch():
    """close() cancels and awaits a dispatch that still runs."""
    api = BlockingStreamAPI(user_ids={"ch": "u1"})
    mon = StubMonitor()
    client = make_client(api=api, monitor=mon)
    client._id_to_channel = {"u1": "twitch:ch"}

    async def scenario():
        await client._handle_message(notification("u1", "stream.online", message_id="m1"))
        async with asyncio.timeout(5):
            await api.lookup_started.wait()
            dispatch = next(iter(client._dispatch_tasks))

            await client.close()

            assert client._dispatch_tasks == set()
            assert dispatch.cancelled()

    asyncio.run(scenario())
    assert mon.online_calls == []
