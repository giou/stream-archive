import logging
import os
from typing import Any

from streamlink.exceptions import NoStreamsError, PluginError
from streamlink.session.session import Streamlink

from stream_archive.config import (
    AppConfig,
    bare_name,
    channel_url,
    is_kick_channel,
)

logger = logging.getLogger(__name__)


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
        quality = self._config.preferred_quality
        best = streams.get(quality) or streams.get("best")
        if best is None:
            raise PluginError(f"No '{quality}' or 'best' stream available")
        author = getattr(plugin, "author", None) or bare_name(channel)
        if title is None:
            title = getattr(plugin, "title", None) or "Untitled"
        if game is None:
            game = getattr(plugin, "category", None) or "Unknown"
        return best, author, title, game
