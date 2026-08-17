import asyncio
import importlib.metadata
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx
from packaging.version import Version

from stream_archive.config import AppConfig

logger = logging.getLogger(__name__)

_PLUGIN_VERSION_RE = re.compile(r'STREAMLINK_TTVLOL_VERSION\s*=\s*"([^"]+)"')
_PLUGIN_RELEASES_URL = "https://api.github.com/repos/2bc4/streamlink-ttvlol/releases/latest"
_PYPI_STREAMLINK_URL = "https://pypi.org/pypi/streamlink/json"
_STREAMLINK_RELEASE_NOTES_URL = "https://api.github.com/repos/streamlink/streamlink/releases/tags/{tag}"
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
            if out:
                out.append("…")
            break
        out.append(ln)
    return out


def _installed_app_version() -> str | None:
    """Installed package version; None when the distribution is missing."""
    try:
        return importlib.metadata.version("stream-archive")
    except importlib.metadata.PackageNotFoundError:
        return None


class UpdateChecker:
    """Periodic informational update checks (app, streamlink, vendored plugin) and /update.

    Checks are read-only and never raise; nothing is downloaded or applied at
    runtime — updates ship in new images.
    """

    def __init__(self, config: AppConfig, notifier: Any, http: Any = None):
        self._config = config
        self._notifier = notifier
        self._workdir = config._workdir
        # GitHub release assets 302-redirect to release-assets.githubusercontent.com,
        # so redirects must be followed.
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10), follow_redirects=True)
        self._lock = asyncio.Lock()
        self._state_path = self._workdir / "update_state.json"
        self._state: dict[str, Any] = {}

    def _plugin_path(self) -> Path:
        plugin_dir = self._config.plugin_dir
        if not os.path.isabs(plugin_dir):
            return self._workdir / plugin_dir / "twitch.py"
        return Path(plugin_dir) / "twitch.py"

    # ---- state -------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            with open(self._state_path) as f:
                self._state = json.load(f)
        except FileNotFoundError:
            self._state = {}
        except json.JSONDecodeError:
            logger.warning("[updater] update_state.json corrupt; starting fresh")
            self._state = {}

    def _save_state(self) -> None:
        tmp = Path(str(self._state_path) + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self._state, f, indent=2)
        os.replace(tmp, self._state_path)

    # ---- checks ------------------------------------------------------------

    async def _check_app(self) -> dict[str, Any]:
        local = _installed_app_version()
        try:
            resp = await self._http.get(_APP_RELEASES_URL)
            resp.raise_for_status()
            data = resp.json()
            tag = (data.get("tag_name") or "").removeprefix("v")
            if not tag:
                raise ValueError("no tag_name in releases payload")
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

    def _plugin_version(self) -> str | None:
        try:
            content = self._plugin_path().read_text(errors="replace")
        except OSError:
            return None
        m = _PLUGIN_VERSION_RE.search(content)
        return m.group(1) if m else None

    async def _check_plugin(self) -> dict[str, Any]:
        current = self._plugin_version()
        try:
            resp = await self._http.get(_PLUGIN_RELEASES_URL)
            resp.raise_for_status()
            data = resp.json()
            tag = data.get("tag_name")
            if not tag:
                raise ValueError("no tag_name in releases payload")
        except Exception as e:
            logger.warning("[updater] plugin update check failed: %s", e)
            return {"status": "unknown", "current": current, "latest": None}
        if current is None:
            logger.warning("[updater] plugins/twitch.py not found or version constant missing")
            return {"status": "unknown", "current": None, "latest": tag}
        status = "up_to_date" if current == tag else "update"
        return {
            "status": status,
            "current": current,
            "latest": tag,
            "changelog": data.get("body") or None,
        }

    def _installed_streamlink(self) -> str | None:
        try:
            return importlib.metadata.version("streamlink")
        except importlib.metadata.PackageNotFoundError:
            return None

    async def _check_streamlink(self) -> dict[str, Any]:
        installed = self._installed_streamlink()
        try:
            resp = await self._http.get(_PYPI_STREAMLINK_URL)
            resp.raise_for_status()
            data = resp.json()
            latest = data["info"]["version"]
        except Exception as e:
            logger.warning("[updater] streamlink update check failed: %s", e)
            return {"status": "unknown", "current": installed, "latest": None}
        if installed is None:
            return {"status": "unknown", "current": None, "latest": latest}
        try:
            status = "update" if Version(latest) > Version(installed) else "up_to_date"
        except Exception:
            status = "unknown"
        changelog = None
        if status == "update":
            changelog = await self._fetch_release_notes("streamlink/streamlink", latest)
        return {"status": status, "current": installed, "latest": latest, "changelog": changelog}

    async def _fetch_release_notes(self, repo: str, tag: str) -> str | None:
        """Best-effort GitHub release notes for a tag; None on any failure."""
        try:
            resp = await self._http.get(_STREAMLINK_RELEASE_NOTES_URL.format(tag=tag))
            resp.raise_for_status()
            return resp.json().get("body") or None
        except Exception as e:
            logger.warning("[updater] changelog fetch failed for %s %s: %s", repo, tag, e)
            return None

    # ---- check / notify ----------------------------------------------------

    async def check(self, notify: bool) -> dict[str, Any]:
        async with self._lock:
            uc = self._config.update_check
            report = {}
            if uc.check_app:
                report["app"] = await self._check_app()
            if uc.check_streamlink:
                report["streamlink"] = await self._check_streamlink()
            if uc.check_plugin:
                report["plugin"] = await self._check_plugin()

            if not notify:
                return report

            self._load_state()
            lines = []
            state_changed = False
            for source, data in report.items():
                latest = data.get("latest")
                if latest is None:
                    continue
                if data["status"] == "update" and self._state.get(source) != latest:
                    if source == "app":
                        lines.append(f"• stream-archive: v{data['current']} → v{latest}")
                        cl = data.get("changelog") or []
                    elif source == "streamlink":
                        lines.append(f"• streamlink: {data['current']} → {latest}")
                        cl = _changelog_lines(data.get("changelog"))
                    else:
                        lines.append(f"• streamlink-ttvlol: {data['current']} → {latest}")
                        cl = _changelog_lines(data.get("changelog"))
                    if cl:
                        lines.append("  Changelog:")
                        lines.extend(f"  • {ln}" for ln in cl)
                    self._state[source] = latest
                    state_changed = True
                elif data["status"] == "up_to_date":
                    if self._state.get(source) != latest:
                        self._state[source] = latest
                        state_changed = True

            if lines:
                app_update = report.get("app", {}).get("status") == "update"
                footer = (
                    "Apply: docker compose pull && docker compose up -d"
                    if app_update
                    else "No action needed — plugin/streamlink updates ship in a future image release."
                )
                text = "📦 Update available for stream-archive\n" + "\n".join(lines) + "\n" + footer
                await self._notifier.notify(text)
            if state_changed:
                self._save_state()
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
        await self._http.aclose()
