# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import json

import requests

from btclib_node import Node
from btclib_node.config import Config
from tests.helpers import wait_until


def test_init(tmp_path):
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=True,
        )
    )
    node.start()

    wait_until(lambda: node.rpc_manager.is_alive())

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "stop",
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )

    assert response["result"] == "Btclib node stopping"

    node.stop()

    # the node was already asked to stop from inside its own loop,
    # which is the one caller that cannot wait for it; asking again
    # from outside is what waits
    assert not node.is_alive()
