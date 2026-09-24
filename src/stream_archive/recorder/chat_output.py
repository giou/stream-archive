import asyncio
import logging
import os
import time
from typing import Any

from stream_archive.emotes import MAX_EMOTES_PER_RECORDING, embed_images, fetch_channel_emotes
from stream_archive.kick_chat import (
    EMOTE_URL,
    build_comment,
    chat_root_trailer,
    collect_emote_names,
    streamer_identity,
)
from stream_archive.recorder.types import KickChatState, Recording

logger = logging.getLogger(__name__)


def _valid_kick_message(payload: Any) -> bool:
    """True when a kick chat payload has the field types the converter assumes.

    Kick delivers JSON, so any field can carry any type. The webhook normalizes
    the shape first (see kick_webhook); this check stops a malformed delivery
    from raising out of the best-effort ingest path.
    """
    if not isinstance(payload, dict):
        return False
    content = payload.get("content")
    if content and not isinstance(content, str):
        return False
    if any(payload.get(key) and not isinstance(payload[key], dict) for key in ("sender", "broadcaster")):
        return False
    badges = payload.get("badges")
    return not badges or (isinstance(badges, list) and all(isinstance(b, dict) for b in badges))


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

        A cancellation is deferred until both finalizers ran. CancelledError
        is a BaseException, so letting it through would skip the second
        writer, and its tmp file is then the only copy of the messages.
        """
        entry = self._recordings.get(channel)
        if entry is None:
            return
        cancelled: asyncio.CancelledError | None = None
        if platform in (None, "twitch"):
            chat_recorder = entry.pop("chat_recorder", None)
            if chat_recorder:
                try:
                    await self._finalize_chat(channel, chat_recorder)
                except asyncio.CancelledError as e:
                    cancelled = e
        if platform in (None, "kick"):
            try:
                await self._finalize_kick_chat(entry)
            except asyncio.CancelledError as e:
                cancelled = e
        if cancelled is not None:
            raise cancelled

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
        if not _valid_kick_message(payload):
            logger.warning("[recorder] [%s] malformed kick chat message, dropping it", channel)
            return
        if state.get("streamer_id") is None:
            streamer_id, username = streamer_identity(payload, state["slug"])
            if streamer_id is not None:
                state["streamer_id"] = streamer_id
                state["streamer_username"] = username
        if state.get("third_party") is None and state.get("streamer_id") is not None:
            # One fetch per recording: later messages split its words.
            state["third_party"] = await fetch_channel_emotes(None, "kick", str(state["streamer_id"]))
        third_party = state.get("third_party") or {}
        comment = build_comment(payload, state.get("streamer_id"), state["video_id"], state["start"], third_party)
        state["writer"].add_comment(comment)
        if third_party:
            used = state.setdefault("tp_used", {})
            for frag in comment["message"]["fragments"]:
                if not isinstance(frag, dict):
                    continue
                emo = frag.get("emoticon")
                pid = emo.get("emoticon_id") if isinstance(emo, dict) else None
                if not isinstance(pid, str) or ":" not in pid or pid in used:
                    continue
                ref = third_party.get(frag.get("text", ""))
                if ref is not None:
                    used[pid] = (frag["text"], ref[1])
        skipped = collect_emote_names(state["emote_names"], payload.get("content") or "")
        if skipped:
            if not state.get("emote_skipped"):
                logger.warning(
                    "[recorder] [%s] kick chat reached the emote limit (%d ids); extra emotes stay as text tokens",
                    channel,
                    MAX_EMOTES_PER_RECORDING,
                )
            state["emote_skipped"] = state.get("emote_skipped", 0) + skipped

    def _kick_chat_trailer(self, entry: Recording, state: KickChatState) -> dict[str, Any]:
        """Build the ChatRoot trailer with the capture age at call time."""
        duration_s = time.monotonic() - entry.get("started_at", time.monotonic())
        return chat_root_trailer(
            state["slug"],
            state.get("title"),
            state["started_wall"],
            state["start"],
            duration_s,
            state.get("streamer_id"),
            state.get("streamer_username") or state["slug"],
        )

    async def _finalize_kick_chat(self, entry: Recording) -> None:
        """Write the kick chat trailer, then rename the file into place.

        The method skips entries without messages. The output file is
        TwitchDownloader ChatRoot JSON with embedded emote images (see
        emotes.embed_images, fed with Kick and 7TV ids). The state stays in the entry until the
        trailer is written, so a message that arrives during the emote fetch
        still lands in the file, and the trailer length then covers it. The
        finalizing flag blocks a second run.

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
            try:
                items: dict[str, tuple[str, str]] = {
                    eid: (name, EMOTE_URL.format(id=eid)) for eid, name in (state.get("emote_names") or {}).items()
                }
                for pid, (word, url) in (state.get("tp_used") or {}).items():
                    items.setdefault(pid, (word, url))
                embedded = await embed_images(None, items)
            except asyncio.CancelledError:
                # Write the trailer now, without the emote images. The
                # comments must not stay in an open, unusable tmp file.
                writer.close(self._kick_chat_trailer(entry, state))
                raise
            except Exception as e:
                logger.warning("[recorder] kick chat emote fetch failed: %s", e)
                embedded = None
            # The duration is read after the fetch: the state stays in the
            # entry for the whole await, so a message that lands there pushes
            # the last comment past a duration frozen before it.
            trailer = self._kick_chat_trailer(entry, state)
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
