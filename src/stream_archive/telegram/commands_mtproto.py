"""Reply-keyboard menu for MTProto upload: enable, disable, and status.

The MTProto client logs in with the same bot token and sends recordings up
to 2 GiB, past the 50 MB Bot API cap. The api id and the api hash stay in
env vars: the menu never shows them, and /status never prints them.
"""

import asyncio
import contextlib
import logging
from typing import Any

from stream_archive.config import AppConfig
from stream_archive.telegram.menu_state import ChatId, is_error

logger = logging.getLogger(__name__)


class MtprotoCommands:
    _config: AppConfig
    _apply: Any
    _mtproto: Any
    _send_admin: Any
    _admin_id: int
    _app: Any
    _mtproto_tasks: set[Any]
    _sending_paths: set[str]
    _mtproto_sends: dict[Any, Any]

    def _mtproto_state_text(self) -> str:
        """One-line state of the MTProto uploader."""
        m = self._config.mtproto
        if not m.enabled:
            return "off"
        if not m.api_id or not m.api_hash.strip():
            return "on (missing api id or hash)"
        connected = self._mtproto is not None and self._mtproto.connected
        return "on (connected)" if connected else "on (not connected yet)"

    async def _set_mtproto_enabled(self, enabled: bool, chat_id: ChatId | None = None) -> str:
        """Enable or disable MTProto upload and persist the change."""
        m = self._config.mtproto
        if enabled and (not m.api_id or not m.api_hash.strip()):
            return (
                "\u274c Set mtproto.api_id and mtproto.api_hash in config.json first "
                "(use ${TELEGRAM_API_ID} and ${TELEGRAM_API_HASH})."
            )

        def mutate(candidate: AppConfig) -> None:
            candidate.mtproto.enabled = enabled

        result: str = self._apply(mutate, lambda c: f"MTProto upload {'enabled' if enabled else 'disabled'}", chat_id)
        if is_error(result):
            return result
        if enabled and self._mtproto is None:
            from stream_archive.mtproto_upload import MtprotoUploader

            self._mtproto = MtprotoUploader(self._config)
        if enabled and self._mtproto is not None:
            try:
                await self._mtproto.connect()
            except Exception:
                logger.warning("[telegram] MTProto connect failed after enable", exc_info=True)
                return result + "\n\u26a0\ufe0f Enabled, but the MTProto login failed \u2014 check the logs."
            if self._mtproto.connected:
                return result + "\nMTProto connected. Recordings can now be sent from the Recordings menu."
            return result + "\n\u26a0\ufe0f Enabled, but not connected yet \u2014 check the logs."
        if not enabled and self._mtproto is not None:
            try:
                for task in list(self._mtproto_tasks):
                    task.cancel()
                if self._mtproto_tasks:
                    await asyncio.gather(*self._mtproto_tasks, return_exceptions=True)
                await self._mtproto.disconnect()
            except Exception:
                logger.warning("[telegram] MTProto disconnect failed", exc_info=True)
        return result

    async def _start_mtproto_send(self, chat_id: ChatId, path: str) -> None:
        """Upload ``path`` in the background and report the outcome to ``chat_id``."""
        import asyncio
        import time
        from pathlib import Path

        from stream_archive import disk as disk_mod

        uploader = self._mtproto
        if uploader is None:
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text="MTProto upload is off. Enable it under Settings, then MTProto upload.",
                )
            except Exception:
                logger.warning("[telegram] Failed to send the upload result", exc_info=True)
            return

        async def _run() -> None:
            import secrets

            from stream_archive.telegram.menus_callbacks import single_keyboard

            name = Path(path).name
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
            nonce = secrets.token_hex(4)
            stop_data = f"mtproto_stop:{nonce}"
            stop_keyboard = single_keyboard("\u23f9 Stop upload", stop_data)
            self._mtproto_sends[(chat_id, nonce)] = asyncio.current_task()
            try:
                notice = await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=f"\U0001f4e4 Sending {name} ({disk_mod.format_bytes(size)})...",
                    reply_markup=stop_keyboard,
                )
            except Exception:
                logger.warning("[telegram] Failed to send the upload notice", exc_info=True)
                self._mtproto_sends.pop((chat_id, nonce), None)
                return
            import queue as _queue

            updates: _queue.SimpleQueue[tuple[int, int, str | None] | None] = _queue.SimpleQueue()
            done = asyncio.Event()
            started = time.monotonic()

            def _progress(sent: int, total: int, note: str | None = None) -> None:
                # Runs in the upload path: only enqueue, never touch
                # the loop or Telegram here.
                if total > 0 and sent >= 0:
                    updates.put((sent, total, note))

            async def _watch() -> None:
                last_frac = -1.0
                last_edit = 0.0
                last_phase: str | None = None
                while True:
                    try:
                        sample = await asyncio.get_running_loop().run_in_executor(None, updates.get)
                    except asyncio.CancelledError:
                        return
                    if sample is None:
                        return
                    sent, total, note = sample
                    now = time.monotonic()
                    frac = min(1.0, sent / total) if total > 0 else 0.0
                    final = frac >= 1.0 or done.is_set()
                    # Phase changes (split -> part 1/5 -> part 2/5) always
                    # edit: the bar stays honest when a new part restarts it.
                    # Same-phase edits throttle: one step per 5 s and 5%.
                    same_phase = note == last_phase
                    if not final and same_phase and (frac - last_frac < 0.05 or now - last_edit < 5.0):
                        continue
                    last_frac = frac
                    last_edit = now
                    last_phase = note
                    # A 100% sample only edits when the upload really finished:
                    # the None sentinel wakes the watcher without an edit.
                    if frac >= 1.0 and not done.is_set():
                        continue
                    line = _progress_line(name, size, sent, total, frac, now - started, note)
                    try:
                        # Re-attach the keyboard: a text edit without markup drops it.
                        await notice.edit_text(line, reply_markup=stop_keyboard)
                    except Exception:
                        logger.debug("[telegram] Progress edit failed", exc_info=True)
                    if final:
                        return

            watcher = asyncio.create_task(_watch())
            failed: str | None = None
            stopped = False
            try:
                try:
                    if not uploader.connected:
                        await uploader.connect()
                    await uploader.send_video(path, chat_id, caption=name, progress=_progress)
                finally:
                    done.set()
                    updates.put(None)
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
            except FileNotFoundError:
                failed = f"\u274c {name} is gone."
            except ValueError as e:
                failed = f"\u274c {e}"
            except asyncio.CancelledError:
                stopped = True
            except Exception:
                logger.exception("[telegram] MTProto upload failed for %s", path)
                failed = f"\u274c Upload of {name} failed \u2014 see logs."
            finally:
                self._mtproto_sends.pop((chat_id, nonce), None)
            if stopped:
                # Final edits drop the keyboard by omitting the markup.
                with contextlib.suppress(Exception):
                    await notice.edit_text(f"\u23f9 Stopped {name} \u2014 nothing was deleted.")
                return
            if failed is not None:
                try:
                    await self._app.bot.send_message(chat_id=chat_id, text=failed)
                except Exception:
                    logger.warning("[telegram] Failed to send the upload result", exc_info=True)
            else:
                try:
                    await notice.edit_text(_done_line(name, size, time.monotonic() - started))
                except Exception:
                    logger.debug("[telegram] Done edit failed", exc_info=True)

        self._sending_paths.add(path)
        task = asyncio.create_task(_run())
        self._mtproto_tasks.add(task)

        def _done(t: object) -> None:
            self._mtproto_tasks.discard(task)
            self._sending_paths.discard(path)

        task.add_done_callback(_done)


def _done_line(name: str, size: int, elapsed: float) -> str:
    """Final progress message: full bar plus the average Mbps."""
    from stream_archive import disk as disk_mod

    speed = size / max(elapsed, 0.001)
    return (
        f"\U0001f4e4 Sent {name} ({disk_mod.format_bytes(size)})\n"
        f"{_progress_bar(1.0)} 100% \u00b7 {speed / 125000:.1f} Mbps"
    )


def _progress_line(
    name: str, size: int, sent: int, total: int, frac: float, elapsed: float, note: str | None = None
) -> str:
    """One progress message: phase, bar, percent, Mbps, and ETA."""
    from stream_archive import disk as disk_mod

    phase = f" ({note})" if note else ""
    bar = _progress_bar(frac)
    line = f"\U0001f4e4 Sending {name}{phase} ({disk_mod.format_bytes(size)})\n{bar} {frac * 100:.0f}%"
    if frac <= 0 or elapsed <= 0:
        return line
    speed = sent / elapsed
    line += f" \u00b7 {speed / 125000:.1f} Mbps"
    if frac < 1.0 and speed > 0:
        line += f" \u00b7 ETA {_format_eta((total - sent) / speed)}"
    return line


def _progress_bar(frac: float, width: int = 10) -> str:
    """Block bar of ``width`` cells for fraction ``frac`` in [0, 1]."""
    filled = min(width, max(0, int(frac * width)))
    return "\U0001f7e9" * filled + "\U00002b1c" * (width - filled)


def _format_eta(seconds: float) -> str:
    """H:MM:SS (or M:SS under an hour) for a remaining-seconds estimate."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
