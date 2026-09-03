# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""testmempoolaccept, sendrawtransaction and getrawtransaction, live.

Drives a chain of dependent transactions through the mempool -- missing
prevouts, an accepted parent, a child that only accepts once its parent
is held in the mempool -- and checks `BitcoinCoreFetcher.get_tx` against
this node unchanged.
"""

from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import BitcoinCoreRpcClient
from btclib.fetch.bitcoin_core import BitcoinCoreFetcher
from btclib.tx.limits import COINBASE_MATURITY

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus
from tests import (
    generate_random_chain,
    generate_random_transaction,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from btclib_node import Node


def test_add_tx(rpc_node: Node) -> None:
    """`testmempoolaccept` and `sendrawtransaction`, live, across a tx chain.

    An unparsable string, a transaction with no known prevout, and a
    child of an unconfirmed, unheld parent are each refused;
    `sendrawtransaction` accepts the parent, `testmempoolaccept` then
    accepts the child once its parent is held, and `getmempoolinfo`
    reports the parent's own presence.
    """
    node = rpc_node
    client = rpc_client(node)

    wait_until_listening(node.rpc_manager)
    # COINBASE_MATURITY long -- exactly that and no more, so nothing in
    # the chain itself spends chain[0]'s coinbase first, leaving it for
    # tx1 below, which is old enough to spend it the moment this
    # chain's tip connects (btclib-org/btclib-node#569)
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    header_chain = [block.header for block in chain]
    block_index = node.chainstate.block_index
    block_index.add_headers(header_chain)
    node.status = NodeStatus.HeaderSynced
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)

    invalid_tx = generate_random_transaction()

    def accept(*hexes: str) -> Any:
        _, body = client.call_raw(
            "testmempoolaccept", [list(hexes)], jsonrpc="1.0", request_timeout=2
        )
        return body["result"][0]

    result = accept("00")
    assert not result["allowed"]
    assert result["reject-reason"] == "Invalid serialization"

    result = accept(invalid_tx.serialize(include_witness=True).hex())
    assert not result["allowed"]
    assert result["reject-reason"] == "Missing prevouts"

    tx1 = generate_random_transaction(chain[0].transactions[0].id)
    tx2 = generate_random_transaction(tx1.id)

    result = accept(tx1.serialize(include_witness=True).hex())
    assert result["allowed"]

    result = accept(tx2.serialize(include_witness=True).hex())
    assert not result["allowed"]
    assert result["reject-reason"] == "Missing prevouts"

    _, body = client.call_raw(
        "sendrawtransaction",
        [tx1.serialize(include_witness=True).hex()],
        jsonrpc="1.0",
        request_timeout=2,
    )
    assert body["result"] == tx1.id.hex()

    # one whose prevouts are nowhere -- not in the chain, not in the
    # mempool -- is answered with an error, not with the txid of
    # something this node has neither kept nor sent
    _, body = client.call_raw(
        "sendrawtransaction",
        [invalid_tx.serialize(include_witness=True).hex()],
        jsonrpc="1.0",
        request_timeout=2,
    )
    assert "result" not in body
    assert body["error"]["code"] == -25
    assert body["error"]["message"] == "Missing prevouts"

    _, body = client.call_raw("getmempoolinfo", jsonrpc="1.0", request_timeout=2)
    assert body["result"]["size"] == 1

    # Now that the transaction is in the mempool it should not fail
    result = accept(tx2.serialize(include_witness=True).hex())
    assert result["allowed"]


def test_get_raw_transaction_is_what_btclib_s_fetcher_gets(rpc_node: Node) -> None:
    """btclib-org/btclib-node#21: `BitcoinCoreFetcher.get_tx`, unchanged.

    The mempool is the only source this fetcher's own call can reach --
    it never passes a blockhash -- so this is the shape #21 actually
    asks for, and not the wider `getrawtransaction` this file's own
    `call_raw` calls already cover through `params`.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    # COINBASE_MATURITY long, for the same reason as test_add_tx above
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    node.status = NodeStatus.HeaderSynced
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)

    tx = generate_random_transaction(chain[0].transactions[0].id)

    # no cookie file on disk here, so credentials rather than
    # `cookie_path` -- this node checks neither, having no
    # authentication of its own (#27's own finding)
    client = BitcoinCoreRpcClient(
        f"http://127.0.0.1:{node.rpc_port}",
        user="pytest",
        password="pytest",  # noqa: S106
    )
    client.call_raw(
        "sendrawtransaction",
        [tx.serialize(include_witness=True).hex()],
        jsonrpc="1.0",
        request_timeout=2,
    )

    fetcher = BitcoinCoreFetcher(client, network="regtest")
    fetched = fetcher.get_tx(tx.id)
    assert fetched.id == tx.id
