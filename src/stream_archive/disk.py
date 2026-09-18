import asyncio
import logging
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from stream_archive.config import AppConfig

logger = logging.getLogger(__name__)


def _resolve_dir(config: AppConfig, raw: str) -> Path:
    """Resolve one configured directory against _workdir when relative.

    This is the single rule for recording_dir and chat_dir. Absolute
    paths pass through unchanged.
    """
    d = Path(raw)
    if not d.is_absolute():
        d = config.workdir / d
    return d


def resolve_recording_dir(config: AppConfig) -> Path:
    """Resolve recording_dir against _workdir when it is relative."""
    return _resolve_dir(config, config.recording_dir)


def chat_dir_path(config: AppConfig) -> Path:
    """Resolve chat_dir against _workdir when relative, like resolve_recording_dir."""
    return _resolve_dir(config, config.chat_dir)


def channel_recording_dir(config: AppConfig, channel_dir: str) -> Path:
    """Recording subdirectory for one channel, resolved against _workdir."""
    return resolve_recording_dir(config) / channel_dir


#: File suffixes of the recording artifacts: video captures, then audio-only.
_RECORDING_SUFFIXES = (".mp4", ".mkv", ".ts", ".m4a")

#: Chat files, plus the in-progress `.tmp` files of the streaming writer.
_CHAT_SUFFIXES = (".chat.json", ".chat.json.tmp")

#: Bounds for the snapshot cache lifetime. The lifetime follows the cap-check
#: interval, so the cache never ages past the refresh of its callers, and a
#: sub-second interval still coalesces the starts of one sweep.
_SNAPSHOT_MIN_TTL_S = 1.0
_SNAPSHOT_MAX_TTL_S = 60.0

#: Timestamp and value of the last snapshot, per archive directory pair.
_snapshot_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def invalidate_snapshot() -> None:
    """Drop the cached snapshot. Call this after a pass that deletes files."""
    _snapshot_cache.clear()


def _snapshot_ttl_s(config: AppConfig) -> float:
    """Return the snapshot cache lifetime: the cap-check interval, in bounds."""
    return min(max(config.disk.check_interval_s, _SNAPSHOT_MIN_TTL_S), _SNAPSHOT_MAX_TTL_S)


def _iter_suffixed(base: Path, suffixes: tuple[str, ...]) -> Iterator[Path]:
    """Yield every file under base whose name ends with one of the suffixes.

    One walk covers every suffix, so the archive is read once per scan.
    """
    for path in base.rglob("*"):
        if path.name.endswith(suffixes) and path.is_file():
            yield path


def iter_recordings(base: Path) -> Iterator[Path]:
    """Yield every recording artifact under base.

    Covers video captures (.ts, .mp4, .mkv) and audio-only captures (.m4a).
    The recorder itself writes only .ts and .m4a. The other two suffixes
    cover a file that an operator remuxed by hand. Chat files (.chat.json)
    are not recording artifacts and stay with the chat cleanup pass.
    """
    yield from _iter_suffixed(base, _RECORDING_SUFFIXES)


def iter_chat_files(base: Path) -> Iterator[Path]:
    """Yield every chat artifact under base, including in-progress chat files.

    The writer creates `<name>.chat.json.tmp` at capture start and renames it
    to `<name>.chat.json` at stop, so both patterns are chat artifacts.
    """
    yield from _iter_suffixed(base, _CHAT_SUFFIXES)


async def disk_snapshot(config: AppConfig) -> dict[str, Any]:
    """Collect filesystem usage and archive directory totals.

    The slow scans run in the default executor. `dir_gb` covers the
    recordings, `chat_gb` covers the chat files, and `archive_gb` is their
    total. The disk watchdog measures `archive_gb` against
    `disk.max_total_gb`.

    The result is cached for one cap-check interval (between 1s and 60s),
    because the monitor and every active recording watchdog ask for the same
    totals. A pass that deletes files calls `invalidate_snapshot()`, so the
    next call measures the smaller archive.

    `usage_ok` is False when the free-space probe failed. The three
    filesystem numbers then hold no meaning, and a caller must show them as
    unknown rather than as zero.
    """
    loop = asyncio.get_running_loop()
    base = resolve_recording_dir(config)
    chat_base = chat_dir_path(config)
    key = f"{base}|{chat_base}"
    cached = _snapshot_cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < _snapshot_ttl_s(config):
        return dict(cached[1])

    def _existing_ancestor() -> Path:
        """Nearest existing ancestor of the recording dir, or the dir itself."""
        fs_dir = base
        while not fs_dir.exists() and fs_dir != fs_dir.parent:
            fs_dir = fs_dir.parent  # missing dir: report the nearest existing ancestor
        return fs_dir

    # Every probe below runs in the executor: on a network-backed archive a
    # single stat can block for a long time and stall the whole event loop.
    fs_dir = await loop.run_in_executor(None, _existing_ancestor)
    usage = None
    try:
        usage = await loop.run_in_executor(None, shutil.disk_usage, fs_dir)
    except OSError:
        # An unmounted archive must not break the monitor or the recorder.
        logger.warning("[disk] disk_usage(%s) failed", fs_dir, exc_info=True)

    def _scan(root: Path, suffixes: tuple[str, ...]) -> tuple[int, int]:
        """Total size and file count under root. A missing root yields zero."""
        total, n = 0, 0
        for p in _iter_suffixed(root, suffixes):
            try:
                total += p.stat().st_size
                n += 1
            except OSError:
                logger.debug("[disk] stat failed for %s", p, exc_info=True)
                continue
        return total, n

    dir_bytes, count = await loop.run_in_executor(None, _scan, base, _RECORDING_SUFFIXES)
    chat_bytes, chat_count = await loop.run_in_executor(None, _scan, chat_base, _CHAT_SUFFIXES)
    result = {
        "dir": str(base),
        # usage_ok False: the probe failed, so the three filesystem numbers
        # below carry no meaning. They stay 0.0 for a caller that formats
        # them without a check.
        "usage_ok": usage is not None,
        "free_gb": round(usage.free / 1024**3, 2) if usage else 0.0,
        "total_fs_gb": round(usage.total / 1024**3, 2) if usage else 0.0,
        "used_fs_gb": round(usage.used / 1024**3, 2) if usage else 0.0,
        "dir_gb": round(dir_bytes / 1024**3, 2),
        "file_count": count,
        "chat_gb": round(chat_bytes / 1024**3, 2),
        "chat_count": chat_count,
        "archive_gb": round((dir_bytes + chat_bytes) / 1024**3, 2),
    }
    _snapshot_cache[key] = (time.monotonic(), result)
    return dict(result)


def format_bytes(n: int) -> str:
    """'3.2 GB' / '512.0 MB' / '48.0 KB' / '123 B'."""
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def format_duration(seconds: float) -> str:
    """Format seconds as zero-padded H:MM:SS, for example '01:23:45'."""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
