# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What the chain before a header requires of that header.

Bitcoin Core's `ContextualCheckBlockHeader` and the two answers it asks
for: `GetNextWorkRequired` in `pow.cpp` for the target, and
`CBlockIndex::GetMedianTimePast` in `chain.h` for the timestamp.
`BlockHeader.assert_valid_pow` answers the other half of the
proof-of-work question -- whether the hash meets the target the header
itself claims -- and needs no chain to do it, which is why one header
carrying a target no chain hands out passes it.

The chain is reached through a callable that steps back one header,
rather than through the index: a batch off the wire is checked before
any of it is indexed, so its own members are what the header after them
is checked against.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from btclib.block import BlockHeader
from btclib.block.proof_of_work import (
    DIFFICULTY_ADJUSTMENT_INTERVAL,
    POW_TARGET_SPACING,
    next_bits,
    retarget_first_height,
)
from btclib.exceptions import BTClibValueError

if TYPE_CHECKING:
    from datetime import datetime

    from btclib_node.chains import Chain

__all__ = [
    "MEDIAN_TIME_SPAN",
    "ParentOf",
    "assert_valid_in_context",
    "block_time",
    "header_at_height",
    "median_time_past",
    "next_bits_required",
]

# Core's CBlockIndex::nMedianTimeSpan
MEDIAN_TIME_SPAN = 11

# a header, and the one before it: `KeyError` where there is none, since
# every walk below stops at a height it computed rather than at the end
ParentOf = Callable[[BlockHeader], BlockHeader]


def block_time(header: BlockHeader) -> int:
    """Return the second the header's four timestamp bytes hold.

    Core's `CBlockHeader::GetBlockTime`. `BlockHeader.serialize` writes
    `int(time.timestamp())`, so that is the value the rules here
    compare: a header is weighed as it goes on the wire and not as it
    was built.
    """
    return int(header.time.timestamp())


def median_time_past(header: BlockHeader, height: int, parent_of: ParentOf) -> int:
    """Return the median timestamp of a header and its ten ancestors.

    Core's `CBlockIndex::GetMedianTimePast`, whose window is however
    many of the eleven exist: nearer the genesis than that it is the
    whole chain, and the middle of an even number of times is the later
    of the two middle ones.
    """
    times = [block_time(header)]
    for _ in range(min(MEDIAN_TIME_SPAN, height + 1) - 1):
        header = parent_of(header)
        times.append(block_time(header))
    times.sort()
    return times[len(times) // 2]


def header_at_height(
    header: BlockHeader, height: int, target_height: int, parent_of: ParentOf
) -> BlockHeader:
    """Walk back from `header`, at `height`, to its ancestor at `target_height`.

    Core's `CBlockIndex::GetAncestor` over a skip list; this walks
    `parent_of` one header at a time, which is the same cost
    `median_time_past` below already pays to reach its own eleventh
    ancestor -- unbounded here rather than capped at ten, since a
    caller asking for a BIP68 time-locked input's own coin height can
    name any past height, not only one within the last eleven blocks.
    `main.py`'s own callers are where that cost is paid, once per
    input actually carrying a time-based relative lock rather than once
    per block regardless.
    """
    for _ in range(height - target_height):
        header = parent_of(header)
    return header


def _min_difficulty_bits(
    chain: Chain, parent: BlockHeader, height: int, time: int, parent_of: ParentOf
) -> bytes:
    # a block more than two target spacings after its parent may be
    # mined at the limit
    if time > block_time(parent) + 2 * POW_TARGET_SPACING:
        return chain.pow_limit_bits
    # and every block after it goes back to the last target that was not
    # the limit, so that one slow block does not make the period easy
    header = parent
    while (
        height
        and height % DIFFICULTY_ADJUSTMENT_INTERVAL
        and header.bits == chain.pow_limit_bits
    ):
        header = parent_of(header)
        height -= 1
    return header.bits


def next_bits_required(
    chain: Chain,
    parent: BlockHeader,
    parent_height: int,
    time: int,
    parent_of: ParentOf,
) -> bytes:
    """Return the compact target a header on this parent has to carry.

    Core's `GetNextWorkRequired`, asked of the parent at `parent_height`
    about
    a header timestamped `time`. The target moves once every
    `DIFFICULTY_ADJUSTMENT_INTERVAL` blocks and is the parent's the rest
    of the time, so `bits` is a value the chain fixes rather than one a
    miner chooses.

    A chain that does not retarget is answered first and with the
    parent's target, where Core reaches the same answer further down:
    every branch it takes on such a chain answers with the limit, and
    the limit is what every block there carries, `Chain.pow_limit_bits`
    being the genesis' own target and the genesis the block the rest
    descend from. Asking it here is what keeps the min-difficulty walk
    below off a chain on which each of its steps is one more block back
    to the genesis.
    """
    if chain.consensus.pow_no_retargeting:
        return parent.bits

    if (parent_height + 1) % DIFFICULTY_ADJUSTMENT_INTERVAL:
        if chain.consensus.pow_allow_min_difficulty_blocks:
            return _min_difficulty_bits(chain, parent, parent_height, time, parent_of)
        return parent.bits

    # the period ends at the parent, and `retarget_first_height` names
    # the block it is measured from -- 2015 blocks back and not 2016,
    # which is the off-by-one Core keeps
    first = parent
    for _ in range(parent_height - retarget_first_height(parent_height)):
        first = parent_of(first)
    return next_bits(
        parent.bits, first.time, parent.time, pow_limit_bits=chain.pow_limit_bits
    )


def assert_valid_in_context(  # noqa: PLR0913, PLR0917
    chain: Chain,
    header: BlockHeader,
    parent: BlockHeader,
    parent_height: int,
    parent_of: ParentOf,
    now: datetime,
) -> None:
    """Assert what the chain before a header requires of it.

    Six parameters and one call site: each is a distinct, independent
    piece of what Core's own `ContextualCheckBlockHeader` reads too --
    the chain's own rules, the header, its parent, that parent's height,
    a way to walk further back for the two checks that need more than
    one ancestor, and the time to check the header's own against. No
    subset of them travels together anywhere else in this module, so
    grouping any of them into a struct of their own would be a wrapper
    built for this one call rather than a shape the data already has.

    Core's `ContextualCheckBlockHeader`, in its order, less the version
    floors a deployment decides: `bad-diffbits`, then `time-too-old`,
    then `time-too-new`. `parent` sits at `parent_height`, so the header
    being checked is the block after it.

    The timewarp rule Core adds under `enforce_BIP94` is not here: it
    holds on testnet4 and on a regtest run with `-test=bip94`, and this
    node offers neither network.
    """
    time = block_time(header)

    required = next_bits_required(chain, parent, parent_height, time, parent_of)
    if header.bits != required:
        err_msg = f"proof-of-work target not the required one: {header.bits.hex()}"
        err_msg += f" instead of {required.hex()}"
        raise BTClibValueError(err_msg)

    median = median_time_past(parent, parent_height, parent_of)
    if time <= median:
        err_msg = f"invalid timestamp (not after the median past): {time}"
        err_msg += f" <= {median}"
        raise BTClibValueError(err_msg)

    header.assert_valid_time(now)
