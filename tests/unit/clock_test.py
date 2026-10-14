# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The wall clock `setmocktime` holds, and the system clock it releases."""

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from btclib_node import clock

if TYPE_CHECKING:
    import pytest


def test_the_clock_is_the_system_time_until_a_mock_time_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no mock time, `now` and `now_utc` read the system at each call."""
    monkeypatch.setattr(time, "time", lambda: 1234.5)
    assert clock.mock_time() == 0
    assert clock.now() == 1234.5
    before = datetime.now(UTC)
    assert before <= clock.now_utc() <= datetime.now(UTC)


def test_a_mock_time_holds_both_readings_still() -> None:
    """Every read of a mocked clock is the same second, however long after."""
    clock.set_mock_time(1_600_000_000)
    assert clock.mock_time() == 1_600_000_000
    assert clock.now() == 1_600_000_000.0
    time.sleep(0.01)
    assert clock.now() == 1_600_000_000.0
    assert clock.now_utc() == datetime.fromtimestamp(1_600_000_000, UTC)


def test_zero_releases_the_clock_to_the_system_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero goes back to using the system time, as Core's help says."""
    clock.set_mock_time(1_600_000_000)
    clock.set_mock_time(0)
    monkeypatch.setattr(time, "time", lambda: 42.0)
    assert clock.mock_time() == 0
    assert clock.now() == 42.0


def test_the_mock_time_bound_is_the_one_bitcoind_v31_1_0_answers() -> None:
    """Core v31.1.0 refuses `setmocktime 9223372037`, past 9223372036."""
    assert clock.MAX_MOCK_TIME == 9223372036
