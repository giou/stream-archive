import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter, TimedOut

from stream_archive import notifier
from stream_archive.notifier import Notifier


class _RetryAfterSeconds(RetryAfter):
    """Report the wait as whole seconds, whatever the environment says.

    PTB reports either a number or a timedelta, and the form depends on the
    PTB_TIMEDELTA variable. Each test pins one form.
    """

    def __init__(self, seconds):
        self._seconds = seconds
        super().__init__(seconds)

    @property
    def retry_after(self):
        return self._seconds


class _RetryAfterTimedelta(RetryAfter):
    """Report the wait as a timedelta, the form PTB_TIMEDELTA=1 gives."""

    def __init__(self, seconds):
        self._period = timedelta(seconds=seconds)
        super().__init__(self._period)

    @property
    def retry_after(self):
        return self._period


class _RetryAfterWithoutValue(RetryAfter):
    """Report no wait at all, which the notifier must replace with its own delay."""

    def __init__(self):
        super().__init__(1)

    @property
    def retry_after(self):
        return None


def timed_out():
    return TimedOut("boom")


class FakeBot:
    """A bot that fails the first ``fail_times`` sends with ``fail_error()``."""

    def __init__(self, token=None, fail_times=0, fail_error=None):
        self.token = token
        self.calls = []
        self.fail_times = fail_times
        self.fail_error = fail_error or timed_out
        self.attempts = 0
        self.shutdown_calls = 0

    async def send_message(self, chat_id, text):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.fail_error()
        self.calls.append((chat_id, text))

    async def shutdown(self):
        self.shutdown_calls += 1


def make_notifier(fail_times=0, fail_error=None, max_attempts=3, retry_delay=0):
    n = Notifier("token", 123)
    n.bot = FakeBot(fail_times=fail_times, fail_error=fail_error)
    # Pin both retry knobs, so the tests do not depend on the defaults of
    # Notifier.__init__.
    n._retry_delay = retry_delay
    n._max_attempts = max_attempts
    return n


def record_sleeps(monkeypatch):
    """Patch the sleep of the notifier module alone. Return the recorded delays."""
    delays = []

    async def fake_sleep(duration):
        delays.append(duration)

    # notifier.asyncio is an attribute of the notifier module, so no other
    # coroutine in the process loses its sleep.
    monkeypatch.setattr(notifier, "asyncio", SimpleNamespace(sleep=fake_sleep))
    return delays


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


def test_notify_retry_after_does_not_count_attempt(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier(fail_times=1, fail_error=lambda: _RetryAfterSeconds(0), max_attempts=1)
    # Flood control waits and retries without counting the attempt, so the
    # caller still sends the message although the attempt budget is 1.
    asyncio.run(n.notify("hello"))
    assert n.bot.attempts == 2
    assert n.bot.calls == [(123, "hello")]


def test_notify_raises_after_final_failure(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier(fail_times=10, max_attempts=3)
    with pytest.raises(RuntimeError, match="telegram send failed after 3 attempts"):
        asyncio.run(n.notify("hello"))
    assert n.bot.attempts == 3
    assert n.bot.calls == []


def test_notify_sleeps_the_retry_delay_between_attempts(monkeypatch):
    """Every ordinary retry waits the configured delay, in seconds."""
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier(fail_times=2, retry_delay=0.5)
    delays = record_sleeps(monkeypatch)

    asyncio.run(n.notify("hello"))

    assert delays == [0.5, 0.5]
    assert n.bot.calls == [(123, "hello")]


def test_notify_bounds_flood_waits(monkeypatch):
    """A chat that always answers flood control must not loop for ever."""
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier(fail_times=10, fail_error=lambda: _RetryAfterSeconds(0))
    delays = record_sleeps(monkeypatch)

    # The message reports the waits and the seconds that really elapsed.
    with pytest.raises(RuntimeError, match=r"still flood-controlled after 3 waits \(0s\)"):
        asyncio.run(n.notify("hello"))

    # Three waits are allowed. The fourth request raises.
    assert n.bot.attempts == 4
    assert delays == [0.0, 0.0, 0.0]
    assert n.bot.calls == []


def test_notify_rejects_a_flood_wait_over_the_budget(monkeypatch):
    """One wait longer than the budget stops the loop instead of sleeping."""
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier(fail_times=10, fail_error=lambda: _RetryAfterSeconds(301))
    delays = record_sleeps(monkeypatch)

    with pytest.raises(RuntimeError, match="still flood-controlled"):
        asyncio.run(n.notify("hello"))

    assert delays == []
    assert n.bot.attempts == 1


def test_notify_flood_wait_without_a_value_uses_the_retry_delay(monkeypatch):
    """A RetryAfter without a period falls back to the retry delay."""
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier(fail_times=2, fail_error=_RetryAfterWithoutValue, retry_delay=0.5)
    delays = record_sleeps(monkeypatch)

    asyncio.run(n.notify("hello"))

    assert delays == [0.5, 0.5]
    assert n.bot.calls == [(123, "hello")]


@pytest.mark.parametrize(
    ("make_error", "expected_delay"),
    [
        (lambda: _RetryAfterSeconds(12), 12.0),
        (lambda: _RetryAfterTimedelta(7), 7.0),
    ],
)
def test_notify_flood_wait_converts_the_reported_period(monkeypatch, make_error, expected_delay):
    """Both forms of the reported period become a wait in seconds."""
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier(fail_times=1, fail_error=make_error)
    delays = record_sleeps(monkeypatch)

    asyncio.run(n.notify("hello"))

    assert delays == [expected_delay]
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


def test_notify_startup_contains_channels_and_version(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier()
    asyncio.run(n.notify_startup(["ch1", "ch2"], "v1.0.0 (abc1234)"))
    text = n.bot.calls[0][1]
    assert "▶️ StreamArchive started" in text
    assert "Monitoring: ch1, ch2" in text
    assert "Version: v1.0.0 (abc1234)" in text


def test_notify_shutdown_sends_message(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier()
    asyncio.run(n.notify_shutdown())
    assert n.bot.calls == [(123, "⏹ StreamArchive stopping")]


def test_close_shuts_down_the_bot(monkeypatch):
    monkeypatch.setattr(notifier, "Bot", FakeBot)
    n = make_notifier()
    asyncio.run(n.close())
    assert n.bot.shutdown_calls == 1
