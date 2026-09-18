"""Incremental ChatRoot writer.

The recorder writes chat to disk while the recording runs, so process memory
does not grow with chat volume. The writer opens `<path>.tmp`, appends one
comment per message, and writes the closing keys at stop. `close()` then
renames the tmp file into place. A crash during the recording leaves a partial
tmp file, never a partial `.chat.json`.

`add_comment()` and `close()` never raise. A write failure stops the capture,
keeps the partial file for recovery, and calls the optional error handler once.
"""

import contextlib
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO

logger = logging.getLogger(__name__)


def file_info() -> dict[str, Any]:
    """Return the ChatRoot FileInfo block for a file written now."""
    now_z = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "Version": {"Major": 1, "Minor": 4, "Patch": 0},
        "CreatedAt": now_z,
        "UpdatedAt": now_z,
    }


class _IndentingWriter:
    """Write JSON text, indenting every new line by the parent's prefix.

    ``json.dumps(value, indent=2).replace("\\n", "\\n  ")`` builds a second
    complete copy of the serialized value. For the embedded emote data that
    copy is megabytes, and it is alive while the first copy still is.
    ``json.dump`` writes its output in small chunks instead, so this wrapper
    adds the prefix chunk by chunk and keeps the bytes identical to the
    replace() form. A JSON encoder escapes a newline inside a string, so a
    real newline in a chunk is always structural and never inside a value.
    """

    def __init__(self, fh: TextIO, prefix: str) -> None:
        self._fh = fh
        self._prefix = prefix

    def write(self, text: str) -> int:
        if not text:
            return 0
        # Report what the handle received, not the input length: the indent
        # adds characters, and a caller may check the count.
        return self._fh.write(text.replace("\n", "\n" + self._prefix) if "\n" in text else text)


def _dump_indented(fh: TextIO, value: Any, prefix: str = "  ") -> None:
    """Serialize ``value`` into ``fh`` with the parent's indentation."""
    json.dump(value, _IndentingWriter(fh, prefix), ensure_ascii=False, indent=2)


class ChatJsonWriter:
    """Write one TwitchDownloader ChatRoot file comment by comment.

    The comments array comes first and the remaining top-level keys follow at
    stop. Top-level key order is not significant. TwitchDownloader and the
    tests deserialize the complete document.
    """

    def __init__(self, path: str, on_error: Callable[[Exception], None] | None = None) -> None:
        self.path = path
        self.tmp_path = path + ".tmp"
        self.comments = 0
        self.failed = False
        self._on_error = on_error
        self._fh: TextIO | None = None
        self._good_offset = 0  # end of the last complete comment
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            # The handle stays open until close(): comments land in the file
            # while the recording runs, not inside one with-block.
            # The file holds chat text, which can carry user data, so create
            # it private rather than with the process umask.
            fd = os.open(self.tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                self._fh = os.fdopen(fd, "w", encoding="utf-8")
            except OSError:
                # _fail() only closes self._fh, which is still None here. A
                # failed fdopen also leaves the descriptor unowned.
                os.close(fd)
                raise
            self._fh.write('{"comments": [')
            self._good_offset = self._fh.tell()
        except OSError as e:
            self._fail(e)

    def add_comment(self, comment: dict[str, Any]) -> bool:
        """Append one comment and flush it. True when the comment is on disk."""
        fh = self._fh
        if fh is None:
            return False
        try:
            fh.write(",\n  " if self.comments else "\n  ")
            _dump_indented(fh, comment)
            fh.flush()
            # tell() can raise too, and the rollback below needs the offset
            # of the last complete comment.
            self._good_offset = fh.tell()
        except (OSError, TypeError, ValueError) as e:
            # A failed write can leave a torn comment in the tmp file. Cut the
            # file back to the last complete comment. This rollback is best
            # effort, so its own failure must not mask the write error.
            with contextlib.suppress(Exception):
                fh.seek(self._good_offset)
                fh.truncate()
            self._fail(e)
            return False
        self.comments += 1
        return True

    def close(self, trailer: dict[str, Any]) -> bool:
        """Write the trailer keys, then rename the file into place.

        A "comments" key in ``trailer`` is skipped: the comments array is
        already in the file, and a second top-level key of that name would
        replace it in JSON readers that keep the last occurrence.

        True when the file is in place. False when the capture already failed,
        so callers can log the outcome.
        """
        fh = self._fh
        if fh is None:
            return False
        try:
            fh.write("]" if not self.comments else "\n]")
            for key, value in trailer.items():
                if key == "comments":
                    logger.error("[chat_writer] trailer key 'comments' collides with the comment array; skipped")
                    continue
                fh.write(",\n  " + json.dumps(key) + ": ")
                _dump_indented(fh, value)
            fh.write("\n}\n")
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()
            self._fh = None
            os.replace(self.tmp_path, self.path)
        except (OSError, TypeError, ValueError) as e:
            self._fail(e)
            return False
        return True

    def discard(self) -> None:
        """Close and remove the tmp file. The target path stays untouched."""
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError as e:
                logger.warning("[chat_writer] close failed for %s: %s", self.tmp_path, e)
            self._fh = None
        try:
            os.unlink(self.tmp_path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("[chat_writer] remove failed for %s: %s", self.tmp_path, e)

    def _fail(self, e: Exception) -> None:
        """Stop writing, keep the partial file, and report the failure once."""
        if self.failed:
            return
        self.failed = True
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None
        logger.error("[chat_writer] write failed for %s: %s", self.path, e)
        if self._on_error is not None:
            try:
                self._on_error(e)
            except Exception:
                logger.error("[chat_writer] error handler failed for %s", self.path, exc_info=True)
