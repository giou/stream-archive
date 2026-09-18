import asyncio
import json
import threading

import pytest

from stream_archive.config import AppConfig
from stream_archive.youtube_streamer import SCOPES, YouTubeStreamer, build_video_description

TOKEN_DATA = {"refresh_token": "rt", "client_id": "cid", "client_secret": "csec"}


def test_description_twitch_channel():
    text = build_video_description("xqc", "xqc", "League of Legends")
    assert text == (
        "Twitch stream by xqc\n"
        "Game: League of Legends\n"
        "Originally streamed at: https://twitch.tv/xqc\n"
        "Recorded by StreamArchive"
    )


def test_description_kick_channel_uses_kick_url_and_label():
    text = build_video_description("xqc", "kick:xqc", "League of Legends")
    assert text == (
        "Kick stream by xqc\n"
        "Game: League of Legends\n"
        "Originally streamed at: https://kick.com/xqc\n"
        "Recorded by StreamArchive"
    )
    assert "twitch.tv/kick:" not in text


class FakeCreds:
    """Credentials whose refresh the test can hold open, and make fail.

    The streamer calls refresh through asyncio.to_thread, so the call lands on
    a worker thread. A gate keeps the first refresh inside the streamer while
    the other callers run.
    """

    def __init__(self, gate=None, fail_times=0):
        self.valid = False
        self.expired = True
        self.token = "access-token"
        self.refresh_token = "rt"
        self.refresh_calls = 0
        self.refresh_thread = None
        self.gate = gate
        self.fail_times = fail_times

    def refresh(self, request):
        self.refresh_thread = threading.get_ident()
        self.refresh_calls += 1
        if self.gate is not None and not self.gate.wait(10):
            msg = "the test never released the refresh gate"
            raise TimeoutError(msg)
        if self.refresh_calls <= self.fail_times:
            msg = "the token endpoint rejected the refresh"
            raise RuntimeError(msg)
        self.valid = True
        self.expired = False

    def to_json(self):
        return json.dumps({"refresh_token": "rt", "token": "fresh"})


class CredentialsStub:
    """Records the payload and the scopes that the streamer loads from."""

    def __init__(self, creds):
        self.creds = creds
        self.data = None
        self.scopes = None

    def from_authorized_user_info(self, data, scopes):
        self.data = data
        self.scopes = scopes
        return self.creds


def make_streamer(tmp_path, creds):
    data = {
        "telegram_user_id": 12345,
        "bot_telegram_api": "bot_token",
        "twitch_client_id": "client_id",
        "twitch_client_secret": "client_secret",
        "channels": ["twitch:ch"],
        "proxy_list": ["httpproxy://user:pass@host:port"],
        "monitoring_interval": 60,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "recording_dir": str(tmp_path),
    }
    cfg = AppConfig.model_validate(data)
    cfg._workdir = tmp_path
    stub = CredentialsStub(creds)
    return YouTubeStreamer(cfg), stub


def test_refresh_is_single_flight_and_offloop(tmp_path, monkeypatch):
    """Three concurrent callers share one refresh, which runs off the loop thread."""
    (tmp_path / "youtube_token.json").write_text(json.dumps(TOKEN_DATA))
    gate = threading.Event()
    fake = FakeCreds(gate=gate)
    streamer, stub = make_streamer(tmp_path, fake)
    monkeypatch.setattr("stream_archive.youtube_streamer.Credentials", stub)
    caller_thread = threading.get_ident()

    async def scenario():
        try:
            tasks = [asyncio.create_task(streamer._get_credentials()) for _ in range(3)]
            # The first refresh waits on the gate, so the loop runs the other
            # two callers. Without the lock they would refresh as well.
            for _ in range(50):
                await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*tasks)
            assert all(r is fake for r in results)
        finally:
            await streamer.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    assert fake.refresh_calls == 1
    assert fake.refresh_thread != caller_thread
    # The streamer loads the token file and asks for the module's scopes.
    assert stub.data == TOKEN_DATA
    assert stub.scopes == SCOPES
    # A refresh writes the new token back.
    assert json.loads((tmp_path / "youtube_token.json").read_text()) == {"refresh_token": "rt", "token": "fresh"}


def test_refresh_failure_reaches_every_caller_and_frees_the_lock(tmp_path, monkeypatch):
    """A failed refresh raises in every caller, and a later call can refresh."""
    (tmp_path / "youtube_token.json").write_text(json.dumps(TOKEN_DATA))
    fake = FakeCreds(fail_times=10)
    streamer, stub = make_streamer(tmp_path, fake)
    monkeypatch.setattr("stream_archive.youtube_streamer.Credentials", stub)

    async def scenario():
        try:
            results = await asyncio.gather(*[streamer._get_credentials() for _ in range(3)], return_exceptions=True)
            assert [type(r) for r in results] == [RuntimeError] * 3
            failures = fake.refresh_calls
            # The lock is free after the failures, so a later call can refresh.
            fake.fail_times = 0
            assert await streamer._get_credentials() is fake
            assert fake.refresh_calls == failures + 1
        finally:
            await streamer.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_broadcast_title_and_description_are_one_line_each():
    """The published title and description carry platform text, so canonicalize it."""
    description = build_video_description("author\nUrl: https://evil.example", "twitch:ch", "game\u2028x")
    assert description.split("\n") == [
        "Twitch stream by author Url: https://evil.example",
        "Game: game x",
        "Originally streamed at: https://twitch.tv/ch",
        "Recorded by StreamArchive",
    ]


class _RecordingClient:
    """HTTP client double that records method/path and can cancel one call."""

    def __init__(self, cancel_path: str | None = None, block_path: str | None = None, delete_delay: float = 0):
        self.calls: list[tuple[str, str]] = []
        self.cancel_path = cancel_path
        self.block_path = block_path
        self.delete_delay = delete_delay
        self.streamer = None
        self.tracking_seen: list[int] = []
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def request(self, method, url, **_kwargs):
        path = url.rsplit("/v3/", 1)[-1]
        self.calls.append((method, path))
        if self.block_path is not None and path == self.block_path:
            self.reached.set()
            await self.release.wait()
        if method == "DELETE":
            # Observe the streamer's reference to this task from inside it:
            # that is exactly what keeps the rollback alive after the await
            # that started it was cancelled.
            if self.streamer is not None:
                self.tracking_seen.append(len(self.streamer._rollback_tasks))
            if self.delete_delay:
                await asyncio.sleep(self.delete_delay)
        if self.cancel_path is not None and path == self.cancel_path:
            raise asyncio.CancelledError
        if method == "POST" and path == "liveBroadcasts":
            return _Response({"id": "bcast-1"})
        if method == "POST" and path == "liveStreams":
            return _Response(
                {
                    "id": "stream-1",
                    "cdn": {"ingestionInfo": {"ingestionAddress": "rtmp://a/live2", "streamName": "key"}},
                }
            )
        return _Response({})


class _Response:
    def __init__(self, payload):
        self.status_code = 200
        self.content = b"{}"
        self.text = ""
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_cancelled_create_rolls_back_the_broadcast_and_stream(tmp_path, monkeypatch):
    """A cancellation must not leave YouTube resources behind.

    The termination path cancels the recording task, and CancelledError is a
    BaseException, so the rollback that handles every other failure never ran:
    the account kept a broadcast and a live stream, bound, with neither ended
    nor deleted, because the caller never received their ids.
    """
    creds = FakeCreds()
    creds.valid = True
    creds.expired = False
    (tmp_path / "youtube_token.json").write_text(json.dumps(TOKEN_DATA))
    streamer, stub = make_streamer(tmp_path, creds)
    # The credentials double keeps the exchange offline.
    monkeypatch.setattr("stream_archive.youtube_streamer.Credentials", stub)
    client = _RecordingClient(cancel_path="liveBroadcasts/bind")
    streamer._client = client

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await streamer.create_stream("author", "title", "twitch:ch", "game")

    asyncio.run(scenario())

    assert ("POST", "liveBroadcasts/bind") in client.calls
    assert ("DELETE", "liveStreams") in client.calls
    assert ("DELETE", "liveBroadcasts") in client.calls


def test_rollback_survives_the_cancellation_that_started_it(tmp_path, monkeypatch):
    """The rollback must keep a strong reference to survive its own trigger.

    The cancellation path starts the rollback task and awaits it through
    shield, which drops its callback on the inner task once the outer await is
    cancelled. Without a reference held by the streamer, the loop can collect
    that task and the broadcast and bound live stream stay on the account.
    """
    creds = FakeCreds()
    creds.valid = True
    creds.expired = False
    (tmp_path / "youtube_token.json").write_text(json.dumps(TOKEN_DATA))
    streamer, stub = make_streamer(tmp_path, creds)
    monkeypatch.setattr("stream_archive.youtube_streamer.Credentials", stub)
    client = _RecordingClient(block_path="liveBroadcasts/bind")
    client.streamer = streamer
    streamer._client = client

    async def scenario():
        task = asyncio.ensure_future(streamer.create_stream("author", "title", "twitch:ch", "game"))
        await client.reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if streamer._rollback_tasks:
            await asyncio.gather(*streamer._rollback_tasks, return_exceptions=True)

    asyncio.run(scenario())

    assert client.tracking_seen and set(client.tracking_seen) == {1}, (
        f"the streamer must hold the rollback task while it runs: observed {client.tracking_seen}"
    )
    assert ("DELETE", "liveStreams") in client.calls
    assert ("DELETE", "liveBroadcasts") in client.calls
    assert streamer._rollback_tasks == set()
