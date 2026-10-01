# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Logger`, a `logging.Logger` writing to a file or to a stream.

A file handler where a caller names a path -- `Node.__init__` resolves
one under `Config.data_dir` when `Config.log_path` is set -- a stream
handler otherwise, and `close` to release whichever one it opened.
Each line opens as Core's `debug.log` line does: `LogTimestampStr`'s
time, in UTC and to the second, then what `GetLogPrefix` writes: the
category of a debug line, as in `[net]`, or the level of any other line.

A debug line is written through `Logger.log_debug` with Core's category
for it, and only where `-debug` selects that category.

`open_history_log` is what a caller with a `Config` (or a `Config` never
built at all, `cli.main`'s own refusal after the lock) opens one
through, in Core's own order: the version line between the warnings
buffered while the settings were read and the unrecognised-section
warning, then the data directory and configuration file lines, then
`LogArgs`'s lines.
"""

import logging
import time
from typing import TYPE_CHECKING, override

from btclib_node.constants import CLIENT_NAME, CLIENT_VERSION, default_data_dir

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

__all__ = ["Logger", "open_history_log"]


def _prefix(record: logging.LogRecord) -> str:
    """Return what Core's `GetLogPrefix` puts ahead of `record`'s message.

    `src/logging.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag: a
    debug line with a category, which `Logger.log_debug` writes, is
    `[<category>] `. A line with none is nothing at `info`, and
    `[debug] `, `[warning] ` or `[error] ` otherwise. `CRITICAL`, which
    Core has no level for, is `[error] `.
    """
    category = getattr(record, "category", None)
    if category:
        return f"[{category}] "
    levelno = record.levelno
    if levelno >= logging.ERROR:
        return "[error] "
    if levelno >= logging.WARNING:
        return "[warning] "
    if levelno >= logging.INFO:
        return ""
    return "[debug] "


class _LevelFormatter(logging.Formatter):
    """A `Formatter` writing Core's time, `_prefix`, then the message.

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
        return f"{record.asctime} {_prefix(record)}{record.message}"


class Logger(logging.Logger):
    """A `logging.Logger` writing to `log_path`, or a stream if unset."""

    def __init__(
        self,
        log_path: str | Path | None = None,
        *,
        debug: bool = False,
        categories: Collection[str] = (),
        excluded: Collection[str] = (),
    ) -> None:
        """Attach a file or stream handler, at `DEBUG` or `INFO` per `debug`.

        `categories` are the ones `log_debug` writes, every category
        where it is empty, but for `excluded`.
        """
        level = logging.DEBUG if debug else logging.INFO
        super().__init__(name="Logger", level=level)
        self._categories = frozenset(categories)
        self._excluded = frozenset(excluded)
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

    def log_debug(self, category: str, msg: str, *args: object) -> None:
        """Write a debug line under Core's `category` if `-debug` selects it.

        Core's `LogDebug(category, ...)` (`src/util/log.h`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), `LogAcceptCategory`
        deciding by the category, which `-debugexclude` takes out.
        `logging.Logger.debug` writes a line with none, which Core never
        does.
        """
        if (
            not self._categories or category in self._categories
        ) and category not in self._excluded:
            self.log(logging.DEBUG, msg, *args, extra={"category": category})

    def close(self) -> None:
        """Close and detach every handler `__init__` attached."""
        for handler in self.handlers:
            handler.close()
            self.removeHandler(handler)


def open_history_log(  # noqa: PLR0913
    log_path: str | Path | None,
    *,
    debug: bool,
    debug_categories: Collection[str] = (),
    debug_exclude: Collection[str] = (),
    data_dir: Path,
    config_file_line: str = "",
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
    chain, empty where there is none -- then `StartLogging`'s own
    "Default data directory" and "Using data directory", then
    `config_file_line`, then `config_args`, `LogArgs`'s own "Config file
    arg:"/"Command-line arg:" lines.
    """
    logger = Logger(
        log_path, debug=debug, categories=debug_categories, excluded=debug_exclude
    )
    for warning in log_warnings:
        logger.warning(warning)
    # `LogPackageVersion` appends " (release build)" or " (debug build)",
    # `#ifdef DEBUG` deciding which -- a compile-time distinction this
    # interpreted tree has no counterpart for, so the line carries
    # neither rather than a suffix that would always read one way
    logger.info("%s version %s", CLIENT_NAME, CLIENT_VERSION)
    if section_warning:
        logger.warning(section_warning)
    logger.info("Default data directory %s", default_data_dir())
    logger.info("Using data directory %s", data_dir)
    if config_file_line:
        logger.info(config_file_line)
    for arg in config_args:
        logger.info(arg)
    return logger
