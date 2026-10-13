# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`-reindex-chainstate` on a started node, read back over RPC.

`tests/unit/reindex_chainstate_test.py` checks the rebuild piece by
piece. This checks what a client sees: a node started again with the
option answers `getblockcount` and `gettxoutsetinfo`, both hash types,
as it did before.
"""

from typing import TYPE_CHECKING, Any

from btclib.tx.limits import COINBASE_MATURITY

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.main import update_chain
from tests import (
    generate_random_chain,
    generate_random_transaction,
    get_random_port,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path

    from btclib.block import Block

# how long a request may take, the scan of `gettxoutsetinfo` included
BOUND = 30


def _started(tmp_path: Path, *, reindex_chainstate: bool = False) -> Node:
    """Start a regtest node on `tmp_path`, with RPC and no P2P."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
            reindex_chainstate=reindex_chainstate,
        )
    )
    node.start()
    wait_until_listening(node.rpc_manager)
    return node


def _answers(node: Node) -> list[Any]:
    """Return what `getblockcount` and both `gettxoutsetinfo` hashes answer."""
    client = rpc_client(node, BOUND)
    return [
        client.call_raw(method, params, request_timeout=BOUND)[1]["result"]
        for method, params in (
            ("getblockcount", []),
            ("gettxoutsetinfo", []),
            ("gettxoutsetinfo", ["muhash"]),
        )
    ]


def _connect(node: Node, chain: list[Block]) -> None:
    """Index, store and connect `chain` on `node`."""
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)


def _reindexed(node: Node) -> bool:
    """Whether `load` has run and the reindex it began has ended."""
    return node.loaded and node.status == NodeStatus.SyncingHeaders


def test_a_node_started_with_the_option_answers_as_before(tmp_path: Path) -> None:
    """Same height, same `hash_serialized_3`, same `muhash`."""
    regtest = RegTest()
    chain = generate_random_chain(COINBASE_MATURITY + 5, regtest.genesis.hash)
    node = _started(tmp_path)
    try:
        _connect(node, chain)
        before = _answers(node)
    finally:
        node.stop()
    assert before[0] == len(chain)

    node = _started(tmp_path, reindex_chainstate=True)
    try:
        # `loaded` is set after `load` sets `Reindexing`, so the stage
        # `run` set before it is back only once the reindex has ended
        wait_until(lambda: _reindexed(node))
        assert _answers(node) == before
    finally:
        node.stop()


def _stop_part_way(tmp_path: Path, blocks: int) -> None:
    """Load with the option, connect `blocks` stored blocks, and close."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            reindex_chainstate=True,
        )
    )
    node.load()
    try:
        for _ in range(blocks):
            update_chain(node)
    finally:
        node._close_worker_pool()
        node.p2p_manager.peer_db.close()
        node.chainstate.close()
        node.block_db.close()
        node.p2p_manager.loop.close()
        node.rpc_manager.loop.close()
        node.logger.close()


def test_the_saved_mempool_loads_once_the_reindex_ends(tmp_path: Path) -> None:
    """A transaction spending a mature coinbase comes back from `mempool.dat`.

    Loaded against the wiped chainstate, it would find no prevout, and
    shutdown would write the emptied mempool over the file.
    """
    regtest = RegTest()
    chain = generate_random_chain(COINBASE_MATURITY + 5, regtest.genesis.hash)
    # chain[0]'s own coinbase is spent inside the chain; chain[1]'s is not
    funding = chain[1].transactions[0]
    tx = generate_random_transaction(funding.id, funding.vout[0].value - 1_000)
    node = _started(tmp_path)
    try:
        _connect(node, chain)
        client = rpc_client(node, BOUND)
        client.call("sendrawtransaction", [tx.serialize(include_witness=True).hex()])
    finally:
        node.stop()

    node = _started(tmp_path, reindex_chainstate=True)
    try:
        wait_until(lambda: _reindexed(node) and node.mempool.load_tried)
        assert rpc_client(node, BOUND).call("getrawmempool") == [tx.id.hex()]
    finally:
        node.stop()


def test_the_saved_mempool_survives_a_plain_start_after_a_stop_part_way(
    tmp_path: Path,
) -> None:
    """The plain start connects the rest before it loads `mempool.dat`.

    The transaction spends chain[1]'s coinbase, immature at the height
    the stop leaves: loaded there, it would be refused, and shutdown
    would write the mempool without it over the file.
    """
    regtest = RegTest()
    chain = generate_random_chain(COINBASE_MATURITY + 5, regtest.genesis.hash)
    funding = chain[1].transactions[0]
    tx = generate_random_transaction(funding.id, funding.vout[0].value - 1_000)
    node = _started(tmp_path)
    try:
        _connect(node, chain)
        client = rpc_client(node, BOUND)
        client.call("sendrawtransaction", [tx.serialize(include_witness=True).hex()])
    finally:
        node.stop()

    _stop_part_way(tmp_path, 10)

    node = _started(tmp_path)
    try:
        wait_until(lambda: _reindexed(node) and node.mempool.load_tried)
        assert node.chainstate.block_index.active_chain[-1] == chain[-1].header.hash
        assert rpc_client(node, BOUND).call("getrawmempool") == [tx.id.hex()]
    finally:
        node.stop()
