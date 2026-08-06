import asyncio

from src.twitch_recorder import notifier
from src.twitch_recorder.notifier import Notifier


class FakeBot:
    def __init__(self, token=None, fail_times=0):
        self.token = token
        self.calls = []
        self.fail_times = fail_times
        self.attempts = 0

    async def send_message(self, chat_id, text):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise Exception("boom")
        self.calls.append((chat_id, text))

    async def shutdown(self):
        pass


def make_notifier(fail_times=0):
    n = Notifier("token", 123)
    n.bot = FakeBot(fail_times=fail_times)
    n._retry_delay = 0
    return n


def test_notify_sends_once_on_success(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier()
    asyncio.run(n.notify("hello"))
    assert n.bot.calls == [(123, "hello")]


def test_notify_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier(fail_times=2)
    asyncio.run(n.notify("hello"))
    assert n.bot.attempts == 3
    assert n.bot.calls == [(123, "hello")]


def test_notify_live_contains_details(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier()
    asyncio.run(n.notify_live("ch", "Title", "Game", "https://twitch.tv/ch"))
    text = n.bot.calls[0][1]
    assert "ch" in text
    assert "Title" in text
    assert "Game" in text
    assert "https://twitch.tv/ch" in text
    assert "YouTube:" not in text


def test_notify_live_youtube_url_only_when_passed(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier()
    asyncio.run(n.notify_live("ch", "Title", "Game", "https://twitch.tv/ch", youtube_url="https://youtu.be/x"))
    text = n.bot.calls[0][1]
    assert "YouTube: https://youtu.be/x" in text


def test_notify_offline_with_file_info(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier()
    file_info = {"name": "rec.ts", "size_mb": 1.5, "date": "01-01-2026 12:00"}
    asyncio.run(n.notify_offline("ch", file_info=file_info))
    text = n.bot.calls[0][1]
    assert "⚫ Offline: ch" in text
    assert "File: rec.ts" in text
    assert "Size: 1.5 MB" in text
    assert "Date: 01-01-2026 12:00" in text


def test_notify_offline_without_file_info(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier()
    asyncio.run(n.notify_offline("ch"))
    text = n.bot.calls[0][1]
    assert text == "⚫ Offline: ch"
