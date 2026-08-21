# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import enum

ProtocolVersion = 70016


class Services(enum.IntEnum):
    network = 1
    getuxxo = 2
    bloom = 4
    witness = 8
    compact_filters = 64
    network_limited = 1024


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
