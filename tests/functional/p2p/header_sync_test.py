# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Two real nodes already at the same tip finish header sync with each other."""

from contextlib import ExitStack
from typing import TYPE_CHECKING

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from tests import (
    generate_random_header_chain,
    get_random_port,
    local_addr,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_peers_at_the_same_tip_both_reach_header_synced(tmp_path: Path) -> None:
    """Each node's initial `getheaders` draws the shared tip from the other.

    Core's own reason for starting the locator at the best header's parent
    (`SendMessages`, `net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7):
    "This ensures that we always get a non-empty list of headers back as long
    as the peer is up-to-date." That short batch, connecting, is what moves a
    node to `HeaderSynced`.
    """
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    nodes = []
    for name in ("node1", "node2"):
        node = Node(
            config=Config(
                chain="regtest",
                data_dir=tmp_path / name,
                p2p_port=get_random_port(),
                allow_rpc=False,
            )
        )
        node.chainstate.block_index.add_headers(chain)
        nodes.append(node)
    node1, node2 = nodes
    with ExitStack() as stack:
        for node in nodes:
            node.start()
            stack.callback(node.stop)
            wait_until_listening(node.p2p_manager)
        node1.p2p_manager.connect(local_addr(node2.p2p_port))
        wait_until(lambda: node1.status >= NodeStatus.HeaderSynced)
        wait_until(lambda: node2.status >= NodeStatus.HeaderSynced)
