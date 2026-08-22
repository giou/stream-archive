import asyncio
import logging
import os
import time
from collections.abc import Coroutine
from contextlib import nullcontext, suppress
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from streamlink.exceptions import NoPluginError, NoStreamsError, PluginError
from streamlink.session.session import Streamlink

from stream_archive import disk
from stream_archive.chat_recorder import ChatRecorder
from stream_archive.config import (
    AppConfig,
    bare_name,
    channel_url,
    is_kick_channel,
    kick_bare_name,
)
from stream_archive.recorder.chat_output import ChatOutputMixin
from stream_archive.recorder.common import _sanitize_filename
from stream_archive.recorder.disk_output import DiskOutputMixin
from stream_archive.recorder.streamlink_source import StreamlinkMixin
from stream_archive.recorder.youtube_output import YoutubeOutputMixin

logger = logging.getLogger(__name__)


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
        # Ride through short HLS playlist stalls: with the default queue-deadline
        # factor (3) and Kick's ~2s target duration, streamlink aborts after ~6s
        # without new segments — which fresh Kick streams routinely hit right
        # after go-live. Factor 10 raises the tolerance to ~20s; a genuinely dead
        # feed is still detected (and the poll cycle covers the rest).
        self._session.set_option("stream-segmented-queue-deadline", 10)
        self._plugin_loaded = False
        self._last_kick_block_notify = {}
        self._quick_ends = {}  # channel -> consecutive short YouTube recordings
        self._backoff_until = {}  # channel -> monotonic time before restart allowed
        self._youtube_starts = []
        self._held = {}  # channel -> hold dict (broadcast kept open awaiting reuse)
        self._ended_clean: set[str] = set()  # channels whose last task ended cleanly (stream over)

    async def start(
        self, channel: str, title: str | None = None, game: str | None = None, user_id: str | None = None
    ) -> bool:
        async with self._lock_for(channel):
            return await self._start_unlocked(channel, title=title, game=game, user_id=user_id)

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

        mode = self._config.channel_output_modes.get(channel, self._config.output_mode)
        self._load_plugin()
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
                if now_ts - self._last_kick_block_notify.get(channel, 0.0) >= 1800:
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

        entry: dict[str, Any] = {"tasks": [], "process": None, "youtube_info": None, "filepath": None}
        entry["started_at"] = time.monotonic()
        entry["mode"] = mode
        self._recordings[channel] = entry
        entry["title"] = title
        entry["game"] = game
        entry["user_id"] = user_id
        entry["quality"] = self._config.preferred_quality
        tasks = []
        live_url = channel_url(channel)

        now = datetime.now(ZoneInfo(self._config.timezone)).strftime("%d_%m_%Y-%H%M%S")
        safe_title = _sanitize_filename(stream_title)

        if mode in ("disk", "both"):
            recording_dir = f"{self._config.recording_dir}/{self._channel_dir(channel)}"
            os.makedirs(recording_dir, exist_ok=True)
            filename = f"{safe_title}-{now}.ts"
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

        self._ended_clean.discard(channel)
        logger.info("[recorder] Started recording %s (mode=%s)", channel, mode)
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
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            file_info = {
                "name": os.path.basename(filepath),
                "size_mb": round(size_mb, 2),
                "date": mtime.astimezone(ZoneInfo(self._config.timezone)).strftime("%d-%m-%Y %H:%M"),
            }

        return {"file_info": file_info, "youtube_info": youtube_info}

    async def restart(self, channel: str) -> bool:
        """Stop and immediately restart a recording with the current config.

        Bypasses monitor start gates (disk cap, max recordings, YouTube budget)
        intentionally: this is an admin-forced action. Suppresses the disk-mode
        live notification (the Telegram apply-result message is the feedback);
        a youtube-mode restart still sends the live notification once the new
        broadcast is created, because the apply-now restart ended the old
        broadcast and the link changed.
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
            # A clean stream end (e.g. the HLS feed stalls and streamlink closes
            # it) must also release the entry once all tasks are done: otherwise
            # the monitor sees the channel as recording and never restarts, and
            # the YouTube broadcast lingers until YouTube auto-ends it.
            logger.info("[recorder] [%s] Recording task ended", channel)
        if entry["tasks"]:
            return  # other recording tasks (e.g. the disk fallback) still running
        chat_recorder = entry.pop("chat_recorder", None)
        if chat_recorder:
            asyncio.create_task(self._finalize_chat(channel, chat_recorder))
        asyncio.create_task(self._finalize_kick_chat(entry))
        youtube_info = entry.get("youtube_info")
        if youtube_info:
            asyncio.create_task(self._release_broadcast(channel, youtube_info, entry))
        if entry.get("mode") in ("youtube", "both"):
            self._note_youtube_end(channel, entry)
        # Remember that the stream ended on its own (as opposed to a task
        # failure) so the monitor can skip restart attempts until the offline
        # event catches up — a dead stream just resolves to a 404 otherwise.
        if not entry.get("failed"):
            self._ended_clean.add(channel)
        del self._recordings[channel]

    def ended_clean(self, channel: str) -> bool:
        """True when the channel's last recording ended cleanly (stream over)."""
        return channel in self._ended_clean

    async def _abort(self, channel: str, reason: str) -> None:
        logger.warning("[recorder] [%s] Stopping recording: %s", channel, reason)
        if self._notifier:
            await self._notifier.notify(f"\u26d4 Stopped recording {channel}: {reason}")
        entry = self._recordings.pop(channel, None)
        if entry is None:
            return
        for task in entry.get("tasks", []):
            task.cancel()
        await asyncio.gather(*entry.get("tasks", []), return_exceptions=True)

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
        """Per active channel: settings the in-flight recording actually uses.

        Output mode and preferred quality are snapshotted at recording start;
        chat capture is the live state (chat disable stops in-flight capture
        immediately, so only chat-enable is a deferred effect).
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
