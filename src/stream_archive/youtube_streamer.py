import asyncio
import json
import logging
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from stream_archive.config import AppConfig, channel_url, is_kick_channel

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
_API_BASE = "https://www.googleapis.com/youtube/v3"


def build_video_description(author: str, channel: str, game: str) -> str:
    """Description for a re-streamed broadcast; platform-aware (Twitch/Kick)."""
    platform = "Kick" if is_kick_channel(channel) else "Twitch"
    return (
        f"{platform} stream by {author}\n"
        f"Game: {game}\n"
        f"Originally streamed at: {channel_url(channel)}\n"
        f"Recorded by StreamArchive"
    )


class YouTubeStreamer:
    def __init__(self, config: AppConfig):
        self._config = config
        yt = config.youtube
        self._privacy_status = yt.privacy_status
        self._token_path = config._workdir / "youtube_token.json"
        self._credentials: Credentials | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5))
        self._refresh_lock = asyncio.Lock()

    async def _get_credentials(self) -> Credentials:
        # Single-flight: hold the lock across load+refresh so concurrent starts
        # don't double-refresh the same expired token. to_thread keeps the
        # synchronous HTTPS refresh off the event loop that feeds the ffmpeg
        # pipes.
        async with self._refresh_lock:
            if self._credentials and self._credentials.valid:
                return self._credentials

            if not self._token_path.exists():
                raise RuntimeError("YouTube token not found. Run 'python setup_youtube.py' first to authenticate.")

            with open(self._token_path) as f:
                data = json.load(f)
            creds = Credentials.from_authorized_user_info(data, SCOPES)  # type: ignore[no-untyped-call]
            if creds is None:
                raise RuntimeError("YouTube token could not be loaded.")
            self._credentials = creds

            if not creds.valid:
                if creds.expired and creds.refresh_token:
                    await asyncio.to_thread(creds.refresh, Request())
                    self._save_token()
                else:
                    raise RuntimeError(
                        "YouTube token expired and cannot be refreshed. Run 'python setup_youtube.py' again."
                    )

            return self._credentials

    def _save_token(self) -> None:
        credentials = self._credentials
        if credentials is None:
            return
        data = json.loads(credentials.to_json())  # type: ignore[no-untyped-call]
        with open(self._token_path, "w") as f:
            json.dump(data, f)
        self._token_path.chmod(0o600)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        creds = await self._get_credentials()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {creds.token}"
        url = f"{_API_BASE}/{path}"
        resp = await self._client.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            logger.error("[youtube] Request failed (%d): %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    async def create_stream(self, author: str, title: str, channel: str, game: str) -> dict[str, Any]:
        raw_title = f"{author} - {title}"
        raw_title = raw_title.replace("<", "").replace(">", "")
        broadcast_title = raw_title[:100]
        if raw_title != broadcast_title:
            logger.info("[youtube] Title truncated to 100 chars: %r", broadcast_title)
        description = build_video_description(author, channel, game)
        from datetime import datetime, timezone

        scheduled_start = datetime.now(timezone.utc).isoformat()

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
            broadcast_id = broadcast["id"]
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
            stream_id = live_stream["id"]
            ingestion = live_stream["cdn"]["ingestionInfo"]
            ingestion_address = ingestion["ingestionAddress"]
            stream_name = ingestion["streamName"]
            logger.info("[youtube] Stream created: %s -> %s/%s", stream_id, ingestion_address, stream_name)

            params = {"id": broadcast_id, "streamId": stream_id, "part": "id,snippet,status"}
            logger.info("[youtube] Binding broadcast %s to stream %s", broadcast_id, stream_id)
            await self._request("POST", "liveBroadcasts/bind", params=params)
        except Exception:
            if broadcast_id is not None:
                try:
                    await self.end_stream(broadcast_id)
                except Exception as cleanup_err:
                    logger.error("[youtube] Failed to clean up broadcast %s: %s", broadcast_id, cleanup_err)
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

    async def close(self) -> None:
        await self._client.aclose()
