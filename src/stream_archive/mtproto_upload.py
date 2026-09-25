"""MTProto uploader: send recordings past the Bot API 50 MB cap.

The Bot API allows 50 MB uploads. The MTProto client logs in with the same
bot token and sends files up to 2 GiB. This module owns no menus: the bot
and the later web UI share it as plain transport.

Speed: Telethon's libssl wrapper copies every 512 KiB part byte-by-byte in
Python before encrypting (~35 Mbps ceiling, measured). This module patches
``encrypt_ige`` with a ``from_buffer_copy`` variant (~1400 Mbps) at connect
time, and uploads parts over a 16-wide worker pool. Measured end to end:
~170 Mbps to the DC on a 362 Mbps pipe.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from stream_archive.config import AppConfig

logger = logging.getLogger(__name__)

#: Size cap for one MTProto upload, in bytes: 4000 parts of 512 KiB.
#: Measured against the live API: 4000 parts send, 4001 fails with
#: FilePartsInvalidError. Below the 2 GiB headline figure.
MAX_UPLOAD_BYTES = 4000 * 512 * 1024

#: Target size of one split chunk for files over the cap.
SPLIT_BYTES = MAX_UPLOAD_BYTES - 1024 * 1024

#: Bot API upload cap. Files at or below this stay on the Bot API path.
BOT_API_BYTES = 50 * 1024 * 1024

#: Upload chunk size: the 512 KiB protocol maximum. Fewer round-trips per GB.
PART_SIZE = 512 * 1024

#: Parallel part uploads in flight. Each part is an independent RPC on the one
#: connection, so parts overlap instead of waiting for the previous ack.
#: 16 keeps the pipe full at ~25 ms RTT without tripping flood limits.
UPLOAD_WORKERS = 16

#: Files below this size skip the parallel path: one RPC is faster than the
#: gather overhead. Above 10 MB Telegram wants SaveBigFilePart anyway.
PARALLEL_MIN_BYTES = 10 * 1024 * 1024


class ProgressCallback(Protocol):
    """Upload progress: bytes sent over bytes total, plus a phase note.

    ``note`` is "split" while ffmpeg cuts (sent/total counts chunks), or
    "part i/N" while one chunk uploads (sent/total counts bytes). A plain
    two-arg callable also satisfies this protocol.
    """

    def __call__(self, sent: int, total: int, note: str | None = None) -> Any: ...


def check_sendable(path: Path) -> tuple[bool, str]:
    """True plus an empty note when ``path`` can go over MTProto.

    The checks run in size order: missing file, empty file, then the 2 GiB
    cap. The note names the failed check, so the bot can quote it.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False, "the file is gone"
    except OSError as e:
        return False, f"the file is unreadable ({e.strerror or e})"
    if size <= 0:
        return False, "the file is empty"
    if size > MAX_UPLOAD_BYTES:
        from stream_archive.recorder.remux import ffmpeg_available

        if not ffmpeg_available():
            return False, "the file is over the 4000-part cap, Telegram rejects it"
    return True, ""


def _report_progress(progress: ProgressCallback | None, sent: int, total: int, note: str | None = None) -> None:
    """Call ``progress`` with a phase note, tolerating two-arg callables."""
    if progress is None:
        return
    if note is None:
        progress(sent, total)
        return
    try:
        takes_note = len(inspect.signature(progress).parameters) >= 3
    except TypeError, ValueError:
        takes_note = True
    # Never retry on TypeError: an error inside the callback must reach
    # the caller instead of running the callback twice with fewer args.
    if takes_note:
        progress(sent, total, note)
    else:
        progress(sent, total)


def session_path(config: AppConfig) -> Path:
    """Session file path of the MTProto client, resolved against _workdir."""
    p = Path(config.mtproto.session)
    if not p.is_absolute():
        p = config.workdir / p
    return p


class MtprotoUploader:
    """MTProto client for recording uploads, logged in with the bot token."""

    def __init__(self, config: AppConfig, client_factory: Callable[..., Any] | None = None) -> None:
        self._config = config
        self._client_factory = client_factory
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """True when MTProto upload is on and holds its credentials."""
        m = self._config.mtproto
        return bool(m.enabled and m.api_id > 0 and m.api_hash.strip())

    @property
    def connected(self) -> bool:
        """True when the client holds a live session."""
        client = self._client
        if client is None:
            return False
        is_connected = getattr(client, "is_connected", None)
        if callable(is_connected):
            try:
                return bool(is_connected())
            except Exception:
                return False
        return True

    async def connect(self) -> None:
        """Log in with the bot token. No-op when disabled or already live."""
        if not self.enabled:
            return
        if self._client is not None and self.connected:
            return
        async with self._lock:
            if self._client is not None and self.connected:
                return
            if self._client is not None:
                old, self._client = self._client, None
                try:
                    await old.disconnect()
                except Exception:
                    logger.warning("[mtproto] Dead session disconnect failed", exc_info=True)
            factory = self._client_factory
            if factory is None:
                from telethon import TelegramClient  # type: ignore[import-untyped]

                factory = TelegramClient
            client = factory(session_path(self._config), self._config.mtproto.api_id, self._config.mtproto.api_hash)
            _patch_fast_ige()
            try:
                await client.start(bot_token=self._config.bot_telegram_api)
            except Exception:
                try:
                    await client.disconnect()
                except Exception:
                    logger.warning("[mtproto] Temp client disconnect failed", exc_info=True)
                raise
            self._client = client
            logger.info("[mtproto] Connected")

    async def disconnect(self) -> None:
        """Close the session. Never raises: shutdown must finish."""
        async with self._send_lock:
            async with self._lock:
                client, self._client = self._client, None
            if client is None:
                return
            try:
                await client.disconnect()
            except Exception:
                logger.warning("[mtproto] Disconnect failed", exc_info=True)

    async def rebind(self) -> None:
        """Close the session after /reload disabled MTProto."""
        if not self.enabled and self._client is not None:
            await self.disconnect()
            logger.info("[mtproto] Disabled, dropping the session")

    async def _ensure_playable(self, path: Path) -> Path:
        """Return a playable path for ``path``: cached .mp4 beside a .ts."""
        if path.suffix.lower() != ".ts":
            return path
        from stream_archive.recorder.remux import (
            _probe_ok,
            ffmpeg_available,
            remux_target,
            remux_ts_to_mp4_async,
        )

        if not ffmpeg_available():
            return path
        target = remux_target(path)
        try:
            if target.is_file():
                try:
                    same = target.resolve() == path.resolve()
                except OSError:
                    return path
                # Trust a cached .mp4 only when it probes clean, like the
                # remuxer does: a stale file must not cost the good .ts.
                if not same:
                    loop = asyncio.get_running_loop()
                    if await loop.run_in_executor(None, _probe_ok, target):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            logger.warning("[mtproto] Cleanup of %s failed", path, exc_info=True)
                        return target
                    logger.warning("[mtproto] Cached %s failed its probe, keeping %s", target, path)
        except OSError:
            return path
        made = await remux_ts_to_mp4_async(path, self._config.workdir)
        return made if made is not None else path

    @staticmethod
    def _video_send_args(path: Path) -> tuple[Any, str | None]:
        """Video attribute plus thumb path for ``path``, or (None, None)."""
        if path.suffix.lower() != ".mp4":
            return None, None
        try:
            from telethon.tl.types import DocumentAttributeVideo  # type: ignore[import-untyped]
        except ImportError:
            return None, None
        info = _probe_video(path)
        if info is None:
            return None, None
        duration, width, height = info
        thumb = _grab_thumb(path)
        video = DocumentAttributeVideo(
            duration=duration, w=width, h=height, round_message=False, supports_streaming=True
        )
        return video, thumb

    async def _send_split(
        self,
        client: Any,
        path: Path,
        chat_id: int,
        caption: str | None,
        progress: ProgressCallback | None,
    ) -> None:
        """Split ``path`` over the cap into chunks, send each, delete chunks.

        Chunk 1 uploads while ffmpeg still cuts chunk 2: each finished chunk
        queues into ``ready`` and the consumer sends in cut order. Part
        captions carry ``(i/N)`` so the chat reads in play order. Progress
        carries a phase note: "split" while ffmpeg cuts, then "part i/N"
        while one chunk uploads. A cancelled send stops the cutter and the
        sender: cut chunks stay on disk for a retry, and the source file is
        never touched.
        """
        from stream_archive.recorder.remux import cleanup_split, ffmpeg_available, split_parts_streaming

        if not ffmpeg_available():
            msg = f"Cannot send {path.name}: the file is over the 4000-part cap, Telegram rejects it."
            raise ValueError(msg)
        loop = asyncio.get_running_loop()
        base = caption or path.name
        ready: asyncio.Queue[tuple[int, int, Path] | None] = asyncio.Queue()
        state: dict[str, Any] = {"failed": None}
        queued: list[str] = []
        stop = asyncio.Event()

        async def _on_chunk(chunk: Path, index: int, total: int) -> None:
            ready.put_nowait((index, total, chunk))
            queued.append(str(chunk))
            _report_progress(progress, index, total, "split")

        async def _produce() -> list[Path] | None:
            try:
                return await split_parts_streaming(path, on_chunk=_on_chunk, cancel=stop)
            except Exception as e:
                state["failed"] = e
                return None

        async def _consume() -> None:
            while True:
                item = await ready.get()
                if item is None:
                    return
                index, total, chunk = item
                video, thumb = await loop.run_in_executor(None, self._video_send_args, chunk)
                try:
                    label = f"part {index}/{total}"

                    def _part_progress(sent: int, part_total: int, label: str = label) -> None:
                        _report_progress(progress, sent, part_total, label)

                    handle = await _upload_handle(client, chunk, progress=_part_progress, workers=UPLOAD_WORKERS)
                    await client.send_file(
                        chat_id,
                        handle,
                        caption=f"{base} ({index}/{total})",
                        supports_streaming=True,
                        thumb=thumb,
                        attributes=[video] if video is not None else None,
                    )
                finally:
                    if thumb is not None:
                        with contextlib.suppress(OSError):
                            Path(thumb).unlink(missing_ok=True)

        producer = asyncio.create_task(_produce())
        consumer = asyncio.create_task(_consume())
        try:
            chunks = await producer
        except asyncio.CancelledError:
            stop.set()
            producer.cancel()
            consumer.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)
            raise
        if state["failed"] is not None:
            await ready.put(None)
            await consumer
            # The cutter owns reported chunks only while it runs. On a
            # failed split the caller deletes what was already reported.
            cleanup_split([Path(p) for p in queued])
            raise state["failed"]
        if not chunks:
            await ready.put(None)
            await consumer
            cleanup_split([Path(p) for p in queued])
            msg = f"Cannot send {path.name}: the split failed, see logs."
            raise ValueError(msg)
        # A cutter that returns paths without reporting them (or a test
        # double): queue the missing ones so every chunk still sends.
        for position, chunk in enumerate(chunks, 1):
            if str(chunk) not in queued:
                ready.put_nowait((position, len(chunks), chunk))
        await ready.put(None)
        try:
            await consumer
        except asyncio.CancelledError:
            stop.set()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            raise
        except Exception:
            # A failed upload leaves partial chunks: delete them, so a
            # retry cuts fresh ones. A cancelled send keeps them instead.
            cleanup_split(chunks)
            raise
        cleanup_split(chunks)

    async def send_video(
        self,
        path: str | os.PathLike[str],
        chat_id: int,
        caption: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        """Send one recording file to ``chat_id`` over MTProto.

        Raises RuntimeError when disabled or not connected, FileNotFoundError
        when the file is gone, ValueError when it breaks the size cap.
        """
        if not self.enabled:
            msg = "MTProto upload is off. Enable it under Settings, then MTProto upload."
            raise RuntimeError(msg)
        ok, note = check_sendable(Path(path))
        if not ok:
            if not Path(path).exists():
                raise FileNotFoundError(str(path))
            msg = f"Cannot send {Path(path).name}: {note}"
            raise ValueError(msg)
        send_path = await self._ensure_playable(Path(path))
        loop = asyncio.get_running_loop()
        try:
            video, thumb = await loop.run_in_executor(None, self._video_send_args, send_path)
        except Exception:
            logger.warning("[mtproto] Video probe failed for %s", send_path, exc_info=True)
            video, thumb = None, None
        try:
            async with self._send_lock:
                client = self._client
                if client is None:
                    msg = "MTProto is not connected. Try again in a few seconds."
                    raise RuntimeError(msg)
                if send_path.stat().st_size > MAX_UPLOAD_BYTES:
                    await self._send_split(client, send_path, chat_id, caption, progress)
                else:
                    handle = await _upload_handle(client, send_path, progress=progress, workers=UPLOAD_WORKERS)
                    await client.send_file(
                        chat_id,
                        handle,
                        caption=caption,
                        supports_streaming=True,
                        thumb=thumb,
                        attributes=[video] if video is not None else None,
                    )
        finally:
            if thumb is not None:
                with contextlib.suppress(OSError):
                    Path(thumb).unlink(missing_ok=True)


def _probe_video(path: Path) -> tuple[int, int, int] | None:
    """(duration_s, width, height) of ``path`` via ffprobe, or None."""
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,duration",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1",
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
    width = height = 0
    duration = 0.0
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "width" and value.isdigit():
            width = int(value)
        elif key == "height" and value.isdigit():
            height = int(value)
        elif key == "duration":
            with contextlib.suppress(ValueError):
                duration = max(duration, float(value))
    if not width or not height:
        return None
    return int(duration), width, height


def _grab_thumb(path: Path) -> str | None:
    """Grab a 320px JPEG thumb of ``path`` into a temp file. Caller deletes it."""
    import subprocess
    import tempfile

    try:
        fd, tmp = tempfile.mkstemp(suffix=".jpg")
    except OSError:
        return None
    import os as _os

    _os.close(fd)
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "5",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:-1",
                tmp,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except OSError, subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        return None
    if proc.returncode != 0:
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        return None
    try:
        if Path(tmp).stat().st_size > 0:
            return tmp
    except OSError:
        pass
    with contextlib.suppress(OSError):
        Path(tmp).unlink(missing_ok=True)
    return None


async def _upload_handle(
    client: Any, path: Path, progress: Callable[..., Any] | None = None, workers: int = UPLOAD_WORKERS
) -> Any:
    """Upload ``path`` with parallel parts, return the file handle for send.

    Falls back to the serial ``upload_file`` when the file is small or the
    client is a test double without raw-call support.
    """
    size = path.stat().st_size
    if size < PARALLEL_MIN_BYTES or not callable(client):
        return await client.upload_file(path, part_size_kb=512, progress_callback=progress)
    parts = await _upload_parallel([client], path, size, progress, workers)
    from telethon.tl import types as _types  # type: ignore[import-untyped]

    return _types.InputFileBig(parts["id"], parts["parts"], parts["name"])


async def _upload_parallel(
    clients: list[Any], path: Path, size: int, progress: Callable[..., Any] | None, workers: int
) -> dict[str, Any]:
    """Save every 512 KiB part of ``path`` with ``workers`` in flight per sender.

    Parts round-robin across ``clients``: worker ``i`` of sender ``k`` takes
    every part with ``index % total_workers == i``. The part index still
    addresses the whole file, so the DC reassembles correctly.
    """
    import os as _os

    from telethon.tl import functions as _functions

    per_sender = max(1, workers)
    n = len(clients)
    part_count = (size + PART_SIZE - 1) // PART_SIZE
    file_id = int.from_bytes(_os.urandom(8), "big", signed=True)
    sent = 0
    lock = asyncio.Lock()
    loop = asyncio.get_running_loop()
    # One queue per sender: sender k owns every part with index % n == k, so
    # parts strictly alternate across connections instead of racing one queue.
    queues: list[asyncio.Queue[int | None]] = [asyncio.Queue() for _ in clients]
    for index in range(part_count):
        queues[index % n].put_nowait(index)
    for q in queues:
        for _ in range(per_sender):
            q.put_nowait(None)

    def _read_part(index: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(index * PART_SIZE)
            return f.read(PART_SIZE)

    async def _worker(client: Any, q: asyncio.Queue[int | None]) -> None:
        nonlocal sent
        while True:
            index = await q.get()
            if index is None:
                return
            chunk = await loop.run_in_executor(None, _read_part, index)
            req = _functions.upload.SaveBigFilePartRequest(file_id, index, part_count, chunk)
            ok = await client(req)
            if not ok:
                msg = f"part {index} rejected"
                raise RuntimeError(msg)
            async with lock:
                nonlocal_sent = sent + len(chunk)
                sent = nonlocal_sent
                if progress is not None:
                    _report_progress(progress, nonlocal_sent, size)

    async with asyncio.TaskGroup() as group:
        for client, q in zip(clients, queues, strict=True):
            for _ in range(per_sender):
                group.create_task(_worker(client, q))
    return {"id": file_id, "parts": part_count, "name": path.name}


_patched_ige = False
_patched_gzip = False


def _patch_fast_ige() -> None:
    """Replace Telethon's byte-copying ``encrypt_ige`` with a fast variant.

    Telethon builds ``(ctypes.c_ubyte * n)(*data)`` per call: a Python-level
    loop over every byte of every 512 KiB part (~50 ms of the ~215 ms a part
    costs). The replacement uses ``from_buffer_copy`` (one memcpy) and caches
    nothing else: the key schedule stays per call, like upstream. Idempotent.
    """
    global _patched_ige
    if _patched_ige:
        return
    try:
        from telethon.crypto import libssl as _libssl  # type: ignore[import-untyped]
    except ImportError:
        return
    lib = getattr(_libssl, "_libssl", None)
    if lib is None or getattr(_libssl, "encrypt_ige", None) is None:
        return
    import ctypes as _ctypes

    class _AesKey(_ctypes.Structure):
        _fields_ = [("rd_key", _ctypes.c_uint32 * 60), ("rounds", _ctypes.c_uint)]

    def _fast_ige(plain: bytes, key: bytes, iv: bytes) -> bytes:
        kb = (_ctypes.c_ubyte * 32).from_buffer_copy(key)
        ivb = (_ctypes.c_ubyte * 32).from_buffer_copy(iv)
        expanded = _AesKey()
        lib.AES_set_encrypt_key(kb, 256, _ctypes.byref(expanded))
        inp = (_ctypes.c_ubyte * len(plain)).from_buffer_copy(plain)
        out = (_ctypes.c_ubyte * len(plain))()
        lib.AES_ige_encrypt(inp, out, len(plain), _ctypes.byref(expanded), _ctypes.byref(ivb), 1)
        return bytes(out)

    _libssl.encrypt_ige = _fast_ige
    _patched_ige = True
    _patch_no_gzip_parts()


def _patch_no_gzip_parts() -> None:
    """Skip Telethon's gzip attempt on file-part uploads.

    ``gzip_if_smaller`` runs on every content-related request over 512 bytes.
    Video bytes never compress, so each 512 KiB part burns ~13 ms gzipping
    only to discard the result. SaveBigFilePart payloads are always opaque
    media bytes: return them as-is. Idempotent.
    """
    global _patched_gzip
    if _patched_gzip:
        return
    try:
        from telethon.tl.core import gzippacked as _gzip_mod  # type: ignore[import-untyped]
    except ImportError:
        return
    _orig = _gzip_mod.GzipPacked.gzip_if_smaller

    def _skip_parts(content_related: bool, data: bytes) -> bytes:
        if len(data) > 256 * 1024:
            return data
        result: bytes = _orig(content_related, data)
        return result

    _gzip_mod.GzipPacked.gzip_if_smaller = staticmethod(_skip_parts)
    _patched_gzip = True
