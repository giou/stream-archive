from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from stream_archive import disk
from stream_archive.config import (
    AppConfig,
    channel_url,
)
from stream_archive.recorder.common import _sanitize_filename
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
# 3s loop) fed into a held broadcast with `-c copy` — no runtime encoding.
_RECONNECT_CLIP = Path(__file__).resolve().parent.parent / "assets" / "reconnect_clip.mp4"


@dataclass(frozen=True)
class YouTubeLimits:
    """Bounds for YouTube re-streams. One place for every magic number.

    Recorder takes one of these so calls shrink the windows without
    touching module state. Defaults equal the old module constants.
    """

    quick_end_s: float = _QUICK_END_S
    backoff_base_s: float = _BACKOFF_BASE_S
    backoff_max_s: float = _BACKOFF_MAX_S
    daily_budget: int = _YOUTUBE_DAILY_BUDGET
    budget_window_s: float = _YOUTUBE_BUDGET_WINDOW_S
    reconnect_clip: Path = _RECONNECT_CLIP


class YoutubeOutputMixin:
    _config: AppConfig
    _youtube: YouTubeStreamer | None
    _notifier: Notifier | None
    _recordings: dict[str, Recording]
    _held: dict[str, HoldState]
    _limits: YouTubeLimits
    _quick_ends: dict[str, int]
    _backoff_until: dict[str, float]
    _youtube_starts: list[float]
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
        if lifetime is not None and lifetime < self._limits.quick_end_s:
            n = self._quick_ends.get(channel, 0) + 1
            self._quick_ends[channel] = n
            wait = min(self._limits.backoff_base_s * (2 ** (n - 1)), self._limits.backoff_max_s)
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
        now_wall = time.time()
        self._youtube_starts = [t for t in self._youtube_starts if t > now_wall - self._limits.budget_window_s]
        if len(self._youtube_starts) >= self._limits.daily_budget:
            wait = self._youtube_starts[0] + self._limits.budget_window_s - now_wall
            return (
                f"YouTube daily broadcast limit reached "
                f"({len(self._youtube_starts)}/{self._limits.daily_budget} in the last 24h), "
                f"next slot in {wait / 60:.0f} min"
            )
        return None

    def _record_youtube_start(self) -> None:
        """Record one broadcast creation against the rolling 24-hour budget."""
        self._youtube_starts.append(time.time())

    async def _end_broadcast(self, channel: str, broadcast_id: str) -> None:
        """Move a YouTube broadcast to the complete state. Never raises."""

        def _require_streamer() -> YouTubeStreamer:
            youtube = self._youtube
            if youtube is None:
                msg = f"no YouTube streamer configured for {channel}"
                raise RuntimeError(msg)
            return youtube

        try:
            await _require_streamer().end_stream(broadcast_id)
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
            str(self._limits.reconnect_clip),
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
            if proc is not None:
                proc.terminate()
            raise
        except Exception as e:
            logger.warning("[recorder] [youtube] keep-alive spawn failed (hold without keep-alive): %s", e)
            return None
        else:
            return proc

    async def _stop_keepalive(self, proc: asyncio.subprocess.Process | None) -> None:
        """Stop the keep-alive feed. Safe to call more than once."""
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError, ProcessLookupError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

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
        old = self._held.get(channel)
        if old:
            end_task = old.get("end_task")
            if end_task is not None:
                end_task.cancel()
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
        """Feed the held broadcast for the delay, or end early if the feed dies."""
        keepalive = await self._start_keepalive(hold["youtube_info"]["rtmp_url"])
        hold["keepalive"] = keepalive
        sleep_task = asyncio.create_task(asyncio.sleep(delay))
        ka_task = asyncio.create_task(keepalive.wait()) if keepalive is not None else None
        try:
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
                await self._end_broadcast(channel, hold["youtube_info"]["broadcast_id"])
                return
            if ka_task is not None:
                ka_task.cancel()
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

            def _require_streamer() -> YouTubeStreamer:
                youtube = self._youtube
                if youtube is None:
                    msg = f"YouTube streamer is not configured for {channel}"
                    raise RuntimeError(msg)
                return youtube

            try:
                youtube = _require_streamer()
                youtube_info = await youtube.create_stream(author, title, channel, game)
                self._record_youtube_start()  # count quota for fresh creates only
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
                        safe_title = _sanitize_filename(f"{author} - {title}")
                        filepath = os.path.join(recording_dir, f"{safe_title}-{now}.ts")
                        entry["filepath"] = filepath
                        logger.info("[recorder] Rate limited — falling back to disk recording for %s", channel)
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
            raise
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError, ProcessLookupError:
                    try:
                        process.kill()
                        await process.wait()
                    except ProcessLookupError:
                        pass
            logger.info("[recorder] [youtube] %s ffmpeg stopped (rc=%s)", channel, process.returncode)

        if not results[0]:
            msg = f"[youtube] {channel} stream interrupted"
            raise RuntimeError(msg)

    def youtube_active_count(self) -> int:
        """Active recordings whose mode uses a YouTube re-stream (for the uplink cap)."""
        return sum(1 for e in self._recordings.values() if e.get("mode") in ("youtube", "both"))
