"""Reply-keyboard upload of recordings to YouTube.

Manual VOD uploads start from the Recordings browser (Upload, then
YouTube). The shared upload hub runs the transfer, so the web panel
shows the same progress. This mixin owns the Telegram side: the progress
message with ETA and the inline stop button, mirroring the MTProto flow.
"""

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from stream_archive import disk as disk_mod
from stream_archive.telegram.commands_mtproto import _format_eta, _progress_bar
from stream_archive.telegram.menu_state import ChatId
from stream_archive.youtube_upload import check_uploadable, youtube_available

logger = logging.getLogger(__name__)


class YoutubeCommands:
    _config: Any
    _app: Any
    _uploads: Any
    _youtube_runner: Any
    _youtube_tasks: set[Any]
    _youtube_sends: dict[Any, Any]

    async def _start_youtube_upload(self, chat_id: ChatId, path: str, channel: str | None = None) -> None:
        """Upload ``path`` to YouTube in the background, with Telegram progress."""
        hub = self._uploads
        if hub.running_id(path) is not None:
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text="That upload already runs. Wait for it to finish.",
                )
            except Exception:
                logger.warning("[telegram] Failed to send the upload notice", exc_info=True)
            return
        factory = self._youtube_runner
        if factory is None or not youtube_available(self._config):
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text="YouTube upload is not configured. Run 'stream-archive-setup-youtube' first.",
                )
            except Exception:
                logger.warning("[telegram] Failed to send the upload notice", exc_info=True)
            return
        ok, note = check_uploadable(Path(path))
        if not ok:
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=f"Cannot upload {Path(path).name}: {note}.",
                )
            except Exception:
                logger.warning("[telegram] Failed to send the upload notice", exc_info=True)
            return

        async def _run() -> None:
            import secrets

            from stream_archive.telegram.menus_callbacks import single_keyboard

            file_path = Path(path)
            name = file_path.name
            try:
                size = file_path.stat().st_size
            except OSError:
                size = 0
            resolved_channel = channel or "unknown"
            nonce = secrets.token_hex(4)
            stop_data = f"youtube_stop:{nonce}"
            stop_keyboard = single_keyboard("\u23f9 Stop upload", stop_data)
            base = factory(resolved_channel, path)
            updates: asyncio.Queue[tuple[int, int, str | None] | None] = asyncio.Queue()
            done = asyncio.Event()
            started = time.monotonic()

            async def _runner(hub_progress: Any) -> str:
                def fanout(sent: int, total: int, note: str | None = None) -> None:
                    hub_progress(sent, total, note)
                    updates.put_nowait((sent, total, note))

                url: str = await base(fanout)
                return url

            try:
                notice = await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=f"\U0001f4e4 Uploading {name} to YouTube ({disk_mod.format_bytes(size)})...",
                    reply_markup=stop_keyboard,
                )
            except Exception:
                logger.warning("[telegram] Failed to send the upload notice", exc_info=True)
                return
            upload_id = None
            # Re-check after the notice send: a second start can pass the
            # entry check while this task awaited it. The check and the
            # submit below hold no await between them, so two starts on one
            # loop cannot both slip through here.
            if hub.running_id(path) is not None:
                with contextlib.suppress(Exception):
                    await notice.edit_text("That upload already runs. Wait for it to finish.")
            else:
                upload_id = hub.submit(resolved_channel, path, _runner)
                self._youtube_sends[(chat_id, nonce)] = upload_id
            if upload_id is None:
                return

            async def _watch() -> None:
                last_frac = -1.0
                last_edit = 0.0
                while True:
                    try:
                        sample = await updates.get()
                    except asyncio.CancelledError:
                        return
                    if sample is None:
                        return
                    sent, total, _note = sample
                    now = time.monotonic()
                    frac = min(1.0, sent / total) if total > 0 else 0.0
                    final = frac >= 1.0 or done.is_set()
                    if not final and (frac - last_frac < 0.05 or now - last_edit < 5.0):
                        continue
                    last_frac = frac
                    last_edit = now
                    if frac >= 1.0 and not done.is_set():
                        continue
                    line = _youtube_progress_line(name, size, sent, total, frac, now - started)
                    try:
                        # Re-attach the keyboard: a text edit without markup drops it.
                        await notice.edit_text(line, reply_markup=stop_keyboard)
                    except Exception:
                        logger.debug("[telegram] Progress edit failed", exc_info=True)
                    if final:
                        return

            watcher = asyncio.create_task(_watch())
            try:
                await hub.wait(upload_id)
            finally:
                done.set()
                updates.put_nowait(None)
                await asyncio.gather(watcher, return_exceptions=True)
            record = hub.result(upload_id)
            status = record.get("status") if record else None
            self._youtube_sends.pop((chat_id, nonce), None)
            if status == "cancelled":
                # Final edits drop the keyboard by omitting the markup.
                with contextlib.suppress(Exception):
                    await notice.edit_text(f"\u23f9 Stopped {name} - nothing was deleted.")
                return
            if status != "done":
                error = record.get("error") if record else None
                # Edit the notice in place: it drops the Stop button with
                # the markup, and no second message is needed.
                with contextlib.suppress(Exception):
                    await notice.edit_text(f"\u274c YouTube upload of {name} failed: {error or 'see logs'}")
                return
            url = record.get("result_url") if record else None
            try:
                await notice.edit_text(_youtube_done_line(name, size, time.monotonic() - started, url))
            except Exception:
                logger.debug("[telegram] Done edit failed", exc_info=True)

        task = asyncio.create_task(_run())
        self._youtube_tasks.add(task)
        task.add_done_callback(self._log_task_done)

    def _log_task_done(self, task: asyncio.Task[None]) -> None:
        """Release a finished upload task, and log its unexpected failure.

        `_run` handles every expected outcome, so a stored exception here
        means a bug outside that handling. Without this the failure is
        silent (or a never-retrieved warning).
        """
        self._youtube_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception("[telegram] YouTube upload task failed", exc_info=exc)


def _youtube_progress_line(name: str, size: int, sent: int, total: int, frac: float, elapsed: float) -> str:
    """One progress message: bar, percent, Mbps, and ETA."""
    bar = _progress_bar(frac)
    line = f"\U0001f4e4 Uploading {name} to YouTube ({disk_mod.format_bytes(size)})\n{bar} {frac * 100:.0f}%"
    if frac <= 0 or elapsed <= 0:
        return line
    speed = sent / elapsed
    line += f" \u00b7 {speed / 125000:.1f} Mbps"
    if frac < 1.0 and speed > 0:
        line += f" \u00b7 ETA {_format_eta((total - sent) / speed)}"
    return line


def _youtube_done_line(name: str, size: int, elapsed: float, url: str | None) -> str:
    """Final progress message: full bar, average Mbps, and watch URL."""
    speed = size / max(elapsed, 0.001)
    line = (
        f"\U0001f4e4 Uploaded {name} to YouTube ({disk_mod.format_bytes(size)})\n"
        f"{_progress_bar(1.0)} 100% \u00b7 {speed / 125000:.1f} Mbps"
    )
    if url:
        line += f"\n{url}"
    return line
