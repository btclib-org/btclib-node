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

A `Logger` writing to a file limits each source location's info,
warning and error lines as Core's `LogRateLimiter` does (`-logratelimit`,
on by default): `RATELIMIT_MAX_BYTES` per `RATELIMIT_WINDOW`, then one
warning and silence until the window ends, then one more warning for
what was dropped. Every line written while a location is silenced starts
with `[*]` and a space. A debug line is never limited: Core's `LogDebug`
is not, "because users specifying -debug are assumed to be developers or
power users" (`src/util/log.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag).

`open_history_log` is what a caller with a `Config` (or a `Config` never
built at all, `cli.main`'s own refusal after the lock) opens one
through, in Core's own order: the version line between the warnings
buffered while the settings were read and the unrecognised-section
warning, then the data directory and configuration file lines, then
`LogArgs`'s lines.
"""

import logging
import threading
import time
from collections.abc import Callable  # noqa: TC003 -- the docs build resolves it
from enum import Enum, auto
from typing import TYPE_CHECKING, override

from btclib_node.constants import CLIENT_NAME, CLIENT_VERSION, default_data_dir

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

__all__ = [
    "RATELIMIT_MAX_BYTES",
    "RATELIMIT_WINDOW",
    "LogRateLimiter",
    "Logger",
    "RateLimitStatus",
    "open_history_log",
]

# `BCLog::RATELIMIT_MAX_BYTES` and `BCLog::RATELIMIT_WINDOW`
# (`src/logging.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the
# bytes one source location may write per window, and the window in seconds
RATELIMIT_MAX_BYTES = 1024 * 1024
RATELIMIT_WINDOW = 3600

# a source location as Core keys its limiter by one: file, line, function
_SourceLocation = tuple[str, int, str]


class RateLimitStatus(Enum):
    """`BCLog::LogRateLimiter::Status`."""

    UNSUPPRESSED = auto()
    NEWLY_SUPPRESSED = auto()
    STILL_SUPPRESSED = auto()


class _Stats:
    """`BCLog::LogRateLimiter::Stats`: one source location's byte budget."""

    def __init__(self, max_bytes: int) -> None:
        """Start with `max_bytes` available."""
        self.available_bytes = max_bytes
        self.dropped_bytes = 0

    def consume(self, nbytes: int) -> bool:
        """Spend `nbytes` and answer `True`, or drop them and answer `False`."""
        if nbytes > self.available_bytes:
            self.dropped_bytes += nbytes
            self.available_bytes = 0
            return False
        self.available_bytes -= nbytes
        return True


class LogRateLimiter:
    """A fixed-window byte budget per source location, as Core's.

    `BCLog::LogRateLimiter` (`src/logging.h` and `src/logging.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Core's scheduler resets
    every budget each window; this one resets at the first `advance`
    past the window's end, which every line written calls. Not
    thread-safe: `Logger` holds its own lock around every call.
    """

    def __init__(
        self,
        max_bytes: int = RATELIMIT_MAX_BYTES,
        reset_window: int = RATELIMIT_WINDOW,
        clock: Callable[..., float] = time.monotonic,
    ) -> None:
        """Start a window of `reset_window` seconds by `clock`."""
        self.max_bytes = max_bytes
        self.reset_window = reset_window
        self._clock = clock
        self._deadline = clock() + reset_window
        self._stats: dict[_SourceLocation, _Stats] = {}
        self.suppression_active = False

    def consume(self, location: _SourceLocation, nbytes: int) -> RateLimitStatus:
        """Charge `nbytes` to `location` and answer its status."""
        stats = self._stats.setdefault(location, _Stats(self.max_bytes))
        status = (
            RateLimitStatus.STILL_SUPPRESSED
            if stats.dropped_bytes > 0
            else RateLimitStatus.UNSUPPRESSED
        )
        if not stats.consume(nbytes) and status is RateLimitStatus.UNSUPPRESSED:
            status = RateLimitStatus.NEWLY_SUPPRESSED
            self.suppression_active = True
        return status

    def advance(self) -> list[tuple[_SourceLocation, int]]:
        """Reset every budget once the window has ended.

        Answers each location that dropped bytes, with how many, as
        `Reset` logs them.
        """
        now = self._clock()
        if now < self._deadline:
            return []
        self._deadline += self.reset_window * (
            1 + int((now - self._deadline) // self.reset_window)
        )
        dropped = [
            (location, stats.dropped_bytes)
            for location, stats in self._stats.items()
            if stats.dropped_bytes
        ]
        self._stats = {}
        self.suppression_active = False
        return dropped


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

    @override
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        # `[*] ` ahead of the time, as `Logger::LogPrintStr_` puts it
        return f"[*] {line}" if getattr(record, "suppressing", False) else line


class Logger(logging.Logger):
    """A `logging.Logger` writing to `log_path`, or a stream if unset."""

    def __init__(
        self,
        log_path: str | Path | None = None,
        *,
        debug: bool = False,
        categories: Collection[str] = (),
        excluded: Collection[str] = (),
        rate_limit: bool = True,
    ) -> None:
        """Attach a file or stream handler, at `DEBUG` or `INFO` per `debug`.

        `categories` are the ones `log_debug` writes, every category
        where it is empty, but for `excluded`. A file is rate limited
        (`rate_limiter`) unless `rate_limit` is off, `-nologratelimit`;
        a stream is not, as Core limits the file alone and leaves the
        console be.
        """
        level = logging.DEBUG if debug else logging.INFO
        super().__init__(name="Logger", level=level)
        self._categories = frozenset(categories)
        self._excluded = frozenset(excluded)
        self.rate_limiter: LogRateLimiter | None = None
        self._limit_lock = threading.RLock()
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
            if rate_limit:
                self.rate_limiter = LogRateLimiter()
        else:
            handler = logging.StreamHandler()
        # `%(asctime)s` in the format string is what makes `format` set
        # `record.asctime` before `formatMessage` reads it
        formatter = _LevelFormatter("%(asctime)s %(message)s")
        handler.setFormatter(formatter)
        self._formatter = formatter
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
            self.log(
                logging.DEBUG, msg, *args, extra={"category": category}, stacklevel=2
            )

    @override
    def callHandlers(self, record: logging.LogRecord) -> None:
        """Write `record` unless its source location is over its budget.

        `Logger::LogPrintStr_`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag: the line's size is charged to its source location; the
        line that exhausts the budget is written, after a warning saying
        so; later ones from that location are not, until the window
        ends and a warning says how many bytes that dropped. A debug
        line is charged nothing, as Core's `should_ratelimit` is false
        below `Level::Info`.
        """
        limiter = self.rate_limiter
        if limiter is None:
            super().callHandlers(record)
            return
        with self._limit_lock:
            for location, dropped in limiter.advance():
                self._notice(
                    limiter,
                    record,
                    "Restarting logging from %s:%d (%s): %d bytes were dropped "
                    "during the last %ds.",
                    *_location_args(location),
                    dropped,
                    limiter.reset_window,
                )
            if record.levelno < logging.INFO:
                record.suppressing = limiter.suppression_active
                super().callHandlers(record)
                return
            location = (record.pathname, record.lineno, record.funcName)
            status = limiter.consume(location, self._size(record))
            if status is RateLimitStatus.NEWLY_SUPPRESSED:
                self._notice(
                    limiter,
                    record,
                    "Excessive logging detected from %s:%d (%s): >%d bytes "
                    "logged during the last time window of %ds. Suppressing "
                    "logging to disk from this source location until time "
                    "window resets. Console logging unaffected. Last log entry.",
                    *_location_args(location),
                    limiter.max_bytes,
                    limiter.reset_window,
                )
            if status is not RateLimitStatus.STILL_SUPPRESSED:
                record.suppressing = limiter.suppression_active
                super().callHandlers(record)

    def _size(self, record: logging.LogRecord) -> int:
        """Return the bytes `record` writes: its line and the newline after."""
        line = self._formatter.format(record)
        return len(line.encode("utf-8", errors="surrogateescape")) + 1

    def _notice(
        self,
        limiter: LogRateLimiter,
        near: logging.LogRecord,
        msg: str,
        *args: object,
    ) -> None:
        """Write a warning that is itself never limited, ahead of `near`."""
        record = self.makeRecord(
            self.name,
            logging.WARNING,
            near.pathname,
            near.lineno,
            msg,
            args,
            None,
            func=near.funcName,
            extra={"suppressing": limiter.suppression_active},
        )
        super().callHandlers(record)

    def close(self) -> None:
        """Close and detach every handler `__init__` attached."""
        self.rate_limiter = None
        for handler in self.handlers:
            handler.close()
            self.removeHandler(handler)


def _location_args(location: _SourceLocation) -> tuple[str, int, str]:
    """Return `location` with its file named from the package, as Core's are."""
    path, line, function = location
    marker = "btclib_node"
    index = path.rfind(marker)
    return (path[index:] if index >= 0 else path), line, function


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
    rate_limit: bool = True,
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
    arg:"/"Command-line arg:" lines. Without `rate_limit` it ends with
    `AppInitMain`'s "Log rate limiting disabled", which Core writes later,
    among lines this node has no counterpart for.
    """
    logger = Logger(
        log_path,
        debug=debug,
        categories=debug_categories,
        excluded=debug_exclude,
        rate_limit=rate_limit,
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
    if not rate_limit:
        logger.info("Log rate limiting disabled")
    return logger
