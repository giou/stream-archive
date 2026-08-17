import logging
import time
from typing import Any

import httpx

from stream_archive.config import AppConfig

logger = logging.getLogger(__name__)


class KickAPI:
    TOKEN_URL = "https://id.kick.com/oauth/token"
    CHANNELS_URL = "https://api.kick.com/public/v1/channels"
    PUBLIC_KEY_URL = "https://api.kick.com/public/v1/public-key"
    EVENTS_SUBS_URL = "https://api.kick.com/public/v1/events/subscriptions"
    MAX_SLUGS_PER_REQUEST = 50

    def __init__(self, config: AppConfig):
        kick = config.kick
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5))
        self._client_id = kick.client_id
        self._client_secret = kick.client_secret
        self._token: str | None = None
        self._token_expires_at = 0
        self._public_key: str | None = None

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._get_token()}"}

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        try:
            resp = await self.client.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._token_expires_at = now + data.get("expires_in", 3600)
            return self._token
        except httpx.HTTPStatusError as e:
            logger.error("[kick_api] Token request failed: %s", e)
            raise

    async def get_channel_statuses(self, slugs: list[str]) -> dict[str, dict[str, Any]]:
        """Map slug -> {title, game, is_live, broadcaster_user_id}; unknown slugs absent."""
        if not slugs:
            return {}
        headers = await self._headers()
        out = {}
        for i in range(0, len(slugs), self.MAX_SLUGS_PER_REQUEST):
            chunk = slugs[i : i + self.MAX_SLUGS_PER_REQUEST]
            resp = await self.client.get(
                self.CHANNELS_URL,
                headers=headers,
                params=[("slug", s) for s in chunk],
            )
            resp.raise_for_status()
            for item in resp.json()["data"]:
                out[item["slug"]] = {
                    "title": item.get("stream_title") or "",
                    "game": (item.get("category") or {}).get("name") or "",
                    "is_live": bool((item.get("stream") or {}).get("is_live")),
                    "broadcaster_user_id": item.get("broadcaster_user_id"),
                }
        return out

    async def get_public_key(self, force: bool = False) -> str | None:
        """PEM string used to verify webhook signatures; cached in memory.

        ``force`` bypasses the cache (key-rotation refetch on signature
        failure). On fetch failure the previous key is kept, so verification
        still runs against the last known-good key.
        """
        if self._public_key and not force:
            return self._public_key
        resp = await self.client.get(self.PUBLIC_KEY_URL)
        resp.raise_for_status()
        data = resp.json().get("data")
        if isinstance(data, dict):  # live API nests the PEM: {"data": {"public_key": "..."}}
            data = data.get("public_key")
        self._public_key = data
        return self._public_key

    def clear_public_key_cache(self) -> None:
        self._public_key = None

    async def list_event_subscriptions(self) -> list[dict[str, Any]]:
        """All webhook subscriptions for this app (fail-closed app_id filter).

        Subscriptions we cannot prove belong to this app are never returned:
        the webhook reconcile deletes subscriptions for unmonitored
        broadcasters, so a wrong filter would destroy another app's
        subscriptions. Without a client_id configured, nothing is managed.
        """
        resp = await self.client.get(self.EVENTS_SUBS_URL, headers=await self._headers())
        resp.raise_for_status()
        data = resp.json()["data"]
        if not self._client_id:
            return []
        return [s for s in data if s.get("app_id") == self._client_id]

    async def create_event_subscriptions(self, broadcaster_user_id: int, events: list[str]) -> list[dict[str, Any]]:
        """Create webhook subscriptions; returns created items (each has subscription_id)."""
        resp = await self.client.post(
            self.EVENTS_SUBS_URL,
            headers=await self._headers(),
            json={
                "broadcaster_user_id": broadcaster_user_id,
                "events": [{"name": name, "version": 1} for name in events],
                "method": "webhook",
            },
        )
        resp.raise_for_status()
        return resp.json()["data"]

    async def delete_event_subscriptions(self, ids: list[str]) -> None:
        if not ids:
            return
        resp = await self.client.delete(
            self.EVENTS_SUBS_URL,
            headers=await self._headers(),
            params=[("id", i) for i in ids],
        )
        resp.raise_for_status()

    async def close(self) -> None:
        await self.client.aclose()
