import asyncio
import json
import logging

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


def make_client(api=None, monitor=None, config=None):
    return EventSubClient(
        api or FakeTwitchAPI(),
        monitor or StubMonitor(),
        config or make_config(),
    )


async def handle_message(client, msg):
    """Dispatch a message and wait for any task it spawned."""
    await client._handle_message(msg)
    await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not asyncio.current_task()))


def notification(user_id, sub_type):
    return {
        "metadata": {"message_type": "notification", "subscription_type": sub_type},
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


class _FakeClosed(Exception):
    def __init__(self, code):
        self.code = code


def _make_closing_ws(code):
    """Fake websocket: delivers a welcome, then closes with ``code``."""

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
            raise _FakeClosed(code)

        async def close(self):
            pass

    class FakeWebsockets:
        ConnectionClosed = _FakeClosed

        async def connect(self, url):
            return FakeWS()

    return FakeWebsockets()


def _run_close_code(client, code):
    return asyncio.run(client._connect_and_listen())


def test_close_code_4007_logged_as_info(caplog, monkeypatch):
    # 4007 is Twitch's normal server-initiated reconnect, not an error.
    client = make_client()
    client._conduit_id = "c1"
    client._subscribed = True
    monkeypatch.setattr("stream_archive.eventsub.websockets", _make_closing_ws(4007))

    with caplog.at_level("INFO", logger="stream_archive.eventsub"):
        _run_close_code(client, 4007)

    assert any("reconnect requested by Twitch" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_close_code_1006_logged_as_warning(caplog, monkeypatch):
    client = make_client()
    client._conduit_id = "c1"
    client._subscribed = True
    monkeypatch.setattr("stream_archive.eventsub.websockets", _make_closing_ws(1006))

    with caplog.at_level("WARNING", logger="stream_archive.eventsub"):
        _run_close_code(client, 1006)

    assert any("abnormally" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_close_code_other_logged_as_error(caplog, monkeypatch):
    client = make_client()
    client._conduit_id = "c1"
    client._subscribed = True
    monkeypatch.setattr("stream_archive.eventsub.websockets", _make_closing_ws(1011))

    with caplog.at_level("ERROR", logger="stream_archive.eventsub"):
        _run_close_code(client, 1011)

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

    asyncio.run(client.add_channel("ch"))

    assert len(api.subscription_creates) == 2
    assert client._subs["ch"] == {"online": "sub-1", "offline": "sub-2"}
    assert client._user_ids == {"ch": "uch"}
    assert client._id_to_channel == {"uch": "ch"}


def test_remove_channel_deletes_subs():
    api = FakeTwitchAPI()
    client = make_client(api=api, config=make_config(channels=["ch"]))
    client._conduit_id = "c1"
    client._session_id = "s1"
    client._subs = {"ch": {"online": "s1", "offline": "s2"}}
    client._user_ids = {"ch": "u1"}
    client._id_to_channel = {"u1": "twitch:ch"}

    asyncio.run(client.remove_channel("ch"))

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
    client._subs = {"ch1": {"online": "s1"}, "stale": {"online": "s2"}}
    client._user_ids = {"ch1": "u1", "stale": "u9"}
    client._id_to_channel = {"u1": "ch1", "u9": "stale"}

    asyncio.run(client.sync_channels(["ch1", "ch2"]))

    assert api.subscription_deletes == ["s2"]
    assert "ch2" in client._subs
    assert "stale" not in client._subs
