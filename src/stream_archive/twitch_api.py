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

    async def _eventsub_headers(self):
        token = await self._get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Client-Id": self._client_id,
        }

    async def list_conduits(self):
        """Return existing EventSub conduits (each dict has id, shard_count)."""
        headers = await self._eventsub_headers()
        resp = await self.client.get("https://api.twitch.tv/helix/eventsub/conduits", headers=headers)
        resp.raise_for_status()
        return resp.json()["data"]

    async def create_conduit(self, shard_count=1):
        headers = await self._eventsub_headers()
        resp = await self.client.post(
            "https://api.twitch.tv/helix/eventsub/conduits",
            headers=headers,
            json={"shard_count": shard_count},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]

    async def delete_conduit(self, conduit_id):
        """Delete a conduit (also cascades to its subscriptions). 404 is success."""
        headers = await self._eventsub_headers()
        resp = await self.client.delete(
            "https://api.twitch.tv/helix/eventsub/conduits", headers=headers, params={"id": conduit_id}
        )
        if resp.status_code == 404:
            return
        resp.raise_for_status()

    async def update_conduit_shards(self, conduit_id, session_id):
        """Associate the single WebSocket shard ('0') with an EventSub session."""
        headers = await self._eventsub_headers()
        resp = await self.client.patch(
            "https://api.twitch.tv/helix/eventsub/conduits/shards",
            headers=headers,
            json={
                "conduit_id": conduit_id,
                "shards": [{"id": "0", "transport": {"method": "websocket", "session_id": session_id}}],
            },
        )
        resp.raise_for_status()
        return resp.json()["data"][0]

    async def create_eventsub_subscription(self, payload):
        """Create a subscription; returns (status_code, body) without raising on 202/400/403/409."""
        headers = await self._eventsub_headers()
        resp = await self.client.post(
            "https://api.twitch.tv/helix/eventsub/subscriptions", headers=headers, json=payload
        )
        if resp.status_code in (202, 400, 403, 409):
            return resp.status_code, resp.json()
        resp.raise_for_status()
        return resp.status_code, resp.json()

    async def delete_eventsub_subscription(self, sub_id):
        """Delete a subscription. 404 is success."""
        headers = await self._eventsub_headers()
        resp = await self.client.delete(
            "https://api.twitch.tv/helix/eventsub/subscriptions", headers=headers, params={"id": sub_id}
        )
        if resp.status_code == 404:
            return
        resp.raise_for_status()

    async def list_eventsub_subscriptions(self):
        """All subscriptions, cursor-paginated (max 10 pages of 100)."""
        headers = await self._eventsub_headers()
        data = []
        cursor = None
        for _ in range(10):
            params = {"first": 100}
            if cursor:
                params["after"] = cursor
            resp = await self.client.get(
                "https://api.twitch.tv/helix/eventsub/subscriptions", headers=headers, params=params
            )
            resp.raise_for_status()
            body = resp.json()
            data.extend(body["data"])
            cursor = body.get("pagination", {}).get("cursor")
            if not cursor:
                break
        return data

    async def get_stream(self, user_id):
        """Single stream snapshot (title/game_name); None when offline."""
        headers = await self._eventsub_headers()
        resp = await self.client.get(
            "https://api.twitch.tv/helix/streams", headers=headers, params={"user_id": user_id}
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return data[0] if data else None

    async def close(self):
        await self.client.aclose()
