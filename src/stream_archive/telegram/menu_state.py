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
PendingKey = tuple[ChatId, str]

#: Audio-only switch awaiting confirm: quality change plus affected channels.
AudioSwitch = tuple[Callable[[AppConfig], Any], list[str]]

#: What one menu press returns: reply text plus keyboard, or None to ignore.
MenuResult = tuple[str, ReplyKeyboardMarkup | InlineKeyboardMarkup] | None

#: Prefix of the channel buttons in the channel list. The renderer of the
#: keyboard and the router of its presses must use the same value.
CHANNEL_BUTTON_PREFIX = "\u2022 "

#: Prefix of every failure reply that a handler returns.
_ERROR_PREFIX = "\u274c"

#: Most unanswered prompts one chat keeps. Without a bound a very old
#: prompt stays a valid key and re-applies its stale change.
_PENDING_LIMIT = 8

#: Most handled presses one chat remembers. The guard only matters while
#: the inline keyboard of its message is still tappable.
_CONFIRM_DONE_LIMIT = 32


def is_error(result: str) -> bool:
    """True when a handler result is a failure reply, not a success message.

    Every handler reports a failure with one shape, so callers that run
    cleanup after a change must branch through this helper. A plain check
    of the prefix would silently skip the cleanup after a change of shape.
    """
    return result.startswith(_ERROR_PREFIX)


@dataclass
class MenuState:
    """Reply-keyboard position of one chat."""

    menu: str = "root"
    channel: str | None = None
    custom: str | None = None
    cloudflare_hostname: str | None = None


class ChatStateMixin:
    """Per-chat menu storage for the controller.

    Owns the ``_states`` map, the confirm guards, and chat resolution.
    The ``_menu``-family properties are single-chat views of the admin
    menu for old callers; new code uses ``_state_for(chat_id)``.
    """

    _admin_id: int
    _states: dict[ChatId, MenuState]
    _confirm_done: dict[PendingKey, None]
    _pending_apply: dict[PendingKey, tuple[str, list[str]]]
    _pending_audio_switch: dict[PendingKey, AudioSwitch]
    _apply_warnings_sent: set[PendingKey]

    def _init_chat_state(self) -> None:
        self._states = {}  # chat id -> reply-keyboard menu, one per chat
        # (chat id, callback data) -> None. An ordered set: the value is
        # unused, and the oldest marker is the first key.
        self._confirm_done = {}
        self._pending_apply = {}  # (chat id, nonce) -> (summary, channels) awaiting apply-now
        self._pending_audio_switch = {}  # (chat id, nonce) -> (quality change, channels) awaiting confirm
        self._apply_warnings_sent = set()  # pending keys already messaged

    def _mark_confirm_done(self, chat_id: ChatId, data: str) -> None:
        """Remember one handled press. Keep only the newest markers of the chat.

        The key holds the nonce of one prompt, so a marker can never match a
        later press. The bound stops the store from growing for the whole
        process lifetime.
        """
        self._confirm_done[(chat_id, data)] = None
        keys = [key for key in self._confirm_done if key[0] == chat_id]
        for key in keys[:-_CONFIRM_DONE_LIMIT]:
            del self._confirm_done[key]

    def _state_for(self, chat_id: ChatId) -> MenuState:
        """Return the menu of ``chat_id``, creating the root menu on first use."""
        state = self._states.get(chat_id)
        if state is None:
            state = MenuState()
            self._states[chat_id] = state
        return state

    def _prune_pending(self, store: dict[PendingKey, Any], chat_id: ChatId) -> None:
        """Drop the oldest prompts of ``chat_id`` beyond ``_PENDING_LIMIT``.

        A dropped prompt is unreachable, so its guard marker goes too.
        """
        keys = [key for key in store if key[0] == chat_id]
        for key in keys[:-_PENDING_LIMIT]:
            del store[key]
            self._apply_warnings_sent.discard(key)

    def _chat_of(self, update: Any) -> ChatId:
        """Chat id of an update, defaulting to the admin chat."""
        chat = getattr(update, "effective_chat", None)
        if chat is not None and getattr(chat, "id", None) is not None:
            cid: int = chat.id
            return cid
        return self._admin_id

    def _callback_chat_of(self, update: Any) -> ChatId:
        """Chat id of a callback query, defaulting to the admin chat.

        The guard keys use chat ids, so the fallback must be a chat id
        too. A user id would miss the stored key in a group chat.
        """
        query = update.callback_query
        message = getattr(query, "message", None)
        chat = getattr(message, "chat", None) if message is not None else None
        if chat is not None and getattr(chat, "id", None) is not None:
            cid: int = chat.id
            return cid
        return self._admin_id

    def _show_root(self, chat_id: ChatId) -> MenuState:
        """Reset one chat to the root menu and drop its per-flow fields."""
        state = self._state_for(chat_id)
        state.menu, state.channel = "root", None
        state.custom = None
        state.cloudflare_hostname = None
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
