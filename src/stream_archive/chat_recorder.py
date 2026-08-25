"""Twitch IRC live-chat capture, written as TwitchDownloader-compatible ChatRoot JSON.

The recorder logs in anonymously (`justinfan`) over TLS and needs no external
packages. This module only saves the JSON file. Rendering happens externally
with TwitchDownloaderCLI.
"""

import asyncio
import json
import logging
import os
import random
import ssl
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_TAG_ESCAPES = {"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}


def _unescape_tag(value: str) -> str:
    """Unescape an IRCv3 tag value (\\s \\: \\\\ \\r \\n).

    An unknown escape keeps its character.
    """
    out = []
    i = 0
    n = len(value)
    while i < n:
        c = value[i]
        if c == "\\" and i + 1 < n:
            out.append(_TAG_ESCAPES.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse_emotes(emotes_tag: str, body: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split body into TwitchDownloader fragments/emoticons from an `emotes` tag value.

    Tag format: `25:0-4,12-16/1902:8-15`. Character ranges are inclusive.
    The return value is ([fragments], [emoticons]). The parser drops malformed
    and overlapping ranges.
    """
    if not emotes_tag:
        return [{"text": body}], []

    ranges = []
    for group in emotes_tag.split("/"):
        emote_id, _, positions = group.partition(":")
        if not emote_id or not positions:
            continue
        for r in positions.split(","):
            begin_s, _, end_s = r.partition("-")
            try:
                begin, end = int(begin_s), int(end_s)
            except ValueError:
                continue
            ranges.append((begin, end, emote_id))
    ranges.sort(key=lambda r: (r[0], r[1]))

    fragments: list[dict[str, Any]] = []
    emoticons: list[dict[str, Any]] = []
    pos = 0
    for begin, end, emote_id in ranges:
        if begin >= len(body) or begin < pos:
            continue  # malformed or overlapping — drop
        if begin > pos:
            fragments.append({"text": body[pos:begin]})
        emote_text = body[begin : min(end + 1, len(body))]
        if not emote_text:
            continue
        # ChatRoot.Emoticon.emoticon_id is a string in TwitchDownloader's schema
        fragments.append({"text": emote_text, "emoticon": {"emoticon_id": emote_id}})
        emoticons.append({"_id": emote_id, "begin": begin, "end": begin + len(emote_text) + 1})
        pos = begin + len(emote_text)
    if pos < len(body):
        fragments.append({"text": body[pos:]})
    if not fragments:
        fragments.append({"text": body})
    return fragments, emoticons


class ChatRecorder:
    """Connects to Twitch IRC for one channel and accumulates comments until stopped.

    The app creates the chat JSON file only through an atomic same-directory
    rename, so a crash mid-write leaves at most an orphan `.tmp` file. The
    `stop()` method is idempotent and writes the file exactly once.
    """

    def __init__(
        self,
        channel: str,
        chat_path: str,
        title: str,
        game: str,
        author: str | None = None,
        user_id: str | int | None = None,
        host: str = "irc.chat.twitch.tv",
        port: int = 6697,
        use_ssl: bool = True,
    ):
        self.channel = channel.lower()
        self.chat_path = chat_path
        self._title = title
        self._game = game
        self._author = author
        self._user_id = user_id
        self._host = host
        self._port = port
        self._use_ssl = use_ssl
        self._comments: list[dict[str, Any]] = []
        self._start_mono = time.monotonic()
        self._start_z = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._task: asyncio.Task[Any] | None = None
        self._connected_once = False
        self._finalized = False

    def start(self) -> asyncio.Task[Any]:
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> int:
        """Cancel the run task, then finalize."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._finalize()
        return len(self._comments)

    async def _run(self) -> None:
        attempts = 0
        while True:
            try:
                ok = await self._connect_and_read()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[chat:%s] connection error: %s", self.channel, e)
                attempts += 1
            else:
                if ok:
                    attempts = 0
                else:
                    attempts += 1
            await asyncio.sleep(min(30, 2**attempts))

    async def _connect_and_read(self) -> bool:
        context = ssl.create_default_context() if self._use_ssl else None
        reader, writer = await asyncio.open_connection(self._host, self._port, ssl=context)
        read_any = False
        try:
            nick = "justinfan" + str(random.randint(0, 10**8 - 1)).zfill(8)
            writer.write(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
            writer.write(b"PASS oauth:anonymous\r\n")
            writer.write(f"NICK {nick}\r\n".encode())
            writer.write(f"JOIN #{self.channel}\r\n".encode())
            await writer.drain()

            while True:
                line = await reader.readline()
                if not line:
                    return read_any  # disconnected
                read_any = True
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")

                if text.startswith("PING :"):
                    writer.write(f"PONG :{text[6:]}\r\n".encode())
                    await writer.drain()
                    continue

                rest = text.split(" ", 1)[1] if text.startswith("@") else text
                header = rest.split(" :", 1)[0]
                parts = header.split()
                cmd = parts[1] if len(parts) > 1 else None
                if cmd in ("PRIVMSG", "USERNOTICE"):
                    comment = self._parse_message(text, cmd)
                    if comment is not None:
                        self._comments.append(comment)
                        self._connected_once = True
                # ignore everything else (001/353/366/NOTICE/ROOMSTATE)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _parse_message(self, text: str, kind: str) -> dict[str, Any] | None:
        """Parse one tagged PRIVMSG/USERNOTICE into a TwitchDownloader comment dict."""
        if not text.startswith("@"):
            return None
        tags_part, _, rest = text.partition(" ")
        if not rest:
            return None
        tags = {}
        for item in tags_part[1:].split(";"):
            key, _, value = item.partition("=")
            tags[key] = _unescape_tag(value)
        if "id" not in tags or "user-id" not in tags:
            return None

        header, _, body = rest.partition(" :")
        if not body:
            return None
        parts = header.split()
        if len(parts) < 3:
            return None
        prefix = parts[0]
        if not prefix.startswith(":"):
            return None
        login = prefix[1:].split("!", 1)[0]
        if kind == "USERNOTICE":
            body = _unescape_tag(body)  # Twitch escapes \s in these system messages

        try:
            ts = int(tags.get("tmi-sent-ts", 0))
        except ValueError:
            ts = 0
        if ts:
            created_at = datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            bits = int(tags.get("bits", 0))
        except ValueError:
            bits = 0

        fragments, emoticons = _parse_emotes(tags.get("emotes", ""), body)

        badges = []
        if tags.get("badges"):
            for item in tags["badges"].split(","):
                name, _, version = item.partition("/")
                badges.append({"_id": name, "version": version})

        message = {
            "body": body,
            "bits_spent": bits,
            "fragments": fragments,
            "user_badges": badges,
            "user_color": tags.get("color") or None,
            "emoticons": emoticons,
        }
        if kind == "USERNOTICE":
            message["user_notice_params"] = {"msg_id": tags.get("msg-id", "")}

        return {
            "_id": tags["id"],
            "created_at": created_at,
            "channel_id": tags.get("room-id", ""),
            "content_type": "video",
            "content_id": self.channel,
            "content_offset_seconds": round(time.monotonic() - self._start_mono, 3),
            "commenter": {
                "display_name": tags.get("display-name") or login,
                "_id": tags["user-id"],
                "name": login,
            },
            "message": message,
        }

    async def _finalize(self) -> None:
        """Write the ChatRoot JSON atomically (tmp + same-directory rename), exactly once."""
        if self._finalized:
            return
        self._finalized = True

        now_z = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            streamer_id = int(self._user_id) if self._user_id else 0
        except TypeError, ValueError:
            streamer_id = 0
        # TDL convention (ChatDownloader.cs): live chat takes length/end from
        # the last comment's offset. chatrender derives the render duration
        # from video.end - video.start, so zeros render a 0-second video.
        if self._comments:
            end = self._comments[-1]["content_offset_seconds"]
        else:
            end = round(time.monotonic() - self._start_mono, 3)
        root = {
            "FileInfo": {
                "Version": {"Major": 1, "Minor": 4, "Patch": 0},
                "CreatedAt": now_z,
                "UpdatedAt": now_z,
            },
            "streamer": {
                "name": self._author or self.channel,
                "login": self.channel,
                "id": streamer_id,
            },
            "video": {
                "title": self._title,
                "description": "",
                # Sentinel id "0". In TDL 1.56.5 PageChatUpdate, the GUI's
                # chat-update preview takes the VOD branch for a numeric id
                # and handles data.video == null gracefully.
                # "" crashes long.Parse(""). null makes the GUI fall back to
                # the comments' content_id (the channel login). A bogus clip
                # slug makes GetClipInfo return data.clip == null, which
                # raises a NullReferenceException in the GUI.
                "id": "0",
                "created_at": self._start_z,
                "start": 0.0,
                "end": end,
                "length": end,
                "viewCount": 0,
                "game": self._game,
            },
            "comments": self._comments,
        }

        os.makedirs(os.path.dirname(self.chat_path) or ".", exist_ok=True)
        tmp = self.chat_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(root, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.chat_path)
        logger.info("[chat] %s -> %s (%d messages)", self.channel, self.chat_path, len(self._comments))
