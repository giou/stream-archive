import asyncio
import logging
import warnings
from datetime import timedelta
from typing import Any

from telegram import Bot
from telegram.error import NetworkError, RetryAfter, TimedOut

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot_token: str, chat_id: int):
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self._max_retries = 3
        self._retry_delay = 2

    async def notify(self, message: str) -> None:
        attempt = 0
        while True:
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=message)
            except RetryAfter as e:
                # Flood control: wait as told, then retry without
                # counting the attempt against the budget.
                with warnings.catch_warnings():
                    # PTB 22 deprecates the int form of retry_after; the
                    # value is still correct, so mute the warning here.
                    warnings.simplefilter("ignore")
                    retry_after = e.retry_after
                if isinstance(retry_after, timedelta):
                    delay = retry_after.total_seconds()
                elif retry_after is not None:
                    delay = float(retry_after)
                else:
                    delay = float(self._retry_delay)
                logger.warning("[notifier] Telegram flood control, waiting %ss...", delay)
                await asyncio.sleep(delay)
                continue
            except (TimedOut, NetworkError) as e:
                attempt += 1
                if attempt >= self._max_retries:
                    msg = f"telegram send failed after {self._max_retries} retries"
                    logger.error("[notifier] Error sending Telegram message after %d retries: %s", self._max_retries, e)
                    raise RuntimeError(msg) from e
                logger.warning(
                    "[notifier] Telegram send failed (attempt %d/%d), retrying in %ds...",
                    attempt,
                    self._max_retries,
                    self._retry_delay,
                )
                await asyncio.sleep(self._retry_delay)
            else:
                return

    async def notify_live(self, channel: str, title: str, game: str, url: str, youtube_url: str | None = None) -> None:
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
                parts.append(f"File: {name}")
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
