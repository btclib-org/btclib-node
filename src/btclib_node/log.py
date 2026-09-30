# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Logger`, a `logging.Logger` writing to a file or to a stream.

A file handler where a caller names a path -- `Node.__init__` resolves
one under `Config.data_dir` when `Config.log_path` is set -- a stream
handler otherwise, and `close` to release whichever one it opened.
Each line opens as Core's `debug.log` line does: `LogTimestampStr`'s
time, in UTC and to the second, then the level as `GetLogPrefix` writes
it for a line with no category.

`open_history_log` is what a caller with a `Config` (or a `Config` never
built at all, `cli.main`'s own refusal after the lock) opens one
through, in Core's own order: the version line between the warnings
buffered while the settings were read and the unrecognised-section
warning, then `LogArgs`'s lines.
"""

import logging
import time
from typing import TYPE_CHECKING, override

from btclib_node.constants import CLIENT_NAME, CLIENT_VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["Logger", "open_history_log"]


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
    """A `Formatter` writing Core's time, `_level_prefix`, then the message.

    The time is `LogTimestampStr`'s (`src/logging.cpp`, at
    bitcoin/bitcoin@9be056a8a7) without `-logtimemicros`, which this
    node does not have: `FormatISO8601DateTime` of the whole second, in
    UTC, and one space.
    """

    @override
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))

    @override
    def formatMessage(self, record: logging.LogRecord) -> str:
        return f"{record.asctime} {_level_prefix(record.levelno)}{record.message}"


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
        if log_path:
            # UTF-8 with `surrogateescape`: a setting holding a byte UTF-8
            # does not accept is logged as that byte, as Core writes the
            # bytes it read (`cli._read_conf_file`)
            handler = logging.FileHandler(
                log_path, encoding="utf-8", errors="surrogateescape"
            )
            stream = handler.stream
            assert stream is not None  # noqa: S101 -- `delay=False` opens it above
            # `StartLogging`, at bitcoin/bitcoin@9be056a8a7
            # (`src/logging.cpp:72`): five blank lines, written to the file
            # the moment it opens and ahead of anything this node logs,
            # marking where this execution's own record begins in a file
            # appended across restarts. A stream has no earlier execution
            # to mark, so this is the file branch alone.
            stream.write("\n\n\n\n\n")
            stream.flush()
        else:
            handler = logging.StreamHandler()
        # `%(asctime)s` in the format string is what makes `format` set
        # `record.asctime` before `formatMessage` reads it
        formatter = _LevelFormatter("%(asctime)s %(message)s")
        handler.setFormatter(formatter)
        self.addHandler(handler)

    def close(self) -> None:
        """Close and detach every handler `__init__` attached."""
        for handler in self.handlers:
            handler.close()
            self.removeHandler(handler)


def open_history_log(
    log_path: str | Path | None,
    *,
    debug: bool,
    log_warnings: Sequence[str] = (),
    section_warning: str = "",
    config_args: Sequence[str] = (),
) -> Logger:
    """Open a `Logger` and write what Core logs ahead of anything else.

    Core's own order, across `init/common.cpp`'s `StartLogging`,
    `LogPackageVersion` and `init.cpp`'s `AppInitParameterInteraction`
    (all at bitcoin/bitcoin@9be056a8a7): `log_warnings`, buffered while
    the settings were read, then the version line, then
    `section_warning` -- the one warning about a section naming no
    chain, empty where there is none -- then `config_args`, `LogArgs`'s
    own "Config file arg:"/"Command-line arg:" lines.
    """
    logger = Logger(log_path, debug=debug)
    for warning in log_warnings:
        logger.warning(warning)
    # `LogPackageVersion` appends " (release build)" or " (debug build)",
    # `#ifdef DEBUG` deciding which -- a compile-time distinction this
    # interpreted tree has no counterpart for, so the line carries
    # neither rather than a suffix that would always read one way
    logger.info("%s version %s", CLIENT_NAME, CLIENT_VERSION)
    if section_warning:
        logger.warning(section_warning)
    for arg in config_args:
        logger.info(arg)
    return logger
