import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from stream_archive import disk
from stream_archive.config import (
    AppConfig,
    bare_name,
    is_kick_channel,
    kick_bare_name,
)

logger = logging.getLogger(__name__)


class DiskOutputMixin:
    _config: AppConfig
    _recordings: dict[str, dict[str, Any]]
    _abort: Any

    def _channel_dir(self, channel: str) -> str:
        """Recording subdirectory: kick/<slug>, twitch/<name>, legacy bare -> bare."""
        if is_kick_channel(channel):
            return f"kick/{kick_bare_name(channel)}"
        if channel.startswith("twitch:"):
            return f"twitch/{bare_name(channel)}"
        return channel

    async def _record_disk(self, channel: str, filepath: str, stream: Any) -> None:
        logger.info("[recorder] [disk] %s -> %s", channel, filepath)
        loop = asyncio.get_running_loop()
        try:
            fd = await loop.run_in_executor(None, stream.open)
            with open(filepath, "wb") as f:
                while True:
                    data = await loop.run_in_executor(None, fd.read, 65536)
                    if not data:
                        break
                    f.write(data)
            logger.info("[recorder] [disk] %s finished", channel)
        except asyncio.CancelledError:
            logger.info("[recorder] [disk] %s cancelled", channel)
        except Exception as e:
            logger.error("[recorder] [disk] %s error: %s", channel, e)
            raise
        finally:
            with contextlib.suppress(Exception):
                fd.close()

    async def _read_ffmpeg_stderr(self, channel: str, process: Any) -> None:
        if process.stderr is None:
            return
        try:
            async for line in process.stderr:
                text = line.decode(errors="replace").strip()
                if text and "Resumed reading" not in text:
                    logger.info("[recorder] [ffmpeg:%s] %s", channel, text)
        except asyncio.CancelledError:
            pass

    async def _watch_growth(self, channel: str) -> None:
        try:
            cfg = self._config.disk
            interval = cfg.check_interval_s
            while True:
                await asyncio.sleep(interval)
                entry = self._recordings.get(channel)
                if entry is None:
                    return
                snap = await disk.disk_snapshot(self._config)
                cap = cfg.max_total_gb
                if cap > 0 and snap["dir_gb"] >= cap:
                    if cfg.delete_oldest:
                        await self.delete_oldest_to_cap()
                        snap = await disk.disk_snapshot(self._config)
                    if snap["dir_gb"] >= cap:
                        await self._abort(channel, f"recording archive at {cap:g} GB cap")
                        return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[recorder] [%s] watchdog error: %s", channel, e)

    async def delete_oldest_to_cap(self) -> tuple[int, int]:
        """Delete oldest .ts files until under disk.max_total_gb; returns (files_removed, freed_gb)."""
        cap = self._config.disk.max_total_gb
        if cap <= 0:
            return (0, 0)
        loop = asyncio.get_running_loop()

        # Never unlink a file that is being written right now: deleting the
        # active target loses the live recording even though ffmpeg keeps its
        # fd open and the space is only reclaimed after teardown.
        active = {os.path.realpath(e["filepath"]) for e in self._recordings.values() if e.get("filepath")}

        def _delete_oldest() -> tuple[int, int]:
            base = disk.recording_dir_path(self._config)
            if not base.exists():
                return (0, 0)
            stats = []
            for p in base.rglob("*.ts"):
                try:
                    st = p.stat()
                except OSError:
                    continue  # retention cleanup may have raced us mid-scan
                stats.append((st.st_mtime, st.st_size, p))
            stats.sort(key=lambda t: t[0])
            total = sum(size for _, size, _ in stats)
            cap_bytes = int(cap * 1024**3)
            removed = freed = 0
            for _, size, p in stats:
                if total < cap_bytes:
                    break
                if os.path.realpath(p) in active:
                    continue
                p.unlink(missing_ok=True)
                total -= size
                removed += 1
                freed += size
                logger.info("[recorder] Deleted oldest to stay under %s GB cap: %s", cap, p)
            return removed, freed

        return await loop.run_in_executor(None, _delete_oldest)

    async def cleanup_old_recordings(self, retention_days: float) -> int:
        """Delete .ts and .chat.json files older than retention_days days; returns count removed."""
        if retention_days <= 0:
            return 0
        base = disk.recording_dir_path(self._config)
        chat_base = disk.chat_dir_path(self._config)
        if not base.exists() and not chat_base.exists():
            return 0
        cutoff = time.time() - retention_days * 86400
        loop = asyncio.get_running_loop()

        def _scan() -> list[Path]:
            found: list[Path] = []
            if base.exists():
                for path in base.rglob("*.ts"):
                    try:
                        if path.stat().st_mtime < cutoff:
                            found.append(path)
                    except OSError:
                        continue
            if chat_base.exists():
                for path in chat_base.rglob("*.chat.json"):
                    try:
                        if path.stat().st_mtime < cutoff:
                            found.append(path)
                    except OSError:
                        continue
            return found

        removed = 0
        try:
            for path in await loop.run_in_executor(None, _scan):
                path.unlink(missing_ok=True)
                removed += 1
                logger.info("[recorder] Removed expired recording: %s", path)
        except OSError as e:
            logger.error("[recorder] Cleanup failed: %s", e)
        return removed

    async def disk_snapshot(self) -> dict[str, Any]:
        return await disk.disk_snapshot(self._config)
