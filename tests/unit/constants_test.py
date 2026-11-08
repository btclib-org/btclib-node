# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`USER_AGENT` is one constant, read rather than recomputed by its two callers.

btclib-org/btclib-node#1009: `p2p.connection`'s own wire bytes and
`rpc.callbacks.get_network_info`'s `subversion` used to compute the
identical formula independently, with nothing tying the two together.
"""

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from btclib_node import constants
from btclib_node.config import (
    DEFAULT_INCREMENTAL_RELAY_FEERATE,
    DEFAULT_MIN_RELAY_FEERATE,
    DEFAULT_TX_SEND_RATE,
)
from btclib_node.notify import Warnings
from btclib_node.p2p import connection
from btclib_node.rpc.callbacks import get_network_info

if TYPE_CHECKING:
    from btclib_node.rpc.connection import RpcConnection


def test_the_wire_user_agent_and_getnetworkinfo_s_subversion_are_one_constant() -> None:
    """Both read `constants.USER_AGENT`, so the two cannot drift apart.

    The rest of `node` is what `get_network_info` reads besides, not this
    test's concern: plain namespaces at `Config`'s own defaults are enough.
    """
    node = cast(
        "Any",
        SimpleNamespace(
            config=SimpleNamespace(
                pruned=False,
                peerblockfilters=False,
                v2transport=False,
                v1transport=True,
                min_relay_feerate=DEFAULT_MIN_RELAY_FEERATE,
                tx_send_rate=DEFAULT_TX_SEND_RATE,
            ),
            download_manager=SimpleNamespace(inv_bucket_info=dict),
            mempool=SimpleNamespace(
                incremental_relay_feerate=DEFAULT_INCREMENTAL_RELAY_FEERATE
            ),
            outbound_time_offsets=[],
            p2p_manager=SimpleNamespace(
                local_snapshot=dict,
                connections={},
                pending_connections={},
                get_network_active=lambda: True,
            ),
            warnings=Warnings(),
        ),
    )
    conn = cast("RpcConnection", None)
    result = get_network_info(node, conn, [])
    assert result["subversion"] == constants.USER_AGENT
    assert constants.USER_AGENT.encode() == connection._USER_AGENT
