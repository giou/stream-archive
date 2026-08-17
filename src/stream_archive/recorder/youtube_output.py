import asyncio
import contextlib
import logging
import os
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from stream_archive.config import (
    AppConfig,
    channel_url,
)
from stream_archive.recorder.common import _sanitize_filename

logger = logging.getLogger(__name__)


_QUICK_END_S = 120

_BACKOFF_BASE_S = 120

_BACKOFF_MAX_S = 1800

_YOUTUBE_DAILY_BUDGET = 10

_YOUTUBE_BUDGET_WINDOW_S = 86400


class YoutubeOutputMixin:
    _config: AppConfig
    _youtube: Any
    _notifier: Any
    _recordings: dict[str, dict[str, Any]]
    _quick_ends: dict[str, int]
    _backoff_until: dict[str, float]
    _youtube_starts: list[float]
    _track: Any
    _record_disk: Any
    _pipe_stream: Any
    _read_ffmpeg_stderr: Any
    _channel_dir: Any

    def _note_youtube_end(self, channel: str, entry: dict[str, Any]) -> None:
        """Apply restart backoff after a short-lived YouTube recording."""
        started = entry.get("started_at")
        lifetime = time.monotonic() - started if started else None
        if lifetime is not None and lifetime < _QUICK_END_S:
            n = self._quick_ends.get(channel, 0) + 1
            self._quick_ends[channel] = n
            wait = min(_BACKOFF_BASE_S * (2 ** (n - 1)), _BACKOFF_MAX_S)
            self._backoff_until[channel] = time.monotonic() + wait
            logger.warning(
                "[recorder] [%s] Recording ended after %.0fs — backing off restarts for %ds",
                channel,
                lifetime,
                wait,
            )
        else:
            self._quick_ends.pop(channel, None)
            self._backoff_until.pop(channel, None)

    def youtube_restart_blocked_reason(self, channel: str) -> str | None:
        """Why a youtube-mode recording for this channel must not start yet, or None.

        Consults both the per-channel quick-end backoff and the global rolling
        24h broadcast budget (all re-streams share one YouTube channel).
        """
        mode = self._config.channel_output_modes.get(channel, self._config.output_mode)
        if mode not in ("youtube", "both"):
            return None
        now = time.monotonic()
        backoff = self._backoff_until.get(channel, 0.0)
        if backoff > now:
            return f"restarting in {backoff - now:.0f}s (short recording, YouTube quota guard)"
        now_wall = time.time()
        self._youtube_starts = [t for t in self._youtube_starts if t > now_wall - _YOUTUBE_BUDGET_WINDOW_S]
        if len(self._youtube_starts) >= _YOUTUBE_DAILY_BUDGET:
            wait = self._youtube_starts[0] + _YOUTUBE_BUDGET_WINDOW_S - now_wall
            return (
                f"YouTube daily broadcast limit reached "
                f"({len(self._youtube_starts)}/{_YOUTUBE_DAILY_BUDGET} in the last 24h), "
                f"next slot in {wait / 60:.0f} min"
            )
        return None

    def _record_youtube_start(self) -> None:
        """Record a broadcast creation against the rolling daily budget."""
        self._youtube_starts.append(time.time())

    async def _end_broadcast(self, channel: str, broadcast_id: str) -> None:
        """Transition a YouTube broadcast to complete; never raises."""
        try:
            await self._youtube.end_stream(broadcast_id)
        except Exception as e:
            logger.error("[recorder] [youtube] Error ending broadcast for %s: %s", channel, e)

    async def _stream_youtube(
        self, channel: str, author: str, title: str, game: str, stream: Any, filepath: str | None
    ) -> None:
        logger.info("[recorder] [youtube] %s resolving...", channel)
        try:
            youtube_info = await self._youtube.create_stream(author, title, channel, game)
            self._record_youtube_start()
        except Exception as e:
            logger.error("[recorder] [youtube] Failed to create YouTube stream: %s", e)
            if "rate limit" in str(e).lower() or "403" in str(e) or "quota" in str(e).lower():
                msg = (
                    f"\u26a0\ufe0f YouTube rate limit reached!\n"
                    f"Channel: {channel}\n"
                    f"Stream: {title or 'Unknown'}\n"
                    f"Stream link: {channel_url(channel)}"
                )
                if self._notifier:
                    await self._notifier.notify(msg)
                entry = self._recordings.get(channel)
                if entry is not None:
                    recording_dir = f"{self._config.recording_dir}/{self._channel_dir(channel)}"
                    os.makedirs(recording_dir, exist_ok=True)
                    now = datetime.now(ZoneInfo(self._config.timezone)).strftime("%d_%m_%Y-%H%M%S")
                    safe_title = _sanitize_filename(f"{author} - {title}")
                    filepath = os.path.join(recording_dir, f"{safe_title}-{now}.ts")
                    entry["filepath"] = filepath
                    logger.info("[recorder] Rate limited — falling back to disk recording for %s", channel)
                    disk_task = self._track(channel, self._record_disk(channel, filepath, stream))
                    entry["tasks"].append(disk_task)
                    if self._notifier:
                        await self._notifier.notify_live(channel, title, game, channel_url(channel))
                return
            raise

        entry = self._recordings.get(channel)
        if entry is None:
            return
        entry["youtube_info"] = youtube_info

        if self._notifier:
            await self._notifier.notify_live(channel, title, game, channel_url(channel), youtube_info["youtube_url"])

        rtmp_url = youtube_info["rtmp_url"]
        ffmpeg_cmd = [
            "ffmpeg",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts+igndts",
            "-re",
            "-i",
            "pipe:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-max_muxing_queue_size",
            "1024",
            "-f",
            "flv",
            "-flvflags",
            "no_duration_filesize",
            rtmp_url,
        ]
        logger.info("[recorder] [youtube] Starting ffmpeg for %s", channel)

        process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        entry["process"] = process

        pipe_task = asyncio.create_task(self._pipe_stream(channel, stream, process, filepath))
        stderr_task = asyncio.create_task(self._read_ffmpeg_stderr(channel, process))

        try:
            results = await asyncio.gather(pipe_task, stderr_task)
        except asyncio.CancelledError:
            pipe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pipe_task
            logger.info("[recorder] [youtube] %s cancelled", channel)
            return
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=10)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        process.kill()
                        await process.wait()
                    except ProcessLookupError:
                        pass
            logger.info("[recorder] [youtube] %s ffmpeg stopped (rc=%s)", channel, process.returncode)

        if not results[0]:
            raise RuntimeError(f"[youtube] {channel} stream interrupted")

    def youtube_active_count(self) -> int:
        """Active recordings whose mode uses a YouTube re-stream (for the uplink cap)."""
        return sum(1 for e in self._recordings.values() if e.get("mode") in ("youtube", "both"))
