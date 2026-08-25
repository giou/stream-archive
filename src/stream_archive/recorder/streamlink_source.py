import logging
import os
import subprocess
import threading
from contextlib import suppress
from typing import Any

from streamlink.exceptions import NoStreamsError, PluginError
from streamlink.session.session import Streamlink

from stream_archive.config import (
    AUDIO_ONLY_QUALITY,
    AppConfig,
    bare_name,
    channel_url,
    effective_quality,
    is_kick_channel,
)

logger = logging.getLogger(__name__)


class _AudioOnlyStream:
    """Wraps a stream so open() yields lossless audio-only fragmented MP4.

    Every audio_only recording uses this wrapper. On Twitch it remuxes
    the native audio-only HLS rendition. On Kick it removes the video
    track from a regular rendition because the plugin has no audio_only
    variant. A pump thread feeds the source bytes into ffmpeg
    (-c:a copy, no re-encode). Consumers read the output like a plain
    streamlink fd (read/close only).
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def open(self) -> Any:
        src = self._inner.open()
        try:
            proc = subprocess.Popen(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-i",
                    "pipe:0",
                    "-vn",
                    "-c:a",
                    "copy",
                    "-bsf:a",
                    "aac_adtstoasc",
                    "-f",
                    "ipod",
                    "-movflags",
                    "+empty_moov+default_base_moof",
                    "-frag_duration",
                    "2000000",
                    "pipe:1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except BaseException:
            with suppress(BaseException):
                src.close()
            raise

        stdin = proc.stdin
        stdout = proc.stdout
        stderr = proc.stderr
        assert stdin is not None and stdout is not None and stderr is not None

        def pump() -> None:
            try:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    stdin.write(chunk)
            except BrokenPipeError, OSError, ValueError:
                pass  # ffmpeg died. The consumer sees stdout EOF.
            finally:
                with suppress(BaseException):
                    stdin.close()
                with suppress(BaseException):
                    src.close()

        def drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                text = line.decode(errors="replace").strip()
                if text:
                    logger.warning("[recorder] [audio-filter] %s", text)

        threading.Thread(target=pump, daemon=True, name="audio-filter-pump").start()
        threading.Thread(target=drain_stderr, daemon=True, name="audio-filter-stderr").start()
        return _PipedFd(proc)


class _PipedFd:
    """read()/close() facade over ffmpeg stdout. close() reaps the process."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        stdout = proc.stdout
        assert stdout is not None
        self._stdout = stdout

    def read(self, size: int) -> bytes | None:
        return self._stdout.read(size)

    def close(self) -> None:
        with suppress(BaseException):
            self._stdout.close()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                with suppress(subprocess.TimeoutExpired):
                    self._proc.wait(timeout=5)


class StreamlinkMixin:
    _config: AppConfig
    _session: Streamlink
    _plugin_loaded: bool

    def _load_plugin(self) -> None:
        if self._plugin_loaded:
            return
        plugin_dir = self._config.plugin_dir
        if not os.path.isabs(plugin_dir):
            self._session.plugins.load_path(str(self._config._workdir / plugin_dir))
        else:
            self._session.plugins.load_path(plugin_dir)
        self._plugin_loaded = True

    def _resolve_stream(self, channel: str, title: str | None, game: str | None) -> tuple[Any, str, str, str]:
        if is_kick_channel(channel):
            # No proxy loop or ad-block workarounds. The built-in kick plugin
            # talks to the kick API itself and solves the JS challenge through
            # a browser when one is installed.
            plugin_name, plugin_class, resolved_url = self._session.resolve_url(channel_url(channel))
            plugin = plugin_class(self._session, resolved_url, options={})
            streams = plugin.streams()
        else:
            url = channel_url(channel)
            plugin_name, plugin_class, resolved_url = self._session.resolve_url(url)
            proxies = list(self._config.proxy_list)
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
                    raise  # offline or proxies exhausted, never retried
                except (PluginError, OSError) as err:
                    # Mirror the plugin's own proxy loop. Skip the failing
                    # proxy, and raise NoStreamsError after the last one.
                    if len(proxies) <= 1:
                        raise NoStreamsError from None
                    logger.warning(
                        "[recorder] [%s] proxy '%s' failed (%s); trying next proxy",
                        channel,
                        proxies[0],
                        err,
                    )
                    proxies = proxies[1:]
        if not streams:
            raise PluginError("No streams available")
        quality = effective_quality(self._config, channel)
        best: Any
        if quality == AUDIO_ONLY_QUALITY:
            native = streams.get("audio_only")
            if native is not None:
                # The native rendition carries AAC inside MPEG-TS. This remux
                # keeps the audio lossless and gives the file the .m4a format.
                best = _AudioOnlyStream(native)
            else:
                # The Kick plugin has no native audio-only rendition. Take the
                # 480p variant and let ffmpeg remove the video track. The
                # fallback is best, never worst, because worst lowers the
                # audio bitrate.
                base = streams.get("480p") or streams.get("best")
                if base is None:
                    raise PluginError("No stream available for audio-only extraction")
                best = _AudioOnlyStream(base)
        else:
            best = streams.get(quality) or streams.get("best")
        if best is None:
            raise PluginError(f"No '{quality}' or 'best' stream available")
        author = getattr(plugin, "author", None) or bare_name(channel)
        if title is None:
            title = getattr(plugin, "title", None) or "Untitled"
        if game is None:
            game = getattr(plugin, "category", None) or "Unknown"
        return best, author, title, game
