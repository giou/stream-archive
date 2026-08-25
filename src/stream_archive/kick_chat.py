"""Kick chat -> TwitchDownloader ChatRoot conversion, with embedded emote images.

TwitchDownloader renders chat emoticons from ``message.fragments[].emoticon``
and resolves their artwork from ``embeddedData.firstParty`` (base64 image
bytes keyed by emote id) before it falls back to Twitch's CDN. This module:

- maps every kick message to a ChatRoot comment (sender, badges, colors,
  timestamps, reply offsets),
- splits the body into fragments so each ``[emote:<id>:<name>]`` token
  becomes an emoticon reference,
- downloads the kick emote images (files.kick.com/emotes/<id>/fullsize) and
  embeds them as base64, so TwitchDownloader renders them offline without
  contacting Twitch's CDN.

Unicode emojis are literal characters in the body and survive untouched. If
the app cannot download an image for an emote, it leaves the text token in
place. TwitchDownloader then renders plain text there, never a broken image.
"""

import asyncio
import base64
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

EMOTE_URL = "https://files.kick.com/emotes/{id}/fullsize"
_EMOTE_TOKEN_RE = re.compile(r"^\[emote:\d+:(.+)\]$")
_EMOTE_FIND_RE = re.compile(r"\[emote:(\d+):([^\]\[]+)\]")
_EMOTE_FETCH_CONCURRENCY = 8


def _parse_time(value: Any) -> datetime | None:
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
    if not parts:
        parts.append({"text": content})
    return parts


def _emote_name(fragment_text: str | None, emote_id: str) -> str:
    m = _EMOTE_TOKEN_RE.match(fragment_text or "")
    return m.group(1) if m else str(emote_id)


def build_chat_root(
    channel: str,
    slug: str,
    title: str | None,
    started_wall: str | None,
    messages: list[dict[str, Any]],
    duration_s: float = 0.0,
) -> dict[str, Any]:
    """Build a TwitchDownloader ChatRoot dict from normalized kick messages.

    Emote images are not included here. Call embed_kick_emotes() afterwards.
    """
    start = _parse_time(started_wall)
    video_id = f"kick-{slug}-{int(start.timestamp()) if start else 0}"
    now_iso = datetime.now(UTC).isoformat()

    streamer_id = None
    broadcaster_username = slug
    for m in messages:
        bc = m.get("broadcaster") or {}
        if bc.get("user_id") is not None:
            streamer_id = bc["user_id"]
            broadcaster_username = bc.get("username") or slug
            break

    comments = []
    for m in messages:
        sender = m.get("sender") or {}
        created_at = m.get("created_at")
        msg_time = _parse_time(created_at)
        offset = 0.0
        if msg_time and start:
            offset = max(0.0, (msg_time - start).total_seconds())

        user_badges = []
        emoticons = []
        for b in m.get("badges") or []:
            user_badges.append(
                {
                    "_id": b.get("type") or "subscriber",
                    "version": str(b.get("count") or 1),
                }
            )
        content = m.get("content") or ""
        for t in _EMOTE_FIND_RE.finditer(content):
            emoticons.append(
                {
                    "_id": t.group(1),
                    "begin": t.start(),
                    "end": t.end(),
                }
            )

        comment = {
            "_id": m.get("message_id") or f"{sender.get('user_id')}-{created_at}",
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
        comments.append(comment)

    streamer = {"name": broadcaster_username, "login": slug}
    if streamer_id is not None:
        streamer["id"] = streamer_id

    video = {
        "title": title or "",
        "id": video_id,
        "start": 0.0,
        "end": round(duration_s, 3),
        "length": round(duration_s, 3),
    }
    if start:
        video["created_at"] = started_wall

    return {
        "FileInfo": {
            "Version": {"Major": 1, "Minor": 4, "Patch": 0},
            "CreatedAt": now_iso,
            "UpdatedAt": now_iso,
        },
        "streamer": streamer,
        "video": video,
        "comments": comments,
    }


def collect_emote_ids(root: dict[str, Any]) -> list[str]:
    """Unique emote ids referenced by fragments, in first-use order."""
    seen: set[str] = set()
    out: list[str] = []
    for c in root.get("comments") or []:
        for f in c.get("message", {}).get("fragments") or []:
            eid = f.get("emoticon", {}).get("emoticon_id")
            if eid and eid not in seen:
                seen.add(eid)
                out.append(eid)
    return out


async def fetch_emote_images(ids: list[str], client: httpx.AsyncClient | None = None) -> dict[str, bytes]:
    """Download kick emote images.

    A failed download is skipped, so the returned dict can be partial.
    """
    out: dict[str, bytes] = {}
    if not ids:
        return out
    own = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5))
    sem = asyncio.Semaphore(_EMOTE_FETCH_CONCURRENCY)

    async def one(eid: str) -> None:
        async with sem:
            try:
                resp = await http.get(EMOTE_URL.format(id=eid))
                resp.raise_for_status()
                out[eid] = resp.content
            except Exception as e:
                logger.warning("[kick_chat] emote %s download failed: %s", eid, e)

    try:
        await asyncio.gather(*(one(i) for i in ids))
    finally:
        if own:
            await http.aclose()
    return out


def embed_emotes(root: dict[str, Any], images: dict[str, bytes]) -> None:
    """Fill embeddedData.firstParty with base64 emote bytes keyed by emote id."""
    if not images:
        return
    first_party: list[dict[str, Any]] = []
    for c in root.get("comments") or []:
        for f in c.get("message", {}).get("fragments") or []:
            emote = f.get("emoticon") or {}
            eid = emote.get("emoticon_id")
            if not isinstance(eid, str):
                continue
            data = images.get(eid)
            if data is None or any(x["id"] == eid for x in first_party):
                continue
            first_party.append(
                {
                    "id": eid,
                    "imageScale": 2,
                    "data": base64.b64encode(data).decode("ascii"),
                    "name": _emote_name(f.get("text"), eid),
                }
            )
    if first_party:
        root["embeddedData"] = {"firstParty": first_party}
        logger.info("[kick_chat] embedded %d emote image(s)", len(first_party))


async def embed_kick_emotes(root: dict[str, Any], client: httpx.AsyncClient | None = None) -> None:
    """Download and embed all emotes referenced by a ChatRoot. Never raises."""
    try:
        images = await fetch_emote_images(collect_emote_ids(root), client)
        embed_emotes(root, images)
    except Exception as e:
        logger.error("[kick_chat] emote embedding failed: %s", e)
