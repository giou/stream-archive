import asyncio
import contextlib
import re
from typing import Any

#: User credentials inside a URL, for example ``scheme://user:pass@host``.
#: The match runs to the LAST "@", because a password can hold a raw "@".
#: A "/", "?" or "#" ends the userinfo, so an "@" in a path or a query
#: string is not a match. The match holds the separator, so the replacement
#: text is ``***@``.
_URL_USERINFO_RE = re.compile(r"(?<=://)[^/\s?#]*@")

#: Stream key of a YouTube ingest URL, for example ``rtmp://host/live2/<key>``.
_INGEST_KEY_RE = re.compile(r"(?<=/live2/)[^\s/?]+")

#: Names that Windows reserves for devices, in any letter case and with any
#: extension. ``CON``, ``CON.ts`` and ``con.txt`` all fail to open.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)


def _close_late_stream(future: asyncio.Future[Any]) -> None:
    """Close a stream that opened after its capture was cancelled.

    A worker thread cannot be stopped, so it returns an open handle long
    after the await raised CancelledError. Attach this callback to the
    executor future on that path. Without it the handle waits for the
    garbage collector, which then reports an open handle.
    """
    if future.cancelled():
        return
    try:
        stream = future.result()
    except Exception:
        return  # the open failed; the caller's error path never sees this future
    with contextlib.suppress(Exception):
        stream.close()


def sanitize_filename(name: str) -> str:
    """Replace the characters that a file system rejects, and cap the length.

    The cap counts UTF-8 bytes, so a CJK or emoji title stays inside the
    255-byte NAME_MAX limit. A control character becomes "_", so a stream
    title cannot break a log line or a path. Windows strips a trailing dot
    or space, and cannot create a reserved device name, so both cases get a
    fix here. An empty name, ".", and ".." name a directory rather than a
    file, so those values fall back to "_".
    """
    safe = re.sub(r"[\x00-\x1f\x7f<>:\"/\\|?*]", "_", name)
    safe = safe.encode("utf-8")[:200].decode("utf-8", errors="ignore")
    safe = safe.rstrip(" .")
    if safe.split(".", 1)[0].upper() in _RESERVED_NAMES:
        safe = "_" + safe
    if safe in ("", ".", ".."):
        return "_"
    return safe


#: Characters that break the line structure of a message, reorder displayed
#: text, or carry no glyph: C0/C1 controls, the line and paragraph
#: separators, and the bidirectional overrides.
_METADATA_BREAKS_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069]")

#: Longest text kept from platform metadata in a message body.
MAX_METADATA_CHARS = 200


def strip_line_breaks(value: str) -> str:
    """Replace the characters that break a line structure, and nothing else.

    Use this for a value that is already bounded and must stay exact, such as
    a file name shown to the operator: collapsing or trimming it would name a
    file that does not exist on disk.
    """
    return _METADATA_BREAKS_RE.sub(" ", value) if value else ""


def sanitize_metadata_text(value: str, limit: int = MAX_METADATA_CHARS) -> str:
    """Make one line of platform metadata safe in a message or a title.

    A streamer controls the stream title and the game name. The Telegram
    notification and the YouTube description are line-structured documents
    the operator reads, so a line break inside a value forges extra lines,
    and a separator such as U+2028 breaks a message exactly like a newline.
    Replace those with a space, drop the control characters, and cap the
    length. This is the same reasoning as ``sanitize_filename``, for text
    that is displayed rather than written to a file.
    """
    if not value:
        return ""
    return " ".join(strip_line_breaks(value).split())[:limit]


def _redact_credentials(text: str) -> str:
    """Replace the credentials of every URL in ``text`` with ``***``.

    Log lines and error texts can carry a proxy URL with a password, or a
    YouTube ingest URL with the stream key. The host stays readable.
    """
    return _INGEST_KEY_RE.sub("***", _URL_USERINFO_RE.sub("***@", text))
