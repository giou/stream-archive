"""Kick chat -> TwitchDownloader ChatRoot conversion.

TwitchDownloader renders chat emoticons from ``message.fragments[].emoticon``
and resolves their artwork from ``embeddedData.firstParty`` (base64 image
bytes keyed by emote id) before it falls back to Twitch's CDN. This module:

- maps one kick message to a ChatRoot comment (sender, badges, colors,
  timestamps, reply offsets),
- splits the body into fragments so each ``[emote:<id>:<name>]`` token
  becomes an emoticon reference,
- collects the emote names the recorder embeds (``EMOTE_URL`` maps an id
  to its image, and the recorder calls ``emotes.embed_images`` itself).

The recorder writes the comments while the recording runs. The name table
holds 1024 distinct emote ids per recording. An over-limit emote keeps its
text token, so TwitchDownloader renders plain text there, never a broken
image.
"""

import re
from datetime import UTC, datetime
from typing import Any

from stream_archive.chat_writer import file_info
from stream_archive.emotes import MAX_EMOTES_PER_RECORDING, find_words

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
