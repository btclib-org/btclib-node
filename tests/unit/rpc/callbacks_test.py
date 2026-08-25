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

import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast, override

import pytest
from btclib.exceptions import BTClibValueError
from btclib.fee import FeeRate
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.script import script
from btclib.script.witness import Witness
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

import btclib_node.rpc.callbacks as cb
from btclib_node.chains import Main, RegTest
from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE
from btclib_node.constants import P2pConnStatus
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from btclib_node.rpc.callbacks import (
    get_best_block_hash,
    get_block_count,
    get_block_hash,
    get_block_header,
    get_blockchain_info,
    get_connection_count,
    get_mempool_info,
    get_peer_info,
    get_raw_mempool,
    get_raw_transaction,
    ping,
    send_raw_transaction,
    service_names,
    stop,
)

# aliased: pytest collects a module-level `test*` as a test, and this
# one is a production function that would be handed fixtures
from btclib_node.rpc.callbacks import test_mempool_accept as mempool_accept
from btclib_node.rpc.connection import RawJSON
from btclib_node.rpc.errors import RpcError, RpcErrorCode
from tests.helpers import generate_random_header_chain

if TYPE_CHECKING:
    from btclib.block import BlockHeader

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

# none of these callbacks reads the connection it is handed
_CONN = cast("RpcConnection", None)


def a_tx(tag: bytes = b"\x11") -> Tx:
    """Build a transaction whose id and hash -- and size and vsize -- differ.

    A witness makes the two diverge, so a test asserting one of a pair
    cannot pass by naming the other instead.
    """
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
    """A socket double for `get_peer_info`, answering fixed peer/bind addresses.

    `getpeername` raises where `gone` is set, standing in for a peer that
    disconnected between `get_peer_info` copying `connections` and this
    lookup running.
    """

    def __init__(
        self, *, gone: bool = False, peer: str = "1.2.3.4", bind: str = "5.6.7.8"
    ) -> None:
        """Set the peer and local addresses this socket answers, and `gone`."""
        self.gone = gone
        self.peer = peer
        self.bind = bind

    def getpeername(self) -> tuple[str, int]:
        """Answer `peer`, or raise `OSError` where `gone` is set."""
        if self.gone:
            raise OSError
        return (self.peer, 8333)

    def getsockname(self) -> tuple[str, int]:
        """Answer `bind`, read as `get_peer_info`'s own `addrbind`."""
        return (self.bind, 18444)


def a_peer(
    status: P2pConnStatus = P2pConnStatus.Connected,
    *,
    gone: bool = False,
    # a different host in each, so that an answer naming the wrong
    # source cannot pass
    peer: str = "1.2.3.4",
    bind: str = "5.6.7.8",
    local: str = "9.10.11.12",
) -> Any:
    """Build a `P2pManager.connections` entry `get_peer_info` can read.

    `peer`, `bind` and `local` each default to a different host, so an
    assertion naming the wrong one of the three cannot pass by accident.
    """
    return SimpleNamespace(
        status=status,
        client=FakeSocket(gone=gone, peer=peer, bind=bind),
        version_message=SimpleNamespace(
            services=ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS,
            addr_recv=NetworkAddress(0, local, 8333),
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
    pending: dict[int, Any] | None = None,
    min_relay_feerate: FeeRate = DEFAULT_MIN_RELAY_FEERATE,
) -> Any:
    """Build a `Node` double carrying only what these callbacks read.

    A peer table, a mempool, and the configured minimum relay feerate --
    nothing else these tests' own callbacks look at.
    """
    return SimpleNamespace(
        p2p_manager=SimpleNamespace(
            connections=peers if peers is not None else {},
            pending_connections=pending if pending is not None else {},
            ping_all=lambda: None,
        ),
        mempool=mempool if mempool is not None else Mempool(Logger(debug=True)),
        config=SimpleNamespace(min_relay_feerate=min_relay_feerate),
        _accept=accept,
    )


def test_the_transactions_here_tell_a_txid_from_a_wtxid() -> None:
    """Check `a_tx`'s own premise: it makes id/hash and size/vsize differ.

    Every assertion below that names one of a pair rests on the two
    actually being distinct values.
    """
    tx = a_tx()
    assert tx.id != tx.hash
    assert tx.vsize != tx.size


def test_the_peer_table_names_a_connected_peer() -> None:
    """`getpeerinfo` names a connected peer's address, network and services.

    Every field comes off the peer's own socket and version message,
    keyed by connection id.
    """
    node = a_node({7: a_peer()})
    (info,) = get_peer_info(node, _CONN, [])
    assert info["id"] == 7
    assert info["addr"] == "1.2.3.4:8333"
    assert info["addrbind"] == "5.6.7.8:18444"
    assert info["network"] == "ipv4"
    # unwrapped, and not the ::ffff: form the sixteen octets of the
    # field hold a v4 peer in
    assert info["addrlocal"] == "9.10.11.12:8333"
    assert info["servicesnames"] == ["NETWORK", "WITNESS"]
    assert info["inbound"] is True


def test_a_v6_peer_is_named_with_the_brackets_core_writes() -> None:
    """`getpeerinfo` brackets every IPv6 address in its answer (issue #147).

    Without the brackets, `2001:db8::1` on port 8333 and `2001:db8::1:8333`
    on another port are the same string, and a client splitting on the
    last colon reads one of the two wrong.
    """
    node = a_node(
        {7: a_peer(peer="2001:db8::1", bind="2001:db8::2", local="2001:db8::3")}
    )
    (info,) = get_peer_info(node, _CONN, [])
    assert info["addr"] == "[2001:db8::1]:8333"
    assert info["addrbind"] == "[2001:db8::2]:18444"
    assert info["addrlocal"] == "[2001:db8::3]:8333"


def test_the_services_are_named_the_way_core_names_them() -> None:
    """service_names matches Core's own `serviceFlagsToStr`.

    The `NODE_` prefix of btclib's own enum is dropped, the bits are
    walked from the least significant up, and a bit no member names is
    reported as `UNKNOWN[2^n]` rather than left out -- Core reserves a
    range for temporary experiments, so a peer offering one is offering a
    service and not making a mistake.
    """
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
    """`getpeerinfo` leaves out a connection short of `verack`.

    Such a connection carries no version message yet for the answer's
    fields to read.
    """
    node = a_node({7: a_peer(P2pConnStatus.Open)})
    assert get_peer_info(node, _CONN, []) == []


def test_a_peer_that_goes_away_mid_lookup_is_skipped() -> None:
    """`getpeerinfo` skips a peer whose socket fails mid-lookup.

    Its own connection state already reports the disconnect; the table
    just carries on rather than failing the whole request.
    """
    node = a_node({7: a_peer(gone=True), 8: a_peer()})
    (info,) = get_peer_info(node, _CONN, [])
    assert info["id"] == 8


def test_a_connection_removed_mid_loop_does_not_raise() -> None:
    """`getpeerinfo` does not raise when a connection is removed mid-loop.

    `get_peer_info` reads `connections.copy()`, so a connection
    `remove_connection` pops mid-loop -- it runs on `P2pManager`'s own
    loop, this on `Node`'s, every pass of `manage_connections` -- does
    not raise `RuntimeError: dictionary changed size during iteration`
    out of a live dict's iterator noticing the pop instead. (issue #356)
    """
    connections: dict[int, Any] = {}

    class PoppingOnCompare:
        """`p2p_conn.status == P2pConnStatus.Connected`'s own left side.

        Standing in for whatever this node's loop is doing when
        `remove_connection` reaches in: the pop happens as a side
        effect of evaluating peer 7's status, between the iterator's
        own `next()` for peer 7 and its `next()` for peer 8 -- mid-loop
        on a live dict, and not reachable at all once the fix's
        `.copy()` hands the loop its own dict to iterate instead.
        """

        @override
        def __eq__(self, other: object) -> bool:
            connections.pop(8, None)
            return False

        # never put in a dict or a set, only compared -- explicit
        # rather than the implicit None a bare `__eq__` override
        # already gets, which the object being unhashable does not
        # itself demonstrate
        __hash__ = None  # type: ignore[assignment]

    connections[7] = a_peer(status=cast("P2pConnStatus", PoppingOnCompare()))
    connections[8] = a_peer()
    node = a_node(connections)
    # peer 8 is popped from the live `connections` above, not from the
    # copy this call iterates -- so the copy still answers for it,
    # unaffected by a pop reaching the dict it was taken from
    assert [info["id"] for info in get_peer_info(node, _CONN, [])] == [8]


def test_the_connection_count_is_every_connection() -> None:
    """`getconnectioncount` counts every entry of the peer table."""
    assert get_connection_count(a_node({1: a_peer(), 2: a_peer()}), _CONN, []) == 2


def test_the_connection_count_includes_a_peer_still_mid_handshake() -> None:
    """`getconnectioncount` also counts a pending, not yet handshaken, peer.

    Core's own `getconnectioncount` counts every entry of `m_nodes`,
    which holds a socket before its handshake and not only after.
    """
    node = a_node({1: a_peer()}, pending={2: a_peer(P2pConnStatus.Open)})
    assert get_connection_count(node, _CONN, []) == 2


def test_the_mempool_reports_its_size_and_bytes() -> None:
    """`getmempoolinfo`'s size and bytes fields read the mempool's own tally.

    `size` is its transaction count and `bytes` its total vsize.
    """
    mempool = Mempool(Logger(debug=True))
    tx = a_tx()
    mempool.add_tx(tx)
    out = get_mempool_info(a_node(mempool=mempool), _CONN, [])
    assert out["loaded"] is True
    assert out["size"] == 1
    assert out["bytes"] == tx.vsize


def test_the_mempool_reports_its_own_limit_as_maxmempool() -> None:
    """`getmempoolinfo`'s maxmempool is the mempool's own bytesize_limit."""
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 12345
    out = get_mempool_info(a_node(mempool=mempool), _CONN, [])
    assert out["maxmempool"] == 12345


def test_mempoolminfee_floors_at_the_configured_relay_feerate() -> None:
    """`mempoolminfee` floors at the configured minimum relay feerate.

    The mempool's own rolling minimum is 0 until something evicts, so the
    configured floor is what answers -- converted to Core's own BTC/kvB
    unit, exact to eight decimals rather than a float's own repr: 500
    sat/kvB is 0.00000500 BTC/kvB.
    """
    mempool = Mempool(Logger(debug=True))
    node = a_node(mempool=mempool, min_relay_feerate=FeeRate(sats_per_kvbyte=500))
    out = get_mempool_info(node, _CONN, [])
    assert isinstance(out["mempoolminfee"], RawJSON)
    assert out["mempoolminfee"].text == "0.00000500"


def test_mempoolminfee_rises_with_the_mempools_own_rolling_minimum() -> None:
    """`mempoolminfee` follows the mempool's own rolling minimum once it rises.

    Once the rolling minimum exceeds the configured relay feerate, it is
    what answers instead of the floor.
    """
    mempool = Mempool(Logger(debug=True))
    mempool._rolling_min_fee_rate = 5000.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time()  # nothing decayed yet
    node = a_node(mempool=mempool, min_relay_feerate=FeeRate(sats_per_kvbyte=500))
    out = get_mempool_info(node, _CONN, [])
    assert out["mempoolminfee"].text == "0.00005000"


def test_mempoolminfee_at_a_magnitude_a_float_would_write_in_exponent_notation() -> (
    None
):
    """`mempoolminfee` at 1 sat/kvB is a plain decimal, not exponent notation.

    1 sat/kvB is 1e-08 BTC/kvB in a Python float's own repr -- exactly the
    magnitude Core's `%d.%08d` format never produces, and the case
    `RawJSON` exists for.
    """
    mempool = Mempool(Logger(debug=True))
    node = a_node(mempool=mempool, min_relay_feerate=FeeRate(sats_per_kvbyte=1))
    out = get_mempool_info(node, _CONN, [])
    assert out["mempoolminfee"].text == "0.00000001"


def test_the_raw_mempool_is_a_plain_list_of_txids_by_default() -> None:
    """`getrawmempool` with no flags, or both false, answers a plain array.

    Matches `MempoolToJSON`'s own default shape (`src/rpc/mempool.cpp`
    :624-634), not the `{"txids": [...]}` object this node used to
    answer regardless of what was asked for -- that shape is owed only
    where mempool_sequence is true, below (issue #219).
    """
    mempool = Mempool(Logger(debug=True))
    tx = a_tx()
    mempool.add_tx(tx)
    node = a_node(mempool=mempool)

    assert get_raw_mempool(node, _CONN, []) == [tx.id.hex()]
    assert get_raw_mempool(node, _CONN, [False]) == [tx.id.hex()]
    assert get_raw_mempool(node, _CONN, [False, False]) == [tx.id.hex()]


def test_the_raw_mempool_verbose_table_names_each_transaction() -> None:
    """`getrawmempool` verbose answers an object keyed by txid.

    Each entry names its own wtxid, vsize and weight.
    """
    mempool = Mempool(Logger(debug=True))
    tx = a_tx()
    mempool.add_tx(tx)
    node = a_node(mempool=mempool)

    verbose = get_raw_mempool(node, _CONN, [True])
    assert isinstance(verbose, dict)
    assert list(verbose) == [tx.id.hex()]
    assert verbose[tx.id.hex()]["wtxid"] == tx.hash.hex()
    assert verbose[tx.id.hex()]["vsize"] == tx.vsize
    assert verbose[tx.id.hex()]["weight"] == tx.weight


def test_mempool_sequence_attaches_the_mempool_s_own_counter() -> None:
    """`getrawmempool`'s mempool_sequence flag attaches the mempool's counter.

    `mempool_sequence` used to be silently ignored; `MempoolToJSON`'s own
    shape for it is an object carrying both the array and the count
    (`src/rpc/mempool.cpp`:635-639), and the count advances with every
    addition, starting at 1 like Core's own `m_sequence_number`
    (issue #219).
    """
    mempool = Mempool(Logger(debug=True))
    node = a_node(mempool=mempool)

    # a fresh mempool answers 1, not 0: Core's own m_sequence_number
    # starts at 1 (src/txmempool.h:202) and GetSequence (:598-600) is a
    # plain read of the current value, with zero add/remove events
    # behind it
    empty = get_raw_mempool(node, _CONN, [False, True])
    assert empty == {"txids": [], "mempool_sequence": 1}

    tx = a_tx()
    mempool.add_tx(tx)
    answer = get_raw_mempool(node, _CONN, [False, True])
    assert answer == {"txids": [tx.id.hex()], "mempool_sequence": 2}

    # the counter is the mempool's own, not recomputed by the callback:
    # a second addition after the first answer moves it
    second = a_tx(b"\x22")
    mempool.add_tx(second)
    again = get_raw_mempool(node, _CONN, [None, True])
    assert isinstance(again, dict)
    assert set(again["txids"]) == {tx.id.hex(), second.id.hex()}
    assert again["mempool_sequence"] == 3


def test_verbose_and_mempool_sequence_together_are_refused() -> None:
    """`getrawmempool` refuses verbose and mempool_sequence given together.

    Matches `MempoolToJSON`'s own refusal (`src/rpc/mempool.cpp`
    :608-611): the combination is refused outright, rather than
    answering one and dropping the other.
    """
    node = a_node(mempool=Mempool(Logger(debug=True)))
    with pytest.raises(RpcError) as raised:
        get_raw_mempool(node, _CONN, [True, True])
    assert raised.value.code == RpcErrorCode.INVALID_PARAMETER
    assert raised.value.message == (
        "Verbose results cannot contain mempool sequence values."
    )


def test_a_raw_mempool_parameter_of_the_wrong_json_type_is_named() -> None:
    """`getrawmempool` names the JSON type of a wrong-typed boolean parameter.

    The same check `RPCMethod::HandleRequest` makes (`src/rpc/util.cpp`
    :653-661) against both of `getrawmempool`'s declared
    `RPCArg::Type::BOOL` parameters (`src/rpc/mempool.cpp`:694-695).
    """
    node = a_node(mempool=Mempool(Logger(debug=True)))

    with pytest.raises(RpcError) as raised:
        get_raw_mempool(node, _CONN, ["true"])
    assert raised.value.code == RpcErrorCode.TYPE_ERROR
    assert (
        raised.value.message == "JSON value of type string is not of expected type bool"
    )

    with pytest.raises(RpcError) as raised:
        get_raw_mempool(node, _CONN, [False, 1])
    assert raised.value.code == RpcErrorCode.TYPE_ERROR
    assert (
        raised.value.message == "JSON value of type number is not of expected type bool"
    )


def a_tx_lookup_node(
    mempool_txs: list[Tx] | None = None,
    blocks: dict[bytes, Any] | None = None,
) -> Any:
    """Build a node with the mempool and block store get_raw_transaction reads.

    `blocks` keys a `SimpleNamespace(transactions=[...])` off the hash
    `get_block_info` would answer it for -- `a_block_index`'s own shape,
    reused here for the height and active-chain position a verbose
    answer names, with `block_db.get_block` a plain dict lookup beside
    it, `None` for a hash the index carries and the block store does
    not: BlockIndex and BlockDb are two stores for a reason (pruning),
    and this is the one place `get_raw_transaction` reads them both.
    """
    mempool = Mempool(Logger(debug=True))
    for tx in mempool_txs or []:
        mempool.add_tx(tx)
    block_index = a_block_index([])
    if blocks:
        headers = [block.header for block in blocks.values()]
        block_index = a_block_index(sorted(headers, key=lambda h: h.time))
    return cast(
        "Node",
        SimpleNamespace(
            mempool=mempool,
            chainstate=SimpleNamespace(block_index=block_index),
            block_db=SimpleNamespace(get_block=(blocks or {}).get),
        ),
    )


def test_a_mempool_transaction_answers_the_raw_hex_by_default() -> None:
    """`getrawtransaction` for a mempool-held transaction answers the raw hex.

    With no verbose flag, the answer is the serialized transaction, not
    the verbose object.
    """
    tx = a_tx()
    node = a_tx_lookup_node(mempool_txs=[tx])
    assert (
        get_raw_transaction(node, _CONN, [tx.id.hex()])
        == tx.serialize(include_witness=True).hex()
    )


def test_a_mempool_transaction_verbose_carries_no_block_fields() -> None:
    """`getrawtransaction` verbose omits block fields for a mempool-only tx.

    A transaction found only in the mempool, not looked up in any block,
    carries no `blockhash` or `confirmations`.
    """
    tx = a_tx()
    node = a_tx_lookup_node(mempool_txs=[tx])
    out = get_raw_transaction(node, _CONN, [tx.id.hex(), True])
    assert isinstance(out, dict)
    assert out["txid"] == tx.id.hex()
    assert out["hex"] == tx.serialize(include_witness=True).hex()
    assert "blockhash" not in out
    assert "confirmations" not in out


def test_a_transaction_neither_mempool_nor_named_block_is_refused() -> None:
    """`getrawtransaction` refuses a txid in neither the mempool nor a block.

    The message points the caller at `gettransaction` for wallet
    transactions, which this refusal is not answering for.
    """
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["11" * 32])
    assert raised.value.code == RpcErrorCode.INVALID_ADDRESS_OR_KEY
    assert raised.value.message.startswith("No such mempool transaction.")


def test_a_transaction_is_read_out_of_the_block_named() -> None:
    """`getrawtransaction` finds a transaction inside a block named explicitly.

    Verbose reports it confirmed and on the active chain.
    """
    tx = a_tx()
    header = generate_random_header_chain(1, RegTest().genesis.hash)[0]
    block = SimpleNamespace(header=header, transactions=[tx])
    node = a_tx_lookup_node(blocks={header.hash: block})

    hex_answer = get_raw_transaction(
        node, _CONN, [tx.id.hex(), False, header.hash.hex()]
    )
    assert hex_answer == tx.serialize(include_witness=True).hex()

    verbose = get_raw_transaction(node, _CONN, [tx.id.hex(), True, header.hash.hex()])
    assert isinstance(verbose, dict)
    assert verbose["blockhash"] == header.hash.hex()
    assert verbose["in_active_chain"] is True
    assert verbose["confirmations"] == 1


def test_a_transaction_off_the_active_chain_is_named_but_not_confirmed() -> None:
    """`getrawtransaction` verbose reports an off-chain transaction unconfirmed.

    `in_active_chain` is false and `confirmations` is -1 for a block that
    holds the transaction but is not on the active chain.
    """
    tx = a_tx()
    header = generate_random_header_chain(1, RegTest().genesis.hash)[0]
    block = SimpleNamespace(header=header, transactions=[tx])
    node = a_tx_lookup_node(blocks={header.hash: block})
    # this block is indexed and stored but not on the active chain --
    # a_block_index's own `validated` narrows what generate_active_chain
    # would otherwise mean, and here it is simplest to fake directly
    block_index = node.chainstate.block_index
    block_index.active_chain = [RegTest().genesis.hash]

    verbose = get_raw_transaction(node, _CONN, [tx.id.hex(), True, header.hash.hex()])
    assert isinstance(verbose, dict)
    assert verbose["in_active_chain"] is False
    assert verbose["confirmations"] == -1


def test_a_transaction_the_named_block_does_not_hold_is_refused() -> None:
    """`getrawtransaction` refuses a txid not held by the named block."""
    tx = a_tx()
    other = a_tx(b"\x22")
    header = generate_random_header_chain(1, RegTest().genesis.hash)[0]
    block = SimpleNamespace(header=header, transactions=[other])
    node = a_tx_lookup_node(blocks={header.hash: block})

    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, [tx.id.hex(), False, header.hash.hex()])
    assert raised.value.code == RpcErrorCode.INVALID_ADDRESS_OR_KEY
    assert raised.value.message.startswith(
        "No such transaction found in the provided block."
    )


def test_an_unknown_block_hash_is_refused_by_name() -> None:
    """`getrawtransaction` refuses a block hash the index has never indexed."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["11" * 32, False, "22" * 32])
    assert raised.value.code == RpcErrorCode.INVALID_ADDRESS_OR_KEY
    assert raised.value.message == "Block hash not found"


def test_a_block_the_index_knows_and_the_store_does_not_is_unavailable() -> None:
    """`getrawtransaction` answers 'Block not available' for a pruned block.

    `BlockIndex` and `BlockDb` are two stores; a hash the first carries
    and the second does not is a pruned block, not a wrong request.
    """
    tx = a_tx()
    header = generate_random_header_chain(1, RegTest().genesis.hash)[0]
    node = a_tx_lookup_node(blocks={header.hash: SimpleNamespace(header=header)})
    node.block_db.get_block = lambda _hash: None

    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, [tx.id.hex(), False, header.hash.hex()])
    assert raised.value.code == RpcErrorCode.MISC_ERROR
    assert raised.value.message == "Block not available"


def test_no_txid_at_all_is_answered_with_the_usage() -> None:
    """`getrawtransaction` with no arguments is refused with its own usage."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, [])
    assert raised.value.code == RpcErrorCode.MISC_ERROR
    assert raised.value.message.startswith("getrawtransaction")


def test_a_txid_of_the_wrong_json_type_is_named() -> None:
    """`getrawtransaction`'s txid of the wrong JSON type is refused by name."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, [5])
    assert raised.value.code == RpcErrorCode.TYPE_ERROR
    assert (
        raised.value.message
        == "JSON value of type number is not of expected type string"
    )


def test_a_txid_that_is_not_hex_is_named_back_to_the_client() -> None:
    """`getrawtransaction`'s txid that fails to decode as hex is echoed back."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["zz"])
    assert raised.value.code == RpcErrorCode.INVALID_PARAMETER
    assert "zz" in raised.value.message


def test_a_blockhash_of_the_wrong_json_type_is_named() -> None:
    """`getrawtransaction`'s blockhash of the wrong JSON type is named."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["11" * 32, False, 5])
    assert raised.value.code == RpcErrorCode.TYPE_ERROR
    assert (
        raised.value.message
        == "JSON value of type number is not of expected type string"
    )


def test_a_blockhash_that_is_not_hex_is_named_back_to_the_client() -> None:
    """`getrawtransaction`'s blockhash that fails to decode as hex is echoed."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["11" * 32, False, "zz"])
    assert raised.value.code == RpcErrorCode.INVALID_PARAMETER
    assert "zz" in raised.value.message


def test_a_null_blockhash_is_the_same_as_none_given() -> None:
    """`getrawtransaction` treats an explicit null blockhash as omitted."""
    tx = a_tx()
    node = a_tx_lookup_node(mempool_txs=[tx])
    assert (
        get_raw_transaction(node, _CONN, [tx.id.hex(), False, None])
        == tx.serialize(include_witness=True).hex()
    )


def test_a_verbose_of_the_wrong_json_type_is_named() -> None:
    """`getrawtransaction`'s verbose of the wrong JSON type is named."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["11" * 32, "true"])
    assert raised.value.code == RpcErrorCode.TYPE_ERROR
    assert (
        raised.value.message == "JSON value of type string is not of expected type bool"
    )


def test_ping_and_stop_answer_without_a_connection() -> None:
    """`ping` and `stop` answer without reading the connection they are handed.

    `ping` pings every peer through `ping_all`; `stop` answers its own
    fixed message.
    """
    pinged: list[bool] = []
    node = a_node()
    node.p2p_manager.ping_all = lambda: pinged.append(True)
    ping(node, _CONN, [])
    assert pinged == [True]
    assert stop(node, _CONN, []) == "Btclib node stopping"


def test_mempool_acceptance_reports_a_reason_for_each_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`testmempoolaccept` reports allowed, or each refusal's own reject-reason.

    Runs the same transaction against every outcome
    `verify_mempool_acceptance` can produce -- accepted, an invalid
    script, missing prevouts, and an unexpected exception -- and checks
    each is reported as its own verdict rather than raising.
    """
    tx = a_tx()
    raw = tx.serialize(include_witness=True).hex()

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
    """`testmempoolaccept` reports a transaction that fails to parse as invalid.

    'Invalid serialization' is reported rather than raising.
    """
    (result,) = mempool_accept(a_node(), _CONN, [["not a transaction"]])
    assert result == {"allowed": False, "reject-reason": "Invalid serialization"}


def test_a_relayed_transaction_is_answered_with_its_txid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` adds an accepted transaction and broadcasts it.

    Answers its own txid, adds it to the mempool, and announces it to
    peers.
    """
    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 1000)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    assert (
        send_raw_transaction(node, _CONN, [tx.serialize(include_witness=True).hex()])
        == tx.id.hex()
    )
    assert mempool.contains_tx(tx)
    assert mempool.fees[tx.hash] == 1000
    assert broadcast == [tx]


def test_something_that_is_not_a_transaction_is_refused_rather_than_relayed() -> None:
    """`sendrawtransaction` refuses a string that fails to decode as a tx."""
    with pytest.raises(RpcError) as raised:
        send_raw_transaction(a_node(), _CONN, ["not a transaction"])
    assert raised.value.code == RpcErrorCode.DESERIALIZATION_ERROR
    assert raised.value.message == (
        "TX decode failed. Make sure the tx has at least one input."
    )


def test_a_transaction_truncated_inside_a_script_is_the_same_refusal() -> None:
    """`sendrawtransaction` refuses a tx truncated mid-script the same way.

    A scriptPubKey whose declared length reaches past the octets that
    follow it makes `Tx.parse` raise `BTClibRuntimeError` rather than the
    `BTClibValueError` the previous test's unparsable hex raises -- both
    are a decode failure and answered the same way.
    """
    truncated = (
        "01000000"  # version
        "01"  # input count
        + "00" * 32  # outpoint tx_id
        + "00000000"  # outpoint vout
        + "00"  # scriptSig, empty
        + "ffffffff"  # sequence
        + "01"  # output count
        + "00" * 8  # value
        + "05"  # scriptPubKey length 5, with nothing after it
    )
    with pytest.raises(RpcError) as raised:
        send_raw_transaction(a_node(), _CONN, [truncated])
    assert raised.value.code == RpcErrorCode.DESERIALIZATION_ERROR


def test_a_rawtx_of_the_wrong_json_type_is_named() -> None:
    """`sendrawtransaction`'s rawtx of the wrong JSON type is named."""
    with pytest.raises(RpcError) as raised:
        send_raw_transaction(a_node(), _CONN, [5])
    assert raised.value.code == RpcErrorCode.TYPE_ERROR
    assert (
        raised.value.message
        == "JSON value of type number is not of expected type string"
    )


def test_a_transaction_the_mempool_will_not_have_is_not_reported_relayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` does not report or broadcast a missing-prevouts tx.

    A refusal is not answered with the txid of a transaction this node
    has neither kept nor sent (issue #83).
    """

    def missing(node: Any, transaction: Any) -> NoReturn:
        raise MissingPrevoutError

    monkeypatch.setattr(cb, "verify_mempool_acceptance", missing)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    with pytest.raises(RpcError) as raised:
        send_raw_transaction(node, _CONN, [tx.serialize(include_witness=True).hex()])
    assert raised.value.code == RpcErrorCode.VERIFY_ERROR
    assert raised.value.message == "Missing prevouts"
    assert not mempool.contains_tx(tx)
    assert broadcast == []


def test_a_transaction_a_full_mempool_cannot_keep_is_refused_not_relayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` refuses, and does not broadcast, an evicted tx.

    A transaction `add_tx` evicts right back out under a full mempool is
    refused rather than reported kept (issue #293, issue #294).
    """
    # bytesize_limit at zero makes this transaction the only, and so the
    # worst, entry held: Mempool._evict_to_limit takes it right back out
    # once add_tx has added it provisionally (btclib-org/btclib-node#294),
    # the same silent no-op a full mempool's outright refusal used to be.
    # Answering with tx.id.hex() regardless would tell the caller this
    # transaction was kept when it was not, the same defect #277 fixed on
    # the peer-to-peer path. btclib-org/btclib-node#293
    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 1000)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 0
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    with pytest.raises(RpcError) as raised:
        send_raw_transaction(node, _CONN, [tx.serialize(include_witness=True).hex()])
    assert raised.value.code == RpcErrorCode.VERIFY_REJECTED
    assert raised.value.message == "Mempool is full"
    assert not mempool.contains_tx(tx)
    assert broadcast == []


def test_resubmitting_a_transaction_already_held_is_tolerated_even_when_the_mempool_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` reannounces a resubmission even when full.

    Matches `BroadcastTransaction`'s own early return for a txid already
    held.
    """
    # BroadcastTransaction's own early return for a txid already in the
    # mempool (node/transaction.cpp, bitcoin/bitcoin@58a7869f86):
    # resubmission is reannounced rather than refused for a fullness
    # this particular submission did not cause
    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 1000)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(tx, 1000)
    mempool.bytesize_limit = mempool.bytesize
    assert mempool.is_full()
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    assert (
        send_raw_transaction(node, _CONN, [tx.serialize(include_witness=True).hex()])
        == tx.id.hex()
    )
    assert broadcast == [tx]


def test_a_resubmission_under_a_different_witness_is_also_tolerated_when_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` reannounces the held wtxid, not the resubmitted one.

    `Mempool.contains_tx` is keyed by wtxid, so it does not recognise a
    txid already held under a different witness; the guard has to be
    txid-keyed instead, to reannounce here rather than refuse a fullness
    this resubmission did not cause.
    """
    # Mempool.contains_tx is keyed by wtxid (Mempool.transactions), so it
    # does not recognise a txid already held under a different witness --
    # exactly the case BroadcastTransaction's own comment names
    # (node/transaction.cpp, bitcoin/bitcoin@58a7869f86): "The mempool
    # transaction may have the same or different witness (and wtxid) as
    # this transaction." The guard has to be txid-keyed
    # (Mempool.txid_index) to reannounce here instead of refusing a
    # fullness this resubmission did not cause.
    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 1000)
    held = a_tx()
    resubmitted = replace(
        held, vin=[replace(held.vin[0], script_witness=Witness([b"\x22" * 8]))]
    )
    assert resubmitted.id == held.id
    assert resubmitted.hash != held.hash

    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(held, 1000)
    mempool.bytesize_limit = mempool.bytesize
    assert mempool.is_full()
    assert not mempool.contains_tx(resubmitted)
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    answer = send_raw_transaction(
        node, _CONN, [resubmitted.serialize(include_witness=True).hex()]
    )
    assert answer == resubmitted.id.hex()
    # the held transaction's own wtxid is what is announced, not the
    # resubmitted object's: broadcast_raw_transaction reads .hash off
    # whatever it is given, and add_tx never stored resubmitted's wtxid
    # -- announcing it would be a wtxid a getdata for it answers with
    # notfound, the defect #277 closed on the peer-to-peer path
    assert broadcast == [held]
    assert mempool.get_tx(broadcast[0].hash, wtxid=True) is held


def test_a_resubmission_under_a_different_witness_is_reannounced_by_wtxid_even_when_not_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` swaps the same wtxid off the full-mempool guard.

    A resubmission's own wtxid is never what `add_tx` stored, whether or
    not the mempool happens to be full.
    """
    # the same substitution, off the full-mempool guard entirely: a
    # resubmission's own wtxid is never what add_tx stored, whether or
    # not the mempool happens to be full
    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 1000)
    held = a_tx()
    resubmitted = replace(
        held, vin=[replace(held.vin[0], script_witness=Witness([b"\x22" * 8]))]
    )
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(held, 1000)
    assert not mempool.is_full()
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    answer = send_raw_transaction(
        node, _CONN, [resubmitted.serialize(include_witness=True).hex()]
    )
    assert answer == resubmitted.id.hex()
    assert broadcast == [held]


def a_block_index(
    chain: list[BlockHeader],
    off_chain: list[BlockHeader] | None = None,
    validated: int | None = None,
) -> Any:
    """Build an index whose lookups are two collections, as the real one's are.

    `active_chain` is the blocks this node has validated and connected,
    where `get_block_info` answers for every header the node has indexed
    -- the losing side of a fork, and a header whose block was never
    downloaded, included. A fake keying both off one list can hold
    neither, which is why btclib-org/btclib-node#87 and #178 were issues
    rather than tests.

    A height is the parent's plus one, which is how `BlockInfo.index` is
    built, so a header off the active chain is at the height its own fork
    puts it and not at a position in a chain it is not on.

    `validated` is how far along `chain` the active chain reaches, and is
    the whole of it by default.
    """

    def block(header: BlockHeader, height: int) -> Any:
        return SimpleNamespace(header=header, index=height)

    blocks = {header.hash: block(header, height) for height, header in enumerate(chain)}
    for header in off_chain or []:
        blocks[header.hash] = block(
            header, blocks[header.previous_block_hash].index + 1
        )
    connected = chain if validated is None else chain[:validated]
    # BlockIndex.chainwork, not carried on BlockInfo: btclib-org/btclib-node#201
    chainwork = {
        header_hash: block_info.index + 1 for header_hash, block_info in blocks.items()
    }
    return SimpleNamespace(
        active_chain=[header.hash for header in connected],
        get_block_info=blocks.__getitem__,
        chainwork=chainwork,
    )


def header_json(node: Any, conn: RpcConnection, params: list[Any]) -> dict[str, Any]:
    """`get_block_header`'s object answer, narrowed for a test that indexes it.

    `get_block_header` also answers a plain hex string where verbose is
    false, so its own return type is a union a test cannot index
    without narrowing first; every test below that reads a field off
    the answer calls through here rather than repeating the assertion.
    """
    result = get_block_header(node, conn, params)
    assert isinstance(result, dict)
    return result


def test_a_block_header_names_the_ones_either_side_of_it() -> None:
    """`getblockheader` verbose names a middle block's neighbours and chainwork.

    Height, confirmations, previous and next block hash, and chainwork.
    """
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    middle = header_json(node, _CONN, [chain[1].hash.hex()])
    assert middle["hash"] == chain[1].hash
    assert middle["height"] == 1
    assert middle["confirmations"] == 2
    assert middle["previousblockhash"] == chain[0].hash
    assert middle["nextblockhash"] == chain[2].hash
    assert middle["chainwork"] == 2


def test_the_first_header_has_nothing_before_it_and_the_last_nothing_after() -> None:
    """`getblockheader` omits previous/next block hash at the chain's ends."""
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    first = header_json(node, _CONN, [chain[0].hash.hex()])
    assert "previousblockhash" not in first
    assert first["nextblockhash"] == chain[1].hash

    last = header_json(node, _CONN, [chain[-1].hash.hex()])
    assert last["previousblockhash"] == chain[-2].hash
    assert "nextblockhash" not in last
    assert last["confirmations"] == 1


def test_verbose_false_answers_the_serialized_header_hex_not_the_object() -> None:
    """`getblockheader` verbose false answers the same hex sent to a peer.

    Where this node used to ignore `params[1]` and answer the object
    regardless of what was asked for (issue #215).
    """
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    answer = get_block_header(node, _CONN, [chain[1].hash.hex(), False])
    assert answer == chain[1].serialize().hex()


def test_verbose_true_and_the_default_answer_the_same_object() -> None:
    """`getblockheader`'s default, explicit true and null all answer alike.

    `verbose`'s Default is true (`src/rpc/blockchain.cpp`:617), and Core
    treats an explicit null the same as an omitted argument.
    """
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    omitted = get_block_header(node, _CONN, [chain[1].hash.hex()])
    explicit_true = get_block_header(node, _CONN, [chain[1].hash.hex(), True])
    explicit_null = get_block_header(node, _CONN, [chain[1].hash.hex(), None])
    assert omitted == explicit_true == explicit_null


def test_a_verbose_of_the_wrong_json_type_is_named_rather_than_coerced() -> None:
    """`getblockheader`'s verbose of the wrong JSON type is named, not coerced.

    The same `RPCMethod::HandleRequest` type check as blockhash's,
    against `verbose`'s own declared `RPCArg::Type::BOOL`.
    """
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    with pytest.raises(RpcError) as raised:
        get_block_header(node, _CONN, [chain[0].hash.hex(), "false"])
    assert raised.value.code == RpcErrorCode.TYPE_ERROR
    assert (
        raised.value.message == "JSON value of type string is not of expected type bool"
    )


def test_a_block_off_the_active_chain_is_described_and_not_refused() -> None:
    """`getblockheader` describes a block off the active chain, not refusing it.

    Matches what Core's `blockheaderToJSON` answers for one: the height
    the block has on its own fork, confirmations -1 in place of a depth,
    the parent it names, and no nextblockhash -- while the block the
    active chain kept at that height still answers a depth.
    """
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    fork = generate_random_header_chain(1, chain[0].hash, chain[0].time)
    node = cast(
        "Node",
        SimpleNamespace(
            chainstate=SimpleNamespace(block_index=a_block_index(chain, fork))
        ),
    )
    stale = header_json(node, _CONN, [fork[0].hash.hex()])
    assert stale["hash"] == fork[0].hash
    assert stale["height"] == 1
    assert stale["confirmations"] == -1
    assert stale["previousblockhash"] == chain[0].hash
    assert "nextblockhash" not in stale

    # the block the active chain kept at that height is another block,
    # and is answered with a depth
    best = header_json(node, _CONN, [chain[1].hash.hex()])
    assert best["hash"] != stale["hash"]
    assert best["height"] == 1
    assert best["confirmations"] == 2
    assert best["nextblockhash"] == chain[2].hash


def test_a_fork_reaching_past_the_tip_is_not_read_off_the_end_of_the_chain() -> None:
    """`getblockheader` answers a fork past the tip by its own height.

    A fork longer than the active chain is still not it, work and not
    length being what decides -- and the active chain has no position to
    answer for a height past its own.
    """
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    fork = generate_random_header_chain(3, chain[0].hash, chain[0].time)
    node = cast(
        "Node",
        SimpleNamespace(
            chainstate=SimpleNamespace(block_index=a_block_index(chain, fork))
        ),
    )
    past_the_tip = header_json(node, _CONN, [fork[-1].hash.hex()])
    assert past_the_tip["height"] == 3
    assert past_the_tip["confirmations"] == -1
    assert past_the_tip["previousblockhash"] == fork[-2].hash
    assert "nextblockhash" not in past_the_tip


def test_a_header_whose_block_is_not_validated_is_confirmed_by_nothing() -> None:
    """`getblockheader` answers -1 confirmations past the active chain's tip.

    A depth is counted from the active chain's tip, so a header this
    node has accepted and not connected is answered -1: during header
    sync the answer is that nothing is confirmed.
    """
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(
            chainstate=SimpleNamespace(block_index=a_block_index(chain, validated=1))
        ),
    )
    for header in chain[1:]:
        answer = header_json(node, _CONN, [header.hash.hex()])
        assert answer["confirmations"] == -1
        assert "nextblockhash" not in answer

    # the one block the active chain does hold is its tip, and the next
    # header is indexed without being what follows it there
    connected = header_json(node, _CONN, [chain[0].hash.hex()])
    assert connected["confirmations"] == 1
    assert "nextblockhash" not in connected


def test_a_block_hash_nothing_indexed_is_refused_rather_than_raising() -> None:
    """`getblockheader` refuses a hash the index never saw (issue #179).

    Refused rather than raised.
    """
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    with pytest.raises(RpcError) as raised:
        get_block_header(node, _CONN, ["11" * 32])
    assert raised.value.code == RpcErrorCode.INVALID_ADDRESS_OR_KEY
    assert raised.value.message == "Block not found"


def test_a_block_hash_that_is_not_hex_is_named_back_to_the_client() -> None:
    """`getblockheader`'s block hash that fails to decode as hex is echoed."""
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    with pytest.raises(RpcError) as raised:
        get_block_header(node, _CONN, ["zz"])
    assert raised.value.code == RpcErrorCode.INVALID_PARAMETER
    assert "zz" in raised.value.message


def test_a_block_hash_of_the_wrong_json_type_is_named_rather_than_faulted() -> None:
    """`getblockheader`'s block hash of the wrong type is named, not faulted.

    `bytes.fromhex(5)` raises `TypeError`, not the `ValueError` the hex
    check above catches, so a non-string blockhash used to fall through
    to the -32603 this node owes its own fault rather than the client's
    (issue #212).
    """
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    with pytest.raises(RpcError) as raised:
        get_block_header(node, _CONN, [5])
    assert raised.value.code == RpcErrorCode.TYPE_ERROR
    assert (
        raised.value.message
        == "JSON value of type number is not of expected type string"
    )


def test_a_null_block_hash_is_the_same_wrong_type_as_any_other() -> None:
    """`getblockheader` treats a null block hash as just another wrong type.

    `blockhash` is a required argument (`RPCArg::Optional::NO`), so a
    null one is not the "argument omitted" case: it is still the wrong
    JSON type, same as a number or an array would be.
    """
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    with pytest.raises(RpcError) as raised:
        get_block_header(node, _CONN, [None])
    assert raised.value.code == RpcErrorCode.TYPE_ERROR
    assert (
        raised.value.message == "JSON value of type null is not of expected type string"
    )


def test_no_block_hash_at_all_is_answered_with_the_usage() -> None:
    """`getblockheader` with no arguments is refused with its own usage."""
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    with pytest.raises(RpcError) as raised:
        get_block_header(node, _CONN, [])
    assert raised.value.code == RpcErrorCode.MISC_ERROR
    assert raised.value.message.startswith("getblockheader")


def test_the_tip_and_the_block_at_a_height_are_read_off_the_active_chain() -> None:
    """`getbestblockhash`/`getblockhash` read off the active chain's list.

    `getbestblockhash` answers the last entry, `getblockhash` the entry
    at a given height.
    """
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


def test_block_count_is_the_active_chain_s_own_last_index() -> None:
    """`getblockcount` answers the active chain's own last index, not length.

    The genesis alone is height 0, matching Core's own.
    """
    node = cast(
        "Node",
        SimpleNamespace(
            chainstate=SimpleNamespace(
                block_index=SimpleNamespace(active_chain=[b"\x00" * 32])
            )
        ),
    )
    assert get_block_count(node, _CONN, []) == 0

    node = cast(
        "Node",
        SimpleNamespace(
            chainstate=SimpleNamespace(
                block_index=SimpleNamespace(active_chain=[b"\x11" * 32, b"\x22" * 32])
            )
        ),
    )
    assert get_block_count(node, _CONN, []) == 1


def test_blockchain_info_names_the_chain_in_core_s_own_vocabulary() -> None:
    """`getblockchaininfo` names the chain in Core's own vocabulary (issue #21).

    `BitcoinCoreFetcher.assert_network` reads "chain" alone, and Core's
    own vocabulary for it is not btclib's network name -- `chains.py`'s
    `Chain.name` is "mainnet", Core answers "main".
    """
    node = cast("Node", SimpleNamespace(chain=RegTest()))
    assert get_blockchain_info(node, _CONN, []) == {"chain": "regtest"}

    node = cast("Node", SimpleNamespace(chain=Main()))
    assert get_blockchain_info(node, _CONN, []) == {"chain": "main"}


def a_chain_index_node(chain: list[bytes]) -> Node:
    """Build a node whose block index carries only an active chain list."""
    return cast(
        "Node",
        SimpleNamespace(
            chainstate=SimpleNamespace(block_index=SimpleNamespace(active_chain=chain))
        ),
    )


def test_a_negative_height_is_refused_rather_than_read_off_the_chain_s_end() -> None:
    """`getblockhash` refuses a negative height, not reading from the chain end.

    A negative index used to count from the end of `active_chain`,
    Python's own list semantics, silently answering the tip's hash for a
    height nothing asked for. Core refuses `nHeight < 0` outright
    (`src/rpc/blockchain.cpp`:600-601, issue #234).
    """
    node = a_chain_index_node([b"\x11" * 32, b"\x22" * 32])
    with pytest.raises(RpcError) as raised:
        get_block_hash(node, _CONN, [-1])
    assert raised.value.code == RpcErrorCode.INVALID_PARAMETER
    assert raised.value.message == "Block height out of range"


def test_a_height_past_the_tip_is_refused() -> None:
    """`getblockhash` refuses a height at or past the active chain's length."""
    node = a_chain_index_node([b"\x11" * 32, b"\x22" * 32])
    with pytest.raises(RpcError) as raised:
        get_block_hash(node, _CONN, [2])
    assert raised.value.code == RpcErrorCode.INVALID_PARAMETER
    assert raised.value.message == "Block height out of range"


def test_a_height_of_the_wrong_json_type_is_named_rather_than_faulted() -> None:
    """`getblockhash`'s height of any wrong JSON type is named, not faulted.

    `int(None)` raises `TypeError`, `int("x")` raises `ValueError`,
    neither caught before this fix, both reaching -32603 Internal Error
    (issue #234).
    """
    node = a_chain_index_node([b"\x11" * 32])
    for bad, type_name in (
        (None, "null"),
        ("1", "string"),
        ([1], "array"),
        (True, "bool"),
    ):
        with pytest.raises(RpcError) as raised:
            get_block_hash(node, _CONN, [bad])
        assert raised.value.code == RpcErrorCode.TYPE_ERROR
        assert raised.value.message == (
            f"JSON value of type {type_name} is not of expected type number"
        )


def test_a_fractional_height_is_refused_the_way_core_s_own_parse_refuses_it() -> None:
    """`getblockhash` refuses a fractional height with MISC_ERROR, as Core does.

    A JSON number written with a decimal point is still VNUM, so it
    passes the type check the way an int does, but
    `UniValue::getInt<int>()` fails on it regardless of its value --
    `RPC_MISC_ERROR`, not `RPC_TYPE_ERROR` (`src/rpc/server.cpp`
    :884-886).
    """
    node = a_chain_index_node([b"\x11" * 32])
    with pytest.raises(RpcError) as raised:
        get_block_hash(node, _CONN, [1.0])
    assert raised.value.code == RpcErrorCode.MISC_ERROR
    assert raised.value.message == "JSON integer out of range"


def test_no_height_at_all_is_answered_with_the_usage() -> None:
    """`getblockhash` with no arguments at all is refused with its own usage.

    Unquoted: `RPCArg::ToString(oneline=true)` quotes an argument's name
    only for `Type::STR`/`STR_HEX`, and height is `Type::NUM` -- unlike
    blockhash's own quoted usage string, which is `STR_HEX`.
    """
    node = a_chain_index_node([b"\x11" * 32])
    with pytest.raises(RpcError) as raised:
        get_block_hash(node, _CONN, [])
    assert raised.value.code == RpcErrorCode.MISC_ERROR
    assert raised.value.message == "getblockhash height"


def test_a_transaction_whose_scripts_do_not_verify_is_answered_with_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` answers VERIFY_REJECTED for a bad-script tx.

    Does not add it to the mempool or broadcast it.
    """

    def invalid(node: Any, transaction: Any) -> NoReturn:
        raise BTClibValueError("no")

    monkeypatch.setattr(cb, "verify_mempool_acceptance", invalid)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    with pytest.raises(RpcError) as raised:
        send_raw_transaction(node, _CONN, [tx.serialize(include_witness=True).hex()])
    assert raised.value.code == RpcErrorCode.VERIFY_REJECTED
    assert raised.value.message == "Invalid signatures or script"
    assert not mempool.contains_tx(tx)
    assert broadcast == []
