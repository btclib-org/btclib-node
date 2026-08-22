# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import pytest
from btclib.script import script
from btclib.tx.tx import Tx, TxIn, TxOut
from btclib.tx.tx_in import OutPoint

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.main import update_chain, verify_mempool_acceptance
from tests.helpers import (
    build_block,
    generate_coinbase,
    generate_random_chain,
    generate_random_transaction,
)


def regtest_node(tmp_path):
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )
    node.status = NodeStatus.HeaderSynced
    return node


def connect(node, chain):
    """Offer a chain to the node and drive it to connect what it will."""
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for hash in block_index.header_dict:
        block_info = block_index.get_block_info(hash)
        block_info.downloaded = True
        block_index.insert_block_info(block_info)
    for block in chain:
        node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(node)
    return block_index


def test_chain(tmp_path):
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )
    node.status = NodeStatus.HeaderSynced
    length = 2000 * 1  # 2000
    chain = generate_random_chain(length, RegTest().genesis.hash)
    headers = [block.header for block in chain]
    block_index = node.chainstate.block_index
    for x in range(0, length, 2000):
        block_index.add_headers(headers[x : x + 2000])
    for x in block_index.header_dict:
        block_info = block_index.get_block_info(x)
        block_info.downloaded = True
        block_index.insert_block_info(block_info)
    for block in chain:
        node.block_db.add_block(block)
    for x in range(len(chain)):
        update_chain(node)
    assert len(block_index.active_chain) == length + 1


def spend(prevout_tx, value, script_sig=None):
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(prevout_tx.id, 0),
                script_sig=script_sig
                if script_sig is not None
                else script.serialize([b"\x11" * 32]),
                sequence=0xFFFFFFFF,
            )
        ],
        vout=[
            TxOut(value=value, script_pub_key=script.serialize([b"\x22" * 32])),
        ],
    )


def test_reject_block_that_prints_money(tmp_path):
    # Script validation never reads the amounts except through the
    # sig_hash, so nothing in the engine notices an output larger than
    # the input it spends.
    node = regtest_node(tmp_path)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[-1].transactions[0]
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(), spend(funding, funding.vout[0].value + 1)],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected


def test_reject_block_with_a_failing_script(tmp_path):
    # An input that does not verify has to fail the block. It used to be
    # written to errors/ and swallowed, inside a worker pool, so nothing
    # reached update_chain and the block was connected anyway.
    node = regtest_node(tmp_path)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[-1].transactions[0]
    unspendable = spend(
        funding,
        funding.vout[0].value,
        script_sig=script.serialize(["OP_RETURN"]),
    )
    bad = build_block(
        chain[-1].header.hash, [generate_coinbase(), unspendable], len(chain)
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected


def test_add_tx(tmp_path):
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )
    node.status = NodeStatus.HeaderSynced
    chain = generate_random_chain(10, RegTest().genesis.hash)
    headers = [block.header for block in chain]
    block_index = node.chainstate.block_index
    block_index.add_headers(headers)
    for x in block_index.header_dict:
        block_info = block_index.get_block_info(x)
        block_info.downloaded = True
        block_index.insert_block_info(block_info)
    for block in chain:
        node.block_db.add_block(block)
    for x in range(len(chain)):
        update_chain(node)

    with pytest.raises(MissingPrevoutError):
        invalid_tx = generate_random_transaction()
        verify_mempool_acceptance(node, invalid_tx)

    tx1 = generate_random_transaction(chain[-1].transactions[0].id)
    tx2 = generate_random_transaction(tx1.id)

    verify_mempool_acceptance(node, tx1)

    # We can't find the prevouts
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, tx2)

    # tx1 needs to be added to the mempool
    node.mempool.add_tx(tx1)
    verify_mempool_acceptance(node, tx2)


def hashes(chain):
    return [block.header.hash for block in chain]


def test_a_heavier_fork_replaces_the_chain_the_node_was_on(tmp_path):
    # more than one block on the branch being left, because with one
    # there is no order to undo them in and the undo used to run the
    # wrong way: an output block N created and block N+1 spent is gone
    # from the utxo set by the time N is undone
    node = regtest_node(tmp_path)
    first = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, first)
    assert block_index.active_chain[1:] == hashes(first)

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)
    assert block_index.active_chain[1:] == hashes(second)
    for block_hash in hashes(first):
        assert block_hash not in block_index.active_chain


def test_a_reorg_gives_the_orphaned_transactions_back_to_the_mempool(tmp_path):
    # only once the node is synced: while it is still catching up, a
    # transaction from a block it steps off is not worth relaying
    node = regtest_node(tmp_path)
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    assert node.status == NodeStatus.BlockSynced

    orphaned = first[-1].transactions[1]
    assert not node.mempool.contains_tx(orphaned)

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)

    assert node.mempool.contains_tx(orphaned)
    # and what the new chain confirmed is not left in it
    for block in second:
        for transaction in block.transactions:
            assert not node.mempool.contains_tx(transaction)


def test_a_reorg_before_the_node_is_synced_leaves_the_mempool_alone(tmp_path):
    node = regtest_node(tmp_path)
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.status = NodeStatus.HeaderSynced

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)
    assert node.mempool.size == 0
