"""Typed state for the recorder.

Recording and KickChatState replace the bare dict[str, Any] entries the
Recorder used to keep. Recording uses total=False because its entries grow
key by key during start, so readers must use .get() for keys set later.
KickChatState uses total=False for its finalize flag, which is set after
the first write. HoldState is built complete in one place, so every one of
its keys is required.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict

from stream_archive.chat_writer import ChatJsonWriter

if TYPE_CHECKING:
    from stream_archive.chat_recorder import ChatRecorder


class KickChatState(TypedDict, total=False):
    """Streaming Kick chat state for one active recording.

    Comments land in the file as webhook events arrive, so the state holds the
    writer, the metadata for the trailer, and the emote ids seen so far. The
    state stays in the entry until the trailer is written, so a message that
    arrives during the emote fetch still lands in the file. The finalizing
    flag marks that finalize run and blocks a second one.
    """

    finalizing: bool
    path: str
    writer: ChatJsonWriter
    title: str | None
    channel: str
    slug: str
    started_wall: str
    start: datetime | None
    video_id: str
    streamer_id: int | None
    streamer_username: str
    emote_names: dict[str, str]
    emote_skipped: int
    #: Third-party emote set of the channel: name to (id, image URL).
    #: None means not loaded yet; {} means none.
    third_party: dict[str, tuple[str, str]] | None
    #: Third-party emotes used by messages: id to (word, image URL).
    tp_used: dict[str, tuple[str, str]]


class Recording(TypedDict, total=False):
    """One active recording. Keys are set in Recorder._start_unlocked."""

    mode: str
    quality: str
    title: str | None
    game: str | None
    user_id: str | None
    filepath: str | None
    youtube_info: dict[str, Any] | None
    started_at: float
    tasks: list[asyncio.Task[Any]]
    # streamlink ships no type stubs, so the stream objects that feed these
    # handles stay Any. The handles themselves have known types.
    chat_recorder: ChatRecorder | None  # Twitch IRC recorder tied to the same capture
    chat_task: asyncio.Task[Any] | None
    kick_chat: KickChatState | None
    watchdog: asyncio.Task[Any] | None
    failed: bool
    reused: bool


class HoldState(TypedDict):
    """A YouTube broadcast kept open after the source stopped, awaiting reuse."""

    youtube_info: dict[str, Any]
    end_task: asyncio.Task[Any] | None
    keepalive: asyncio.subprocess.Process | None
