import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlink
from streamlink.exceptions import NoPluginError, NoStreamsError, PluginError

from src.stream_archive import disk
from src.stream_archive.chat_recorder import ChatRecorder
from src.stream_archive.config import bare_name, channel_url, is_kick_channel, kick_bare_name
from src.stream_archive.kick_chat import build_chat_root, embed_kick_emotes

logger = logging.getLogger(__name__)


def _sanitize_filename(name):
    return re.sub(r"[<>:\"/\\|?*]", "_", name)[:200]


class Recorder:
    def __init__(self, config, youtube_streamer=None, notifier=None):
        self._config = config
        self._youtube = youtube_streamer
        self._notifier = notifier
        self._recordings = {}
        self._session = streamlink.Streamlink()
        self._session.set_option("http-timeout", 30)
        self._plugin_loaded = False
        self._last_kick_block_notify = {}

    def _load_plugin(self):
        if self._plugin_loaded:
            return
        plugin_dir = self._config["plugin_dir"]
        if not os.path.isabs(plugin_dir):
            plugin_dir = self._config["_workdir"] / plugin_dir
        self._session.plugins.load_path(str(plugin_dir))
        self._plugin_loaded = True

    def _resolve_stream(self, channel, title, game):
        if is_kick_channel(channel):
            # No proxy loop / ad-block workarounds: streamlink's built-in kick
            # plugin talks to kick's API itself (and solves the JS challenge
            # via a browser when one is installed).
            plugin_name, plugin_class, resolved_url = self._session.resolve_url(channel_url(channel))
            plugin = plugin_class(self._session, resolved_url, options={})
            streams = plugin.streams()
        else:
            url = channel_url(channel)
            plugin_name, plugin_class, resolved_url = self._session.resolve_url(url)
            proxies = list(self._config["proxy_list"])
            while True:
                plugin = plugin_class(
                    self._session,
                    resolved_url,
                    options={
                        "proxy-playlist": proxies,
                        "supported-codecs": ["h264"],
                    },
                )
                try:
                    streams = plugin.streams()
                    break
                except NoStreamsError:
                    raise  # channel offline, or all proxies exhausted — never retried
                except (PluginError, OSError) as err:
                    # Mirrors the plugin's proxy loop: skip the failing proxy and try
                    # the next; after the last proxy, match the plugin's NoStreamsError.
                    if len(proxies) <= 1:
                        raise NoStreamsError
                    logger.warning(
                        "[recorder] [%s] proxy '%s' failed (%s); trying next proxy",
                        channel, proxies[0], err,
                    )
                    proxies = proxies[1:]
        if not streams:
            raise PluginError("No streams available")
        quality = self._config.get("preferred_quality", "best")
        best = streams.get(quality) or streams.get("best")
        if best is None:
            raise PluginError(f"No '{quality}' or 'best' stream available")
        author = getattr(plugin, "author", None) or bare_name(channel)
        if title is None:
            title = getattr(plugin, "title", None) or "Untitled"
        if game is None:
            game = getattr(plugin, "category", None) or "Unknown"
        return best, author, title, game

    async def start(self, channel, title=None, game=None, user_id=None):
        if channel in self._recordings:
            return True

        mode = self._config.get("channel_output_modes", {}).get(channel, self._config["output_mode"])
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
                now = time.monotonic()
                if now - self._last_kick_block_notify.get(channel, 0.0) >= 1800:
                    self._last_kick_block_notify[channel] = now
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

        entry = {"tasks": [], "process": None, "youtube_info": None, "filepath": None}
        entry["started_at"] = time.monotonic()
        entry["mode"] = mode
        self._recordings[channel] = entry
        tasks = []
        live_url = channel_url(channel)

        now = datetime.now(ZoneInfo(self._config["timezone"])).strftime("%d_%m_%Y-%H%M%S")
        safe_title = _sanitize_filename(stream_title)

        if mode in ("disk", "both"):
            recording_dir = f"{self._config['recording_dir']}/{self._channel_dir(channel)}"
            os.makedirs(recording_dir, exist_ok=True)
            filename = f"{safe_title}-{now}.ts"
            filepath = os.path.join(recording_dir, filename)
            entry["filepath"] = filepath

        if mode == "disk":
            disk_task = self._track(
                channel, self._record_disk(channel, entry["filepath"], best)
            )
            tasks.append(disk_task)
            if self._notifier:
                await self._notifier.notify_live(channel, stream_title, stream_game, live_url)
        elif mode == "youtube":
            if self._youtube is not None:
                yt_task = self._track(
                    channel, self._stream_youtube(channel, author, stream_title, stream_game, best, None)
                )
                tasks.append(yt_task)
        elif mode == "both":
            if self._youtube is not None:
                yt_task = self._track(
                    channel, self._stream_youtube(channel, author, stream_title, stream_game, best, entry["filepath"])
                )
                tasks.append(yt_task)

        if not tasks:
            del self._recordings[channel]
            return False

        if self._config.get("record_chat", True) and not is_kick_channel(channel):
            chat_dir = disk.chat_dir_path(self._config)
            chat_path = os.path.join(chat_dir, self._channel_dir(channel), f"{safe_title}-{now}.chat.json")
            os.makedirs(os.path.dirname(chat_path), exist_ok=True)
            chat_recorder = ChatRecorder(bare_name(channel), chat_path, stream_title, stream_game,
                                         author=author, user_id=user_id)
            entry["chat_recorder"] = chat_recorder
            entry["chat_task"] = chat_recorder.start()

        if is_kick_channel(channel) and self._config.get("kick", {}).get("record_chat", True):
            chat_dir = disk.chat_dir_path(self._config)
            chat_path = os.path.join(chat_dir, "kick", kick_bare_name(channel), f"{safe_title}-{now}.chat.json")
            os.makedirs(os.path.dirname(chat_path), exist_ok=True)
            entry["kick_chat"] = {
                "path": chat_path,
                "messages": [],
                "title": stream_title,
                "channel": channel,
                "started_wall": datetime.now(ZoneInfo(self._config["timezone"])).isoformat(),
            }

        entry["tasks"] = tasks

        disk_cfg = self._config.get("disk", {})
        if disk_cfg.get("max_total_gb", 0) > 0:
            entry["watchdog"] = asyncio.create_task(self._watch_growth(channel))

        logger.info("[recorder] Started recording %s (mode=%s)", channel, mode)
        return True

    def _channel_dir(self, channel):
        """Recording subdirectory: kick/<slug>, twitch/<name>, legacy bare -> bare."""
        if is_kick_channel(channel):
            return f"kick/{kick_bare_name(channel)}"
        if channel.startswith("twitch:"):
            return f"twitch/{bare_name(channel)}"
        return channel

    def _track(self, channel, coro):
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda t: self._on_task_finished(channel, t))
        return task

    def _on_task_finished(self, channel, task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error("[recorder] [%s] Recording task failed: %s", channel, exc)
        entry = self._recordings.get(channel)
        if entry is not None and task in entry.get("tasks", []):
            chat_recorder = entry.pop("chat_recorder", None)
            if chat_recorder:
                asyncio.create_task(self._finalize_chat(channel, chat_recorder))
            asyncio.create_task(self._finalize_kick_chat(entry))
            del self._recordings[channel]

    async def _finalize_chat(self, channel, chat_recorder):
        """Fire-and-forget chat finalize for the failure path; never raises."""
        try:
            await chat_recorder.stop()
        except Exception as e:
            logger.error("[recorder] [%s] chat finalize error: %s", channel, e)

    async def _watch_growth(self, channel):
        try:
            cfg = self._config["disk"]
            interval = cfg["check_interval_s"]
            while True:
                await asyncio.sleep(interval)
                entry = self._recordings.get(channel)
                if entry is None:
                    return
                snap = await disk.disk_snapshot(self._config)
                cap = cfg.get("max_total_gb", 0)
                if cap > 0 and snap["dir_gb"] >= cap:
                    if cfg.get("delete_oldest", True):
                        await self.delete_oldest_to_cap()
                        snap = await disk.disk_snapshot(self._config)
                    if snap["dir_gb"] >= cap:
                        await self._abort(channel, f"recording archive at {cap:g} GB cap")
                        return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[recorder] [%s] watchdog error: %s", channel, e)

    async def _abort(self, channel, reason):
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
            try:
                await self._youtube.end_stream(entry["youtube_info"]["broadcast_id"])
            except Exception as e:
                logger.error("[recorder] [youtube] Error ending broadcast for %s: %s", channel, e)

    async def stop_chat(self, channel, platform=None):
        """Stop and finalize chat capture for an active recording; the video continues.

        platform=None stops both; "twitch" only the IRC recorder; "kick" only kick chat.
        """
        entry = self._recordings.get(channel)
        if entry is None:
            return
        if platform in (None, "twitch"):
            chat_recorder = entry.pop("chat_recorder", None)
            if chat_recorder:
                try:
                    await chat_recorder.stop()
                except Exception as e:
                    logger.error("[recorder] [%s] chat finalize error: %s", channel, e)
        if platform in (None, "kick"):
            await self._finalize_kick_chat(entry)

    async def add_kick_chat(self, channel, payload):
        """Append one normalized kick chat message to the active recording's buffer.

        No-op when the channel is not being recorded (webhook delivery is
        best-effort and there is no replay).
        """
        entry = self._recordings.get(channel)
        if entry is not None and entry.get("kick_chat") is not None:
            entry["kick_chat"]["messages"].append(payload)

    async def _finalize_kick_chat(self, entry):
        """Write the collected kick chat buffer atomically; skip when empty.

        Output is TwitchDownloader ChatRoot JSON with embedded emote images
        (see kick_chat.build_chat_root / embed_kick_emotes).
        """
        kick_chat = entry.pop("kick_chat", None)
        if kick_chat is None or not kick_chat.get("messages"):
            return
        try:
            duration_s = time.monotonic() - entry.get("started_at", time.monotonic())
            root = build_chat_root(
                kick_chat["channel"],
                kick_bare_name(kick_chat["channel"]),
                kick_chat.get("title"),
                kick_chat.get("started_wall"),
                kick_chat["messages"],
                duration_s=duration_s,
            )
            await embed_kick_emotes(root)
            tmp = kick_chat["path"] + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(root, f, ensure_ascii=False, indent=2)
            os.replace(tmp, kick_chat["path"])
            logger.info(
                "[recorder] kick chat saved: %s (%d messages)",
                kick_chat["path"], len(kick_chat["messages"]),
            )
        except Exception as e:
            logger.error("[recorder] kick chat finalize failed: %s", e)

    def is_recording(self, channel):
        return channel in self._recordings

    def active_channels(self):
        """Names of channels currently being recorded, sorted."""
        return sorted(self._recordings)

    async def disk_snapshot(self):
        return await disk.disk_snapshot(self._config)

    def recording_info(self):
        """Per active channel: duration + current file size (approx). Sorted by channel."""
        out = []
        now = time.monotonic()
        for channel in sorted(self._recordings):
            e = self._recordings[channel]
            size_mb = None
            fp = e.get("filepath")
            if fp:
                try:
                    size_mb = os.path.getsize(fp) / (1024 * 1024)
                except OSError:
                    pass
            out.append({
                "channel": channel,
                "mode": e.get("mode"),
                "duration_s": round(now - e.get("started_at", now)),
                "size_mb": size_mb,
            })
        return out

    def youtube_active_count(self):
        """Active recordings whose mode uses a YouTube re-stream (for the uplink cap)."""
        return sum(1 for e in self._recordings.values() if e.get("mode") in ("youtube", "both"))

    async def _record_disk(self, channel, filepath, stream):
        logger.info("[recorder] [disk] %s -> %s", channel, filepath)
        loop = asyncio.get_running_loop()
        try:
            fd = await loop.run_in_executor(None, stream.open)
            with open(filepath, "wb") as f:
                while True:
                    data = await loop.run_in_executor(None, fd.read, 65536)
                    if not data:
                        break
                    f.write(data)
            logger.info("[recorder] [disk] %s finished", channel)
        except asyncio.CancelledError:
            logger.info("[recorder] [disk] %s cancelled", channel)
        except Exception as e:
            logger.error("[recorder] [disk] %s error: %s", channel, e)
            raise
        finally:
            try:
                fd.close()
            except Exception:
                pass

    async def _stream_youtube(self, channel, author, title, game, stream, filepath):
        logger.info("[recorder] [youtube] %s resolving...", channel)
        try:
            youtube_info = await self._youtube.create_stream(
                author, title, channel, game
            )
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
                    recording_dir = f"{self._config['recording_dir']}/{self._channel_dir(channel)}"
                    os.makedirs(recording_dir, exist_ok=True)
                    now = datetime.now(ZoneInfo(self._config["timezone"])).strftime("%d_%m_%Y-%H%M%S")
                    safe_title = _sanitize_filename(f"{author} - {title}")
                    filepath = os.path.join(recording_dir, f"{safe_title}-{now}.ts")
                    entry["filepath"] = filepath
                    logger.info("[recorder] Rate limited — falling back to disk recording for %s", channel)
                    disk_task = self._track(
                        channel, self._record_disk(channel, filepath, stream)
                    )
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
            await self._notifier.notify_live(
                channel, title, game, channel_url(channel), youtube_info["youtube_url"]
            )

        rtmp_url = youtube_info["rtmp_url"]
        ffmpeg_cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-fflags", "+genpts+igndts",
            "-re",
            "-i", "pipe:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "160k",
            "-max_muxing_queue_size", "1024",
            "-f", "flv",
            "-flvflags", "no_duration_filesize",
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

        pipe_task = asyncio.create_task(
            self._pipe_stream(channel, stream, process, filepath)
        )
        stderr_task = asyncio.create_task(
            self._read_ffmpeg_stderr(channel, process)
        )

        try:
            results = await asyncio.gather(pipe_task, stderr_task)
        except asyncio.CancelledError:
            pipe_task.cancel()
            try:
                await pipe_task
            except (asyncio.CancelledError, Exception):
                pass
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

    async def _pipe_stream(self, channel, stream, process, filepath):
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
                file_handle = open(filepath, "wb")

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
            try:
                fd.close()
            except Exception:
                pass
            if file_handle:
                try:
                    file_handle.close()
                except Exception:
                    pass
            try:
                process.stdin.close()
            except Exception:
                pass
        return clean

    async def _read_ffmpeg_stderr(self, channel, process):
        if process.stderr is None:
            return
        try:
            async for line in process.stderr:
                text = line.decode(errors="replace").strip()
                if text and "Resumed reading" not in text:
                    logger.info("[recorder] [ffmpeg:%s] %s", channel, text)
        except asyncio.CancelledError:
            pass

    async def stop(self, channel):
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
            try:
                await self._youtube.end_stream(youtube_info["broadcast_id"])
            except Exception as e:
                logger.error("[recorder] [youtube] Error ending broadcast for %s: %s", channel, e)

        filepath = entry.get("filepath")
        file_info = None
        if filepath and os.path.exists(filepath):
            st = os.stat(filepath)
            size_mb = st.st_size / (1024 * 1024)
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            file_info = {
                "name": os.path.basename(filepath),
                "size_mb": round(size_mb, 2),
                "date": mtime.astimezone(ZoneInfo(self._config["timezone"])).strftime("%d-%m-%Y %H:%M"),
            }

        return {"file_info": file_info, "youtube_info": youtube_info}

    async def delete_oldest_to_cap(self):
        """Delete oldest .ts files until under disk.max_total_gb; returns (files_removed, freed_gb)."""
        cap = self._config["disk"]["max_total_gb"]
        if cap <= 0:
            return (0, 0.0)
        loop = asyncio.get_running_loop()

        def _delete_oldest():
            base = disk.recording_dir_path(self._config)
            if not base.exists():
                return (0, 0.0)
            files = sorted((p for p in base.rglob("*.ts")), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            cap_bytes = int(cap * 1024**3)
            removed = freed = 0
            for p in files:
                if total < cap_bytes:
                    break
                size = p.stat().st_size
                p.unlink(missing_ok=True)
                total -= size
                removed += 1
                freed += size
                logger.info("[recorder] Deleted oldest to stay under %s GB cap: %s", cap, p)
            return removed, freed

        return await loop.run_in_executor(None, _delete_oldest)

    async def cleanup_old_recordings(self, retention_days):
        """Delete .ts and .chat.json files older than retention_days days; returns count removed."""
        if retention_days <= 0:
            return 0
        base = disk.recording_dir_path(self._config)
        chat_base = disk.chat_dir_path(self._config)
        if not base.exists() and not chat_base.exists():
            return 0
        cutoff = time.time() - retention_days * 86400
        loop = asyncio.get_running_loop()

        def _scan():
            found = []
            if base.exists():
                for path in base.rglob("*.ts"):
                    try:
                        if path.stat().st_mtime < cutoff:
                            found.append(path)
                    except OSError:
                        continue
            if chat_base.exists():
                for path in chat_base.rglob("*.chat.json"):
                    try:
                        if path.stat().st_mtime < cutoff:
                            found.append(path)
                    except OSError:
                        continue
            return found

        removed = 0
        try:
            for path in await loop.run_in_executor(None, _scan):
                path.unlink(missing_ok=True)
                removed += 1
                logger.info("[recorder] Removed expired recording: %s", path)
        except OSError as e:
            logger.error("[recorder] Cleanup failed: %s", e)
        return removed

    async def stop_all(self):
        for channel in list(self._recordings):
            await self.stop(channel)

    async def close(self):
        await self.stop_all()
