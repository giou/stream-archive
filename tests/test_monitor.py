import asyncio

from src.twitch_recorder import monitor as monitor_module
from src.twitch_recorder.monitor import Monitor


class FakeTwitchAPI:
    def __init__(self, streams=None, error=None, user_ids=None):
        self.streams = streams
        self.error = error
        self.user_ids = user_ids

    async def resolve_user_ids(self, channels):
        return self.user_ids or {c: c for c in channels}

    async def get_live_streams(self, user_ids):
        if self.error:
            raise self.error
        return self.streams or {}


class FakeRecorder:
    def __init__(self, ok=True):
        self.ok = ok
        self.started = []
        self.stopped = []

    async def start(self, channel, title=None, game=None):
        self.started.append(channel)
        return self.ok

    async def stop(self, channel):
        self.stopped.append(channel)
        return {}


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def notify(self, m):
        self.messages.append(m)

    async def notify_live(self, *a, **k):
        pass

    async def notify_offline(self, *a, **k):
        pass


def make_monitor(recorder=None, notifier=None):
    return Monitor(recorder or FakeRecorder(), notifier or FakeNotifier())


def test_live_channel_started_once():
    rec = FakeRecorder()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, config))
    asyncio.run(mon.check_channels(api, config))

    assert rec.started == ["ch"]
    assert rec.stopped == []


def test_offline_transition_stops():
    rec = FakeRecorder()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, config))
    api.streams = {}
    asyncio.run(mon.check_channels(api, config))

    assert rec.started == ["ch"]
    assert rec.stopped == ["ch"]


def test_failed_start_is_retried_and_alert_rate_limited():
    rec = FakeRecorder(ok=False)
    notifier = FakeNotifier()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec, notifier=notifier)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, config))
    asyncio.run(mon.check_channels(api, config))

    assert rec.started == ["ch", "ch"]
    assert len(notifier.messages) == 1
    assert "Failed to start recording for ch" in notifier.messages[0]


def test_failure_alert_not_rate_limited_when_interval_zero(monkeypatch):
    monkeypatch.setattr(monitor_module, "FAILURE_NOTIFY_INTERVAL", 0)
    rec = FakeRecorder(ok=False)
    notifier = FakeNotifier()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec, notifier=notifier)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, config))
    asyncio.run(mon.check_channels(api, config))

    assert rec.started == ["ch", "ch"]
    assert len(notifier.messages) == 2


def test_transient_api_error_does_not_raise_or_act():
    rec = FakeRecorder()
    api = FakeTwitchAPI(error=RuntimeError("boom"), user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, config))

    assert rec.started == []
    assert rec.stopped == []
