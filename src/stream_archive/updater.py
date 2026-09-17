import asyncio
import importlib.metadata
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from packaging.version import Version

from stream_archive.config import AppConfig

logger = logging.getLogger(__name__)

_APP_RELEASES_URL = "https://api.github.com/repos/giou/stream-archive/releases/latest"
_MAX_CHANGELOG_CHARS = 600


def _changelog_lines(body: str | None, limit: int = _MAX_CHANGELOG_CHARS) -> list[str]:
    """Normalize a release-notes body into a truncated list of non-empty lines."""
    lines = [ln.strip() for ln in (body or "").splitlines()]
    lines = [ln for ln in lines if ln]
    out: list[str] = []
    total = 0
    for ln in lines:
        total += len(ln) + 1
        if total > limit:
            # Mark the cut even when the first line alone is too long.
            out.append("…")
            break
        out.append(ln)
    return out


def installed_app_version() -> str | None:
    """Installed package version, or None when the distribution is missing."""
    try:
        return importlib.metadata.version("stream-archive")
    except importlib.metadata.PackageNotFoundError:
        return None


class UpdateChecker:
    """Periodic app update check and /update.

    The check is read-only and never raises. The runtime downloads nothing and
    applies nothing: a new app release ships in a new image. Streamlink and the
    vendored twitch.py plugin are not part of the check; the image build
    resolves them, and Dependabot updates the lockfile.
    """

    def __init__(self, config: AppConfig, notifier: Any, http: httpx.AsyncClient | None = None):
        self._config = config
        self._notifier = notifier
        self._workdir = config._workdir
        # GitHub answers 301 when a repository is renamed, so the client must
        # follow redirects.
        if http is not None:
            self._http = http
            self._owns_client = False
        else:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10), follow_redirects=True)
            self._owns_client = True
        self._lock = asyncio.Lock()
        self._state_path = self._workdir / "update_state.json"
        self._state: dict[str, Any] = {}

    # ---- state -------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self._state = {}
            return
        except json.JSONDecodeError, UnicodeDecodeError:
            logger.warning("[updater] update_state.json corrupt; starting fresh")
            self._state = {}
            return
        except OSError as e:
            logger.warning("[updater] cannot read update_state.json (%s); starting fresh", e)
            self._state = {}
            return
        if not isinstance(data, dict):
            # A list or a scalar breaks every later check with an
            # AttributeError, so treat it like a corrupt file.
            logger.warning("[updater] update_state.json is not a JSON object; starting fresh")
            self._state = {}
            return
        self._state = data

    def _save_state(self) -> None:
        tmp = Path(str(self._state_path) + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp, self._state_path)
        except OSError as e:
            logger.error("[updater] failed to save state to %s: %s", tmp, e, exc_info=True)

    # ---- checks ------------------------------------------------------------

    async def _check_app(self) -> dict[str, Any]:
        local = installed_app_version()

        def _missing_tag() -> None:
            msg = "no tag_name in releases payload"
            raise ValueError(msg)

        try:
            resp = await self._http.get(_APP_RELEASES_URL)
            resp.raise_for_status()
            data = resp.json()
            tag = (data.get("tag_name") or "").removeprefix("v")
            if not tag:
                _missing_tag()
        except Exception as e:
            logger.warning("[updater] app update check failed: %s", e)
            return {"status": "unknown", "current": local, "latest": None}
        if local is None:
            return {"status": "unknown", "current": None, "latest": tag}
        try:
            status = "update" if Version(tag) > Version(local) else "up_to_date"
        except Exception:
            status = "unknown"
        changelog = _changelog_lines(data.get("body")) if status == "update" else None
        return {"status": status, "current": local, "latest": tag, "changelog": changelog}

    # ---- check / notify ----------------------------------------------------

    async def check(self, notify: bool) -> dict[str, Any]:
        report = {"app": await self._check_app()}

        if not notify:
            return report

        data = report["app"]
        latest = data.get("latest")
        lines: list[str] = []
        record = False
        # The state file is small, but the read and the write are blocking
        # I/O. Keep them off the event loop.
        async with self._lock:
            await asyncio.to_thread(self._load_state)
            # Record every version that the check resolved. Thus a version
            # that comes back later (for example after a rollback) notifies
            # again. An inconclusive check must not consume the release.
            if latest is not None and data["status"] != "unknown" and self._state.get("app") != latest:
                if data["status"] == "update":
                    lines.append(f"• stream-archive: v{data['current']} → v{latest}")
                    cl = data.get("changelog") or []
                    if cl:
                        lines.append("  Changelog:")
                        lines.extend(f"  • {ln}" for ln in cl)
                record = True
            previous = self._state.get("app")

        if lines:
            text = (
                "📦 Update available for stream-archive\n"
                + "\n".join(lines)
                + "\nApply: docker compose pull && docker compose up -d"
            )
            try:
                await self._notifier.notify(text)
            except Exception:
                # Keep the release unrecorded. A failed send must retry on the
                # next check, or the alert for this release is lost.
                logger.error("[updater] update notification failed", exc_info=True)
                return report

        if record:
            async with self._lock:
                await asyncio.to_thread(self._load_state)
                # Another check can have recorded a newer release meanwhile.
                if self._state.get("app") == previous:
                    self._state["app"] = latest
                    await asyncio.to_thread(self._save_state)
        return report

    # ---- loop / lifecycle --------------------------------------------------

    async def run_loop(self) -> None:
        while True:
            try:
                uc = self._config.update_check
                if uc.enabled:
                    await self.check(notify=True)
            except Exception:
                logger.exception("[updater] update check cycle failed")
            interval = self._config.update_check.interval_hours * 3600
            await asyncio.sleep(interval)

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()
