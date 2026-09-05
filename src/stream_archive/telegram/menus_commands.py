"""Telegram /command entries: the admin-visible command list and handlers.

Each handler resolves its chat, routes to one ``handle_*`` method, and (for
menus) resets that chat to the root menu. Command names match
``command_list`` exactly; nothing here renames a command.
"""

from typing import Any

from telegram import BotCommand


class CommandsMixin:
    """The 17 /command entries of the controller."""

    _chat_of: Any
    _show_root: Any
    _maybe_send_apply_warnings: Any
    handle_help: Any
    handle_status: Any
    handle_channels: Any
    handle_add: Any
    handle_remove: Any
    handle_retention: Any
    handle_mode: Any
    handle_reload: Any
    handle_restart: Any
    handle_update: Any
    handle_quality: Any
    handle_maxrecordings: Any
    handle_maxyoutube: Any
    handle_disk: Any
    handle_chat: Any
    menu_text: Any
    reply_keyboard: Any

    def command_list(self) -> list[BotCommand]:
        """BotCommand entries for the Telegram /-menu that only the admin sees."""
        return [
            BotCommand("start", "Show available commands and open settings"),
            BotCommand("help", "Show available commands"),
            BotCommand("status", "Show current status and settings"),
            BotCommand("channels", "List monitored channels"),
            BotCommand("add", "Start monitoring a channel (twitch:name or kick:name)"),
            BotCommand("remove", "Stop monitoring a channel"),
            BotCommand("retention", "Set recording retention in days"),
            BotCommand("mode", "Set output mode (disk, youtube, both)"),
            BotCommand("reload", "Re-read config.json from disk"),
            BotCommand("restart", "Restart the service"),
            BotCommand("update", "Check for and apply updates"),
            BotCommand("quality", "Show or set quality (global or per-channel)"),
            BotCommand("maxrecordings", "Set concurrent recording limit"),
            BotCommand("maxyoutube", "Set YouTube re-stream limit"),
            BotCommand("disk", "Show or set disk limits"),
            BotCommand("chat", "Toggle live chat recording"),
            BotCommand("settings", "Open the settings menu (reply keyboard buttons)"),
        ]

    async def _cmd_help(self, update: Any, context: Any) -> None:
        self._show_root(self._chat_of(update))
        await update.effective_message.reply_text(self.handle_help(), reply_markup=self.reply_keyboard("root"))

    async def _cmd_status(self, update: Any, context: Any) -> None:
        self._show_root(self._chat_of(update))
        await update.effective_message.reply_text(await self.handle_status(), reply_markup=self.reply_keyboard("root"))

    async def _cmd_settings(self, update: Any, context: Any) -> None:
        self._show_root(self._chat_of(update))
        await update.effective_message.reply_text(
            await self.menu_text("root"), reply_markup=self.reply_keyboard("root")
        )

    async def _cmd_start(self, update: Any, context: Any) -> None:
        self._show_root(self._chat_of(update))
        await update.effective_message.reply_text(self.handle_help(), reply_markup=self.reply_keyboard("root"))

    async def _cmd_channels(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_channels())

    async def _cmd_add(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(
            await self.handle_add(context.args or [], chat_id=self._chat_of(update))
        )

    async def _cmd_remove(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(
            await self.handle_remove(context.args or [], chat_id=self._chat_of(update))
        )

    async def _cmd_retention(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(
            self.handle_retention(context.args or [], chat_id=self._chat_of(update))
        )

    async def _cmd_mode(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_mode(context.args or [], chat_id=self._chat_of(update)))
        await self._maybe_send_apply_warnings()

    async def _cmd_reload(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(await self.handle_reload())

    async def _cmd_restart(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_restart())

    async def _cmd_update(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(await self.handle_update())

    async def _cmd_quality(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(
            self.handle_quality(context.args or [], chat_id=self._chat_of(update))
        )
        await self._maybe_send_apply_warnings()

    async def _cmd_maxrecordings(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(
            self.handle_maxrecordings(context.args or [], chat_id=self._chat_of(update))
        )

    async def _cmd_maxyoutube(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(
            self.handle_maxyoutube(context.args or [], chat_id=self._chat_of(update))
        )

    async def _cmd_disk(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(self.handle_disk(context.args or [], chat_id=self._chat_of(update)))

    async def _cmd_chat(self, update: Any, context: Any) -> None:
        await update.effective_message.reply_text(
            await self.handle_chat(context.args or [], chat_id=self._chat_of(update))
        )
        await self._maybe_send_apply_warnings()
