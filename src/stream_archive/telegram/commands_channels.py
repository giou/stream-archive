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
        note = await self._release_channel(ch)
        return f"{result}\n{note}" if note else result

    async def reconcile_removed_channels(self, removed: list[str]) -> list[str]:
        """Release every channel that left ``config.channels`` without /remove.

        An operator can delete a channel from config.json and apply the file
        with /reload, which replaces the config but runs no command. Without
        this step the capture, the chat writer, the YouTube re-stream, the
        monitor's live state and the platform subscriptions all keep running
        for a channel the config no longer monitors, and no later command can
        stop them: /remove refuses a channel that is not in the list, and the
        platform events for it are dropped as unmonitored.
        """
        notes: list[str] = []
        for ch in removed:
            note = await self._release_channel(ch)
            if note:
                notes.append(f"{ch}: {note}")
        return notes

    async def _release_channel(self, ch: str) -> str | None:
        """Stop the capture, the live state and the subscriptions of one channel.

        Returns a note for the admin when a capture was stopped or a stop
        failed, or None when there was nothing to stop.
        """
        note: str | None = None
        if self._recorder.is_recording(ch):
            try:
                await self._recorder.stop(ch)
                note = "Recording stopped."
            except Exception:
                logger.exception("[telegram] Failed to stop the recording of %s", ch)
                note = "\u26a0\ufe0f Recording stop failed \u2014 see logs."
        # The monitor keeps live state and a per-channel lock when the stop
        # fails, so tell it about the removal either way.
        self._monitor.remove_channel(ch)
        try:
            if is_kick_channel(ch):
                if self._kick_webhook:
                    await self._kick_webhook.remove_channel(ch)
            else:
                await self._eventsub.remove_channel(ch)
        except Exception:
            # The local state is released already; a subscription that stays
            # behind only costs a few ignored deliveries, and the sync loop
            # reconciles it on the next pass.
            logger.exception("[telegram] Failed to remove the subscriptions of %s", ch)
        return note
