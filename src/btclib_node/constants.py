# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The small enumerations and constants shared across this package.

`ProtocolVersion`, `P2pConnStatus` for a single peer connection's own
handshake state, `NodeStatus` for what stage of startup, sync or
shutdown the node as a whole is in, and `COINBASE_MATURITY`.
"""

import enum

__all__ = ["COINBASE_MATURITY", "NodeStatus", "P2pConnStatus", "ProtocolVersion"]

ProtocolVersion = 70016

# Core's own `COINBASE_MATURITY` (`src/consensus/consensus.h`,
# at bitcoin/bitcoin@204256c73f): how many blocks a coinbase output has
# to sit before a spend of it may connect. Not part of `Chain` below
# despite `bip34_height` living there -- Core keeps this one a bare
# `constexpr`, the same across every network rather than a
# `Consensus::Params` field, and regtest does not relax it either;
# `tests/__init__.py`'s own `generate_random_chain` is where that is
# argued against a chain short enough to have nothing mature to spend.
COINBASE_MATURITY = 100


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
