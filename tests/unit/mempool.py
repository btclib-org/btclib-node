# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import secrets

from btclib.script.witness import Witness
from btclib.tx.tx import Tx

from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from tests.helpers import generate_random_transaction


def a_witness_transaction() -> Tx:
    # a txid and a wtxid are the same bytes until there is a witness, and
    # an assertion about one would then pass by naming the other
    tx = generate_random_transaction()
    tx.vin[0].script_witness = Witness([secrets.token_bytes(32)])
    return tx


def test_init() -> None:
    Mempool(Logger(debug=True))


def test_workflow() -> None:
    mempool = Mempool(Logger(debug=True))

    tx = generate_random_transaction()
    mempool.add_tx(tx)

    assert mempool.size == 1
    assert mempool.bytesize == tx.vsize
    assert mempool.get_tx(tx.id) == tx
    assert mempool.get_tx(tx.hash, wtxid=True) == tx

    mempool.remove_tx(tx)
    assert mempool.size == 0
    assert mempool.bytesize == 0

    txs = []
    for x in range(100):
        tx = generate_random_transaction()
        mempool.add_tx(tx)
        txs.append(tx)

    prev_size = mempool.size
    prev_bytesize = mempool.bytesize
    # Test is_full() method
    mempool.bytesize_limit = mempool.bytesize
    mempool.add_tx(generate_random_transaction())
    assert prev_size == mempool.size
    assert prev_bytesize == mempool.bytesize

    tx = generate_random_transaction()
    mempool.bytesize_limit = 1000**2
    assert mempool.get_missing([tx.id for tx in txs] + [tx.id]) == [tx.id]

    assert mempool.get_tx(b"\x00" * 32) is None


def test_a_full_mempool_takes_nothing_and_asks_for_nothing() -> None:
    # bytesize_limit is what stops an unbounded relay: past it the
    # mempool neither accepts a transaction nor reports one missing, so
    # download.tx_download stops asking peers for what it cannot hold.
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 0
    assert mempool.is_full()

    tx = generate_random_transaction()
    assert mempool.get_missing([tx.id]) == []
    mempool.add_tx(tx)
    assert mempool.size == 0
    assert not mempool.contains_tx(tx)


def test_the_same_transaction_twice_is_counted_once() -> None:
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx)
    mempool.add_tx(tx)
    assert mempool.size == 1
    assert mempool.bytesize == tx.vsize


def test_removing_what_was_never_there_changes_nothing() -> None:
    mempool = Mempool(Logger(debug=True))
    mempool.remove_tx(generate_random_transaction())
    assert mempool.size == 0
    assert mempool.bytesize == 0


def test_nothing_is_missing_when_everything_is_held() -> None:
    mempool = Mempool(Logger(debug=True))
    txs = [a_witness_transaction() for _ in range(3)]
    for tx in txs:
        mempool.add_tx(tx)
    assert mempool.get_missing([tx.id for tx in txs]) == []
    assert mempool.get_missing([tx.hash for tx in txs], wtxid=True) == []
    # the other identifier of the same transaction is not held under this
    # one, so asking by txid for a wtxid reports every one of them missing
    assert mempool.get_missing([tx.hash for tx in txs]) == [tx.hash for tx in txs]


def test_a_transaction_is_found_by_either_of_its_identifiers() -> None:
    mempool = Mempool(Logger(debug=True))
    tx = a_witness_transaction()
    assert tx.id != tx.hash  # what the two lookups below are about
    mempool.add_tx(tx)
    assert mempool.get_tx(tx.id) == tx
    assert mempool.get_tx(tx.hash, wtxid=True) == tx
    # and neither identifier answers under the other's index
    assert mempool.get_tx(tx.hash) is None
    assert mempool.get_tx(tx.id, wtxid=True) is None
    assert mempool.get_tx(b"\x11" * 32) is None
