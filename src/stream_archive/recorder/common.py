import re

#: User credentials inside a URL, for example ``scheme://user:pass@host``.
#: A "?" or "#" ends the userinfo, so an "@" in a query string is not a match.
_URL_USERINFO_RE = re.compile(r"(?<=://)[^/@\s?#]+(?=@)")

#: Stream key of a YouTube ingest URL, for example ``rtmp://host/live2/<key>``.
_INGEST_KEY_RE = re.compile(r"(?<=/live2/)[^\s/?]+")


def sanitize_filename(name: str) -> str:
    """Replace the characters that a file system rejects, and cap the length.

    The cap counts UTF-8 bytes, so a CJK or emoji title stays inside the
    255-byte NAME_MAX limit. An empty name, ".", and ".." name a directory
    rather than a file, so those values fall back to "_".
    """
    safe = re.sub(r"[<>:\"/\\|?*]", "_", name)
    safe = safe.encode("utf-8")[:200].decode("utf-8", errors="ignore")
    if safe in ("", ".", ".."):
        return "_"
    return safe


def _redact_credentials(text: str) -> str:
    """Replace the credentials of every URL in ``text`` with ``***``.

    Log lines and error texts can carry a proxy URL with a password, or a
    YouTube ingest URL with the stream key. The host stays readable.
    """
    return _INGEST_KEY_RE.sub("***", _URL_USERINFO_RE.sub("***", text))
