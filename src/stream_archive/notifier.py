import asyncio
import logging
from typing import Any

from telegram import Bot

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot_token: str, chat_id: int):
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self._max_retries = 3
        self._retry_delay = 2

    async def notify(self, message: str) -> None:
        for attempt in range(1, self._max_retries + 1):
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=message)
                return
            except Exception as e:
                if attempt == self._max_retries:
                    logger.error("[notifier] Error sending Telegram message after %d retries: %s", self._max_retries, e)
                    return
                logger.warning(
                    "[notifier] Telegram send failed (attempt %d/%d), retrying in %ds...",
                    attempt,
                    self._max_retries,
                    self._retry_delay,
                )
                await asyncio.sleep(self._retry_delay)

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
            parts.append(f"File: {file_info['name']}")
            parts.append(f"Size: {file_info['size_mb']} MB")
            parts.append(f"Date: {file_info['date']}")
        await self.notify("\n".join(parts))

    async def notify_startup(self, channels: list[str], version: str) -> None:
        text = f"▶️ StreamArchive started\nMonitoring: {', '.join(channels)}\nVersion: {version}"
        await self.notify(text)

    async def notify_shutdown(self) -> None:
        await self.notify("⏹ StreamArchive stopping")

    async def close(self) -> None:
        await self.bot.shutdown()
