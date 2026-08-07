import logging
import re
from pathlib import Path

import httpx
from src.stream_archive.config import get_config

logger = logging.getLogger(__name__)


class TwitchAPI:
    def __init__(self):
        config = get_config()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5))
        self._token = None
        self._token_expires_at = 0
        self._client_id = config["twitch_client_id"]
        self._client_secret = config["twitch_client_secret"]

    async def _get_token(self):
        import time
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        try:
            resp = await self.client.post(
                "https://id.twitch.tv/oauth2/token",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._token_expires_at = now + data.get("expires_in", 3600)
            return self._token
        except httpx.HTTPStatusError as e:
            logger.error("[twitch_api] Token request failed: %s", e)
            raise

    async def resolve_user_ids(self, usernames):
        if not usernames:
            return {}
        try:
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Client-Id": self._client_id,
            }
            params = {"login": usernames}
            resp = await self.client.get("https://api.twitch.tv/helix/users", headers=headers, params=params)
            resp.raise_for_status()
            return {user["login"]: user["id"] for user in resp.json()["data"]}
        except httpx.HTTPStatusError as e:
            logger.error("[twitch_api] resolve_user_ids failed: %s", e)
            raise

    async def get_live_streams(self, user_ids):
        if not user_ids:
            return {}
        try:
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Client-Id": self._client_id,
            }
            params = {"user_id": list(user_ids.values())}
            resp = await self.client.get("https://api.twitch.tv/helix/streams", headers=headers, params=params)
            resp.raise_for_status()
            return {stream["user_id"]: stream for stream in resp.json()["data"]}
        except httpx.HTTPStatusError as e:
            logger.error("[twitch_api] get_live_streams failed: %s", e)
            raise

    async def close(self):
        await self.client.aclose()
