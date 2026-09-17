import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

from stream_archive import disk
from stream_archive.config import (
    AppConfig,
    bare_name,
    is_kick_channel,
    kick_bare_name,
)
from stream_archive.recorder.common import _close_late_stream, _redact_credentials
from stream_archive.recorder.types import Recording

logger = logging.getLogger(__name__)


def _archive_files(recording_base: Path, chat_base: Path) -> Iterator[Path]:
    """Yield every deletable archive file: recordings and chat files."""
    if recording_base.exists():
        yield from disk.iter_recordings(recording_base)
    if chat_base.exists():
        yield from disk.iter_chat_files(chat_base)


class DiskOutputMixin:
    _config: AppConfig
    _recordings: dict[str, Recording]
    # Set by Recorder (core.py). Typed as the exact call shape so a
    # signature drift fails type checks instead of failing at runtime.
    _abort: Callable[[str, str], Coroutine[Any, Any, None]]

    def _channel_dir(self, channel: str) -> str:
        """Return the recording subdirectory (kick/<slug>, twitch/<name>, else bare)."""
        if is_kick_channel(channel):
            return f"kick/{kick_bare_name(channel)}"
        if channel.startswith("twitch:"):
            return f"twitch/{bare_name(channel)}"
        return channel

    async def _record_disk(self, channel: str, filepath: str, stream: Any) -> None:
        logger.info("[recorder] [disk] %s -> %s", channel, filepath)
        loop = asyncio.get_running_loop()
        fd: Any = None
        f: Any = None
        try:
            # Shield the open. On cancellation the worker thread keeps
            # running, so the callback closes the handle it returns. Without
            # the shield, that handle is dropped and only the garbage
            # collector closes it.
            open_future = asyncio.ensure_future(loop.run_in_executor(None, stream.open))
            try:
                fd = await asyncio.shield(open_future)
            except asyncio.CancelledError:
                open_future.add_done_callback(_close_late_stream)
                raise
            # Open on the loop thread. A cancellation between an executor
            # call and its return drops the file object, and the garbage
            # collector then reports an open handle. One open() call costs
            # little, and the copy loop below stays on the executor.
            f = open(filepath, "wb")  # noqa: SIM115
            while True:
                data = await loop.run_in_executor(None, fd.read, 65536)
                if not data:
                    break
                await loop.run_in_executor(None, f.write, data)
            # Close inside the try, on the executor, before the "finished"
            # log. A failed final flush then reaches the handler below, which
            # logs and re-raises it, so the recording counts as failed.
            await loop.run_in_executor(None, f.close)
            f = None
            logger.info("[recorder] [disk] %s finished", channel)
        except asyncio.CancelledError:
            logger.info("[recorder] [disk] %s cancelled", channel)
            raise
        except Exception as e:
            logger.error("[recorder] [disk] %s error: %s", channel, e)
            raise
        finally:
            # Safety net for the error paths. The success path closes f
            # inside the try.
            if f is not None:
                try:
                    f.close()
                except OSError as e:
                    logger.error("[recorder] [disk] %s close failed: %s", channel, e)
            with contextlib.suppress(Exception):
                fd.close()

    async def _read_ffmpeg_stderr(self, channel: str, process: Any) -> None:
        if process.stderr is None:
            return
        # Let a cancellation propagate to the awaiter.
        async for line in process.stderr:
            text = line.decode(errors="replace").strip()
            if text and "Resumed reading" not in text:
                # ffmpeg can echo the output URL, which holds the
                # YouTube stream key.
                logger.info("[recorder] [ffmpeg:%s] %s", channel, _redact_credentials(text))

    async def _watch_growth(self, channel: str) -> None:
        while True:
            try:
                # Read the config each tick: a config reload replaces
                # self._config.disk with a fresh deep copy, and the live
                # values must win.
                cfg = self._config.disk
                await asyncio.sleep(cfg.check_interval_s)
                entry = self._recordings.get(channel)
                if entry is None:
                    return
                cap = cfg.max_total_gb
                if cap <= 0:
                    continue  # cap disabled: no snapshot, no archive walk
                snap = await disk.disk_snapshot(self._config)
                if snap["archive_gb"] >= cap:
                    if cfg.delete_oldest:
                        await self.delete_oldest_to_cap()
                    # The abort decision needs a live total, never the cached
                    # one: a stale value must not stop a healthy recording.
                    disk.invalidate_snapshot()
                    snap = await disk.disk_snapshot(self._config)
                    if snap["archive_gb"] >= cap:
                        await self._abort(channel, f"recording archive at {cap:g} GB cap")
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # One transient fault must not disable the cap for the rest of
                # the recording, so log it and check again next tick.
                logger.error("[recorder] [%s] watchdog error: %s", channel, e)

    def _active_paths(self) -> set[str]:
        """Real paths that live captures hold open.

        A deletion pass must never unlink a file that a recording writes
        through an open handle. That covers the recording file, the chat
        file, and the in-progress chat `.tmp` file.
        """
        active: set[str] = set()
        for e in self._recordings.values():
            filepath = e.get("filepath")
            if filepath:
                active.add(os.path.realpath(filepath))
            chat_recorder = e.get("chat_recorder")
            if chat_recorder is not None:
                active.add(os.path.realpath(chat_recorder.chat_path))
                active.add(os.path.realpath(chat_recorder.chat_path + ".tmp"))
            state = e.get("kick_chat")
            if state is not None:
                active.add(os.path.realpath(state["path"]))
                writer = state.get("writer")
                if writer is not None:
                    active.add(os.path.realpath(writer.tmp_path))
        return active

    async def delete_oldest_to_cap(self) -> tuple[int, int]:
        """Delete the oldest archive files until under disk.max_total_gb.

        The candidates are the recordings and the chat files. A live capture
        (recording file, chat file, or in-progress chat `.tmp` file) is never
        a candidate. Returns (files_removed, freed_bytes).
        """
        cap = self._config.disk.max_total_gb
        if cap <= 0:
            return (0, 0)
        loop = asyncio.get_running_loop()

        def _scan() -> list[tuple[float, int, Path]]:
            base = disk.resolve_recording_dir(self._config)
            chat_base = disk.chat_dir_path(self._config)
            stats = []
            for path in _archive_files(base, chat_base):
                try:
                    st = path.stat()
                except OSError:
                    continue  # retention cleanup can race us mid-scan
                stats.append((st.st_mtime, st.st_size, path))
            stats.sort(key=lambda t: t[0])
            return stats

        stats = await loop.run_in_executor(None, _scan)
        # A capture can start while the scan runs in the worker thread. The
        # loop thread starts every capture, so the active set read here stays
        # valid for the deletion pass below.
        active = self._active_paths()
        total = sum(size for _, size, _ in stats)
        cap_bytes = int(cap * 1024**3)
        removed = freed = 0
        for _, size, path in stats:
            if total < cap_bytes:
                break
            if os.path.realpath(path) in active:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("[recorder] Failed to delete %s: %s", path, e)
                continue
            total -= size
            removed += 1
            freed += size
            logger.info("[recorder] Deleted oldest to stay under %s GB cap: %s", cap, path)
        # The pass measured the archive and can have changed it, so drop any
        # cached snapshot. The caller may re-check the cap straight after.
        disk.invalidate_snapshot()
        return (removed, freed)

    async def cleanup_old_recordings(self, retention_days: float) -> int:
        """Delete recordings and chat files older than retention_days days.

        The chat scan includes stale `.tmp` files, so a chat capture that
        ended in a crash does not linger. Returns the number of files removed.
        """
        if retention_days <= 0:
            return 0
        base = disk.resolve_recording_dir(self._config)
        chat_base = disk.chat_dir_path(self._config)
        if not base.exists() and not chat_base.exists():
            return 0
        cutoff = time.time() - retention_days * 86400
        loop = asyncio.get_running_loop()

        def _scan() -> list[Path]:
            found: list[Path] = []
            for path in _archive_files(base, chat_base):
                try:
                    if path.stat().st_mtime < cutoff:
                        found.append(path)
                except OSError:
                    continue
            return found

        removed = 0
        try:
            expired = await loop.run_in_executor(None, _scan)
            # A stalled feed can sit past retention_days while still writing.
            # Never unlink that in-flight file. Its fd stays open, and removal
            # would cut off the live capture mid-write. The same rule covers
            # the chat files and their in-progress `.tmp` files. Read the
            # active set here, because a capture can start while the scan
            # runs in the worker thread.
            active = self._active_paths()
            for path in expired:
                if os.path.realpath(path) in active:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("[recorder] Failed to delete %s: %s", path, e)
                    continue
                removed += 1
                logger.info("[recorder] Removed expired recording: %s", path)
        except OSError as e:
            logger.error("[recorder] Cleanup failed: %s", e)
        # The pass measured the archive and can have changed it, so drop any
        # cached snapshot.
        disk.invalidate_snapshot()
        return removed

    async def disk_snapshot(self) -> dict[str, Any]:
        return await disk.disk_snapshot(self._config)
