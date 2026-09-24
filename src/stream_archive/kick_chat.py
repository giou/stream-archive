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

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from stream_archive.chat_writer import file_info
from stream_archive.emotes import (
    MAX_EMOTE_BYTES,
    MAX_EMOTE_TOTAL_BYTES,
    MAX_EMOTES_PER_RECORDING,
    download_images,
    embed_images,
    find_words,
)

logger = logging.getLogger(__name__)

EMOTE_URL = "https://files.kick.com/emotes/{id}/fullsize"
_EMOTE_FIND_RE = re.compile(r"\[emote:(\d+):([^\]\[]+)\]")


def parse_time(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, or return None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except TypeError, ValueError:
        return None


def _fragments(
    content: str, third_party: dict[str, tuple[str, str]] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split body into ChatRoot fragments plus third-party emoticon entries.

    Emote tokens are self-describing ("[emote:<id>:<name>]"), so the parser
    derives fragments by scanning the body itself. Kick's webhook "emotes"
    positions are not used for splitting. Live data shows that these positions
    are frequently absent or inconsistent with the actual body (offsets past
    the string length), while the token text is always exact. Plain words
    matching the third-party set become ``provider:id`` references when the
    set is given. Positions are absolute in ``content``.
    """
    if not content:
        return [{"text": ""}], []
    parts: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    pos = 0
    for m in _EMOTE_FIND_RE.finditer(content):
        if m.start() > pos:
            _split_plain(parts, extra, content, pos, m.start(), third_party)
        parts.append(
            {
                "text": m.group(0),
                "emoticon": {"emoticon_id": m.group(1)},
            }
        )
        pos = m.end()
    if pos < len(content):
        _split_plain(parts, extra, content, pos, len(content), third_party)
    return parts, extra


def _split_plain(
    parts: list[dict[str, Any]],
    extra: list[dict[str, Any]],
    content: str,
    begin: int,
    end: int,
    third_party: dict[str, tuple[str, str]] | None,
) -> None:
    """Append the plain span of [begin, end) plus its third-party word fragments."""
    text = content[begin:end]
    if not third_party:
        parts.append({"text": text})
        return
    pos = 0
    for start, finish, pid, _name, _url in find_words(text, third_party):
        if start > pos:
            parts.append({"text": text[pos:start]})
        parts.append({"text": text[start:finish], "emoticon": {"emoticon_id": pid}})
        extra.append({"_id": pid, "begin": begin + start, "end": begin + finish})
        pos = finish
    if pos < len(text):
        parts.append({"text": text[pos:]})


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
    third_party: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Map one normalized kick message to a ChatRoot comment."""
    sender = message.get("sender") or {}
    created_at = message.get("created_at")
    msg_time = parse_time(created_at)
    offset = 0.0
    if msg_time and start:
        # Kick can send an offset-less timestamp, and the recording start can
        # be offset-less too. Both sides are UTC then, so the subtraction is
        # defined and a naive value is not read as local time.
        if msg_time.tzinfo is None:
            msg_time = msg_time.replace(tzinfo=UTC)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
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
    fragments, word_emos = _fragments(content, third_party)
    emoticons = [
        {
            "_id": t.group(1),
            "begin": t.start(),
            "end": t.end(),
        }
        for t in _EMOTE_FIND_RE.finditer(content)
    ]
    emoticons.extend(word_emos)
    emoticons.sort(key=lambda e: (e["begin"], e["end"]))

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
            "fragments": fragments,
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
    also drops an empty body and a body whose content type is not an image.
    """
    return await download_images(
        client,
        {i: EMOTE_URL.format(id=i) for i in ids},
        max_emotes=max_emotes,
        max_bytes_each=max_bytes_each,
        max_total_bytes=max_total_bytes,
    )


async def embedded_data(emote_names: dict[str, str], client: httpx.AsyncClient | None = None) -> dict[str, Any] | None:
    """Download the emote images and build the ChatRoot embeddedData block.

    The method never raises. It returns None when no image is available, so
    the caller still writes a complete file and TwitchDownloader renders the
    text token instead.
    """
    if not emote_names:
        return None
    try:
        embedded = await embed_images(
            client,
            {eid: (name, EMOTE_URL.format(id=eid)) for eid, name in emote_names.items()},
            max_total_bytes=MAX_EMOTE_TOTAL_BYTES,
        )
    except Exception as e:
        logger.error("[kick_chat] emote embedding failed: %s", e)
        return None
    if embedded is not None:
        logger.info("[kick_chat] embedded %d emote image(s)", len(embedded["firstParty"]))
    return embedded
