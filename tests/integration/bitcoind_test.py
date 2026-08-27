# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A live `Node` reaches a real bitcoind's own tip, over p2p.

Every other p2p test in this suite connects one `Node` to another --
`tests/functional/p2p/download_test.py`'s own bootstrap peers included --
which shows this node's handshake and download working against itself
and nothing about `btclib`'s own p2p implementation meeting a Bitcoin
Core it did not write. This is the one test in the suite that asks that
question, against the pinned release `integration-bitcoind.yml` installs.

`peer_address("127.0.0.1", ...)`, not `tests.local_addr`'s
`0.0.0.0` construction: bitcoind here is a real process bound to an
explicit host and not this suite's own convenience for dialling a peer on
the same machine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.p2p.address import peer_address
from tests import get_random_port, wait_until, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind


def test_node_syncs_from_a_real_bitcoind(bitcoind: Bitcoind, tmp_path: Path) -> None:
    """A fresh node, connected to bitcoind, matches its tip after a sync.

    bitcoind mines onto a wallet address of its own -- what matters to
    this test is that the address is valid, not who can spend it -- and
    the node under test is never told to mine or relay anything back:
    what is being checked is the download path alone.
    """
    bitcoind.rpc("createwallet", ["integration"])
    address = bitcoind.rpc("getnewaddress")
    bitcoind.rpc("generatetoaddress", [5, address])
    tip = bitcoind.rpc("getbestblockhash")

    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node.start()
    wait_until_listening(node.p2p_manager)

    node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))

    block_index = node.chainstate.block_index
    expected_length = len(block_index.active_chain) + 5
    wait_until(lambda: len(block_index.active_chain) == expected_length)
    wait_until(lambda: node.status == NodeStatus.BlockSynced)

    assert block_index.active_chain[-1].hex() == tip

    node.stop()
    node.join()
