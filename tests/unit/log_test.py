# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Logger` picks the right handler and drops it cleanly on `close`."""

import logging
import time
from typing import TYPE_CHECKING

import pytest

from btclib_node.log import Logger

if TYPE_CHECKING:
    from pathlib import Path


def test_a_log_path_is_a_file_the_lines_end_up_in(tmp_path: Path) -> None:
    """A `Logger` given a path attaches a `FileHandler`, and writes reach it."""
    # the destination is the whole of what the branch in Logger decides,
    # and nothing said so: a node configured with a log path and logging
    # to the terminal instead loses the record it was asked to keep
    path = tmp_path / "history.log"
    logger = Logger(path, debug=True)
    (handler,) = logger.handlers
    assert isinstance(handler, logging.FileHandler)
    logger.info("a line")
    logger.close()
    assert "a line" in path.read_text(encoding="utf-8")


def test_no_log_path_is_the_stream_and_not_a_file() -> None:
    """A `Logger` built with no path attaches a `StreamHandler`, not a file."""
    logger = Logger(debug=True)
    (handler,) = logger.handlers
    # FileHandler is a StreamHandler, so the second half is what says
    # which of the two this is
    assert isinstance(handler, logging.StreamHandler)
    assert not isinstance(handler, logging.FileHandler)
    logger.close()


def test_closing_leaves_no_handler_a_late_record_could_reach() -> None:
    """`Logger.close` detaches every handler `__init__` attached."""
    logger = Logger(debug=True)
    logger.close()
    assert not logger.handlers


def test_a_line_carries_its_level_as_core_s_log_does(tmp_path: Path) -> None:
    """ISS 1280: `GetLogPrefix`'s `[warning] ` and `[error] `, none at info.

    As `bitcoind` v31.1.0's `debug.log` has them for a line with no
    category: `[error] Unable to start HTTP server. See debug log for
    details.` over a taken RPC port. `debug` is `[debug] `, and
    `critical`, which Core has no level for, is `[error] `.
    """
    path = tmp_path / "history.log"
    logger = Logger(path, debug=True)
    logger.debug("d")
    logger.info("i")
    logger.warning("w")
    logger.error("e")
    logger.critical("c")
    logger.close()
    messages = [
        line.split(" ", 1)[1] for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert messages == ["[debug] d", "i", "[warning] w", "[error] e", "[error] c"]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="no time.tzset on Windows")
def test_a_line_opens_with_core_s_utc_second_and_a_space(tmp_path: Path) -> None:
    """ISS 1297: `LogTimestampStr`, as `bitcoind` v31.1.0's `debug.log` has it.

    `2026-09-26T09:42:51Z [error] Unable to start HTTP server. See debug
    log for details.`: ISO 8601 in UTC, whatever the machine's zone, the
    fraction of the second dropped, then one space. The zone is pinned
    to one five and a half hours off UTC with no daylight saving, so a
    local time cannot pass for UTC on a machine that runs in UTC; and
    reset once the variable is, for the tests the same worker runs next.
    """
    logger = Logger(tmp_path / "history.log")
    (handler,) = logger.handlers
    record = logging.makeLogRecord(
        {"msg": "a line", "levelno": logging.ERROR, "created": 86399.9}
    )
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("TZ", "Asia/Kolkata")
            time.tzset()
            assert time.localtime(0).tm_gmtoff == 5 * 3600 + 30 * 60
            assert handler.format(record) == "1970-01-01T23:59:59Z [error] a line"
    finally:
        time.tzset()
        logger.close()
