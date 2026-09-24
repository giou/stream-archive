"""Per-chat Telegram menu state shared by the dispatcher and menu modules.

The bot used to keep one global menu, so two chats could never hold
different menus. State now lives in small ``MenuState`` records keyed by
chat id. Confirm guards use ``(chat_id, nonce)`` keys, so one chat can
never confirm another chat's prompt.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup

from stream_archive.config import AppConfig

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController

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
    rec_offset: int = 0
    rec_path: str | None = None
    rec_channel: str | None = None


#: Fields of ``MenuState`` that one menu reads. Navigation clears every field
#: that the target menu does not own, so no value of an earlier flow
#: survives. This table is the only place that decides the ownership.
_OWNED_FIELDS: dict[str, frozenset[str]] = {
    "channel": frozenset({"channel"}),
    "channel_mode": frozenset({"channel"}),
    "channel_hold": frozenset({"channel"}),
    "channel_quality": frozenset({"channel"}),
    # The custom menu waits for a value, so it owns the pending key. It keeps
    # the channel: the channel_hold key names the channel to change.
    "custom": frozenset({"channel", "custom"}),
    # The DNS step reads the hostname that the hostname step stored.
    "kick_cloudflare_dns": frozenset({"cloudflare_hostname"}),
    # The recordings browser keeps its page and its picked file. The
    # channel page keeps its channel too; the list owns neither file.
    "recordings": frozenset({"rec_offset"}),
    "rec_channel": frozenset({"rec_offset", "rec_channel"}),
    "rec_detail": frozenset({"rec_offset", "rec_path", "rec_channel"}),
}

#: Menu that owns each custom value setting.
_CUSTOM_PARENT: dict[str, str] = {
    "retention": "storage",
    "maxrec": "storage",
    "maxyt": "storage",
    "disk_maxsize": "disk",
    "channel_hold": "channel",
}


def custom_parent(custom: str) -> str:
    """Return the menu that owns the custom setting ``custom``.

    An unknown key goes to the root menu. A value menu that holds a key of
    an older version can then never open an unrelated menu.
    """
    return _CUSTOM_PARENT.get(custom, "root")


async def open_menu(
    ctrl: TelegramController,
    menu: str,
    chat_id: ChatId,
    *,
    channel: str | None = None,
) -> MenuResult:
    """Open ``menu`` for one chat and answer with its text and keyboard.

    Without ``channel`` the text and the keyboard fall back to the channel
    of the chat state. Both read that state, so they render the same channel.
    """
    ctrl._enter_menu(chat_id, menu, channel=channel)
    return await ctrl.menu_text(menu, channel, chat_id=chat_id), ctrl.reply_keyboard(menu, channel, chat_id=chat_id)


async def pick_preset(
    ctrl: TelegramController,
    text: str,
    choices: dict[str, str],
    apply: Callable[[str], str],
    back: str,
    chat_id: ChatId,
    *,
    global_value: str | None = None,
) -> MenuResult:
    """Apply the preset that one pressed label names, then open ``back``.

    ``global_value`` is the config value of the "Global" button. A menu
    without that button passes none, so a "Global" press is not handled.
    """
    if text == "Global":
        if global_value is None:
            return None
        value = global_value
    elif text in choices:
        value = choices[text]
    else:
        return None
    result = apply(value)
    ctrl._enter_menu(chat_id, back)
    return result, ctrl.reply_keyboard(back, chat_id=chat_id)


class ChatStateMixin:
    """Per-chat menu storage for the controller.

    Owns the ``_states`` map, the confirm guards, and chat resolution.
    Every caller reads and writes the menu of a chat through
    ``_state_for(chat_id)``.
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

    def _mark_confirm_done(self, chat_id: ChatId, nonce: str) -> None:
        """Remember one handled prompt. Keep only the newest markers of the chat.

        The key holds the nonce of one prompt, so both buttons of that prompt
        share it: a Confirm retires the Cancel of the same message and the
        other way round. A marker never matches a later message, whose nonce
        differs. The bound stops the store from growing for the whole process
        lifetime.
        """
        self._confirm_done[(chat_id, nonce)] = None
        keys = [key for key in self._confirm_done if key[0] == chat_id]
        for key in keys[:-_CONFIRM_DONE_LIMIT]:
            del self._confirm_done[key]

    def _press_handled(self, chat_id: ChatId, nonce: str) -> bool:
        """True when that chat already handled a prompt with this nonce."""
        return (chat_id, nonce) in self._confirm_done

    def _enter_menu(self, chat_id: ChatId, menu: str, *, channel: str | None = None) -> MenuState:
        """Move one chat into ``menu`` and clear the fields that menu does not own.

        Every navigation goes through here, so a value of an earlier flow (a
        pending custom key or a tunnel hostname) can never reach the next
        menu. See ``_OWNED_FIELDS`` for the fields of each menu.
        """
        state = self._state_for(chat_id)
        if channel is not None:
            state.channel = channel
        owned = _OWNED_FIELDS.get(menu, frozenset())
        if "channel" not in owned:
            state.channel = None
        if "custom" not in owned:
            state.custom = None
        if "cloudflare_hostname" not in owned:
            state.cloudflare_hostname = None
        if "rec_offset" not in owned:
            state.rec_offset = 0
        if "rec_path" not in owned:
            state.rec_path = None
        if "rec_channel" not in owned:
            state.rec_channel = None
        state.menu = menu
        return state

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
        return self._enter_menu(chat_id, "root")
