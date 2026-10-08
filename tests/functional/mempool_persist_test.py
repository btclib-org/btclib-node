# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A node stopped and started again keeps its mempool, unless told not to."""

import secrets
import threading
import time
from typing import TYPE_CHECKING, Any

from btclib.tx.limits import COINBASE_MATURITY

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from tests import (
    generate_random_chain,
    generate_random_transaction,
    get_random_port,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    import pytest
    from bitcoin_core_rpc import BitcoinCoreRpcClient


def started(tmp_path: Path, *, persist_mempool: bool = True) -> Node:
    """Start an RPC-only regtest node on `tmp_path`."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
            debug=True,
            persist_mempool=persist_mempool,
        )
    )
    node.start()
    wait_until_listening(node.rpc_manager)
    return node


def loaded(client: BitcoinCoreRpcClient) -> dict[str, Any]:
    """Wait for the load of `mempool.dat` to end, and return the mempool."""
    wait_until(lambda: client.call("getmempoolinfo")["loaded"])
    return {
        txid: client.call("getmempoolentry", [txid])
        for txid in client.call("getrawmempool")
    }


def hold_two(node: Node) -> list[str]:
    """Connect a mature chain, then send a parent and its child; return them."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    node.status = NodeStatus.HeaderSynced
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)
    funding = chain[0].transactions[0]
    parent = generate_random_transaction(funding.id, funding.vout[0].value - 1_000)
    child = generate_random_transaction(parent.id, parent.vout[0].value - 1_000)
    client = rpc_client(node)
    for tx in (parent, child):
        client.call("sendrawtransaction", [tx.serialize(include_witness=True).hex()])
    return [parent.id.hex(), child.id.hex()]


def test_a_restart_keeps_the_mempool_and_the_deltas(tmp_path: Path) -> None:
    """Each entry comes back as it was: its time, delta and unbroadcast mark.

    A delta given to a transaction not held comes back as well.
    """
    node = started(tmp_path)
    try:
        client = rpc_client(node)
        parent, _ = hold_two(node)
        client.call("prioritisetransaction", [parent, None, 5_000])
        client.call("prioritisetransaction", [secrets.token_hex(32), None, -7])
        before = loaded(client)
        deltas = client.call("getprioritisedtransactions")
    finally:
        node.stop()
    assert len(before) == 2
    assert (node.data_dir / "mempool.dat").is_file()

    node = started(tmp_path)
    try:
        client = rpc_client(node)
        assert loaded(client) == before
        assert client.call("getprioritisedtransactions") == deltas
    finally:
        node.stop()


def test_persistmempool_0_neither_reads_nor_writes_the_file(tmp_path: Path) -> None:
    """The file a previous run left stays unread and untouched."""
    node = started(tmp_path)
    try:
        hold_two(node)
    finally:
        node.stop()
    path = node.data_dir / "mempool.dat"
    written = path.read_bytes()

    node = started(tmp_path, persist_mempool=False)
    try:
        client = rpc_client(node)
        assert loaded(client) == {}
        client.call("prioritisetransaction", [secrets.token_hex(32), None, 1])
    finally:
        node.stop()
    assert path.read_bytes() == written


def test_a_node_stopped_while_loading_keeps_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load never ends here, so the file is left as it was read."""
    node = started(tmp_path)
    try:
        hold_two(node)
    finally:
        node.stop()
    path = node.data_dir / "mempool.dat"
    written = path.read_bytes()
    stepped = threading.Event()

    def endless(*_: object) -> Generator[None, None, bool]:
        while True:
            stepped.set()
            time.sleep(0.01)
            yield

    monkeypatch.setattr("btclib_node.load_mempool", endless)
    node = started(tmp_path)
    try:
        assert stepped.wait(10)
        assert not rpc_client(node).call("getmempoolinfo")["loaded"]
    finally:
        node.stop()
    assert path.read_bytes() == written
