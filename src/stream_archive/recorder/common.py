import re

#: User credentials inside a URL, for example ``scheme://user:pass@host``.
_URL_USERINFO_RE = re.compile(r"(?<=://)[^/@\s]+(?=@)")

#: Stream key of a YouTube ingest URL, for example ``rtmp://host/live2/<key>``.
_INGEST_KEY_RE = re.compile(r"(?<=/live2/)[^\s/?]+")


def sanitize_filename(name: str) -> str:
    return re.sub(r"[<>:\"/\\|?*]", "_", name)[:200]


def _redact_credentials(text: str) -> str:
    """Replace the credentials of every URL in ``text`` with ``***``.

    Log lines and error texts can carry a proxy URL with a password, or a
    YouTube ingest URL with the stream key. The host stays readable.
    """
    return _INGEST_KEY_RE.sub("***", _URL_USERINFO_RE.sub("***", text))
