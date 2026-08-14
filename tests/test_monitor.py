import asyncio
import time

from src.stream_archive import monitor as monitor_module
from src.stream_archive.monitor import Monitor


class FakeTwitchAPI:
    def __init__(self, streams=None, error=None, user_ids=None):
        self.streams = streams
        self.error = error
        self.user_ids = user_ids
        self.resolve_calls = []

    async def resolve_user_ids(self, channels):
        self.resolve_calls.append(list(channels))
        return self.user_ids or {c: c for c in channels}

    async def get_live_streams(self, user_ids):
        if self.error:
            raise self.error
        return self.streams or {}


class FakeKickAPI:
    def __init__(self, statuses=None, error=None):
        self.statuses = statuses
        self.error = error

    async def get_channel_statuses(self, slugs):
        if self.error:
            raise self.error
        return self.statuses or {}


class FakeRecorder:
    def __init__(self, ok=True):
        self.ok = ok
        self.started = []
        self.started_kwargs = []
        self.stopped = []
        self._recording = True
        self.snapshot = {
            "free_gb": 100.0,
            "total_fs_gb": 500.0,
            "used_fs_gb": 400.0,
            "dir_gb": 0.0,
            "file_count": 0,
            "dir": "recordings",
        }
        self.delete_oldest_calls = []
        self.mode = "disk"

    async def start(self, channel, title=None, game=None, user_id=None):
        self.started.append(channel)
        self.started_kwargs.append(
            {"channel": channel, "title": title, "game": game, "user_id": user_id}
        )
        return self.ok

    async def stop(self, channel):
        self.stopped.append(channel)
        return {}

    def is_recording(self, channel):
        return self._recording

    def active_channels(self):
        return list(self.started)

    async def disk_snapshot(self):
        return self.snapshot

    async def delete_oldest_to_cap(self):
        self.delete_oldest_calls.append(1)
        self.snapshot["dir_gb"] = 0.0
        return (0, 0.0)

    def youtube_active_count(self):
        return len(self.started) if self.mode in ("youtube", "both") else 0

    def youtube_restart_blocked_reason(self, channel):
        return None


class FakeNotifier:
    def __init__(self):
        self.messages = []
        self.offline = []

    async def notify(self, m):
        self.messages.append(m)

    async def notify_live(self, *a, **k):
        pass

    async def notify_offline(self, *a, **k):
        self.offline.append((a, k))


def make_monitor(recorder=None, notifier=None):
    return Monitor(recorder or FakeRecorder(), notifier or FakeNotifier())


def test_live_channel_started_once():
    rec = FakeRecorder()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))
    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert rec.started == ["ch"]
    assert rec.stopped == []


def test_offline_transition_stops():
    rec = FakeRecorder()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))
    api.streams = {}
    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert rec.started == ["ch"]
    assert rec.stopped == ["ch"]


def test_failed_start_is_retried_and_alert_rate_limited():
    rec = FakeRecorder(ok=False)
    notifier = FakeNotifier()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec, notifier=notifier)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))
    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

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

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))
    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert rec.started == ["ch", "ch"]
    assert len(notifier.messages) == 2


def test_recording_death_triggers_restart():
    rec = FakeRecorder()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))
    assert rec.started == ["ch"]

    rec._recording = False
    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert rec.started == ["ch", "ch"]
    assert rec.stopped == []


def test_unknown_user_stream_is_skipped():
    rec = FakeRecorder()
    api = FakeTwitchAPI(streams={"u999": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert rec.started == []


def test_transient_api_error_does_not_raise_or_act():
    rec = FakeRecorder()
    api = FakeTwitchAPI(error=RuntimeError("boom"), user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert rec.started == []
    assert rec.stopped == []


def test_delete_oldest_and_starts_when_over_cap():
    rec = FakeRecorder()
    rec.snapshot["dir_gb"] = 25.0
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"], "disk": {"max_total_gb": 20, "delete_oldest": True}}

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert rec.delete_oldest_calls == [1]
    assert rec.started == ["ch"]


def test_block_when_cap_reached_and_nothing_to_delete():
    rec = FakeRecorder()
    rec.snapshot["dir_gb"] = 25.0
    notifier = FakeNotifier()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec, notifier=notifier)
    config = {"channels": ["ch"], "disk": {"max_total_gb": 20, "delete_oldest": True}}

    async def keep_full():
        rec.delete_oldest_calls.append(1)
        return (0, 0.0)

    rec.delete_oldest_to_cap = keep_full

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert rec.started == []
    assert any("cap" in m for m in notifier.messages)


def test_concurrency_limit_records_first_n():
    rec = FakeRecorder()
    notifier = FakeNotifier()
    api = FakeTwitchAPI(
        streams={"u1": {"title": "T", "game_name": "G"}, "u2": {"title": "T", "game_name": "G"}},
        user_ids={"ch_a": "u1", "ch_b": "u2"},
    )
    mon = make_monitor(recorder=rec, notifier=notifier)
    config = {"channels": ["ch_a", "ch_b"], "max_concurrent_recordings": 1}

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert rec.started == ["ch_a"]
    assert any("concurrent recording limit reached" in m for m in notifier.messages)


def test_youtube_limit_blocks_restreams():
    rec = FakeRecorder()
    rec.mode = "youtube"
    notifier = FakeNotifier()
    api = FakeTwitchAPI(
        streams={"u1": {"title": "T", "game_name": "G"}, "u2": {"title": "T", "game_name": "G"}},
        user_ids={"ch_a": "u1", "ch_b": "u2"},
    )
    mon = make_monitor(recorder=rec, notifier=notifier)
    config = {"channels": ["ch_a", "ch_b"], "output_mode": "youtube", "max_concurrent_youtube_streams": 1}

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert rec.started == ["ch_a"]
    assert any("YouTube re-stream limit reached" in m for m in notifier.messages)


def test_handle_online_starts_recording():
    rec = FakeRecorder()
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.handle_online("ch", "T", "G", "u1", config))

    assert rec.started == ["ch"]
    assert rec.stopped == []


def test_handle_online_twice_is_noop():
    rec = FakeRecorder()
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.handle_online("ch", "T", "G", "u1", config))
    asyncio.run(mon.handle_online("ch", "T", "G", "u1", config))

    assert rec.started == ["ch"]


def test_handle_online_restarts_dead_recording():
    rec = FakeRecorder()
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    asyncio.run(mon.handle_online("ch", "T", "G", "u1", config))
    rec._recording = False
    asyncio.run(mon.handle_online("ch", "T", "G", "u1", config))

    assert rec.started == ["ch", "ch"]
    assert rec.stopped == []


def test_handle_online_ignores_unknown_channel():
    rec = FakeRecorder()
    mon = make_monitor(recorder=rec)
    config = {"channels": ["other"]}

    asyncio.run(mon.handle_online("ch", "T", "G", "u1", config))

    assert rec.started == []


def test_recorder_backoff_blocks_restart():
    class BackoffRecorder(FakeRecorder):
        def youtube_restart_blocked_reason(self, channel):
            return "restarting in 60s (short recording, YouTube quota guard)"

    rec = BackoffRecorder()
    notifier = FakeNotifier()
    mon = make_monitor(recorder=rec, notifier=notifier)
    config = {"channels": ["ch"]}

    async def scenario():
        await mon.handle_online("ch", "T", "G", "u1", config)
        assert rec.started == []
        assert any("restarting in" in m for m in notifier.messages)

    asyncio.run(scenario())


def test_handle_offline_stops_and_notifies():
    rec = FakeRecorder()
    notifier = FakeNotifier()
    mon = make_monitor(recorder=rec, notifier=notifier)
    config = {"channels": ["ch"]}

    asyncio.run(mon.handle_online("ch", "T", "G", "u1", config))
    asyncio.run(mon.handle_offline("ch", config))

    assert rec.stopped == ["ch"]
    assert len(notifier.offline) == 1


def test_handle_offline_ignores_when_not_live():
    rec = FakeRecorder()
    notifier = FakeNotifier()
    mon = make_monitor(recorder=rec, notifier=notifier)
    config = {"channels": ["ch"]}

    asyncio.run(mon.handle_offline("ch", config))

    assert rec.stopped == []
    assert notifier.offline == []


def test_poll_and_event_lock_same_channel():
    rec = FakeRecorder()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"ch": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch"]}

    async def concurrent():
        await asyncio.gather(
            mon.check_channels(api, FakeKickAPI(), config),
            mon.handle_online("ch", "T", "G", "u1", config),
        )

    asyncio.run(concurrent())

    assert rec.started == ["ch"]
    assert rec.stopped == []


KICK_STATUS_LIVE = {
    "xqc": {
        "title": "Kick title",
        "game": "Kick game",
        "is_live": True,
        "broadcaster_user_id": 111,
    }
}


def test_kick_live_starts_with_title_game_and_no_user_id():
    rec = FakeRecorder()
    mon = make_monitor(recorder=rec)
    config = {"channels": ["kick:xqc"]}

    asyncio.run(mon.check_channels(FakeTwitchAPI(), FakeKickAPI(statuses=KICK_STATUS_LIVE), config))

    assert rec.started == ["kick:xqc"]
    assert rec.started_kwargs == [{
        "channel": "kick:xqc", "title": "Kick title", "game": "Kick game", "user_id": None,
    }]


def test_kick_live_to_offline_stops():
    rec = FakeRecorder()
    mon = make_monitor(recorder=rec)
    config = {"channels": ["kick:xqc"]}
    kick = FakeKickAPI(statuses=KICK_STATUS_LIVE)

    asyncio.run(mon.check_channels(FakeTwitchAPI(), kick, config))
    kick.statuses = {"xqc": {**KICK_STATUS_LIVE["xqc"], "is_live": False}}
    asyncio.run(mon.check_channels(FakeTwitchAPI(), kick, config))

    assert rec.started == ["kick:xqc"]
    assert rec.stopped == ["kick:xqc"]


def test_kick_api_error_no_start_stop_or_raise():
    rec = FakeRecorder()
    mon = make_monitor(recorder=rec)
    config = {"channels": ["kick:xqc"]}

    asyncio.run(mon.check_channels(
        FakeTwitchAPI(), FakeKickAPI(error=RuntimeError("boom")), config
    ))

    assert rec.started == []
    assert rec.stopped == []


def test_unknown_kick_slug_warned_once_no_start(caplog):
    rec = FakeRecorder()
    mon = make_monitor(recorder=rec)
    config = {"channels": ["kick:ghost"]}

    with caplog.at_level("WARNING", logger="src.stream_archive.monitor"):
        asyncio.run(mon.check_channels(FakeTwitchAPI(), FakeKickAPI(), config))
        asyncio.run(mon.check_channels(FakeTwitchAPI(), FakeKickAPI(), config))

    assert rec.started == []
    assert mon._warned_unknown_kick == {"ghost"}
    warnings = [r for r in caplog.records if "kick channel not found" in r.getMessage()]
    assert len(warnings) == 1


def test_unknown_kick_slug_stops_if_live():
    rec = FakeRecorder()
    mon = make_monitor(recorder=rec)
    config = {"channels": ["kick:ghost"]}
    kick = FakeKickAPI(statuses={
        "ghost": {"title": "T", "game": "G", "is_live": True, "broadcaster_user_id": 1},
    })

    asyncio.run(mon.check_channels(FakeTwitchAPI(), kick, config))
    kick.statuses = {}
    asyncio.run(mon.check_channels(FakeTwitchAPI(), kick, config))

    assert rec.started == ["kick:ghost"]
    assert rec.stopped == ["kick:ghost"]


def test_mixed_channels_resolve_twitch_ids_for_twitch_only():
    rec = FakeRecorder()
    api = FakeTwitchAPI(
        streams={"u1": {"title": "T", "game_name": "G"}},
        user_ids={"ch": "u1"},
    )
    mon = make_monitor(recorder=rec)
    config = {"channels": ["ch", "kick:xqc"]}

    asyncio.run(mon.check_channels(api, FakeKickAPI(statuses=KICK_STATUS_LIVE), config))

    assert api.resolve_calls == [["ch"]]
    assert rec.started == ["ch", "kick:xqc"]


def test_twitch_prefixed_channel_resolves_bare_and_starts():
    rec = FakeRecorder()
    api = FakeTwitchAPI(streams={"u1": {"title": "T", "game_name": "G"}}, user_ids={"streamer1": "u1"})
    mon = make_monitor(recorder=rec)
    config = {"channels": ["twitch:streamer1"]}

    asyncio.run(mon.check_channels(api, FakeKickAPI(), config))

    assert api.resolve_calls == [["streamer1"]]  # bare name goes to the API
    assert rec.started == ["twitch:streamer1"]  # identity stays prefixed
    assert rec.started_kwargs[0]["user_id"] == "u1"


def test_kick_only_config_skips_twitch_api():
    rec = FakeRecorder()
    api = FakeTwitchAPI(error=RuntimeError("twitch should not be called"))
    mon = make_monitor(recorder=rec)
    config = {"channels": ["kick:xqc"]}

    asyncio.run(mon.check_channels(api, FakeKickAPI(statuses=KICK_STATUS_LIVE), config))

    assert api.resolve_calls == []
    assert rec.started == ["kick:xqc"]
