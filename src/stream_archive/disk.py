import asyncio
import logging
import shutil
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


_RECORDING_PATTERNS = ("*.mp4", "*.mkv", "*.ts", "*.m4a", "*.jsonl")

#: Chat files, plus the in-progress `.tmp` files of the streaming writer.
_CHAT_PATTERNS = ("*.chat.json", "*.chat.json.tmp")

#: File suffixes of the pattern tuples above, for the single-pass scans.
_RECORDING_SUFFIXES = tuple(pattern[1:] for pattern in _RECORDING_PATTERNS)
_CHAT_SUFFIXES = tuple(pattern[1:] for pattern in _CHAT_PATTERNS)


def _iter_suffixed(base: Path, suffixes: tuple[str, ...]) -> Iterator[Path]:
    """Yield every file under base whose name ends with one of the suffixes.

    One walk covers every suffix, so the archive is read once per scan.
    """
    for path in base.rglob("*"):
        if path.name.endswith(suffixes) and path.is_file():
            yield path


def iter_recordings(base: Path) -> Iterator[Path]:
    """Yield every recording artifact under base.

    Covers video captures (.ts, .mp4, .mkv), audio-only captures (.m4a),
    and sidecar segment logs (.jsonl). Chat files (.chat.json) are not
    recording artifacts and stay with the chat cleanup pass.
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
    """
    loop = asyncio.get_running_loop()
    base = resolve_recording_dir(config)
    chat_base = chat_dir_path(config)
    fs_dir = base
    while not fs_dir.exists() and fs_dir != fs_dir.parent:
        fs_dir = fs_dir.parent  # missing dir: report the nearest existing ancestor
    usage = None
    try:
        usage = await loop.run_in_executor(None, shutil.disk_usage, fs_dir)
    except OSError:
        # An unmounted archive must not break the monitor or the recorder.
        logger.warning("[disk] disk_usage(%s) failed", fs_dir, exc_info=True)
    dir_bytes, count = 0, 0
    if base.exists():

        def _scan() -> tuple[int, int]:
            total, n = 0, 0
            for p in iter_recordings(base):
                try:
                    total += p.stat().st_size
                    n += 1
                except OSError:
                    logger.debug("[disk] stat failed for %s", p, exc_info=True)
                    continue
            return total, n

        dir_bytes, count = await loop.run_in_executor(None, _scan)
    chat_bytes, chat_count = 0, 0
    if chat_base.exists():

        def _scan_chat() -> tuple[int, int]:
            total, n = 0, 0
            for p in iter_chat_files(chat_base):
                try:
                    total += p.stat().st_size
                    n += 1
                except OSError:
                    logger.debug("[disk] stat failed for %s", p, exc_info=True)
                    continue
            return total, n

        chat_bytes, chat_count = await loop.run_in_executor(None, _scan_chat)
    return {
        "dir": str(base),
        # Unknown free space reports 0, so the callers never divide by None.
        "free_gb": round(usage.free / 1024**3, 2) if usage else 0.0,
        "total_fs_gb": round(usage.total / 1024**3, 2) if usage else 0.0,
        "used_fs_gb": round(usage.used / 1024**3, 2) if usage else 0.0,
        "dir_gb": round(dir_bytes / 1024**3, 2),
        "file_count": count,
        "chat_gb": round(chat_bytes / 1024**3, 2),
        "chat_count": chat_count,
        "archive_gb": round((dir_bytes + chat_bytes) / 1024**3, 2),
    }


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
