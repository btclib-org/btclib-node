# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Running an operator's `-*notify` command, and the warnings that trigger one.

Core's `runCommand` (`src/common/system.cpp`, at bitcoin/bitcoin@9be056a8a7,
the v31.1 tag) is `run_command` below: every notify command runs through
the shell, as Core's own `::system()` does, and a nonzero exit is a
warning in the log and nowhere else. `-blocknotify`, `-startupnotify`
and `-alertnotify` each fire it from a thread nothing waits for --
Core's own `std::thread(runCommand, cmd); t.detach(); // thread runs
free` (`src/init.cpp` and `src/node/kernel_notifications.cpp`, same
citation) -- which `run_detached` below is. `-shutdownnotify` is the
one shape that waits: Core's `ShutdownNotify` (`src/init.cpp`, same
citation) starts one thread per configured command and joins every one
before returning, so shutdown does not go on to `InterruptHTTPServer`
and the rest until every command it named has finished --
`run_shutdown_notify` below.

`Warnings` is Core's `node::Warnings` (`src/node/warnings.h`/`.cpp`,
same citation), the record of which warning this node currently raises;
its own docstring says which of Core's warnings this tree tracks and
why it is only the one.
"""

import subprocess
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging
    from collections.abc import Sequence

__all__ = [
    "Warnings",
    "alert_notify",
    "run_command",
    "run_detached",
    "run_shutdown_notify",
]


def run_command(logger: logging.Logger, command: str) -> None:
    """Run `command` through the shell, and warn (only) on a nonzero exit.

    Core's `runCommand` (same citation as the module docstring):
    `subprocess.run`'s own `shell=True` is `::system()`, and the
    `returncode` it answers is the same `nErr` that call's own
    `LogWarning` names. `command` empty is `runCommand`'s own
    `if (strCommand.empty()) return;`.
    """
    if not command:
        return
    result = subprocess.run(command, shell=True, check=False)  # noqa: S602
    if result.returncode:
        logger.warning(
            "runCommand error: system(%s) returned %d", command, result.returncode
        )


def run_detached(logger: logging.Logger, command: str) -> None:
    """Run `command` on a thread nothing waits for.

    Core's own `std::thread(runCommand, cmd); t.detach();` (same
    citation): `-blocknotify`, `-startupnotify` and `-alertnotify` each
    fire this way. A daemon thread is this interpreter's own "runs
    free" -- it does not keep the process open the way a thread
    `Node.stop` had to join would.
    """
    if not command:
        return
    threading.Thread(target=run_command, args=(logger, command), daemon=True).start()


def run_shutdown_notify(logger: logging.Logger, commands: Sequence[str]) -> None:
    """Run every `-shutdownnotify` command, on its own thread, then join all.

    Core's `ShutdownNotify` (same citation): `-shutdownnotify` may be
    given more than once, unlike the other three notify options, and
    every command it names gets its own thread, all of them joined
    before this call returns -- `-shutdownnotify`'s own help text is
    where Core warns that this can delay shutdown, not a claim this
    module makes on its own.
    """
    threads = [
        threading.Thread(target=run_command, args=(logger, command))
        for command in commands
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


# Core's `SAFE_CHARS_DEFAULT` (`src/util/strencodings.cpp`, same
# citation): ASCII letters and digits, plus " .,;-_/:?@()". `AlertNotify`
# calls `SanitizeString` with no explicit rule, which is this one.
_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,;-_/:?@()"
)


def _sanitize(message: str) -> str:
    """Return `message` with every character outside `_SAFE_CHARS` dropped.

    Core's `SanitizeString` (`src/util/strencodings.cpp`, same citation).
    """
    return "".join(c for c in message if c in _SAFE_CHARS)


def alert_notify(logger: logging.Logger, command: str, message: str) -> None:
    """Run `-alertnotify`'s `command`, `%s` replaced by `message`.

    Core's `AlertNotify` (`src/node/kernel_notifications.cpp`, same
    citation): `message` is sanitized and wrapped in single quotes
    before it replaces `%s`, "to be safe", in Core's own words, rather
    than because this tree's own callers ever pass anything untrusted --
    `main.check_fork_warning_conditions` is the only caller so far, and
    its message is this node's own fixed text. Matching Core's own
    unconditional quoting costs nothing here and is what this line
    does instead of relying on that.
    """
    if not command:
        return
    safe = "'" + _sanitize(message) + "'"
    run_detached(logger, command.replace("%s", safe))


class Warnings:
    """The node-wide warnings this tree raises, Core's `node::Warnings`.

    (`src/node/warnings.h`/`.cpp`, same citation as the module
    docstring.) Tracks one warning id, `"large_work_invalid_chain"` --
    `main.py`'s `check_fork_warning_conditions`, for
    btclib-org/btclib-node#1522 -- because it is the only one this tree
    raises of its own. Core's other kernel warning,
    `UNKNOWN_NEW_RULES_ACTIVATED`, needs the per-bit BIP9 threshold-state
    cache `VersionBitsCache::CheckUnknownActivations`
    (`src/versionbits.cpp`, same citation) keeps over every deployment
    period; this tree tracks no version-bits deployment state at all
    (`main.py`'s own comment on `get_flags`), so that warning is not
    raised here -- btclib-org/btclib-node#1475 is where that gap is
    recorded, left open rather than closed by the branch that added this
    class. None of Core's `node::Warning` members (`CLOCK_OUT_OF_SYNC`,
    `PRE_RELEASE_TEST_BUILD`, `FATAL_INTERNAL_ERROR`) has a counterpart
    here either, this tree raising none of them.

    `set_warning`/`unset_warning`/`get_messages` are Core's own
    `Set`/`Unset`/`GetMessages`. `set_warning`'s own dedup -- an id
    already active is not set again -- is what a caller reads to decide
    whether to fire `-alertnotify` again for a condition that was
    already true, Core's own `Warnings::Set`'s `bool inserted` doing the
    same work for `KernelNotifications::warningSet`.
    """

    def __init__(self) -> None:
        """Start with no warning set."""
        self._messages: dict[str, str] = {}

    def set_warning(self, warning_id: str, message: str) -> bool:
        """Set `warning_id`'s message; answer whether it was not already set."""
        if warning_id in self._messages:
            return False
        self._messages[warning_id] = message
        return True

    def unset_warning(self, warning_id: str) -> bool:
        """Unset `warning_id`; answer whether it had been set."""
        return self._messages.pop(warning_id, None) is not None

    def get_messages(self) -> list[str]:
        """Return every active warning's message, Core's own `GetMessages`."""
        return list(self._messages.values())
