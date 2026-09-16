import asyncio
import logging
import time
from typing import Any

import httpx

from stream_archive.config import AppConfig

logger = logging.getLogger(__name__)

#: Twitch accepts at most 100 entries in a query filter such as login or user_id.
_MAX_QUERY_ITEMS = 100


class TwitchAPI:
    def __init__(self, config: AppConfig, http: httpx.AsyncClient | None = None):
        self.client = http if http is not None else httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5))
        self._owns_client = http is None
        self._client_id = config.twitch_client_id
        self._client_secret = config.twitch_client_secret
        self._token: str | None = None
        self._token_expires_at = 0
        self._token_lock = asyncio.Lock()

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        # Single-flight: concurrent callers must not each POST
        # client_credentials. Double-check the cache inside the lock
        # because the winner of the race already refreshed it.
        async with self._token_lock:
            if self._token and time.time() < self._token_expires_at - 60:
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
                self._token_expires_at = time.time() + data.get("expires_in", 3600)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                # Transport faults and a malformed body must reach the log.
                logger.error("[twitch_api] Token request failed: %s", e)
                raise
            else:
                return self._token

    async def resolve_user_ids(self, usernames: list[str]) -> dict[str, str]:
        if not usernames:
            return {}
        resolved: dict[str, str] = {}
        try:
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Client-Id": self._client_id,
            }
            # Twitch rejects more than 100 logins in one request.
            for start in range(0, len(usernames), _MAX_QUERY_ITEMS):
                chunk = usernames[start : start + _MAX_QUERY_ITEMS]
                resp = await self.client.get(
                    "https://api.twitch.tv/helix/users", headers=headers, params={"login": chunk}
                )
                resp.raise_for_status()
                # Twitch answers with the canonical lowercase login. Key the
                # result by the name that the caller passed, so a mixed-case
                # configured channel still matches.
                by_login = {user["login"]: user["id"] for user in resp.json()["data"]}
                resolved.update({name: by_login[name.lower()] for name in chunk if name.lower() in by_login})
        except httpx.HTTPStatusError as e:
            logger.error("[twitch_api] resolve_user_ids failed: %s", e)
            raise
        return resolved

    async def get_live_streams(self, user_ids: dict[str, str]) -> dict[str, Any]:
        if not user_ids:
            return {}
        streams: dict[str, Any] = {}
        try:
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Client-Id": self._client_id,
            }
            ids = list(user_ids.values())
            # Twitch rejects more than 100 user_ids in one request.
            for start in range(0, len(ids), _MAX_QUERY_ITEMS):
                resp = await self.client.get(
                    "https://api.twitch.tv/helix/streams",
                    headers=headers,
                    params={"user_id": ids[start : start + _MAX_QUERY_ITEMS]},
                )
                resp.raise_for_status()
                streams.update({stream["user_id"]: stream for stream in resp.json()["data"]})
        except httpx.HTTPStatusError as e:
            logger.error("[twitch_api] get_live_streams failed: %s", e)
            raise
        return streams

    async def _eventsub_headers(self) -> dict[str, str]:
        token = await self._get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Client-Id": self._client_id,
        }

    async def list_conduits(self) -> list[Any]:
        """Return existing EventSub conduits. Each dict has id and shard_count."""
        headers = await self._eventsub_headers()
        resp = await self.client.get("https://api.twitch.tv/helix/eventsub/conduits", headers=headers)
        resp.raise_for_status()
        data: list[Any] = resp.json()["data"]
        return data

    async def create_conduit(self, shard_count: int = 1) -> dict[str, Any]:
        headers = await self._eventsub_headers()
        resp = await self.client.post(
            "https://api.twitch.tv/helix/eventsub/conduits",
            headers=headers,
            json={"shard_count": shard_count},
        )
        resp.raise_for_status()
        conduit: dict[str, Any] = resp.json()["data"][0]
        return conduit

    async def delete_conduit(self, conduit_id: str) -> None:
        """Delete a conduit. Deletion cascades to its subscriptions. Treats 404 as success."""
        headers = await self._eventsub_headers()
        resp = await self.client.delete(
            "https://api.twitch.tv/helix/eventsub/conduits", headers=headers, params={"id": conduit_id}
        )
        if resp.status_code == 404:
            return
        resp.raise_for_status()

    async def update_conduit_shards(self, conduit_id: str, session_id: str) -> dict[str, Any]:
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
        shard: dict[str, Any] = resp.json()["data"][0]
        return shard

    async def create_eventsub_subscription(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Create a subscription and return (status_code, body).

        The method does not raise for status on 202/400/403/409. Callers
        handle those statuses. Every other failing status, for example 401,
        429, or a 5xx, raises ``httpx.HTTPStatusError``, and the caller
        must handle it.
        """
        headers = await self._eventsub_headers()
        resp = await self.client.post(
            "https://api.twitch.tv/helix/eventsub/subscriptions", headers=headers, json=payload
        )
        if resp.status_code in (202, 400, 403, 409):
            return resp.status_code, resp.json()
        resp.raise_for_status()
        return resp.status_code, resp.json()

    async def delete_eventsub_subscription(self, sub_id: str) -> None:
        """Delete a subscription. 404 is success."""
        headers = await self._eventsub_headers()
        resp = await self.client.delete(
            "https://api.twitch.tv/helix/eventsub/subscriptions", headers=headers, params={"id": sub_id}
        )
        if resp.status_code == 404:
            return
        resp.raise_for_status()

    async def list_eventsub_subscriptions(self) -> list[Any]:
        """List all subscriptions with cursor pagination (max 10 pages of 100)."""
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
        if cursor:
            # A caller uses this list to find an existing subscription.
            logger.warning(
                "[twitch_api] subscription list stopped at the 10-page cap (%d items): the list is partial",
                len(data),
            )
        return data

    async def get_stream(self, user_id: str) -> Any:
        """Return a single stream snapshot (title/game_name), or None when offline."""
        headers = await self._eventsub_headers()
        resp = await self.client.get(
            "https://api.twitch.tv/helix/streams", headers=headers, params={"user_id": user_id}
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return data[0] if data else None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
