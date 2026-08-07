import asyncio
import logging
from telegram import Bot

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot_token, chat_id):
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self._max_retries = 3
        self._retry_delay = 2

    async def notify(self, message):
        for attempt in range(1, self._max_retries + 1):
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=message)
                return
            except Exception as e:
                if attempt == self._max_retries:
                    logger.error("[notifier] Error sending Telegram message after %d retries: %s", self._max_retries, e)
                    return
                logger.warning("[notifier] Telegram send failed (attempt %d/%d), retrying in %ds...", attempt, self._max_retries, self._retry_delay)
                await asyncio.sleep(self._retry_delay)

    async def notify_live(self, channel, title, game, url, youtube_url=None):
        text = (
            f"🔴 LIVE: {channel}\n"
            f"Title: {title}\n"
            f"Game: {game}\n"
            f"Url: {url}"
        )
        if youtube_url:
            text += f"\nYouTube: {youtube_url}"
        await self.notify(text)

    async def notify_offline(self, channel, file_info=None, youtube_url=None):
        parts = [f"⚫ Offline: {channel}"]
        if file_info:
            parts.append(f"File: {file_info['name']}")
            parts.append(f"Size: {file_info['size_mb']} MB")
            parts.append(f"Date: {file_info['date']}")
        await self.notify("\n".join(parts))

    async def close(self):
        await self.bot.shutdown()
