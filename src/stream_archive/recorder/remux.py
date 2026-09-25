"""Remux helper: stream-copy a finished .ts capture into a playable .mp4.

Telegram plays MP4 inline (faststart plus streaming); MPEG-TS arrives as a
download-only document. The codecs copy across untouched, so the run costs
disk I/O only: ~15 s for a 1.2 GB file on this host, near-zero CPU.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stream_archive.disk import LEGACY_REMUX_TMP_SUFFIX, TMP_DIRNAME

logger = logging.getLogger(__name__)

#: Suffixes the remux accepts as input. Only .ts recordings need it.
REMUXABLE_SUFFIXES = (".ts",)

#: File inside the data tmp folder that lists captures left for the boot
#: repair. Shutdown skips the remux, so it records the kept `.ts` files
#: here first: a remux that never started leaves no scratch behind.
PENDING_FILENAME = "pending.json"


def _scratch_path(source: Path, workdir: str | Path | None) -> Path:
    """Scratch output of one remux: inside the data tmp folder, never the archive.

    The path mirrors the source below the tmp folder, so two channels with
    the same file name never share one scratch file. Without a workdir, or
    with a source outside it, the scratch sits in a `.tmp` folder beside
    the source. Both sit on the same filesystem, so the final move is atomic.
    """
    name = source.stem + ".mp4"
    if workdir is not None:
        try:
            rel = source.parent.relative_to(Path(workdir))
        except ValueError, OSError, RuntimeError:
            pass
        else:
            return Path(workdir) / TMP_DIRNAME / rel / name
    return source.parent / ".tmp" / name


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


def remux_ts_to_mp4(source: str | Path, workdir: str | Path | None = None) -> Path | None:
    """Remux a finished .ts capture to .mp4, replacing the source.

    The scratch file lives in the data tmp folder, never beside the
    source: a killed run leaves no partial file in the archive listings.
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
    tmp = _scratch_path(src, workdir)
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("[remux] scratch dir failed for %s: %s", src, e)
        return None
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


#: Width of the cached thumbnail images. Height follows the source ratio.
THUMBNAIL_WIDTH = 640

#: Seek positions tried for one thumbnail, in seconds. Short captures miss
#: the first mark, so a second grab near the start covers them.
_THUMBNAIL_SEEKS = (30, 1)


def capture_thumbnail(source: str | Path, dest: str | Path) -> bool:
    """Grab one 320px frame of ``source`` into ``dest``. True on success.

    Best effort: a missing file, a missing ffmpeg, or a short capture
    leaves no thumbnail and logs, so the recording never depends on it.
    """
    src = Path(source)
    out = Path(dest)
    try:
        if not src.is_file():
            return False
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    if not ffmpeg_available():
        logger.warning("[remux] ffmpeg missing, no thumbnail for %s", src.name)
        return False
    for mark in _THUMBNAIL_SEEKS:
        try:
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    str(mark),
                    "-i",
                    str(src),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={THUMBNAIL_WIDTH}:-1",
                    str(out),
                ],
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("[remux] thumbnail ffmpeg failed for %s: %s", src.name, e)
            return False
        if proc.returncode == 0 and _thumb_ok(out):
            return True
    with contextlib.suppress(OSError):
        out.unlink(missing_ok=True)
    logger.warning("[remux] thumbnail failed for %s", src.name)
    return False


def _thumb_ok(path: Path) -> bool:
    """True when ``path`` is a non-empty JPEG file."""
    try:
        with open(path, "rb") as f:
            head = f.read(3)
        return head.startswith(b"\xff\xd8\xff") and path.stat().st_size > 0
    except OSError:
        return False


async def remux_ts_to_mp4_async(source: str | Path, workdir: str | Path | None = None) -> Path | None:
    """Executor offload of :func:`remux_ts_to_mp4`. Never raises."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, remux_ts_to_mp4, source, workdir)
    except Exception:
        logger.warning("[remux] background remux failed for %s", source, exc_info=True)
        return None


def record_pending(workdir: str | Path, sources: list[str]) -> None:
    """List `.ts` files for the boot repair. Never raises.

    Shutdown calls this before it stops the captures without a remux.
    The boot sweep remuxes the listed files. Entries that vanish or turn
    out live are skipped there.
    """
    try:
        tmp_root = Path(workdir) / TMP_DIRNAME
        tmp_root.mkdir(parents=True, exist_ok=True)
        (tmp_root / PENDING_FILENAME).write_text(json.dumps([s for s in sources if s]))
    except OSError:
        logger.warning("[remux] pending list not stored, boot repair skips %d file(s)", len(sources))


def find_pending_remuxes(base: str | Path, workdir: str | Path | None = None) -> list[Path]:
    """`.ts` files whose last remux died with the process, oldest first.

    Evidence is a leftover scratch file or a shutdown pending list: a
    legacy `*.remux.tmp.mp4` beside the source, a file under the data tmp
    folder that maps back to the `.ts`, or an entry in the pending file
    that shutdown wrote. Scratch with no live `.ts` is garbage and goes
    at once, as do empty tmp folders. A `.ts` whose finished `.mp4`
    already probes clean needs no new remux: the source goes like a
    normal run. Live captures never belong here: the caller skips active
    paths before it remuxes.
    """
    root = Path(base)
    found: dict[str, Path] = {}

    def _note(ts: Path, tmp: Path | None) -> None:
        try:
            if not ts.is_file():
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)
                return
        except OSError:
            return
        target = remux_target(ts)
        try:
            has_target = target.is_file()
        except OSError:
            return
        if has_target:
            if _probe_ok(target):
                for p in (ts,) if tmp is None else (ts, tmp):
                    with contextlib.suppress(OSError):
                        p.unlink(missing_ok=True)
                return
            logger.warning("[remux] cached %s failed its probe, remuxing again", target)
        try:
            key = str(ts.resolve())
        except OSError:
            key = str(ts)
        found.setdefault(key, ts)

    try:
        legacy = [p for p in root.rglob(f"*{LEGACY_REMUX_TMP_SUFFIX}") if p.is_file()]
    except OSError:
        legacy = []
    for tmp in legacy:
        _note(tmp.with_name(tmp.name[: -len(LEGACY_REMUX_TMP_SUFFIX)] + ".ts"), tmp)

    if workdir is not None:
        tmp_root = Path(workdir) / TMP_DIRNAME
        pending_file = tmp_root / PENDING_FILENAME
        try:
            raw = pending_file.read_bytes()
        except OSError:
            raw = b""
        if raw:
            try:
                entries = json.loads(raw)
            except ValueError:
                entries = []
            if isinstance(entries, list):
                for item in entries:
                    if isinstance(item, str) and item:
                        _note(Path(item), None)
            with contextlib.suppress(OSError):
                pending_file.unlink(missing_ok=True)
        try:
            leftovers = [p for p in tmp_root.rglob("*.mp4") if p.is_file()] if tmp_root.is_dir() else []
        except OSError:
            leftovers = []
        for tmp in leftovers:
            _note(Path(workdir) / tmp.parent.relative_to(tmp_root) / (tmp.stem + ".ts"), tmp)
        with contextlib.suppress(OSError):
            for dirpath, dirnames, filenames in os.walk(tmp_root, topdown=False):
                if not dirnames and not filenames:
                    Path(dirpath).rmdir()

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(found.values(), key=_mtime)


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
