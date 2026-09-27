"""Third-party emote sets, word splitting, and image embedding."""

import asyncio
import base64

import httpx

from stream_archive.emotes import (
    build_first_party,
    download_images,
    embed_images,
    fetch_channel_emotes,
    find_words,
    parse_7tv,
    parse_bttv,
    parse_ffz,
    sniff_mime,
)

# Shape of the live 7TV response, trimmed to two xqc emotes.
XQC_7TV = {
    "emote_set": {
        "emotes": [
            {"id": "01G3WEGZN0000ET2J0MQP5YJ0G", "name": "GAMBA"},
            {"id": "01FP6SPEB00001BCZZ99DVK9W5", "name": "baseg"},
        ]
    }
}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            msg = f"status {self._status}"
            raise httpx.HTTPStatusError(msg, request=None, response=None)  # type: ignore[arg-type]

    def json(self):
        return self._payload


class _Stub:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls: list[str] = []

    async def get(self, url):
        self.calls.append(url)
        if url in self.payloads:
            return _Resp(self.payloads[url])
        return _Resp(None, status=404)

    async def aclose(self):
        pass


def test_parse_7tv_names():
    assert parse_7tv(XQC_7TV) == {
        "GAMBA": ("7tv:01G3WEGZN0000ET2J0MQP5YJ0G", "https://cdn.7tv.app/emote/01G3WEGZN0000ET2J0MQP5YJ0G/1x.webp"),
        "baseg": ("7tv:01FP6SPEB00001BCZZ99DVK9W5", "https://cdn.7tv.app/emote/01FP6SPEB00001BCZZ99DVK9W5/1x.webp"),
    }
    assert parse_7tv({}) == {}
    assert parse_7tv(None) == {}


def test_parse_bttv_and_ffz():
    assert parse_bttv({"channelEmotes": [{"id": "abc", "code": "Wow"}], "sharedEmotes": []}) == {
        "Wow": ("bttv:abc", "https://cdn.betterttv.net/emote/abc/1x")
    }
    assert parse_bttv([{"id": "abc", "code": "Wow"}]) == {"Wow": ("bttv:abc", "https://cdn.betterttv.net/emote/abc/1x")}
    ffz = {
        "sets": {"1": {"emoticons": [{"id": 9, "name": "ZrehplaR", "urls": {"1": "//cdn.frankerfacez.com/emote/9/1"}}]}}
    }
    assert parse_ffz(ffz) == {"ZrehplaR": ("ffz:9", "https://cdn.frankerfacez.com/emote/9/1")}
    assert parse_ffz({}) == {}


def test_parse_ffz_drops_foreign_hosts():
    """Only the provider image host is downloadable; the rest stays text."""
    ffz = {
        "sets": {"1": {"emoticons": [{"id": 666, "name": "Skin", "urls": {"1": "https://attacker.example/skin.webp"}}]}}
    }
    assert parse_ffz(ffz) == {}


def test_find_words_positions():
    names = {"GAMBA": ("7tv:g", "https://x/g"), "baseg": ("7tv:b", "https://x/b")}
    assert find_words("GAMBA all baseg", names) == [
        (0, 5, "7tv:g", "GAMBA", "https://x/g"),
        (10, 15, "7tv:b", "baseg", "https://x/b"),
    ]
    assert find_words("nothing here", names) == []
    assert find_words("GAMBA!", names) == []  # punctuation needs the replay fallback


def test_sniff_mime():
    assert sniff_mime(b"\x89PNG\r\n\x1a\n" + b"\0") == "image/png"
    assert sniff_mime(b"GIF89a" + b"\0") == "image/gif"
    assert sniff_mime(b"\xff\xd8\xff" + b"\0") == "image/jpeg"
    assert sniff_mime(b"RIFF\0\0\0\0WEBP") == "image/webp"
    assert sniff_mime(b"nope") is None


def test_fetch_kick_channel_emotes():
    stub = _Stub({"https://7tv.io/v3/users/kick/676": XQC_7TV})

    async def run():
        return await fetch_channel_emotes(stub, "kick", "676")

    names = asyncio.run(run())
    assert names["GAMBA"][0] == "7tv:01G3WEGZN0000ET2J0MQP5YJ0G"
    assert stub.calls == ["https://7tv.io/v3/users/kick/676"]  # one provider only


def test_embed_images_shape():
    """An unknown URL answers 404, so nothing is embedded."""
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await embed_images(client, {"7tv:g": ("GAMBA", "https://x/g")})

    assert asyncio.run(run()) is None  # unknown URL 404s, nothing embedded
    assert requested == ["https://x/g"]  # the download was attempted, not skipped


def test_build_first_party_entries():
    raw = b"\x89PNG\r\n\x1a\n" + b"\0" * 10
    entries = build_first_party({"7tv:g": raw}, {"7tv:g": "GAMBA"})
    assert entries == [{"id": "7tv:g", "imageScale": 2, "data": base64.b64encode(raw).decode("ascii"), "name": "GAMBA"}]


def _run_download(handler, items, **kwargs):
    """Run ``download_images`` with a client on a mock transport."""

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await download_images(client, items, **kwargs)

    return asyncio.run(scenario())


def test_download_images_skips_failures():
    def handler(request):
        if request.url.path.endswith("/good"):
            return httpx.Response(200, content=b"PNGDATA")
        return httpx.Response(404)

    images = _run_download(handler, {"a": "https://x/good", "b": "https://x/bad"})
    assert images == {"a": b"PNGDATA"}  # 404 skipped silently


def test_download_images_count_limit():
    names = []

    def handler(request):
        names.append(request.url.path)
        return httpx.Response(200, content=b"IMG")

    items = {str(i): f"https://x/{i}" for i in range(4)}
    images = _run_download(handler, items, max_emotes=2)
    assert len(images) == 2
    assert len(names) == 2  # the limit stops the requests, not only the result


def test_download_images_per_image_limit():
    def handler(request):
        return httpx.Response(200, content=b"x" * 64)

    images = _run_download(handler, {"1": "https://x/1"}, max_bytes_each=16)
    assert images == {}  # an oversized image is skipped, the capture continues


def test_download_images_total_limit():
    def handler(request):
        return httpx.Response(200, content=b"x" * 4)

    items = {str(i): f"https://x/{i}" for i in range(5)}
    images = _run_download(handler, items, max_total_bytes=8)
    assert sum(len(v) for v in images.values()) == 8  # 4-byte images fill the cap exactly
    assert images
