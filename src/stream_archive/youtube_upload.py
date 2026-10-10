"""YouTube VOD upload: gates, metadata, and the resumable protocol.

Live restreams own ``YouTubeStreamer``. This module owns file uploads
through ``videos.insert``. The flow follows the resumable protocol: a POST
to ``/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status``
with the video resource answers a session URI in the ``Location`` header,
then chunked PUTs fill it. A finished upload answers the video resource
with its id. References: ``docs/videos/insert`` and
``guides/using_resumable_upload_protocol`` of the YouTube Data API v3.
"""

import inspect
import logging
from pathlib import Path
from typing import Any, Protocol

from stream_archive.config import AppConfig
from stream_archive.recorder.common import sanitize_metadata_text

logger = logging.getLogger(__name__)

#: Session host for the resumable protocol. It differs from the Data API host.
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3/videos"

#: Upload chunk size: 32 MiB, a multiple of the 256 KiB protocol quantum.
#: One chunk fills a fast uplink between round trips: small chunks cap
#: throughput on round-trip overhead instead of bandwidth.
UPLOAD_CHUNK_BYTES = 32 * 1024 * 1024

#: Largest file the API takes, per docs/videos/insert: 256 GB.
MAX_VOD_BYTES = 256 * 1024 * 1024 * 1024

#: Default video category: 22 (People & Blogs), like the upload sample.
VOD_CATEGORY_ID = "22"

#: Suffix of the sidecar that remembers a YouTube VOD upload per file.
#: Listings only match video suffixes, so this file never surfaces as a
#: recording. Deleting the recording drops it (see _remove_if_inactive).
YOUTUBE_SIDECAR_SUFFIX = ".youtube.json"

#: Retries for one chunk after a 5xx answer or a transport error, with
#: exponential backoff. Matches the backoff policy of the upload sample.
MAX_CHUNK_RETRIES = 10

#: Answers that allow a retry of the same chunk.
RETRIABLE_STATUS_CODES = (500, 502, 503, 504)


class ProgressCallback(Protocol):
    """Upload progress: bytes sent over bytes total, plus a phase note.

    The note names the phase (for example "upload"). A plain two-arg
    callable also satisfies this protocol.
    """

    def __call__(self, sent: int, total: int, note: str | None = None) -> Any: ...


def report_upload_progress(progress: ProgressCallback | None, sent: int, total: int, note: str | None = None) -> None:
    """Call ``progress`` with a phase note, tolerating two-arg callables."""
    if progress is None:
        return
    if note is None:
        progress(sent, total)
        return
    try:
        takes_note = len(inspect.signature(progress).parameters) >= 3
    except TypeError, ValueError:
        takes_note = True
    # Never retry on TypeError: an error inside the callback must reach
    # the caller instead of running the callback twice with fewer args.
    if takes_note:
        progress(sent, total, note)
    else:
        progress(sent, total)


def check_uploadable(path: Path) -> tuple[bool, str]:
    """True plus an empty note when ``path`` can go to YouTube.

    The checks run in size order: missing file, empty file, then the
    256 GB API cap. The note names the failed check.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False, "the file is gone"
    except OSError as e:
        return False, f"the file is unreadable ({e.strerror or e})"
    if size <= 0:
        return False, "the file is empty"
    if size > MAX_VOD_BYTES:
        return False, "the file is over the 256 GB YouTube cap"
    return True, ""


def youtube_available(config: AppConfig) -> bool:
    """True when a YouTube token file exists, so an upload can run.

    The setup command writes the token. A missing token means the operator
    never authenticated, and every upload would fail with the same error.
    """
    try:
        return (config.workdir / "youtube_token.json").exists()
    except RuntimeError:
        return False


def upload_title(path: Path) -> str:
    """Video title from the file stem, capped at the 100-char API limit.

    Recording names carry a date stamp, so the stem is already unique.
    """
    raw = sanitize_metadata_text(path.stem, limit=200)
    raw = raw.replace("<", "").replace(">", "")
    return raw[:100] or "Untitled"


def upload_description(path: Path, channel: str | None = None, game: str | None = None) -> str:
    """Video description for ``path``: platform lines when known.

    With a channel tag the text reuses the restream description, so VODs
    and live broadcasts read the same. Without one it names the file.
    """
    if channel:
        from stream_archive.youtube_streamer import build_video_description

        author = channel.split(":", 1)[-1]
        return build_video_description(author, channel, game or "Unknown")
    return f"Recorded by StreamArchive: {path.name}"


def upload_metadata(path: Path, config: AppConfig, channel: str | None = None) -> dict[str, Any]:
    """The ``videos.insert`` resource body for ``path``.

    The title comes from the file name and the privacy from the config,
    per the operator choice. The video is marked not made for kids, like
    the live broadcasts.
    """
    return {
        "snippet": {
            "title": upload_title(path),
            "description": upload_description(path, channel),
            "categoryId": VOD_CATEGORY_ID,
        },
        "status": {
            "privacyStatus": config.youtube.privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }


def watch_url(video_id: str) -> str:
    """Public watch URL of an uploaded video."""
    return f"https://www.youtube.com/watch?v={video_id}"


def youtube_sidecar(path: str | Path) -> Path:
    """Sidecar path that remembers the upload of ``path``."""
    file_path = Path(path)
    return file_path.with_name(file_path.name + YOUTUBE_SIDECAR_SUFFIX)


def read_youtube_url(path: str | Path) -> str | None:
    """Watch URL remembered for ``path``, or None. Never raises."""
    import contextlib
    import json

    try:
        raw = youtube_sidecar(path).read_text(encoding="utf-8")
    except OSError:
        return None
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        url = json.loads(raw).get("youtube_url")
        return url if isinstance(url, str) and url else None
    return None


def write_youtube_url(path: str | Path, url: str) -> None:
    """Remember the upload URL of ``path`` next to the file. Never raises."""
    import contextlib
    import json

    payload = json.dumps({"youtube_url": url})
    with contextlib.suppress(OSError):
        youtube_sidecar(path).write_text(payload, encoding="utf-8")


def drop_youtube_url(path: str | Path) -> None:
    """Forget the upload of ``path``. Never raises."""
    import contextlib

    with contextlib.suppress(OSError):
        youtube_sidecar(path).unlink(missing_ok=True)
