from typing import Any

from stream_archive.config import (
    AppConfig,
    is_kick_channel,
    normalize_channel_name,
)


class ChannelsCommands:
    _config: AppConfig
    _apply: Any
    _recorder: Any
    _monitor: Any
    _eventsub: Any
    _kick_webhook: Any

    def handle_channels(self) -> str:
        return "\n".join(f"{i}. {ch}" for i, ch in enumerate(self._config.channels, 1))

    async def handle_add(self, args: list[str]) -> str:
        if len(args) != 1:
            return "Usage: /add <channel>"
        ch = normalize_channel_name(args[0])
        if ch is None:
            return f"\u274c Invalid channel name: {args[0]!r} (use twitch:<name> for Twitch or kick:<name> for Kick)"

        def mutate(candidate: AppConfig) -> None:
            if ch in candidate.channels:
                raise ValueError(f"{ch} is already monitored")
            candidate.channels.append(ch)

        result = self._apply(mutate, lambda c: f"Added {ch} \u2014 {len(c.channels)} channel(s) monitored")
        if not result.startswith("\u274c"):
            if is_kick_channel(ch):
                if self._kick_webhook:
                    await self._kick_webhook.add_channel(ch)
            else:
                await self._eventsub.add_channel(ch)
        return result

    async def handle_remove(self, args: list[str]) -> str:
        if len(args) != 1:
            return "Usage: /remove <channel>"
        ch = normalize_channel_name(args[0])
        if ch is None:
            return f"\u274c Invalid channel name: {args[0]!r} (use twitch:<name> for Twitch or kick:<name> for Kick)"

        def mutate(candidate: AppConfig) -> None:
            if ch not in candidate.channels:
                raise ValueError(f"{ch} is not in the monitored list")
            candidate.channels.remove(ch)
            candidate.channel_output_modes.pop(ch, None)
            candidate.channel_youtube_hold_seconds.pop(ch, None)
            candidate.channel_preferred_qualities.pop(ch, None)

        result = self._apply(mutate, lambda c: f"Removed {ch} \u2014 {len(c.channels)} channel(s) monitored")
        if not result.startswith("\u274c") and self._recorder.is_recording(ch):
            await self._recorder.stop(ch)
            self._monitor.remove_channel(ch)
        if not result.startswith("\u274c"):
            if is_kick_channel(ch):
                if self._kick_webhook:
                    await self._kick_webhook.remove_channel(ch)
            else:
                await self._eventsub.remove_channel(ch)
        return result
