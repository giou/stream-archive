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
        d = config._workdir / d
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


def iter_recordings(base: Path) -> Iterator[Path]:
    """Yield every recording artifact under base.

    Covers video captures (.ts, .mp4, .mkv), audio-only captures (.m4a),
    and sidecar segment logs (.jsonl). Chat files (.chat.json) are not
    recording artifacts and stay with the chat cleanup pass.
    """
    for pattern in _RECORDING_PATTERNS:
        yield from base.rglob(pattern)


async def disk_snapshot(config: AppConfig) -> dict[str, Any]:
    """Collect filesystem usage and recordings directory totals.

    The slow recordings scan runs in the default executor.
    """
    loop = asyncio.get_running_loop()
    base = resolve_recording_dir(config)
    fs_dir = base if base.exists() else base.parent  # missing dir: report parent fs
    usage = await loop.run_in_executor(None, shutil.disk_usage, fs_dir)
    dir_bytes, count = 0, 0
    if base.exists():

        def _scan() -> tuple[int, int]:
            total, n = 0, 0
            for p in iter_recordings(base):
                try:
                    total += p.stat().st_size
                    n += 1
                except OSError:
                    continue
            return total, n

        dir_bytes, count = await loop.run_in_executor(None, _scan)
    return {
        "dir": str(base),
        "free_gb": round(usage.free / 1024**3, 2),
        "total_fs_gb": round(usage.total / 1024**3, 2),
        "used_fs_gb": round(usage.used / 1024**3, 2),
        "dir_gb": round(dir_bytes / 1024**3, 2),
        "file_count": count,
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
