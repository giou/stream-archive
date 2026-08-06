import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlink
from streamlink.exceptions import NoPluginError, NoStreamsError, PluginError

logger = logging.getLogger(__name__)


def _sanitize_filename(name):
    return re.sub(r"[<>:\"/\\|?*]", "_", name)[:200]


class Recorder:
    def __init__(self, config, youtube_streamer=None, notifier=None):
        self._config = config
        self._youtube = youtube_streamer
        self._notifier = notifier
        self._mode = config["output_mode"]
        self._recordings = {}
        self._session = streamlink.Streamlink()
        self._session.set_option("http-timeout", 30)
        self._plugin_loaded = False

    def _load_plugin(self):
        if self._plugin_loaded:
            return
        plugin_dir = self._config["plugin_dir"]
        if not os.path.isabs(plugin_dir):
            plugin_dir = self._config["_workdir"] / plugin_dir
        self._session.plugins.load_path(str(plugin_dir))
        self._plugin_loaded = True

    def _resolve_stream(self, channel, title, game):
        url = f"https://twitch.tv/{channel}"
        plugin_name, plugin_class, resolved_url = self._session.resolve_url(url)
        plugin = plugin_class(
            self._session,
            resolved_url,
            options={
                "proxy-playlist": self._config["proxy_list"],
                "supported-codecs": ["h264"],
            },
        )
        streams = plugin.streams()
        if not streams or "best" not in streams:
            raise PluginError("No best stream available")
        best = streams["best"]
        author = getattr(plugin, "author", None) or channel
        if title is None:
            title = getattr(plugin, "title", None) or "Untitled"
        if game is None:
            game = getattr(plugin, "category", None) or "Unknown"
        return best, author, title, game

    async def start(self, channel, title=None, game=None):
        if channel in self._recordings:
            return True

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
            return False
        except Exception as e:
            logger.error("[recorder] Unexpected error resolving %s: %s", channel, e)
            return False

        entry = {"tasks": [], "process": None, "youtube_info": None, "filepath": None}
        self._recordings[channel] = entry
        tasks = []
        twitch_url = f"https://twitch.tv/{channel}"

        if self._mode in ("disk", "both"):
            recording_dir = f"{self._config['recording_dir']}/{channel}"
            os.makedirs(recording_dir, exist_ok=True)
            now = datetime.now(ZoneInfo(self._config["timezone"])).strftime("%d_%m_%Y-%H%M%S")
            safe_title = _sanitize_filename(stream_title)
            filename = f"{safe_title}-{now}.ts"
            filepath = os.path.join(recording_dir, filename)
            entry["filepath"] = filepath

        if self._mode == "disk":
            disk_task = asyncio.create_task(
                self._record_disk(channel, entry["filepath"], best)
            )
            tasks.append(disk_task)
            if self._notifier:
                await self._notifier.notify_live(channel, stream_title, stream_game, twitch_url)
        elif self._mode == "youtube":
            if self._youtube is not None:
                yt_task = asyncio.create_task(
                    self._stream_youtube(channel, author, stream_title, stream_game, best, None)
                )
                tasks.append(yt_task)
        elif self._mode == "both":
            if self._youtube is not None:
                yt_task = asyncio.create_task(
                    self._stream_youtube(channel, author, stream_title, stream_game, best, entry["filepath"])
                )
                tasks.append(yt_task)

        if not tasks:
            del self._recordings[channel]
            return False

        entry["tasks"] = tasks
        logger.info("[recorder] Started recording %s (mode=%s)", channel, self._mode)
        return True

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
            if "rate limit" in str(e).lower() or "403" in str(e):
                msg = (
                    f"\u26a0\ufe0f YouTube rate limit reached!\n"
                    f"Channel: {channel}\n"
                    f"Stream: {title or 'Unknown'}\n"
                    f"Stream link: https://twitch.tv/{channel}"
                )
                if self._notifier:
                    await self._notifier.notify(msg)
                entry = self._recordings.get(channel)
                if entry is not None:
                    recording_dir = f"{self._config['recording_dir']}/{channel}"
                    os.makedirs(recording_dir, exist_ok=True)
                    now = datetime.now(ZoneInfo(self._config["timezone"])).strftime("%d_%m_%Y-%H%M%S")
                    safe_title = _sanitize_filename(f"{author} - {title}")
                    filepath = os.path.join(recording_dir, f"{safe_title}-{now}.ts")
                    entry["filepath"] = filepath
                    logger.info("[recorder] Rate limited — falling back to disk recording for %s", channel)
                    disk_task = asyncio.create_task(
                        self._record_disk(channel, filepath, stream)
                    )
                    entry["tasks"].append(disk_task)
                    if self._notifier:
                        twitch_url = f"https://twitch.tv/{channel}"
                        await self._notifier.notify_live(channel, title, game, twitch_url)
            return

        entry = self._recordings.get(channel)
        if entry is None:
            return
        entry["youtube_info"] = youtube_info

        twitch_url = f"https://twitch.tv/{channel}"
        if self._notifier:
            await self._notifier.notify_live(
                channel, title, game, twitch_url, youtube_info["youtube_url"]
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
            await asyncio.gather(pipe_task, stderr_task)
        except asyncio.CancelledError:
            pipe_task.cancel()
            try:
                await pipe_task
            except (asyncio.CancelledError, Exception):
                pass
            logger.info("[recorder] [youtube] %s cancelled", channel)
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

    async def _pipe_stream(self, channel, stream, process, filepath):
        loop = asyncio.get_running_loop()
        try:
            fd = await loop.run_in_executor(None, stream.open)
        except Exception as e:
            logger.error("[recorder] [youtube] %s stream open failed: %s", channel, e)
            return

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
        youtube_info = entry.get("youtube_info")

        for task in entry.get("tasks", []):
            task.cancel()
        if entry.get("tasks"):
            await asyncio.gather(*entry["tasks"], return_exceptions=True)

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

    async def cleanup_old_recordings(self, retention_days):
        """Delete .ts files under recording_dir older than retention_days days; returns count removed."""
        if retention_days <= 0:
            return 0
        base = Path(self._config["recording_dir"])
        if not base.is_absolute():
            base = self._config["_workdir"] / base
        if not base.exists():
            return 0
        cutoff = time.time() - retention_days * 86400
        loop = asyncio.get_running_loop()

        def _scan():
            found = []
            for path in base.rglob("*.ts"):
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
