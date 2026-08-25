import asyncio
import logging
import time
from typing import Any

import httpx

from stream_archive.config import AppConfig

logger = logging.getLogger(__name__)

# Kick's edge intermittently rejects otherwise-valid app-token requests
# (observed as 400/403 waves that self-recover). Sending a descriptive
# User-Agent instead of the default python-httpx one is the standard
# mitigation for that (Cloudflare-fronted APIs commonly 403 the SDK default);
# retries below ride out the residual blips. Deliberately versionless: the
# string must stay stable across releases.
_USER_AGENT = "stream-archive"

# Transient statuses to retry with short backoff: edge/WAF hiccups (429/5xx)
# and rate limiting. Auth/param errors (401/400/403) stay immediate.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (1.0, 3.0)


class KickAPI:
    TOKEN_URL = "https://id.kick.com/oauth/token"
    CHANNELS_URL = "https://api.kick.com/public/v1/channels"
    PUBLIC_KEY_URL = "https://api.kick.com/public/v1/public-key"
    EVENTS_SUBS_URL = "https://api.kick.com/public/v1/events/subscriptions"
    MAX_SLUGS_PER_REQUEST = 50

    def __init__(self, config: AppConfig):
        kick = config.kick
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(10, connect=5),
            headers={"User-Agent": _USER_AGENT},
        )
        self._client_id = kick.client_id
        self._client_secret = kick.client_secret
        self._token: str | None = None
        self._token_expires_at = 0
        self._public_key: str | None = None
        self._token_lock = asyncio.Lock()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request, retrying transient failures with short backoff.

        Retries only the statuses in ``_RETRY_STATUSES`` plus transport-level
        errors; auth/param problems (401/400/403) fail immediately so the
        caller's error path (and any alert) fires without added latency.
        """
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await self.client.request(method, url, **kwargs)
            except httpx.TransportError:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
                resp = None
            if resp is not None and (resp.status_code not in _RETRY_STATUSES or attempt == _MAX_ATTEMPTS - 1):
                return resp
            await asyncio.sleep(_RETRY_DELAYS[attempt])
        raise RuntimeError("unreachable")

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._get_token()}"}

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        # Single-flight: concurrent callers (webhook verifications run in
        # parallel) must not each POST client_credentials. Double-check the
        # cache inside the lock — the winner of the race already refreshed.
        async with self._token_lock:
            if self._token and time.time() < self._token_expires_at - 60:
                return self._token
            try:
                resp = await self._request(
                    "POST",
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
                self._token_expires_at = time.time() + data.get("expires_in", 3600)
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
            resp = await self._request(
                "GET",
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
        resp = await self._request("GET", self.PUBLIC_KEY_URL)
        resp.raise_for_status()
        data = resp.json().get("data")
        if isinstance(data, dict):  # live API nests the PEM: {"data": {"public_key": "..."}}
            data = data.get("public_key")
        if data:  # a 200 without a key must not wipe the known-good PEM
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
        resp = await self._request("GET", self.EVENTS_SUBS_URL, headers=await self._headers())
        resp.raise_for_status()
        data = resp.json()["data"]
        if not self._client_id:
            return []
        return [s for s in data if s.get("app_id") == self._client_id]

    async def create_event_subscriptions(self, broadcaster_user_id: int, events: list[str]) -> list[dict[str, Any]]:
        """Create webhook subscriptions; returns created items (each has subscription_id)."""
        resp = await self._request(
            "POST",
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
        resp = await self._request(
            "DELETE",
            self.EVENTS_SUBS_URL,
            headers=await self._headers(),
            params=[("id", i) for i in ids],
        )
        resp.raise_for_status()

    async def close(self) -> None:
        await self.client.aclose()
