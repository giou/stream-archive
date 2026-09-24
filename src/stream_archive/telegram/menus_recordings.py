"""Recordings browser: list, send, and delete stored recordings.

The browser reads the archive through ``disk.iter_recordings`` and reuses
the recorder guards: a live capture is never a delete candidate, and every
delete invalidates the disk snapshot. The list and the per-file detail view
are plain reply-keyboard submenus: a picked file opens Send / Delete / Back
rows under its name. Destructive deletes still ask for confirm through the
shared inline confirm buttons.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telegram import ReplyKeyboardMarkup

from stream_archive import disk
from stream_archive.mtproto_upload import MAX_UPLOAD_BYTES, check_sendable
from stream_archive.telegram.menu_state import ChatId, MenuResult

if TYPE_CHECKING:
    from stream_archive.telegram.dispatcher import TelegramController

logger = logging.getLogger(__name__)

#: Reply labels of the per-file detail submenu.
SEND_LABEL = "📤 Send"
DELETE_LABEL = "🗑 Delete"

#: Bulk delete of a channel file page, above Back.
DELETE_CHANNEL_LABEL = "🗑 Delete channel files"

#: Bulk delete of the whole archive, above Back on the channel list.
DELETE_ALL_LABEL = "🗑 Delete all files"

#: Files per recordings page. Five rows keep one page below the tap limit.
PAGE_SIZE = 5


def _scan(ctrl: TelegramController) -> list[tuple[float, int, Path]]:
    """Archive recordings, newest first. A missing dir reads as empty."""
    base = disk.resolve_recording_dir(ctrl._config)
    found: list[tuple[float, int, Path]] = []
    if not base.exists():
        return found
    for path in disk.iter_recordings(base):
        try:
            st = path.stat()
        except OSError:
            continue
        found.append((st.st_mtime, st.st_size, path))
    found.sort(key=lambda t: t[0], reverse=True)
    return found


def _live_paths(ctrl: TelegramController) -> set[str]:
    """Real paths of live captures, or empty when the recorder hides them."""
    recorder = ctrl._recorder
    if hasattr(recorder, "_active_paths"):
        return set(recorder._active_paths())
    return set()


def channel_of(base: Path, path: Path) -> str:
    """Channel tag of ``path`` from its archive dir: ``twitch:xqc``."""
    try:
        rel = path.relative_to(base)
    except ValueError:
        return "unknown"
    if len(rel.parts) >= 3 and rel.parts[0] in ("twitch", "kick"):
        return f"{rel.parts[0]}:{rel.parts[1]}"
    if rel.parts:
        return rel.parts[0]
    return "unknown"


def _display_name(path: Path) -> str:
    """Truncated file name as shown on a picker row, without size or marker."""
    return path.name if len(path.name) <= 40 else path.name[:37] + "..."


def _detail_text(path: Path, size: int, mtime: float, live: bool) -> str:
    """Detail body for one recording: name, size, date, send state."""
    ok, note = check_sendable(path)
    if ok:
        send_note = "Sendable over MTProto."
    elif sendable_path(path):
        send_note = "Over 2 GB: sends as split parts."
    else:
        send_note = f"Cannot send: {note}."
    live_note = " Recording now, delete is blocked." if live else ""
    date = datetime.fromtimestamp(mtime).strftime("%d-%m-%Y %H:%M")
    return f"{path.name}\n{disk.format_bytes(size)} · {date}\n{send_note}{live_note}"


def _detail_keyboard(*, sendable: bool = True) -> ReplyKeyboardMarkup:
    """Send / Delete / Back rows of the per-file detail submenu.

    Files at or over the 2 GiB cap get Delete / Back only, unless ffmpeg can
    split them into chunks below the cap.
    """
    rows = [[SEND_LABEL, DELETE_LABEL]] if sendable else [[DELETE_LABEL]]
    rows.append(["Back"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def last_page_start(count: int) -> int:
    """First offset of the last page: keeps Next/Prev on page starts."""
    return ((count - 1) // PAGE_SIZE) * PAGE_SIZE if count else 0


def _page_buttons(count: int, offset: int, live: set[str], files: list[tuple[float, int, Path]]) -> list[list[str]]:
    """Reply-keyboard rows for the page at ``offset`` plus paging rows."""
    offset = max(0, min(offset, last_page_start(count))) if count else 0
    rows: list[list[str]] = []
    for i in range(offset, min(offset + PAGE_SIZE, count)):
        _, size, path = files[i]
        rows.append([_label(path, size, os.path.realpath(path) in live)])
    nav: list[str] = []
    if offset > 0:
        nav.append("◀ Prev")
    if offset + PAGE_SIZE < count:
        nav.append("Next ▶")
    if nav:
        rows.append(nav)
    if count:
        rows.append([DELETE_CHANNEL_LABEL])
    rows.append(["Back"])
    return rows


#: Prefix of a channel row in the recordings channel list.
CHANNEL_PREFIX = "\U0001f3a5 "


def channel_rows(ctrl: TelegramController) -> tuple[list[str], dict[str, list[tuple[float, int, Path]]]]:
    """Channel names plus their files, newest channel first, newest file first."""
    base = disk.resolve_recording_dir(ctrl._config)
    by_channel: dict[str, list[tuple[float, int, Path]]] = {}
    for mtime, size, path in _scan(ctrl):
        by_channel.setdefault(channel_of(base, path), []).append((mtime, size, path))
    ordered = sorted(by_channel, key=lambda ch: max(m for m, _, _ in by_channel[ch]), reverse=True)
    return ordered, by_channel


def channel_label(channel: str, files: list[tuple[float, int, Path]], live: set[str]) -> str:
    """One channel row: tag, file count, total size, live marker when recording."""
    total = sum(size for _, size, _ in files)
    mark = "\U0001f534 " if any(os.path.realpath(p) in live for _, _, p in files) else ""
    n = len(files)
    return f"{mark}{CHANNEL_PREFIX}{channel} ({n} file{'s' if n != 1 else ''}, {disk.format_bytes(total)})"


def _channel_keyboard(ctrl: TelegramController) -> Any:
    """Channel list keyboard: one row per channel, bulk delete, plus Back."""
    from telegram import ReplyKeyboardMarkup

    ordered, by_channel = channel_rows(ctrl)
    live = _live_paths(ctrl)
    rows = [[channel_label(ch, by_channel[ch], live)] for ch in ordered]
    if ordered:
        rows.append([DELETE_ALL_LABEL])
    rows.append(["Back"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _channel_list_text(ctrl: TelegramController) -> str:
    """Header of the channel list: file and byte totals."""
    files = _scan(ctrl)
    total = sum(size for _, size, _ in files)
    n = len(files)
    return f"Recordings ({n} file{'s' if n != 1 else ''}, {disk.format_bytes(total)}). Tap a channel:"


async def open_recordings(ctrl: TelegramController, chat_id: ChatId) -> MenuResult:
    """Open the recordings browser at the channel list."""
    files = _scan(ctrl)
    if not files:
        ctrl._enter_menu(chat_id, "recordings")
        return "No recordings stored yet.", ctrl.reply_keyboard("recordings", chat_id=chat_id)
    state = ctrl._enter_menu(chat_id, "recordings")
    state.rec_offset = 0
    state.rec_path = None
    state.rec_channel = None
    return _channel_list_text(ctrl), _channel_keyboard(ctrl)


def _match_channel_row(ordered: list[str], text: str) -> str | None:
    """Channel tag of a channel-list row press, or None.

    The row carries the live marker, the file count, and the total size,
    and all three move while a channel records. Match the stable tag
    only, so a tap still lands after the numbers change.
    """
    want = text
    if want.startswith("\U0001f534 "):
        want = want[len("\U0001f534 ") :]
    if not want.startswith(CHANNEL_PREFIX):
        return None
    want = want[len(CHANNEL_PREFIX) :]
    if " (" in want and want.endswith(")"):
        want = want.rsplit(" (", 1)[0]
    return want if want in ordered else None


def _strip_row(text: str) -> str:
    """Picker row text without the live marker and the trailing size."""
    if text.startswith("\U0001f534 "):
        text = text[len("\U0001f534 ") :]
    if text.endswith(")") and " (" in text:
        text = text.rsplit(" (", 1)[0]
    return text


def _label(path: Path, size: int, live: bool) -> str:
    """One picker row: name, size, and a live marker when recording now."""
    return f"{'\U0001f534 ' if live else ''}{_display_name(path)} ({disk.format_bytes(size)})"


def _find_pick(files: list[tuple[float, int, Path]], live: set[str], text: str) -> Path | None:
    """File whose picker row matches the pressed ``text``.

    Exact row first: name, size, and live marker all match. Finished files
    then stay distinct even when their truncated names collide. A live
    capture grows between the list render and the tap, so a row with no
    exact match retries size-insensitive, live files only. Truncated names
    can still collide: then the pick is ambiguous, return None, and the
    caller asks to re-open instead of touching the wrong file.
    """
    match: Path | None = None
    for _, size, path in files:
        if _label(path, size, os.path.realpath(path) in live) != text:
            continue
        if match is not None:
            return None
        match = path
    if match is not None:
        return match
    want = _strip_row(text)
    for _, _, path in files:
        if _display_name(path) != want:
            continue
        if match is not None:
            return None
        match = path
    return match


def sendable_path(path: str | Path) -> bool:
    """True when ``path`` passes the MTProto size gate or splits below it."""
    from stream_archive.recorder.remux import ffmpeg_available

    ok, _ = check_sendable(Path(path))
    if ok:
        return True
    # Only an over-cap file can still go as split parts. Gate on the size
    # itself, not on the wording of the rejection note.
    try:
        over = Path(path).stat().st_size > MAX_UPLOAD_BYTES
    except OSError:
        return False
    return over and ffmpeg_available()


def _detail_body(ctrl: TelegramController, path: Path) -> tuple[str, Any] | None:
    """Detail text plus detail keyboard for ``path``, or None when it is gone."""
    try:
        st = path.stat()
    except OSError:
        return None
    live = os.path.realpath(path) in _live_paths(ctrl)
    return _detail_text(path, st.st_size, st.st_mtime, live), _detail_keyboard(sendable=sendable_path(path))


def _clamp_offset(ctrl: TelegramController, chat_id: ChatId, count: int) -> int:
    """Clamp the stored page offset to the shrunken archive. Returns it."""
    state = ctrl._state_for(chat_id)
    state.rec_offset = min(max(0, state.rec_offset), last_page_start(count))
    return state.rec_offset


def _list_keyboard(ctrl: TelegramController, chat_id: ChatId) -> Any:
    """Channel list keyboard. Empty archive falls back to the bare menu."""
    ordered, by_channel = channel_rows(ctrl)
    if not ordered:
        return ctrl.reply_keyboard("recordings", chat_id=chat_id)
    live = _live_paths(ctrl)
    rows = [[channel_label(ch, by_channel[ch], live)] for ch in ordered]
    rows.append([DELETE_ALL_LABEL])
    rows.append(["Back"])
    from telegram import ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _open_detail(ctrl: TelegramController, chat_id: ChatId, path: Path) -> MenuResult:
    """Enter the detail submenu for ``path`` and render it."""
    ctrl._enter_menu(chat_id, "rec_detail")
    ctrl._state_for(chat_id).rec_path = str(path)
    body = _detail_body(ctrl, path)
    if body is None:
        ctrl._enter_menu(chat_id, "recordings")
        ctrl._state_for(chat_id).rec_path = None
        return f"{path.name} is gone.", _list_keyboard(ctrl, chat_id)
    text, markup = body
    return text, markup


async def _send_picked(ctrl: TelegramController, chat_id: ChatId) -> MenuResult:
    """Start the MTProto upload of the picked file. Reports why not when blocked."""
    state = ctrl._state_for(chat_id)
    picked = state.rec_path
    if not picked:
        return "No recording picked. Open Recordings again.", ctrl.reply_keyboard("recordings", chat_id=chat_id)
    if ctrl._sending_rec_path(picked):
        body = _detail_body(ctrl, Path(picked))
        text = "That upload already runs. Wait for it to finish."
        if body is not None:
            text = f"{text}\n\n{body[0]}"
        return text, ctrl.reply_keyboard("rec_detail", chat_id=chat_id)
    uploader = getattr(ctrl, "_mtproto", None)
    if uploader is None or not uploader.enabled:
        return "MTProto upload is off. Enable it under Settings, then MTProto upload.", ctrl.reply_keyboard(
            "rec_detail", chat_id=chat_id
        )
    path = Path(picked)
    ok, note = check_sendable(path)
    if not ok and not sendable_path(path):
        body = _detail_body(ctrl, path)
        text = f"Cannot send {path.name}: {note}."
        if body is not None:
            text = f"{text}\n\n{body[0]}"
        return text, ctrl.reply_keyboard("rec_detail", chat_id=chat_id)
    await ctrl._start_mtproto_send(chat_id, str(path))
    return None


def _store_pending_delete(ctrl: TelegramController, chat_id: ChatId, path: str) -> str:
    """Stash ``path`` for the delete confirm of ``chat_id``. Returns its nonce."""
    import secrets

    store = ctrl._pending_delete
    for _ in range(16):
        nonce = secrets.token_hex(4)
        if (chat_id, nonce) not in store:
            store[(chat_id, nonce)] = path
            break
    else:
        nonce = secrets.token_hex(8)
        store[(chat_id, nonce)] = path
    ctrl._prune_pending(store, chat_id)
    return nonce


async def _ask_delete(ctrl: TelegramController, chat_id: ChatId) -> MenuResult:
    """Ask for confirm before deleting the picked file, via inline buttons.

    The callback carries a nonce only: raw archive paths overflow Telegram's
    64-byte callback_data cap and the confirm never arrives.
    """
    from stream_archive.telegram.menus_callbacks import confirm_keyboard

    state = ctrl._state_for(chat_id)
    picked = state.rec_path
    if not picked:
        return "No recording picked. Open Recordings again.", ctrl.reply_keyboard("recordings", chat_id=chat_id)
    path = Path(picked)
    if not path.exists():
        return f"{path.name} is already gone.", ctrl.reply_keyboard("rec_detail", chat_id=chat_id)
    try:
        size = path.stat().st_size
    except OSError:
        return f"{path.name} is already gone.", ctrl.reply_keyboard("rec_detail", chat_id=chat_id)
    nonce = _store_pending_delete(ctrl, chat_id, picked)
    return (
        f"Delete {path.name} ({disk.format_bytes(size)})? This cannot be undone.",
        confirm_keyboard("confirm_recdel", nonce),
    )


def _store_pending_bulk(ctrl: TelegramController, chat_id: ChatId, channel: str | None) -> str:
    """Stash a bulk-delete scope for ``chat_id``. Returns its nonce.

    ``channel`` is one channel tag, or None for the whole archive. The
    callback carries a nonce only: channel tags overflow Telegram's
    64-byte callback_data cap.
    """
    import secrets

    store = ctrl._pending_bulk_delete
    for _ in range(16):
        nonce = secrets.token_hex(4)
        if (chat_id, nonce) not in store:
            store[(chat_id, nonce)] = channel
            break
    else:
        nonce = secrets.token_hex(8)
        store[(chat_id, nonce)] = channel
    ctrl._prune_pending(store, chat_id)
    return nonce


def _bulk_targets(ctrl: TelegramController, channel: str | None) -> list[tuple[float, int, Path]]:
    """Files a bulk delete covers: one channel, or the whole archive."""
    if channel is None:
        return _scan(ctrl)
    return _channel_files(ctrl, channel)


async def _ask_bulk_delete(ctrl: TelegramController, chat_id: ChatId, channel: str | None) -> MenuResult:
    """Ask for confirm before deleting a channel or the whole archive."""
    from stream_archive.telegram.menus_callbacks import confirm_keyboard

    files = _bulk_targets(ctrl, channel)
    if not files:
        if channel is None:
            return "No recordings stored yet.", ctrl.reply_keyboard("recordings", chat_id=chat_id)
        return _channel_list_text(ctrl), _channel_keyboard(ctrl)
    total = sum(size for _, size, _ in files)
    n = len(files)
    where = "the archive" if channel is None else channel
    nonce = _store_pending_bulk(ctrl, chat_id, channel)
    return (
        f"Delete {n} file{'s' if n != 1 else ''} ({disk.format_bytes(total)}) from {where}? "
        "Live captures stay. This cannot be undone.",
        confirm_keyboard("confirm_recbulk", nonce),
    )


def _bulk_scope(ctrl: TelegramController, chat_id: ChatId, nonce: str) -> tuple[bool, str | None]:
    """Scope a bulk-delete confirm targets: ``(expired, scope)``.

    Scope is one channel tag, or None for the whole archive. A channel
    scope must still equal the picked channel: a re-pick between Delete
    and Confirm targets the new pick, never the stale prompt.
    """
    store = ctrl._pending_bulk_delete
    if (chat_id, nonce) not in store:
        return True, None
    scope = store.pop((chat_id, nonce))
    if scope is not None and ctrl._state_for(chat_id).rec_channel != scope:
        return True, None
    return False, scope


async def handle_bulk_callback(ctrl: TelegramController, data: str, chat_id: ChatId) -> tuple[str, Any] | None:
    """Apply one bulk-delete inline-button press for ``chat_id``."""
    rest = data.split(":", 1)[1] if ":" in data else ""
    nonce = rest.split(":")[0] if rest else ""
    expired, scope = _bulk_scope(ctrl, chat_id, nonce)
    if expired:
        return "That button expired. Open Recordings again.", None
    return await _delete_bulk(ctrl, chat_id, scope)


async def _delete_bulk(ctrl: TelegramController, chat_id: ChatId, channel: str | None) -> tuple[str, Any] | None:
    """Delete every finished recording in scope. Live captures stay.

    Returns the channel file page (or the channel list when the channel
    emptied or the scope was the whole archive).
    """
    from stream_archive import disk as disk_mod

    files = _bulk_targets(ctrl, channel)
    if not files:
        if channel is None:
            return "No recordings stored yet.", None
        return _channel_list_text(ctrl), _channel_keyboard(ctrl)
    live = _live_paths(ctrl)
    recorder = ctrl._recorder
    deleted = 0
    freed = 0
    skipped = 0
    for _, _, path in files:
        if os.path.realpath(path) in live:
            skipped += 1
            continue
        if hasattr(recorder, "_remove_if_inactive"):
            try:
                got = recorder._remove_if_inactive(path, live)
            except OSError:
                logger.warning("[telegram] Failed to delete %s", path, exc_info=True)
                continue
            if got is None:
                skipped += 1
                continue
            freed += got
            deleted += 1
            continue
        try:
            freed += path.stat().st_size
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("[telegram] Failed to delete %s", path, exc_info=True)
            continue
        deleted += 1
    disk_mod.invalidate_snapshot()
    parts = [f"Deleted {deleted} file{'s' if deleted != 1 else ''} ({disk.format_bytes(freed)})."]
    if skipped:
        parts.append(f"{skipped} live capture{'s stay' if skipped != 1 else ' stays'}.")
    state = ctrl._state_for(chat_id)
    state.rec_path = None
    if channel is not None and _channel_files(ctrl, channel):
        ctrl._enter_menu(chat_id, "rec_channel")
        state.rec_channel = channel
        return " ".join(parts), _channel_page_keyboard(ctrl, chat_id)
    ctrl._enter_menu(chat_id, "recordings")
    state.rec_channel = None
    return " ".join(parts), _list_keyboard(ctrl, chat_id)


def _channel_files(ctrl: TelegramController, channel: str) -> list[tuple[float, int, Path]]:
    """Newest-first files of ``channel`` (empty when the channel is gone)."""
    _, by_channel = channel_rows(ctrl)
    return by_channel.get(channel, [])


def _file_page_text(channel: str, files: list[tuple[float, int, Path]]) -> str:
    """Header of one channel file page."""
    total = sum(size for _, size, _ in files)
    n = len(files)
    return f"{channel} ({n} file{'s' if n != 1 else ''}, {disk.format_bytes(total)}). Tap a file to manage it:"


async def menu_recordings(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route one press in the recordings browser: channel, file, or page."""
    from telegram import ReplyKeyboardMarkup

    state = ctrl._state_for(chat_id)
    ordered, by_channel = channel_rows(ctrl)
    if not ordered:
        ctrl._enter_menu(chat_id, "recordings")
        return "No recordings stored yet.", ctrl.reply_keyboard("recordings", chat_id=chat_id)
    live = _live_paths(ctrl)
    if text == DELETE_ALL_LABEL:
        return await _ask_bulk_delete(ctrl, chat_id, None)
    # A channel row carries the live marker, the file count, and the
    # total size, and all three move while a channel records. Match the
    # stable tag only, so a tap still lands after the numbers change.
    picked = _match_channel_row(ordered, text)
    if picked is not None:
        ch = picked
        ctrl._enter_menu(chat_id, "rec_channel")
        st = ctrl._state_for(chat_id)
        st.rec_channel = ch
        st.rec_offset = 0
        st.rec_path = None
        files = by_channel[ch]
        rows = _page_buttons(len(files), 0, live, files)
        return _file_page_text(ch, files), ReplyKeyboardMarkup(rows, resize_keyboard=True)
    files = _scan(ctrl)
    if text == "Next ▶":
        state.rec_offset = min(state.rec_offset + PAGE_SIZE, last_page_start(len(files)))
        rows = _page_buttons(len(files), state.rec_offset, live, files)
        return f"Recordings ({len(files)}). Tap a file to manage it:", ReplyKeyboardMarkup(rows, resize_keyboard=True)
    if text == "◀ Prev":
        state.rec_offset = max(0, state.rec_offset - PAGE_SIZE)
        rows = _page_buttons(len(files), state.rec_offset, live, files)
        return f"Recordings ({len(files)}). Tap a file to manage it:", ReplyKeyboardMarkup(rows, resize_keyboard=True)
    pick = _find_pick(files, live, text)
    if pick is not None:
        return _open_detail(ctrl, chat_id, pick)
    if any(
        _strip_row(_label(path, size, os.path.realpath(path) in live)) == _strip_row(text) for _, size, path in files
    ):
        return "Two files share that label. Rename one file, then open Recordings again.", _list_keyboard(ctrl, chat_id)
    return None


async def menu_rec_channel(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route one press in a channel file page: pick, page, Back to channels."""
    from telegram import ReplyKeyboardMarkup

    state = ctrl._state_for(chat_id)
    channel = state.rec_channel
    files = _channel_files(ctrl, channel) if channel else []
    if not files:
        ctrl._enter_menu(chat_id, "recordings")
        ctrl._state_for(chat_id).rec_channel = None
        return _channel_list_text(ctrl), _channel_keyboard(ctrl)
    live = _live_paths(ctrl)
    if text == DELETE_CHANNEL_LABEL:
        return await _ask_bulk_delete(ctrl, chat_id, channel)
    if text == "Next ▶":
        state.rec_offset = min(state.rec_offset + PAGE_SIZE, last_page_start(len(files)))
        rows = _page_buttons(len(files), state.rec_offset, live, files)
        return _file_page_text(channel or "", files), ReplyKeyboardMarkup(rows, resize_keyboard=True)
    if text == "◀ Prev":
        state.rec_offset = max(0, state.rec_offset - PAGE_SIZE)
        rows = _page_buttons(len(files), state.rec_offset, live, files)
        return _file_page_text(channel or "", files), ReplyKeyboardMarkup(rows, resize_keyboard=True)
    pick = _find_pick(files, live, text)
    if pick is not None:
        return _open_detail(ctrl, chat_id, pick)
    if any(
        _strip_row(_label(path, size, os.path.realpath(path) in live)) == _strip_row(text) for _, size, path in files
    ):
        return "Two files share that label. Rename one file, then open Recordings again.", _channel_page_keyboard(
            ctrl, chat_id
        )
    return None


def _channel_page_keyboard(ctrl: TelegramController, chat_id: ChatId) -> Any:
    """File page keyboard of the stored channel at the stored offset."""
    from telegram import ReplyKeyboardMarkup

    state = ctrl._state_for(chat_id)
    files = _channel_files(ctrl, state.rec_channel or "")
    if not files:
        return _channel_keyboard(ctrl)
    offset = _clamp_offset(ctrl, chat_id, len(files))
    live = _live_paths(ctrl)
    rows = _page_buttons(len(files), offset, live, files)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def menu_rec_detail(ctrl: TelegramController, chat_id: ChatId, text: str) -> MenuResult:
    """Route one press in the per-file detail submenu: Send, Delete, or Back."""
    if text == SEND_LABEL:
        picked = ctrl._state_for(chat_id).rec_path
        if picked:
            ok, note = check_sendable(Path(picked))
            if not ok and not sendable_path(picked):
                return (
                    f"Cannot send {Path(picked).name}: {note}.",
                    ctrl.reply_keyboard("rec_detail", chat_id=chat_id),
                )
        return await _send_picked(ctrl, chat_id)
    if text == DELETE_LABEL:
        return await _ask_delete(ctrl, chat_id)
    return None


def _confirm_delete_target(ctrl: TelegramController, chat_id: ChatId, nonce: str) -> str | None:
    """Path the delete confirm targets: the nonce map entry of this chat.

    The entry must still equal the picked file: a re-pick between Delete and
    Confirm targets the new pick, never the stale prompt.
    """
    pending = ctrl._pending_delete.pop((chat_id, nonce), None)
    if pending is None:
        return None
    picked = ctrl._state_for(chat_id).rec_path
    if not picked or picked != pending:
        return None
    return picked


async def handle_rec_callback(ctrl: TelegramController, data: str, chat_id: ChatId) -> tuple[str, Any] | None:
    """Apply one recordings inline-button press for ``chat_id``.

    Only the delete confirm travels inline now: the detail submenu owns Send
    and Delete as reply-keyboard rows. The confirm binds the exact path it
    was asked for, so a re-pick between Delete and Confirm cannot retarget it.
    """
    if data == "confirm_recdel" or data.startswith("confirm_recdel:"):
        # Wire form is confirm_recdel:<nonce>:<guard-nonce>; the path lives
        # server-side in _pending_delete. The guard lives in menus_callbacks:
        # it marks handled there before delegating.
        rest = data.split(":", 1)[1] if ":" in data else ""
        nonce = rest.split(":")[0] if rest else ""
        target = _confirm_delete_target(ctrl, chat_id, nonce)
        if target is None:
            return "That button expired. Open Recordings again.", None
        return await _delete_picked(ctrl, chat_id)
    return None


async def _delete_picked(ctrl: TelegramController, chat_id: ChatId) -> tuple[str, Any] | None:
    """Delete the picked recording. Live captures stay.

    Returns the channel file page (or the channel list when the channel
    emptied), so the browser never strands on a bare Back row.
    """
    from stream_archive import disk as disk_mod

    state = ctrl._state_for(chat_id)
    picked = state.rec_path
    channel = state.rec_channel
    if not picked:
        return "No recording picked. Open Recordings again.", None
    path = Path(picked)
    recorder = ctrl._recorder
    active = _live_paths(ctrl)
    if hasattr(recorder, "_remove_if_inactive"):
        freed = recorder._remove_if_inactive(path, active)
        if freed is None:
            if not path.exists():
                return f"{path.name} is already gone.", None
            return f"{path.name} is recording now. Delete is blocked.", None
        disk_mod.invalidate_snapshot()
        return f"Deleted {path.name} ({disk.format_bytes(freed)}).", _after_delete_keyboard(ctrl, chat_id, channel)
    if os.path.realpath(path) in active:
        return f"{path.name} is recording now. Delete is blocked.", None
    try:
        size = path.stat().st_size
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("[telegram] Failed to delete %s", path, exc_info=True)
        return f"Could not delete {path.name}. See logs.", None
    disk_mod.invalidate_snapshot()
    return f"Deleted {path.name} ({disk.format_bytes(size)}).", _after_delete_keyboard(ctrl, chat_id, channel)


def _after_delete_keyboard(ctrl: TelegramController, chat_id: ChatId, channel: str | None) -> Any:
    """Keyboard after a delete: the channel page, or the channel list."""
    from telegram import ReplyKeyboardMarkup

    ctrl._enter_menu(chat_id, "recordings")
    ctrl._state_for(chat_id).rec_path = None
    if channel:
        files = _channel_files(ctrl, channel)
        if files:
            ctrl._enter_menu(chat_id, "rec_channel")
            st = ctrl._state_for(chat_id)
            st.rec_channel = channel
            offset = _clamp_offset(ctrl, chat_id, len(files))
            live = _live_paths(ctrl)
            rows = _page_buttons(len(files), offset, live, files)
            return ReplyKeyboardMarkup(rows, resize_keyboard=True)
    return _list_keyboard(ctrl, chat_id)
