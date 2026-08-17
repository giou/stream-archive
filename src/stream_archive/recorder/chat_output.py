import json
import logging
import os
import time
from typing import Any

from stream_archive.config import (
    kick_bare_name,
)
from stream_archive.kick_chat import build_chat_root, embed_kick_emotes

logger = logging.getLogger(__name__)


class ChatOutputMixin:
    _recordings: dict[str, dict[str, Any]]

    async def _finalize_chat(self, channel: str, chat_recorder: Any) -> None:
        """Fire-and-forget chat finalize for the failure path; never raises."""
        try:
            await chat_recorder.stop()
        except Exception as e:
            logger.error("[recorder] [%s] chat finalize error: %s", channel, e)

    async def stop_chat(self, channel: str, platform: str | None = None) -> None:
        """Stop and finalize chat capture for an active recording; the video continues.

        platform=None stops both; "twitch" only the IRC recorder; "kick" only kick chat.
        """
        entry = self._recordings.get(channel)
        if entry is None:
            return
        if platform in (None, "twitch"):
            chat_recorder = entry.pop("chat_recorder", None)
            if chat_recorder:
                try:
                    await chat_recorder.stop()
                except Exception as e:
                    logger.error("[recorder] [%s] chat finalize error: %s", channel, e)
        if platform in (None, "kick"):
            await self._finalize_kick_chat(entry)

    async def add_kick_chat(self, channel: str, payload: dict[str, Any]) -> None:
        """Append one normalized kick chat message to the active recording's buffer.

        No-op when the channel is not being recorded (webhook delivery is
        best-effort and there is no replay).
        """
        entry = self._recordings.get(channel)
        if entry is not None and entry.get("kick_chat") is not None:
            entry["kick_chat"]["messages"].append(payload)

    async def _finalize_kick_chat(self, entry: dict[str, Any]) -> None:
        """Write the collected kick chat buffer atomically; skip when empty.

        Output is TwitchDownloader ChatRoot JSON with embedded emote images
        (see kick_chat.build_chat_root / embed_kick_emotes).
        """
        kick_chat = entry.pop("kick_chat", None)
        if kick_chat is None or not kick_chat.get("messages"):
            return
        try:
            duration_s = time.monotonic() - entry.get("started_at", time.monotonic())
            root = build_chat_root(
                kick_chat["channel"],
                kick_bare_name(kick_chat["channel"]),
                kick_chat.get("title"),
                kick_chat.get("started_wall"),
                kick_chat["messages"],
                duration_s=duration_s,
            )
            await embed_kick_emotes(root)
            tmp = kick_chat["path"] + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(root, f, ensure_ascii=False, indent=2)
            os.replace(tmp, kick_chat["path"])
            logger.info(
                "[recorder] kick chat saved: %s (%d messages)",
                kick_chat["path"],
                len(kick_chat["messages"]),
            )
        except Exception as e:
            logger.error("[recorder] kick chat finalize failed: %s", e)
