import asyncio
import logging
import time
from typing import Any

from stream_archive.kick_chat import (
    MAX_EMOTES_PER_RECORDING,
    build_comment,
    chat_root_trailer,
    collect_emote_names,
    embedded_data,
    streamer_identity,
)
from stream_archive.recorder.types import Recording

logger = logging.getLogger(__name__)


class ChatOutputMixin:
    _recordings: dict[str, Recording]

    async def _finalize_chat(self, channel: str, chat_recorder: Any) -> None:
        """Finalize chat after a failure. The method logs errors and never raises."""
        try:
            await chat_recorder.stop()
        except Exception as e:
            logger.error("[recorder] [%s] chat finalize error: %s", channel, e)

    async def stop_chat(self, channel: str, platform: str | None = None) -> None:
        """Stop and finalize chat capture for an active recording.

        The video itself keeps recording. platform=None stops both recorders,
        "twitch" stops only the IRC recorder, and "kick" stops only kick chat.
        """
        entry = self._recordings.get(channel)
        if entry is None:
            return
        if platform in (None, "twitch"):
            chat_recorder = entry.pop("chat_recorder", None)
            if chat_recorder:
                await self._finalize_chat(channel, chat_recorder)
        if platform in (None, "kick"):
            await self._finalize_kick_chat(entry)

    async def add_kick_chat(self, channel: str, payload: dict[str, Any]) -> None:
        """Write one normalized kick chat message to the active recording's file.

        The method does nothing when nobody records the channel. Kick webhook
        delivery is best-effort, and there is no replay.
        """
        entry = self._recordings.get(channel)
        state = entry.get("kick_chat") if entry is not None else None
        if state is None:
            return
        if state.get("streamer_id") is None:
            streamer_id, username = streamer_identity(payload, state["slug"])
            if streamer_id is not None:
                state["streamer_id"] = streamer_id
                state["streamer_username"] = username
        comment = build_comment(payload, state.get("streamer_id"), state["video_id"], state["start"])
        state["writer"].add_comment(comment)
        skipped = collect_emote_names(state["emote_names"], payload.get("content") or "")
        if skipped:
            if not state.get("emote_skipped"):
                logger.warning(
                    "[recorder] [%s] kick chat reached the emote limit (%d ids); extra emotes stay as text tokens",
                    channel,
                    MAX_EMOTES_PER_RECORDING,
                )
            state["emote_skipped"] = state.get("emote_skipped", 0) + skipped

    async def _finalize_kick_chat(self, entry: Recording) -> None:
        """Write the kick chat trailer, then rename the file into place.

        The method skips entries without messages. The output file is
        TwitchDownloader ChatRoot JSON with embedded emote images (see
        kick_chat.embedded_data). The state stays in the entry until the
        trailer is written, so a message that arrives during the emote fetch
        still lands in the file. The finalizing flag blocks a second run.

        The writer never stays open: every path either writes the trailer,
        discards the file, or reports the failure that closed it. A cancelled
        emote fetch still writes the trailer, because the entry drops the
        state here and no other call can close the writer.
        """
        state = entry.get("kick_chat")
        if state is None or state.get("finalizing"):
            return
        # Set the flag before the first await. A second finalize call for the
        # same entry then returns instead of writing the trailer twice.
        state["finalizing"] = True
        writer = state["writer"]
        try:
            if writer.comments == 0:
                writer.discard()
                return
            duration_s = time.monotonic() - entry.get("started_at", time.monotonic())
            trailer = chat_root_trailer(
                state["slug"],
                state.get("title"),
                state["started_wall"],
                state["start"],
                duration_s,
                state.get("streamer_id"),
                state.get("streamer_username") or state["slug"],
            )
            try:
                embedded = await embedded_data(state.get("emote_names") or {})
            except asyncio.CancelledError:
                # Write the trailer now, without the emote images. The
                # comments must not stay in an open, unusable tmp file.
                writer.close(trailer)
                raise
            except Exception as e:
                logger.warning("[recorder] kick chat emote fetch failed: %s", e)
                embedded = None
            if embedded is not None:
                trailer["embeddedData"] = embedded
            if writer.close(trailer):
                logger.info(
                    "[recorder] kick chat saved: %s (%d messages, %d emote ids skipped)",
                    state["path"],
                    writer.comments,
                    state.get("emote_skipped", 0),
                )
        except Exception as e:
            logger.error("[recorder] kick chat finalize failed: %s", e)
            # A write failure keeps the partial file for recovery (see
            # chat_writer). The retention pass removes a stale .tmp file, so
            # keep the collected comments on disk.
        finally:
            entry.pop("kick_chat", None)
