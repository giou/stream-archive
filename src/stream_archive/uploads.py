"""Shared VOD upload hub: parallel uploads with progress for every surface.

Telegram and the web panel start YouTube uploads, and both show the same
state. The hub owns that state: one record per upload with bytes sent,
total, phase, and terminal outcome. Callers submit a runner (an async
callable that takes a progress callback and returns the result URL), and
the hub runs it as a task. A task per upload means parallel uploads each
keep their own progress and their own cancel.
"""

import asyncio
import logging
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from stream_archive.youtube_upload import ProgressCallback, write_youtube_url

logger = logging.getLogger(__name__)

#: Recent finished uploads kept for the web list, newest last.
FINISHED_KEEP = 20


class UploadHub:
    """Run and track YouTube VOD uploads. All methods are loop-bound."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._finished: deque[dict[str, Any]] = deque(maxlen=FINISHED_KEEP)
        self._seq = 0

    def submit(
        self,
        channel: str,
        path: str | Path,
        runner: Callable[[ProgressCallback], Awaitable[str]],
    ) -> str:
        """Start ``runner`` for ``path``. Return the upload id.

        The runner takes a progress callback ``(sent, total, note)`` called
        from the event loop thread, and returns the result URL. The hub
        records the terminal state when the runner ends.
        """
        self._seq += 1
        upload_id = f"up{self._seq}{secrets.token_hex(2)}"
        file_path = Path(path)
        try:
            size = file_path.stat().st_size
        except OSError:
            size = 0
        now = time.monotonic()
        self._records[upload_id] = {
            "id": upload_id,
            "kind": "youtube",
            "channel": channel,
            "name": file_path.name,
            "path": str(file_path),
            "size": size,
            "sent": 0,
            "total": size,
            "note": None,
            "status": "running",
            "started": now,
            "updated": now,
            "result_url": None,
            "error": None,
        }
        task = asyncio.create_task(self._run(upload_id, runner))
        self._tasks[upload_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(upload_id, None))
        return upload_id

    def running_id(self, path: str | Path) -> str | None:
        """Id of the running upload of ``path``, or None."""
        want = str(path)
        for upload_id, record in self._records.items():
            if record["path"] == want and record["status"] == "running":
                return upload_id
        return None

    def cancel(self, upload_id: str) -> bool:
        """Cancel a running upload. False when it is unknown or finished."""
        task = self._tasks.get(upload_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def wait(self, upload_id: str) -> None:
        """Wait for one upload to leave the running state. Never raises."""
        task = self._tasks.get(upload_id)
        if task is None:
            return
        await asyncio.gather(task, return_exceptions=True)

    def result(self, upload_id: str) -> dict[str, Any] | None:
        """Terminal record of ``upload_id``, or the live record, or None."""
        for record in self._finished:
            if record["id"] == upload_id:
                return record
        return self._records.get(upload_id)

    def snapshot(self) -> list[dict[str, Any]]:
        """Running uploads in start order, then finished ones, newest first."""
        now = time.monotonic()
        running = [r for r in self._records.values() if r["status"] == "running"]
        items = [{**r, "elapsed_s": round(now - r["started"], 1)} for r in running]
        for record in reversed(self._finished):
            items.append({**record, "elapsed_s": round(record["updated"] - record["started"], 1)})
        return items

    def _progress_for(self, upload_id: str) -> ProgressCallback:
        record = self._records[upload_id]

        def _report(sent: int, total: int, note: str | None = None) -> None:
            if total > 0 and sent >= 0:
                record["sent"] = sent
                record["total"] = total
                record["note"] = note
                record["updated"] = time.monotonic()

        return _report

    async def _run(
        self,
        upload_id: str,
        runner: Callable[[ProgressCallback], Awaitable[str]],
    ) -> None:
        record = self._records[upload_id]
        try:
            url = await runner(self._progress_for(upload_id))
        except asyncio.CancelledError:
            record["status"] = "cancelled"
        except (FileNotFoundError, ValueError) as e:
            record["status"] = "failed"
            record["error"] = str(e) or "the file is gone"
        except Exception:
            logger.exception("[uploads] YouTube upload failed for %s", record["path"])
            record["status"] = "failed"
            record["error"] = f"Upload of {record['name']} failed - see logs."
        else:
            record["status"] = "done"
            record["result_url"] = url
            record["sent"] = record["total"]
            # Remember the URL next to the file: the hub is memory-only,
            # so a restart would otherwise drop the link from the panel.
            # Skip a file that vanished mid-upload (deleted during the
            # transfer): writing its sidecar then would orphan it, since
            # the delete already dropped the sidecar with the file.
            if Path(record["path"]).exists():
                write_youtube_url(record["path"], url)
        finally:
            record["updated"] = time.monotonic()
            self._finished.append(record)
            self._records.pop(upload_id, None)
