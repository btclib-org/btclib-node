# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The node's wall clock, which `setmocktime` can hold at a chosen second.

Core's `NodeClock` (`src/util/time.cpp`, at bitcoin/bitcoin@9be056a8a7,
the v31.1 tag): the system time, unless a non-zero mock time is set, and
then that second. Every read of the node's clock in this package goes
through `now` or `now_utc`, so a mock time moves them all together.

What stays on the system is what Core keeps off `NodeClock`: durations
measured with `time.monotonic()` (Core's `SteadyClock`), the sleeps, and
the cadence of what Core runs on its scheduler or in a sleeping thread,
which a held or set-back mock time must not stop. Such a cadence gate
reads `time.monotonic()` and hands `now` to what runs behind it. Also
off the clock are the age of a file measured against its own
modification time and the timestamps the logging module writes.

The mock time is one `int` rebound by `set_mock_time` on `Node`'s thread
and read from any thread, as Core's `std::atomic` is; a read sees the
old value or the new one and nothing between.
"""

import time
from datetime import UTC, datetime

__all__ = ["MAX_MOCK_TIME", "mock_time", "now", "now_utc", "set_mock_time"]

# `std::chrono::nanoseconds::max()` in seconds: the bound `setmocktime`
# takes at the v31.1 tag (`src/rpc/node.cpp`), where master takes
# `uint32_t`'s maximum.
MAX_MOCK_TIME = (2**63 - 1) // 10**9

_mock_time: int = 0


def set_mock_time(seconds: int) -> None:
    """Hold the clock at `seconds` after the epoch, or release it with 0."""
    global _mock_time  # noqa: PLW0603
    _mock_time = seconds


def mock_time() -> int:
    """Return the second the clock is held at, 0 where it is not."""
    return _mock_time


def now() -> float:
    """Return seconds since the epoch, the mock time where one is set."""
    mocked = _mock_time
    return float(mocked) if mocked else time.time()


def now_utc() -> datetime:
    """Return `now` as an aware UTC datetime."""
    mocked = _mock_time
    return datetime.fromtimestamp(mocked, UTC) if mocked else datetime.now(UTC)
