import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def recording_dir_path(config) -> Path:
    """Resolve recording_dir against _workdir when relative (same rule as cleanup_old_recordings)."""
    d = Path(config["recording_dir"])
    if not d.is_absolute():
        d = config["_workdir"] / d
    return d


def chat_dir_path(config) -> Path:
    """Resolve chat_dir against _workdir when relative (same rule as recording_dir_path)."""
    d = Path(config.get("chat_dir", "chat"))
    if not d.is_absolute():
        d = config["_workdir"] / d
    return d


async def disk_snapshot(config) -> dict:
    """Fresh fs + recordings-dir usage. Slow .ts scan runs in the default executor."""
    loop = asyncio.get_running_loop()
    base = recording_dir_path(config)
    fs_dir = base if base.exists() else base.parent  # missing dir: report parent fs
    usage = await loop.run_in_executor(None, shutil.disk_usage, fs_dir)
    dir_bytes, count = 0, 0
    if base.exists():
        def _scan():
            total, n = 0, 0
            for p in base.rglob("*.ts"):
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
    """H:MM:SS, zero-padded, e.g. '01:23:45'."""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
