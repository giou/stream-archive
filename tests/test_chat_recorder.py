import asyncio
import contextlib
import json
from datetime import datetime, timezone

from stream_archive.chat_recorder import ChatRecorder

TS_MS = 1720000000000  # 2024-07-03T09:46:40Z
TS2_MS = 1720000000500

PRIVMSG_TEMPLATE = (
    "@badges=subscriber/12;color=#FF0000;display-name=ViewerName;emotes=25:6-10;"
    "id={msg_id};room-id=123456;tmi-sent-ts={ts};user-id=987654 "
    ":viewername!viewername@viewername.tmi.twitch.tv PRIVMSG #ch :{body}"
)


class FakeIRCServer:
    """Scripted IRC server. spec per accepted connection: (lines, hold_open, expect_reply).

    lines may contain floats: sleep that many seconds before continuing.
    """

    def __init__(self, specs):
        self.specs = list(specs)
        self.received = []
        self.accepted = 0
        self.handler_tasks = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        for t in self.handler_tasks:
            t.cancel()
        await asyncio.gather(*self.handler_tasks, return_exceptions=True)
        await self.server.wait_closed()

    async def _handler(self, reader, writer):
        self.handler_tasks.append(asyncio.current_task())
        idx = self.accepted
        self.accepted += 1
        lines, hold, expect_reply = self.specs[min(idx, len(self.specs) - 1)]
        try:
            while True:  # registration until JOIN
                line = await reader.readline()
                if not line:
                    return
                self.received.append(line.decode(errors="replace").strip())
                if b"JOIN" in line:
                    break
            for item in lines:
                if isinstance(item, float):
                    await asyncio.sleep(item)
                    continue
                writer.write(item.encode() + b"\r\n")
                await writer.drain()
                await asyncio.sleep(0.01)
            if expect_reply:
                try:
                    reply = await asyncio.wait_for(reader.readline(), timeout=2)
                    if reply:
                        self.received.append(reply.decode(errors="replace").strip())
                except asyncio.TimeoutError:
                    pass
            if hold:
                await asyncio.sleep(300)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def make_recorder(tmp_path, server, channel="ch", **kwargs):
    return ChatRecorder(
        channel,
        str(tmp_path / "chat.json"),
        "Title",
        "Game",
        host="127.0.0.1",
        port=server.port,
        use_ssl=False,
        **kwargs,
    )


def read_chat(tmp_path):
    with open(tmp_path / "chat.json") as f:
        return json.load(f)


async def wait_for_comments(cr, n, timeout=5.0):
    async def _poll():
        while len(cr._comments) < n:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


def test_privmsg_with_emotes_badges_color(tmp_path):
    line = PRIVMSG_TEMPLATE.format(msg_id="abc-123", ts=TS_MS, body="Hello Kappa there")

    async def scenario():
        async with FakeIRCServer([([line], False, False)]) as server:
            cr = make_recorder(tmp_path, server)
            cr.start()
            await wait_for_comments(cr, 1)
            await cr.stop()

        data = read_chat(tmp_path)
        assert len(data["comments"]) == 1
        comment = data["comments"][0]
        expected = {
            "_id": "abc-123",
            "created_at": datetime.fromtimestamp(TS_MS / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel_id": "123456",
            "content_type": "video",
            "content_id": "ch",
            "commenter": {
                "display_name": "ViewerName",
                "_id": "987654",
                "name": "viewername",
            },
            "message": {
                "body": "Hello Kappa there",
                "bits_spent": 0,
                "fragments": [
                    {"text": "Hello "},
                    {"text": "Kappa", "emoticon": {"emoticon_id": "25"}},
                    {"text": " there"},
                ],
                "user_badges": [{"_id": "subscriber", "version": "12"}],
                "user_color": "#FF0000",
                "emoticons": [{"_id": "25", "begin": 6, "end": 12}],
            },
        }
        offset = comment.pop("content_offset_seconds")
        assert comment == expected
        assert isinstance(offset, float) and offset >= 0
        assert round(offset, 3) == offset
        # render duration: TDL chatrender uses video.end - video.start
        assert data["video"]["end"] == offset
        assert data["video"]["length"] == offset
        # sentinel "0" (not ""/null): GUI chat-update preview takes the VOD
        # branch and degrades gracefully instead of FormatException/NRE
        assert data["video"]["id"] == "0"

    asyncio.run(scenario())


def test_ping_gets_pong(tmp_path):
    async def scenario():
        async with FakeIRCServer([(["PING :tmi.twitch.tv"], False, True)]) as server:
            cr = make_recorder(tmp_path, server)
            cr.start()
            await asyncio.sleep(0.2)
            await cr.stop()
        assert "PONG :tmi.twitch.tv" in server.received

    asyncio.run(scenario())


def test_offsets_monotonic(tmp_path):
    line1 = PRIVMSG_TEMPLATE.format(msg_id="m1", ts=TS_MS, body="first")
    line2 = PRIVMSG_TEMPLATE.format(msg_id="m2", ts=TS2_MS, body="second")

    async def scenario():
        async with FakeIRCServer([([line1, 0.06, line2], False, False)]) as server:
            cr = make_recorder(tmp_path, server)
            cr.start()
            await wait_for_comments(cr, 2)
            await cr.stop()

        data = read_chat(tmp_path)
        o1 = data["comments"][0]["content_offset_seconds"]
        o2 = data["comments"][1]["content_offset_seconds"]
        assert o1 >= 0 and o2 >= 0
        assert o2 > o1
        assert round(o1, 3) == o1 and round(o2, 3) == o2

    asyncio.run(scenario())


def test_user_notice_records_system_message(tmp_path):
    line = (
        "@badges=subscriber/12;color=;display-name=ViewerName;id=not-1;msg-id=resub;"
        f"room-id=123456;tmi-sent-ts={TS_MS};user-id=987654 "
        ":viewername!viewername@viewername.tmi.twitch.tv USERNOTICE #ch :"
        "viewername subscribed at Tier 1\\s- 6 months"
    )

    async def scenario():
        async with FakeIRCServer([([line], False, False)]) as server:
            cr = make_recorder(tmp_path, server)
            cr.start()
            await wait_for_comments(cr, 1)
            await cr.stop()

        data = read_chat(tmp_path)
        comment = data["comments"][0]
        assert comment["message"]["user_notice_params"] == {"msg_id": "resub"}
        assert comment["message"]["body"] == "viewername subscribed at Tier 1 - 6 months"

    asyncio.run(scenario())


def test_reconnects_after_disconnect(tmp_path):
    line1 = PRIVMSG_TEMPLATE.format(msg_id="m1", ts=TS_MS, body="first")
    line2 = PRIVMSG_TEMPLATE.format(msg_id="m2", ts=TS2_MS, body="second")

    async def scenario():
        # first connection closes after one message; second holds open
        async with FakeIRCServer(
            [
                ([line1], False, False),
                ([line2], True, False),
            ]
        ) as server:
            cr = make_recorder(tmp_path, server)
            cr.start()
            await wait_for_comments(cr, 2)
            await cr.stop()

        data = read_chat(tmp_path)
        assert [c["_id"] for c in data["comments"]] == ["m1", "m2"]
        assert cr._connected_once

    asyncio.run(scenario())


def test_empty_chat_writes_valid_json(tmp_path):
    async def scenario():
        async with FakeIRCServer([([], False, False)]) as server:
            cr = make_recorder(tmp_path, server)
            cr.start()
            await asyncio.sleep(0.2)
            await cr.stop()

        data = read_chat(tmp_path)
        assert data["comments"] == []
        assert data["FileInfo"]["Version"] == {"Major": 1, "Minor": 4, "Patch": 0}
        assert data["streamer"]["login"] == "ch"
        assert data["video"]["title"] == "Title"
        # no comments: end/length fall back to the capture wall-clock duration
        assert data["video"]["end"] > 0
        assert data["video"]["length"] == data["video"]["end"]

    asyncio.run(scenario())


def test_stop_is_idempotent_no_corruption(tmp_path):
    line1 = PRIVMSG_TEMPLATE.format(msg_id="m1", ts=TS_MS, body="first")
    line2 = PRIVMSG_TEMPLATE.format(msg_id="m2", ts=TS2_MS, body="second")

    async def scenario():
        async with FakeIRCServer([([line1, line2], False, False)]) as server:
            cr = make_recorder(tmp_path, server)
            cr.start()
            await wait_for_comments(cr, 2)
            await cr.stop()
            await cr.stop()

        data = read_chat(tmp_path)
        assert [c["_id"] for c in data["comments"]] == ["m1", "m2"]
        assert list(tmp_path.glob("*.tmp")) == []

    asyncio.run(scenario())


def test_cancel_mid_stream_writes_complete_json(tmp_path):
    line1 = PRIVMSG_TEMPLATE.format(msg_id="m1", ts=TS_MS, body="first")
    line2 = PRIVMSG_TEMPLATE.format(msg_id="m2", ts=TS2_MS, body="second")

    async def scenario():
        # server holds the connection open mid-stream
        async with FakeIRCServer([([line1, line2], True, False)]) as server:
            cr = make_recorder(tmp_path, server)
            cr.start()
            await wait_for_comments(cr, 2)
            await asyncio.sleep(0.05)  # still streaming when stop hits
            await cr.stop()

        text = (tmp_path / "chat.json").read_text()
        # whole file parses => the comments array is closed and complete
        data = json.loads(text)
        assert [c["_id"] for c in data["comments"]] == ["m1", "m2"]

    asyncio.run(scenario())


def test_failure_cleanup_racing_stop_writes_once(tmp_path):
    line1 = PRIVMSG_TEMPLATE.format(msg_id="m1", ts=TS_MS, body="first")

    async def scenario():
        async with FakeIRCServer([([line1], True, False)]) as server:
            cr = make_recorder(tmp_path, server)
            cr.start()
            await wait_for_comments(cr, 1)
            await asyncio.gather(cr.stop(), cr.stop())

        data = read_chat(tmp_path)
        assert [c["_id"] for c in data["comments"]] == ["m1"]
        assert list(tmp_path.glob("*.tmp")) == []

    asyncio.run(scenario())
