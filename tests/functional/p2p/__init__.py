# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from pathlib import Path

from btclib_node import Node
from btclib_node.config import Config
from tests.helpers import get_random_port, wait_until_listening


def test_init(tmp_path: Path) -> None:
    # a port of its own, as every other node in the suite has: left to
    # the chain's default this binds regtest's fixed 18444, and a second
    # suite running anywhere on the machine takes it. The bind then
    # raises inside a coroutine nobody awaits, so the listener never
    # comes up and nothing says why.
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node.start()

    wait_until_listening(node.p2p_manager)

    node.stop()

    # a test that ends with its node still running ends with something
    # still logging, and this is the node the p2p manager runs under
    assert not node.is_alive()
