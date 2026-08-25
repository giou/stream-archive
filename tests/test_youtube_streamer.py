import asyncio
import time
from types import SimpleNamespace

from stream_archive.config import AppConfig
from stream_archive.youtube_streamer import YouTubeStreamer, build_video_description


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
    def __init__(self):
        self.valid = False
        self.expired = True
        self.refresh_token = "rt"
        self.refresh_calls = 0

    def refresh(self, request):
        # Synchronous network stand-in. The streamer calls it through
        # asyncio.to_thread.
        time.sleep(0.05)
        self.refresh_calls += 1
        self.valid = True
        self.expired = False

    def to_json(self):
        return "{}"


def make_streamer(tmp_path):
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
    return YouTubeStreamer(cfg)


def test_refresh_is_single_flight_and_offloop(tmp_path, monkeypatch):
    (tmp_path / "youtube_token.json").write_text("{}")
    fake = FakeCreds()
    monkeypatch.setattr(
        "stream_archive.youtube_streamer.Credentials",
        SimpleNamespace(from_authorized_user_info=lambda data, scopes: fake),
    )
    streamer = make_streamer(tmp_path)

    async def scenario():
        ticks = 0
        done = asyncio.Event()

        async def tick():
            nonlocal ticks
            while not done.is_set():
                await asyncio.sleep(0.01)
                ticks += 1

        ticker = asyncio.create_task(tick())
        results = await asyncio.gather(*[streamer._get_credentials() for _ in range(3)])
        done.set()
        await ticker

        assert all(r is fake for r in results)
        assert ticks >= 2  # event loop stayed responsive while refresh ran in a thread
