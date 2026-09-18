"""Kick chat -> TwitchDownloader ChatRoot conversion, with embedded emote images.

TwitchDownloader renders chat emoticons from ``message.fragments[].emoticon``
and resolves their artwork from ``embeddedData.firstParty`` (base64 image
bytes keyed by emote id) before it falls back to Twitch's CDN. This module:

- maps one kick message to a ChatRoot comment (sender, badges, colors,
  timestamps, reply offsets),
- splits the body into fragments so each ``[emote:<id>:<name>]`` token
  becomes an emoticon reference,
- downloads the kick emote images (files.kick.com/emotes/<id>/fullsize) and
  builds the embeddedData block, so TwitchDownloader renders them offline
  without contacting Twitch's CDN.

The recorder writes the comments while the recording runs. The limits in this
module bound the emote work for one recording: 1024 distinct emote ids, 512
KiB for one image, 16 MiB for all images, and 16 MiB for the base64 text the
chat file embeds. An over-limit emote keeps its text token, so
TwitchDownloader renders plain text there, never a broken image.
"""

import asyncio
import base64
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from stream_archive.chat_writer import file_info

logger = logging.getLogger(__name__)

EMOTE_URL = "https://files.kick.com/emotes/{id}/fullsize"
_EMOTE_FIND_RE = re.compile(r"\[emote:(\d+):([^\]\[]+)\]")
_EMOTE_FETCH_CONCURRENCY = 8

#: Distinct emote ids fetched for one recording.
MAX_EMOTES_PER_RECORDING = 1024
#: Largest image accepted for one emote.
MAX_EMOTE_BYTES = 512 * 1024
#: Largest total download for one recording. The same value bounds the
#: base64 text that the chat file embeds, because that text is held in
#: memory while the trailer is written.
MAX_EMOTE_TOTAL_BYTES = 16 * 1024 * 1024


def parse_time(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, or return None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError, AttributeError:
        return None


def _fragments(content: str) -> list[dict[str, Any]]:
    """Split body into ChatRoot fragments. Each emote becomes an emoticon reference.

    Emote tokens are self-describing ("[emote:<id>:<name>]"), so the parser
    derives fragments by scanning the body itself. Kick's webhook "emotes"
    positions are not used for splitting. Live data shows that these positions
    are frequently absent or inconsistent with the actual body (offsets past
    the string length), while the token text is always exact.
    """
    if not content:
        return [{"text": ""}]
    parts: list[dict[str, Any]] = []
    pos = 0
    for m in _EMOTE_FIND_RE.finditer(content):
        if m.start() > pos:
            parts.append({"text": content[pos : m.start()]})
        parts.append(
            {
                "text": m.group(0),
                "emoticon": {"emoticon_id": m.group(1)},
            }
        )
        pos = m.end()
    if pos < len(content):
        parts.append({"text": content[pos:]})
    return parts


def video_id_for(slug: str, start: datetime | None) -> str:
    """ChatRoot video id for a kick recording, from its start time."""
    return f"kick-{slug}-{int(start.timestamp()) if start else 0}"


def streamer_identity(message: dict[str, Any], slug: str) -> tuple[int | None, str]:
    """Return the (user_id, username) of the broadcaster, or (None, slug)."""
    broadcaster = message.get("broadcaster") or {}
    if broadcaster.get("user_id") is not None:
        return broadcaster["user_id"], broadcaster.get("username") or slug
    return None, slug


def collect_emote_names(names: dict[str, str], content: str) -> int:
    """Add the emote ids of one message to names, in first-use order.

    The dict keeps the name of the first token that uses each id. The function
    returns the number of token occurrences that the size limit rejected.
    """
    skipped = 0
    for m in _EMOTE_FIND_RE.finditer(content or ""):
        emote_id = m.group(1)
        if emote_id in names:
            continue
        if len(names) >= MAX_EMOTES_PER_RECORDING:
            skipped += 1
            continue
        names[emote_id] = m.group(2)
    return skipped


def build_comment(
    message: dict[str, Any],
    streamer_id: int | None,
    video_id: str,
    start: datetime | None,
) -> dict[str, Any]:
    """Map one normalized kick message to a ChatRoot comment."""
    sender = message.get("sender") or {}
    created_at = message.get("created_at")
    msg_time = parse_time(created_at)
    offset = 0.0
    if msg_time and start:
        if msg_time.tzinfo is None:
            # Kick can send an offset-less timestamp. datetime.fromisoformat
            # returns a naive datetime then, and the subtraction below would
            # raise TypeError and lose the message.
            msg_time = msg_time.replace(tzinfo=UTC)
        offset = max(0.0, (msg_time - start).total_seconds())

    user_badges = []
    for b in message.get("badges") or []:
        user_badges.append(
            {
                "_id": b.get("type") or "subscriber",
                "version": str(b.get("count") or 1),
            }
        )
    content = message.get("content") or ""
    emoticons = [
        {
            "_id": t.group(1),
            "begin": t.start(),
            "end": t.end(),
        }
        for t in _EMOTE_FIND_RE.finditer(content)
    ]

    comment = {
        "_id": message.get("message_id") or f"{sender.get('user_id')}-{created_at}",
        "channel_id": str(streamer_id) if streamer_id is not None else "",
        "content_type": "video",
        "content_id": video_id,
        "content_offset_seconds": round(offset, 3),
        "commenter": {
            "display_name": sender.get("username") or "anonymous",
            "_id": str(sender.get("user_id")) if sender.get("user_id") is not None else "",
            "name": sender.get("username") or "anonymous",
            "bio": "",
            # created_at/updated_at are intentionally omitted. TD maps
            # these fields to non-nullable DateTime values, and an empty
            # string fails deserialization.
            "logo": sender.get("profile_picture") or "",
        },
        "message": {
            "body": content,
            "bits_spent": 0,
            "fragments": _fragments(content),
            "user_badges": user_badges,
            "user_color": sender.get("username_color") or "",
            "emoticons": emoticons,
        },
    }
    if msg_time:
        comment["created_at"] = created_at
    return comment


def chat_root_trailer(
    slug: str,
    title: str | None,
    started_wall: str | None,
    start: datetime | None,
    duration_s: float,
    streamer_id: int | None,
    streamer_username: str,
) -> dict[str, Any]:
    """Return the ChatRoot keys that follow the comments array."""
    streamer: dict[str, Any] = {"name": streamer_username, "login": slug}
    if streamer_id is not None:
        streamer["id"] = streamer_id

    video: dict[str, Any] = {
        "title": title or "",
        "id": video_id_for(slug, start),
        "start": 0.0,
        "end": round(duration_s, 3),
        "length": round(duration_s, 3),
    }
    if start and started_wall:
        # TwitchDownloader maps created_at to a non-nullable DateTime, so
        # the key needs a value.
        video["created_at"] = started_wall

    return {"FileInfo": file_info(), "streamer": streamer, "video": video}


async def fetch_emote_images(
    ids: list[str],
    client: httpx.AsyncClient | None = None,
    *,
    max_emotes: int = MAX_EMOTES_PER_RECORDING,
    max_bytes_each: int = MAX_EMOTE_BYTES,
    max_total_bytes: int = MAX_EMOTE_TOTAL_BYTES,
) -> dict[str, bytes]:
    """Download kick emote images, up to the given limits.

    A failed download is skipped, so the returned dict can be partial. The
    function stops at max_emotes ids, drops one image larger than
    max_bytes_each, and returns at most max_total_bytes of image data. It
    also drops a body whose content type is not an image.
    """
    out: dict[str, bytes] = {}
    selected = ids[:max_emotes]
    if len(ids) > max_emotes:
        logger.warning("[kick_chat] emote limit reached: fetching %d of %d ids", max_emotes, len(ids))
    if not selected:
        return out
    own = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5))
    sem = asyncio.Semaphore(_EMOTE_FETCH_CONCURRENCY)
    total = 0

    async def one(eid: str) -> None:
        nonlocal total
        async with sem:
            if total >= max_total_bytes:
                return
            try:
                async with http.stream("GET", EMOTE_URL.format(id=eid)) as resp:
                    resp.raise_for_status()
                    # An error page or JSON error body is 200 and small, but it
                    # is not an image. TwitchDownloader cannot decode it. Reject
                    # it here and keep the text token instead. A response with
                    # no content type passes.
                    content_type = resp.headers.get("content-type", "")
                    if content_type and not content_type.lower().startswith("image/"):
                        logger.warning(
                            "[kick_chat] emote %s skipped: content type %r is not an image", eid, content_type
                        )
                        return
                    body = bytearray()
                    oversized = False
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes_each:
                            oversized = True
                            break
                    if oversized:
                        logger.warning("[kick_chat] emote %s skipped: image larger than %d bytes", eid, max_bytes_each)
                    else:
                        total += len(body)
                        out[eid] = bytes(body)
            except Exception as e:
                logger.warning("[kick_chat] emote %s download failed: %s", eid, e)

    try:
        await asyncio.gather(*(one(i) for i in selected))
    finally:
        if own:
            await http.aclose()
    if total > max_total_bytes:
        # Up to _EMOTE_FETCH_CONCURRENCY requests can be in flight when the
        # limit is reached. Drop downloads until the result fits the limit.
        for eid in list(out):
            if total <= max_total_bytes:
                break
            total -= len(out.pop(eid))
    return out


async def embedded_data(emote_names: dict[str, str], client: httpx.AsyncClient | None = None) -> dict[str, Any] | None:
    """Download the emote images and build the ChatRoot embeddedData block.

    The method never raises. It returns None when no image is available, so
    the caller still writes a complete file and TwitchDownloader renders the
    text token instead.
    """
    if not emote_names:
        return None
    try:
        images = await fetch_emote_images(list(emote_names), client)
    except Exception as e:
        logger.error("[kick_chat] emote embedding failed: %s", e)
        return None
    if not images:
        return None
    # The embedded block is held as text while the trailer is written, so it
    # is bounded separately from the download cap: base64 grows the data by a
    # third, and the whole block is serialized into the file.
    first_party: list[dict[str, Any]] = []
    encoded_total = 0
    skipped = 0
    for eid in emote_names:
        image = images.get(eid)
        if image is None:
            continue
        encoded = base64.b64encode(image)
        if encoded_total + len(encoded) > MAX_EMOTE_TOTAL_BYTES:
            skipped += 1
            continue
        encoded_total += len(encoded)
        first_party.append(
            {
                "id": eid,
                "imageScale": 2,
                "data": encoded.decode("ascii"),
                "name": emote_names.get(eid, eid),
            }
        )
    if skipped:
        logger.warning(
            "[kick_chat] embeddedData limit reached (%d bytes); %d emote image(s) stay as text",
            MAX_EMOTE_TOTAL_BYTES,
            skipped,
        )
    if not first_party:
        return None
    logger.info("[kick_chat] embedded %d emote image(s), %d bytes of encoded data", len(first_party), encoded_total)
    return {"firstParty": first_party}
