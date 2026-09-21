# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The small enumerations and constants shared across this package.

`P2pConnStatus` for a single peer connection's own handshake state,
`NodeStatus` for what stage of startup, sync or shutdown the node as a
whole is in, and `MAX_TIP_AGE`. `COINBASE_MATURITY` is
`btclib.tx.limits`'s own, a function of a `Coin`'s own arguments rather
than a node's knob (btclib-org/btclib#1580), and `PROTOCOL_VERSION` is
`btclib.p2p.limits`'s own the same way (btclib-org/btclib#1582).
"""

import enum
from datetime import timedelta
from importlib.metadata import version

__all__ = [
    "MAX_TIP_AGE",
    "MIN_BLOCKS_TO_KEEP",
    "MIN_PRUNE_TARGET_MIB",
    "USER_AGENT",
    "NodeStatus",
    "P2pConnStatus",
]

# Core's own `DEFAULT_MAX_TIP_AGE` (`src/kernel/chainstatemanager_opts.h`
# :24, at bitcoin/bitcoin@ca7162cde5): how old the active chain's own
# tip may be and still count as recent, half of what
# `main.update_ibd_status` reads to decide `IsInitialBlockDownload` --
# the other half is `Chain.consensus.minimum_chain_work`
# (`btclib.consensus`).
MAX_TIP_AGE = timedelta(hours=24)

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

# BIP14's `/Name:Version/`, the shape Core builds in FormatSubVersion
# (`src/clientversion.cpp:65-70`, at bitcoin/bitcoin@204256c73f) and sends
# as `/Satoshi:29.0.0/` -- the one thing this node says about itself to
# every peer it meets, and what a crawler reporting the composition of
# the network parses. `p2p.connection`'s own `_USER_AGENT` (the wire
# bytes of a `version` message) and `rpc.callbacks.get_network_info`'s
# `subversion` answer both read this one string rather than each
# computing it, so the two can never drift the way they once did
# (btclib-org/btclib-node#1009).
#
# The version is read from the installed distribution rather than
# written here. `RELEASING.md`'s *Which version string is which*
# already tracks four spellings of one version, and a fifth that only a
# peer ever sees is the one nothing in this tree would catch drifting:
# no gate reads the wire. So this follows the cycle honestly -- a
# checkout of `main` announces the month it is open on, and what pip
# installs announces its release day.
#
# The name is the project's own, lowercase, and not the distribution's
# `btclib-node`: it is the organization's name on the network, where
# btclib is the library this node is a node over.
#
# A tree that was never installed has no metadata to read, and this
# raises there rather than falling back on a placeholder: a user agent
# is a claim, and one that says `unknown` where the version belongs is
# worse than a node that says why it will not start.
# btclib-org/btclib-node#580
USER_AGENT = f"/btclib:{version('btclib-node')}/"


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
