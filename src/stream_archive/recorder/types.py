"""Typed state for the recorder.

Recording and HoldState replace the bare dict[str, Any] entries the
Recorder used to keep. Both use total=False where entries grow key by
key during start, so readers must use .get() for keys set later.
"""

import asyncio
from typing import Any, TypedDict


class KickChatState(TypedDict, total=False):
    """Buffered Kick chat for one active recording."""

    path: str
    messages: list[dict[str, Any]]
    title: str | None
    channel: str
    started_wall: str


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
    # streamlink ships no type stubs, so handles fed by its stream
    # objects stay Any.
    process: Any  # ffmpeg child fed from a streamlink stream
    chat_recorder: Any  # Twitch IRC recorder tied to the same capture
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
