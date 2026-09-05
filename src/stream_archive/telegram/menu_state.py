"""Per-chat Telegram menu state shared by the dispatcher and menu modules.

The bot used to keep one global menu (``_menu``/``_menu_channel``/``_custom_setting``),
so two chats could never hold different menus. State now lives in small
``MenuState`` records keyed by chat id. Confirm guards use ``(chat_id, nonce)``
keys, so one chat can never confirm another chat's prompt.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup

from stream_archive.config import AppConfig

#: Chat id of the Telegram conversation a menu belongs to.
ChatId = int

#: Key for confirm guards: the chat that owns the prompt plus its nonce.
#: Plain strings are legacy keys from before per-chat state; they read as
#: the passed chat's entry.
PendingKey = tuple[ChatId, str] | str

#: Audio-only switch awaiting confirm: quality change plus affected channels.
AudioSwitch = tuple[Callable[[AppConfig], Any], list[str]]

#: What one menu press returns: reply text plus keyboard, or None to ignore.
MenuResult = tuple[str, ReplyKeyboardMarkup | InlineKeyboardMarkup] | None


@dataclass
class MenuState:
    """Reply-keyboard position of one chat."""

    menu: str = "root"
    channel: str | None = None
    custom: str | None = None
    cloudflare_hostname: str | None = None


def split_key(key: PendingKey, chat_id: ChatId) -> tuple[ChatId, str]:
    """Split a pending key into ``(chat_id, nonce)``."""
    if isinstance(key, tuple):
        return key
    return (chat_id, key)


class ChatStateMixin:
    """Per-chat menu storage for the controller.

    Owns the ``_states`` map, the confirm guards, and chat resolution.
    The ``_menu``-family properties are single-chat views of the admin
    menu for old callers; new code uses ``_state_for(chat_id)``.
    """

    _admin_id: int
    _states: dict[ChatId, MenuState]
    _confirm_done: set[tuple[ChatId, str]]
    _pending_apply: dict[PendingKey, tuple[str, list[str]]]
    _pending_audio_switch: dict[PendingKey, AudioSwitch]
    _apply_warnings_sent: set[PendingKey]

    def _init_chat_state(self) -> None:
        self._states = {}  # chat id -> reply-keyboard menu, one per chat
        self._confirm_done = set()  # (chat id, callback data) already confirmed
        self._pending_apply = {}  # (chat id, nonce) -> (summary, channels) awaiting apply-now
        self._pending_audio_switch = {}  # (chat id, nonce) -> (quality change, channels) awaiting confirm
        self._apply_warnings_sent = set()  # pending keys already messaged

    def _state_for(self, chat_id: ChatId) -> MenuState:
        """Return the menu of ``chat_id``, creating the root menu on first use."""
        state = self._states.get(chat_id)
        if state is None:
            state = MenuState()
            self._states[chat_id] = state
        return state

    def _chat_of(self, update: Any) -> ChatId:
        """Chat id of an update, defaulting to the admin chat."""
        chat = getattr(update, "effective_chat", None)
        if chat is not None and getattr(chat, "id", None) is not None:
            cid: int = chat.id
            return cid
        return self._admin_id

    def _callback_chat_of(self, update: Any) -> ChatId:
        """Chat id of a callback query, defaulting to the admin chat."""
        query = update.callback_query
        message = getattr(query, "message", None)
        chat = getattr(message, "chat", None) if message is not None else None
        if chat is not None and getattr(chat, "id", None) is not None:
            cid: int = chat.id
            return cid
        user = getattr(query, "from_user", None) or getattr(update, "effective_user", None)
        if user is not None and getattr(user, "id", None) is not None:
            uid: int = user.id
            return uid
        return self._admin_id

    def _show_root(self, chat_id: ChatId) -> MenuState:
        """Reset one chat to the root menu."""
        state = self._state_for(chat_id)
        state.menu, state.channel = "root", None
        return state

    @property
    def _menu(self) -> str:
        return self._state_for(self._admin_id).menu

    @_menu.setter
    def _menu(self, value: str) -> None:
        self._state_for(self._admin_id).menu = value

    @property
    def _menu_channel(self) -> str | None:
        return self._state_for(self._admin_id).channel

    @_menu_channel.setter
    def _menu_channel(self, value: str | None) -> None:
        self._state_for(self._admin_id).channel = value

    @property
    def _custom_setting(self) -> str | None:
        return self._state_for(self._admin_id).custom

    @_custom_setting.setter
    def _custom_setting(self, value: str | None) -> None:
        self._state_for(self._admin_id).custom = value

    @property
    def _cloudflare_hostname(self) -> str | None:
        return self._state_for(self._admin_id).cloudflare_hostname

    @_cloudflare_hostname.setter
    def _cloudflare_hostname(self, value: str | None) -> None:
        self._state_for(self._admin_id).cloudflare_hostname = value
