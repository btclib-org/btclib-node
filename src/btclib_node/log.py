# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Logger`, a `logging.Logger` writing to a file or to a stream.

A file handler where a caller names a path -- `Node.__init__` resolves
one under `Config.data_dir` when `Config.log_path` is set -- a stream
handler otherwise, and `close` to release whichever one it opened.
Each line carries the level ahead of its message as Core's
`GetLogPrefix` writes it for a line with no category.
"""

import logging
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["Logger"]


def _level_prefix(levelno: int) -> str:
    """Return what Core's `GetLogPrefix` puts ahead of a line at `levelno`.

    `src/logging.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag, for
    a line with no category, which is every line this node logs: nothing
    at `info`, and `[debug] `, `[warning] ` or `[error] ` otherwise.
    `CRITICAL`, which Core has no level for, is `[error] `.
    """
    if levelno >= logging.ERROR:
        return "[error] "
    if levelno >= logging.WARNING:
        return "[warning] "
    if levelno >= logging.INFO:
        return ""
    return "[debug] "


class _LevelFormatter(logging.Formatter):
    """A `Formatter` putting `_level_prefix` between time and message."""

    @override
    def formatMessage(self, record: logging.LogRecord) -> str:
        return f"{record.asctime} - {_level_prefix(record.levelno)}{record.message}"


class Logger(logging.Logger):
    """A `logging.Logger` writing to `log_path`, or a stream if unset."""

    def __init__(
        self,
        log_path: str | Path | None = None,
        *,
        debug: bool = False,
    ) -> None:
        """Attach a file or stream handler, at `DEBUG` or `INFO` per `debug`."""
        level = logging.DEBUG if debug else logging.INFO
        super().__init__(name="Logger", level=level)
        # `logging.Handler`, which is the type the two branches share:
        # inferred from the first of them instead, the second is a
        # StreamHandler assigned to a name holding a FileHandler. What
        # stood here before the branch was a third handler, built on
        # every path and used on none.
        handler: logging.Handler
        handler = logging.FileHandler(log_path) if log_path else logging.StreamHandler()
        # `%(asctime)s` in the format string is what makes `format` set
        # `record.asctime` before `formatMessage` reads it
        formatter = _LevelFormatter("%(asctime)s - %(message)s")
        handler.setFormatter(formatter)
        self.addHandler(handler)

    def close(self) -> None:
        """Close and detach every handler `__init__` attached."""
        for handler in self.handlers:
            handler.close()
            self.removeHandler(handler)
