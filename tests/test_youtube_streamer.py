from src.stream_archive.youtube_streamer import build_video_description


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
