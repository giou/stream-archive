from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from stream_archive import disk
from stream_archive.config import (
    AppConfig,
    channel_url,
)
from stream_archive.recorder.common import sanitize_filename
from stream_archive.recorder.types import HoldState, Recording

if TYPE_CHECKING:
    from stream_archive.notifier import Notifier
    from stream_archive.youtube_streamer import YouTubeStreamer

logger = logging.getLogger(__name__)


_QUICK_END_S = 120

_BACKOFF_BASE_S = 120

_BACKOFF_MAX_S = 1800

# Rolling 24-hour budget of broadcast creations. Guards YouTube's daily
# limit on new broadcast creations.
_YOUTUBE_DAILY_BUDGET = 10

_YOUTUBE_BUDGET_WINDOW_S = 86400

# Pre-encoded 1920x1080@60 "Reconnecting..." interstitial (animated dots,
# 3s loop) fed into a held broadcast with `-c copy` - no runtime encoding.
_RECONNECT_CLIP = Path(__file__).resolve().parent.parent / "assets" / "reconnect_clip.mp4"


class YoutubeOutputMixin:
    _config: AppConfig
    _youtube: YouTubeStreamer | None
    _notifier: Notifier | None
    _recordings: dict[str, Recording]
    _held: dict[str, HoldState]
    _quick_ends: dict[str, int]
    _backoff_until: dict[str, float]
    _youtube_starts: list[float]
    _youtube_budget_lock: asyncio.Lock
    # Set by sibling mixins and Recorder (core.py). Exact call shapes so
    # a signature drift fails type checks instead of failing at runtime.
    _track: Callable[[str, Coroutine[Any, Any, Any]], asyncio.Task[Any]]
    _record_disk: Callable[[str, str, Any], Coroutine[Any, Any, None]]
    _pipe_stream: Callable[[str, Any, Any, str | None], Coroutine[Any, Any, bool]]
    _read_ffmpeg_stderr: Callable[[str, Any], Coroutine[Any, Any, None]]
    _channel_dir: Callable[[str], str]

    def _note_youtube_end(self, channel: str, entry: Recording) -> None:
        """Apply quick-end backoff after a short recording.

        A stable recording clears the backoff instead.
        """
        started = entry.get("started_at")
        lifetime = time.monotonic() - started if started else None
        if lifetime is not None and lifetime < _QUICK_END_S:
            n = self._quick_ends.get(channel, 0) + 1
            self._quick_ends[channel] = n
            wait = min(_BACKOFF_BASE_S * (2 ** (n - 1)), _BACKOFF_MAX_S)
            self._backoff_until[channel] = time.monotonic() + wait
            logger.warning(
                "[recorder] [%s] Recording ended after %.0fs - backing off restarts for %ds",
                channel,
                lifetime,
                wait,
            )
        else:
            self._quick_ends.pop(channel, None)
            self._backoff_until.pop(channel, None)

    def youtube_restart_blocked_reason(self, channel: str) -> str | None:
        """Return why a youtube-mode recording for this channel must wait, or None.

        Checks the per-channel quick-end backoff and the global rolling
        24-hour broadcast budget. All re-streams share one YouTube channel.
        """
        mode = self._config.channel_output_modes.get(channel, self._config.output_mode)
        if mode not in ("youtube", "both"):
            return None
        if self._held.get(channel):
            return None  # held broadcast is reused, so no create and no quota cost
        now = time.monotonic()
        backoff = self._backoff_until.get(channel, 0.0)
        if backoff > now:
            return f"restarting in {backoff - now:.0f}s (short recording, YouTube quota guard)"
        return self._youtube_budget_blocked_reason()

    def _youtube_budget_blocked_reason(self) -> str | None:
        """Return why the rolling 24-hour budget blocks a create now, or None."""
        now_wall = time.time()
        self._youtube_starts = [t for t in self._youtube_starts if t > now_wall - _YOUTUBE_BUDGET_WINDOW_S]
        if _YOUTUBE_DAILY_BUDGET <= 0:
            # A zero budget blocks every create. Guard it here: the line
            # below would index an empty list.
            return "YouTube daily broadcast limit reached (0/0 in the last 24h)"
        if len(self._youtube_starts) >= _YOUTUBE_DAILY_BUDGET:
            wait = self._youtube_starts[0] + _YOUTUBE_BUDGET_WINDOW_S - now_wall
            return (
                f"YouTube daily broadcast limit reached "
                f"({len(self._youtube_starts)}/{_YOUTUBE_DAILY_BUDGET} in the last 24h), "
                f"next slot in {wait / 60:.0f} min"
            )
        return None

    def _require_streamer(self, channel: str) -> YouTubeStreamer:
        """Return the YouTube streamer. Raise when none is configured."""
        youtube = self._youtube
        if youtube is None:
            msg = f"YouTube streamer is not configured for {channel}"
            raise RuntimeError(msg)
        return youtube

    def _require_budget_slot(self) -> None:
        """Raise when the rolling 24-hour budget blocks a new broadcast.

        A caller holds `_youtube_budget_lock`, so the check and the record
        of the matching slot cannot interleave with another start.
        """
        blocked = self._youtube_budget_blocked_reason()
        if blocked is not None:
            msg = blocked
            raise RuntimeError(msg)

    async def _terminate(self, proc: asyncio.subprocess.Process | None, timeout: float = 10.0) -> None:
        """Stop a child process: terminate, then kill when it does not exit.

        Never raises. A process that already exited is a no-op.
        """
        if proc is None or proc.returncode is not None:
            return
        try:
            # terminate() raises ProcessLookupError when the child already
            # exited, so it belongs inside the guard.
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError, ProcessLookupError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    async def _end_broadcast(self, channel: str, broadcast_id: str) -> None:
        """Move a YouTube broadcast to the complete state. Never raises."""
        try:
            await self._require_streamer(channel).end_stream(broadcast_id)
        except Exception as e:
            logger.error("[recorder] [youtube] Error ending broadcast for %s: %s", channel, e)

    def _hold_seconds(self, channel: str) -> float:
        """Return the hold delay in seconds after the source stops (0 ends now)."""
        return self._config.channel_youtube_hold_seconds.get(channel, self._config.youtube.hold_seconds)

    async def _start_keepalive(self, rtmp_url: str) -> asyncio.subprocess.Process | None:
        """Loop the bundled reconnect clip into the RTMP URL with `-c copy`.

        The feed keeps the broadcast alive during the hold. Without it,
        YouTube auto-ends a broadcast about 90 s after the encoder goes
        silent. Returns None when ffmpeg cannot spawn. The hold then
        proceeds without a keep-alive.
        """
        cmd = [
            "ffmpeg",
            "-loglevel",
            "warning",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(_RECONNECT_CLIP),
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-f",
            "flv",
            "-flvflags",
            "no_duration_filesize",
            rtmp_url,
        ]
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except asyncio.CancelledError:
            # The await was cancelled, so no handle exists yet. A child that
            # was already spawned cannot be reaped here: make it traceable.
            logger.warning("[recorder] [youtube] keep-alive spawn cancelled; an ffmpeg may be orphaned")
            raise
        except Exception as e:
            logger.warning("[recorder] [youtube] keep-alive spawn failed (hold without keep-alive): %s", e)
            return None
        else:
            return proc

    async def _stop_keepalive(self, proc: asyncio.subprocess.Process | None) -> None:
        """Stop the keep-alive feed. Safe to call more than once."""
        await self._terminate(proc)

    async def _release_broadcast(self, channel: str, youtube_info: dict[str, Any] | None, entry: Recording) -> None:
        """End the broadcast now, or hold it open for the configured delay."""
        if youtube_info is None:
            return
        delay = self._hold_seconds(channel)
        if delay <= 0 or (entry.get("reused") and entry.get("failed")):
            # Zero delay means the feature is off. A reuse that failed left a
            # dead broadcast, so holding it would only loop.
            await self._end_broadcast(channel, youtube_info["broadcast_id"])
            return
        old = self._held.pop(channel, None)
        if old is not None:
            # A replaced hold loses its timer, and nothing else still knows
            # its broadcast. End it here, or it stays live on YouTube.
            end_task = old.get("end_task")
            if end_task is not None:
                end_task.cancel()
            await self._end_broadcast(channel, old["youtube_info"]["broadcast_id"])
        hold: HoldState = {"youtube_info": youtube_info, "end_task": None, "keepalive": None}
        self._held[channel] = hold
        hold["end_task"] = asyncio.create_task(self._hold_then_end(channel, delay, hold))
        logger.info(
            "[recorder] [youtube] %s broadcast %s held for %.0fs (streamer may return)",
            channel,
            youtube_info["broadcast_id"],
            delay,
        )

    async def _hold_then_end(self, channel: str, delay: float, hold: HoldState) -> None:
        """Feed the held broadcast for the delay, or end early if the feed dies.

        This task ends its own broadcast. A cancellation only drops the hold
        and stops the keep-alive: the canceller took the hold over (reuse), or
        it ends the broadcast itself (close).
        """
        keepalive: asyncio.subprocess.Process | None = None
        sleep_task: asyncio.Task[Any] | None = None
        ka_task: asyncio.Task[Any] | None = None
        try:
            keepalive = await self._start_keepalive(hold["youtube_info"]["rtmp_url"])
            hold["keepalive"] = keepalive
            sleep_task = asyncio.create_task(asyncio.sleep(delay))
            ka_task = asyncio.create_task(keepalive.wait()) if keepalive is not None else None
            done, _ = await asyncio.wait(
                [t for t in (sleep_task, ka_task) if t is not None],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ka_task is not None and ka_task in done:
                # The keep-alive died early, so the broadcast is gone. End it now.
                if self._held.get(channel) is hold:
                    self._held.pop(channel, None)
                await self._stop_keepalive(keepalive)
                logger.warning("[recorder] [youtube] %s keep-alive feed stopped early, ending broadcast", channel)
                sleep_task.cancel()  # the hold is over; do not leave the timer pending
                await asyncio.gather(sleep_task, return_exceptions=True)
                await self._end_broadcast(channel, hold["youtube_info"]["broadcast_id"])
                return
            if ka_task is not None:
                ka_task.cancel()  # the feed outlived the hold; stop watching it
                await asyncio.gather(ka_task, return_exceptions=True)
            if self._held.get(channel) is not hold:
                await self._stop_keepalive(keepalive)
                return  # a new stream consumed the hold while we slept
            self._held.pop(channel, None)
            await self._stop_keepalive(keepalive)
            await self._end_broadcast(channel, hold["youtube_info"]["broadcast_id"])
        except asyncio.CancelledError:
            for t in (sleep_task, ka_task):
                if t is not None:
                    t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                # A cancelled task that nobody awaits warns at teardown.
                await asyncio.gather(*(t for t in (sleep_task, ka_task) if t is not None), return_exceptions=True)
            if self._held.get(channel) is hold:
                # Nobody took the hold over, so drop it: the next start must
                # not reuse a broadcast that no task feeds any more.
                self._held.pop(channel, None)
            await self._stop_keepalive(keepalive)
            raise

    async def _stream_youtube(
        self,
        channel: str,
        author: str,
        title: str,
        game: str,
        stream: Any,
        filepath: str | None,
        notify: bool = True,
        youtube_notify: bool = True,
    ) -> None:
        entry = self._recordings.get(channel)
        if entry is None:
            return
        held = self._held.pop(channel, None)
        if held is not None:
            end_task = held.get("end_task")
            if end_task is not None:
                end_task.cancel()
            await self._stop_keepalive(held.get("keepalive"))
            youtube_info = held["youtube_info"]
            entry["youtube_info"] = youtube_info
            entry["reused"] = True
            logger.info("[recorder] [youtube] %s reusing held broadcast %s", channel, youtube_info["broadcast_id"])
        else:
            try:
                youtube = self._require_streamer(channel)
                # Hold the lock across the check, the create and the record.
                # Concurrent starts otherwise all pass the check before any
                # of them records its slot, and the daily budget overflows.
                async with self._youtube_budget_lock:
                    self._require_budget_slot()
                    youtube_info = await youtube.create_stream(author, title, channel, game)
                    self._youtube_starts.append(time.time())  # count fresh creates only
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
                        try:
                            await self._notifier.notify(msg)
                        except Exception:
                            logger.error("[recorder] rate-limit notification failed for %s", channel, exc_info=True)
                    entry = self._recordings.get(channel)
                    if entry is not None:
                        recording_dir = str(disk.channel_recording_dir(self._config, self._channel_dir(channel)))
                        os.makedirs(recording_dir, exist_ok=True)
                        now = datetime.now(ZoneInfo(self._config.timezone)).strftime("%d_%m_%Y-%H%M%S")
                        safe_title = sanitize_filename(f"{author} - {title}")
                        filepath = os.path.join(recording_dir, f"{safe_title}-{now}.ts")
                        entry["filepath"] = filepath
                        logger.info("[recorder] Rate limited - falling back to disk recording for %s", channel)
                        disk_task = self._track(channel, self._record_disk(channel, filepath, stream))
                        entry["tasks"].append(disk_task)
                        if self._notifier and notify:
                            try:
                                await self._notifier.notify_live(channel, title, game, channel_url(channel))
                            except Exception:
                                logger.error("[recorder] live notification failed for %s", channel, exc_info=True)
                    return
                raise
            entry = self._recordings.get(channel)
            if entry is None:
                # The recording vanished while the broadcast was created, so
                # nothing can feed it. End it here: a broadcast left live and
                # unowned costs quota and shows on the channel.
                await self._end_broadcast(channel, youtube_info["broadcast_id"])
                return
            entry["youtube_info"] = youtube_info

        if self._notifier and youtube_notify:
            try:
                await self._notifier.notify_live(
                    channel, title, game, channel_url(channel), youtube_info["youtube_url"]
                )
            except Exception:
                logger.error("[recorder] live notification failed for %s", channel, exc_info=True)
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
        pipe_task = asyncio.create_task(self._pipe_stream(channel, stream, process, filepath))
        stderr_task = asyncio.create_task(self._read_ffmpeg_stderr(channel, process))

        try:
            results = await asyncio.gather(pipe_task, stderr_task)
        except asyncio.CancelledError:
            # Both readers hold the ffmpeg pipes, so both must stop and be
            # awaited here. A task left pending masks the ffmpeg stderr and
            # warns "Task was destroyed but it is pending" at shutdown.
            for task in (pipe_task, stderr_task):
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(pipe_task, stderr_task, return_exceptions=True)
            logger.info("[recorder] [youtube] %s cancelled", channel)
            raise
        finally:
            await self._terminate(process)
            logger.info("[recorder] [youtube] %s ffmpeg stopped (rc=%s)", channel, process.returncode)

        if not results[0]:
            msg = f"[youtube] {channel} stream interrupted"
            raise RuntimeError(msg)

    def youtube_active_count(self) -> int:
        """Active recordings whose mode uses a YouTube re-stream (for the uplink cap)."""
        return sum(1 for e in self._recordings.values() if e.get("mode") in ("youtube", "both"))
