# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import enum

ProtocolVersion = 70016


# The service bits are `btclib.p2p.address.ServiceFlags`, not a table
# here: they are a bitfield rather than an enumeration, so an unnamed
# bit is a service nobody here has heard of and not an error, and the
# table this used to hold named a bit Core removed while missing the
# one BIP324 added.


class P2pConnStatus(enum.IntEnum):
    Open = 1
    Connected = 2
    Closed = 3


class NodeStatus(enum.IntEnum):
    Starting = 1
    SyncingHeaders = 2
    HeaderSynced = 3
    Reindexing = 4
    BlockSynced = 5
