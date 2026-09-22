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
from stream_archive.recorder.common import _redact_credentials

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
                    try:
                        chunk = src.read(65536)
                    except (OSError, ValueError) as err:
                        # The source itself failed (for example a network
                        # drop). Log it: the recording ends truncated, and
                        # that must not look like an orderly end of stream.
                        logger.warning("[recorder] [audio-filter] source read failed: %s", err)
                        break
                    if not chunk:
                        break
                    try:
                        stdin.write(chunk)
                    except BrokenPipeError, OSError, ValueError:
                        break  # ffmpeg died. The consumer sees stdout EOF.
            finally:
                with suppress(BaseException):
                    stdin.close()
                with suppress(BaseException):
                    src.close()

        def drain_stderr() -> None:
            err = proc.stderr
            assert err is not None
            try:
                for line in err:
                    text = line.decode(errors="replace").strip()
                    if text:
                        logger.warning("[recorder] [audio-filter] %s", text)
            except ValueError:
                pass  # close() retired stderr; the process is gone.
            finally:
                with suppress(BaseException):
                    err.close()

        try:
            threading.Thread(target=pump, daemon=True, name="audio-filter-pump").start()
            threading.Thread(target=drain_stderr, daemon=True, name="audio-filter-stderr").start()
        except BaseException:
            # A failed start (for example "can't start new thread") would
            # leave ffmpeg and the source unreachable. Clean up, then report.
            with suppress(BaseException):
                src.close()
            with suppress(BaseException):
                proc.kill()
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            raise
        return _PipedFd(proc, stdin, src)


class _PipedFd:
    """read()/close() facade over ffmpeg stdout. close() reaps the process."""

    def __init__(self, proc: subprocess.Popen[bytes], stdin: Any, src: Any) -> None:
        self._proc = proc
        self._stdin = stdin
        self._src = src
        stdout = proc.stdout
        assert stdout is not None
        self._stdout = stdout

    def read(self, size: int) -> bytes | None:
        data: bytes | None = self._stdout.read(size)
        return data

    def close(self) -> None:
        # Wake the pump thread first. Closing stdin raises in a blocked
        # write, and closing the source raises in a blocked read, so the
        # thread reaches its finally and releases the rest. terminate()
        # alone leaves it parked in read() forever, with the network
        # connection and the stdin pipe still open.
        with suppress(BaseException):
            self._stdin.close()
        with suppress(BaseException):
            self._src.close()
        with suppress(BaseException):
            self._stdout.close()
        stderr = self._proc.stderr
        if stderr is not None:
            with suppress(BaseException):
                stderr.close()
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
    _plugin_lock: threading.Lock

    def _load_plugin(self) -> None:
        """Load the plugins of ``plugin_dir`` into the session. Run this once.

        The directory holds the ad-block Twitch plugin, which overrides the
        built-in plugin of the same name. Resolution runs in a worker
        thread, so a lock protects the one-time load. A load failure is not
        fatal: the built-in plugins stay in use, and the log names the
        directory.
        """
        if self._plugin_loaded:
            return
        with self._plugin_lock:
            if self._plugin_loaded:
                return
            plugin_dir = self._config.plugin_dir
            path = plugin_dir if os.path.isabs(plugin_dir) else str(self._config.workdir / plugin_dir)
            try:
                self._session.plugins.load_path(path)
            except Exception as e:
                logger.error("[recorder] Cannot load plugins from %s: %s", path, e)
            else:
                logger.info("[recorder] Loaded plugins from %s", path)
            finally:
                # Set the flag after the load finishes. A second thread then
                # waits on the lock instead of resolving with the built-in
                # plugin while load_path() still mutates the registry.
                self._plugin_loaded = True

    def _resolve_stream(self, channel: str, title: str | None, game: str | None) -> tuple[Any, str, str, str]:
        # The ad-block plugin lives in plugin_dir. Without this call, the
        # session uses the built-in plugin and the proxy list does nothing.
        self._load_plugin()
        if is_kick_channel(channel):
            # No proxy loop or ad-block workarounds. The built-in kick plugin
            # talks to the kick API itself and solves the JS challenge through
            # a browser when one is installed.
            _, plugin_class, resolved_url = self._session.resolve_url(channel_url(channel))
            plugin = plugin_class(self._session, resolved_url, options={})
            streams = plugin.streams()
        else:
            url = channel_url(channel)
            _, plugin_class, resolved_url = self._session.resolve_url(url)
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
                    if not proxies:
                        # No proxy to rotate: surface the real plugin error.
                        raise
                    if len(proxies) == 1:
                        raise NoStreamsError from err
                    logger.warning(
                        "[recorder] [%s] proxy '%s' failed (%s); trying next proxy",
                        channel,
                        _redact_credentials(proxies[0]),
                        _redact_credentials(str(err)),
                    )
                    proxies = proxies[1:]
        if not streams:
            msg = "No streams available"
            raise PluginError(msg)
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
                    msg = "No stream available for audio-only extraction"
                    raise PluginError(msg)
                best = _AudioOnlyStream(base)
        else:
            best = streams.get(quality) or streams.get("best")
        if best is None:
            msg = f"No '{quality}' or 'best' stream available"
            raise PluginError(msg)
        author = getattr(plugin, "author", None) or bare_name(channel)
        if title is None:
            title = getattr(plugin, "title", None) or "Untitled"
        if game is None:
            game = getattr(plugin, "category", None) or "Unknown"
        return best, author, title, game
