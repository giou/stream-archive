"""Remux helper: stream-copy a finished .ts capture into a playable .mp4.

Telegram plays MP4 inline (faststart plus streaming); MPEG-TS arrives as a
download-only document. The codecs copy across untouched, so the run costs
disk I/O only: ~15 s for a 1.2 GB file on this host, near-zero CPU.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Suffixes the remux accepts as input. Only .ts recordings need it.
REMUXABLE_SUFFIXES = (".ts",)


def remux_target(source: Path) -> Path:
    """Output path of the remux: same name, .mp4 suffix."""
    return source.with_suffix(".mp4")


def _run_ffmpeg(source: Path, tmp: Path) -> bool:
    """Stream-copy ``source`` to ``tmp``. True when ffmpeg exits clean."""
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-stats",
                "-i",
                str(source),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(tmp),
            ],
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("[remux] ffmpeg failed for %s: %s", source, e)
        return False
    if proc.returncode != 0:
        logger.warning("[remux] ffmpeg rejected %s: %s", source, proc.stderr.strip()[-500:])
        return False
    return True


def _probe_ok(path: Path) -> bool:
    """True when ``path`` probes as a non-empty video file."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("[remux] ffprobe failed for %s: %s", path, e)
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def remux_ts_to_mp4(source: str | Path) -> Path | None:
    """Remux a finished .ts capture to .mp4, replacing the source.

    Returns the .mp4 path on success, None when nothing changed: wrong
    suffix, missing file, ffmpeg failure, or a bad probe. A failure keeps
    the .ts and logs, so the recording is never lost to a remux.
    """
    src = Path(source)
    if src.suffix.lower() not in REMUXABLE_SUFFIXES:
        return None
    try:
        if not src.is_file():
            return None
    except OSError:
        return None
    target = remux_target(src)
    if target.exists():
        try:
            same = target.resolve() == src.resolve()
        except OSError:
            return None
        if not same:
            # A same-stem .mp4 from an earlier run counts only when it
            # probes clean: a stale or partial file must never cost the
            # original capture. A bad cache falls through to a fresh remux.
            if _probe_ok(target):
                try:
                    src.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("[remux] cleanup of %s failed: %s", src, e)
                return target
            logger.warning("[remux] cached %s failed its probe, remuxing again", target)
    tmp = src.with_name(src.stem + ".remux.tmp.mp4")
    try:
        if not _run_ffmpeg(src, tmp):
            return None
        if not _probe_ok(tmp):
            logger.warning("[remux] probe failed for %s, keeping the .ts", src)
            return None
        tmp.replace(target)
    except OSError as e:
        logger.warning("[remux] replace failed for %s: %s", src, e)
        return None
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    try:
        size = target.stat().st_size
    except OSError as e:
        logger.warning("[remux] verify of %s failed: %s", target, e)
        return None
    logger.info("[remux] %s -> %s (%d bytes)", src.name, target.name, size)
    try:
        src.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("[remux] cleanup of %s failed: %s", src, e)
    return target


async def remux_ts_to_mp4_async(source: str | Path) -> Path | None:
    """Executor offload of :func:`remux_ts_to_mp4`. Never raises."""
    try:
        return await asyncio.get_running_loop().run_in_executor(None, remux_ts_to_mp4, source)
    except Exception:
        logger.warning("[remux] background remux failed for %s", source, exc_info=True)
        return None


def ffmpeg_available() -> bool:
    """True when ffmpeg and ffprobe resolve on PATH."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def split_parts(source: str | Path, chunk_bytes: int | None = None) -> list[Path] | None:
    """Split ``source`` into stream-copy .mp4 chunks under the protocol cap.

    Returns the chunk paths in play order, or None when ffmpeg is missing,
    the file is gone, or a chunk fails its probe or exceeds the cap. A
    failed split deletes its partial chunks. The source file stays: the
    caller deletes the chunks when the upload of every chunk succeeds.
    """
    from stream_archive.mtproto_upload import MAX_UPLOAD_BYTES, SPLIT_BYTES

    if chunk_bytes is None:
        chunk_bytes = SPLIT_BYTES
    src = Path(source)
    try:
        size = src.stat().st_size
    except OSError:
        return None
    if size <= 0 or chunk_bytes <= 0:
        return None
    if size <= MAX_UPLOAD_BYTES:
        return [src]
    if not ffmpeg_available():
        return None
    try:
        duration = _media_duration(src)
    except Exception:
        duration = None
    if not duration or duration <= 0:
        return None
    tmpdir = src.with_name(f"{src.stem}.split")
    try:
        tmpdir.mkdir(exist_ok=True)
    except OSError as e:
        logger.warning("[remux] split dir failed for %s: %s", src, e)
        return None
    count = (size + chunk_bytes - 1) // chunk_bytes
    seg_time = duration / count
    chunks: list[Path] = []
    try:
        for index in range(count):
            out = tmpdir / f"{src.stem}.part{index + 1:02d}of{count:02d}.mp4"
            if not _run_split(src, out, index * seg_time, seg_time if index + 1 < count else None):
                cleanup_split(chunks, tmpdir)
                return None
            if not _chunk_usable(out, MAX_UPLOAD_BYTES):
                with contextlib.suppress(OSError):
                    out.unlink(missing_ok=True)
                cleanup_split(chunks, tmpdir)
                return None
            chunks.append(out)
    except Exception:
        logger.warning("[remux] split failed for %s", src, exc_info=True)
        cleanup_split(chunks, tmpdir)
        return None
    if not chunks:
        return None
    logger.info("[remux] %s -> %d chunks", src.name, len(chunks))
    return chunks


async def split_parts_streaming(
    source: str | Path,
    chunk_bytes: int | None = None,
    on_chunk: Callable[[Path, int, int], Any] | None = None,
    cancel: Any | None = None,
) -> list[Path] | None:
    """Split like :func:`split_parts`, reporting each finished chunk.

    Calls ``on_chunk(path, index, total)`` (sync or async) after each chunk
    probes clean and fits under the cap, so the uploader starts chunk 1
    while ffmpeg still cuts chunk 2. ``cancel`` is an optional
    ``threading.Event``-like with ``is_set()``: when set, the split stops
    after the current chunk and already-cut chunks stay for retry. A
    failure deletes its partial chunks. Returns all chunk paths, or None
    on failure like :func:`split_parts`.
    """
    import asyncio as _asyncio

    from stream_archive.mtproto_upload import MAX_UPLOAD_BYTES, SPLIT_BYTES

    if chunk_bytes is None:
        chunk_bytes = SPLIT_BYTES
    src = Path(source)
    try:
        size = src.stat().st_size
    except OSError:
        return None
    if size <= 0 or chunk_bytes <= 0:
        return None
    if size <= MAX_UPLOAD_BYTES:
        return [src]
    if not ffmpeg_available():
        return None
    loop = _asyncio.get_running_loop()
    try:
        duration = await loop.run_in_executor(None, _media_duration, src)
    except Exception:
        duration = None
    if not duration or duration <= 0:
        return None
    tmpdir = src.with_name(f"{src.stem}.split")
    try:
        tmpdir.mkdir(exist_ok=True)
    except OSError as e:
        logger.warning("[remux] split dir failed for %s: %s", src, e)
        return None
    count = (size + chunk_bytes - 1) // chunk_bytes
    seg_time = duration / count
    chunks: list[Path] = []
    try:
        for index in range(count):
            if cancel is not None and cancel.is_set():
                logger.info("[remux] split of %s cancelled after %d chunks", src.name, len(chunks))
                return chunks or None
            out = tmpdir / f"{src.stem}.part{index + 1:02d}of{count:02d}.mp4"
            ok = await loop.run_in_executor(
                None, _run_split, src, out, index * seg_time, seg_time if index + 1 < count else None
            )
            if not ok:
                # The failed chunk never reached the consumer: drop it, but
                # keep reported chunks. The consumer may still upload them,
                # and the caller owns their cleanup on failure.
                with contextlib.suppress(OSError):
                    out.unlink(missing_ok=True)
                cleanup_split([], tmpdir)
                return None
            if not await loop.run_in_executor(None, _chunk_usable, out, MAX_UPLOAD_BYTES):
                with contextlib.suppress(OSError):
                    out.unlink(missing_ok=True)
                cleanup_split([], tmpdir)
                return None
            chunks.append(out)
            if on_chunk is not None:
                result = on_chunk(out, len(chunks), count)
                if _asyncio.iscoroutine(result):
                    await result
    except Exception:
        logger.warning("[remux] split failed for %s", src, exc_info=True)
        cleanup_split([], tmpdir)
        return None
    if not chunks:
        return None
    logger.info("[remux] %s -> %d chunks", src.name, len(chunks))
    return chunks


def _chunk_usable(out: Path, cap: int) -> bool:
    """True when ``out`` probes clean and fits under the upload cap.

    Time-proportional cuts assume constant bitrate, but VBR spikes and
    keyframe rounding can push one chunk over the cap, and Telegram then
    rejects it at upload time. Fail fast here instead.
    """
    if not _probe_ok(out):
        logger.warning("[remux] probe failed for chunk %s", out)
        return False
    try:
        size = out.stat().st_size
    except OSError:
        logger.warning("[remux] chunk vanished: %s", out)
        return False
    if size > cap:
        logger.warning("[remux] chunk %s over the cap (%d bytes)", out, size)
        return False
    return True


def cleanup_split(chunks: list[Path], tmpdir: Path | None = None) -> None:
    """Delete split chunks and their temp dir. Never raises.

    ``tmpdir`` drops the dir even when ``chunks`` is empty: a failure
    before the first finished chunk would otherwise leave an empty
    ``<stem>.split/`` behind. Callers with a live consumer pass no
    chunks, so in-flight files are never unlinked from under it.
    """
    if tmpdir is None and chunks:
        tmpdir = chunks[0].parent
    for chunk in chunks:
        with contextlib.suppress(OSError):
            chunk.unlink(missing_ok=True)
    if tmpdir is not None and tmpdir.name.endswith(".split"):
        with contextlib.suppress(OSError):
            tmpdir.rmdir()


def _media_duration(path: Path) -> float | None:
    """Duration of ``path`` in seconds via ffprobe, or None."""
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def _run_split(source: Path, out: Path, start: float, length: float | None) -> bool:
    """Stream-copy one ``[start, start+length)`` window into ``out``."""
    import subprocess

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "make_zero",
    ]
    if length is not None:
        cmd += ["-t", f"{length:.3f}"]
    cmd.append(str(out))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("[remux] ffmpeg split failed for %s: %s", source, e)
        return False
    if proc.returncode != 0:
        logger.warning("[remux] ffmpeg split rejected %s: %s", source, proc.stderr.strip()[-500:])
        return False
    return True
