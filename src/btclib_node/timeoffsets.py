# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The median clock offset of the peers this node dialled, and its warning.

Core's `TimeOffsets` (`src/node/timeoffsets.cpp`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag) over
`Node.outbound_time_offsets`. `median` is `TimeOffsets::Median`, which
`getnetworkinfo` answers as `timeoffset`. `warn_if_out_of_sync` is
`TimeOffsets::WarnIfOutOfSync`, which `p2p.callbacks.version` runs after
each sample it adds, as Core's `ProcessMessage` does
(`src/net_processing.cpp`, same sha).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

    from btclib_node import Node

__all__ = ["median", "warn_if_out_of_sync"]

# `TimeOffsets::Median` answers 0 under this many samples
_MIN_SAMPLES = 5

# `WARN_THRESHOLD{10}`, in minutes (`src/node/timeoffsets.h`, same sha)
_WARN_THRESHOLD_MINUTES = 10

# the `node.warnings` id of Core's `node::Warning::CLOCK_OUT_OF_SYNC`
_WARNING_ID = "clock_out_of_sync"

# Core's message, untranslated, as its log and its RPCs carry it
_OUT_OF_SYNC_MESSAGE = (
    "Your computer's date and time appear to be more than "
    f"{_WARN_THRESHOLD_MINUTES} minutes out of sync with the network, "
    "this may lead to consensus failure. After you've confirmed your "
    "computer's clock, this message should no longer appear when you "
    "restart your node. Without a restart, it should stop showing "
    "automatically after you've connected to a sufficient number of new "
    "outbound peers, which may take some time. You can inspect the "
    "`timeoffset` field of the `getpeerinfo` and `getnetworkinfo` RPC "
    "methods to get more info."
)


def median(offsets: Collection[int]) -> int:
    """Return 0 under five samples, else the one at half their count, sorted.

    For an even count that is the upper of the two middle samples, as
    Core's "approximate median" is.
    """
    if len(offsets) < _MIN_SAMPLES:
        return 0
    return sorted(offsets)[len(offsets) // 2]


def warn_if_out_of_sync(node: Node) -> bool:
    """Set the warning where the median is beyond the threshold, else unset it.

    Answer whether it is set. Out of sync, the message is logged as a
    warning on every call, as Core's `LogWarning` there is, and set in
    `node.warnings`, which `getblockchaininfo` and `getnetworkinfo` answer.
    Core's `node::Warnings::Set` runs no `-alertnotify`, so this does not
    either.

    Core clamps the median before `std::chrono::abs`, which is undefined
    at the minimum of `int64_t`. A Python `int` has no minimum, so `abs`
    takes it as it is.
    """
    if abs(median(node.outbound_time_offsets)) <= _WARN_THRESHOLD_MINUTES * 60:
        node.warnings.unset_warning(_WARNING_ID)
        return False
    node.logger.warning(_OUT_OF_SYNC_MESSAGE)
    node.warnings.set_warning(_WARNING_ID, _OUT_OF_SYNC_MESSAGE)
    return True
