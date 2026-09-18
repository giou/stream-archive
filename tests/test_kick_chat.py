import asyncio
import base64
import json

import httpx

from stream_archive import kick_chat
from stream_archive.kick_chat import (
    build_comment,
    chat_root_trailer,
    collect_emote_names,
    embedded_data,
    fetch_emote_images,
    parse_time,
    streamer_identity,
    video_id_for,
)


def make_msg(**kw):
    msg = {
        "message_id": "m1",
        "created_at": "2026-08-14T10:00:00Z",
        "broadcaster": {"user_id": 123, "username": "xqc", "profile_picture": "https://example.com/bc.png"},
        "sender": {
            "user_id": 999,
            "username": "viewer1",
            "is_verified": False,
            "is_anonymous": False,
            "profile_picture": "https://example.com/av.png",
            "username_color": "#FF5733",
        },
        "content": "hey \U0001f600 [emote:37226:KEKW]",
        "emotes": [{"emote_id": "37226", "positions": [{"s": 6, "e": 23}]}],
        "badges": [{"text": "Subscriber", "type": "subscriber", "count": 3}],
    }
    msg.update(kw)
    return msg


START_WALL = "2026-08-14T10:00:00+00:00"
START = parse_time(START_WALL)
VIDEO_ID = video_id_for("xqc", START)


def test_comment_structure_and_fields():
    c = build_comment(make_msg(), 123, VIDEO_ID, START)

    assert c["_id"] == "m1"
    assert c["created_at"] == "2026-08-14T10:00:00Z"
    assert c["channel_id"] == "123"
    assert c["content_type"] == "video"
    assert c["content_id"] == VIDEO_ID
    assert c["content_offset_seconds"] == 0.0
    assert c["commenter"]["display_name"] == "viewer1"
    assert c["commenter"]["_id"] == "999"
    assert c["commenter"]["name"] == "viewer1"
    assert c["commenter"]["logo"] == "https://example.com/av.png"
    msg = c["message"]
    assert msg["body"] == "hey \U0001f600 [emote:37226:KEKW]"  # unicode emoji + emote token preserved
    assert msg["fragments"] == [
        {"text": "hey \U0001f600 "},
        {"text": "[emote:37226:KEKW]", "emoticon": {"emoticon_id": "37226"}},
    ]
    assert msg["user_badges"] == [{"_id": "subscriber", "version": "3"}]
    assert msg["user_color"] == "#FF5733"
    assert msg["emoticons"] == [{"_id": "37226", "begin": 6, "end": 24}]
    assert msg["bits_spent"] == 0


def test_comment_no_emotes_single_fragment():
    c = build_comment(make_msg(emotes=None, content="just text"), 123, VIDEO_ID, START)
    assert c["message"]["fragments"] == [{"text": "just text"}]
    assert c["message"]["emoticons"] == []


def test_comment_tokens_split_without_emotes_field():
    # Kick's webhook often omits or breaks the "emotes" array in live data.
    # The self-describing tokens in the body must still become emoticon
    # fragments.
    msg = make_msg(
        message_id="q",
        emotes=None,  # the quirk under test, no emotes payload at all
        content="[emote:39265:EDMusiC][emote:5756616:DanceDance] hi",
    )
    c = build_comment(msg, 123, VIDEO_ID, START)
    assert c["message"]["fragments"] == [
        {"text": "[emote:39265:EDMusiC]", "emoticon": {"emoticon_id": "39265"}},
        {"text": "[emote:5756616:DanceDance]", "emoticon": {"emoticon_id": "5756616"}},
        {"text": " hi"},
    ]
    assert c["message"]["emoticons"] == [
        {"_id": "39265", "begin": 0, "end": 21},
        {"_id": "5756616", "begin": 21, "end": 47},
    ]


def test_comment_bad_positions_ignored_tokens_win():
    # Bogus or out-of-bounds positions in the emotes array must not break splitting.
    msg = make_msg(content="abc [emote:37226:KEKW]", emotes=[{"emote_id": "37226", "positions": [{"s": 99, "e": 120}]}])
    c = build_comment(msg, 123, VIDEO_ID, START)
    assert c["message"]["fragments"] == [
        {"text": "abc "},
        {"text": "[emote:37226:KEKW]", "emoticon": {"emoticon_id": "37226"}},
    ]


def test_comment_multiple_emotes_split_order():
    msg = make_msg(
        content="[emote:1:AAA] mid [emote:2:BBB] end",
        emotes=[
            {"emote_id": "1", "positions": [{"s": 0, "e": 12}]},
            {"emote_id": "2", "positions": [{"s": 18, "e": 30}]},
        ],
    )
    fragments = build_comment(msg, 123, VIDEO_ID, START)["message"]["fragments"]
    assert fragments == [
        {"text": "[emote:1:AAA]", "emoticon": {"emoticon_id": "1"}},
        {"text": " mid "},
        {"text": "[emote:2:BBB]", "emoticon": {"emoticon_id": "2"}},
        {"text": " end"},
    ]


def test_comment_offset_less_timestamp_is_treated_as_utc():
    """Kick can send no offset. The subtraction must not raise then."""
    msg = make_msg(created_at="2026-08-14T10:05:30", emotes=None, badges=None)

    c = build_comment(msg, 123, VIDEO_ID, START)

    assert c["content_offset_seconds"] == 330.0


def test_comment_offsets_and_missing_fields():
    msg = make_msg(
        created_at="2026-08-14T10:05:30Z",
        message_id=None,
        sender={},
        badges=None,
        broadcaster={},
    )
    c = build_comment(msg, None, VIDEO_ID, START)

    assert c["content_offset_seconds"] == 330.0
    assert c["_id"] == "None-2026-08-14T10:05:30Z"  # fallback id
    assert c["channel_id"] == ""  # no broadcaster id known yet
    assert c["commenter"]["name"] == "anonymous"
    assert c["commenter"]["_id"] == ""
    assert c["message"]["user_badges"] == []


def test_comment_no_start_time_zero_offsets():
    c = build_comment(make_msg(), None, "kick-xqc-0", None)
    assert c["content_offset_seconds"] == 0.0


def test_comment_unicode_emoji_roundtrip():
    c = build_comment(make_msg(content="\U0001f525\U0001f389"), 123, VIDEO_ID, START)
    assert c["message"]["body"] == "\U0001f525\U0001f389"


def test_streamer_identity_from_broadcaster():
    assert streamer_identity(make_msg(), "xqc") == (123, "xqc")
    assert streamer_identity(make_msg(broadcaster={}), "xqc") == (None, "xqc")


def test_video_id_for_uses_start_time():
    assert VIDEO_ID == "kick-xqc-1786701600"  # 2026-08-14T10:00:00Z, pinned
    assert video_id_for("xqc", START) == "kick-xqc-1786701600"
    assert START is not None and START.tzinfo is not None  # parse_time keeps the UTC offset
    assert video_id_for("xqc", None) == "kick-xqc-0"


def test_trailer_structure_and_fields():
    trailer = chat_root_trailer("xqc", "Big stream", START_WALL, START, 3600.0, 123, "xqc")

    assert trailer["FileInfo"]["Version"] == {"Major": 1, "Minor": 4, "Patch": 0}
    assert trailer["streamer"] == {"id": 123, "name": "xqc", "login": "xqc"}
    assert trailer["video"]["title"] == "Big stream"
    assert trailer["video"]["created_at"] == START_WALL
    assert trailer["video"]["start"] == 0.0
    assert trailer["video"]["end"] == 3600.0
    assert trailer["video"]["length"] == 3600.0
    assert trailer["video"]["id"] == f"kick-xqc-{int(START.timestamp())}"


def test_trailer_without_streamer_id_or_start():
    trailer = chat_root_trailer("xqc", "T", None, None, 0.0, None, "xqc")

    assert trailer["streamer"] == {"name": "xqc", "login": "xqc"}
    assert "created_at" not in trailer["video"]  # TD DateTime field omitted, never ""
    assert trailer["video"]["id"] == "kick-xqc-0"


def test_collect_emote_names_first_use_order():
    names = {}
    assert collect_emote_names(names, "[emote:1:AAA] hi [emote:2:BBB]") == 0
    assert names == {"1": "AAA", "2": "BBB"}
    # A repeated id keeps the first name, a new id appends.
    assert collect_emote_names(names, "[emote:1:AAA] [emote:3:CCC]") == 0
    assert list(names) == ["1", "2", "3"]


def test_collect_emote_names_reports_limit_skips(monkeypatch):
    monkeypatch.setattr(kick_chat, "MAX_EMOTES_PER_RECORDING", 2)
    names = {"1": "AAA", "2": "BBB"}

    skipped = collect_emote_names(names, "[emote:3:CCC][emote:4:DDD][emote:1:AAA]")

    assert skipped == 2
    assert names == {"1": "AAA", "2": "BBB"}


def test_fetch_emote_images_with_mock_transport():
    def handler(request):
        if request.url.path.endswith("/37226/fullsize"):
            return httpx.Response(200, content=b"PNGDATA")
        return httpx.Response(404)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            images = await fetch_emote_images(["37226", "missing"], client)
        assert images == {"37226": b"PNGDATA"}  # 404 skipped silently

    asyncio.run(scenario())


def test_fetch_emote_images_count_limit():
    names = []

    def handler(request):
        names.append(request.url.path)
        return httpx.Response(200, content=b"IMG")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            images = await fetch_emote_images(["1", "2", "3", "4"], client, max_emotes=2)
        assert len(images) == 2
        assert len(names) == 2  # the limit stops the requests, not only the result

    asyncio.run(scenario())


def test_fetch_emote_images_per_image_limit():
    def handler(request):
        return httpx.Response(200, content=b"x" * 64)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            images = await fetch_emote_images(["1"], client, max_bytes_each=16)
        assert images == {}  # an oversized image is skipped, the capture continues

    asyncio.run(scenario())


def test_fetch_emote_images_total_limit():
    def handler(request):
        return httpx.Response(200, content=b"x" * 4)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            images = await fetch_emote_images(["1", "2", "3", "4", "5"], client, max_total_bytes=8)
        assert sum(len(v) for v in images.values()) == 8  # 4-byte images fill the cap exactly
        assert images

    asyncio.run(scenario())


def test_embedded_data_base64_and_name():
    def handler(request):
        if request.url.path.endswith("/37226/fullsize"):
            return httpx.Response(200, content=b"\x89PNG-fake")
        return httpx.Response(404)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            embedded = await embedded_data({"37226": "KEKW", "404": "MISSING"}, client)
        assert embedded == {
            "firstParty": [
                {
                    "id": "37226",
                    "imageScale": 2,
                    "data": base64.b64encode(b"\x89PNG-fake").decode("ascii"),
                    "name": "KEKW",  # parsed from the [emote:id:NAME] token
                }
            ]
        }
        # TwitchDownloader can deserialize the result. FileInfo versions above
        # 1.2.2 gate this modern shape.
        json.dumps(embedded)

    asyncio.run(scenario())


def test_embedded_data_without_images_or_names():
    assert asyncio.run(embedded_data({})) is None

    def handler(request):
        return httpx.Response(404)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await embedded_data({"1": "AAA"}, client) is None

    asyncio.run(scenario())


def test_embedded_data_never_raises(monkeypatch, caplog):
    calls = []

    async def boom(ids, client=None):
        calls.append(list(ids))
        msg = "network down"
        raise httpx.ConnectError(msg)

    monkeypatch.setattr(kick_chat, "fetch_emote_images", boom)

    with caplog.at_level("ERROR", logger="stream_archive.kick_chat"):
        assert asyncio.run(embedded_data({"1": "AAA"})) is None

    assert calls == [["1"]]  # the stub ran, so the guard path is the one under test
    assert any("emote embedding failed" in r.getMessage() for r in caplog.records)


def test_embedded_data_bounds_the_encoded_payload(monkeypatch, caplog):
    """The embedded block is held as text, so its own size is bounded.

    Base64 grows the images by a third, and the whole block is serialized into
    the file. Without a bound, one recording's finalize held several copies of
    a 16 MiB emote set in memory.
    """
    monkeypatch.setattr("stream_archive.kick_chat.MAX_EMOTE_TOTAL_BYTES", 8)

    def handler(request):
        return httpx.Response(200, content=b"1234")  # 4 bytes -> 8 base64 chars

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with caplog.at_level("WARNING"):
                embedded = await embedded_data({"1": "A", "2": "B", "3": "C"}, client)
        return embedded

    embedded = asyncio.run(scenario())

    assert embedded is not None
    assert [item["id"] for item in embedded["firstParty"]] == ["1"]
    assert "embeddedData limit reached" in caplog.text
    json.dumps(embedded)  # the truncated block is still valid JSON
