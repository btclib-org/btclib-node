# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node import Node
from btclib_node.config import Config
from tests.helpers import wait_until


def test_init(tmp_path):
    node = Node(
        config=Config(
            chain="regtest", data_dir=tmp_path, allow_p2p=True, allow_rpc=False
        )
    )
    node.start()

    wait_until(lambda: node.p2p_manager.is_alive())

    node.stop()

    # a test that ends with its node still running ends with something
    # still logging, and this is the node the p2p manager runs under
    assert not node.is_alive()
