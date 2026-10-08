# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Logger` picks the right handler and drops it cleanly on `close`."""

import ast
import logging
import time
from pathlib import Path

import pytest

import btclib_node
from btclib_node import cli
from btclib_node.constants import default_data_dir
from btclib_node.log import Logger, LogRateLimiter, open_history_log


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
    # the time, one space, then `GetLogPrefix`'s level, as ISS 1280 and
    # ISS 1297 have every line
    _, message = line.split(b" ", 1)
    assert message == b"[warning] Invalid -rpccookieperms=o\xe9"


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
    # the five blank lines `Logger.__init__` opens the file on (#1309)
    # are not a record this test is about
    lines = path.read_text(encoding="utf-8").splitlines()[5:]
    messages = [line.split(" ", 1)[1] for line in lines]
    assert messages == ["[debug] d", "i", "[warning] w", "[error] e", "[error] c"]


def test_a_file_log_opens_on_five_blank_lines(tmp_path: Path) -> None:
    """ISS 1309: `StartLogging`'s five blank lines, absent from a stream.

    `src/logging.cpp:72`, at bitcoin/bitcoin@9be056a8a7: written to the
    file the moment it opens, marking one execution's record off from
    the last one's in a file appended across restarts.
    """
    path = tmp_path / "history.log"
    logger = Logger(path)
    logger.info("i")
    logger.close()
    assert path.read_text(encoding="utf-8").splitlines()[:5] == [""] * 5


def test_a_stream_log_opens_on_no_blank_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stream has no earlier execution to mark off from this one."""
    logger = Logger()
    logger.info("i")
    logger.close()
    (line,) = capsys.readouterr().err.splitlines()
    assert line.endswith(" i")


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


def _log_lines(path: Path) -> list[str]:
    """Return what a `Logger` wrote to `path`, each line after its time."""
    lines = path.read_text(encoding="utf-8").splitlines()[5:]
    return [line.split(" ", 1)[1] for line in lines]


def test_a_debug_line_carries_its_category_as_core_s_log_does(tmp_path: Path) -> None:
    """ISS 1322: `GetLogPrefix` writes `[net] ` for a debug line, no level.

    As `bitcoind` v31.1.0's `debug.log` has it under `-debug=net`:
    `[net] Flushed 0 banned node addresses/subnets to disk  0ms`.
    """
    path = tmp_path / "history.log"
    logger = Logger(path, debug=True)
    logger.log_debug("net", "peer=%d", 7)
    logger.close()
    assert _log_lines(path) == ["[net] peer=7"]


def test_log_debug_writes_the_categories_debug_selects_and_no_others(
    tmp_path: Path,
) -> None:
    """ISS 1322: `-debug=net` writes `net` and not `rpc`; none selects all."""
    path = tmp_path / "history.log"
    logger = Logger(path, debug=True, categories={"net"})
    logger.log_debug("net", "n")
    logger.log_debug("rpc", "r")
    logger.close()
    assert _log_lines(path) == ["[net] n"]
    every = tmp_path / "every.log"
    logger = Logger(every, debug=True)
    logger.log_debug("net", "n")
    logger.log_debug("rpc", "r")
    logger.close()
    assert _log_lines(every) == ["[net] n", "[rpc] r"]


def test_log_debug_writes_nothing_where_debug_is_off(tmp_path: Path) -> None:
    """A category alone is not `-debug`: the level is what keeps it out."""
    path = tmp_path / "history.log"
    logger = Logger(path, categories={"net"})
    logger.log_debug("net", "n")
    logger.close()
    assert not _log_lines(path)


def _debug_calls() -> dict[str, list[ast.Call]]:
    """Return each source file's `.debug(` and `.log_debug(` calls."""
    source = Path(btclib_node.__file__).parent
    calls: dict[str, list[ast.Call]] = {}
    for path in source.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"debug", "log_debug"}
            ):
                calls.setdefault(path.relative_to(source).as_posix(), []).append(node)
    return calls


def test_no_call_writes_a_debug_line_with_no_category() -> None:
    """ISS 1322: every debug line goes through `log_debug`.

    `logger.debug` writes `[debug] `, which Core writes for no line.
    """
    assert not [
        f"{name}:{call.lineno}"
        for name, calls in _debug_calls().items()
        for call in calls
        if isinstance(call.func, ast.Attribute) and call.func.attr == "debug"
    ]


# the Core subsystem each file's debug lines belong to: where Core has the
# same line, the category its `LogDebug` gives it
_CATEGORY_OF_FILE = {
    "chainstate/block_index.py": "validation",
    "download.py": "net",
    "fee_estimator.py": "estimatefee",
    "main.py": "validation",
    "p2p/banman.py": "net",
    "p2p/callbacks.py": "net",
    "p2p/compact_block.py": "net",
    "p2p/connection.py": "net",
    "p2p/main.py": "net",
    "p2p/manager.py": "net",
    "rpc/connection.py": "http",
    "rpc/main.py": "rpc",
    "rpc/manager.py": "http",
}


def test_every_debug_line_is_under_the_category_of_its_subsystem() -> None:
    """ISS 1322: `net` for a peer's, `validation` for a block's, `http`, `rpc`.

    A literal and one of Core's own names, so that `-debug=<category>`
    selects it.
    """
    found = {
        name: {
            call.args[0].value if isinstance(call.args[0], ast.Constant) else None
            for call in calls
        }
        for name, calls in _debug_calls().items()
    }
    assert found == {name: {category} for name, category in _CATEGORY_OF_FILE.items()}
    assert set(_CATEGORY_OF_FILE.values()) <= cli._LOG_CATEGORIES


def test_the_data_directory_and_config_file_lines_follow_the_version_line(
    tmp_path: Path,
) -> None:
    """ISS 1444: `StartLogging` writes them, ahead of `LogArgs`'s lines."""
    path = tmp_path / "history.log"
    logger = open_history_log(
        path,
        debug=False,
        data_dir=tmp_path / "regtest",
        config_file_line="Config file: <disabled>",
        config_args=("Command-line arg: regtest=true",),
    )
    logger.close()
    assert _log_lines(path)[1:] == [
        f"Default data directory {default_data_dir()}",
        f"Using data directory {tmp_path / 'regtest'}",
        "Config file: <disabled>",
        "Command-line arg: regtest=true",
    ]


def test_log_debug_writes_no_excluded_category(tmp_path: Path) -> None:
    """ISS 1609: `-debug=1 -debugexclude=net` writes every other category."""
    path = tmp_path / "history.log"
    logger = Logger(path, debug=True, excluded={"net"})
    logger.log_debug("net", "n")
    logger.log_debug("rpc", "r")
    logger.close()
    assert _log_lines(path) == ["[rpc] r"]


def test_an_excluded_category_is_not_written_though_debug_names_it(
    tmp_path: Path,
) -> None:
    """ISS 1609: `-debugexclude` takes priority over `-debug`."""
    path = tmp_path / "history.log"
    logger = Logger(path, debug=True, categories={"net", "rpc"}, excluded={"net"})
    logger.log_debug("net", "n")
    logger.log_debug("rpc", "r")
    logger.close()
    assert _log_lines(path) == ["[rpc] r"]


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _noisy(logger: Logger, text: str) -> None:
    """Log `text` from one source location, whatever the caller's own."""
    logger.info(text)


def _whole_lines(path: Path) -> list[str]:
    """Return what a `Logger` wrote to `path`, each line whole."""
    return path.read_text(encoding="utf-8").splitlines()[5:]


def test_a_source_location_past_its_budget_is_silenced_until_the_window_ends(
    tmp_path: Path,
) -> None:
    """Core's `LogRateLimiter`: warn once, stay silent, then warn and resume.

    A line from another source location is written throughout, and every
    line written while one is silenced starts with `[*] `.
    """
    path = tmp_path / "history.log"
    clock = _Clock()
    logger = Logger(path)
    # each line is 52 bytes: the 20-byte time, a space, 30 characters and
    # the newline; the budget is two lines short of three
    logger.rate_limiter = LogRateLimiter(100, 60, clock)
    for i in range(5):
        _noisy(logger, str(i) * 30)
    logger.info("elsewhere")
    clock.now += 61
    _noisy(logger, "after")
    logger.close()
    first, notice, second, elsewhere, restart, after = _whole_lines(path)
    assert first.endswith(" " + "0" * 30)
    assert not first.startswith("[*]")
    assert notice.startswith("[*] ")
    assert "[warning] Excessive logging detected from " in notice
    assert "(_noisy): >100 bytes logged during the last time window of 60s." in notice
    assert notice.endswith("Last log entry.")
    assert second.startswith("[*] ")
    assert second.endswith(" " + "1" * 30)
    assert elsewhere.startswith("[*] ")
    assert elsewhere.endswith(" elsewhere")
    # the line that went over the budget and the three after it
    assert restart.endswith("(_noisy): 208 bytes were dropped during the last 60s.")
    assert "[warning] Restarting logging from " in restart
    assert not restart.startswith("[*]")
    assert after.endswith(" after")
    assert not after.startswith("[*]")


def test_a_second_window_limits_again(tmp_path: Path) -> None:
    """The budget is a window's own: each window starts with it whole."""
    path = tmp_path / "history.log"
    clock = _Clock()
    logger = Logger(path)
    logger.rate_limiter = LogRateLimiter(100, 60, clock)
    for _ in range(3):
        for i in range(4):
            _noisy(logger, str(i) * 30)
        clock.now += 60
    logger.close()
    written = [line for line in _whole_lines(path) if "[warning]" not in line]
    # two lines written per window, the third and fourth dropped
    assert len(written) == 3 * 2


def test_a_stream_is_not_rate_limited() -> None:
    """Core limits what goes to the file and leaves the console be."""
    logger = Logger()
    assert logger.rate_limiter is None
    logger.close()


def test_no_rate_limit_leaves_a_file_unlimited(tmp_path: Path) -> None:
    """`-nologratelimit`: a `Logger` built without it has no limiter."""
    logger = Logger(tmp_path / "history.log", rate_limit=False)
    assert logger.rate_limiter is None
    logger.close()


def test_a_file_is_rate_limited_with_core_s_budget(tmp_path: Path) -> None:
    """`RATELIMIT_MAX_BYTES` a `RATELIMIT_WINDOW`, `-logratelimit`'s default."""
    logger = Logger(tmp_path / "history.log")
    limiter = logger.rate_limiter
    assert limiter is not None
    assert (limiter.max_bytes, limiter.reset_window) == (1024 * 1024, 3600)
    logger.close()


def _debug_noisy(logger: Logger, text: str) -> None:
    """Log `text` as two debug lines, from one source location each."""
    logger.log_debug("net", text)
    logger.debug(text)


def test_a_debug_line_is_not_limited_while_info_is(tmp_path: Path) -> None:
    """Core's `LogDebug` passes no rate limit; `LogInfo` does.

    A debug line written while a location is silenced still starts with
    `[*] `, as every line Core writes then does.
    """
    path = tmp_path / "history.log"
    logger = Logger(path, debug=True)
    logger.rate_limiter = LogRateLimiter(100, 60, _Clock())
    for i in range(5):
        _debug_noisy(logger, str(i) * 30)
        _noisy(logger, str(i) * 30)
    logger.close()
    lines = _whole_lines(path)
    (notice,) = [line for line in lines if "Excessive" in line]
    assert "(_noisy)" in notice
    debug = [line for line in lines if "[net]" in line or "[debug]" in line]
    assert len(debug) == 2 * 5
    assert debug[-1].startswith("[*] ")
    info = [line for line in lines if line not in debug and line != notice]
    assert len(info) == 2


def test_rate_limiting_disabled_is_logged(tmp_path: Path) -> None:
    """`-nologratelimit` logs "Log rate limiting disabled", as `AppInitMain`."""
    path = tmp_path / "history.log"
    logger = open_history_log(path, debug=False, data_dir=tmp_path, rate_limit=False)
    logger.close()
    assert _log_lines(path)[-1] == "Log rate limiting disabled"
