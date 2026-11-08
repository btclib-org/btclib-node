# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.timeoffsets`, Core's `TimeOffsets`."""

from types import SimpleNamespace
from typing import Any

import pytest

from btclib_node.notify import Warnings
from btclib_node.timeoffsets import warn_if_out_of_sync

# what bitcoind, at bitcoin/bitcoin@f0de87f040 (the v32.1 tag), answered
# in `getnetworkinfo`'s and `getblockchaininfo`'s `warnings`, and logged,
# on regtest after five peers it dialled sent a `version` an hour ahead
# of its clock
_CORE_MESSAGE = (
    "Your computer's date and time appear to be more than 10 minutes out of "
    "sync with the network, this may lead to consensus failure. After you've "
    "confirmed your computer's clock, this message should no longer appear "
    "when you restart your node. Without a restart, it should stop showing "
    "automatically after you've connected to a sufficient number of new "
    "outbound peers, which may take some time. You can inspect the "
    "`timeoffset` field of the `getpeerinfo` and `getnetworkinfo` RPC methods "
    "to get more info."
)


def a_node(offsets: list[int]) -> Any:
    """Build a node double with the samples, an empty `Warnings` and a log."""
    logged: list[str] = []
    return SimpleNamespace(
        outbound_time_offsets=offsets,
        warnings=Warnings(),
        logger=SimpleNamespace(warning=logged.append),
        logged=logged,
    )


@pytest.mark.parametrize(
    ("offsets", "raised"),
    [
        # Core's own `timeoffsets_warning` cases
        # (`src/test/timeoffsets_tests.cpp`, same sha)
        ([-3600, -2400, -1800, 0, 600], True),
        ([660] * 5, True),
        ([3600] * 4, False),
        ([180] * 100, False),
        # either side of `WARN_THRESHOLD`, which a median equal to it is within
        ([600] * 5, False),
        ([-600] * 5, False),
        ([601] * 5, True),
        ([-601] * 5, True),
    ],
)
def test_the_warning_is_raised_where_core_raises_it(
    offsets: list[int], *, raised: bool
) -> None:
    """ISS 2282: beyond ten minutes of median, the warning is set and logged."""
    node = a_node(offsets)
    assert warn_if_out_of_sync(node) is raised
    assert node.warnings.get_messages() == ([_CORE_MESSAGE] if raised else [])
    assert node.logged == ([_CORE_MESSAGE] if raised else [])


def test_the_warning_is_logged_each_time_and_set_once() -> None:
    """ISS 2282: as bitcoind did, a log line per out-of-sync sample."""
    node = a_node([3600] * 5)
    warn_if_out_of_sync(node)
    warn_if_out_of_sync(node)
    assert node.logged == [_CORE_MESSAGE, _CORE_MESSAGE]
    assert node.warnings.get_messages() == [_CORE_MESSAGE]


def test_the_warning_is_unset_once_the_median_comes_back() -> None:
    """ISS 2282: as on bitcoind, five samples an hour ahead, then six at 0.

    The warning stays while the median is an hour, and goes, unlogged,
    at the sixth sample in sync.
    """
    node = a_node([3600] * 5)
    assert warn_if_out_of_sync(node)
    for _ in range(5):
        node.outbound_time_offsets.append(0)
        assert warn_if_out_of_sync(node)
    node.outbound_time_offsets.append(0)
    assert not warn_if_out_of_sync(node)
    assert not node.warnings.get_messages()
    assert len(node.logged) == 6
