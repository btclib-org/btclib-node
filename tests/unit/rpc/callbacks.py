# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What each RPC method answers, including for the shapes nothing sends.

The functional tests drive the happy path of a few of these through a
real client. What is left is the rest of the table, and the branches a
client reaches only by asking about a block at the tip or on a fork this
node did not follow, a peer that goes away mid-lookup, or a transaction
the mempool refuses.
"""

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast

import pytest
from btclib.block import BlockHeader
from btclib.exceptions import BTClibValueError
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.script import script
from btclib.script.witness import Witness
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node.chains import RegTest
from btclib_node.constants import P2pConnStatus
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
    service_names,
    stop,
)

# aliased: pytest collects a module-level `test*` as a test, and this
# one is a production function that would be handed fixtures
from btclib_node.rpc.callbacks import test_mempool_accept as mempool_accept
from tests.helpers import generate_random_header_chain

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import Connection

# none of these callbacks reads the connection it is handed
_CONN = cast("Connection", None)


def a_tx(tag: bytes = b"\x11") -> Tx:
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
    def __init__(self, *, gone: bool = False) -> None:
        self.gone = gone

    def getpeername(self) -> tuple[str, int]:
        if self.gone:
            raise OSError
        return ("1.2.3.4", 8333)

    def getsockname(self) -> tuple[str, int]:
        return ("5.6.7.8", 18444)


def a_peer(
    status: P2pConnStatus = P2pConnStatus.Connected, *, gone: bool = False
) -> Any:
    return SimpleNamespace(
        status=status,
        client=FakeSocket(gone=gone),
        version_message=SimpleNamespace(
            services=ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS,
            addr_recv=NetworkAddress(0, "9.10.11.12", 8333),
            version=70015,
        ),
        address=SimpleNamespace(network_id=SimpleNamespace(name="IPV4")),
        last_send=1,
        last_receive=2,
        last_block_timestamp=3,
        latency=0.5,
        inbound=True,
    )


def a_node(
    peers: dict[int, Any] | None = None,
    mempool: Mempool | None = None,
    accept: Any = None,
) -> Any:
    return SimpleNamespace(
        p2p_manager=SimpleNamespace(
            connections=peers if peers is not None else {},
            ping_all=lambda: None,
        ),
        mempool=mempool if mempool is not None else Mempool(Logger(debug=True)),
        _accept=accept,
    )


def test_the_transactions_here_tell_a_txid_from_a_wtxid() -> None:
    # what the assertions below rest on: without a witness the two are
    # the same bytes, and an answer that named the wrong one would pass
    tx = a_tx()
    assert tx.id != tx.hash
    assert tx.vsize != tx.size


def test_the_peer_table_names_a_connected_peer() -> None:
    node = a_node({7: a_peer()})
    (info,) = get_peer_info(node, _CONN, [])
    assert info["id"] == 7
    assert info["addr"] == "1.2.3.4:8333"
    assert info["addrbind"] == "5.6.7.8:18444"
    assert info["network"] == "ipv4"
    # unwrapped, and not the ::ffff: form the sixteen octets of the
    # field hold a v4 peer in. Core would bracket a v6 one and this does
    # not, which is #147 and not this table's business
    assert info["addrlocal"] == "9.10.11.12:8333"
    assert info["servicesnames"] == ["NETWORK", "WITNESS"]
    assert info["inbound"] is True


def test_the_services_are_named_the_way_core_names_them() -> None:
    # `serviceFlagsToStr`: the NODE_ prefix of btclib's own enum
    # dropped, the bits walked from the least significant up, and a bit
    # no member names reported rather than left out -- Core reserves a
    # range for temporary experiments, so a peer offering one is
    # offering a service and not making a mistake
    assert service_names(ServiceFlags.NODE_NONE) == []
    assert service_names(
        ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_COMPACT_FILTERS
    ) == ["NETWORK", "COMPACT_FILTERS"]
    # bit 40, which no member names, in among two that do and in the
    # place its own bit puts it
    assert service_names(
        ServiceFlags.NODE_WITNESS | (1 << 40) | ServiceFlags.NODE_NETWORK
    ) == ["NETWORK", "WITNESS", "UNKNOWN[2^40]"]


def test_a_peer_still_handshaking_is_not_in_the_table() -> None:
    node = a_node({7: a_peer(P2pConnStatus.Open)})
    assert get_peer_info(node, _CONN, []) == []


def test_a_peer_that_goes_away_mid_lookup_is_skipped() -> None:
    # its own connection state already reports it; the table just
    # carries on rather than failing the whole request
    node = a_node({7: a_peer(gone=True), 8: a_peer()})
    (info,) = get_peer_info(node, _CONN, [])
    assert info["id"] == 8


def test_the_connection_count_is_every_connection() -> None:
    assert get_connection_count(a_node({1: a_peer(), 2: a_peer()}), _CONN, []) == 2


def test_the_mempool_reports_its_size_and_bytes() -> None:
    mempool = Mempool(Logger(debug=True))
    tx = a_tx()
    mempool.add_tx(tx)
    out = get_mempool_info(a_node(mempool=mempool), _CONN, [])
    assert out == {"loaded": True, "size": 1, "bytes": tx.vsize}


def test_the_raw_mempool_is_txids_or_a_table() -> None:
    mempool = Mempool(Logger(debug=True))
    tx = a_tx()
    mempool.add_tx(tx)
    node = a_node(mempool=mempool)

    assert get_raw_mempool(node, _CONN, []) == {"txids": [tx.id.hex()]}
    assert get_raw_mempool(node, _CONN, [False]) == {"txids": [tx.id.hex()]}

    verbose = get_raw_mempool(node, _CONN, [True])
    assert list(verbose) == [tx.id.hex()]
    assert verbose[tx.id.hex()]["wtxid"] == tx.hash.hex()
    assert verbose[tx.id.hex()]["vsize"] == tx.vsize
    assert verbose[tx.id.hex()]["weight"] == tx.weight


def test_ping_and_stop_answer_without_a_connection() -> None:
    pinged: list[bool] = []
    node = a_node()
    node.p2p_manager.ping_all = lambda: pinged.append(True)
    ping(node, _CONN, [])
    assert pinged == [True]
    assert stop(node, _CONN, []) == "Btclib node stopping"


def test_mempool_acceptance_reports_a_reason_for_each_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from btclib.exceptions import BTClibValueError

    import btclib_node.rpc.callbacks as cb

    tx = a_tx()
    raw = tx.serialize(True).hex()

    outcomes: dict[str, Exception | None] = {
        "accepted": None,
        "Invalid signatures or script": BTClibValueError("no"),
        "Missing prevouts": MissingPrevoutError(),
        "Unknown error": RuntimeError("no"),
    }
    for reason, error in outcomes.items():

        def verify(node: Any, tx: Any, error: Exception | None = error) -> None:
            if error is not None:
                raise error

        monkeypatch.setattr(cb, "verify_mempool_acceptance", verify)
        (result,) = mempool_accept(a_node(), _CONN, [[raw]])
        if reason == "accepted":
            assert result["allowed"] is True
            assert "reject-reason" not in result
        else:
            assert result["allowed"] is False
            assert result["reject-reason"] == reason


def test_an_unparsable_transaction_is_named_as_such() -> None:
    (result,) = mempool_accept(a_node(), _CONN, [["not a transaction"]])
    assert result == {"allowed": False, "reject-reason": "Invalid serialization"}


def test_a_relayed_transaction_is_answered_with_its_txid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import btclib_node.rpc.callbacks as cb

    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: None)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = broadcast.append

    assert send_raw_transaction(node, _CONN, [tx.serialize(True).hex()]) == tx.id.hex()
    assert mempool.contains_tx(tx)
    assert broadcast == [tx]


def test_something_that_is_not_a_transaction_is_answered_with_nothing() -> None:
    assert send_raw_transaction(a_node(), _CONN, ["not a transaction"]) is None


def test_a_transaction_the_mempool_will_not_have_is_not_reported_relayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a refusal is not answered with the txid of a transaction this node
    # has neither kept nor sent
    import btclib_node.rpc.callbacks as cb

    def missing(node: Any, transaction: Any) -> NoReturn:
        raise MissingPrevoutError

    monkeypatch.setattr(cb, "verify_mempool_acceptance", missing)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = broadcast.append

    with pytest.raises(MissingPrevoutError):
        send_raw_transaction(node, _CONN, [tx.serialize(True).hex()])
    assert not mempool.contains_tx(tx)
    assert broadcast == []


def a_header_index(
    chain: list[BlockHeader], off_chain: list[BlockHeader] | None = None
) -> Any:
    """An index whose two lookups are two collections, as the real one's are.

    `header_index` is the best chain and nothing else, where
    `get_block_info` answers for every header the node has indexed, the
    losing side of a fork included. A fake keying both off one list
    cannot hold a block that is known and not on the best chain, which
    is why btclib-org/btclib-node#87 was an issue rather than a test.

    A height is the parent's plus one, which is how `BlockInfo.index` is
    built, so a header off the best chain is at the height its own fork
    puts it and not at a position in a chain it is not on.
    """

    def block(header: BlockHeader, height: int) -> Any:
        return SimpleNamespace(header=header, index=height, chainwork=height + 1)

    blocks = {header.hash: block(header, height) for height, header in enumerate(chain)}
    for header in off_chain or []:
        blocks[header.hash] = block(
            header, blocks[header.previous_block_hash].index + 1
        )
    return SimpleNamespace(
        header_index=[header.hash for header in chain],
        get_block_info=blocks.__getitem__,
    )


def test_a_block_header_names_the_ones_either_side_of_it() -> None:
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_header_index(chain))),
    )
    middle = get_block_header(node, _CONN, [chain[1].hash.hex()])
    assert middle["hash"] == chain[1].hash
    assert middle["height"] == 1
    assert middle["confirmations"] == 2
    assert middle["previousblockhash"] == chain[0].hash
    assert middle["nextblockhash"] == chain[2].hash
    assert middle["chainwork"] == 2


def test_the_first_header_has_nothing_before_it_and_the_last_nothing_after() -> None:
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_header_index(chain))),
    )
    first = get_block_header(node, _CONN, [chain[0].hash.hex()])
    assert "previousblockhash" not in first
    assert first["nextblockhash"] == chain[1].hash

    last = get_block_header(node, _CONN, [chain[-1].hash.hex()])
    assert last["previousblockhash"] == chain[-2].hash
    assert "nextblockhash" not in last
    assert last["confirmations"] == 1


def test_a_block_off_the_best_chain_is_described_and_not_refused() -> None:
    # what Core's blockheaderToJSON answers for one: the height the
    # block has on its own fork, confirmations -1 in place of a depth,
    # the parent it names, and no nextblockhash
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    fork = generate_random_header_chain(1, chain[0].hash)
    node = cast(
        "Node",
        SimpleNamespace(
            chainstate=SimpleNamespace(block_index=a_header_index(chain, fork))
        ),
    )
    stale = get_block_header(node, _CONN, [fork[0].hash.hex()])
    assert stale["hash"] == fork[0].hash
    assert stale["height"] == 1
    assert stale["confirmations"] == -1
    assert stale["previousblockhash"] == chain[0].hash
    assert "nextblockhash" not in stale

    # the block the best chain kept at that height is another block, and
    # is answered as before
    best = get_block_header(node, _CONN, [chain[1].hash.hex()])
    assert best["hash"] != stale["hash"]
    assert best["height"] == 1
    assert best["confirmations"] == 2
    assert best["nextblockhash"] == chain[2].hash


def test_a_fork_reaching_past_the_tip_is_not_read_off_the_end_of_the_chain() -> None:
    # a fork longer than the best chain is still not it, work and not
    # length being what decides -- and the best chain has no position to
    # answer for a height past its own
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    fork = generate_random_header_chain(3, chain[0].hash)
    node = cast(
        "Node",
        SimpleNamespace(
            chainstate=SimpleNamespace(block_index=a_header_index(chain, fork))
        ),
    )
    past_the_tip = get_block_header(node, _CONN, [fork[-1].hash.hex()])
    assert past_the_tip["height"] == 3
    assert past_the_tip["confirmations"] == -1
    assert past_the_tip["previousblockhash"] == fork[-2].hash
    assert "nextblockhash" not in past_the_tip


def test_the_tip_and_the_block_at_a_height_are_read_off_the_active_chain() -> None:
    chain = [b"\x11" * 32, b"\x22" * 32]
    node = cast(
        "Node",
        SimpleNamespace(
            chainstate=SimpleNamespace(block_index=SimpleNamespace(active_chain=chain))
        ),
    )
    assert get_best_block_hash(node, _CONN, []) == chain[-1]
    assert get_block_hash(node, _CONN, [0]) == chain[0]
    assert get_block_hash(node, _CONN, [1]) == chain[1]


def test_a_transaction_whose_scripts_do_not_verify_is_still_answered_with_its_txid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # what the code does, not what it should: #83 is the verdict a
    # rejected transaction ought to be answered with
    import btclib_node.rpc.callbacks as cb

    def invalid(node: Any, transaction: Any) -> NoReturn:
        raise BTClibValueError("no")

    monkeypatch.setattr(cb, "verify_mempool_acceptance", invalid)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx: None

    assert send_raw_transaction(node, _CONN, [tx.serialize(True).hex()]) == tx.id.hex()
    assert not mempool.contains_tx(tx)
