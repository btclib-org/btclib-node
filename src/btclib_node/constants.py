# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The small enumerations and constants shared across this package.

`ProtocolVersion`, `P2pConnStatus` for a single peer connection's own
handshake state, `NodeStatus` for what stage of startup, sync or
shutdown the node as a whole is in, `COINBASE_MATURITY`, and
`MAX_TIP_AGE`.
"""

import enum
from datetime import timedelta

__all__ = [
    "COINBASE_MATURITY",
    "MAX_TIP_AGE",
    "MIN_BLOCKS_TO_KEEP",
    "MIN_PRUNE_TARGET_MIB",
    "NodeStatus",
    "P2pConnStatus",
    "ProtocolVersion",
]

ProtocolVersion = 70016

# Core's own `DEFAULT_MAX_TIP_AGE` (`src/kernel/chainstatemanager_opts.h`
# :24, at bitcoin/bitcoin@ca7162cde5): how old the active chain's own
# tip may be and still count as recent, half of what
# `main.update_ibd_status` reads to decide `IsInitialBlockDownload` --
# the other half is `Chain.minimum_chain_work` (chains.py).
MAX_TIP_AGE = timedelta(hours=24)

# Core's own `COINBASE_MATURITY` (`src/consensus/consensus.h`,
# at bitcoin/bitcoin@204256c73f): how many blocks a coinbase output has
# to sit before a spend of it may connect. Not part of `Chain` below
# despite `bip34_height` living there -- Core keeps this one a bare
# `constexpr`, the same across every network rather than a
# `Consensus::Params` field, and regtest does not relax it either;
# `tests/__init__.py`'s own `generate_random_chain` is where that is
# argued against a chain short enough to have nothing mature to spend.
COINBASE_MATURITY = 100

# Core's own `MIN_BLOCKS_TO_KEEP` (`src/validation.h:76`, at
# bitcoin/bitcoin@ca7162cde5): block files within this many blocks of the
# tip are never pruned. `NODE_NETWORK_LIMITED_MIN_BLOCKS`
# (`src/net_processing.cpp:157`, same commit) is the separate constant
# Core checks before answering a peer's `getdata` for an old block once
# this node's own services say `NODE_NETWORK_LIMITED` rather than
# `NODE_NETWORK` -- both 288 (two days of ten-minute blocks) at this
# sha, so `block_db.BlockDB.prune_up_to` and `p2p.callbacks`'s own
# below-threshold disconnect share this one name rather than carrying
# two constants that only happen to agree today.
MIN_BLOCKS_TO_KEEP = 288

# Core's own `MIN_DISK_SPACE_FOR_BLOCK_FILES` (`src/validation.h:87`, at
# bitcoin/bitcoin@ca7162cde5): the smallest `-prune=<n>` MiB target Core
# accepts for automatic pruning -- `node::ApplyArgsManOptions`
# (`node/blockmanager_args.cpp:28-34`, same sha) treats `<n>` between 2
# and this value minus one as too small to run a node on and refuses to
# start, in Core's own words, rather than rounding it up; `<n>` of
# exactly 1 is manual pruning instead of a MiB target at all, the same
# special case `cli.py`'s own `-prune` parsing carries.
MIN_PRUNE_TARGET_MIB = 550


# The service bits are `btclib.p2p.address.ServiceFlags`, not a table
# here: they are a bitfield rather than an enumeration, so an unnamed
# bit is a service nobody here has heard of and not an error, and the
# table this used to hold named a bit Core removed while missing the
# one BIP324 added.


class P2pConnStatus(enum.IntEnum):
    """One peer connection's own handshake state, from accept to `verack`."""

    Open = 1
    Connected = 2
    Closed = 3


class NodeStatus(enum.IntEnum):
    """Which stage of startup or sync a `Node` as a whole is in."""

    Starting = 1
    SyncingHeaders = 2
    HeaderSynced = 3
    BlockSynced = 5
