import asyncio
import logging
import os
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
from stream_archive.recorder.types import KickChatState, Recording

logger = logging.getLogger(__name__)


class ChatOutputMixin:
    _recordings: dict[str, Recording]
    #: Chat captures whose writer is still open, keyed by channel.
    #: A capture keeps its entry only until its end path detaches it, and
    #: the finalizer then runs on the detached entry; this registry is what
    #: the ingest gate and the deletion passes consult for that window.
    _open_chat: dict[str, KickChatState]
    #: Real paths of every open chat writer, so a deletion pass can never
    #: unlink a file a capture still holds (entry or not).
    _chat_paths: set[str]

    def _chat_writer_paths(self, chat_path: str) -> set[str]:
        """Real paths of one chat capture: the final file and its tmp copy."""
        return {os.path.realpath(chat_path), os.path.realpath(chat_path + ".tmp")}

    def _register_chat_paths(self, chat_path: str) -> None:
        """Track an open chat writer, of either platform."""
        self._chat_paths |= self._chat_writer_paths(chat_path)

    def _release_chat_paths(self, chat_path: str) -> None:
        """Forget a writer that is closed, renamed or discarded."""
        self._chat_paths -= self._chat_writer_paths(chat_path)

    def _register_kick_chat(self, channel: str, state: KickChatState, chat_path: str) -> None:
        """Track one Kick capture, for the ingest gate and the deletion passes."""
        self._open_chat[channel] = state
        self._register_chat_paths(chat_path)

    def _release_kick_chat(self, channel: str, state: KickChatState, chat_path: str) -> None:
        """Forget a Kick capture whose writer is closed, renamed or discarded.

        The channel can already hold a newer capture when a finalizer that was
        still running when the next recording started gets here, so only the
        registration this call owns is dropped.
        """
        if self._open_chat.get(channel) is state:
            self._open_chat.pop(channel, None)
        self._release_chat_paths(chat_path)

    async def _finalize_chat(self, channel: str, chat_recorder: Any) -> None:
        """Finalize chat after a failure. The method logs errors and never raises."""
        try:
            await chat_recorder.stop()
        except Exception as e:
            logger.error("[recorder] [%s] chat finalize error: %s", channel, e)
        finally:
            # The writer is closed or renamed now, so its paths are archive
            # files again and the deletion passes may consider them.
            self._release_chat_paths(chat_recorder.chat_path)

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
            # Every end path but /chat off detaches the entry before the
            # finalizer finishes, and the finalizer keeps the file open until
            # it renames it. A delivery inside that window belongs in the file
            # being written, not in the bin: the contract is stated at
            # _finalize_kick_chat, and the /chat off path already behaves this
            # way because it keeps the entry.
            state = self._open_chat.get(channel)
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
            self._release_kick_chat(state["channel"], state, state["writer"].path)
