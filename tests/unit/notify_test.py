# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`notify.py`: running a `-*notify` command, and `Warnings`'s own dedup."""

import logging
import threading
from typing import TYPE_CHECKING, Any, override

from btclib_node.log import Logger
from btclib_node.notify import (
    Warnings,
    alert_notify,
    run_command,
    run_detached,
    run_shutdown_notify,
)
from tests import wait_until

if TYPE_CHECKING:
    from pathlib import Path


class _RecordingHandler(logging.Handler):
    """A handler that keeps every record it is given, for a test to read.

    `caplog` sees nothing `Logger` emits, it never being looked up
    through `logging.getLogger` (`log.py`'s own module docstring), so a
    test that has to observe one attaches one of these directly instead
    -- the same class `init_test.py` defines for the same reason.
    """

    def __init__(self) -> None:
        """Start with no records."""
        super().__init__()
        self.records: list[logging.LogRecord] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        """Keep `record`."""
        self.records.append(record)


def test_run_command_does_nothing_for_an_empty_command() -> None:
    """Core's own `if (strCommand.empty()) return;` (`runCommand`)."""
    logger = Logger()
    handler = _RecordingHandler()
    logger.addHandler(handler)
    run_command(logger, "")
    assert handler.records == []


def test_run_command_is_silent_on_a_successful_exit() -> None:
    """A zero exit logs nothing, only a nonzero one does."""
    logger = Logger()
    handler = _RecordingHandler()
    logger.addHandler(handler)
    run_command(logger, "true")
    assert handler.records == []


def test_run_command_warns_cores_own_way_on_a_nonzero_exit() -> None:
    """`runCommand error: system(%s) returned %d`, Core's own `LogWarning`."""
    logger = Logger()
    handler = _RecordingHandler()
    logger.addHandler(handler)
    run_command(logger, "exit 3")
    (record,) = handler.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "runCommand error: system(exit 3) returned 3"


def test_run_command_runs_through_the_shell(tmp_path: Path) -> None:
    """`shell=True` is Core's own `::system()`.

    A pipe reaches a second command. No space sits before `|` or `>`:
    cmd.exe's own `echo` keeps a space that directly precedes either as
    part of what it echoes, where a POSIX shell discards it either way
    -- asserting the platform's own difference is the point of the test
    below, not of this one, so this command carries none for either
    shell to keep.
    """
    marker = tmp_path / "marker"
    run_command(Logger(), f"echo hi|cat>{marker}")
    assert marker.read_text() == "hi\n"


def test_run_detached_runs_the_command_on_a_thread_nothing_waits_for(
    tmp_path: Path,
) -> None:
    """The command still runs, off whatever thread called `run_detached`."""
    marker = tmp_path / "marker"
    run_detached(Logger(), f"touch {marker}")
    wait_until(marker.exists)


def test_run_detached_does_nothing_for_an_empty_command() -> None:
    """No thread is started for an empty command."""
    before = threading.active_count()
    run_detached(Logger(), "")
    assert threading.active_count() == before


def test_run_shutdown_notify_returns_only_once_every_command_has_finished(
    tmp_path: Path,
) -> None:
    """Core's own `ShutdownNotify`: joined before this call returns.

    If it returned early, the slower command's marker would not exist
    yet -- there being nothing else in this test to wait for it.
    """
    fast = tmp_path / "fast"
    slow = tmp_path / "slow"
    run_shutdown_notify(Logger(), (f"touch {fast}", f"sleep 0.3 && touch {slow}"))
    assert fast.exists()
    assert slow.exists()


def test_run_shutdown_notify_starts_one_thread_per_command(
    monkeypatch: Any,
) -> None:
    """Core's own one-`std::thread`-per-command, not one thread for all."""
    started: list[str] = []

    class _Recording(threading.Thread):
        def __init__(self, *, target: Any, args: Any, **kwargs: Any) -> None:
            started.append(args[1])
            super().__init__(target=target, args=args, **kwargs)

    monkeypatch.setattr(threading, "Thread", _Recording)
    run_shutdown_notify(Logger(), ("true", "true", "true"))
    assert started == ["true", "true", "true"]


def test_alert_notify_does_nothing_for_an_empty_command() -> None:
    """No thread is started for an empty command."""
    before = threading.active_count()
    alert_notify(Logger(), "", "a message")
    assert threading.active_count() == before


def test_alert_notify_substitutes_percent_s_with_the_quoted_message(
    tmp_path: Path,
) -> None:
    """`%s` in the command becomes the message, single-quoted.

    No space sits before `>`, here and in the two tests below that
    share this same command shape: cmd.exe's own `echo` keeps a space
    that directly precedes a redirection operator as part of what it
    echoes -- a trailing space before the newline the content otherwise
    ends on -- where a POSIX shell discards it either way, so asserting
    one fixed answer across platforms needs the command to carry no
    such space for either shell to treat differently.
    """
    marker = tmp_path / "marker"
    alert_notify(Logger(), f"echo %s>{marker}", "hello")
    wait_until(marker.exists)
    assert marker.read_text() == "hello\n"


def test_alert_notify_drops_a_single_quote_before_wrapping_the_message(
    tmp_path: Path,
) -> None:
    """A `'` in the message cannot break out of the wrapping Core adds.

    `_sanitize` drops every `'` before `alert_notify` wraps the message
    in one pair of its own -- if it did not, this message's semicolons
    would end the `echo` early and run `touch <injected>` for real
    rather than being printed as inert text inside the quotes.
    """
    marker = tmp_path / "marker"
    injected = tmp_path / "injected"
    message = f"x'; touch {injected}; echo 'y"
    alert_notify(Logger(), f"echo %s>{marker}", message)
    wait_until(marker.exists)
    assert not injected.exists()
    assert marker.read_text() == f"x; touch {injected}; echo y\n"


def test_alert_notify_drops_a_character_outside_cores_safe_set(
    tmp_path: Path,
) -> None:
    """`$` and `` ` ``, among `SanitizeString`'s own excluded characters."""
    marker = tmp_path / "marker"
    alert_notify(Logger(), f"echo %s>{marker}", "a$b`c")
    wait_until(marker.exists)
    assert marker.read_text() == "abc\n"


def test_set_warning_answers_whether_it_was_not_already_set() -> None:
    """Core's own `Warnings::Set`'s `bool inserted`."""
    warnings = Warnings()
    assert warnings.set_warning("w", "message one") is True
    assert warnings.set_warning("w", "message two") is False
    assert warnings.get_messages() == ["message one"]


def test_unset_warning_answers_whether_it_had_been_set() -> None:
    """Core's own `Warnings::Unset`'s `bool success`."""
    warnings = Warnings()
    assert warnings.unset_warning("w") is False
    warnings.set_warning("w", "message")
    assert warnings.unset_warning("w") is True
    assert warnings.get_messages() == []
