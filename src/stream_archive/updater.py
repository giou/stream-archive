import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import re
from pathlib import Path

import httpx
from packaging.version import Version

logger = logging.getLogger(__name__)

_PLUGIN_VERSION_RE = re.compile(r'STREAMLINK_TTVLOL_VERSION\s*=\s*"([^"]+)"')
_PLUGIN_RELEASES_URL = "https://api.github.com/repos/2bc4/streamlink-ttvlol/releases/latest"
_PLUGIN_DOWNLOAD_URL = "https://github.com/2bc4/streamlink-ttvlol/releases/download/{tag}/twitch.py"
_PYPI_STREAMLINK_URL = "https://pypi.org/pypi/streamlink/json"
_STREAMLINK_RELEASE_NOTES_URL = "https://api.github.com/repos/streamlink/streamlink/releases/tags/{tag}"
_APP_BRANCH = "main"
_MAX_CHANGELOG_CHARS = 600
_MAX_CHANGELOG_COMMITS = 10


def _changelog_lines(body, limit=_MAX_CHANGELOG_CHARS):
    """Normalize a release-notes body into a truncated list of non-empty lines."""
    lines = [ln.strip() for ln in (body or "").splitlines()]
    lines = [ln for ln in lines if ln]
    out = []
    total = 0
    for ln in lines:
        total += len(ln) + 1
        if total > limit:
            if out:
                out.append("…")
            break
        out.append(ln)
    return out


async def _default_run_cmd(cmd, cwd):
    """Run a command, returning (returncode, stdout, stderr). Never raises."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except (TimeoutError, OSError) as e:
        return (1, "", str(e))
    return (proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace"))


class UpdateChecker:
    """Periodic update checks (app repo, streamlink, vendored plugin) and /update.

    Checks are read-only and never raise; ``apply`` applies git pull / uv lock /
    plugin download for the sources reported as ``"update"``.
    """

    def __init__(self, config, notifier, run_cmd=None, http=None):
        self._config = config
        self._notifier = notifier
        self._workdir = Path(config["_workdir"])
        self._run_cmd = run_cmd or _default_run_cmd
        # GitHub release assets 302-redirect to release-assets.githubusercontent.com,
        # so redirects must be followed or the plugin download always fails.
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=10), follow_redirects=True
        )
        self._lock = asyncio.Lock()
        self._state_path = self._workdir / "update_state.json"
        self._state = {}

    def _plugin_path(self):
        plugin_dir = self._config["plugin_dir"]
        if not os.path.isabs(plugin_dir):
            plugin_dir = self._workdir / plugin_dir
        return Path(plugin_dir) / "twitch.py"

    # ---- state -------------------------------------------------------------

    def _load_state(self):
        try:
            with open(self._state_path) as f:
                self._state = json.load(f)
        except FileNotFoundError:
            self._state = {}
        except json.JSONDecodeError:
            logger.warning("[updater] update_state.json corrupt; starting fresh")
            self._state = {}

    def _save_state(self):
        tmp = Path(str(self._state_path) + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self._state, f, indent=2)
        os.replace(tmp, self._state_path)

    # ---- checks ------------------------------------------------------------

    async def local_sha(self):
        rc, out, _ = await self._run_cmd(["git", "rev-parse", "HEAD"], self._workdir)
        return out.strip() if rc == 0 else None

    async def _check_app(self):
        local = await self.local_sha()
        rc, _, err = await self._run_cmd(["git", "fetch", "origin"], self._workdir)
        if rc != 0:
            logger.warning("[updater] app update check failed: %s", err)
            return {"status": "unknown", "local": local, "remote": None, "behind": 0, "subject": None}
        rc, out, _ = await self._run_cmd(["git", "rev-parse", "FETCH_HEAD"], self._workdir)
        remote = out.strip() if rc == 0 else None
        rc, out, _ = await self._run_cmd(["git", "rev-list", "--count", "HEAD..FETCH_HEAD"], self._workdir)
        try:
            behind = int(out.strip()) if rc == 0 else 0
        except ValueError:
            behind = 0
        rc, out, _ = await self._run_cmd(["git", "log", "-1", "--format=%s", "FETCH_HEAD"], self._workdir)
        subject = out.strip() if rc == 0 else None
        rc, out, _ = await self._run_cmd(["git", "log", "--format=%s", "HEAD..FETCH_HEAD"], self._workdir)
        commits = [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []
        if len(commits) > _MAX_CHANGELOG_COMMITS:
            extra = len(commits) - _MAX_CHANGELOG_COMMITS
            commits = commits[:_MAX_CHANGELOG_COMMITS] + [f"…and {extra} more"]
        status = "update" if behind > 0 else "up_to_date"
        return {
            "status": status,
            "local": local,
            "remote": remote,
            "behind": behind,
            "subject": subject,
            "changelog": commits,
        }

    def _plugin_version(self):
        try:
            content = self._plugin_path().read_text(errors="replace")
        except OSError:
            return None
        m = _PLUGIN_VERSION_RE.search(content)
        return m.group(1) if m else None

    async def _check_plugin(self):
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
            return {"status": "unknown", "current": current, "latest": None, "digest": None}
        digest = None
        for asset in data.get("assets", []):
            if asset.get("name") == "twitch.py":
                d = asset.get("digest") or ""
                if d.startswith("sha256:"):
                    digest = d
                break
        if current is None:
            logger.warning("[updater] plugins/twitch.py not found or version constant missing")
            return {"status": "unknown", "current": None, "latest": tag, "digest": digest}
        status = "up_to_date" if current == tag else "update"
        return {
            "status": status,
            "current": current,
            "latest": tag,
            "digest": digest,
            "changelog": data.get("body") or None,
        }

    def _installed_streamlink(self):
        try:
            return importlib.metadata.version("streamlink")
        except importlib.metadata.PackageNotFoundError:
            return None

    async def _check_streamlink(self):
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

    async def _fetch_release_notes(self, repo, tag):
        """Best-effort GitHub release notes for a tag; None on any failure."""
        try:
            resp = await self._http.get(_STREAMLINK_RELEASE_NOTES_URL.format(tag=tag))
            resp.raise_for_status()
            return resp.json().get("body") or None
        except Exception as e:
            logger.warning("[updater] changelog fetch failed for %s %s: %s", repo, tag, e)
            return None

    # ---- check / notify ----------------------------------------------------

    async def check(self, notify):
        async with self._lock:
            uc = self._config.get("update_check") or {}
            report = {}
            if uc.get("check_app", True):
                report["app"] = await self._check_app()
            if uc.get("check_streamlink", True):
                report["streamlink"] = await self._check_streamlink()
            if uc.get("check_plugin", True):
                report["plugin"] = await self._check_plugin()

            if not notify:
                return report

            self._load_state()
            lines = []
            state_changed = False
            for source, data in report.items():
                latest = data.get("remote") if source == "app" else data.get("latest")
                if latest is None:
                    continue
                if data["status"] == "update":
                    if self._state.get(source) != latest:
                        if source == "app":
                            lines.append(
                                f'• stream-archive: {data["behind"]} new commit(s) — "{data["subject"] or ""}"'
                            )
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
                text = (
                    "📦 Update available for stream-archive\n"
                    + "\n".join(lines)
                    + "\nReply /update to apply (service restarts)."
                )
                await self._notifier.notify(text)
            if state_changed:
                self._save_state()
            return report

    # ---- apply -------------------------------------------------------------

    async def apply(self, report):
        async with self._lock:
            results = {}
            for source in ("app", "plugin", "streamlink"):
                data = report.get(source) or {}
                if data.get("status") != "update":
                    results[source] = ("skipped", "no update available")
                    continue
                try:
                    if source == "app":
                        results[source] = await self._apply_app(data)
                    elif source == "plugin":
                        results[source] = await self._apply_plugin(data)
                    else:
                        results[source] = await self._apply_streamlink(data)
                except Exception as e:
                    logger.exception("[updater] %s apply failed", source)
                    results[source] = ("failed", str(e))
            return results

    async def _apply_app(self, data):
        rc, _, err = await self._run_cmd(["git", "pull", "--ff-only"], self._workdir)
        if rc != 0:
            detail = err.strip().splitlines()[0] if err.strip() else "unknown error"
            return ("failed", f"git pull: {detail}")
        return ("applied", f"pulled {data.get('behind', 0)} commit(s) — {data.get('subject') or ''}")

    async def _apply_plugin(self, data):
        latest = data.get("latest")
        if not latest:
            return ("failed", "no latest release tag known")
        current = data.get("current")
        if current is None:
            return ("failed", "plugins/twitch.py not found — cannot replace")
        path = self._plugin_path()
        rc, _, _ = await self._run_cmd(["git", "diff", "--quiet", "--", "plugins/twitch.py"], self._workdir)
        dirty = rc != 0
        if dirty:
            (path.with_suffix(path.suffix + ".bak")).write_bytes(path.read_bytes())
        try:
            resp = await self._http.get(_PLUGIN_DOWNLOAD_URL.format(tag=latest))
            resp.raise_for_status()
            content = resp.content
        except Exception as e:
            return ("failed", str(e))
        digest = data.get("digest")
        if digest:
            expected = digest.removeprefix("sha256:")
            if hashlib.sha256(content).hexdigest() != expected:
                return ("failed", "sha256 mismatch — download rejected")
        tmp = Path(str(path) + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)
        msg = f"plugins/twitch.py replaced ({current} → {latest})"
        if dirty:
            msg += "; previous version backed up to plugins/twitch.py.bak"
        return ("applied", msg)

    async def _apply_streamlink(self, data):
        rc, _, err = await self._run_cmd(["uv", "lock", "--upgrade-package", "streamlink"], self._workdir)
        if rc != 0:
            detail = err.strip().splitlines()[0] if err.strip() else "unknown error"
            return ("failed", f"uv lock: {detail}")
        return ("applied", f"uv.lock updated ({data.get('current')} → {data.get('latest')}); venv syncs on restart")

    # ---- loop / lifecycle --------------------------------------------------

    async def run_loop(self):
        while True:
            try:
                uc = self._config.get("update_check") or {}
                if uc.get("enabled", True):
                    await self.check(notify=True)
            except Exception:
                logger.exception("[updater] update check cycle failed")
            interval = (self._config.get("update_check") or {}).get("interval_hours", 24) * 3600
            await asyncio.sleep(interval)

    async def close(self):
        await self._http.aclose()
