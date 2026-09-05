import asyncio
import logging
import time
from typing import Any

import httpx

from stream_archive.config import AppConfig

logger = logging.getLogger(__name__)

# The Kick edge intermittently rejects otherwise-valid app-token requests.
# These failures appear as 400/403 waves that recover on their own. A
# descriptive User-Agent instead of the default python-httpx one is the
# standard mitigation for this. Cloudflare-fronted APIs commonly reject
# the SDK default with 403. The retries below ride out remaining blips.
# This string is deliberately versionless: it must stay stable across
# releases.
_USER_AGENT = "stream-archive"

# Transient statuses to retry with short backoff: edge and WAF hiccups
# (429/5xx) and rate limiting. Auth and parameter errors (401/400/403)
# stay immediate.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (1.0, 3.0)


class KickAPI:
    TOKEN_URL = "https://id.kick.com/oauth/token"
    CHANNELS_URL = "https://api.kick.com/public/v1/channels"
    PUBLIC_KEY_URL = "https://api.kick.com/public/v1/public-key"
    EVENTS_SUBS_URL = "https://api.kick.com/public/v1/events/subscriptions"
    MAX_SLUGS_PER_REQUEST = 50

    def __init__(self, config: AppConfig, http: httpx.AsyncClient | None = None):
        kick = config.kick
        self.client = (
            http
            if http is not None
            else httpx.AsyncClient(
                timeout=httpx.Timeout(10, connect=5),
                headers={"User-Agent": _USER_AGENT},
            )
        )
        self._owns_client = http is None
        self._client_id = kick.client_id
        self._client_secret = kick.client_secret
        self._token: str | None = None
        self._token_expires_at = 0
        self._public_key: str | None = None
        self._token_lock = asyncio.Lock()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request, retrying transient failures with short backoff.

        Retries only transient statuses (429/5xx, see ``_RETRY_STATUSES``)
        and transport errors. Auth and parameter errors fail immediately,
        so caller error paths fire without delay.
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
        msg = "unreachable"
        raise RuntimeError(msg)

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._get_token()}"}

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
            except httpx.HTTPStatusError as e:
                logger.error("[kick_api] Token request failed: %s", e)
                raise
            else:
                return self._token

    async def get_channel_statuses(self, slugs: list[str]) -> dict[str, dict[str, Any]]:
        """Map slug to {title, game, is_live, broadcaster_user_id}.

        Unknown slugs are absent.
        """
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
        """Return the PEM string used to verify webhook signatures.

        The value is cached in memory. ``force`` bypasses the cache for a
        key-rotation refetch after a signature failure. If a fetch fails,
        the method keeps the previous key, so verification still runs
        against the last known-good key.
        """
        if self._public_key and not force:
            return self._public_key
        resp = await self._request("GET", self.PUBLIC_KEY_URL)
        resp.raise_for_status()
        data = resp.json().get("data")
        if isinstance(data, dict):  # live API nests the PEM under data.public_key
            data = data.get("public_key")
        if data:  # a 200 without a key must not wipe the known-good PEM
            self._public_key = data
        return self._public_key

    def clear_public_key_cache(self) -> None:
        self._public_key = None

    async def list_event_subscriptions(self) -> list[dict[str, Any]]:
        """List all webhook subscriptions for this app with a fail-closed app_id filter.

        The method returns only subscriptions whose app_id matches this
        client_id. The webhook reconcile deletes subscriptions for
        unmonitored broadcasters, so a wrong filter would destroy another
        app's subscriptions. Without a configured client_id, nothing is
        managed.
        """
        resp = await self._request("GET", self.EVENTS_SUBS_URL, headers=await self._headers())
        resp.raise_for_status()
        data = resp.json()["data"]
        if not self._client_id:
            return []
        return [s for s in data if s.get("app_id") == self._client_id]

    async def create_event_subscriptions(self, broadcaster_user_id: int, events: list[str]) -> list[dict[str, Any]]:
        """Create webhook subscriptions. Returns created items with subscription_id."""
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
        items: list[dict[str, Any]] = resp.json()["data"]
        return items

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
        if self._owns_client:
            await self.client.aclose()
