import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import UTC
from pathlib import Path
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from stream_archive.config import AppConfig, channel_url, is_kick_channel
from stream_archive.recorder.common import sanitize_metadata_text

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
#: Name of the OAuth token file inside the working directory.
TOKEN_NAME = "youtube_token.json"
_API_BASE = "https://www.googleapis.com/youtube/v3"


def save_token(credentials: Credentials, path: Path) -> None:
    """Write credentials as a JSON token file with mode 0600."""
    data = json.loads(credentials.to_json())  # type: ignore[no-untyped-call]
    # Write a sibling file and rename it over the token. A crash in the middle
    # of the write can then never leave a truncated token behind, which would
    # break every later refresh. mkstemp creates the file with mode 0600, and
    # the rename gives the token that mode even when it was world-readable.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _resume_offset(resp: Any, default: int) -> int:
    """Next byte the server wants, from a 308 Range header or ``default``.

    A 308 answers ``Range: bytes=0-<last>`` for the bytes it holds. With
    no header nothing landed, so the caller resends from ``default``.
    """
    match = re.match(r"bytes=0-(\d+)", resp.headers.get("range", ""))
    if match is None:
        return default
    return int(match.group(1)) + 1


def _read_upload_chunk(handle: Any, offset: int, size: int) -> bytes:
    """Seek to ``offset`` and read up to ``size`` bytes. Runs off the loop."""
    handle.seek(offset)
    data: bytes = handle.read(size)
    return data


def _required_field(payload: Any, path: str, what: str) -> str:
    """Return a string field of a YouTube API payload, or raise.

    A 200 answer can still carry an error body or a new shape. The caller
    needs a clear error before it uses the value, and it needs the ids it
    already created, so it can remove them again.
    """
    *parents, leaf = path.split(".")
    node: Any = payload
    for key in parents:
        node = node.get(key) if isinstance(node, dict) else None
    node = node.get(leaf) if isinstance(node, dict) else None
    if not isinstance(node, str):
        msg = f"YouTube returned no {what}: {payload!r}"
        raise RuntimeError(msg)
    value: str = node
    return value


def build_video_description(author: str, channel: str, game: str) -> str:
    """Build the description text for a re-streamed Twitch or Kick broadcast."""
    platform = "Kick" if is_kick_channel(channel) else "Twitch"
    author = sanitize_metadata_text(author)
    game = sanitize_metadata_text(game)
    return (
        f"{platform} stream by {author}\n"
        f"Game: {game}\n"
        f"Originally streamed at: {channel_url(channel)}\n"
        "Recorded by StreamArchive"
    )


class YouTubeStreamer:
    def __init__(self, config: AppConfig):
        yt = config.youtube
        self._privacy_status = yt.privacy_status
        self._token_path = config.workdir / TOKEN_NAME
        self._credentials: Credentials | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5))
        self._refresh_lock = asyncio.Lock()
        #: Rollback tasks that outlived the await that started them.
        self._rollback_tasks: set[asyncio.Task[None]] = set()

    async def _get_credentials(self, refresh: bool = False) -> Credentials:
        """Return usable credentials, or raise.

        The method returns the cached credentials while they are valid. With
        ``refresh`` True it refreshes the token, even when the cached token
        still looks valid. A caller sets that flag after the API rejects a
        cached token.
        """
        # Single-flight: hold the lock across load+refresh so concurrent starts
        # do not double-refresh the same expired token. to_thread keeps the
        # synchronous HTTPS refresh off the event loop that feeds the ffmpeg
        # pipes.
        async with self._refresh_lock:
            if not refresh and self._credentials and self._credentials.valid:
                return self._credentials

            if not self._token_path.exists():
                msg = "YouTube token not found. Run 'stream-archive-setup-youtube' first to authenticate."
                raise RuntimeError(msg)

            try:
                with open(self._token_path) as f:
                    data = json.load(f)
            except (OSError, ValueError) as err:
                data = {}
                # Rebind before the handler ends: the name dies with the
                # except block, and the message below needs the cause.
                load_err: BaseException | None = err
            else:
                load_err = None
            token_err: str | None = None
            if load_err is not None:
                token_err = str(load_err)
            elif not isinstance(data, dict):
                # json.load succeeded on a list, string, or null. That shape
                # never authenticates, so fail here with the operator message
                # instead of a bare traceback from the credentials call below.
                token_err = "is not a JSON object"
            if token_err is not None:
                msg = (
                    f"YouTube token file {self._token_path} {token_err}. "
                    "Run 'stream-archive-setup-youtube' again to authenticate."
                )
                raise RuntimeError(msg) from load_err
            try:
                creds = Credentials.from_authorized_user_info(data, SCOPES)  # type: ignore[no-untyped-call]
            except (TypeError, KeyError, AttributeError, ValueError) as err:
                msg = (
                    f"YouTube token file {self._token_path} has an unexpected shape ({err}). "
                    "Run 'stream-archive-setup-youtube' again to authenticate."
                )
                raise RuntimeError(msg) from err
            self._credentials = creds

            if refresh or not creds.valid:
                if (refresh or creds.expired) and creds.refresh_token:
                    await asyncio.to_thread(creds.refresh, Request())
                    save_token(self._credentials, self._token_path)
                else:
                    msg = "YouTube token expired and cannot be refreshed. Run 'stream-archive-setup-youtube' again."
                    raise RuntimeError(msg)

            return self._credentials

    async def _rollback_create(self, stream_id: str | None, broadcast_id: str | None) -> None:
        """Remove the live stream and broadcast a failed create left behind.

        Both requests must run: an unused live stream keeps counting against
        the concurrent-stream quota, and a broadcast that never went live can
        only be deleted, not completed.
        """
        if stream_id is not None:
            try:
                await self._request("DELETE", "liveStreams", params={"id": stream_id})
            except Exception as cleanup_err:
                logger.error("[youtube] Failed to clean up stream %s: %s", stream_id, cleanup_err)
        if broadcast_id is not None:
            try:
                await self._request("DELETE", "liveBroadcasts", params={"id": broadcast_id})
            except Exception as cleanup_err:
                logger.error("[youtube] Failed to clean up broadcast %s: %s", broadcast_id, cleanup_err)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        creds = await self._get_credentials()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {creds.token}"
        url = f"{_API_BASE}/{path}"
        resp = await self._client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:
            # The API can reject a cached token that still looks valid, for
            # example after a revocation. Force a refresh and retry once.
            creds = await self._get_credentials(refresh=True)
            headers["Authorization"] = f"Bearer {creds.token}"
            resp = await self._client.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            logger.error("[youtube] Request failed (%d): %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            # A delete answers 204 with no body, so there is no JSON to read.
            return None
        return resp.json()

    async def create_stream(self, author: str, title: str, channel: str, game: str) -> dict[str, Any]:
        # The author and the title come from the platform, and this title is
        # published on the operator's public channel: canonicalize it to one
        # line before the API's own limits apply.
        raw_title = sanitize_metadata_text(f"{author} - {title}", limit=200)
        raw_title = raw_title.replace("<", "").replace(">", "")
        broadcast_title = raw_title[:100]
        if raw_title != broadcast_title:
            logger.info("[youtube] Title truncated to 100 chars: %r", broadcast_title)
        description = build_video_description(author, channel, game)
        from datetime import datetime

        scheduled_start = datetime.now(UTC).isoformat()

        broadcast_body = {
            "snippet": {
                "title": broadcast_title,
                "description": description,
                "scheduledStartTime": scheduled_start,
            },
            "status": {
                "privacyStatus": self._privacy_status,
                "selfDeclaredMadeForKids": False,
            },
            "contentDetails": {
                "enableAutoStart": True,
                "enableAutoStop": True,
                "enableDvr": True,
            },
        }

        broadcast_id = None
        stream_id = None

        try:
            params = {"part": "snippet,status,contentDetails"}
            logger.info("[youtube] Creating live broadcast: %s", broadcast_title)
            broadcast = await self._request("POST", "liveBroadcasts", params=params, json=broadcast_body)
            broadcast_id = _required_field(broadcast, "id", "broadcast id")
            logger.info("[youtube] Broadcast created: %s", broadcast_id)

            stream_body = {
                "snippet": {
                    "title": broadcast_title,
                },
                "cdn": {
                    "ingestionType": "rtmp",
                    "frameRate": "variable",
                    "resolution": "variable",
                },
            }
            params = {"part": "snippet,cdn,status"}
            logger.info("[youtube] Creating live stream")
            live_stream = await self._request("POST", "liveStreams", params=params, json=stream_body)
            stream_id = _required_field(live_stream, "id", "live stream id")
            ingestion_address = _required_field(live_stream, "cdn.ingestionInfo.ingestionAddress", "ingestion address")
            stream_name = _required_field(live_stream, "cdn.ingestionInfo.streamName", "stream name")
            logger.info("[youtube] Stream created: %s -> %s", stream_id, ingestion_address)

            params = {"id": broadcast_id, "streamId": stream_id, "part": "id,snippet,status"}
            logger.info("[youtube] Binding broadcast %s to stream %s", broadcast_id, stream_id)
            await self._request("POST", "liveBroadcasts/bind", params=params)
        except BaseException:
            # BaseException, not Exception: the termination path cancels the
            # recording task, and CancelledError is not an Exception. With
            # ``except Exception`` a shutdown during the create left the
            # broadcast and the live stream on the account, bound and neither
            # ended nor deleted, because the caller never received their ids.
            # The cleanup itself must survive the pending cancellation, so it
            # runs shielded.
            # The task is held in a set: the loop keeps only a weak reference,
            # and shield drops its callback on the inner task once this await is
            # cancelled, so without the reference a second cancellation or loop
            # teardown could collect the rollback mid-flight and leave the
            # broadcast and its bound live stream behind.
            rollback = asyncio.ensure_future(self._rollback_create(stream_id, broadcast_id))
            self._rollback_tasks.add(rollback)
            rollback.add_done_callback(self._rollback_tasks.discard)
            try:
                await asyncio.shield(rollback)
            except BaseException:  # a second cancellation: the task still runs
                logger.error(
                    "[youtube] Rollback of stream %s / broadcast %s was interrupted; "
                    "it continues in the background, remove them manually if it also fails",
                    stream_id,
                    broadcast_id,
                )
            raise

        return {
            "broadcast_id": broadcast_id,
            "stream_id": stream_id,
            "stream_name": stream_name,
            "ingestion_address": ingestion_address,
            "rtmp_url": f"{ingestion_address}/{stream_name}",
            "youtube_url": f"https://youtube.com/watch?v={broadcast_id}",
        }

    async def end_stream(self, broadcast_id: str) -> None:
        params = {
            "id": broadcast_id,
            "broadcastStatus": "complete",
            "part": "id,snippet,status",
        }
        logger.info("[youtube] Ending broadcast: %s", broadcast_id)
        await self._request("POST", "liveBroadcasts/transition", params=params)

    async def get_broadcast_status(self, broadcast_id: str) -> str | None:
        """lifeCycleStatus of one broadcast, or None when it is gone.

        A takedown shows as ``revoked`` while the RTMP ingest stays open,
        so ffmpeg never errors. The restream polls this during the capture
        to catch that state. ``live``, ``liveStarting``, and ``testing``
        all mean the broadcast still exists.
        """
        payload = await self._request("GET", "liveBroadcasts", params={"id": broadcast_id, "part": "status"})
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            return None
        first = items[0]
        status = first.get("status") if isinstance(first, dict) else None
        life = status.get("lifeCycleStatus") if isinstance(status, dict) else None
        return life if isinstance(life, str) else None

    async def close(self) -> None:
        # Let an in-flight rollback finish first: it is removing resources that
        # would otherwise stay on the account with nothing tracking them.
        # The snapshot and the shield keep a cancellation of this call from
        # abandoning that rollback, and the finally always releases the client.
        pending = list(self._rollback_tasks)
        try:
            if pending:
                await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
        finally:
            await self._client.aclose()

    async def upload_video_file(
        self,
        path: str | Path,
        title: str,
        description: str,
        progress: Any = None,
    ) -> dict[str, str]:
        """Upload ``path`` as a video-on-demand. Return its id and watch URL.

        The upload follows the resumable protocol: a session POST answers
        the session URI, then chunked PUTs fill it. Each chunk reports
        progress. A 5xx answer or a transport error retries the chunk with
        backoff. A cancellation aborts the upload: the session URI simply
        expires server-side, so no rollback runs here.
        """
        import mimetypes
        import os

        file_path = Path(path)
        try:
            size = os.path.getsize(file_path)
        except FileNotFoundError:
            msg = f"the file is gone: {file_path.name}"
            raise ValueError(msg) from None
        if size <= 0:
            msg = f"the file is empty: {file_path.name}"
            raise ValueError(msg)
        mime, _ = mimetypes.guess_type(file_path.name)
        content_type = mime if mime and mime.startswith("video/") else "application/octet-stream"
        body = {
            "snippet": {"title": title, "description": description, "categoryId": "22"},
            "status": {
                "privacyStatus": self._privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }
        session_url = await self._start_upload_session(body, size, content_type)
        video_id = await self._send_upload_chunks(session_url, file_path, size, content_type, progress)
        return {"video_id": video_id, "youtube_url": f"https://www.youtube.com/watch?v={video_id}"}

    async def _upload_headers(self) -> dict[str, str]:
        """Bearer headers of the current credentials."""
        creds = await self._get_credentials()
        return {"Authorization": f"Bearer {creds.token}"}

    async def _start_upload_session(self, body: dict[str, Any], size: int, content_type: str) -> str:
        """Open a resumable session. Return its session URI."""
        from stream_archive.youtube_upload import UPLOAD_BASE

        params = {"uploadType": "resumable", "part": "snippet,status"}
        headers = await self._upload_headers()
        headers.update(
            {
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": content_type,
            }
        )
        resp = await self._client.post(UPLOAD_BASE, params=params, headers=headers, json=body)
        if resp.status_code == 401:
            # Refresh into the same dict: a replace would drop the
            # Content-Type and X-Upload-* headers the session needs.
            headers.update(await self._upload_headers_refresh())
            resp = await self._client.post(UPLOAD_BASE, params=params, headers=headers, json=body)
        if resp.status_code >= 400:
            logger.error("[youtube] Upload session failed (%d): %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        session_url = resp.headers.get("location")
        if not session_url or not isinstance(session_url, str):
            msg = f"YouTube answered the upload session without a session URI: {resp.text[:200]!r}"
            raise RuntimeError(msg)
        return session_url

    async def _upload_headers_refresh(self) -> dict[str, str]:
        """Bearer headers after a forced token refresh."""
        creds = await self._get_credentials(refresh=True)
        return {"Authorization": f"Bearer {creds.token}"}

    async def _put_upload_chunk(
        self, session_url: str, headers: dict[str, str], chunk: bytes, timeout: httpx.Timeout, auth: dict[str, str]
    ) -> Any:
        """One chunk PUT, with one retry after a forced token refresh on 401.

        A refresh updates ``auth`` too, so later chunks reuse the fresh
        token instead of each 401ing and refreshing again.
        """
        resp = await self._client.put(session_url, headers=headers, content=chunk, timeout=timeout)
        if resp.status_code == 401:
            refreshed = await self._upload_headers_refresh()
            headers.update(refreshed)
            auth.update(refreshed)
            resp = await self._client.put(session_url, headers=headers, content=chunk, timeout=timeout)
        return resp

    async def _send_upload_chunks(
        self,
        session_url: str,
        file_path: Path,
        size: int,
        content_type: str,
        progress: Any,
    ) -> str:
        """PUT ``size`` bytes to ``session_url`` in chunks. Return the video id."""
        from stream_archive.youtube_upload import (
            MAX_CHUNK_RETRIES,
            RETRIABLE_STATUS_CODES,
            UPLOAD_CHUNK_BYTES,
            report_upload_progress,
        )

        completed_id: str | None = None

        async def resend_from_server(current: int) -> int:
            """Resume point after a failed chunk: what the server holds.

            A failed status query must not fail the chunk: the chunk is
            resent from its start instead. Resending stored bytes is safe.
            When the server answers that the upload already finished, the
            video id rides back out through ``completed_id`` below.
            """
            nonlocal completed_id
            try:
                offset, video_id = await self._query_upload_offset(session_url, size)
            except Exception:
                logger.warning("[youtube] Upload status query failed, resending the current chunk")
                return current
            if video_id is not None:
                completed_id = video_id
            return offset

        # Bearer headers up front: without them every chunk 401s first and
        # each one forces a token refresh. The 401 path below only covers a
        # token that expires mid-upload.
        auth = await self._upload_headers()
        offset = 0
        retries = 0
        backoff = 1.0
        chunk_timeout = httpx.Timeout(120.0, connect=10.0)
        with open(file_path, "rb") as handle:
            while offset < size:
                chunk = await asyncio.to_thread(_read_upload_chunk, handle, offset, UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                end = offset + len(chunk) - 1
                headers = {
                    "Content-Type": content_type,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                    **auth,
                }
                try:
                    resp = await self._put_upload_chunk(session_url, headers, chunk, chunk_timeout, auth)
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    # The chunk may or may not have landed: ask the server
                    # where the upload stands instead of resending blindly.
                    retries += 1
                    if retries > MAX_CHUNK_RETRIES:
                        raise
                    logger.warning("[youtube] Upload chunk failed (%s), retry %d", e, retries)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    offset = await resend_from_server(offset)
                    continue
                if resp.status_code in (200, 201):
                    report_upload_progress(progress, size, size, "upload")
                    return _required_field(resp.json(), "id", "video id")
                if resp.status_code == 308:
                    # Incomplete: the server kept the chunk (all of it or a
                    # prefix) and waits for the rest. Without a Range header
                    # nothing landed, so the same chunk goes again: resuming
                    # past it would leave a gap in the object.
                    offset = _resume_offset(resp, offset)
                    report_upload_progress(progress, offset, size, "upload")
                    retries = 0
                    backoff = 1.0
                    continue
                if resp.status_code in RETRIABLE_STATUS_CODES:
                    retries += 1
                    if retries > MAX_CHUNK_RETRIES:
                        logger.error("[youtube] Upload chunk failed (%d): %s", resp.status_code, resp.text[:500])
                        resp.raise_for_status()
                    logger.warning("[youtube] Upload chunk got %d, retry %d", resp.status_code, retries)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    offset = await resend_from_server(offset)
                    continue
                logger.error("[youtube] Upload chunk failed (%d): %s", resp.status_code, resp.text[:500])
                resp.raise_for_status()
        if completed_id is not None:
            # A retry probe found the upload already finished (the final
            # chunk landed despite its failed answer). Report the success
            # with its id instead of a bogus "no video id" failure, which
            # would also orphan the sidecar and invite a duplicate retry.
            report_upload_progress(progress, size, size, "upload")
            return completed_id
        # A complete upload answers 200/201 above, so reaching here means
        # the stream ended early without an answer.
        msg = f"YouTube upload of {file_path.name} ended without a video id"
        raise RuntimeError(msg)

    async def _query_upload_offset(self, session_url: str, size: int) -> tuple[int, str | None]:
        """Resume point of ``session_url`` plus the video id when finished.

        A status query is a zero-length PUT with an unknown range. The
        server answers 308 with a Range header, or 200/201 with the video
        resource when the upload already finished. Returns
        ``(offset, video_id)``; the id is None unless finished.
        """
        headers = await self._upload_headers()
        resp = await self._client.put(
            session_url,
            headers={"Content-Length": "0", "Content-Range": f"bytes */{size}", **headers},
            content=b"",
        )
        if resp.status_code in (200, 201):
            return size, _required_field(resp.json(), "id", "video id")
        if resp.status_code == 308:
            return _resume_offset(resp, 0), None
        if resp.status_code == 401:
            headers = await self._upload_headers_refresh()
            resp = await self._client.put(
                session_url,
                headers={
                    "Content-Length": "0",
                    "Content-Range": f"bytes */{size}",
                    **headers,
                },
                content=b"",
            )
            if resp.status_code in (200, 201):
                return size, _required_field(resp.json(), "id", "video id")
            if resp.status_code == 308:
                return _resume_offset(resp, 0), None
        if resp.status_code >= 400:
            logger.error("[youtube] Upload status query failed (%d): %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        return 0, None
