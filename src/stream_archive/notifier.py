import asyncio
import logging
import warnings
from datetime import timedelta
from typing import Any

from telegram import Bot
from telegram.error import NetworkError, RetryAfter, TimedOut

from stream_archive.config import AppConfig
from stream_archive.recorder.common import sanitize_metadata_text, strip_line_breaks

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: AppConfig):
        self._config = config
        self.bot = Bot(token=config.bot_telegram_api)
        #: Total send attempts, the first one included.
        self._max_attempts = 3
        self._retry_delay = 2
        #: Bound the flood-control path, so a permanently limited bot cannot
        #: block the caller forever. The counter stops a loop of short waits,
        #: and the budget stops a hostile or broken RetryAfter value.
        self._max_flood_waits = 3
        self._max_flood_wait_seconds = 300.0

    @property
    def chat_id(self) -> int:
        """The alert target. It follows the live config, so a reload moves it."""
        return self._config.telegram_user_id

    async def notify(self, message: str) -> None:
        attempt = 0
        flood_waits = 0
        flood_seconds = 0.0
        while True:
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=message)
            except RetryAfter as e:
                # Flood control: wait as told, then retry without
                # counting the attempt against the budget.
                with warnings.catch_warnings():
                    # PTB returns a timedelta with PTB_TIMEDELTA=1 (the
                    # Dockerfile sets it) and a number otherwise, where the
                    # read itself warns. Both forms are handled below, so
                    # mute the warning of the number form.
                    warnings.simplefilter("ignore")
                    retry_after = e.retry_after
                if isinstance(retry_after, timedelta):
                    delay = retry_after.total_seconds()
                elif retry_after is not None:
                    delay = float(retry_after)
                else:
                    delay = float(self._retry_delay)
                # Check the budget before the counters grow. The message then
                # reports the waits that really happened.
                if flood_waits >= self._max_flood_waits or flood_seconds + delay > self._max_flood_wait_seconds:
                    msg = f"telegram send still flood-controlled after {flood_waits} waits ({flood_seconds:.0f}s)"
                    logger.error("[notifier] %s", msg)
                    raise RuntimeError(msg) from e
                flood_waits += 1
                flood_seconds += delay
                logger.warning("[notifier] Telegram flood control, waiting %ss...", delay)
                await asyncio.sleep(delay)
                continue
            except (TimedOut, NetworkError) as e:
                attempt += 1
                if attempt >= self._max_attempts:
                    msg = f"telegram send failed after {self._max_attempts} attempts"
                    logger.error("[notifier] %s: %s", msg, e)
                    raise RuntimeError(msg) from e
                logger.warning(
                    "[notifier] Telegram send failed (attempt %d/%d), retrying in %ds...",
                    attempt,
                    self._max_attempts,
                    self._retry_delay,
                )
                await asyncio.sleep(self._retry_delay)
            else:
                return

    async def notify_live(self, channel: str, title: str, game: str, url: str, youtube_url: str | None = None) -> None:
        # The title and the game name come from the streamer, and this message
        # is line-structured, so both are canonicalized to one line each.
        title = sanitize_metadata_text(title)
        game = sanitize_metadata_text(game)
        text = f"🔴 LIVE: {channel}\nTitle: {title}\nGame: {game}\nUrl: {url}"
        if youtube_url:
            text += f"\nYouTube: {youtube_url}"
        await self.notify(text)

    async def notify_offline(
        self, channel: str, file_info: dict[str, Any] | None = None, youtube_url: str | None = None
    ) -> None:
        parts = [f"⚫ Offline: {channel}"]
        if file_info:
            name = file_info.get("name")
            size_mb = file_info.get("size_mb")
            date = file_info.get("date")
            if name is not None:
                # The name points at a file on disk, so only the line-breaking
                # characters are replaced: collapsing or capping it would name
                # a file that does not exist.
                parts.append(f"File: {strip_line_breaks(name)}")
            if size_mb is not None:
                parts.append(f"Size: {size_mb} MB")
            if date is not None:
                parts.append(f"Date: {date}")
        if youtube_url:
            parts.append(f"YouTube: {youtube_url}")
        await self.notify("\n".join(parts))

    async def notify_startup(self, channels: list[str], version: str) -> None:
        text = f"▶️ StreamArchive started\nMonitoring: {', '.join(channels)}\nVersion: {version}"
        await self.notify(text)

    async def notify_shutdown(self) -> None:
        await self.notify("⏹ StreamArchive stopping")

    async def close(self) -> None:
        await self.bot.shutdown()
