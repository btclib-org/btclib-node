# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Logger` picks the right handler and drops it cleanly on `close`."""

import logging
from typing import TYPE_CHECKING

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


def test_a_byte_utf8_refuses_is_logged_as_that_byte(tmp_path: Path) -> None:
    """ISS 1290: a setting's byte reaches the file as Core writes it.

    The lone surrogate `surrogateescape` read `0xe9` into, written back
    as `0xe9`, where UTF-8 alone would refuse to write it.
    """
    path = tmp_path / "history.log"
    logger = Logger(path)
    logger.warning("Invalid -rpccookieperms=o\udce9")
    logger.close()
    # the line's own end left out: a text-mode file ends it in `\r\n` on
    # Windows, which is not what this test is about
    line = path.read_bytes().rstrip(b"\r\n")
    assert line.endswith(b"Invalid -rpccookieperms=o\xe9")


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
