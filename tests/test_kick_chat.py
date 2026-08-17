import base64
import json

import httpx

from stream_archive.kick_chat import (
    build_chat_root,
    collect_emote_ids,
    embed_emotes,
    embed_kick_emotes,
    fetch_emote_images,
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


START = "2026-08-14T10:00:00+00:00"


def test_chat_root_structure_and_fields():
    root = build_chat_root("kick:xqc", "xqc", "Big stream", START, [make_msg()], duration_s=3600)

    assert root["FileInfo"]["Version"] == {"Major": 1, "Minor": 4, "Patch": 0}
    assert root["streamer"] == {"id": 123, "name": "xqc", "login": "xqc"}
    assert root["video"]["title"] == "Big stream"
    assert root["video"]["created_at"] == START
    assert root["video"]["start"] == 0.0
    assert root["video"]["end"] == 3600.0
    assert root["video"]["length"] == 3600.0
    assert root["video"]["id"].startswith("kick-xqc-")

    c = root["comments"][0]
    assert c["_id"] == "m1"
    assert c["created_at"] == "2026-08-14T10:00:00Z"
    assert c["channel_id"] == "123"
    assert c["content_type"] == "video"
    assert c["content_id"] == root["video"]["id"]
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


def test_chat_root_no_emotes_single_fragment():
    root = build_chat_root("kick:xqc", "xqc", "T", START, [make_msg(emotes=None, content="just text")])
    assert root["comments"][0]["message"]["fragments"] == [{"text": "just text"}]
    assert root["comments"][0]["message"]["emoticons"] == []


def test_chat_root_tokens_split_without_emotes_field():
    # kick's webhook often omits/breaks the "emotes" array (live data) — the
    # self-describing tokens in the body must still become emoticon fragments
    msg = make_msg(
        message_id="q",
        emotes=None,  # the quirk: no emotes payload at all
        content="[emote:39265:EDMusiC][emote:5756616:DanceDance] hi",
    )
    c = build_chat_root("kick:xqc", "xqc", "T", START, [msg])["comments"][0]
    assert c["message"]["fragments"] == [
        {"text": "[emote:39265:EDMusiC]", "emoticon": {"emoticon_id": "39265"}},
        {"text": "[emote:5756616:DanceDance]", "emoticon": {"emoticon_id": "5756616"}},
        {"text": " hi"},
    ]
    assert c["message"]["emoticons"] == [
        {"_id": "39265", "begin": 0, "end": 21},
        {"_id": "5756616", "begin": 21, "end": 47},
    ]


def test_chat_root_bad_positions_ignored_tokens_win():
    # bogus/out-of-bounds positions in the emotes array must not break splitting
    msg = make_msg(content="abc [emote:37226:KEKW]", emotes=[{"emote_id": "37226", "positions": [{"s": 99, "e": 120}]}])
    root = build_chat_root("kick:xqc", "xqc", "T", START, [msg])
    assert root["comments"][0]["message"]["fragments"] == [
        {"text": "abc "},
        {"text": "[emote:37226:KEKW]", "emoticon": {"emoticon_id": "37226"}},
    ]


def test_chat_root_multiple_emotes_split_order():
    msg = make_msg(
        content="[emote:1:AAA] mid [emote:2:BBB] end",
        emotes=[
            {"emote_id": "1", "positions": [{"s": 0, "e": 12}]},
            {"emote_id": "2", "positions": [{"s": 18, "e": 30}]},
        ],
    )
    fragments = build_chat_root("kick:xqc", "xqc", "T", START, [msg])["comments"][0]["message"]["fragments"]
    assert fragments == [
        {"text": "[emote:1:AAA]", "emoticon": {"emoticon_id": "1"}},
        {"text": " mid "},
        {"text": "[emote:2:BBB]", "emoticon": {"emoticon_id": "2"}},
        {"text": " end"},
    ]


def test_chat_root_offsets_and_missing_fields():
    msg = make_msg(
        created_at="2026-08-14T10:05:30Z",
        message_id=None,
        sender={},
        badges=None,
        broadcaster={},
    )
    root = build_chat_root("kick:xqc", "xqc", "T", START, [msg])

    c = root["comments"][0]
    assert c["content_offset_seconds"] == 330.0
    assert c["_id"] == "None-2026-08-14T10:05:30Z"  # fallback id
    assert c["commenter"]["name"] == "anonymous"
    assert c["commenter"]["_id"] == ""
    assert c["message"]["user_badges"] == []
    assert root["streamer"] == {"name": "xqc", "login": "xqc"}  # no id
    assert c["channel_id"] == ""


def test_chat_root_no_start_time_zero_offsets():
    root = build_chat_root("kick:xqc", "xqc", "T", None, [make_msg()])
    assert root["comments"][0]["content_offset_seconds"] == 0.0
    assert "created_at" not in root["video"]  # TD DateTime: omit, never ""


def test_chat_root_empty_messages():
    root = build_chat_root("kick:xqc", "xqc", "T", START, [])
    assert root["comments"] == []
    assert "id" not in root["streamer"]


def test_chat_root_unicode_emoji_roundtrip():
    root = build_chat_root("kick:xqc", "xqc", "T", START, [make_msg(content="\U0001f525\U0001f389")])
    assert root["comments"][0]["message"]["body"] == "\U0001f525\U0001f389"


def test_collect_emote_ids_unique_in_order():
    root = build_chat_root(
        "kick:xqc",
        "xqc",
        "T",
        START,
        [
            make_msg(message_id="a", content="[emote:1:AAA]"),
            make_msg(message_id="b", content="[emote:2:BBB] [emote:1:AAA]"),
        ],
    )
    assert collect_emote_ids(root) == ["1", "2"]


def test_fetch_emote_images_with_mock_transport():
    def handler(request):
        if request.url.path.endswith("/37226/fullsize"):
            return httpx.Response(200, content=b"PNGDATA")
        return httpx.Response(404)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            images = await fetch_emote_images(["37226", "missing"], client)
        assert images == {"37226": b"PNGDATA"}  # 404 skipped silently

    import asyncio

    asyncio.run(scenario())


def test_embed_emotes_fills_first_party_base64():
    root = build_chat_root("kick:xqc", "xqc", "T", START, [make_msg()])
    embed_emotes(root, {"37226": b"\x89PNG-fake"})

    first = root["embeddedData"]["firstParty"]
    assert first == [
        {
            "id": "37226",
            "imageScale": 2,
            "data": base64.b64encode(b"\x89PNG-fake").decode("ascii"),
            "name": "KEKW",  # parsed from the [emote:id:NAME] token
        }
    ]
    # TwitchDownloader can deserialize it: FileInfo > 1.2.2 gates the modern shape
    json.dumps(root)


def test_embed_emotes_missing_images_noop():
    root = build_chat_root("kick:xqc", "xqc", "T", START, [make_msg()])
    embed_emotes(root, {})
    assert "embeddedData" not in root


def test_embed_kick_emotes_orchestrates(monkeypatch):
    import asyncio

    root = build_chat_root("kick:xqc", "xqc", "T", START, [make_msg()])

    async def fake_fetch(ids, client=None):
        return {i: b"IMG" for i in ids}

    monkeypatch.setattr("stream_archive.kick_chat.fetch_emote_images", fake_fetch)

    async def scenario():
        await embed_kick_emotes(root, client="unused")

    asyncio.run(scenario())

    assert root["embeddedData"]["firstParty"][0]["id"] == "37226"
    assert root["embeddedData"]["firstParty"][0]["data"] == base64.b64encode(b"IMG").decode("ascii")
