# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import logging
from pathlib import Path

from btclib_node.log import Logger


def test_a_log_path_is_a_file_the_lines_end_up_in(tmp_path: Path) -> None:
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
    logger = Logger(debug=True)
    (handler,) = logger.handlers
    # FileHandler is a StreamHandler, so the second half is what says
    # which of the two this is
    assert isinstance(handler, logging.StreamHandler)
    assert not isinstance(handler, logging.FileHandler)
    logger.close()


def test_closing_leaves_no_handler_a_late_record_could_reach() -> None:
    logger = Logger(debug=True)
    logger.close()
    assert not logger.handlers
