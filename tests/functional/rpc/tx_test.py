# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""testmempoolaccept, sendrawtransaction and getrawtransaction, live.

Drives a chain of dependent transactions through the mempool -- missing
prevouts, an accepted parent, a child that only accepts once its parent
is held in the mempool -- and checks `BitcoinCoreFetcher.get_tx` against
this node unchanged.
"""

import json
from typing import TYPE_CHECKING

import requests
from bitcoin_core_rpc import BitcoinCoreRpcClient
from btclib.fetch.bitcoin_core import BitcoinCoreFetcher

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus
from tests.helpers import (
    generate_random_chain,
    generate_random_transaction,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from btclib_node import Node


def test_add_tx(rpc_node: Node) -> None:
    """testmempoolaccept and sendrawtransaction, live, across a dependent chain.

    An unparsable string, a transaction with no known prevout, and a
    child of an unconfirmed, unheld parent are each refused;
    `sendrawtransaction` accepts the parent, `testmempoolaccept` then
    accepts the child once its parent is held, and `getmempoolinfo`
    reports the parent's own presence.
    """
    node = rpc_node

    wait_until_listening(node.rpc_manager)
    chain = generate_random_chain(10, RegTest().genesis.hash)
    header_chain = [block.header for block in chain]
    block_index = node.chainstate.block_index
    block_index.add_headers(header_chain)
    node.status = NodeStatus.HeaderSynced
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
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
                    "params": [[invalid_tx.serialize(include_witness=True).hex()]],
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
                    "params": [[tx1.serialize(include_witness=True).hex()]],
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
                    "params": [[tx2.serialize(include_witness=True).hex()]],
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
                    "params": [tx1.serialize(include_witness=True).hex()],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert response["result"] == tx1.id.hex()

    # one whose prevouts are nowhere -- not in the chain, not in the
    # mempool -- is answered with an error, not with the txid of
    # something this node has neither kept nor sent
    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "sendrawtransaction",
                    "params": [invalid_tx.serialize(include_witness=True).hex()],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert "result" not in response
    assert response["error"]["code"] == -25
    assert response["error"]["message"] == "Missing prevouts"

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
                    "params": [[tx2.serialize(include_witness=True).hex()]],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert response["result"][0]["allowed"]


def test_get_raw_transaction_is_what_btclib_s_fetcher_gets(rpc_node: Node) -> None:
    """btclib-org/btclib-node#21: `BitcoinCoreFetcher.get_tx`, unchanged.

    The mempool is the only source this fetcher's own call can reach --
    it never passes a blockhash -- so this is the shape #21 actually
    asks for, and not the wider `getrawtransaction` this file's own raw
    requests already cover through `params`.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    chain = generate_random_chain(1, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    node.status = NodeStatus.HeaderSynced
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == 1 + 1)

    tx = generate_random_transaction(chain[-1].transactions[0].id)
    requests.post(
        url=f"http://127.0.0.1:{node.rpc_port}",
        data=json.dumps(
            {
                "jsonrpc": "1.0",
                "id": "pytest",
                "method": "sendrawtransaction",
                "params": [tx.serialize(include_witness=True).hex()],
            }
        ).encode(),
        timeout=2,
    )

    client = BitcoinCoreRpcClient(
        f"http://127.0.0.1:{node.rpc_port}",
        user="pytest",
        password="pytest",  # noqa: S106
    )
    fetcher = BitcoinCoreFetcher(client, network="regtest")
    fetched = fetcher.get_tx(tx.id)
    assert fetched.id == tx.id
