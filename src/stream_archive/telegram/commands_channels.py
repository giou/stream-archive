import logging
from typing import Any

from stream_archive.config import (
    AppConfig,
    is_kick_channel,
    normalize_channel_name,
)
from stream_archive.telegram.menu_state import is_error

logger = logging.getLogger(__name__)


class ChannelsCommands:
    _config: AppConfig
    _apply: Any
    _recorder: Any
    _monitor: Any
    _eventsub: Any
    _kick_webhook: Any

    def handle_channels(self) -> str:
        return "\n".join(f"{i}. {ch}" for i, ch in enumerate(self._config.channels, 1))

    async def handle_add(self, args: list[str], chat_id: int | None = None) -> str:
        if len(args) != 1:
            return "Usage: /add <channel>"
        ch = normalize_channel_name(args[0])
        if ch is None:
            return f"\u274c Invalid channel name: {args[0]!r} (use twitch:<name> for Twitch or kick:<name> for Kick)"

        def mutate(candidate: AppConfig) -> None:
            if ch in candidate.channels:
                msg = f"{ch} is already monitored"
                raise ValueError(msg)
            candidate.channels.append(ch)

        result: str = self._apply(
            mutate, lambda c: f"Added {ch} \u2014 {len(c.channels)} channel(s) monitored", chat_id
        )
        if is_error(result):
            return result
        if is_kick_channel(ch):
            if self._kick_webhook:
                await self._kick_webhook.add_channel(ch)
        else:
            await self._eventsub.add_channel(ch)
        return result

    async def handle_remove(self, args: list[str], chat_id: int | None = None) -> str:
        if len(args) != 1:
            return "Usage: /remove <channel>"
        ch = normalize_channel_name(args[0])
        if ch is None:
            return f"\u274c Invalid channel name: {args[0]!r} (use twitch:<name> for Twitch or kick:<name> for Kick)"

        def mutate(candidate: AppConfig) -> None:
            if ch not in candidate.channels:
                msg = f"{ch} is not in the monitored list"
                raise ValueError(msg)
            candidate.channels.remove(ch)
            candidate.channel_output_modes.pop(ch, None)
            candidate.channel_youtube_hold_seconds.pop(ch, None)
            candidate.channel_preferred_qualities.pop(ch, None)

        result: str = self._apply(
            mutate, lambda c: f"Removed {ch} \u2014 {len(c.channels)} channel(s) monitored", chat_id
        )
        if is_error(result):
            return result
        if self._recorder.is_recording(ch):
            try:
                await self._recorder.stop(ch)
                result += "\nRecording stopped."
            except Exception:
                # The config no longer holds the channel, so the monitor and
                # the subscriptions must still learn about the removal.
                logger.exception("[telegram] Failed to stop the recording of %s", ch)
                result += "\n\u26a0\ufe0f Recording stop failed \u2014 see logs."
        # The monitor keeps live state and a per-channel lock when
        # the stop fails, so tell it about the removal either way.
        self._monitor.remove_channel(ch)
        if is_kick_channel(ch):
            if self._kick_webhook:
                await self._kick_webhook.remove_channel(ch)
        else:
            await self._eventsub.remove_channel(ch)
        return result
