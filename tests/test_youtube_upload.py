"""VOD upload to YouTube: metadata, gates, and the resumable flow."""

import asyncio
import json
from pathlib import Path

import pytest
from conftest import make_config as valid_config

from stream_archive import youtube_upload
from stream_archive.youtube_streamer import YouTubeStreamer
from stream_archive.youtube_upload import (
    check_uploadable,
    upload_description,
    upload_metadata,
    upload_title,
    watch_url,
    youtube_available,
)


def make_config(tmp_path, **overrides):
    cfg = valid_config(channels=["twitch:ch"], recording_dir=str(tmp_path), **overrides)
    cfg._workdir = tmp_path
    cfg._config_path = tmp_path / "config.json"
    return cfg


def test_upload_title_from_stem_capped():
    """The title comes from the file stem and never exceeds 100 chars."""
    path = Path("stream title " + "x" * 200 + "-01_01_2026-120000.mp4")
    title = upload_title(path)
    assert title.startswith("stream title")
    assert len(title) <= 100
    assert "<" not in title and ">" not in title


def test_upload_metadata_privacy_and_category(tmp_path):
    """Privacy follows the config and the category defaults to 22."""
    cfg = make_config(tmp_path, youtube={"privacy_status": "private"})
    path = Path("show-01_01_2026-120000.mp4")
    body = upload_metadata(path, cfg, channel="twitch:ch")
    assert body["snippet"]["categoryId"] == "22"
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert "show-01_01_2026-120000" in body["snippet"]["title"]


def test_upload_description_channel_lines():
    """A known channel reuses the restream description lines."""
    text = upload_description(Path("f.mp4"), channel="twitch:ch")
    assert text.split("\n") == [
        "Twitch stream by ch",
        "Game: Unknown",
        "Originally streamed at: https://twitch.tv/ch",
        "Recorded by StreamArchive",
    ]


def test_watch_url():
    assert watch_url("abc123") == "https://www.youtube.com/watch?v=abc123"


def test_sidecar_round_trip(tmp_path):
    """The remembered URL survives as a sidecar: write, read, drop."""
    from stream_archive.youtube_upload import (
        drop_youtube_url,
        read_youtube_url,
        write_youtube_url,
        youtube_sidecar,
    )

    target = tmp_path / "show.mp4"
    target.write_bytes(b"x")
    assert read_youtube_url(target) is None
    write_youtube_url(target, "https://www.youtube.com/watch?v=vid1")
    assert youtube_sidecar(target).exists()
    assert read_youtube_url(target) == "https://www.youtube.com/watch?v=vid1"
    drop_youtube_url(target)
    assert read_youtube_url(target) is None


def test_sidecar_garbage_reads_none(tmp_path):
    """A corrupt sidecar reads as no URL, never raises."""
    from stream_archive.youtube_upload import read_youtube_url, youtube_sidecar

    target = tmp_path / "show.mp4"
    target.write_bytes(b"x")
    youtube_sidecar(target).write_text("not json", encoding="utf-8")
    assert read_youtube_url(target) is None
    youtube_sidecar(target).write_text('{"youtube_url": 42}', encoding="utf-8")
    assert read_youtube_url(target) is None


def test_check_uploadable_gates(tmp_path, monkeypatch):
    """Missing, empty, over-cap, and valid files each get their answer."""
    assert check_uploadable(tmp_path / "gone.mp4") == (False, "the file is gone")
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert check_uploadable(empty) == (False, "the file is empty")
    small = tmp_path / "small.mp4"
    small.write_bytes(b"data")
    assert check_uploadable(small) == (True, "")
    monkeypatch.setattr(youtube_upload, "MAX_VOD_BYTES", 3)
    ok, note = check_uploadable(small)
    assert ok is False
    assert "256 GB" in note


def test_youtube_available_needs_token(tmp_path):
    """No token file means no YouTube button and no upload attempt."""
    cfg = make_config(tmp_path)
    assert youtube_available(cfg) is False
    (tmp_path / "youtube_token.json").write_text(json.dumps({"refresh_token": "rt"}))
    assert youtube_available(cfg) is True


class FakeCreds:
    def __init__(self):
        self.valid = True
        self.token = "access-token"
        self.refresh_calls = 0

    async def refresh_call(self):
        return None


class UploadClient:
    """Upload-host double: session POST plus scripted chunk PUTs."""

    def __init__(self, puts, posts=None):
        self.posts = []
        self.puts = []
        self._puts = list(puts)
        self._posts = list(posts) if posts else []
        self.refresh_calls = 0

    async def post(self, url, **kwargs):
        # Snapshot the headers: the streamer mutates the dict on a 401
        # retry, and the record must show what each attempt actually sent.
        self.posts.append((url, {**kwargs, "headers": dict(kwargs.get("headers", {}))}))
        if self._posts:
            status, headers, payload = self._posts.pop(0)
            return _Response(status, headers, payload)
        return _Response(200, {"location": "https://upload/session/1"}, {})

    async def put(self, url, **kwargs):
        self.puts.append((url, {**kwargs, "headers": dict(kwargs.get("headers", {}))}))
        item = self._puts.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, headers, payload = item
        return _Response(status, headers, payload)

    async def aclose(self):
        return None


class _Response:
    def __init__(self, status_code, headers, payload):
        self.status_code = status_code
        self.headers = headers
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            msg = "upload request failed"
            raise httpx.HTTPStatusError(msg, request=None, response=None)  # type: ignore[arg-type]


def make_streamer(tmp_path, client):
    cfg = make_config(tmp_path)
    streamer = YouTubeStreamer(cfg)

    async def fake_creds(refresh=False):
        if refresh:
            client.refresh_calls += 1
        return FakeCreds()

    streamer._get_credentials = fake_creds  # type: ignore[method-assign]
    streamer._client = client
    return streamer


def write_big(tmp_path, name="show-01_01_2026-120000.mp4", extra=100):
    """A file just over one 8 MiB chunk, so the flow sends two PUTs."""
    from stream_archive.youtube_upload import UPLOAD_CHUNK_BYTES

    path = tmp_path / name
    with open(path, "wb") as f:
        f.write(b"v" * UPLOAD_CHUNK_BYTES)
        f.write(b"v" * extra)
    return path


def test_upload_video_file_two_chunks(tmp_path):
    """Session POST plus two ranged PUTs return the video id and URL."""
    from stream_archive.youtube_upload import UPLOAD_CHUNK_BYTES

    path = write_big(tmp_path)
    size = path.stat().st_size
    first_end = UPLOAD_CHUNK_BYTES - 1
    client = UploadClient(
        [
            (308, {"range": f"bytes=0-{first_end}"}, {}),
            (201, {}, {"id": "vid123"}),
        ]
    )
    streamer = make_streamer(tmp_path, client)
    seen = []

    async def scenario():
        try:
            result = await streamer.upload_video_file(
                path, "show", "desc", progress=lambda s, t, n=None: seen.append((s, t, n))
            )
            assert result == {"video_id": "vid123", "youtube_url": "https://www.youtube.com/watch?v=vid123"}
        finally:
            await streamer.close()

    asyncio.run(scenario())

    url, kwargs = client.posts[0]
    assert url == "https://www.googleapis.com/upload/youtube/v3/videos"
    assert kwargs["params"] == {"uploadType": "resumable", "part": "snippet,status"}
    assert kwargs["headers"]["X-Upload-Content-Length"] == str(size)
    assert kwargs["json"]["snippet"]["title"] == "show"
    ranges = [call[1]["headers"]["Content-Range"] for call in client.puts]
    assert ranges[0] == f"bytes 0-{first_end}/{size}"
    assert ranges[1] == f"bytes {UPLOAD_CHUNK_BYTES}-{size - 1}/{size}"
    assert seen[-1] == (size, size, "upload")
    # Auth rides on the first attempt: no 401, no forced token refresh.
    assert client.puts[0][1]["headers"]["Authorization"] == "Bearer access-token"
    assert client.refresh_calls == 0


def test_upload_retries_503_then_resumes(tmp_path):
    """A 503 chunk is not resent blindly: the client queries the offset first."""
    from stream_archive.youtube_upload import UPLOAD_CHUNK_BYTES

    path = write_big(tmp_path, extra=10)
    size = path.stat().st_size
    client = UploadClient(
        [
            (503, {}, {"error": "backend"}),
            # Status query: nothing landed.
            (308, {}, {}),
            (308, {"range": f"bytes=0-{UPLOAD_CHUNK_BYTES - 1}"}, {}),
            (201, {}, {"id": "vid9"}),
        ]
    )
    streamer = make_streamer(tmp_path, client)

    async def scenario():
        try:
            result = await streamer.upload_video_file(path, "t", "d")
            assert result["video_id"] == "vid9"
        finally:
            await streamer.close()

    asyncio.run(scenario())
    status_queries = [c for c in client.puts if c[1]["headers"]["Content-Range"] == f"bytes */{size}"]
    assert len(status_queries) == 1


def test_upload_session_401_keeps_upload_headers(tmp_path):
    """A 401 session retry keeps the upload headers, not just the bearer."""
    path = tmp_path / "show.mp4"
    path.write_bytes(b"v" * 100)
    client = UploadClient(
        [(201, {}, {"id": "vid401"})],
        posts=[(401, {}, {"error": "auth"})],
    )
    streamer = make_streamer(tmp_path, client)

    async def scenario():
        try:
            result = await streamer.upload_video_file(path, "t", "d")
            assert result["video_id"] == "vid401"
        finally:
            await streamer.close()

    asyncio.run(scenario())
    assert len(client.posts) == 2
    retry_headers = client.posts[1][1]["headers"]
    assert retry_headers["X-Upload-Content-Length"] == "100"
    assert retry_headers["X-Upload-Content-Type"] == "video/mp4"
    assert retry_headers["Authorization"] == "Bearer access-token"


def test_upload_308_without_range_resends_same_chunk(tmp_path):
    """A 308 with no Range header resends the chunk, never skips it."""
    from stream_archive.youtube_upload import UPLOAD_CHUNK_BYTES

    path = write_big(tmp_path, extra=10)
    size = path.stat().st_size
    client = UploadClient(
        [
            (308, {}, {}),  # no Range: nothing landed
            (308, {"range": f"bytes=0-{UPLOAD_CHUNK_BYTES - 1}"}, {}),
            (201, {}, {"id": "vidgap"}),
        ]
    )
    streamer = make_streamer(tmp_path, client)

    async def scenario():
        try:
            result = await streamer.upload_video_file(path, "t", "d")
            assert result["video_id"] == "vidgap"
        finally:
            await streamer.close()

    asyncio.run(scenario())
    ranges = [call[1]["headers"]["Content-Range"] for call in client.puts]
    assert ranges[0] == f"bytes 0-{UPLOAD_CHUNK_BYTES - 1}/{size}"
    assert ranges[1] == ranges[0]
    assert ranges[2] == f"bytes {UPLOAD_CHUNK_BYTES}-{size - 1}/{size}"


def test_upload_status_query_failure_resends_chunk(tmp_path):
    """A failed status query resends the current chunk instead of failing."""
    from stream_archive.youtube_upload import UPLOAD_CHUNK_BYTES

    path = write_big(tmp_path, extra=10)
    size = path.stat().st_size
    first = f"bytes 0-{UPLOAD_CHUNK_BYTES - 1}/{size}"
    client = UploadClient(
        [
            (503, {}, {"error": "backend"}),
            (500, {}, {"error": "still down"}),  # status query fails too
            (201, {}, {"id": "vidq"}),
        ]
    )
    streamer = make_streamer(tmp_path, client)

    async def scenario():
        try:
            result = await streamer.upload_video_file(path, "t", "d")
            assert result["video_id"] == "vidq"
        finally:
            await streamer.close()

    asyncio.run(scenario())
    ranges = [call[1]["headers"]["Content-Range"] for call in client.puts]
    assert ranges[0] == first
    assert ranges[1] == f"bytes */{size}"
    assert ranges[2] == first


def test_upload_refreshes_token_after_401(tmp_path):
    """A 401 chunk PUT refreshes the token once and resends the chunk."""
    path = write_big(tmp_path, extra=10)
    client = UploadClient(
        [
            (401, {}, {"error": "auth"}),
            (201, {}, {"id": "vid7"}),
        ]
    )
    streamer = make_streamer(tmp_path, client)

    async def scenario():
        try:
            result = await streamer.upload_video_file(path, "t", "d")
            assert result["video_id"] == "vid7"
        finally:
            await streamer.close()

    asyncio.run(scenario())
    assert client.refresh_calls == 1


def test_upload_reuses_refreshed_token(tmp_path):
    """One mid-upload 401 refreshes once; later chunks reuse the token."""
    from stream_archive.youtube_upload import UPLOAD_CHUNK_BYTES

    path = write_big(tmp_path, extra=10)
    first_end = UPLOAD_CHUNK_BYTES - 1
    client = UploadClient(
        [
            (401, {}, {"error": "expired"}),
            (308, {"range": f"bytes=0-{first_end}"}, {}),
            (201, {}, {"id": "vidrot"}),
        ]
    )
    streamer = make_streamer(tmp_path, client)
    current = {"token": "t1"}

    async def rotating_creds(refresh=False):
        if refresh:
            current["token"] = "t2"
            client.refresh_calls += 1
        creds = FakeCreds()
        creds.token = current["token"]
        return creds

    streamer._get_credentials = rotating_creds  # type: ignore[method-assign]

    async def scenario():
        try:
            result = await streamer.upload_video_file(path, "t", "d")
            assert result["video_id"] == "vidrot"
        finally:
            await streamer.close()

    asyncio.run(scenario())
    assert client.refresh_calls == 1
    auths = [call[1]["headers"]["Authorization"] for call in client.puts]
    assert auths[0] == "Bearer t1"
    assert auths[1] == "Bearer t2"
    assert auths[2] == "Bearer t2"


def test_upload_late_complete_returns_video_id(tmp_path):
    """A 200/201 on a retry probe reports success instead of 'no video id'.

    The final chunk can land despite its failed answer. The status query
    then finds the upload finished: its id must come back, or the success
    reports as failed and a retry would upload a duplicate.
    """
    import httpx

    path = tmp_path / "show.mp4"
    path.write_bytes(b"v" * 100)
    client = UploadClient(
        [
            httpx.TransportError("connection lost"),
            (201, {}, {"id": "vidlate"}),
        ]
    )
    streamer = make_streamer(tmp_path, client)

    async def scenario():
        try:
            result = await streamer.upload_video_file(path, "t", "d")
            assert result == {"video_id": "vidlate", "youtube_url": "https://www.youtube.com/watch?v=vidlate"}
        finally:
            await streamer.close()

    asyncio.run(scenario())
    ranges = [call[1]["headers"]["Content-Range"] for call in client.puts]
    assert ranges[1] == "bytes */100"


def test_upload_missing_file_raises_value_error(tmp_path):
    """A gone file fails fast with a ValueError, like the MTProto path."""
    streamer = make_streamer(tmp_path, UploadClient([]))

    async def scenario():
        try:
            with pytest.raises(ValueError, match="gone"):
                await streamer.upload_video_file(tmp_path / "gone.mp4", "t", "d")
        finally:
            await streamer.close()

    asyncio.run(scenario())
