"""Third-party emote sets and image embedding for chat files.

TwitchDownloader renders fragments with emoticon refs from
``embeddedData.firstParty`` before any CDN. Twitch and Kick native emotes
already carry ids, but third-party emotes (7TV, BTTV, FFZ) arrive as plain
words. Capture splits those words into emoticon fragments with
provider-prefixed ids, downloads the images, and embeds them, so offline
renderers and the web replay show the same emotes the live chat showed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

#: Image of one Twitch emote id, dark theme, 1x.
TWITCH_EMOTE_URL = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/1.0"

#: Third-party emote lookups. A name in two sets keeps the first URL.
SEVENTV_USER_URL = "https://7tv.io/v3/users/twitch/{channel_id}"
SEVENTV_KICK_USER_URL = "https://7tv.io/v3/users/kick/{channel_id}"
SEVENTV_EMOTE_URL = "https://cdn.7tv.app/emote/{id}/1x.webp"
BTTV_USER_URL = "https://api.betterttv.net/3/cached/users/twitch/{channel_id}"
BTTV_GLOBAL_URL = "https://api.betterttv.net/3/cached/emotes/global"
BTTV_EMOTE_URL = "https://cdn.betterttv.net/emote/{id}/1x"
FFZ_ROOM_URL = "https://api.frankerfacez.com/v1/room/id/{channel_id}"
FFZ_GLOBAL_URL = "https://api.frankerfacez.com/v1/set/global"

#: Only host the recorder downloads FFZ emote images from. Provider data
#: carries absolute URLs, so any other host stays a plain word.
FFZ_IMAGE_HOST = "cdn.frankerfacez.com"

#: Distinct emote ids embedded for one recording.
MAX_EMOTES_PER_RECORDING = 1024
#: Largest image accepted for one emote.
MAX_EMOTE_BYTES = 512 * 1024
#: Largest total download for one recording. The same value bounds the
#: base64 text that the chat file embeds, because that text is held in
#: memory while the trailer is written.
MAX_EMOTE_TOTAL_BYTES = 16 * 1024 * 1024

#: Fetch concurrency of the image downloads.
_EMOTE_FETCH_CONCURRENCY = 8


def _own_client(client: httpx.AsyncClient | None) -> tuple[httpx.AsyncClient, bool]:
    """Client to use plus whether the caller must close it."""
    if client is not None:
        return client, False
    return httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)), True


async def _fetch_json(client: httpx.AsyncClient | None, url: str) -> Any | None:
    """GET one JSON document, or None on any failure. Best effort only."""
    http, own = _own_client(client)
    try:
        resp = await http.get(url)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.debug("[emotes] fetch failed for %s", url, exc_info=True)
        return None
    finally:
        if own:
            await http.aclose()


def parse_7tv(payload: Any) -> dict[str, tuple[str, str]]:
    """Emote name to (id, image URL) of a 7TV user response, or empty."""
    out: dict[str, tuple[str, str]] = {}
    if not isinstance(payload, dict):
        return out
    eset = payload.get("emote_set")
    emotes = eset.get("emotes") if isinstance(eset, dict) else None
    if not isinstance(emotes, list):
        return out
    for emote in emotes:
        if not isinstance(emote, dict):
            continue
        name, eid = emote.get("name"), emote.get("id")
        if isinstance(name, str) and name and isinstance(eid, str) and eid:
            out.setdefault(name, (f"7tv:{eid}", SEVENTV_EMOTE_URL.format(id=eid)))
    return out


def parse_bttv(payload: Any) -> dict[str, tuple[str, str]]:
    """Emote code to (id, image URL) of a BTTV user or global response, or empty."""
    out: dict[str, tuple[str, str]] = {}
    groups: list[Any] = []
    if isinstance(payload, dict):
        groups = [payload.get("channelEmotes"), payload.get("sharedEmotes")]
    elif isinstance(payload, list):
        groups = [payload]
    for group in groups:
        if not isinstance(group, list):
            continue
        for emote in group:
            if not isinstance(emote, dict):
                continue
            code, eid = emote.get("code"), emote.get("id")
            if isinstance(code, str) and code and isinstance(eid, str) and eid:
                out.setdefault(code, (f"bttv:{eid}", BTTV_EMOTE_URL.format(id=eid)))
    return out


def parse_ffz(payload: Any) -> dict[str, tuple[str, str]]:
    """Emote name to (id, image URL) of an FFZ room or global response, or empty."""
    out: dict[str, tuple[str, str]] = {}
    sets = payload.get("sets") if isinstance(payload, dict) else None
    if not isinstance(sets, dict):
        return out
    for group in sets.values():
        emoticons = group.get("emoticons") if isinstance(group, dict) else None
        if not isinstance(emoticons, list):
            continue
        for emote in emoticons:
            if not isinstance(emote, dict):
                continue
            name = emote.get("name")
            urls = emote.get("urls")
            eid = emote.get("id")
            if not isinstance(name, str) or not name or not isinstance(urls, dict):
                continue
            if isinstance(eid, bool) or (not isinstance(eid, (str, int)) or not str(eid)):
                continue
            raw = urls.get("1") or urls.get("2") or urls.get("4")
            if not isinstance(raw, str) or not raw:
                continue
            url = f"https:{raw}" if raw.startswith("//") else raw
            if not url.startswith("https://") or urlsplit(url).hostname != FFZ_IMAGE_HOST:
                continue
            out.setdefault(name, (f"ffz:{eid}", url))
    return out


async def fetch_channel_emotes(
    client: httpx.AsyncClient | None, platform: str, channel_id: str
) -> dict[str, tuple[str, str]]:
    """Emote name to (id, image URL) of one channel: 7TV, BTTV, FFZ.

    Kick channels carry 7TV sets only. A name in two sets keeps the
    first URL. Never raises: a down provider only shrinks the map.
    """
    merged: dict[str, tuple[str, str]] = {}
    if platform == "kick":
        seventv = await _fetch_json(client, SEVENTV_KICK_USER_URL.format(channel_id=channel_id))
        return parse_7tv(seventv)
    seventv = await _fetch_json(client, SEVENTV_USER_URL.format(channel_id=channel_id))
    for name, ref in parse_7tv(seventv).items():
        merged.setdefault(name, ref)
    bttv = await _fetch_json(client, BTTV_USER_URL.format(channel_id=channel_id))
    for name, ref in parse_bttv(bttv).items():
        merged.setdefault(name, ref)
    ffz = await _fetch_json(client, FFZ_ROOM_URL.format(channel_id=channel_id))
    for name, ref in parse_ffz(ffz).items():
        merged.setdefault(name, ref)
    return merged


async def fetch_global_emotes(client: httpx.AsyncClient | None) -> dict[str, tuple[str, str]]:
    """Emote name to (id, image URL) of the shared BTTV and FFZ sets."""
    merged: dict[str, tuple[str, str]] = {}
    bttv = await _fetch_json(client, BTTV_GLOBAL_URL)
    for name, ref in parse_bttv(bttv).items():
        merged.setdefault(name, ref)
    ffz = await _fetch_json(client, FFZ_GLOBAL_URL)
    for name, ref in parse_ffz(ffz).items():
        merged.setdefault(name, ref)
    return merged


def find_words(text: str, names: dict[str, tuple[str, str]]) -> list[tuple[int, int, str, str, str]]:
    """Third-party emotes in ``text`` as (start, end, id, name, URL).

    Whole whitespace-separated words only. Positions are relative to
    ``text``; the caller offsets them into the message body.
    """
    if not names:
        return []
    out: list[tuple[int, int, str, str, str]] = []
    for match in re.finditer(r"\S+", text):
        word = match.group(0)
        ref = names.get(word)
        if ref is None:
            continue
        pid, url = ref
        out.append((match.start(), match.end(), pid, word, url))
    return out


async def download_images(
    client: httpx.AsyncClient | None,
    items: dict[str, str],
    *,
    max_emotes: int = MAX_EMOTES_PER_RECORDING,
    max_bytes_each: int = MAX_EMOTE_BYTES,
    max_total_bytes: int = MAX_EMOTE_TOTAL_BYTES,
) -> dict[str, bytes]:
    """Download emote images for {id: image URL}, up to the given limits.

    A failed download is skipped, so the returned dict can be partial. The
    function stops at max_emotes ids, drops one image larger than
    max_bytes_each, and returns at most max_total_bytes of image data.
    """
    out: dict[str, bytes] = {}
    selected = list(items)[:max_emotes]
    if not selected:
        return out
    http, own = _own_client(client)
    sem = asyncio.Semaphore(_EMOTE_FETCH_CONCURRENCY)
    total = 0

    async def one(eid: str) -> None:
        nonlocal total
        async with sem:
            if total >= max_total_bytes:
                return
            try:
                async with http.stream("GET", items[eid]) as resp:
                    resp.raise_for_status()
                    # An error page or JSON error body is 200 and small, but
                    # it is not an image. Reject it here and keep the text
                    # token instead. A response with no content type passes.
                    content_type = resp.headers.get("content-type", "")
                    if content_type and not content_type.lower().startswith("image/"):
                        logger.warning("[emotes] emote %s skipped: content type %r is not an image", eid, content_type)
                        return
                    body = bytearray()
                    rejected = ""
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        # A sibling download fills the total while this one
                        # waits for its headers or its body, so both limits
                        # are tested against the live total: the check at the
                        # permit is not enough on its own.
                        if len(body) > max_bytes_each:
                            rejected = f"image larger than {max_bytes_each} bytes"
                            break
                        if total + len(body) > max_total_bytes:
                            rejected = "total download limit reached"
                            break
                    if rejected:
                        logger.warning("[emotes] emote %s skipped: %s", eid, rejected)
                    elif not body:
                        # A 200 with an empty body carries no image. Storing
                        # it would embed an empty base64 value.
                        logger.warning("[emotes] emote %s skipped: empty body", eid)
                    else:
                        total += len(body)
                        out[eid] = bytes(body)
            except Exception as e:
                logger.warning("[emotes] emote %s download failed: %s", eid, e)

    try:
        await asyncio.gather(*(one(i) for i in selected))
    finally:
        if own:
            await http.aclose()
    if total > max_total_bytes:
        # Up to _EMOTE_FETCH_CONCURRENCY requests can be in flight when the
        # limit is reached. Drop the downloads in first-use order, so the
        # same recording always keeps the same emote images.
        for eid in selected:
            if total <= max_total_bytes:
                break
            dropped = out.pop(eid, None)
            if dropped is not None:
                total -= len(dropped)
    return out


def build_first_party(
    images: dict[str, bytes], names: dict[str, str], *, max_total_bytes: int = MAX_EMOTE_TOTAL_BYTES
) -> list[dict[str, Any]]:
    """ChatRoot firstParty entries for {id: image bytes} with {id: name}."""
    import base64

    first_party: list[dict[str, Any]] = []
    encoded_total = 0
    skipped = 0
    for eid, image in images.items():
        encoded = base64.b64encode(image)
        if encoded_total + len(encoded) > max_total_bytes:
            skipped += 1
            continue
        encoded_total += len(encoded)
        first_party.append({"id": eid, "imageScale": 2, "data": encoded.decode("ascii"), "name": names.get(eid, eid)})
    if skipped:
        logger.warning(
            "[emotes] embeddedData limit reached (%d bytes); %d emote image(s) stay as text",
            max_total_bytes,
            skipped,
        )
    return first_party


async def embed_images(
    client: httpx.AsyncClient | None,
    items: dict[str, tuple[str, str]],
    *,
    max_total_bytes: int = MAX_EMOTE_TOTAL_BYTES,
) -> dict[str, Any] | None:
    """Download {id: (name, image URL)} and build the embeddedData block.

    Returns None when no image is available, so the caller still writes
    a complete file and renderers fall back to text or their CDN.
    """
    if not items:
        return None
    images = await download_images(client, {eid: url for eid, (_name, url) in items.items()})
    if not images:
        return None
    first_party = build_first_party(
        images, {eid: name for eid, (name, _url) in items.items() if eid in images}, max_total_bytes=max_total_bytes
    )
    return {"firstParty": first_party} if first_party else None


def sniff_mime(data: bytes) -> str | None:
    """Image MIME of raw bytes by magic number, or None when unknown."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None
