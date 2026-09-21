# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`USER_AGENT` is one constant, read rather than recomputed by its two callers.

btclib-org/btclib-node#1009: `p2p.connection`'s own wire bytes and
`rpc.callbacks.get_network_info`'s `subversion` used to compute the
identical formula independently, with nothing tying the two together.
"""

from typing import TYPE_CHECKING, cast

from btclib_node import constants
from btclib_node.p2p import connection
from btclib_node.rpc.callbacks import get_network_info

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection


def test_the_wire_user_agent_and_getnetworkinfo_s_subversion_are_one_constant() -> None:
    """Both read `constants.USER_AGENT`, so the two cannot drift apart."""
    node = cast("Node", None)
    conn = cast("RpcConnection", None)
    result = get_network_info(node, conn, [])
    assert result["subversion"] == constants.USER_AGENT
    assert constants.USER_AGENT.encode() == connection._USER_AGENT
