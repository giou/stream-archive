import asyncio
import contextlib
import logging
import os
import time
from datetime import datetime
from pathlib import Path
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

# Rolling 24-hour budget of broadcast creations. Guards YouTube's daily
# limit on new broadcast creations.
_YOUTUBE_DAILY_BUDGET = 10

_YOUTUBE_BUDGET_WINDOW_S = 86400

# Pre-encoded 1920x1080@60 "Reconnecting..." interstitial (animated dots,
# 3s loop) fed into a held broadcast with `-c copy` — no runtime encoding.
_RECONNECT_CLIP = Path(__file__).resolve().parent.parent / "assets" / "reconnect_clip.mp4"


class YoutubeOutputMixin:
    _config: AppConfig
    _youtube: Any
    _notifier: Any
    _recordings: dict[str, dict[str, Any]]
    _held: dict[str, dict[str, Any]]
    _quick_ends: dict[str, int]
    _backoff_until: dict[str, float]
    _youtube_starts: list[float]
    _track: Any
    _record_disk: Any
    _pipe_stream: Any
    _read_ffmpeg_stderr: Any
    _channel_dir: Any

    def _note_youtube_end(self, channel: str, entry: dict[str, Any]) -> None:
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
        """Record one broadcast creation against the rolling 24-hour budget."""
        self._youtube_starts.append(time.time())

    async def _end_broadcast(self, channel: str, broadcast_id: str) -> None:
        """Move a YouTube broadcast to the complete state. Never raises."""
        try:
            await self._youtube.end_stream(broadcast_id)
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
            return proc
        except asyncio.CancelledError:
            if proc is not None:
                proc.terminate()
            raise
        except Exception as e:
            logger.warning("[recorder] [youtube] keep-alive spawn failed (hold without keep-alive): %s", e)
            return None

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

    async def _release_broadcast(
        self, channel: str, youtube_info: dict[str, Any] | None, entry: dict[str, Any]
    ) -> None:
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
            old["end_task"].cancel()
        hold: dict[str, Any] = {"youtube_info": youtube_info, "end_task": None, "keepalive": None}
        self._held[channel] = hold
        hold["end_task"] = asyncio.create_task(self._hold_then_end(channel, delay, hold))
        logger.info(
            "[recorder] [youtube] %s broadcast %s held for %.0fs (streamer may return)",
            channel,
            youtube_info["broadcast_id"],
            delay,
        )

    async def _hold_then_end(self, channel: str, delay: float, hold: dict[str, Any]) -> None:
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
            held["end_task"].cancel()
            await self._stop_keepalive(held.get("keepalive"))
            youtube_info = held["youtube_info"]
            entry["youtube_info"] = youtube_info
            entry["reused"] = True
            logger.info("[recorder] [youtube] %s reusing held broadcast %s", channel, youtube_info["broadcast_id"])
        else:
            try:
                youtube_info = await self._youtube.create_stream(author, title, channel, game)
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
                        if self._notifier and notify:
                            await self._notifier.notify_live(channel, title, game, channel_url(channel))
                    return
                raise
            entry = self._recordings.get(channel)
            if entry is None:
                return
            entry["youtube_info"] = youtube_info

        if self._notifier and youtube_notify:
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
            raise RuntimeError(f"[youtube] {channel} stream interrupted")

    def youtube_active_count(self) -> int:
        """Active recordings whose mode uses a YouTube re-stream (for the uplink cap)."""
        return sum(1 for e in self._recordings.values() if e.get("mode") in ("youtube", "both"))
