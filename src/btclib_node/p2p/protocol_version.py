# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The protocol versions a peer is kept at, and each feature begins at.

Core's `src/node/protocol_version.h` (at bitcoin/bitcoin@9be056a8a7, the
v31.1 tag), under Core's names; `PROTOCOL_VERSION` itself is
`btclib.p2p.limits`'. `common_version` is Core's
`CNode::GetCommonVersion`, which every feature gate reads.
`INVALID_CB_NO_BAN_VERSION` is left out: this node relays no compact
block for it to gate.
"""

from typing import TYPE_CHECKING

from btclib.p2p.limits import PROTOCOL_VERSION

if TYPE_CHECKING:
    from btclib_node.p2p.connection import Connection

__all__ = [
    "BIP0031_VERSION",
    "FEEFILTER_VERSION",
    "INIT_PROTO_VERSION",
    "MIN_PEER_PROTO_VERSION",
    "SENDHEADERS_VERSION",
    "SHORT_IDS_BLOCKS_VERSION",
    "WTXID_RELAY_VERSION",
    "common_version",
]

INIT_PROTO_VERSION = 209
MIN_PEER_PROTO_VERSION = 31800
# a `ping` carries a nonce, and is answered with a `pong`, only above it
BIP0031_VERSION = 60000
SENDHEADERS_VERSION = 70012
FEEFILTER_VERSION = 70013
SHORT_IDS_BLOCKS_VERSION = 70014
WTXID_RELAY_VERSION = 70016


def common_version(conn: Connection) -> int:
    """Return the lower of the peer's protocol version and this node's.

    `INIT_PROTO_VERSION` until the peer's `version` has been read, as
    Core starts `m_greatest_common_version`.
    """
    if conn.version_message is None:
        return INIT_PROTO_VERSION
    return min(conn.version_message.version, PROTOCOL_VERSION)
