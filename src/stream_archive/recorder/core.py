from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable, Coroutine
from contextlib import nullcontext, suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from streamlink.exceptions import NoPluginError, NoStreamsError, PluginError
from streamlink.session.session import Streamlink

from stream_archive import disk
from stream_archive.chat_recorder import ChatRecorder
from stream_archive.chat_writer import ChatJsonWriter
from stream_archive.config import (
    AUDIO_ONLY_QUALITY,
    AppConfig,
    bare_name,
    channel_url,
    effective_quality,
    is_kick_channel,
)
from stream_archive.kick_chat import parse_time, video_id_for
from stream_archive.recorder.chat_output import ChatOutputMixin
from stream_archive.recorder.common import _open_stream, sanitize_filename
from stream_archive.recorder.disk_output import DiskOutputMixin
from stream_archive.recorder.streamlink_source import StreamlinkMixin
from stream_archive.recorder.types import HoldState, KickChatState, Recording
from stream_archive.recorder.youtube_output import YoutubeOutputMixin

if TYPE_CHECKING:
    from stream_archive.notifier import Notifier
    from stream_archive.youtube_streamer import YouTubeStreamer

logger = logging.getLogger(__name__)

# A clean feed end suppresses monitor restarts until the offline webhook or API
_ENDED_CLEAN_GRACE_S = 600.0


def _require_filepath(entry: Recording, channel: str) -> str:
    """Filepath for a disk recording. Raise when the path is missing."""
    disk_filepath = entry.get("filepath")
    if disk_filepath is None:
        msg = f"missing filepath for disk recording of {channel}"
        raise RuntimeError(msg)
    return disk_filepath


class Recorder(StreamlinkMixin, DiskOutputMixin, YoutubeOutputMixin, ChatOutputMixin):
    _config: AppConfig
    _youtube: YouTubeStreamer | None
    _notifier: Notifier | None
    _recordings: dict[str, Recording]
    _locks: dict[str, asyncio.Lock]
    _session: Streamlink
    _plugin_loaded: bool
    _plugin_lock: threading.Lock
    _last_kick_block_notify: dict[str, float]
    _quick_ends: dict[str, int]
    _backoff_until: dict[str, float]
    _youtube_starts: list[float]
    _youtube_budget_lock: asyncio.Lock
    _held: dict[str, HoldState]
    _reserve_lock: asyncio.Lock
    _reserved_channels: dict[str, str]
    _bg_tasks: set[asyncio.Task[Any]]

    def __init__(
        self,
        config: AppConfig,
        youtube_streamer: YouTubeStreamer | None = None,
        notifier: Notifier | None = None,
    ) -> None:
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
        self._plugin_lock = threading.Lock()
        self._last_kick_block_notify = {}
        self._quick_ends = {}  # channel -> consecutive short YouTube recordings
        self._backoff_until = {}  # channel -> monotonic time before restart allowed
        self._youtube_starts = []
        # Serializes the budget check, the broadcast create and the budget
        # record, so concurrent starts cannot all pass the check.
        self._youtube_budget_lock = asyncio.Lock()
        self._held = {}  # channel -> hold dict (broadcast kept open awaiting reuse)
        self._ended_clean: dict[str, float] = {}  # channel -> monotonic end time (clean stream over)
        self._reserve_lock = asyncio.Lock()
        self._reserved_channels = {}  # channel -> output mode, reserved but not yet started
        # Fire-and-forget finalize tasks. The set holds a reference, so the
        # event loop cannot garbage-collect a task in flight, and close()
        # can drain the tasks.
        self._bg_tasks = set()
        # Open chat writers, keyed by channel, and their real paths. See
        # ChatOutputMixin for why the recording entry is not enough.
        self._open_chat = {}
        self._chat_paths = set()

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
        return self._locks.setdefault(channel, asyncio.Lock())

    def _chat_error_handler(self, channel: str, chat_path: str) -> Callable[[Exception], None]:
        """Build the callback that a chat writer calls after a write failure.

        The writer calls it once. The callback only schedules the operator
        notification, so it can run from any context.
        """

        def handler(e: Exception) -> None:
            with suppress(RuntimeError):  # no running loop: the writer already logged the failure
                self._spawn_bg(self._notify_chat_error(channel, chat_path, e))

        return handler

    async def _notify_chat_error(self, channel: str, chat_path: str, e: Exception) -> None:
        """Tell the operator that chat capture stopped. Never raises."""
        if self._notifier is None:
            return
        try:
            await self._notifier.notify(
                f"\u26a0\ufe0f Chat capture stopped for {channel}: {e}. "
                f"The recording continues. Partial chat stays in {chat_path}.tmp."
            )
        except Exception:
            logger.error("[recorder] chat failure notification failed for %s", channel, exc_info=True)

    def _start_chat_capture(
        self,
        entry: Recording,
        channel: str,
        stream_title: str,
        stream_game: str,
        author: str,
        user_id: str | None,
        safe_title: str,
        now: str,
    ) -> None:
        """Attach the chat capture of a new recording to its entry.

        Twitch chat comes from IRC, so a recorder object starts here. Kick
        chat arrives over the webhook, so the entry only gets its writer and
        the metadata for the trailer.
        """
        if self._config.record_chat and not is_kick_channel(channel):
            chat_dir = disk.chat_dir_path(self._config)
            chat_path = os.path.join(chat_dir, self._channel_dir(channel), f"{safe_title}-{now}.chat.json")
            chat_recorder = ChatRecorder(
                bare_name(channel),
                chat_path,
                stream_title,
                stream_game,
                author=author,
                user_id=user_id,
                on_error=self._chat_error_handler(channel, chat_path),
            )
            entry["chat_recorder"] = chat_recorder
            self._register_chat_paths(chat_path)
            entry["chat_task"] = chat_recorder.start()

        if is_kick_channel(channel) and self._config.kick.record_chat:
            chat_dir = disk.chat_dir_path(self._config)
            slug = bare_name(channel)
            chat_path = os.path.join(chat_dir, "kick", slug, f"{safe_title}-{now}.chat.json")
            started_wall = datetime.now(ZoneInfo(self._config.timezone)).isoformat()
            start = parse_time(started_wall)
            kick_state: KickChatState = {
                "path": chat_path,
                "writer": ChatJsonWriter(chat_path, on_error=self._chat_error_handler(channel, chat_path)),
                "title": stream_title,
                "channel": channel,
                "slug": slug,
                "started_wall": started_wall,
                "start": start,
                "video_id": video_id_for(slug, start),
                "streamer_id": None,
                "streamer_username": slug,
                "emote_names": {},
                "emote_skipped": 0,
            }
            entry["kick_chat"] = kick_state
            self._register_kick_chat(channel, kick_state, chat_path)

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
                        try:
                            await self._notifier.notify(
                                f"\u26a0\ufe0f Kick is blocking requests from this server (anti-bot challenge). "
                                f"Recording {channel} failed: {msg}. Will retry automatically. "
                                f"Install a browser on this host (streamlink then solves the challenge automatically) "
                                f"or run from a non-blocked IP."
                            )
                        except Exception:
                            logger.error("[recorder] kick-block notification failed for %s", channel, exc_info=True)
            return False
        except Exception as e:
            logger.error("[recorder] Unexpected error resolving %s: %s", channel, e)
            return False

        # Bind the task list before the try below. The failure handler reads
        # it, and a fault between the registration and the first append must
        # not turn into an UnboundLocalError that masks the real error.
        tasks: list[asyncio.Task[Any]] = []
        try:
            entry: Recording = {"tasks": [], "youtube_info": None, "filepath": None}
            entry["started_at"] = time.monotonic()
            entry["mode"] = mode
            self._recordings[channel] = entry
            entry["title"] = title
            entry["game"] = game
            entry["user_id"] = user_id
            entry["quality"] = effective_quality(self._config, channel)
            live_url = channel_url(channel)

            now = datetime.now(ZoneInfo(self._config.timezone)).strftime("%d_%m_%Y-%H%M%S")
            safe_title = sanitize_filename(stream_title)

            if mode in ("disk", "both"):
                recording_dir = str(disk.channel_recording_dir(self._config, self._channel_dir(channel)))
                os.makedirs(recording_dir, exist_ok=True)
                extension = ".m4a" if entry["quality"] == AUDIO_ONLY_QUALITY else ".ts"
                filename = f"{safe_title}-{now}{extension}"
                filepath = os.path.join(recording_dir, filename)
                entry["filepath"] = filepath

            if mode == "disk":
                disk_task = self._track(channel, self._record_disk(channel, _require_filepath(entry, channel), best))
                tasks.append(disk_task)
                # The live notification goes out at the end of this method.
                # A task that ends during that wait then finds its task list
                # here, and the entry cannot stay behind without a task.
            elif self._youtube is not None:  # mode youtube or both
                yt_task = self._track(
                    channel,
                    self._stream_youtube(
                        channel,
                        author,
                        stream_title,
                        stream_game,
                        best,
                        entry["filepath"] if mode == "both" else None,
                        notify=notify,
                        youtube_notify=youtube_notify,
                    ),
                )
                tasks.append(yt_task)

            if not tasks:
                del self._recordings[channel]
                return False

            self._start_chat_capture(entry, channel, stream_title, stream_game, author, user_id, safe_title, now)

            entry["tasks"] = tasks

            # Arm the watchdog for every capture. It re-reads the live cap
            # each tick and does nothing while the cap is disabled, so a cap
            # that is enabled after the capture started still applies to it.
            # Arming it only when the cap was already set left a running
            # capture unmeasured for the rest of its life.
            entry["watchdog"] = asyncio.create_task(self._watch_growth(channel))

            self._ended_clean.pop(channel, None)
            logger.info("[recorder] Started recording %s (mode=%s)", channel, mode)
            if mode == "disk" and notify and self._notifier:
                try:
                    await self._notifier.notify_live(channel, stream_title, stream_game, live_url)
                except Exception:
                    logger.error("[recorder] live notification failed for %s", channel, exc_info=True)
        except BaseException as e:
            # The entry is already registered here. Without this cleanup, any
            # failure below (makedirs, task creation) or a cancellation from
            # the caller leaves a taskless entry behind. Every later start
            # short-circuits on that entry, and the monitor reports the
            # channel as LIVE forever. Returning False routes the failure
            # into _handle_start_failure (rate-limited alert plus
            # next-cycle retry).
            to_cancel: list[asyncio.Task[Any]] = list(tasks)
            chat_task = entry.get("chat_task")
            if chat_task is not None:
                to_cancel.append(chat_task)
            watchdog = entry.get("watchdog")
            if watchdog is not None:
                to_cancel.append(watchdog)
            for t in to_cancel:
                t.cancel()
            state = entry.get("kick_chat")
            if state is not None:
                # The entry drops the state below, so nothing else can close
                # the writer. A start that failed keeps no chat, so the tmp
                # file goes too.
                state["writer"].discard()
                entry.pop("kick_chat", None)
                self._release_kick_chat(channel, state, state["writer"].path)
            recorder = entry.get("chat_recorder")
            if recorder is not None:
                # Cancelling the chat task does not close its writer, and a
                # failed start keeps no chat, so remove the partial file first.
                # Closing is synchronous, so the release cannot happen while
                # the writer is still open.
                recorder.discard()
                self._release_chat_paths(recorder.chat_path)
            self._recordings.pop(channel, None)
            if not isinstance(e, Exception):
                # CancelledError and the like: the caller does not want a
                # result, so clean up and let the caller handle it.
                raise
            await asyncio.gather(*to_cancel, return_exceptions=True)
            logger.error("[recorder] [%s] Failed to start recording: %s", channel, e)
            return False
        else:
            return True

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
        youtube_info = entry.get("youtube_info")
        await self._finalize_entry(channel, entry, chat_recorder)

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
        cancelled: asyncio.CancelledError | None = None
        for channel in list(self._recordings):
            # One failing channel must not keep the others running: their
            # streamlink and ffmpeg children would survive shutdown. A
            # cancellation is deferred the same way, because the shutdown
            # deadline cancels whatever it interrupts and the remaining
            # channels still need their children and chat writers closed.
            try:
                await self.stop(channel)
            except asyncio.CancelledError as e:
                cancelled = e
                logger.warning("[recorder] stop of %s was interrupted; stopping the rest first", channel)
            except Exception:
                logger.error("[recorder] stop failed for %s", channel, exc_info=True)
        if cancelled is not None:
            raise cancelled

    async def close(self) -> None:
        cancelled: asyncio.CancelledError | None = None
        try:
            await self.stop_all()
        except asyncio.CancelledError as e:
            # The shutdown deadline fired. Finish the teardown anyway: the
            # held broadcasts and the background finalizers must not be left
            # behind, and the caller still sees the cancellation.
            cancelled = e
        for ch, held in list(self._held.items()):
            # One bad entry must not skip the rest: every remaining
            # keep-alive process and broadcast needs its own teardown.
            try:
                end_task = held.get("end_task")
                if end_task is not None:
                    end_task.cancel()
                await self._stop_keepalive(held.get("keepalive"))
                self._held.pop(ch, None)
                youtube_info = held.get("youtube_info") or {}
                broadcast_id = youtube_info.get("broadcast_id")
                if broadcast_id:
                    await self._end_broadcast(ch, broadcast_id)
            except Exception:
                logger.error("[recorder] held broadcast cleanup failed for %s", ch, exc_info=True)
        self._reserved_channels.clear()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        if cancelled is not None:
            raise cancelled

    def _spawn_bg(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run a finalize coroutine in the background, with a held reference.

        The event loop keeps only a weak reference, so a bare task can be
        garbage-collected in flight and its exception never retrieved. The
        set holds the task, and close() drains it.
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

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
            self._spawn_bg(self._finalize_chat(channel, chat_recorder))
        self._spawn_bg(self._finalize_kick_chat(entry))
        youtube_info = entry.get("youtube_info")
        if youtube_info:
            self._spawn_bg(self._release_broadcast(channel, youtube_info, entry))
        if entry.get("mode") in ("youtube", "both"):
            self._note_youtube_end(channel, entry)
        # Cancel the watchdog here too, like the stop and abort paths. It only
        # stops once it sees no entry for the channel, and a restart inside
        # that window would leave a second, untracked watchdog behind.
        wd = entry.pop("watchdog", None)
        if wd is not None:
            wd.cancel()
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
        # Pop first. A stale abort (the entry already went away through
        # _on_task_finished or an earlier abort) must not alert the operator.
        entry = self._recordings.pop(channel, None)
        if entry is None:
            return
        logger.warning("[recorder] [%s] Stopping recording: %s", channel, reason)
        if self._notifier:
            try:
                await self._notifier.notify(f"\u26d4 Stopped recording {channel}: {reason}")
            except Exception:
                logger.error("[recorder] stop notification failed for %s", channel, exc_info=True)
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
        await self._finalize_entry(channel, entry, chat_recorder)

    async def _finalize_entry(self, channel: str, entry: Recording, chat_recorder: ChatRecorder | None) -> None:
        """Finalize the chat capture, the broadcast, and the held state."""
        if chat_recorder:
            # _finalize_chat releases the writer's paths. Stopping the capture
            # directly would leave its chat file protected from the archive
            # passes for the rest of the process.
            await self._finalize_chat(channel, chat_recorder)
        await self._finalize_kick_chat(entry)
        youtube_info = entry.get("youtube_info")
        if youtube_info:
            await self._release_broadcast(channel, youtube_info, entry)

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
            fd = await _open_stream(stream)
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
                        # The archive volume can be slow. Write on the
                        # executor, like the reads.
                        await loop.run_in_executor(None, file_handle.write, data)

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
