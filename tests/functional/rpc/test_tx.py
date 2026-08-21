# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import json

import requests

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus
from tests.helpers import generate_random_chain, generate_random_transaction, wait_until


def test_add_tx(rpc_node):
    node = rpc_node

    wait_until(lambda: node.rpc_manager.is_alive())
    chain = generate_random_chain(10, RegTest().genesis.hash)
    header_chain = [block.header for block in chain]
    block_index = node.chainstate.block_index
    block_index.add_headers(header_chain)
    node.status = NodeStatus.HeaderSynced
    for block in chain:
        node.block_db.add_block(block)
        block_info = block_index.get_block_info(block.header.hash)
        block_info.downloaded = True
        block_index.insert_block_info(block_info)
    wait_until(lambda: len(block_index.active_chain) == 11)

    invalid_tx = generate_random_transaction()

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "testmempoolaccept",
                    "params": [["00"]],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert not response["result"][0]["allowed"]
    assert response["result"][0]["reject-reason"] == "Invalid serialization"

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "testmempoolaccept",
                    "params": [[invalid_tx.serialize(True).hex()]],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert not response["result"][0]["allowed"]
    assert response["result"][0]["reject-reason"] == "Missing prevouts"

    tx1 = generate_random_transaction(chain[-1].transactions[0].id)
    tx2 = generate_random_transaction(tx1.id)

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "testmempoolaccept",
                    "params": [[tx1.serialize(True).hex()]],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert response["result"][0]["allowed"]

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "testmempoolaccept",
                    "params": [[tx2.serialize(True).hex()]],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert not response["result"][0]["allowed"]
    assert response["result"][0]["reject-reason"] == "Missing prevouts"

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "sendrawtransaction",
                    "params": [tx1.serialize(True).hex()],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert response["result"] == tx1.id.hex()
    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "getmempoolinfo",
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert response["result"]["size"] == 1

    # Now that the transaction is in the mempool it should not fail
    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "testmempoolaccept",
                    "params": [[tx2.serialize(True).hex()]],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert response["result"][0]["allowed"]
