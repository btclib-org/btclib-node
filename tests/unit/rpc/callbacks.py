# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What each RPC method answers, including for the shapes nothing sends.

The functional tests drive the happy path of a few of these through a
real client. What is left is the rest of the table, and the branches a
client reaches only by asking about a block at the tip, a peer that goes
away mid-lookup, or a transaction the mempool refuses.
"""

from types import SimpleNamespace

import pytest
from btclib.exceptions import BTClibValueError
from btclib.script import script
from btclib.script.witness import Witness
from btclib.tx.tx import Tx, TxIn, TxOut
from btclib.tx.tx_in import OutPoint

from btclib_node.chains import RegTest
from btclib_node.constants import P2pConnStatus, Services
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from btclib_node.rpc.callbacks import (
    get_best_block_hash,
    get_block_hash,
    get_block_header,
    get_connection_count,
    get_mempool_info,
    get_peer_info,
    get_raw_mempool,
    ping,
    send_raw_transaction,
    stop,
)

# aliased: pytest collects a module-level `test*` as a test, and this
# one is a production function that would be handed fixtures
from btclib_node.rpc.callbacks import test_mempool_accept as mempool_accept
from tests.helpers import generate_random_header_chain


def a_tx(tag=b"\x11"):
    # with a witness, so that id and hash -- and size and vsize -- are
    # different values: an assertion about one cannot pass by naming the
    # other
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(tag * 32, 0),
                script_sig=script.serialize([tag * 8]),
                sequence=0xFFFFFFFF,
                script_witness=Witness([tag * 8]),
            )
        ],
        vout=[TxOut(value=10**8, script_pub_key=script.serialize([tag * 8]))],
    )


class FakeSocket:
    def __init__(self, *, gone=False):
        self.gone = gone

    def getpeername(self):
        if self.gone:
            raise OSError
        return ("1.2.3.4", 8333)

    def getsockname(self):
        return ("5.6.7.8", 18444)


def a_peer(status=P2pConnStatus.Connected, *, gone=False):
    return SimpleNamespace(
        status=status,
        client=FakeSocket(gone=gone),
        version_message=SimpleNamespace(
            services=Services.network | Services.witness,
            addr_recv="addr-recv",
            version=70015,
        ),
        address=SimpleNamespace(netid=SimpleNamespace(name="ipv4")),
        last_send=1,
        last_receive=2,
        last_block_timestamp=3,
        latency=0.5,
        inbound=True,
    )


def a_node(peers=None, mempool=None, accept=None):
    return SimpleNamespace(
        p2p_manager=SimpleNamespace(
            connections=peers if peers is not None else {},
            ping_all=lambda: None,
        ),
        mempool=mempool if mempool is not None else Mempool(Logger(debug=True)),
        _accept=accept,
    )


def test_the_transactions_here_tell_a_txid_from_a_wtxid():
    # what the assertions below rest on: without a witness the two are
    # the same bytes, and an answer that named the wrong one would pass
    tx = a_tx()
    assert tx.id != tx.hash
    assert tx.vsize != tx.size


def test_the_peer_table_names_a_connected_peer():
    node = a_node({7: a_peer()})
    (info,) = get_peer_info(node, None, [])
    assert info["id"] == 7
    assert info["addr"] == "1.2.3.4:8333"
    assert info["addrbind"] == "5.6.7.8:18444"
    assert info["network"] == "ipv4"
    assert "NETWORK" in info["servicesnames"]
    assert info["inbound"] is True


def test_a_peer_still_handshaking_is_not_in_the_table():
    node = a_node({7: a_peer(P2pConnStatus.Open)})
    assert get_peer_info(node, None, []) == []


def test_a_peer_that_goes_away_mid_lookup_is_skipped():
    # its own connection state already reports it; the table just
    # carries on rather than failing the whole request
    node = a_node({7: a_peer(gone=True), 8: a_peer()})
    (info,) = get_peer_info(node, None, [])
    assert info["id"] == 8


def test_the_connection_count_is_every_connection():
    assert get_connection_count(a_node({1: a_peer(), 2: a_peer()}), None, []) == 2


def test_the_mempool_reports_its_size_and_bytes():
    mempool = Mempool(Logger(debug=True))
    tx = a_tx()
    mempool.add_tx(tx)
    out = get_mempool_info(a_node(mempool=mempool), None, [])
    assert out == {"loaded": True, "size": 1, "bytes": tx.vsize}


def test_the_raw_mempool_is_txids_or_a_table():
    mempool = Mempool(Logger(debug=True))
    tx = a_tx()
    mempool.add_tx(tx)
    node = a_node(mempool=mempool)

    assert get_raw_mempool(node, None, []) == {"txids": [tx.id.hex()]}
    assert get_raw_mempool(node, None, [False]) == {"txids": [tx.id.hex()]}

    verbose = get_raw_mempool(node, None, [True])
    assert list(verbose) == [tx.id.hex()]
    assert verbose[tx.id.hex()]["wtxid"] == tx.hash.hex()
    assert verbose[tx.id.hex()]["vsize"] == tx.vsize


def test_ping_and_stop_answer_without_a_connection():
    pinged = []
    node = a_node()
    node.p2p_manager.ping_all = lambda: pinged.append(True)
    assert ping(node, None, []) is None
    assert pinged == [True]
    assert stop(node, None, []) == "Btclib node stopping"


def test_mempool_acceptance_reports_a_reason_for_each_refusal(monkeypatch):
    from btclib.exceptions import BTClibValueError

    import btclib_node.rpc.callbacks as cb

    tx = a_tx()
    raw = tx.serialize(True).hex()

    outcomes = {
        "accepted": None,
        "Invalid signatures or script": BTClibValueError("no"),
        "Missing prevouts": MissingPrevoutError(),
        "Unknown error": RuntimeError("no"),
    }
    for reason, error in outcomes.items():

        def verify(node, tx, error=error):
            if error is not None:
                raise error

        monkeypatch.setattr(cb, "verify_mempool_acceptance", verify)
        (result,) = mempool_accept(a_node(), None, [[raw]])
        if reason == "accepted":
            assert result["allowed"] is True
            assert "reject-reason" not in result
        else:
            assert result["allowed"] is False
            assert result["reject-reason"] == reason


def test_an_unparsable_transaction_is_named_as_such():
    (result,) = mempool_accept(a_node(), None, [["not a transaction"]])
    assert result == {"allowed": False, "reject-reason": "Invalid serialization"}


def test_a_relayed_transaction_is_answered_with_its_txid(monkeypatch):
    import btclib_node.rpc.callbacks as cb

    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: None)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    broadcast = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = broadcast.append

    assert send_raw_transaction(node, None, [tx.serialize(True).hex()]) == tx.id.hex()
    assert mempool.contains_tx(tx)
    assert broadcast == [tx]


def test_something_that_is_not_a_transaction_is_answered_with_nothing():
    assert send_raw_transaction(a_node(), None, ["not a transaction"]) is None


def test_a_transaction_the_mempool_will_not_have_is_not_reported_relayed(monkeypatch):
    # a return inside a finally used to discard whatever was propagating
    # through it, so every refusal came back as the txid of a
    # transaction this node had neither kept nor sent
    import btclib_node.rpc.callbacks as cb

    def missing(node, transaction):
        raise MissingPrevoutError

    monkeypatch.setattr(cb, "verify_mempool_acceptance", missing)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    broadcast = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = broadcast.append

    with pytest.raises(MissingPrevoutError):
        send_raw_transaction(node, None, [tx.serialize(True).hex()])
    assert not mempool.contains_tx(tx)
    assert broadcast == []


def a_header_index(chain):
    hashes = [header.hash for header in chain]
    return SimpleNamespace(
        header_index=hashes,
        get_block_info=lambda block_hash: SimpleNamespace(
            header=chain[hashes.index(block_hash)],
            chainwork=hashes.index(block_hash) + 1,
        ),
    )


def test_a_block_header_names_the_ones_either_side_of_it():
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    node = SimpleNamespace(
        chainstate=SimpleNamespace(block_index=a_header_index(chain))
    )
    middle = get_block_header(node, None, [chain[1].hash.hex()])
    assert middle["height"] == 1
    assert middle["confirmations"] == 2
    assert middle["previousblockhash"] == chain[0].hash
    assert middle["nextblockhash"] == chain[2].hash
    assert middle["chainwork"] == 2


def test_the_first_header_has_nothing_before_it_and_the_last_nothing_after():
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    node = SimpleNamespace(
        chainstate=SimpleNamespace(block_index=a_header_index(chain))
    )
    first = get_block_header(node, None, [chain[0].hash.hex()])
    assert "previousblockhash" not in first
    assert first["nextblockhash"] == chain[1].hash

    last = get_block_header(node, None, [chain[-1].hash.hex()])
    assert last["previousblockhash"] == chain[-2].hash
    assert "nextblockhash" not in last
    assert last["confirmations"] == 1


def test_the_tip_and_the_block_at_a_height_are_read_off_the_active_chain():
    chain = [b"\x11" * 32, b"\x22" * 32]
    node = SimpleNamespace(
        chainstate=SimpleNamespace(block_index=SimpleNamespace(active_chain=chain))
    )
    assert get_best_block_hash(node, None, []) == chain[-1]
    assert get_block_hash(node, None, [0]) == chain[0]


def test_a_transaction_whose_scripts_do_not_verify_is_still_answered_with_its_txid(
    monkeypatch,
):
    # what the code does, not what it should: #83 is the verdict a
    # rejected transaction ought to be answered with
    import btclib_node.rpc.callbacks as cb

    def invalid(node, transaction):
        raise BTClibValueError("no")

    monkeypatch.setattr(cb, "verify_mempool_acceptance", invalid)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx: None

    assert send_raw_transaction(node, None, [tx.serialize(True).hex()]) == tx.id.hex()
    assert not mempool.contains_tx(tx)
