import asyncio
import json
import threading

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
