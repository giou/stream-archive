import asyncio
import logging
import os
import time
from collections.abc import Coroutine
from contextlib import nullcontext, suppress
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from streamlink.exceptions import NoPluginError, NoStreamsError, PluginError
from streamlink.session.session import Streamlink

from stream_archive import disk
from stream_archive.chat_recorder import ChatRecorder
from stream_archive.config import (
    AUDIO_ONLY_QUALITY,
    AppConfig,
    bare_name,
    channel_url,
    effective_quality,
    is_kick_channel,
    kick_bare_name,
)
from stream_archive.recorder.chat_output import ChatOutputMixin
from stream_archive.recorder.common import _sanitize_filename
from stream_archive.recorder.disk_output import DiskOutputMixin
from stream_archive.recorder.streamlink_source import StreamlinkMixin
from stream_archive.recorder.youtube_output import YoutubeOutputMixin

logger = logging.getLogger(__name__)

# A clean feed end suppresses monitor restarts until the offline webhook or API
# event catches up. The 10 min covers encoder restarts and HLS playlist END
# stalls. The value stays short so a still-live feed resumes recording quickly.
_ENDED_CLEAN_GRACE_S = 600.0


class Recorder(StreamlinkMixin, DiskOutputMixin, YoutubeOutputMixin, ChatOutputMixin):
    _config: AppConfig
    _youtube: Any
    _notifier: Any
    _recordings: dict[str, dict[str, Any]]
    _locks: dict[str, asyncio.Lock]
    _session: Streamlink
    _plugin_loaded: bool
    _last_kick_block_notify: dict[str, float]
    _quick_ends: dict[str, int]
    _backoff_until: dict[str, float]
    _youtube_starts: list[float]
    _held: dict[str, dict[str, Any]]
    _reserve_lock: asyncio.Lock
    _reserved_channels: dict[str, str]
    _finalize_chat: Any
    _finalize_kick_chat: Any
    _end_broadcast: Any
    _note_youtube_end: Any

    def __init__(self, config: AppConfig, youtube_streamer: Any = None, notifier: Any = None) -> None:
        self._config = config
        self._youtube = youtube_streamer
        self._notifier = notifier
        self._recordings = {}
        self._locks = {}
        self._session = Streamlink()
        self._session.set_option("http-timeout", 30)
        # Ride through short HLS playlist stalls. With the default queue-deadline
        # factor (3) and Kick's ~2s target duration, streamlink aborts after ~6s
        # without new segments. Fresh Kick streams often hit that right after
        # go-live. Factor 10 raises the tolerance to ~20s. A genuinely dead feed
        # is still detected, and the poll cycle covers the rest.
        self._session.set_option("stream-segmented-queue-deadline", 10)
        self._plugin_loaded = False
        self._last_kick_block_notify = {}
        self._quick_ends = {}  # channel -> consecutive short YouTube recordings
        self._backoff_until = {}  # channel -> monotonic time before restart allowed
        self._youtube_starts = []
        self._held = {}  # channel -> hold dict (broadcast kept open awaiting reuse)
        self._ended_clean: dict[str, float] = {}  # channel -> monotonic end time (clean stream over)
        self._reserve_lock = asyncio.Lock()
        self._reserved_channels = {}  # channel -> output mode, reserved but not yet started

    async def start(
        self, channel: str, title: str | None = None, game: str | None = None, user_id: str | None = None
    ) -> bool:
        async with self._lock_for(channel):
            return await self._start_unlocked(channel, title=title, game=game, user_id=user_id)

    async def reserve_start(self, channel: str) -> str | None:
        """Reserve recording/YT capacity atomically. Returns a block reason or None.

        This closes the check-then-act gap between the monitor's limit counters
        and the registration in _recordings seconds later. Two simultaneous
        go-lives can no longer both slip past max_concurrent_recordings /
        max_concurrent_youtube_streams. The monitor releases the reservation in
        a finally block once start() has registered or failed.
        """
        async with self._reserve_lock:
            mode = self._effective_mode(channel)
            max_rec = self._config.max_concurrent_recordings
            if max_rec > 0 and len(self._recordings) + len(self._reserved_channels) >= max_rec:
                return f"concurrent recording limit reached ({max_rec}/{max_rec})"
            max_yt = self._config.max_concurrent_youtube_streams
            if max_yt > 0 and mode in ("youtube", "both"):
                yt_busy = self.youtube_active_count() + sum(
                    1 for m in self._reserved_channels.values() if m in ("youtube", "both")
                )
                if yt_busy >= max_yt:
                    return f"YouTube re-stream limit reached ({max_yt}/{max_yt})"
            self._reserved_channels[channel] = mode
            return None

    def release_start(self, channel: str) -> None:
        """Drop a reservation made by reserve_start (idempotent)."""
        self._reserved_channels.pop(channel, None)

    def _effective_mode(self, channel: str) -> str:
        """Effective output mode for a channel, with the audio-only guard.

        An audio-only stream cannot feed a YouTube re-stream. Channels with an
        audio-only quality always record to disk. This is also the safety net
        for manual config.json edits and for config changes made while the
        bot was down.
        """
        mode = self._config.channel_output_modes.get(channel, self._config.output_mode)
        if mode != "disk" and effective_quality(self._config, channel) == AUDIO_ONLY_QUALITY:
            return "disk"
        return mode

    def _lock_for(self, channel: str) -> asyncio.Lock:
        if channel not in self._locks:
            self._locks[channel] = asyncio.Lock()
        return self._locks[channel]

    async def _start_unlocked(
        self,
        channel: str,
        title: str | None = None,
        game: str | None = None,
        user_id: str | None = None,
        notify: bool = True,
        youtube_notify: bool = True,
    ) -> bool:
        if channel in self._recordings:
            return True

        raw_mode = self._config.channel_output_modes.get(channel, self._config.output_mode)
        mode = self._effective_mode(channel)
        if raw_mode != mode:
            logger.warning(
                "[recorder] [%s] audio_only selected but output mode is %s — recording to disk instead",
                channel,
                raw_mode,
            )
        loop = asyncio.get_running_loop()

        try:
            best, author, stream_title, stream_game = await loop.run_in_executor(
                None, self._resolve_stream, channel, title, game
            )
        except NoStreamsError:
            logger.error(
                "[recorder] [%s] No streams available (ad-block proxies exhausted or stream not ready). Will retry on the next check.",
                channel,
            )
            return False
        except (NoPluginError, PluginError) as e:
            logger.error("[recorder] Failed to get streams for %s: %s", channel, e)
            msg = str(e)
            if is_kick_channel(channel) and ("403" in msg or "blocked by security policy" in msg):
                now_ts = time.monotonic()
                if now_ts - self._last_kick_block_notify.get(channel, -1800.0) >= 1800:
                    self._last_kick_block_notify[channel] = now_ts
                    if self._notifier:
                        await self._notifier.notify(
                            f"\u26a0\ufe0f Kick is blocking requests from this server (anti-bot challenge). "
                            f"Recording {channel} failed: {msg}. Will retry automatically. "
                            f"Install a browser on this host (streamlink then solves the challenge automatically) "
                            f"or run from a non-blocked IP."
                        )
            return False
        except Exception as e:
            logger.error("[recorder] Unexpected error resolving %s: %s", channel, e)
            return False

        try:
            entry: dict[str, Any] = {"tasks": [], "process": None, "youtube_info": None, "filepath": None}
            entry["started_at"] = time.monotonic()
            entry["mode"] = mode
            self._recordings[channel] = entry
            entry["title"] = title
            entry["game"] = game
            entry["user_id"] = user_id
            entry["quality"] = effective_quality(self._config, channel)
            tasks = []
            live_url = channel_url(channel)

            now = datetime.now(ZoneInfo(self._config.timezone)).strftime("%d_%m_%Y-%H%M%S")
            safe_title = _sanitize_filename(stream_title)

            if mode in ("disk", "both"):
                recording_dir = f"{self._config.recording_dir}/{self._channel_dir(channel)}"
                os.makedirs(recording_dir, exist_ok=True)
                extension = ".m4a" if entry["quality"] == AUDIO_ONLY_QUALITY else ".ts"
                filename = f"{safe_title}-{now}{extension}"
                filepath = os.path.join(recording_dir, filename)
                entry["filepath"] = filepath

            if mode == "disk":
                disk_task = self._track(channel, self._record_disk(channel, entry["filepath"], best))
                tasks.append(disk_task)
                if self._notifier and notify:
                    await self._notifier.notify_live(channel, stream_title, stream_game, live_url)
            elif mode == "youtube":
                if self._youtube is not None:
                    yt_task = self._track(
                        channel,
                        self._stream_youtube(
                            channel,
                            author,
                            stream_title,
                            stream_game,
                            best,
                            None,
                            notify=notify,
                            youtube_notify=youtube_notify,
                        ),
                    )
                    tasks.append(yt_task)
            elif mode == "both":
                if self._youtube is not None:
                    yt_task = self._track(
                        channel,
                        self._stream_youtube(
                            channel,
                            author,
                            stream_title,
                            stream_game,
                            best,
                            entry["filepath"],
                            notify=notify,
                            youtube_notify=youtube_notify,
                        ),
                    )
                    tasks.append(yt_task)

            if not tasks:
                del self._recordings[channel]
                return False

            if self._config.record_chat and not is_kick_channel(channel):
                chat_dir = disk.chat_dir_path(self._config)
                chat_path = os.path.join(chat_dir, self._channel_dir(channel), f"{safe_title}-{now}.chat.json")
                os.makedirs(os.path.dirname(chat_path), exist_ok=True)
                chat_recorder = ChatRecorder(
                    bare_name(channel), chat_path, stream_title, stream_game, author=author, user_id=user_id
                )
                entry["chat_recorder"] = chat_recorder
                entry["chat_task"] = chat_recorder.start()

            if is_kick_channel(channel) and self._config.kick.record_chat:
                chat_dir = disk.chat_dir_path(self._config)
                chat_path = os.path.join(chat_dir, "kick", kick_bare_name(channel), f"{safe_title}-{now}.chat.json")
                os.makedirs(os.path.dirname(chat_path), exist_ok=True)
                entry["kick_chat"] = {
                    "path": chat_path,
                    "messages": [],
                    "title": stream_title,
                    "channel": channel,
                    "started_wall": datetime.now(ZoneInfo(self._config.timezone)).isoformat(),
                }

            entry["tasks"] = tasks

            disk_cfg = self._config.disk
            if disk_cfg.max_total_gb > 0:
                entry["watchdog"] = asyncio.create_task(self._watch_growth(channel))

            self._ended_clean.pop(channel, None)
            logger.info("[recorder] Started recording %s (mode=%s)", channel, mode)
            return True
        except Exception as e:
            # The entry is already registered here. Without this cleanup, any
            # OSError below (makedirs, task creation) leaves a taskless entry
            # behind. Every later start short-circuits on that entry, and the
            # monitor reports the channel as LIVE forever. Returning False
            # routes the failure into _handle_start_failure (rate-limited alert
            # plus next-cycle retry).
            to_cancel: list[asyncio.Task[Any]] = list(tasks)
            chat_task = entry.get("chat_task")
            if chat_task is not None:
                to_cancel.append(chat_task)
            for t in to_cancel:
                t.cancel()
            await asyncio.gather(*to_cancel, return_exceptions=True)
            self._recordings.pop(channel, None)
            logger.error("[recorder] [%s] Failed to start recording: %s", channel, e)
            return False

    async def stop(self, channel: str) -> dict[str, Any] | None:
        async with self._lock_for(channel):
            return await self._stop_unlocked(channel)

    async def _stop_unlocked(self, channel: str) -> dict[str, Any] | None:
        if channel not in self._recordings:
            return None

        entry = self._recordings.pop(channel)
        wd = entry.pop("watchdog", None)
        if wd:
            wd.cancel()
        for task in entry.get("tasks", []):
            task.cancel()
        if entry.get("tasks") or wd:
            await asyncio.gather(*(entry.get("tasks", []) + ([wd] if wd else [])), return_exceptions=True)

        chat_recorder = entry.pop("chat_recorder", None)
        if chat_recorder:
            try:
                await chat_recorder.stop()
            except Exception as e:
                logger.error("[recorder] [%s] chat finalize error: %s", channel, e)
        await self._finalize_kick_chat(entry)

        youtube_info = entry.get("youtube_info")

        if youtube_info:
            await self._release_broadcast(channel, youtube_info, entry)

        filepath = entry.get("filepath")
        file_info = None
        if filepath and os.path.exists(filepath):
            st = os.stat(filepath)
            size_mb = st.st_size / (1024 * 1024)
            mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC)
            file_info = {
                "name": os.path.basename(filepath),
                "size_mb": round(size_mb, 2),
                "date": mtime.astimezone(ZoneInfo(self._config.timezone)).strftime("%d-%m-%Y %H:%M"),
            }

        return {"file_info": file_info, "youtube_info": youtube_info}

    async def restart(self, channel: str) -> bool:
        """Stop and immediately restart a recording with the current config.

        Restart bypasses the monitor start gates (disk cap, max recordings,
        YouTube budget) intentionally because this is an admin-forced action.
        Disk mode suppresses the live notification: the Telegram apply-result
        message gives the feedback. A youtube-mode restart still sends the live
        notification once the new broadcast is created, because the apply-now
        restart ended the old broadcast and changed the link.
        """
        async with self._lock_for(channel):
            entry = self._recordings.get(channel)
            if entry is None:
                return False
            title, game, user_id = entry.get("title"), entry.get("game"), entry.get("user_id")
            await self._stop_unlocked(channel)
            return await self._start_unlocked(
                channel, title=title, game=game, user_id=user_id, notify=False, youtube_notify=True
            )

    async def stop_all(self) -> None:
        for channel in list(self._recordings):
            await self.stop(channel)

    async def close(self) -> None:
        await self.stop_all()
        for ch, held in list(self._held.items()):
            held["end_task"].cancel()
            await self._stop_keepalive(held.get("keepalive"))
            self._held.pop(ch, None)
            await self._end_broadcast(ch, held["youtube_info"]["broadcast_id"])

    def _track(self, channel: str, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda t: self._on_task_finished(channel, t))
        return task

    def _on_task_finished(self, channel: str, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        entry = self._recordings.get(channel)
        if entry is None or task not in entry.get("tasks", []):
            return
        entry["tasks"].remove(task)
        if exc is not None:
            logger.error("[recorder] [%s] Recording task failed: %s", channel, exc)
            entry["failed"] = True
        else:
            # A clean stream end (for example, the HLS feed stalls and streamlink
            # closes it) must also release the entry once all tasks finish.
            # Otherwise the monitor sees the channel as recording and never
            # restarts, and the broadcast lingers until YouTube auto-ends it.
            logger.info("[recorder] [%s] Recording task ended", channel)
        if entry["tasks"]:
            return  # other recording tasks (for example the disk fallback) still running
        chat_recorder = entry.pop("chat_recorder", None)
        if chat_recorder:
            asyncio.create_task(self._finalize_chat(channel, chat_recorder))
        asyncio.create_task(self._finalize_kick_chat(entry))
        youtube_info = entry.get("youtube_info")
        if youtube_info:
            asyncio.create_task(self._release_broadcast(channel, youtube_info, entry))
        if entry.get("mode") in ("youtube", "both"):
            self._note_youtube_end(channel, entry)
        # Remember that the stream ended on its own, not through a task failure.
        # The monitor then skips restart attempts until the offline event catches
        # up. Otherwise a dead stream just resolves to a 404.
        if not entry.get("failed"):
            self._ended_clean[channel] = time.monotonic()
        del self._recordings[channel]

    def ended_clean(self, channel: str) -> bool:
        """True when the channel's last recording ended cleanly and recently."""
        ts = self._ended_clean.get(channel)
        if ts is None:
            return False
        if time.monotonic() - ts >= _ENDED_CLEAN_GRACE_S:
            self._ended_clean.pop(channel, None)
            return False
        return True

    async def _abort(self, channel: str, reason: str) -> None:
        # The disk-cap watchdog calls this from outside any per-channel lock.
        # Taking the lock here serializes the _recordings mutation with
        # stop()/start() for the same channel.
        async with self._lock_for(channel):
            await self._abort_unlocked(channel, reason)

    async def _abort_unlocked(self, channel: str, reason: str) -> None:
        logger.warning("[recorder] [%s] Stopping recording: %s", channel, reason)
        if self._notifier:
            await self._notifier.notify(f"\u26d4 Stopped recording {channel}: {reason}")
        entry = self._recordings.pop(channel, None)
        if entry is None:
            return
        wd = entry.pop("watchdog", None)
        if wd:
            wd.cancel()
        for task in entry.get("tasks", []):
            task.cancel()
        # The watchdog calls _abort from inside its own task. Gathering that
        # task after self-cancelling it makes Task.cancel recurse through a
        # Task<->GatheringFuture cycle (RecursionError). Await everything else.
        me = asyncio.current_task()
        gathered = list(entry.get("tasks", []))
        if wd is not None and wd is not me:
            gathered.append(wd)
        if gathered:
            await asyncio.gather(*gathered, return_exceptions=True)

        chat_recorder = entry.pop("chat_recorder", None)
        if chat_recorder:
            try:
                await chat_recorder.stop()
            except Exception as e:
                logger.error("[recorder] [%s] chat finalize error: %s", channel, e)
        await self._finalize_kick_chat(entry)

        if entry.get("youtube_info"):
            await self._release_broadcast(channel, entry["youtube_info"], entry)

    def is_recording(self, channel: str) -> bool:
        return channel in self._recordings

    def active_channels(self) -> list[str]:
        """Names of channels currently being recorded, sorted."""
        return sorted(self._recordings)

    def recording_settings(self) -> dict[str, dict[str, Any]]:
        """Per active channel: settings that the in-flight recording uses.

        Output mode and preferred quality come from snapshots taken at
        recording start. Chat capture reflects the live state: chat disable
        stops in-flight capture immediately, so only chat enable takes effect
        on later recordings.
        """
        out = {}
        for ch, e in self._recordings.items():
            out[ch] = {
                "output_mode": e.get("mode"),
                "preferred_quality": e.get("quality"),
                "record_chat": "chat_recorder" in e,
                "kick_record_chat": e.get("kick_chat") is not None,
            }
        return out

    def recording_info(self) -> list[dict[str, Any]]:
        """Per active channel: duration + current file size (approx). Sorted by channel."""
        out = []
        now = time.monotonic()
        for channel in sorted(self._recordings):
            e = self._recordings[channel]
            size_mb = None
            fp = e.get("filepath")
            if fp:
                with suppress(OSError):
                    size_mb = os.path.getsize(fp) / (1024 * 1024)
            out.append(
                {
                    "channel": channel,
                    "mode": e.get("mode"),
                    "duration_s": round(now - e.get("started_at", now)),
                    "size_mb": size_mb,
                }
            )
        return out

    async def _pipe_stream(self, channel: str, stream: Any, process: Any, filepath: str | None) -> bool:
        loop = asyncio.get_running_loop()
        clean = False
        try:
            fd = await loop.run_in_executor(None, stream.open)
        except Exception as e:
            logger.error("[recorder] [youtube] %s stream open failed: %s", channel, e)
            return False

        file_handle = None
        try:
            if filepath:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "wb") if filepath else nullcontext() as file_handle:
                while True:
                    try:
                        data = await loop.run_in_executor(None, fd.read, 65536)
                    except Exception as e:
                        logger.error("[recorder] [youtube] %s read error: %s", channel, e)
                        break
                    if not data:
                        clean = True
                        break

                    if file_handle:
                        file_handle.write(data)

                    process.stdin.write(data)
                    await process.stdin.drain()

                logger.info("[recorder] [youtube] %s pipe finished", channel)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[recorder] [youtube] %s pipe error: %s", channel, e)
        finally:
            with suppress(Exception):
                fd.close()
            with suppress(Exception):
                process.stdin.close()
        return clean
