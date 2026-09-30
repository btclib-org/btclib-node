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

import math
import time
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast, override

import pytest
from bitcoin_core_rpc import RPCErrorCode
from btclib.block import Block, BlockHeader
from btclib.consensus import MAX_BLOCK_WEIGHT
from btclib.descriptors import add_checksum, from_address
from btclib.fee import FeeRate
from btclib.key import PrvKeyData
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.p2p.limits import PROTOCOL_VERSION
from btclib.script import script
from btclib.script.script_pub_key import ScriptPubKey, p2ms_m_and_keys, type_and_payload
from btclib.script.witness import Witness
from btclib.tx.limits import COINBASE_MATURITY
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

import btclib_node.p2p.banman as banman_module
import btclib_node.rpc.callbacks as cb
from btclib_node.block_db import Coin
from btclib_node.chains import Chain, Main, RegTest
from btclib_node.chainstate.block_index import BlockStatus, block_time, calculate_work
from btclib_node.chainstate.muhash import CoinStats
from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE
from btclib_node.constants import (
    MIN_BLOCKS_TO_KEEP,
    MIN_PRUNE_TARGET_MIB,
    USER_AGENT,
    P2pConnStatus,
)
from btclib_node.exceptions import (
    ChainstateInconsistencyError,
    MissingPrevoutError,
    StoreCorruptionError,
    TxRejectedError,
)
from btclib_node.log import Logger
from btclib_node.main import MempoolAcceptance, verify_mempool_acceptance
from btclib_node.mempool import Mempool
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.banman import BanEntry, BanMan, lookup_subnet
from btclib_node.p2p.block_availability import BlockAvailability
from btclib_node.p2p.connection import PeerStats
from btclib_node.rpc.callbacks import (
    add_node,
    callbacks,
    clear_banned,
    disconnect_node,
    get_best_block_hash,
    get_block,
    get_block_count,
    get_block_hash,
    get_block_header,
    get_blockchain_info,
    get_chain_tips,
    get_connection_count,
    get_mempool_info,
    get_network_info,
    get_peer_info,
    get_raw_mempool,
    get_raw_transaction,
    get_tx_out,
    get_tx_out_set_info,
    help_rpc,
    list_banned,
    ping,
    prune_blockchain,
    send_raw_transaction,
    service_names,
    set_ban,
    stop,
    submit_block,
)

# aliased: pytest collects a module-level `test*` as a test, and this
# one is a production function that would be handed fixtures
from btclib_node.rpc.callbacks import test_mempool_accept as mempool_accept
from btclib_node.rpc.connection import RawJSON
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import HELP_TEXT, answer_help
from tests import (
    build_block,
    generate_coinbase,
    generate_random_chain,
    generate_random_header_chain,
    generate_random_transaction,
    generate_segwit_block,
)
from tests.unit.main_test import connect, locked_spend, spend

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

# none of these callbacks reads the connection it is handed
_CONN = cast("RpcConnection", None)

# a fixed key, the identical one `tests/unit/interpreter_test.py`'s own
# `_PRV`/`_PUB_KEY` already use, for building a p2pk/p2ms/p2tr `ScriptPubKey`
_TEST_PUB_KEY = PrvKeyData(
    0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF
).pub


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
    user_agent: bytes = b"/btclib:test/",
    latency: float = 0.5,
    min_ping_time: float = 0.25,
    ping_sent: float = 0,
    relay: bool = True,
    inbound: bool = True,
    automatic: bool = False,
    block_relay: bool = False,
    feeler: bool = False,
    addr_fetch: bool = False,
    versioned: bool = True,
) -> Any:
    """Build a `P2pManager.connections` entry `get_peer_info` can read.

    `peer`, `bind` and `local` each default to a different host, so an
    assertion naming the wrong one of the three cannot pass by accident.
    `versioned` false is a connection whose `version` has not arrived.
    """
    version_message = SimpleNamespace(
        services=ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS,
        addr_recv=NetworkAddress(0, local, 8333),
        version=70015,
        user_agent=user_agent,
        is_relay_requested=relay,
    )
    return SimpleNamespace(
        status=status,
        client=FakeSocket(gone=gone, peer=peer, bind=bind),
        version_message=version_message if versioned else None,
        address=peer_address(peer, 8333),
        # fractional where the connection keeps them so, and each a
        # different value, so that an answer naming the wrong source or
        # left unrounded cannot pass
        last_send=1.9,
        last_receive=2.7,
        last_novel_block_time=4,
        last_novel_tx_time=5,
        connected_time=6,
        latency=latency,
        min_ping_time=min_ping_time,
        ping_sent=ping_sent,
        inbound=inbound,
        automatic=automatic,
        block_relay=block_relay,
        feeler=feeler,
        addr_fetch=addr_fetch,
        stats=PeerStats(),
        block_availability=BlockAvailability(),
        tx_announce_queue=[],
        download_queue=[],
        feefilter=0,
        # what `Connection` starts every connection at
        addr_relay_enabled=False,
    )


def a_node(
    peers: dict[int, Any] | None = None,
    mempool: Mempool | None = None,
    accept: Any = None,
    pending: dict[int, Any] | None = None,
    min_relay_feerate: FeeRate = DEFAULT_MIN_RELAY_FEERATE,
    *,
    heights: dict[bytes, int] | None = None,
    pruned: bool = False,
    peerblockfilters: bool = False,
) -> Any:
    """Build a `Node` double carrying only what these callbacks read.

    A peer table, a mempool, the configured minimum relay feerate, and a
    block index answering the height of each hash in `heights` --
    nothing else these tests' own callbacks look at. `pruned` and
    `peerblockfilters` are `p2p.connection.local_services`'s own, off by
    default here as `Config`'s own defaults are, for
    `get_network_info`'s `localservices`/`localservicesnames`.
    """
    known = heights if heights is not None else {}
    return SimpleNamespace(
        chainstate=SimpleNamespace(
            block_index=SimpleNamespace(
                get_block_info=lambda block_hash: SimpleNamespace(
                    index=known[block_hash]
                )
            )
        ),
        p2p_manager=SimpleNamespace(
            connections=peers if peers is not None else {},
            pending_connections=pending if pending is not None else {},
            ping_all=lambda: None,
        ),
        mempool=mempool if mempool is not None else Mempool(Logger(debug=True)),
        config=SimpleNamespace(
            min_relay_feerate=min_relay_feerate,
            pruned=pruned,
            peerblockfilters=peerblockfilters,
        ),
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


def test_a_peer_s_subver_is_its_own_announced_user_agent() -> None:
    """`getpeerinfo`'s `subver` is the wire bytes the peer's `version` carried.

    `connect_nodes` (`test_framework.py:568-594`, at
    bitcoin/bitcoin@bb529657) matches this against the local node's own
    `getnetworkinfo`-reported `subversion` to find itself in a peer's
    own list.
    """
    node = a_node({7: a_peer(user_agent=b"/btclib:2026.9/")})
    (info,) = get_peer_info(node, _CONN, [])
    assert info["subver"] == "/btclib:2026.9/"


def test_a_v6_peer_is_named_with_the_brackets_core_writes() -> None:
    """`getpeerinfo` brackets every IPv6 address in its answer (issue #147).

    Without the brackets, `2001:db8::1` on port 8333 and `2001:db8::1:8333`
    on another port are the same string, and a client splitting on the
    last colon reads one of the two wrong.
    """
    node = a_node(
        {7: a_peer(peer="2001:db8::1", bind="2001:db8::2", local="2a01:4f8::3")}
    )
    (info,) = get_peer_info(node, _CONN, [])
    assert info["addr"] == "[2001:db8::1]:8333"
    assert info["addrbind"] == "[2001:db8::2]:18444"
    assert info["addrlocal"] == "[2a01:4f8::3]:8333"


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


def test_the_time_fields_are_core_s_whole_seconds() -> None:
    """`getpeerinfo`'s times are whole seconds, and its block the novel one.

    `lastsend`, `lastrecv`, `last_transaction`, `last_block` and
    `conntime` are integers, pushed for every peer, and `last_block` is
    the last novel block rather than the stall check's timestamp.
    """
    (info,) = get_peer_info(a_node({7: a_peer()}), _CONN, [])
    keys = ("lastsend", "lastrecv", "last_transaction", "last_block", "conntime")
    times = {key: info[key] for key in keys}
    assert times == {
        "lastsend": 1,
        "lastrecv": 2,
        "last_transaction": 5,
        "last_block": 4,
        "conntime": 6,
    }
    assert all(type(value) is int for value in times.values())


def test_no_ping_field_is_answered_before_a_ping_is_sent() -> None:
    """`pingtime`, `minping` and `pingwait` are all absent with no ping yet."""
    peer = a_peer(latency=0, min_ping_time=math.inf, ping_sent=0)
    (info,) = get_peer_info(a_node({7: peer}), _CONN, [])
    assert "pingtime" not in info
    assert "minping" not in info
    assert "pingwait" not in info


def test_a_ping_answered_reports_its_round_trips_and_no_wait() -> None:
    """After a pong, `pingtime` and `minping` are answered, `pingwait` not."""
    (info,) = get_peer_info(a_node({7: a_peer()}), _CONN, [])
    assert info["pingtime"] == 0.5
    assert info["minping"] == 0.25
    assert "pingwait" not in info


def test_a_ping_outstanding_reports_how_long_it_has_waited() -> None:
    """`pingwait` is the seconds since the outstanding ping was sent."""
    before = time.time()
    peer = a_peer(latency=0, min_ping_time=math.inf, ping_sent=before - 3)
    (info,) = get_peer_info(a_node({7: peer}), _CONN, [])
    assert 3 <= info["pingwait"] <= 3 + time.time() - before
    assert "pingtime" not in info
    assert "minping" not in info


def test_a_ping_sent_after_now_reports_no_wait() -> None:
    """A wall clock stepped back past the ping answers no `pingwait`.

    Core pushes `pingwait` only where it is positive.
    """
    peer = a_peer(ping_sent=time.time() + 3600)
    (info,) = get_peer_info(a_node({7: peer}), _CONN, [])
    assert "pingwait" not in info


def test_a_peer_still_handshaking_is_in_the_table_in_id_order() -> None:
    """`getpeerinfo` lists a connection short of `verack`, as Core does.

    Pending and handshake-complete connections are one table, ordered by
    connection id as Core's `m_nodes` is by when each was opened.
    """
    node = a_node(
        {5: a_peer(), 9: a_peer()},
        pending={7: a_peer(P2pConnStatus.Open), 3: a_peer(P2pConnStatus.Open)},
    )
    assert [info["id"] for info in get_peer_info(node, _CONN, [])] == [3, 5, 7, 9]


def test_a_peer_before_its_version_answers_core_s_defaults() -> None:
    """Before `version`: no `addrlocal`, zero services and version, no relay."""
    node = a_node(pending={7: a_peer(P2pConnStatus.Open, versioned=False)})
    (info,) = get_peer_info(node, _CONN, [])
    assert "addrlocal" not in info
    assert info["services"] == "0000000000000000"
    assert info["servicesnames"] == []
    assert info["version"] == 0
    assert info["subver"] == ""
    assert info["relaytxes"] is False
    assert info["last_inv_sequence"] == 0
    assert info["timeoffset"] == 0
    assert info["addr_relay_enabled"] is False


@pytest.mark.parametrize(
    ("peer", "network"),
    [
        ("1.2.3.4", "ipv4"),
        ("127.0.0.1", "not_publicly_routable"),
        ("10.0.0.1", "not_publicly_routable"),
        ("::1", "not_publicly_routable"),
        ("2a01:4f8::1", "ipv6"),
        # 6to4, which carries an IPv4 address
        ("2002:102:304::1", "ipv4"),
        ("fd6b:88c0:8724::1", "internal"),
    ],
)
def test_the_network_is_core_s_net_class(peer: str, network: str) -> None:
    """`network` is `GetNetClass` of the peer, not the BIP155 id it came by."""
    (info,) = get_peer_info(a_node({7: a_peer(peer=peer)}), _CONN, [])
    assert info["network"] == network


# addresses a peer names, none of them bound to
@pytest.mark.parametrize(
    "local",
    ["::", "0.0.0.0", "255.255.255.255", "2001:db8::1"],  # noqa: S104
)
def test_an_invalid_local_address_leaves_addrlocal_out(local: str) -> None:
    """Core pushes `addrlocal` only where the peer named a valid address."""
    (info,) = get_peer_info(a_node({7: a_peer(local=local)}), _CONN, [])
    assert "addrlocal" not in info


def test_a_loopback_local_address_is_kept() -> None:
    """A loopback address is valid, only not routable, and is answered."""
    (info,) = get_peer_info(a_node({7: a_peer(local="127.0.0.1")}), _CONN, [])
    assert info["addrlocal"] == "127.0.0.1:8333"


def test_every_key_core_pushes_for_every_peer_is_answered() -> None:
    """The unconditional keys of Core's `getpeerinfo`, in Core's order.

    `addrlocal` and the ping fields follow here where Core pushes them.
    """
    peer = a_peer(latency=0, min_ping_time=math.inf)
    (info,) = get_peer_info(a_node({7: peer}), _CONN, [])
    assert list(info) == [
        "id",
        "addr",
        "addrbind",
        "addrlocal",
        "network",
        "services",
        "servicesnames",
        "relaytxes",
        "last_inv_sequence",
        "inv_to_send",
        "lastsend",
        "lastrecv",
        "last_transaction",
        "last_block",
        "bytessent",
        "bytesrecv",
        "conntime",
        "timeoffset",
        "version",
        "subver",
        "inbound",
        "bip152_hb_to",
        "bip152_hb_from",
        "presynced_headers",
        "synced_headers",
        "synced_blocks",
        "inflight",
        "addr_relay_enabled",
        "addr_processed",
        "addr_rate_limited",
        "permissions",
        "minfeefilter",
        "bytessent_per_msg",
        "bytesrecv_per_msg",
        "connection_type",
        "transport_protocol_type",
        "session_id",
    ]


def test_the_fields_this_node_keeps_state_for_read_that_state() -> None:
    """Relay, traffic, clock and block download fields come off the peer."""
    peer = a_peer()
    peer.stats = PeerStats(
        time_offset=-3,
        last_inv_sequence=42,
        addr_processed=5,
        addr_rate_limited=6,
        bytes_sent=100,
        bytes_recv=200,
        bytes_sent_per_msg=Counter({"version": 60, "ping": 40}),
        bytes_recv_per_msg=Counter({"verack": 24, "*other*": 176}),
    )
    peer.tx_announce_queue = [b"\x01" * 32, b"\x02" * 32]
    peer.download_queue = [b"\x0b" * 32, b"\x0a" * 32]
    peer.feefilter = 1234
    peer.addr_relay_enabled = True
    node = a_node({7: peer}, heights={b"\x0a" * 32: 10, b"\x0b" * 32: 11})
    (info,) = get_peer_info(node, _CONN, [])
    assert info["relaytxes"] is True
    assert info["last_inv_sequence"] == 42
    assert info["inv_to_send"] == 2
    assert info["bytessent"] == 100
    assert info["bytesrecv"] == 200
    assert info["timeoffset"] == -3
    # in the order they were asked for
    assert info["inflight"] == [11, 10]
    assert info["addr_relay_enabled"] is True
    assert info["addr_processed"] == 5
    assert info["addr_rate_limited"] == 6
    assert info["minfeefilter"].text == "0.00001234"
    # in key order, as Core's `std::map` iterates
    assert list(info["bytessent_per_msg"].items()) == [("ping", 40), ("version", 60)]
    assert list(info["bytesrecv_per_msg"].items()) == [("*other*", 176), ("verack", 24)]


def test_a_peer_that_asked_for_no_relay_has_no_tx_relay() -> None:
    """Core's `TxRelay`-backed fields answer 0 and false for such a peer."""
    peer = a_peer(relay=False)
    peer.stats = PeerStats(last_inv_sequence=42)
    peer.tx_announce_queue = [b"\x01" * 32]
    peer.feefilter = 1234
    (info,) = get_peer_info(a_node({7: peer}), _CONN, [])
    assert info["relaytxes"] is False
    assert info["last_inv_sequence"] == 0
    assert info["inv_to_send"] == 0
    assert info["minfeefilter"].text == "0.00000000"


@pytest.mark.parametrize(
    ("inbound", "automatic", "block_relay", "feeler", "addr_fetch", "connection_type"),
    [
        (True, False, False, False, False, "inbound"),
        (False, True, False, False, False, "outbound-full-relay"),
        (False, True, True, False, False, "block-relay-only"),
        (False, True, False, True, False, "feeler"),
        (False, False, False, False, True, "addr-fetch"),
        (False, False, False, False, False, "manual"),
    ],
)
def test_the_connection_type_is_core_s(  # noqa: PLR0917
    inbound: bool,  # noqa: FBT001
    automatic: bool,  # noqa: FBT001
    block_relay: bool,  # noqa: FBT001
    feeler: bool,  # noqa: FBT001
    addr_fetch: bool,  # noqa: FBT001
    connection_type: str,
) -> None:
    """Inbound, drawn by this node as any kind, or named by an operator."""
    peer = a_peer(
        inbound=inbound,
        automatic=automatic,
        block_relay=block_relay,
        feeler=feeler,
        addr_fetch=addr_fetch,
    )
    (info,) = get_peer_info(a_node({7: peer}), _CONN, [])
    assert info["connection_type"] == connection_type


def test_a_block_relay_only_peer_has_no_tx_relay_nor_addr_relay() -> None:
    """ISS 1095: no `TxRelay` whatever it asked for, and no address relay.

    Core's `TxRelay`-backed fields answer 0 and false, and
    `m_addr_relay_enabled` stays false, `SetupAddressRelay` refusing it.
    """
    peer = a_peer(inbound=False, automatic=True, block_relay=True, relay=True)
    peer.stats = PeerStats(last_inv_sequence=42)
    peer.tx_announce_queue = [b"\x01" * 32]
    peer.feefilter = 1234
    (info,) = get_peer_info(a_node({7: peer}), _CONN, [])
    assert info["relaytxes"] is False
    assert info["last_inv_sequence"] == 0
    assert info["inv_to_send"] == 0
    assert info["minfeefilter"].text == "0.00000000"
    assert info["addr_relay_enabled"] is False


@pytest.mark.parametrize(
    ("best_known", "last_common", "expected"),
    [
        (None, None, (-1, -1)),
        (b"\x0c" * 32, None, (12, -1)),
        (b"\x0c" * 32, b"\x0a" * 32, (12, 10)),
    ],
    ids=["neither", "best-known-only", "both"],
)
def test_the_synced_heights_are_the_peer_s_best_known_and_last_common_blocks(
    best_known: bytes | None,
    last_common: bytes | None,
    expected: tuple[int, int],
) -> None:
    """`synced_headers` and `synced_blocks` are heights, -1 where unset.

    Core's `pindexBestKnownBlock` and `pindexLastCommonBlock`
    (btclib-org/btclib-node#1105).
    """
    peer = a_peer()
    peer.block_availability = BlockAvailability(
        best_known=best_known, last_common=last_common
    )
    node = a_node({7: peer}, heights={b"\x0a" * 32: 10, b"\x0c" * 32: 12})
    (info,) = get_peer_info(node, _CONN, [])
    assert (info["synced_headers"], info["synced_blocks"]) == expected


def test_the_fields_this_node_has_no_state_for_answer_core_s_value() -> None:
    """No `cmpctblock` announcing, presync, permissions or BIP324 here."""
    (info,) = get_peer_info(a_node({7: a_peer()}), _CONN, [])
    assert info["bip152_hb_to"] is False
    assert info["bip152_hb_from"] is False
    assert info["presynced_headers"] == -1
    assert info["permissions"] == []
    assert info["transport_protocol_type"] == "v1"
    assert info["session_id"] == ""


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

    `get_peer_info` loops over a list it built before starting, so a
    connection `remove_connection` pops mid-loop -- it runs on
    `P2pManager`'s own loop, this on `Node`'s, every pass of
    `manage_connections` -- does not raise `RuntimeError: dictionary
    changed size during iteration` out of a live dict's iterator
    noticing the pop instead. (issue #356)
    """
    connections: dict[int, Any] = {}

    class PoppingOnIter(list[bytes]):
        """`p2p_conn.download_queue`, which `inflight` iterates.

        Standing in for whatever this node's loop is doing when
        `remove_connection` reaches in: the pop happens as a side
        effect of building peer 7's entry, between the iterator's
        own `next()` for peer 7 and its `next()` for peer 8 -- mid-loop
        on a live dict, and not reachable at all from a loop over a list
        built before it started.
        """

        @override
        def __iter__(self) -> Iterator[bytes]:
            connections.pop(8, None)
            return super().__iter__()

    connections[7] = a_peer()
    connections[7].download_queue = PoppingOnIter()
    connections[8] = a_peer()
    node = a_node(connections)
    # peer 8 is popped from the live `connections` above, not from the
    # list this call iterates -- so the list still answers for it,
    # unaffected by a pop reaching the dict it was built from
    assert [info["id"] for info in get_peer_info(node, _CONN, [])] == [7, 8]


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

    Each entry names its own wtxid, vsize and weight; the vsize is the one
    the entry was added with, Core's sigop-adjusted `GetTxSize`
    (btclib-org/btclib-node#1357).
    """
    mempool = Mempool(Logger(debug=True))
    tx = a_tx()
    mempool.add_tx(tx, 0, tx.vsize + 7)
    node = a_node(mempool=mempool)

    verbose = get_raw_mempool(node, _CONN, [True])
    assert isinstance(verbose, dict)
    assert list(verbose) == [tx.id.hex()]
    assert verbose[tx.id.hex()]["wtxid"] == tx.hash.hex()
    assert verbose[tx.id.hex()]["vsize"] == tx.vsize + 7
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
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
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
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 1 (verbose)": "JSON value of '
        'type string is not of expected type bool"\n}'
    )

    with pytest.raises(RpcError) as raised:
        get_raw_mempool(node, _CONN, [False, 1])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 2 (mempool_sequence)": "JSON '
        'value of type number is not of expected type bool"\n}'
    )

    # ISS 1293: both wrong at once are both named, not the first alone
    with pytest.raises(RpcError) as raised:
        get_raw_mempool(node, _CONN, ["true", 1])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        "Wrong type passed:\n"
        "{\n"
        '    "Position 1 (verbose)": "JSON value of type string is not'
        ' of expected type bool",\n'
        '    "Position 2 (mempool_sequence)": "JSON value of type number'
        ' is not of expected type bool"\n'
        "}"
    )


def _a_coin_stats_coin(value: int = 1000) -> tuple[bytes, Coin]:
    out_point_bytes = OutPoint(b"\x33" * 32, 0, check_validity=False).serialize(
        check_validity=False
    )
    tx_out = TxOut(value=value, script_pub_key=script.serialize(["OP_1"]))
    return out_point_bytes, Coin(tx_out, height=1, is_coinbase=False)


def a_coin_stats_node(coin_stats: CoinStats, chain: list[bytes]) -> Any:
    """Build a node whose chainstate carries a real `CoinStats`.

    `get_tx_out_set_info` reads `chainstate.block_index.active_chain`
    for `height`/`bestblock` and `chainstate.utxo_index.coin_stats` for
    everything else, matching `a_chain_index_node`'s own minimal shape.
    """
    return SimpleNamespace(
        chainstate=SimpleNamespace(
            block_index=SimpleNamespace(active_chain=chain),
            utxo_index=SimpleNamespace(coin_stats=coin_stats),
        )
    )


def test_tx_out_set_info_answers_core_s_own_field_names() -> None:
    """`gettxoutsetinfo` answers height, bestblock, txouts, bogosize, amount.

    `hash_type: "muhash"` also carries `muhash`, the digest reversed to
    match `uint256::GetHex()` rather than `CoinStats.digest`'s own
    byte order.
    """
    coin_stats = CoinStats()
    coin_stats.insert(*_a_coin_stats_coin(value=5_000_000_000))
    chain = [b"\x11" * 32, b"\x22" * 32]
    node = a_coin_stats_node(coin_stats, chain)

    result = get_tx_out_set_info(node, _CONN, ["muhash"])
    assert result["height"] == 1
    assert result["bestblock"] == chain[-1]
    assert result["txouts"] == 1
    assert result["bogosize"] == coin_stats.bogo_size
    assert result["total_amount"].text == "50.00000000"
    assert result["muhash"] == coin_stats.digest[::-1]


def test_tx_out_set_info_hash_type_none_omits_the_muhash_field() -> None:
    """`hash_type: "none"` answers every field but `muhash` itself."""
    coin_stats = CoinStats()
    coin_stats.insert(*_a_coin_stats_coin())
    node = a_coin_stats_node(coin_stats, [b"\x11" * 32])

    result = get_tx_out_set_info(node, _CONN, ["none"])
    assert "muhash" not in result
    assert result["txouts"] == 1


def test_tx_out_set_info_defaults_to_muhash_not_hash_serialized_3() -> None:
    """With no `hash_type` given, a bare call answers `muhash`, not Core's own.

    This tree answers only from `CoinStats`, never from a live scan, so
    `hash_serialized_3` -- Core's own default -- has nothing to compute
    it from; the departure is this tree's own default, argued in
    `get_tx_out_set_info`'s own docstring.
    """
    coin_stats = CoinStats()
    coin_stats.insert(*_a_coin_stats_coin())
    node = a_coin_stats_node(coin_stats, [b"\x11" * 32])

    bare = get_tx_out_set_info(node, _CONN, [])
    explicit = get_tx_out_set_info(node, _CONN, ["muhash"])
    bare["total_amount"] = bare["total_amount"].text
    explicit["total_amount"] = explicit["total_amount"].text
    assert bare == explicit
    assert "muhash" in bare


def test_tx_out_set_info_still_refuses_hash_serialized_3_asked_for_by_name() -> None:
    """Asking for `hash_serialized_3` explicitly is refused as before.

    Only the *default* moved; the hash type itself is still one this
    tree has no accumulator for.
    """
    node = a_coin_stats_node(CoinStats(), [b"\x11" * 32])
    with pytest.raises(RpcError) as raised:
        get_tx_out_set_info(node, _CONN, ["hash_serialized_3"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == "'hash_serialized_3' is not a valid hash_type"


def test_tx_out_set_info_refuses_an_unknown_hash_type_by_name() -> None:
    """An unrecognized `hash_type` is refused and named back."""
    node = a_coin_stats_node(CoinStats(), [b"\x11" * 32])
    with pytest.raises(RpcError) as raised:
        get_tx_out_set_info(node, _CONN, ["not_a_real_type"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == "'not_a_real_type' is not a valid hash_type"


def test_tx_out_set_info_hash_type_of_the_wrong_json_type_is_named() -> None:
    """A non-string `hash_type` is named the way `type_error` names it."""
    node = a_coin_stats_node(CoinStats(), [b"\x11" * 32])
    with pytest.raises(RpcError) as raised:
        get_tx_out_set_info(node, _CONN, [1])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 1 (hash_type)": "JSON value '
        'of type number is not of expected type string"\n}'
    )


def test_tx_out_set_info_names_both_wrongly_typed_arguments_at_once() -> None:
    """ISS 1293: a wrong `hash_type` and a wrong `use_index` are both named."""
    node = a_coin_stats_node(CoinStats(), [b"\x11" * 32])
    with pytest.raises(RpcError) as raised:
        get_tx_out_set_info(node, _CONN, [1, None, "yes"])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        "Wrong type passed:\n"
        "{\n"
        '    "Position 1 (hash_type)": "JSON value of type number is not'
        ' of expected type string",\n'
        '    "Position 3 (use_index)": "JSON value of type string is not'
        ' of expected type bool"\n'
        "}"
    )


def test_tx_out_set_info_refuses_a_specific_block_the_way_an_unindexed_node_does() -> (
    None
):
    """`hash_or_height` is refused: no index keeps an earlier block's stats.

    The same refusal an ordinary `bitcoind`, run without
    `-coinstatsindex`, already answers with.
    """
    node = a_coin_stats_node(CoinStats(), [b"\x11" * 32])
    with pytest.raises(RpcError) as raised:
        get_tx_out_set_info(node, _CONN, ["muhash", 5])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == (
        "Querying specific block heights requires coinstatsindex"
    )


def test_tx_out_set_info_use_index_is_type_checked_but_changes_nothing() -> None:
    """`use_index` is validated like Core's own BOOL argument, then ignored.

    There is no non-indexed path in this tree for it to switch onto --
    `CoinStats` is the only one there is, so a valid boolean changes no
    field of the answer, and an invalid one is still refused by type.
    """
    coin_stats = CoinStats()
    coin_stats.insert(*_a_coin_stats_coin())
    node = a_coin_stats_node(coin_stats, [b"\x11" * 32])

    with_index = get_tx_out_set_info(node, _CONN, ["none", None, True])
    without_index = get_tx_out_set_info(node, _CONN, ["none", None, False])
    with_index["total_amount"] = with_index["total_amount"].text
    without_index["total_amount"] = without_index["total_amount"].text
    assert with_index == without_index

    with pytest.raises(RpcError) as raised:
        get_tx_out_set_info(node, _CONN, ["none", None, "true"])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR


def test_get_tx_out_answers_a_confirmed_coin(
    regtest_node: Callable[..., Node],
) -> None:
    """`gettxout` answers Core's own shape for a coin still in the UTXO set."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    coinbase = chain[0].transactions[0]

    answer = get_tx_out(node, _CONN, [coinbase.id.hex(), 0])

    assert answer is not None
    assert answer["bestblock"] == chain[0].header.hash
    assert answer["confirmations"] == 1
    assert answer["value"].text == "50.00000000"
    assert answer["coinbase"] is True
    assert answer["scriptPubKey"]["hex"] == coinbase.vout[0].script_pub_key.script.hex()
    assert answer["scriptPubKey"]["type"] == "nonstandard"
    assert "address" not in answer["scriptPubKey"]


def test_get_tx_out_answers_none_for_an_unknown_outpoint(
    regtest_node: Callable[..., Node],
) -> None:
    """An outpoint this node never created answers `None`, not an error."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)

    assert get_tx_out(node, _CONN, [(b"\x11" * 32).hex(), 0]) is None


def test_get_tx_out_include_mempool_hides_a_coin_a_mempool_tx_spends(
    regtest_node: Callable[..., Node],
) -> None:
    """`include_mempool` (true by default) answers null once mempool spends it.

    `include_mempool: false` still answers the confirmed coin, matching
    Core's own `fMempool` branch reading straight from `coins_view`.
    """
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    coinbase = chain[0].transactions[0]
    spend = generate_random_transaction(coinbase.id, value=coinbase.vout[0].value)
    node.mempool.add_tx(spend, fee=0)

    assert get_tx_out(node, _CONN, [coinbase.id.hex(), 0]) is None
    assert get_tx_out(node, _CONN, [coinbase.id.hex(), 0, True]) is None
    still_confirmed = get_tx_out(node, _CONN, [coinbase.id.hex(), 0, False])
    assert still_confirmed is not None
    assert still_confirmed["confirmations"] == 1


def test_get_tx_out_include_mempool_walks_past_a_sibling_output_s_spend(
    regtest_node: Callable[..., Node],
) -> None:
    """`Mempool.spent_by` keys on the parent txid, `isSpent` on the outpoint.

    A query for one output walks past a mempool spend of a different
    one of that parent's own outputs before answering -- `_mempool_spends`'s
    own docstring names this scan, unlike Core's O(1) `mapNextTx` lookup.
    """
    node = regtest_node()
    parent_txid = b"\x33" * 32
    spend0 = generate_random_transaction(parent_txid)
    spend1 = generate_random_transaction(parent_txid)
    spend1.vin[0].prev_out = OutPoint(parent_txid, 1)
    node.mempool.add_tx(spend0, fee=0)
    node.mempool.add_tx(spend1, fee=0)

    assert get_tx_out(node, _CONN, [parent_txid.hex(), 2]) is None


def test_get_tx_out_include_mempool_answers_a_coin_a_mempool_tx_creates(
    regtest_node: Callable[..., Node],
) -> None:
    """A mempool transaction's own output answers, at `confirmations: 0`.

    Not yet in the confirmed UTXO set at all: `include_mempool: false`
    answers `None` for the identical outpoint.
    """
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    coinbase = chain[0].transactions[0]
    spend = generate_random_transaction(coinbase.id, value=coinbase.vout[0].value)
    node.mempool.add_tx(spend, fee=0)

    mempool_only = get_tx_out(node, _CONN, [spend.id.hex(), 0])
    assert mempool_only is not None
    assert mempool_only["confirmations"] == 0
    assert mempool_only["coinbase"] is False
    assert mempool_only["bestblock"] == chain[0].header.hash

    assert get_tx_out(node, _CONN, [spend.id.hex(), 0, False]) is None


def test_get_tx_out_include_mempool_answers_none_past_a_mempool_tx_s_own_outputs(
    regtest_node: Callable[..., Node],
) -> None:
    """A vout past a mempool-only transaction's own `vout` list answers `None`.

    Matching Core's own `CCoinsViewMemPool::GetCoin` for an outpoint whose
    own txid is a mempool transaction but whose index is not one of its
    outputs -- neither confirmed nor a mempool output to answer with.
    """
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    coinbase = chain[0].transactions[0]
    spend = generate_random_transaction(coinbase.id, value=coinbase.vout[0].value)
    node.mempool.add_tx(spend, fee=0)

    assert len(spend.vout) == 1
    assert get_tx_out(node, _CONN, [spend.id.hex(), 1]) is None


def test_get_tx_out_answers_core_s_nested_scriptpubkey_for_a_p2pkh_coin(
    regtest_node: Callable[..., Node],
) -> None:
    """A p2pkh coin's own `scriptPubKey` nests Core's own `type` and `address`.

    Staged directly into `UtxoIndex.updated_utxo_set`, ahead of any
    `finalize`: `UtxoIndex.get_coin` reads that dict before the store,
    so this is real UTXO-index state and not a double standing in for
    one.
    """
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)

    raw_script = bytes.fromhex("76a914") + b"\x11" * 20 + bytes.fromhex("88ac")
    script_pub_key = ScriptPubKey(raw_script)
    out_point = OutPoint(b"\x22" * 32, 0)
    coin = Coin(TxOut(1234, script_pub_key), height=1, is_coinbase=False)
    node.chainstate.utxo_index.updated_utxo_set[
        out_point.serialize(check_validity=False)
    ] = coin

    answer = get_tx_out(node, _CONN, [out_point.tx_id.hex(), out_point.vout])

    assert answer is not None
    script_pub_key_dict = answer["scriptPubKey"]
    assert script_pub_key_dict["type"] == "pubkeyhash"
    assert script_pub_key_dict["hex"] == raw_script.hex()
    assert script_pub_key_dict["address"] == script_pub_key.address
    assert script_pub_key_dict["desc"] == from_address(script_pub_key.address)


def _stage_coin_and_get_tx_out(
    node: Node, script_pub_key: ScriptPubKey
) -> dict[str, Any]:
    """Stage `script_pub_key`'s own coin into `UtxoIndex` and answer `gettxout`.

    The identical staging `test_get_tx_out_answers_core_s_nested_scriptpubkey_
    for_a_p2pkh_coin` above does, factored out for the three script types
    that test's own `desc` does not exercise.
    """
    out_point = OutPoint(b"\x22" * 32, 0)
    coin = Coin(TxOut(1234, script_pub_key), height=1, is_coinbase=False)
    node.chainstate.utxo_index.updated_utxo_set[
        out_point.serialize(check_validity=False)
    ] = coin
    answer = get_tx_out(node, _CONN, [out_point.tx_id.hex(), out_point.vout])
    assert answer is not None
    return cast("dict[str, Any]", answer["scriptPubKey"])


def test_get_tx_out_answers_pk_desc_for_a_p2pk_coin(
    regtest_node: Callable[..., Node],
) -> None:
    """A p2pk coin's own `desc` is `pk(<pubkey>)`, `InferPubkey` with no key."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    script_pub_key = ScriptPubKey.p2pk(_TEST_PUB_KEY)

    script_pub_key_dict = _stage_coin_and_get_tx_out(node, script_pub_key)

    assert script_pub_key_dict["type"] == "pubkey"
    _, payload = type_and_payload(script_pub_key.script)
    assert script_pub_key_dict["desc"] == add_checksum(f"pk({payload.hex()})")


def test_get_tx_out_answers_multi_desc_for_a_p2ms_coin(
    regtest_node: Callable[..., Node],
) -> None:
    """A p2ms coin's own `desc` is `multi(...)`, `InferPubkey` with no key."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    script_pub_key = ScriptPubKey.p2ms(1, [_TEST_PUB_KEY])

    script_pub_key_dict = _stage_coin_and_get_tx_out(node, script_pub_key)

    assert script_pub_key_dict["type"] == "multisig"
    threshold, keys = p2ms_m_and_keys(script_pub_key.script)
    key_list = ",".join(key.hex() for key in keys)
    assert script_pub_key_dict["desc"] == add_checksum(f"multi({threshold},{key_list})")


def test_get_tx_out_answers_rawtr_desc_for_a_p2tr_coin(
    regtest_node: Callable[..., Node],
) -> None:
    """A p2tr coin's own `desc` is `rawtr(...)`, no `TaprootSpendData` found."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    script_pub_key = ScriptPubKey.p2tr(_TEST_PUB_KEY)

    script_pub_key_dict = _stage_coin_and_get_tx_out(node, script_pub_key)

    assert script_pub_key_dict["type"] == "witness_v1_taproot"
    _, payload = type_and_payload(script_pub_key.script)
    assert script_pub_key_dict["desc"] == add_checksum(f"rawtr({payload.hex()})")


def test_get_tx_out_answers_anchor_type_and_addr_desc_for_a_p2a_coin(
    regtest_node: Callable[..., Node],
) -> None:
    """A P2A coin's own `type` is `anchor`, `desc` still `addr(...)`.

    `Solver`'s own `TxoutType::ANCHOR` (`src/script/solver.cpp:167-171`,
    at bitcoin/bitcoin@9be056a8a7) is a cut this library's own
    `type_and_payload` does not make -- the script answers
    "witness_unknown" there -- but `ExtractDestination` answers a
    destination for both the same way (`src/addresstype.cpp:90-93`,
    same sha), so `desc` is unaffected and still `addr(...)`.
    """
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    script_pub_key = ScriptPubKey(bytes.fromhex("51024e73"))

    script_pub_key_dict = _stage_coin_and_get_tx_out(node, script_pub_key)

    assert script_pub_key_dict["type"] == "anchor"
    assert script_pub_key_dict["address"] == script_pub_key.address
    assert script_pub_key_dict["desc"] == from_address(script_pub_key.address)


@pytest.mark.parametrize(
    "params",
    [
        [],
        [(b"\x11" * 32).hex()],
    ],
)
def test_get_tx_out_with_too_few_arguments_is_answered_with_the_usage(
    regtest_node: Callable[..., Node], params: list[Any]
) -> None:
    """`txid` and `n` are both required; either missing is the same refusal."""
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        get_tx_out(node, _CONN, params)
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["gettxout"]


def test_get_tx_out_type_checks_txid_and_n_together(
    regtest_node: Callable[..., Node],
) -> None:
    """A wrongly typed `txid` and `n` are both named, in one refusal."""
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        get_tx_out(node, _CONN, [12345, "not a number"])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert "Position 1 (txid)" in raised.value.message
    assert "Position 2 (n)" in raised.value.message


def test_get_tx_out_refuses_a_non_hexadecimal_txid(
    regtest_node: Callable[..., Node],
) -> None:
    """A right-length, non-hex `txid` is Core's own `ParseHashV` text."""
    node = regtest_node()
    not_hex = "z" * 64
    with pytest.raises(RpcError) as raised:
        get_tx_out(node, _CONN, [not_hex, 0])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == f"txid must be hexadecimal string (not '{not_hex}')"


def test_get_tx_out_refuses_a_txid_of_the_wrong_length(
    regtest_node: Callable[..., Node],
) -> None:
    """A valid-hex, wrong-length `txid` is refused before it is decoded.

    `ParseHashV` checks the string's own length before it ever tries to
    decode it (`src/rpc/util.cpp:117-124`, at
    bitcoin/bitcoin@9be056a8a7), so `"aabb"` -- four valid hex digits --
    is refused for its length rather than silently decoding to a
    two-byte `txid` nothing then finds.
    """
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        get_tx_out(node, _CONN, ["aabb", 0])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == "txid must be of length 64 (not 4, for 'aabb')"


@pytest.mark.parametrize("n", [-1, 2**32, 1.5])
def test_get_tx_out_refuses_n_out_of_uint32_range(
    regtest_node: Callable[..., Node], n: float
) -> None:
    """`UniValue::getInt<uint32_t>()`'s own range, not this library's `int`."""
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        get_tx_out(node, _CONN, [(b"\x11" * 32).hex(), n])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "JSON integer out of range"


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
            chain=RegTest(),
            mempool=mempool,
            chainstate=SimpleNamespace(block_index=block_index),
            # -1: BlockDB's own "nothing pruned yet" (block_db/__init__.py),
            # matching the default here for the same reason `pruned=False`
            # is `Config`'s: every test not about pruning gets the answer
            # that reads as "this store has never pruned anything".
            block_db=SimpleNamespace(get_block=(blocks or {}).get, pruned_up_to=-1),
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
    assert raised.value.code == RPCErrorCode.INVALID_ADDRESS_OR_KEY
    assert raised.value.message.startswith("No such mempool transaction.")


def test_the_genesis_coinbase_is_refused_with_core_s_own_message() -> None:
    """`getrawtransaction` refuses the genesis coinbase, block named or not.

    The genesis block is in `block_db` (btclib-org/btclib-node#1072), and
    Core still answers its coinbase with this refusal before reading any
    other argument.
    """
    node = a_tx_lookup_node()
    genesis = RegTest().genesis_block
    txid = genesis.transactions[0].id.hex()
    for params in ([txid], [txid, False, genesis.header.hash.hex()]):
        with pytest.raises(RpcError) as raised:
            get_raw_transaction(node, _CONN, params)
        assert raised.value.code == RPCErrorCode.INVALID_ADDRESS_OR_KEY
        assert raised.value.message == (
            "The genesis block coinbase is not considered an ordinary "
            "transaction and cannot be retrieved"
        )


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
    assert raised.value.code == RPCErrorCode.INVALID_ADDRESS_OR_KEY
    assert raised.value.message.startswith(
        "No such transaction found in the provided block."
    )


def test_an_unknown_block_hash_is_refused_by_name() -> None:
    """`getrawtransaction` refuses a block hash the index has never indexed."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["11" * 32, False, "22" * 32])
    assert raised.value.code == RPCErrorCode.INVALID_ADDRESS_OR_KEY
    assert raised.value.message == "Block hash not found"


def test_a_block_the_index_knows_and_the_store_does_not_is_not_fully_downloaded() -> (
    None
):
    """`getrawtransaction` answers Core's own not-downloaded wording, unpruned.

    `BlockIndex` and `BlockDb` are two stores; a hash the first carries
    and the second does not, with nothing yet pruned
    (`block_db.pruned_up_to` at its own default), is a block never
    downloaded rather than one deleted -- Core's own
    `CheckBlockDataAvailability` (`rpc/blockchain.cpp`, at
    bitcoin/bitcoin@ca7162cde5) answers the same way for the same
    reason: `IsBlockPruned` false, `check_for_undo` false.
    """
    tx = a_tx()
    header = generate_random_header_chain(1, RegTest().genesis.hash)[0]
    node = a_tx_lookup_node(blocks={header.hash: SimpleNamespace(header=header)})
    node.block_db.get_block = lambda _hash: None

    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, [tx.id.hex(), False, header.hash.hex()])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "Block not available (not fully downloaded)"


def test_a_block_below_the_stores_own_pruned_height_is_pruned_data() -> None:
    """`getrawtransaction` answers 'Block not available (pruned data)' pruned.

    The same missing block as the test above, except `block_db.pruned_up_to`
    now covers its own height -- `BlockDB.prune_up_to`'s own doing on a
    real store, stood in here by setting the field directly the way
    `a_tx_lookup_node`'s own default already does for the unpruned case.
    """
    tx = a_tx()
    header = generate_random_header_chain(1, RegTest().genesis.hash)[0]
    node = a_tx_lookup_node(blocks={header.hash: SimpleNamespace(header=header)})
    node.block_db.get_block = lambda _hash: None
    node.block_db.pruned_up_to = 0

    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, [tx.id.hex(), False, header.hash.hex()])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "Block not available (pruned data)"


def test_no_txid_at_all_is_answered_with_the_usage() -> None:
    """`getrawtransaction` with no arguments is refused with its own help."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["getrawtransaction"]


def test_a_txid_of_the_wrong_json_type_is_named() -> None:
    """`getrawtransaction`'s txid of the wrong JSON type is refused by name."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, [5])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 1 (txid)": "JSON value of '
        'type number is not of expected type string"\n}'
    )


def test_a_txid_that_is_not_hex_is_named_back_to_the_client() -> None:
    """`getrawtransaction`'s txid that fails to decode as hex is echoed back."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["zz"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert "zz" in raised.value.message


def test_a_txid_of_the_wrong_length_is_refused_before_it_is_decoded() -> None:
    """A valid-hex, wrong-length `txid` names "parameter 1", Core's own label.

    `ParseHashV` checks the string's own length before it ever tries to
    decode it (`src/rpc/util.cpp:117-124`, at
    bitcoin/bitcoin@9be056a8a7).
    """
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["aabb"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert (
        raised.value.message == "parameter 1 must be of length 64 (not 4, for 'aabb')"
    )


def test_a_blockhash_of_the_wrong_json_type_is_named() -> None:
    """`getrawtransaction`'s blockhash of the wrong JSON type is named."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["11" * 32, False, 5])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 3 (blockhash)": "JSON value '
        'of type number is not of expected type string"\n}'
    )


def test_a_blockhash_that_is_not_hex_is_named_back_to_the_client() -> None:
    """`getrawtransaction`'s blockhash that fails to decode as hex is echoed."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["11" * 32, False, "zz"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert "zz" in raised.value.message


def test_a_blockhash_of_the_wrong_length_is_refused_before_it_is_decoded() -> None:
    """A valid-hex, wrong-length `blockhash` names "parameter 3", Core's own."""
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, ["11" * 32, False, "aabb"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert (
        raised.value.message == "parameter 3 must be of length 64 (not 4, for 'aabb')"
    )


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
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 2 (verbose)": "JSON value of '
        'type string is not of expected type bool"\n}'
    )


def test_getrawtransaction_names_every_wrongly_typed_argument_at_once() -> None:
    """ISS 1293: txid, verbose and blockhash wrong at once are all named.

    Also proves the value-level checks -- the genesis exception, the
    two hex decodes -- do not run ahead of this: a wrong-typed txid
    would otherwise never reach `bytes.fromhex` for the genesis
    comparison to run at all, so reaching one `TYPE_ERROR` naming all
    three shows every declared argument's type is checked first.
    """
    node = a_tx_lookup_node()
    with pytest.raises(RpcError) as raised:
        get_raw_transaction(node, _CONN, [5, "true", 5])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        "Wrong type passed:\n"
        "{\n"
        '    "Position 1 (txid)": "JSON value of type number is not of'
        ' expected type string",\n'
        '    "Position 2 (verbose)": "JSON value of type string is not'
        ' of expected type bool",\n'
        '    "Position 3 (blockhash)": "JSON value of type number is not'
        ' of expected type string"\n'
        "}"
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
    `verify_mempool_acceptance` can produce as a verdict on `tx` itself
    -- accepted, a refusal in Core's words, a missing input -- and checks
    each is reported as its own entry rather than raising, as Core
    reports it: a reason and its details, but "missing-inputs" alone
    (btclib-org/btclib-node#1328). A fault that is none of those is a
    different test, below (btclib-org/btclib-node#668): it is not one of
    `tx`'s own verdicts.
    """
    tx = a_tx()
    raw = tx.serialize(include_witness=True).hex()

    outcomes: dict[str, tuple[Exception | None, dict[str, Any]]] = {
        "accepted": (None, {"allowed": True}),
        "refused": (
            TxRejectedError("non-final"),
            {
                "allowed": False,
                "reject-reason": "non-final",
                "reject-details": "non-final",
            },
        ),
        "missing": (
            MissingPrevoutError(),
            {"allowed": False, "reject-reason": "missing-inputs"},
        ),
    }
    for name, (error, expected) in outcomes.items():

        def verify(
            node: Any, tx: Any, error: Exception | None = error
        ) -> MempoolAcceptance:
            if error is not None:
                raise error
            return MempoolAcceptance(0, 81)

        monkeypatch.setattr(cb, "verify_mempool_acceptance", verify)
        (result,) = mempool_accept(a_node(), _CONN, [[raw]])
        verdict = {
            k: v for k, v in result.items() if k not in {"txid", "wtxid", "vsize"}
        }
        assert verdict == expected
        # the size verification answered, and only for an accepted one,
        # as Core answers it (btclib-org/btclib-node#1357)
        if name == "accepted":
            assert result["vsize"] == 81
        else:
            assert "vsize" not in result


def test_mempool_acceptance_propagates_a_store_error_rather_than_reporting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`testmempoolaccept` has no catch of its own for a storage fault.

    Neither `MissingPrevoutError` nor `BTClibValueError`, so
    `StoreCorruptionError` -- the one exception
    `verify_mempool_acceptance` can still raise for this node's own
    storage rather than for `tx`'s content, once
    btclib-org/btclib-node#650 stopped `UtxoIndex.get_coin` raising
    `ChainstateInconsistencyError` for the other, checksum-clean case
    -- propagates out of `test_mempool_accept` uncaught, ending the
    whole batch rather than being folded into this one entry's own
    `"reject-reason"`. This matches Core's own `testmempoolaccept`
    (`src/rpc/mempool.cpp:379-430`, at bitcoin/bitcoin@ca7162cde5),
    whose per-tx loop has no catch-all of its own either
    (btclib-org/btclib-node#668).
    """

    def corrupted(node: Any, tx: Any) -> NoReturn:
        err_msg = "stored utxo- record failed to parse"
        raise StoreCorruptionError(err_msg)

    monkeypatch.setattr(cb, "verify_mempool_acceptance", corrupted)
    tx = a_tx()
    with pytest.raises(StoreCorruptionError):
        mempool_accept(a_node(), _CONN, [[tx.serialize(include_witness=True).hex()]])


def decode_failure(rawtx: str) -> str:
    """Core's own `-22` message for a `rawtx` that does not decode."""
    return f"TX decode failed: {rawtx} Make sure the tx has at least one input."


def test_an_unparsable_transaction_ends_the_call() -> None:
    """A `rawtx` that does not decode is `-22` for the call, as in Core.

    `bitcoind` v31.1 on regtest: `testmempoolaccept '["zz","00"]'` is
    `-22` "TX decode failed: zz Make sure the tx has at least one input.",
    where this reported each entry "Invalid serialization"
    (btclib-org/btclib-node#1329).
    """
    with pytest.raises(RpcError) as raised:
        mempool_accept(a_node(), _CONN, [["zz", "00"]])
    assert raised.value.code == RPCErrorCode.DESERIALIZATION_ERROR
    assert raised.value.message == decode_failure("zz")


def test_the_first_bad_rawtx_in_order_is_the_one_named() -> None:
    """Each element is typed and then decoded, one after the other."""
    with pytest.raises(RpcError) as raised:
        mempool_accept(a_node(), _CONN, [["zz", 5]])
    assert raised.value.code == RPCErrorCode.DESERIALIZATION_ERROR
    with pytest.raises(RpcError) as raised:
        mempool_accept(a_node(), _CONN, [[5, "zz"]])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR


@pytest.mark.parametrize("count", [0, 26])
def test_an_array_outside_one_to_twenty_five_is_refused(count: int) -> None:
    """Core's `MAX_PACKAGE_COUNT`: `-8` for an empty or a 26-entry array.

    `bitcoind` v31.1 on regtest answers both "Array must contain between
    1 and 25 transactions."
    """
    raw = a_tx().serialize(include_witness=True).hex()
    with pytest.raises(RpcError) as raised:
        mempool_accept(a_node(), _CONN, [[raw] * count])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == "Array must contain between 1 and 25 transactions."


def test_twenty_five_rawtxs_are_each_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound's own edge is inside it."""
    monkeypatch.setattr(
        cb, "verify_mempool_acceptance", lambda node, tx: MempoolAcceptance(0, 0)
    )
    raw = a_tx().serialize(include_witness=True).hex()
    assert len(mempool_accept(a_node(), _CONN, [[raw] * 25])) == 25


def test_test_mempool_accept_with_no_params_is_answered_the_usage() -> None:
    """`testmempoolaccept` with no arguments is refused with its own usage.

    An empty `params` used to reach `params[0]` unguarded and raise
    `IndexError`, which `handle_rpc` answers `-32603 Internal Error` --
    the code this node owes its own fault, not a call short of a
    required argument (issue #443).
    """
    with pytest.raises(RpcError) as raised:
        mempool_accept(a_node(), _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["testmempoolaccept"]


def test_test_mempool_accept_rawtxs_of_the_wrong_json_type_is_named() -> None:
    """`testmempoolaccept`'s `rawtxs` of the wrong JSON type is named.

    `rawtxs` used to be handed straight to a `for` loop, so a JSON
    string -- itself iterable in Python -- was walked one character at a
    time instead of being refused (issue #443). Core declares this
    argument `RPCArg::Type::ARR`, type-checked before the handler body
    runs.
    """
    with pytest.raises(RpcError) as raised:
        mempool_accept(a_node(), _CONN, ["not an array"])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 1 (rawtxs)": "JSON value of '
        'type string is not of expected type array"\n}'
    )


@pytest.mark.parametrize(
    ("rawtx", "json_type"),
    [
        (5, "number"),
        (1.5, "number"),
        (None, "null"),
        ([], "array"),
        ({}, "object"),
        (True, "bool"),
    ],
)
def test_test_mempool_accept_a_rawtx_of_the_wrong_json_type_is_named(
    monkeypatch: pytest.MonkeyPatch, rawtx: object, json_type: str
) -> None:
    """A `rawtx` that is not a string ends the call with Core's own message.

    `bitcoind` v31.1 on regtest answers `-3` "JSON value of type number
    is not of expected type string" to `testmempoolaccept [[5]]`, and
    the same for the other JSON types, where this answered `-32603
    Internal Error` (btclib-org/btclib-node#1253). The valid entry ahead
    of the bad one is not validated: Core reads every element before it
    validates any.
    """
    verified: list[object] = []
    monkeypatch.setattr(
        cb, "verify_mempool_acceptance", lambda node, tx: verified.append(tx)
    )
    raw = a_tx().serialize(include_witness=True).hex()
    with pytest.raises(RpcError) as raised:
        mempool_accept(a_node(), _CONN, [[raw, rawtx]])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        f"JSON value of type {json_type} is not of expected type string"
    )
    assert verified == []


def test_a_rawtx_with_a_truncated_script_is_a_decode_failure() -> None:
    """A script shorter than its declared length does not decode either.

    `Tx.parse` raises `BTClibRuntimeError` there rather than
    `BTClibValueError`, which answered `-32603 Internal Error`.
    """
    truncated = "02000000" + "01" + "00" * 32 + "00000000" + "05" + "0000"
    with pytest.raises(RpcError) as raised:
        mempool_accept(a_node(), _CONN, [[truncated]])
    assert raised.value.code == RPCErrorCode.DESERIALIZATION_ERROR
    assert raised.value.message == decode_failure(truncated)


def test_a_relayed_transaction_is_answered_with_its_txid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` adds an accepted transaction and broadcasts it.

    Answers its own txid, adds it to the mempool, and announces it to
    peers, at the vsize verification answered
    (btclib-org/btclib-node#1357).
    """
    monkeypatch.setattr(
        cb, "verify_mempool_acceptance", lambda node, tx: MempoolAcceptance(1000, 999)
    )
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
    assert mempool.vsizes[tx.hash] == 999
    assert broadcast == [tx]


def test_something_that_is_not_a_transaction_is_refused_rather_than_relayed() -> None:
    """`sendrawtransaction` refuses a string that fails to decode as a tx."""
    with pytest.raises(RpcError) as raised:
        send_raw_transaction(a_node(), _CONN, ["not a transaction"])
    assert raised.value.code == RPCErrorCode.DESERIALIZATION_ERROR
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
    assert raised.value.code == RPCErrorCode.DESERIALIZATION_ERROR


def test_a_rawtx_of_the_wrong_json_type_is_named() -> None:
    """`sendrawtransaction`'s rawtx of the wrong JSON type is named."""
    with pytest.raises(RpcError) as raised:
        send_raw_transaction(a_node(), _CONN, [5])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 1 (hexstring)": "JSON value '
        'of type number is not of expected type string"\n}'
    )


def test_send_raw_transaction_with_no_params_is_answered_the_usage() -> None:
    """`sendrawtransaction` with no arguments is refused with its own usage.

    An empty `params` used to reach `params[0]` unguarded and raise
    `IndexError`, which `handle_rpc` answers `-32603 Internal Error` --
    the code this node owes its own fault, not a call short of a
    required argument (issue #443).
    """
    with pytest.raises(RpcError) as raised:
        send_raw_transaction(a_node(), _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["sendrawtransaction"]


def test_a_transaction_the_mempool_will_not_have_is_not_reported_relayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` does not report or broadcast a missing-prevouts tx.

    A refusal is not answered with the txid of a transaction this node
    has neither kept nor sent (issue #83). `bitcoind` v31.1 on regtest
    answers a spend of an unknown txid `-25`
    "bad-txns-inputs-missingorspent" (btclib-org/btclib-node#1328).
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
    assert raised.value.code == RPCErrorCode.VERIFY_ERROR
    assert raised.value.message == "bad-txns-inputs-missingorspent"
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
    monkeypatch.setattr(
        cb,
        "verify_mempool_acceptance",
        lambda node, tx: MempoolAcceptance(1000, tx.vsize),
    )
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 0
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    with pytest.raises(RpcError) as raised:
        send_raw_transaction(node, _CONN, [tx.serialize(include_witness=True).hex()])
    assert raised.value.code == RPCErrorCode.VERIFY_REJECTED
    assert raised.value.message == "mempool full"
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
    # mempool (node/transaction.cpp, at bitcoin/bitcoin@58a7869f86):
    # resubmission is reannounced rather than refused for a fullness
    # this particular submission did not cause
    monkeypatch.setattr(
        cb,
        "verify_mempool_acceptance",
        lambda node, tx: MempoolAcceptance(1000, tx.vsize),
    )
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
    # (node/transaction.cpp, at bitcoin/bitcoin@58a7869f86): "The mempool
    # transaction may have the same or different witness (and wtxid) as
    # this transaction." The guard has to be txid-keyed
    # (Mempool.txid_index) to reannounce here instead of refusing a
    # fullness this resubmission did not cause.
    monkeypatch.setattr(
        cb,
        "verify_mempool_acceptance",
        lambda node, tx: MempoolAcceptance(1000, tx.vsize),
    )
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
    monkeypatch.setattr(
        cb,
        "verify_mempool_acceptance",
        lambda node, tx: MempoolAcceptance(1000, tx.vsize),
    )
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

    `header_dict` is `blocks` itself: `get_block_header`'s own
    `mediantime` walks it through `main.parent_lookup`, which reads
    `header_dict[hash].header` -- the same shape `block()` below
    already builds each entry as, so no second collection is needed to
    answer it, only every chain built here staying short of the eleven
    ancestors `median_time_past` would need to walk off the end of it.
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
        header_dict=blocks,
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


def test_a_block_header_s_chainwork_is_hex_and_zero_padded_to_64() -> None:
    """`chainwork` is hex here (closes #658), not the plain int used to pin it.

    `a_block_index`'s own fake chainwork is `index + 1`, so the middle
    block of a 3-header chain answers `2` -- unpadded hex is one digit,
    the `2` the un-corrected version of this test used to assert
    against directly, so this exercises exactly the difference padding
    to 64 makes rather than a value long enough to hide it.
    """
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    middle = header_json(node, _CONN, [chain[1].hash.hex()])
    assert middle["chainwork"] == (
        "0000000000000000000000000000000000000000000000000000000000000002"
    )
    assert len(middle["chainwork"]) == 64


def test_a_block_header_answers_every_scalar_field_core_s_own_does() -> None:
    """`version`, `merkleroot`, `time`, `nonce`, `bits`, `target`, `difficulty`.

    `bits`/`target`/`difficulty` are cross-checked the same way
    `get_blockchain_info`'s own equivalent test is, against Core's
    literal `SetCompact`/`GetDifficulty` algorithm on regtest's own
    easy genesis bits, independently of the `BlockHeader` properties
    this callback reads. `versionHex` and `merkleroot`/`previous_block_hash`
    being absent under btclib's own `to_dict` spelling are what closes
    the naming half of #658, chainwork's own hex form closing the rest.
    """
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    header = chain[0]
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    result = header_json(node, _CONN, [header.hash.hex()])
    assert result["version"] == 70015
    assert result["versionHex"] == "0001117f"
    assert result["merkleroot"] == header.merkle_root
    assert result["time"] == int(header.time.timestamp())
    assert result["mediantime"] == int(header.time.timestamp())
    assert result["nonce"] == header.nonce
    assert result["bits"] == bytes.fromhex("207fffff")
    assert result["target"] == bytes.fromhex(
        "7fffff0000000000000000000000000000000000000000000000000000000000"
    )
    assert result["difficulty"] == 4.6565423739069247e-10
    assert "previous_block_hash" not in result
    assert "merkle_root" not in result
    assert "nTx" not in result


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
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 2 (verbose)": "JSON value of '
        'type string is not of expected type bool"\n}'
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
    assert raised.value.code == RPCErrorCode.INVALID_ADDRESS_OR_KEY
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
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
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
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 1 (blockhash)": "JSON value '
        'of type number is not of expected type string"\n}'
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
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 1 (blockhash)": "JSON value '
        'of type null is not of expected type string"\n}'
    )


def test_get_block_header_refuses_a_hash_that_is_not_hexadecimal() -> None:
    """A right-length, non-hex `hash` is Core's own `ParseHashV` text."""
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    not_hex = "z" * 64
    with pytest.raises(RpcError) as raised:
        get_block_header(node, _CONN, [not_hex])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == f"hash must be hexadecimal string (not '{not_hex}')"


def test_get_block_header_refuses_a_hash_of_the_wrong_length() -> None:
    """A valid-hex, wrong-length `hash` is refused before it is decoded.

    `ParseHashV` checks the string's own length before it ever tries to
    decode it (`src/rpc/util.cpp:117-124`, at
    bitcoin/bitcoin@9be056a8a7).
    """
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    with pytest.raises(RpcError) as raised:
        get_block_header(node, _CONN, ["aabb"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == "hash must be of length 64 (not 4, for 'aabb')"


def test_two_wrongly_typed_arguments_are_both_named_at_once() -> None:
    """ISS 1293: `getblockheader [1, "x"]` names both, as a real bitcoind does.

    Measured against a regtest bitcoind v31.1.0: `getblockheader`
    called with a number `blockhash` and a string `verbose` answers
    `"Position 1 (blockhash)"` and `"Position 2 (verbose)"` in one
    `Wrong type passed` object, not the first alone.
    """
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    node = cast(
        "Node",
        SimpleNamespace(chainstate=SimpleNamespace(block_index=a_block_index(chain))),
    )
    with pytest.raises(RpcError) as raised:
        get_block_header(node, _CONN, [1, "x"])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        "Wrong type passed:\n"
        "{\n"
        '    "Position 1 (blockhash)": "JSON value of type number is not'
        ' of expected type string",\n'
        '    "Position 2 (verbose)": "JSON value of type string is not'
        ' of expected type bool"\n'
        "}"
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
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["getblockheader"]


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


def a_blockchain_info_node(
    *,
    chain: Chain | None = None,
    headers: list[BlockHeader] | None = None,
    header_index: list[bytes] | None = None,
    is_initial_block_download: bool = False,
    pruned: bool = False,
    pruned_up_to: int = -1,
    prune_target_mib: int | None = None,
    current_usage: int = 0,
) -> Node:
    """Build a node carrying just what `get_blockchain_info` reads.

    `headers` is the active chain's own headers, genesis first and each
    linked to the one before it -- `median_time_past`'s own walk over
    `header_dict` needs real ancestors, not bare hashes, unlike
    `header_index` below it. RegTest's own genesis alone by default,
    which is also `bits=0x207fffff`, the value this file's own
    bits/target/difficulty tests are cross-checked against.
    `header_index` defaults to the active chain's own hashes -- a node
    whose every known header has also been validated and connected,
    which is every test here but the one naming the two apart.
    """
    chain = chain if chain is not None else RegTest()
    headers = headers if headers is not None else [chain.genesis]
    active_chain = [header.hash for header in headers]
    header_dict = {header.hash: SimpleNamespace(header=header) for header in headers}
    chainwork: dict[bytes, int] = {}
    total_work = 0
    for header in headers:
        total_work += calculate_work(header)
        chainwork[header.hash] = total_work
    header_index = header_index if header_index is not None else active_chain
    return cast(
        "Node",
        SimpleNamespace(
            chain=chain,
            chainstate=SimpleNamespace(
                block_index=SimpleNamespace(
                    active_chain=active_chain,
                    header_index=header_index,
                    header_dict=header_dict,
                    chainwork=chainwork,
                )
            ),
            is_initial_block_download=is_initial_block_download,
            config=SimpleNamespace(pruned=pruned, prune_target_mib=prune_target_mib),
            block_db=SimpleNamespace(
                pruned_up_to=pruned_up_to, current_usage=lambda: current_usage
            ),
        ),
    )


def test_blockchain_info_names_the_chain_in_core_s_own_vocabulary() -> None:
    """`getblockchaininfo` names the chain in Core's own vocabulary (issue #21).

    `BitcoinCoreFetcher.assert_network` reads "chain" alone, and Core's
    own vocabulary for it is not btclib's network name -- `chains.py`'s
    `Chain.name` is "mainnet", Core answers "main".
    """
    node = a_blockchain_info_node(chain=RegTest())
    assert get_blockchain_info(node, _CONN, [])["chain"] == "regtest"

    node = a_blockchain_info_node(chain=Main())
    assert get_blockchain_info(node, _CONN, [])["chain"] == "main"


def test_blockchain_info_s_blocks_is_the_active_chain_s_own_last_index() -> None:
    """`blocks` is `active_chain`'s own height, matching `getblockcount`."""
    chain = RegTest()
    headers = [chain.genesis, *generate_random_header_chain(2, chain.genesis.hash)]
    node = a_blockchain_info_node(chain=chain, headers=headers)
    assert get_blockchain_info(node, _CONN, [])["blocks"] == 2


def test_blockchain_info_s_headers_outruns_blocks_during_header_sync() -> None:
    """`headers` moves ahead of `blocks` while header sync outruns validation.

    `header_index` carries every header this node knows of, `active_chain`
    only the ones it has validated and connected -- the gap between the
    two answers is what a header sync in progress looks like from here
    (issue #575).
    """
    node = a_blockchain_info_node(
        header_index=[b"\x11" * 32, b"\x22" * 32, b"\x33" * 32, b"\x44" * 32]
    )
    result = get_blockchain_info(node, _CONN, [])
    assert result["blocks"] == 0
    assert result["headers"] == 3


def test_blockchain_info_s_bestblockhash_is_the_active_chain_s_own_tip() -> None:
    """`bestblockhash` is `active_chain`'s tip, per `getbestblockhash`."""
    chain = RegTest()
    headers = [chain.genesis, *generate_random_header_chain(1, chain.genesis.hash)]
    node = a_blockchain_info_node(chain=chain, headers=headers)
    assert get_blockchain_info(node, _CONN, [])["bestblockhash"] == headers[-1].hash


def test_blockchain_info_s_bits_target_and_difficulty_cross_checked_against_core() -> (
    None
):
    """`bits`, `target` and `difficulty` on regtest's own genesis header.

    `target` and `difficulty` are computed here from `bits` by Core's own
    literal algorithm -- `SetCompact`'s mantissa*256**(exponent-3) and
    `GetDifficulty`'s repeated `*=`/`/=` 256.0 loop -- independently of
    `btclib.block.block_header.BlockHeader`'s own properties this
    callback reads, rather than by re-deriving the same code under test.
    """
    node = a_blockchain_info_node()
    result = get_blockchain_info(node, _CONN, [])
    assert result["bits"] == bytes.fromhex("207fffff")
    assert result["target"] == bytes.fromhex(
        "7fffff0000000000000000000000000000000000000000000000000000000000"
    )
    assert result["difficulty"] == 4.6565423739069247e-10


def test_blockchain_info_s_time_and_mediantime_on_a_single_header_chain() -> None:
    """`time` and `mediantime` both answer the one header's own timestamp."""
    node = a_blockchain_info_node()
    result = get_blockchain_info(node, _CONN, [])
    assert result["time"] == 1296688602
    assert result["mediantime"] == 1296688602


def test_blockchain_info_s_chainwork_is_hex_and_zero_padded_to_64() -> None:
    """`chainwork` is hex here, unlike `getblockheader`'s own plain int.

    Core's `GetBlockProof` on regtest's own easy genesis target is 2,
    computed independently of `btclib.block.proof_of_work.block_work`
    this callback reads through `BlockIndex.chainwork`.
    """
    node = a_blockchain_info_node()
    result = get_blockchain_info(node, _CONN, [])
    assert result["chainwork"] == (
        "0000000000000000000000000000000000000000000000000000000000000002"
    )
    assert len(result["chainwork"]) == 64


def test_blockchain_info_s_initialblockdownload_reads_the_node_s_own_latch() -> None:
    """`initialblockdownload` reads `node.is_initial_block_download` as-is."""
    node = a_blockchain_info_node(is_initial_block_download=True)
    assert get_blockchain_info(node, _CONN, [])["initialblockdownload"] is True

    node = a_blockchain_info_node(is_initial_block_download=False)
    assert get_blockchain_info(node, _CONN, [])["initialblockdownload"] is False


def test_blockchain_info_s_size_on_disk_reads_current_usage_unconditionally() -> None:
    """`size_on_disk` is `block_db.current_usage`, present either way.

    Core's own member is unconditional too, computed before the `pruned`
    branch it sits beside (`rpc/blockchain.cpp:1450-1452`, at
    bitcoin/bitcoin@ca7162cde5).
    """
    node = a_blockchain_info_node(pruned=False, current_usage=12345)
    assert get_blockchain_info(node, _CONN, [])["size_on_disk"] == 12345


def test_blockchain_info_s_pruned_reads_config() -> None:
    """`pruned` is `Config.pruned`, with no `pruneheight` when `False`."""
    node = a_blockchain_info_node(pruned=False)
    result = get_blockchain_info(node, _CONN, [])
    assert result["pruned"] is False
    assert "pruneheight" not in result
    assert "automatic_pruning" not in result
    assert "prune_target_size" not in result


def test_blockchain_info_s_pruneheight_is_the_first_unpruned_block() -> None:
    """`pruneheight` is `pruned_up_to + 1`, present once `pruned` holds.

    Core's own "the first block unpruned, all previous blocks were
    pruned" (`rpc/blockchain.cpp:1400`, at bitcoin/bitcoin@ca7162cde5).
    """
    node = a_blockchain_info_node(pruned=True, pruned_up_to=41)
    assert get_blockchain_info(node, _CONN, [])["pruneheight"] == 42


def test_blockchain_info_s_automatic_pruning_is_whether_a_mib_target_is_set() -> None:
    """`automatic_pruning` is `Config.prune_target_mib is not None`.

    Core's own `GetPruneTarget() != PRUNE_TARGET_MANUAL`
    (`rpc/blockchain.cpp:1457`, at bitcoin/bitcoin@ca7162cde5); no
    `prune_target_size` where it is `False`.
    """
    node = a_blockchain_info_node(pruned=True, prune_target_mib=None)
    result = get_blockchain_info(node, _CONN, [])
    assert result["automatic_pruning"] is False
    assert "prune_target_size" not in result


def test_blockchain_info_s_prune_target_size_is_in_bytes() -> None:
    """`prune_target_size` is `prune_target_mib` in bytes, Core's own unit."""
    node = a_blockchain_info_node(pruned=True, prune_target_mib=MIN_PRUNE_TARGET_MIB)
    result = get_blockchain_info(node, _CONN, [])
    assert result["automatic_pruning"] is True
    assert result["prune_target_size"] == MIN_PRUNE_TARGET_MIB * 1024 * 1024


def test_prune_blockchain_refuses_when_not_in_prune_mode(
    regtest_node: Callable[..., Node],
) -> None:
    """Core's own `IsPruneMode()` refusal (`rpc/blockchain.cpp:936-938`)."""
    node = regtest_node(pruned=False)
    with pytest.raises(RpcError) as raised:
        prune_blockchain(node, _CONN, [10])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert (
        raised.value.message == "Cannot prune blocks because node is not in prune mode."
    )


def test_prune_blockchain_refuses_a_missing_height(
    regtest_node: Callable[..., Node],
) -> None:
    """A call with no argument is refused with the method's own full help."""
    node = regtest_node(pruned=True, prune_target_mib=None)
    with pytest.raises(RpcError) as raised:
        prune_blockchain(node, _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["pruneblockchain"]


def test_prune_blockchain_refuses_a_height_of_the_wrong_json_type(
    regtest_node: Callable[..., Node],
) -> None:
    """A non-numeric `height` is named the way `type_error` names it."""
    node = regtest_node(pruned=True, prune_target_mib=None)
    with pytest.raises(RpcError) as raised:
        prune_blockchain(node, _CONN, ["10"])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        'Wrong type passed:\n{\n    "Position 1 (height)": "JSON value '
        'of type string is not of expected type number"\n}'
    )


def test_prune_blockchain_refuses_a_bool_height(
    regtest_node: Callable[..., Node],
) -> None:
    """A JSON bool is its own VBOOL, not VNUM, refused the same as a string."""
    node = regtest_node(pruned=True, prune_target_mib=None)
    with pytest.raises(RpcError) as raised:
        prune_blockchain(node, _CONN, [True])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR


def test_prune_blockchain_refuses_a_fractional_height(
    regtest_node: Callable[..., Node],
) -> None:
    """A JSON number with a decimal point fails `getInt<int>()`, Core's way."""
    node = regtest_node(pruned=True, prune_target_mib=None)
    with pytest.raises(RpcError) as raised:
        prune_blockchain(node, _CONN, [10.5])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "JSON integer out of range"


def test_prune_blockchain_refuses_a_negative_height(
    regtest_node: Callable[..., Node],
) -> None:
    """Core's own `heightParam < 0` check (`rpc/blockchain.cpp:945-947`)."""
    node = regtest_node(pruned=True, prune_target_mib=None)
    with pytest.raises(RpcError) as raised:
        prune_blockchain(node, _CONN, [-1])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == "Negative block height."


def test_prune_blockchain_refuses_a_chain_too_short_for_pruning(
    regtest_node: Callable[..., Node],
) -> None:
    """A chain shorter than `chain.prune_after_height` refuses to prune at all.

    Regtest's own `prune_after_height` is 1000
    (`src/kernel/chainparams.cpp:601`, at bitcoin/bitcoin@ca7162cde5,
    without `-fastprune`), not `MIN_BLOCKS_TO_KEEP` -- the two are
    separate constants in Core and this checks the one this refusal
    actually reads. A thousand-block chain connects in well under a
    second on regtest's own trivial target, measured before writing
    this test rather than assumed.
    """
    node = regtest_node(pruned=True, prune_target_mib=None)
    chain = generate_random_chain(
        node.chain.prune_after_height - 1, node.chain.genesis.hash
    )
    connect(node, chain)
    with pytest.raises(RpcError) as raised:
        prune_blockchain(node, _CONN, [1])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "Blockchain is too short for pruning."


def test_prune_blockchain_allows_a_chain_at_exactly_prune_after_height(
    regtest_node: Callable[..., Node],
) -> None:
    """A chain exactly `chain.prune_after_height` tall is not refused."""
    node = regtest_node(pruned=True, prune_target_mib=None)
    chain = generate_random_chain(
        node.chain.prune_after_height, node.chain.genesis.hash
    )
    connect(node, chain)

    result = prune_blockchain(node, _CONN, [1])

    assert result == 1
    assert node.block_db.pruned_up_to == 1


def test_prune_blockchain_refuses_a_height_past_the_tip(
    regtest_node: Callable[..., Node],
) -> None:
    """Core's own `height > chainHeight` check (`blockchain.cpp:964-965`)."""
    node = regtest_node(pruned=True, prune_target_mib=None)
    chain = generate_random_chain(
        node.chain.prune_after_height + 5, node.chain.genesis.hash
    )
    block_index = connect(node, chain)
    tip_height = len(block_index.active_chain) - 1
    with pytest.raises(RpcError) as raised:
        prune_blockchain(node, _CONN, [tip_height + 1])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert (
        raised.value.message == "Blockchain is shorter than the attempted prune height."
    )


def test_prune_blockchain_deletes_up_to_the_given_height(
    regtest_node: Callable[..., Node],
) -> None:
    """A height comfortably behind the tip deletes exactly through it.

    `prune_target_mib=None` -- manual pruning -- so nothing was deleted
    on its own before this call; the only reason a block is gone
    afterwards is this one RPC call.
    """
    node = regtest_node(pruned=True, prune_target_mib=None)
    chain = generate_random_chain(
        node.chain.prune_after_height + 5, node.chain.genesis.hash
    )
    block_index = connect(node, chain)
    assert node.block_db.pruned_up_to == -1

    result = prune_blockchain(node, _CONN, [3])

    assert result == 3
    assert node.block_db.pruned_up_to == 3
    assert node.block_db.get_block(block_index.active_chain[3]) is None
    assert node.block_db.get_block(block_index.active_chain[4]) is not None


def test_prune_blockchain_clamps_a_height_close_to_the_tip(
    regtest_node: Callable[..., Node],
) -> None:
    """A `height` within `MIN_BLOCKS_TO_KEEP` of the tip is clamped.

    Core's own `blockchain.cpp:966-969`: pruning still runs, down to the
    retained depth rather than up to the height actually asked for.
    """
    node = regtest_node(pruned=True, prune_target_mib=None)
    chain = generate_random_chain(
        node.chain.prune_after_height + 5, node.chain.genesis.hash
    )
    block_index = connect(node, chain)
    tip_height = len(block_index.active_chain) - 1

    result = prune_blockchain(node, _CONN, [tip_height])

    assert result == tip_height - MIN_BLOCKS_TO_KEEP
    assert node.block_db.pruned_up_to == tip_height - MIN_BLOCKS_TO_KEEP


def test_prune_blockchain_reads_a_large_height_as_a_timestamp(
    regtest_node: Callable[..., Node],
) -> None:
    """A `height` over the billion threshold is the earliest block that old.

    `target_height`'s own block time, plus the two-hour drift window
    `pruneblockchain` subtracts back off before searching, round-trips
    to `target_height` itself: this chain's blocks are dated one second
    apart, so no earlier block shares that same time.
    """
    node = regtest_node(pruned=True, prune_target_mib=None)
    chain = generate_random_chain(
        node.chain.prune_after_height + 5, node.chain.genesis.hash
    )
    block_index = connect(node, chain)
    target_height = 3
    target_header = node.chainstate.block_index.header_dict[
        block_index.active_chain[target_height]
    ].header
    timestamp_param = block_time(target_header) + 2 * 60 * 60

    result = prune_blockchain(node, _CONN, [timestamp_param])

    assert result == target_height
    assert node.block_db.pruned_up_to == target_height


def test_prune_blockchain_refuses_a_timestamp_after_every_block(
    regtest_node: Callable[..., Node],
) -> None:
    """A timestamp past every block's own time finds nothing to prune to."""
    node = regtest_node(pruned=True, prune_target_mib=None)
    chain = generate_random_chain(
        node.chain.prune_after_height + 5, node.chain.genesis.hash
    )
    block_index = connect(node, chain)
    tip_header = node.chainstate.block_index.header_dict[
        block_index.active_chain[-1]
    ].header
    timestamp_param = block_time(tip_header) + 2 * 60 * 60 + 1000

    with pytest.raises(RpcError) as raised:
        prune_blockchain(node, _CONN, [timestamp_param])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == (
        "Could not find block with at least the specified timestamp."
    )


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
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == "Block height out of range"


def test_a_height_past_the_tip_is_refused() -> None:
    """`getblockhash` refuses a height at or past the active chain's length."""
    node = a_chain_index_node([b"\x11" * 32, b"\x22" * 32])
    with pytest.raises(RpcError) as raised:
        get_block_hash(node, _CONN, [2])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
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
        assert raised.value.code == RPCErrorCode.TYPE_ERROR
        assert raised.value.message == (
            'Wrong type passed:\n{\n    "Position 1 (height)": "JSON value '
            f'of type {type_name} is not of expected type number"\n}}'
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
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "JSON integer out of range"


def test_no_height_at_all_is_answered_with_the_usage() -> None:
    """`getblockhash` with no arguments at all is refused with its own help."""
    node = a_chain_index_node([b"\x11" * 32])
    with pytest.raises(RpcError) as raised:
        get_block_hash(node, _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["getblockhash"]


def test_a_transaction_whose_scripts_do_not_verify_is_answered_with_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` answers VERIFY_REJECTED for a bad-script tx.

    Does not add it to the mempool or broadcast it, and answers Core's
    reason and details (btclib-org/btclib-node#1328).
    """
    reason = "mempool-script-verify-flag-failed (no)"
    details = "input 0 of 00 (wtxid 00), spending 00:0"

    def invalid(node: Any, transaction: Any) -> NoReturn:
        raise TxRejectedError(reason, details)

    monkeypatch.setattr(cb, "verify_mempool_acceptance", invalid)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    with pytest.raises(RpcError) as raised:
        send_raw_transaction(node, _CONN, [tx.serialize(include_witness=True).hex()])
    assert raised.value.code == RPCErrorCode.VERIFY_REJECTED
    assert raised.value.message == f"{reason}, {details}"
    assert not mempool.contains_tx(tx)
    assert broadcast == []


def a_fee_refusal(node: Any, transaction: Any) -> NoReturn:
    """Refuse as `verify_mempool_acceptance` refuses a fee under the floor."""
    reason, details = "min relay fee not met", "0 < 11"
    raise TxRejectedError(reason, details)


def test_a_fee_refusal_is_answered_in_core_s_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sendrawtransaction` answers `-26` and Core's own reason and details.

    `bitcoind` v31.1 on regtest: "min relay fee not met, 0 < 11" for a
    zero-fee 110-vbyte spend (btclib-org/btclib-node#1245).
    """
    monkeypatch.setattr(cb, "verify_mempool_acceptance", a_fee_refusal)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    with pytest.raises(RpcError) as raised:
        send_raw_transaction(node, _CONN, [tx.serialize(include_witness=True).hex()])
    assert raised.value.code == RPCErrorCode.VERIFY_REJECTED
    assert raised.value.message == "min relay fee not met, 0 < 11"
    assert not mempool.contains_tx(tx)
    assert broadcast == []


def test_a_fee_refusal_is_reported_with_core_s_reason_and_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`testmempoolaccept` reports the reason and details, as Core does."""
    monkeypatch.setattr(cb, "verify_mempool_acceptance", a_fee_refusal)
    (result,) = mempool_accept(
        a_node(), _CONN, [[a_tx().serialize(include_witness=True).hex()]]
    )
    assert result["allowed"] is False
    assert result["reject-reason"] == "min relay fee not met"
    assert result["reject-details"] == "min relay fee not met, 0 < 11"


def a_node_holding(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch, fee: int
) -> tuple[Node, Tx, list[tuple[bytes, int]]]:
    """Return a node holding a spend paying `fee`, and what it announces."""
    node = regtest_node()
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    funding = chain[0].transactions[0]
    held = generate_random_transaction(funding.id, value=funding.vout[0].value - fee)
    # `bypass_limits`, as a reorg re-adds a transaction: no floor at all
    assert node.mempool.add_tx(
        held, *verify_mempool_acceptance(node, held, bypass_limits=True)
    )
    announced: list[tuple[bytes, int]] = []
    monkeypatch.setattr(
        node.p2p_manager,
        "broadcast_raw_transaction",
        lambda tx, fee: announced.append((tx.hash, fee)),
    )
    return node, held, announced


def test_a_held_transaction_is_reannounced_not_judged_again(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A txid already held answers its txid, the real verifier never asked.

    Core's `BroadcastTransaction` returns early for a txid its mempool
    holds, before any acceptance check: a fee-free transaction a reorg
    re-added, resubmitted, is not "min relay fee not met"
    (btclib-org/btclib-node#1245). The same txid under another witness
    reannounces the mempool's copy.
    """
    node, held, announced = a_node_holding(regtest_node, monkeypatch, 0)
    twin = Tx.parse(held.serialize(include_witness=True))
    twin.vin[0].script_witness = Witness([b"\x01"])
    assert twin.id == held.id
    assert twin.hash != held.hash
    for resubmitted in (held, twin):
        raw = resubmitted.serialize(include_witness=True).hex()
        assert send_raw_transaction(node, _CONN, [raw]) == held.id.hex()
    assert announced == [(held.hash, 0), (held.hash, 0)]


def a_twin(held: Tx) -> Tx:
    """Return `held` under another witness: its txid, another wtxid."""
    twin = Tx.parse(held.serialize(include_witness=True))
    twin.vin[0].script_witness = Witness([b"\x01"])
    assert twin.id == held.id
    assert twin.hash != held.hash
    return twin


def test_testmempoolaccept_refuses_a_held_txid_in_core_s_words(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A txid already held is refused as held, not as its own conflict.

    Core's `PreChecks` refuses it ahead of the conflict checks, as
    bitcoind v31.1 answers on regtest: "txn-already-in-mempool" for the
    same wtxid, "txn-same-nonwitness-data-in-mempool" for another
    witness, each its own reject-details. btclib-org/btclib-node#1244
    """
    node, held, _ = a_node_holding(regtest_node, monkeypatch, 1_000)
    answers = [
        mempool_accept(node, _CONN, [[t.serialize(include_witness=True).hex()]])[0]
        for t in (held, a_twin(held))
    ]
    assert [(a["reject-reason"], a["reject-details"]) for a in answers] == [
        ("txn-already-in-mempool",) * 2,
        ("txn-same-nonwitness-data-in-mempool",) * 2,
    ]


def test_a_held_fee_free_transaction_is_refused_as_held_not_for_its_fee(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held check comes before the fee floor, as in Core's `PreChecks`.

    A fee-free transaction a reorg re-added, put to `testmempoolaccept`,
    is "txn-already-in-mempool" and not "min relay fee not met".
    btclib-org/btclib-node#1244
    """
    node, held, _ = a_node_holding(regtest_node, monkeypatch, 0)
    raw = held.serialize(include_witness=True).hex()
    (answer,) = mempool_accept(node, _CONN, [[raw]])
    assert answer["reject-reason"] == "txn-already-in-mempool"


def test_a_held_transaction_under_a_risen_minimum_is_reannounced(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rolling minimum risen past what a held tx pays is no refusal."""
    node, held, announced = a_node_holding(regtest_node, monkeypatch, 1_000)
    node.mempool._rolling_min_fee_rate = 1_000_000.0
    node.mempool._block_since_last_rolling_fee_bump = False
    raw = held.serialize(include_witness=True).hex()
    assert send_raw_transaction(node, _CONN, [raw]) == held.id.hex()
    assert announced == [(held.hash, 1_000)]


def test_a_corrupted_stored_record_is_not_answered_as_the_tx_s_own_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`send_raw_transaction` has no catch of its own for a storage fault.

    Unlike `MissingPrevoutError` and `BTClibValueError` above,
    `StoreCorruptionError` -- the one exception `verify_mempool_acceptance`
    can still raise for this node's own storage rather than for `tx`'s
    content, once btclib-org/btclib-node#650 stopped `UtxoIndex.get_coin`
    raising `ChainstateInconsistencyError` for the other, checksum-clean
    case -- propagates out of `send_raw_transaction` uncaught, for
    `handle_rpc`'s own generic catch to answer as this node's own fault
    rather than as a refusal of `tx` (btclib-org/btclib-node#631).
    """

    def corrupted(node: Any, transaction: Any) -> NoReturn:
        err_msg = "stored utxo- record failed to parse"
        raise StoreCorruptionError(err_msg)

    monkeypatch.setattr(cb, "verify_mempool_acceptance", corrupted)
    tx = a_tx()
    mempool = Mempool(Logger(debug=True))
    broadcast: list[Tx] = []
    node = a_node(mempool=mempool)
    node.p2p_manager.broadcast_raw_transaction = lambda tx, fee: broadcast.append(tx)

    with pytest.raises(StoreCorruptionError):
        send_raw_transaction(node, _CONN, [tx.serialize(include_witness=True).hex()])
    assert not mempool.contains_tx(tx)
    assert broadcast == []


def test_get_network_info_answers_this_node_s_own_subversion_and_protocol() -> None:
    """`getnetworkinfo` answers `subversion`/`protocolversion` and the services.

    `connect_nodes`'s own read is `subversion` alone
    (`test_framework.py:568-594`, at bitcoin/bitcoin@bb529657);
    `protocolversion` is included beside it as a real, cheaply-answered
    constant rather than as decoration. `NODE_NETWORK_LIMITED |
    NODE_WITNESS` is `Config`'s own defaults, unpruned and with
    `-peerblockfilters` off.
    """
    result = get_network_info(a_node(), _CONN, [])
    assert result == {
        "subversion": USER_AGENT,
        "protocolversion": PROTOCOL_VERSION,
        "localservices": f"{ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_NETWORK_LIMITED | ServiceFlags.NODE_WITNESS:016x}",
        "localservicesnames": ["NETWORK", "WITNESS", "NETWORK_LIMITED"],
    }


def test_get_network_info_s_localservices_follows_pruned_and_peerblockfilters() -> None:
    """ISS 1394: pruned drops `NODE_NETWORK`, `peerblockfilters` adds a bit.

    The same `local_services` `own_version` sends, so the two never
    disagree about what this node advertises (ISS 1394's own "Fix").
    """
    result = get_network_info(a_node(pruned=True, peerblockfilters=True), _CONN, [])
    assert result["localservices"] == (
        f"{ServiceFlags.NODE_NETWORK_LIMITED | ServiceFlags.NODE_WITNESS | ServiceFlags.NODE_COMPACT_FILTERS:016x}"
    )
    assert result["localservicesnames"] == [
        "WITNESS",
        "COMPACT_FILTERS",
        "NETWORK_LIMITED",
    ]


def test_addnode_onetry_dials_the_given_address_once() -> None:
    """`addnode "host:port" "onetry"` schedules exactly one dial."""
    dialed: list[Any] = []
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=dialed.append),
        ),
    )
    add_node(node, _CONN, ["127.0.0.1:9999", "onetry"])
    assert len(dialed) == 1
    assert dialed[0].network_id.name == "IPV4"
    assert dialed[0].port == 9999


def test_addnode_falls_back_to_the_chain_s_own_default_port() -> None:
    """A `node` naming no port dials this chain's own default one."""
    dialed: list[Any] = []
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=dialed.append),
        ),
    )
    add_node(node, _CONN, ["127.0.0.1", "onetry"])
    assert dialed[0].port == 18444


def test_addnode_add_also_dials_once_rather_than_persisting() -> None:
    """`addnode ... "add"` is accepted, and dialled the same as `onetry`.

    This node keeps no added-node list distinct from `Config.addnode`'s
    own startup tuple, so `add` does not persist across a later dial the
    way Core's own `CConnman::AddNode` does -- the module-level comment
    beside `_ADDNODE_COMMANDS` argues why dialling once and not raising
    is the more faithful of the two shortfalls available.
    """
    dialed: list[Any] = []
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=dialed.append),
        ),
    )
    add_node(node, _CONN, ["127.0.0.1:9999", "add"])
    assert len(dialed) == 1


def test_addnode_remove_answers_not_added_every_time() -> None:
    """`addnode ... "remove"` is Core's own `RPC_CLIENT_NODE_NOT_ADDED`.

    There is nothing this node ever added by RPC for it to find.
    """
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=lambda _address: None),
        ),
    )
    with pytest.raises(RpcError) as raised:
        add_node(node, _CONN, ["127.0.0.1:9999", "remove"])
    assert raised.value.code == RPCErrorCode.CLIENT_NODE_NOT_ADDED
    assert raised.value.message == (
        "Error: Node could not be removed. It has not been added previously."
    )


def test_addnode_refuses_an_empty_node_address() -> None:
    """Core's own exact text for a blank `node` argument."""
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=lambda _address: None),
        ),
    )
    with pytest.raises(RpcError) as raised:
        add_node(node, _CONN, ["   ", "onetry"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == "Error: Node address cannot be empty"


def test_addnode_refuses_an_unknown_command() -> None:
    """A `command` outside `add`/`remove`/`onetry` is refused with the help."""
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=lambda _address: None),
        ),
    )
    with pytest.raises(RpcError) as raised:
        add_node(node, _CONN, ["127.0.0.1:9999", "bogus"])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["addnode"]


def test_addnode_with_no_arguments_is_answered_with_the_usage() -> None:
    """Fewer than the two required arguments is refused with the help."""
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=lambda _address: None),
        ),
    )
    with pytest.raises(RpcError) as raised:
        add_node(node, _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["addnode"]


def test_addnode_refuses_a_hostname() -> None:
    """A hostname, rather than a literal IP, is refused: no DNS resolve here.

    The same refusal `Config.addnode`'s own `_resolve_peers` already
    gives `-addnode`'s spec, and for the identical reason.
    """
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=lambda _address: None),
        ),
    )
    with pytest.raises(RpcError) as raised:
        add_node(node, _CONN, ["example.com:9999", "onetry"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER


def test_addnode_refuses_a_port_int_would_read() -> None:
    """`127.0.0.1:+80` is refused as `127.0.0.1:0x50` is, not dialled at 80."""
    dialled: list[object] = []
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=dialled.append),
        ),
    )
    with pytest.raises(RpcError) as raised:
        add_node(node, _CONN, ["127.0.0.1:+80", "onetry"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert dialled == []


def test_addnode_type_checks_node_and_command() -> None:
    """`node` and `command` of the wrong JSON type are named, not coerced."""
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=lambda _address: None),
        ),
    )
    with pytest.raises(RpcError) as raised:
        add_node(node, _CONN, [1, "onetry"])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    with pytest.raises(RpcError) as raised2:
        add_node(node, _CONN, ["127.0.0.1", 1])
    assert raised2.value.code == RPCErrorCode.TYPE_ERROR


def test_addnode_names_both_wrongly_typed_arguments_at_once() -> None:
    """ISS 1293: `addnode [1, 2]` names both, as a real bitcoind does.

    Measured against a regtest bitcoind v31.1.0: `addnode` called with
    a number `node` and a number `command` answers `"Position 1 (node)"`
    and `"Position 2 (command)"` in one `Wrong type passed` object, not
    the first alone.
    """
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=lambda _address: None),
        ),
    )
    with pytest.raises(RpcError) as raised:
        add_node(node, _CONN, [1, 2])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        "Wrong type passed:\n"
        "{\n"
        '    "Position 1 (node)": "JSON value of type number is not of'
        ' expected type string",\n'
        '    "Position 2 (command)": "JSON value of type number is not'
        ' of expected type string"\n'
        "}"
    )


def test_addnode_names_all_three_wrongly_typed_arguments_at_once() -> None:
    """ISS 1293: a wrongly typed `v2transport` is named alongside the rest.

    `add_node`'s own `v2transport_mismatch` -- built by `bool_mismatch`
    rather than by `bool_param`'s own raise, for exactly this reason --
    is otherwise never exercised: neither
    `test_addnode_type_checks_node_and_command` above nor
    `test_addnode_names_both_wrongly_typed_arguments_at_once` above it
    ever passes a third argument at all.
    """
    node = cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(port=18444),
            p2p_manager=SimpleNamespace(connect=lambda _address: None),
        ),
    )
    with pytest.raises(RpcError) as raised:
        add_node(node, _CONN, [1, 2, "not a bool"])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        "Wrong type passed:\n"
        "{\n"
        '    "Position 1 (node)": "JSON value of type number is not of'
        ' expected type string",\n'
        '    "Position 2 (command)": "JSON value of type number is not'
        ' of expected type string",\n'
        '    "Position 3 (v2transport)": "JSON value of type string is'
        ' not of expected type bool"\n'
        "}"
    )


_BAN_NOW = 1_700_000_000


def a_banning_node(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
    """Build a node double with a ban list, its clock stopped at `_BAN_NOW`.

    The list is the second element's record of every subnet
    `disconnect_subnet` was asked to drop, as `str`.
    """
    monkeypatch.setattr(banman_module, "_now", lambda: _BAN_NOW)
    monkeypatch.setattr(time, "time", lambda: _BAN_NOW + 0.5)
    dropped: list[str] = []
    node = SimpleNamespace(
        p2p_manager=SimpleNamespace(
            ban_man=BanMan(None, Logger(debug=True)),
            disconnect_subnet=lambda subnet: dropped.append(str(subnet)),
        )
    )
    return node, dropped


def refusal(node: Any, params: list[Any]) -> RpcError:
    """Return what `setban` raised for `params`, asserting it raised."""
    with pytest.raises(RpcError) as raised:
        set_ban(node, _CONN, params)
    return raised.value


def test_setban_is_in_the_method_table() -> None:
    """`setban`, `listbanned` and `clearbanned` are Core's names."""
    assert callbacks["setban"] is set_ban
    assert callbacks["listbanned"] is list_banned
    assert callbacks["clearbanned"] is clear_banned


def test_setban_add_bans_and_drops_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core's `setban add`: the ban, then `DisconnectNode` of what it bans.

    `listbanned` answers it with Core's five fields, a day long by
    default.
    """
    node, dropped = a_banning_node(monkeypatch)
    set_ban(node, _CONN, ["1.2.3.4", "add"])
    set_ban(node, _CONN, ["5.6.0.0/16", "add", 100, None])
    assert dropped == ["1.2.3.4/32", "5.6.0.0/16"]
    assert list_banned(node, _CONN, []) == [
        {
            "address": "1.2.3.4/32",
            "ban_created": _BAN_NOW,
            "banned_until": _BAN_NOW + 86400,
            "ban_duration": 86400,
            "time_remaining": 86400,
        },
        {
            "address": "5.6.0.0/16",
            "ban_created": _BAN_NOW,
            "banned_until": _BAN_NOW + 100,
            "ban_duration": 100,
            "time_remaining": 100,
        },
    ]


def test_setban_add_refuses_what_is_already_banned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An address inside any ban is banned already, a subnet only by its key."""
    node, dropped = a_banning_node(monkeypatch)
    set_ban(node, _CONN, ["1.2.3.0/24", "add"])
    for params in (["1.2.3.4", "add"], ["1.2.3.0/255.255.255.0", "add"]):
        error = refusal(node, params)
        assert error.code == RPCErrorCode.CLIENT_NODE_ALREADY_ADDED
        assert error.message == "Error: IP/Subnet already banned"
    set_ban(node, _CONN, ["1.2.3.0/25", "add"])
    assert dropped == ["1.2.3.0/24", "1.2.3.0/25"]


@pytest.mark.parametrize(
    "subnet",
    [
        "bloop",
        "1.2.3.0/33",
        # without a slash the address has to be a valid one
        "0.0.0.0",  # noqa: S104
        "2001:db8::1",
        "fd87:d87e:eb43::1",
    ],
)
def test_setban_refuses_what_is_no_ip_nor_subnet(
    monkeypatch: pytest.MonkeyPatch, subnet: str
) -> None:
    """Core's `RPC_CLIENT_INVALID_IP_OR_SUBNET`, for add and remove alike."""
    node, _ = a_banning_node(monkeypatch)
    for command in ("add", "remove"):
        error = refusal(node, [subnet, command])
        assert error.code == RPCErrorCode.CLIENT_INVALID_IP_OR_SUBNET
        assert error.message == "Error: Invalid IP/Subnet"


_ONION = "pg6mmjiyjmcrsslvykfwnntlaru7p5svn6y2ymmju6nubxndf4pscryd.onion"
_I2P = "udhdrtrcetjm5sxzskjyr5ztpeszydbh4dpl3pl4utgqqw2v4jna.b32.i2p"


def test_setban_bans_an_onion_or_i2p_host_and_no_subnet_of_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1218: as bitcoind v31.1 answers `setban`.

    The host alone, each on its key whatever its case; a prefix is no
    subnet of one. `listbanned` lists them after the IP bans.
    """
    node, dropped = a_banning_node(monkeypatch)
    set_ban(node, _CONN, [_I2P, "add"])
    set_ban(node, _CONN, [_ONION, "add"])
    set_ban(node, _CONN, ["1.2.3.4", "add"])
    assert dropped == [_I2P, _ONION, "1.2.3.4/32"]
    upper = _ONION.removesuffix(".onion").upper() + ".onion"
    for params in ([upper, "add"], [f"[{_ONION}]", "add"], [_I2P.upper(), "add"]):
        assert refusal(node, params).code == RPCErrorCode.CLIENT_NODE_ALREADY_ADDED
    error = refusal(node, [f"{_ONION}/32", "add"])
    assert error.code == RPCErrorCode.CLIENT_INVALID_IP_OR_SUBNET
    assert [entry["address"] for entry in list_banned(node, _CONN, [])] == [
        "1.2.3.4/32",
        _ONION,
        _I2P,
    ]
    set_ban(node, _CONN, [upper, "remove"])
    assert [entry["address"] for entry in list_banned(node, _CONN, [])] == [
        "1.2.3.4/32",
        _I2P,
    ]


def test_setban_bans_a_scoped_address_on_its_address_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1220: `fe80::1%2` is banned already, and removed, by `fe80::1%1`."""
    node, _ = a_banning_node(monkeypatch)
    set_ban(node, _CONN, ["fe80::1%1", "add"])
    error = refusal(node, ["fe80::1%2", "add"])
    assert error.code == RPCErrorCode.CLIENT_NODE_ALREADY_ADDED
    assert [entry["address"] for entry in list_banned(node, _CONN, [])] == [
        "fe80::1%1/128"
    ]
    set_ban(node, _CONN, ["fe80::1%2", "remove"])
    assert list_banned(node, _CONN, []) == []


def test_setban_takes_an_invalid_address_as_a_subnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a slash, `CSubNet` is valid of any IPv4 or IPv6 address."""
    node, _ = a_banning_node(monkeypatch)
    set_ban(node, _CONN, ["0.0.0.0/32", "add"])
    assert [entry["address"] for entry in list_banned(node, _CONN, [])] == [
        "0.0.0.0/32"
    ]


def test_setban_remove_answers_whether_there_was_a_ban(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's `Unban failed` for a subnet not on the list, by its exact key."""
    node, _ = a_banning_node(monkeypatch)
    set_ban(node, _CONN, ["1.2.3.0/24", "add"])
    error = refusal(node, ["1.2.3.4", "remove"])
    assert error.code == RPCErrorCode.CLIENT_INVALID_IP_OR_SUBNET
    assert error.message == (
        "Error: Unban failed. Requested address/subnet was not previously"
        " manually banned."
    )
    # the bantime is read by `add` alone
    set_ban(node, _CONN, ["1.2.3.0/24", "remove", 1.5])
    assert list_banned(node, _CONN, []) == []


def test_setban_absolute_bans_until_the_time_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absolute bantime is when the ban ends, and never in the past."""
    node, _ = a_banning_node(monkeypatch)
    error = refusal(node, ["1.2.3.4", "add", _BAN_NOW - 1, True])
    assert error.code == RPCErrorCode.INVALID_PARAMETER
    assert error.message == "Error: Absolute timestamp is in the past"
    set_ban(node, _CONN, ["1.2.3.4", "add", _BAN_NOW, True])
    (entry,) = node.p2p_manager.ban_man.banned()
    assert entry[1] == BanEntry(_BAN_NOW, _BAN_NOW)


def test_setban_add_past_int64_succeeds_and_bans_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bantime past `int64_t` succeeds and bans nothing, as in Core v31.1.0.

    The host is dropped all the same, `DisconnectNode` following `Ban`
    whatever `Ban` did.
    """
    node, dropped = a_banning_node(monkeypatch)
    set_ban(node, _CONN, ["11.0.0.1", "add", (1 << 63) - 1])
    assert list_banned(node, _CONN, []) == []
    assert dropped == ["11.0.0.1/32"]


@pytest.mark.parametrize("params", [[], ["1.2.3.4"], ["1.2.3.4", "ban"]])
def test_setban_answers_the_usage(
    monkeypatch: pytest.MonkeyPatch, params: list[Any]
) -> None:
    """Too few arguments, or a command Core has no case for, is refused.

    A call carrying too many is `rpc.main._execute`'s own refusal now,
    generic across every method (`main_test.py`'s own coverage of it),
    not `set_ban`'s -- this direct call bypasses that layer entirely.
    """
    node, _ = a_banning_node(monkeypatch)
    error = refusal(node, params)
    assert error.code == RPCErrorCode.MISC_ERROR
    assert error.message == HELP_TEXT["setban"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ([1, "add"], "Position 1 (subnet)"),
        (["1.2.3.4", 1], "Position 2 (command)"),
        (["1.2.3.4", "add", "1"], "Position 3 (bantime)"),
        (["1.2.3.4", "add", True], "Position 3 (bantime)"),
        (["1.2.3.4", "add", 1, "yes"], "Position 4 (absolute)"),
        # the types are checked ahead of the command
        (["1.2.3.4", "ban", "1"], "Position 3 (bantime)"),
    ],
)
def test_setban_type_checks_every_argument(
    monkeypatch: pytest.MonkeyPatch, params: list[Any], expected: str
) -> None:
    """Core's `HandleRequest` names the argument of the wrong JSON type."""
    node, _ = a_banning_node(monkeypatch)
    error = refusal(node, params)
    assert error.code == RPCErrorCode.TYPE_ERROR
    assert expected in error.message


def test_setban_names_every_wrongly_typed_argument_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1293: all four wrong at once are all named, not the first alone."""
    node, _ = a_banning_node(monkeypatch)
    error = refusal(node, [1, 2, "x", "y"])
    assert error.code == RPCErrorCode.TYPE_ERROR
    assert error.message == (
        "Wrong type passed:\n"
        "{\n"
        '    "Position 1 (subnet)": "JSON value of type number is not'
        ' of expected type string",\n'
        '    "Position 2 (command)": "JSON value of type number is not'
        ' of expected type string",\n'
        '    "Position 3 (bantime)": "JSON value of type string is not'
        ' of expected type number",\n'
        '    "Position 4 (absolute)": "JSON value of type string is not'
        ' of expected type bool"\n'
        "}"
    )


@pytest.mark.parametrize("bantime", [1.5, 1 << 63])
def test_setban_add_refuses_a_bantime_out_of_range(
    monkeypatch: pytest.MonkeyPatch, bantime: float
) -> None:
    """UniValue's `getInt<int64_t>`: no fraction, and within 64 bits."""
    node, _ = a_banning_node(monkeypatch)
    error = refusal(node, ["1.2.3.4", "add", bantime])
    assert error.code == RPCErrorCode.MISC_ERROR
    assert error.message == "JSON integer out of range"


def test_clearbanned_empties_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core's `clearbanned`."""
    node, _ = a_banning_node(monkeypatch)
    set_ban(node, _CONN, ["1.2.3.4", "add"])
    subnet = lookup_subnet("1.2.3.4")
    assert subnet is not None
    assert node.p2p_manager.ban_man.is_subnet_banned(subnet)
    clear_banned(node, _CONN, [])
    assert list_banned(node, _CONN, []) == []


def test_get_block_answers_the_hex_serialization_of_a_stored_block(
    regtest_node: Callable[..., Node],
) -> None:
    """`getblock` verbosity 0 answers the block's own bytes, hex-encoded."""
    node = regtest_node()
    chain = generate_random_chain(2, node.chain.genesis.hash)
    connect(node, chain)

    answer = get_block(node, _CONN, [chain[0].header.hash.hex(), 0])

    assert answer == chain[0].serialize(check_validity=False).hex()


def test_get_block_false_answers_the_same_hex_zero_does(
    regtest_node: Callable[..., Node],
) -> None:
    """`ParseVerbosity`'s own `allow_bool=true`: `false` is `0`."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)

    zero = get_block(node, _CONN, [chain[0].header.hash.hex(), 0])
    false = get_block(node, _CONN, [chain[0].header.hash.hex(), False])

    assert zero == false == chain[0].serialize(check_validity=False).hex()


def test_get_block_refuses_a_hash_this_node_has_never_indexed(
    regtest_node: Callable[..., Node],
) -> None:
    """An unknown blockhash is Core's own `Block not found`."""
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        get_block(node, _CONN, [(b"\x11" * 32).hex(), 0])
    assert raised.value.code == RPCErrorCode.INVALID_ADDRESS_OR_KEY
    assert raised.value.message == "Block not found"


def test_get_block_refuses_verbosity_past_2(
    regtest_node: Callable[..., Node],
) -> None:
    """Verbosity 3 (Core's own prevout detail) and past it are still refused.

    Verbosity 1 and 2 are answered now; the tests below cover them.
    """
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    block_hash_hex = chain[0].header.hash.hex()

    for params in ([block_hash_hex, 3], [block_hash_hex, -1]):
        with pytest.raises(RpcError) as raised:
            get_block(node, _CONN, params)
        assert raised.value.code == RPCErrorCode.MISC_ERROR
        assert raised.value.message == (
            "getblock: only verbosity 0, 1 and 2 are served here"
        )


def test_get_block_verbosity_1_answers_the_header_plus_size_and_txids(
    regtest_node: Callable[..., Node],
) -> None:
    """Verbosity 1, Core's own default, answers the header, size and txids."""
    node = regtest_node()
    chain = generate_random_chain(2, node.chain.genesis.hash)
    connect(node, chain)
    block = chain[0]

    for params in ([block.header.hash.hex()], [block.header.hash.hex(), True]):
        answer = get_block(node, _CONN, params)
        assert isinstance(answer, dict)
        assert answer["hash"] == block.header.hash
        assert answer["height"] == 1
        assert answer["confirmations"] == 2
        assert answer["strippedsize"] == block.stripped_size
        assert answer["size"] == block.size
        assert answer["weight"] == block.weight
        assert answer["nTx"] == len(block.transactions)
        assert answer["tx"] == [tx.id for tx in block.transactions]
        assert answer["previousblockhash"] == block.header.previous_block_hash
        assert answer["nextblockhash"] == chain[1].header.hash
        assert answer["coinbase_tx"]["coinbase"] == (
            block.transactions[0].vin[0].script_sig.hex()
        )
        assert answer["coinbase_tx"]["version"] == block.transactions[0].version


def test_get_block_verbosity_2_answers_decoded_transactions(
    regtest_node: Callable[..., Node],
) -> None:
    """Verbosity 2 answers `tx` as decoded objects, not a bare txid array."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    block = chain[0]

    answer = get_block(node, _CONN, [block.header.hash.hex(), 2])

    assert isinstance(answer, dict)
    entries = answer["tx"]
    assert len(entries) == len(block.transactions)
    for entry, tx in zip(entries, block.transactions, strict=True):
        assert entry["txid"] == tx.id.hex()
        assert entry["hex"] == tx.serialize(include_witness=True).hex()
        assert "fee" not in entry


def test_get_block_verbosity_1_answers_the_coinbase_witness(
    regtest_node: Callable[..., Node],
) -> None:
    """A segwit coinbase's own witness stack answers `coinbase_tx["witness"]`.

    `coinbaseTxToJSON`'s own `CHECK_NONFATAL(witness_stack.size() == 1)`
    (`src/rpc/blockchain.cpp:195`, at bitcoin/bitcoin@9be056a8a7): the
    coinbase's own BIP141 nonce, once the block itself commits to one.
    """
    node = regtest_node()
    block = generate_segwit_block()
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header])
    node.block_db.add_block(block)
    block_index.set_downloaded(block.header.hash)

    answer = get_block(node, _CONN, [block.header.hash.hex(), 1])

    assert isinstance(answer, dict)
    nonce = block.transactions[0].vin[0].script_witness.stack[0]
    assert answer["coinbase_tx"]["witness"] == nonce.hex()


def test_get_block_genesis_has_no_previousblockhash(
    regtest_node: Callable[..., Node],
) -> None:
    """Genesis, height 0, is the one block missing `previousblockhash` here."""
    node = regtest_node()

    answer = get_block(node, _CONN, [node.chain.genesis.hash.hex(), 1])

    assert isinstance(answer, dict)
    assert answer["height"] == 0
    assert "previousblockhash" not in answer


def test_get_block_refuses_a_fractional_verbosity(
    regtest_node: Callable[..., Node],
) -> None:
    """A JSON number with a decimal point fails `getInt<int>()`, Core's way."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)

    with pytest.raises(RpcError) as raised:
        get_block(node, _CONN, [chain[0].header.hash.hex(), 1.5])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "JSON integer out of range"


def test_get_block_refuses_a_verbosity_of_the_wrong_json_type(
    regtest_node: Callable[..., Node],
) -> None:
    """A `verbosity` that is neither bool, int nor float is named, not read."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)

    with pytest.raises(RpcError) as raised:
        get_block(node, _CONN, [chain[0].header.hash.hex(), "1"])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR


def test_get_block_verbosity_false_and_2_are_not_confused_with_each_other(
    regtest_node: Callable[..., Node],
) -> None:
    """`false` is still verbosity 0, `ParseVerbosity`'s own bool degrading."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)
    block_hash_hex = chain[0].header.hash.hex()

    hex_answer = get_block(node, _CONN, [block_hash_hex, False])
    json_answer = get_block(node, _CONN, [block_hash_hex, 2])

    assert hex_answer == chain[0].serialize(check_validity=False).hex()
    assert isinstance(json_answer, dict)


def test_get_block_refuses_a_block_this_node_has_pruned(
    regtest_node: Callable[..., Node],
) -> None:
    """A pruned block answers Core's own pruned-data message."""
    node = regtest_node(pruned=True, prune_target_mib=None)
    chain = generate_random_chain(
        node.chain.prune_after_height + 5, node.chain.genesis.hash
    )
    block_index = connect(node, chain)
    prune_blockchain(node, _CONN, [3])

    with pytest.raises(RpcError) as raised:
        get_block(node, _CONN, [block_index.active_chain[1].hex(), 0])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "Block not available (pruned data)"


def test_get_block_with_no_arguments_is_answered_with_the_usage(
    regtest_node: Callable[..., Node],
) -> None:
    """`getblock` with no arguments at all is refused with its own help."""
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        get_block(node, _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["getblock"]


def test_get_block_refuses_a_blockhash_of_the_wrong_json_type(
    regtest_node: Callable[..., Node],
) -> None:
    """A `blockhash` that is not a string is named, not coerced."""
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        get_block(node, _CONN, [12345, 0])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR


def test_get_block_refuses_a_blockhash_that_is_not_hexadecimal(
    regtest_node: Callable[..., Node],
) -> None:
    """A right-length, non-hex `blockhash` is Core's own `ParseHashV` text."""
    node = regtest_node()
    not_hex = "z" * 64
    with pytest.raises(RpcError) as raised:
        get_block(node, _CONN, [not_hex, 0])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert (
        raised.value.message
        == f"blockhash must be hexadecimal string (not '{not_hex}')"
    )


def test_get_block_refuses_a_blockhash_of_the_wrong_length(
    regtest_node: Callable[..., Node],
) -> None:
    """A valid-hex, wrong-length `blockhash` is refused before it is decoded."""
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        get_block(node, _CONN, ["aabb", 0])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == "blockhash must be of length 64 (not 4, for 'aabb')"


def test_get_block_refuses_a_header_only_block_as_not_fully_downloaded(
    regtest_node: Callable[..., Node],
) -> None:
    """A block this node has only ever indexed the header of is not pruned.

    `_find_transaction`'s own identical distinction, exercised here for
    the branch `test_get_block_refuses_a_block_this_node_has_pruned`
    above does not reach: an ordinary, unpruned node whose peer has
    sent a header but never the block body.
    """
    node = regtest_node()
    header = generate_random_header_chain(1, node.chain.genesis.hash)[0]
    node.chainstate.block_index.add_headers([header])

    with pytest.raises(RpcError) as raised:
        get_block(node, _CONN, [header.hash.hex(), 0])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "Block not available (not fully downloaded)"


def test_get_chain_tips_answers_only_the_active_tip_on_a_single_chain(
    regtest_node: Callable[..., Node],
) -> None:
    """With no fork, the one tip is the active chain's own, `branchlen: 0`."""
    node = regtest_node()
    chain = generate_random_chain(2, node.chain.genesis.hash)
    connect(node, chain)

    tips = get_chain_tips(node, _CONN, [])

    assert len(tips) == 1
    assert tips[0] == {
        "height": 2,
        "hash": chain[-1].header.hash,
        "branchlen": 0,
        "status": "active",
    }


def test_get_chain_tips_answers_a_headers_only_fork(
    regtest_node: Callable[..., Node],
) -> None:
    """A header-only fork's tip is `headers-only`, `branchlen` its length."""
    node = regtest_node()
    chain = generate_random_chain(2, node.chain.genesis.hash)
    connect(node, chain)
    fork = generate_random_header_chain(2, chain[0].header.hash, chain[0].header.time)
    node.chainstate.block_index.add_headers(fork)

    tips = get_chain_tips(node, _CONN, [])

    by_hash = {tip["hash"]: tip for tip in tips}
    assert len(tips) == 2
    fork_tip = by_hash[fork[-1].hash]
    assert fork_tip["status"] == "headers-only"
    assert fork_tip["branchlen"] == 2
    assert fork_tip["height"] == 3
    assert by_hash[chain[-1].header.hash]["status"] == "active"


def test_get_chain_tips_answers_a_reorged_away_fork(
    regtest_node: Callable[..., Node],
) -> None:
    """A downloaded fork `BlockStatus.valid` marks is `valid-fork`.

    `_finalize_fork`'s own `to_remove` loop is the only place that sets
    that status, on a block a reorg has dropped from the active chain
    after it was itself once connected; `set_status` reaches the same
    state directly, the same shortcut
    `tests/unit/p2p/callbacks_test.py`'s own reorg tests already take.
    """
    node = regtest_node()
    chain = generate_random_chain(2, node.chain.genesis.hash)
    connect(node, chain)
    height = len(chain) + 1
    block = build_block(
        chain[-1].header.hash, [generate_coinbase(height=height)], height
    )
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header])
    node.block_db.add_block(block)
    block_index.set_downloaded(block.header.hash)
    block_index.set_status(block.header.hash, BlockStatus.valid)

    tips = get_chain_tips(node, _CONN, [])

    by_hash = {tip["hash"]: tip for tip in tips}
    assert by_hash[block.header.hash]["status"] == "valid-fork"
    assert by_hash[block.header.hash]["branchlen"] == 1


def test_get_chain_tips_answers_a_downloaded_never_activated_fork(
    regtest_node: Callable[..., Node],
) -> None:
    """A block whose body is downloaded but never connected is `valid-headers`.

    Never offered to `update_chain` at all, so it stays `valid_header` in
    `BlockIndex` -- `set_downloaded` alone is enough for `getchaintips` to
    tell it apart from a header-only tip. Built extending the active
    chain's own tip, at its real next height, rather than a second,
    independently-dated fork off a block already connected: two chains
    built by `generate_random_chain` from two different starting hashes
    date their own blocks the identical way relative to each one's own
    start, so a second one forking mid-chain collides on the parent's
    own timestamp instead of following it.
    """
    node = regtest_node()
    chain = generate_random_chain(2, node.chain.genesis.hash)
    connect(node, chain)
    height = len(chain) + 1
    block = build_block(
        chain[-1].header.hash, [generate_coinbase(height=height)], height
    )
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header])
    node.block_db.add_block(block)
    block_index.set_downloaded(block.header.hash)

    tips = get_chain_tips(node, _CONN, [])

    by_hash = {tip["hash"]: tip for tip in tips}
    assert by_hash[block.header.hash]["status"] == "valid-headers"
    assert by_hash[block.header.hash]["branchlen"] == 1


def test_get_chain_tips_answers_an_invalid_fork(
    regtest_node: Callable[..., Node],
) -> None:
    """A fork `BlockIndex.invalidate` has marked answers `invalid`."""
    node = regtest_node()
    chain = generate_random_chain(2, node.chain.genesis.hash)
    connect(node, chain)
    fork = generate_random_header_chain(1, chain[0].header.hash, chain[0].header.time)
    block_index = node.chainstate.block_index
    block_index.add_headers(fork)
    block_index.invalidate(fork[0].hash)

    tips = get_chain_tips(node, _CONN, [])

    by_hash = {tip["hash"]: tip for tip in tips}
    assert by_hash[fork[0].hash]["status"] == "invalid"


def a_block_claiming_an_easier_target_than_the_chain_allows(block: Block) -> Block:
    """Rebuild `block` with `bits` set past regtest's own proof-of-work limit.

    The identical construction `tests/unit/p2p/callbacks_test.py`'s own
    helper of the same name uses, for the identical reason: regtest's
    limit is `7fffff00...`, and `800000...` is the next target up that
    still fits the field, so `assert_valid` refuses it deterministically
    rather than by chance of a nonce.
    """
    header = BlockHeader(
        version=block.header.version,
        previous_block_hash=block.header.previous_block_hash,
        merkle_root=block.header.merkle_root,
        time=block.header.time,
        bits=b"\x21\x00\x80\x00",
        nonce=block.header.nonce,
        check_validity=False,
    )
    return Block(header, block.transactions, check_validity=False)


def test_submit_block_accepts_a_new_block_extending_the_tip(
    regtest_node: Callable[..., Node],
) -> None:
    """A valid new block answers `None`, Core's own shape for acceptance."""
    node = regtest_node()
    chain = generate_random_chain(4, node.chain.genesis.hash)
    connect(node, chain[:3])
    new_block = chain[3]

    result = submit_block(
        node, _CONN, [new_block.serialize(check_validity=False).hex()]
    )

    assert result is None
    block_info = node.chainstate.block_index.get_block_info(new_block.header.hash)
    assert block_info.downloaded
    stored = node.block_db.get_block(new_block.header.hash)
    assert stored is not None
    assert stored.serialize(check_validity=False) == new_block.serialize(
        check_validity=False
    )


def test_submit_block_stores_valid_a_block_off_a_known_non_tip_ancestor(
    regtest_node: Callable[..., Node],
) -> None:
    """A valid block extending a known block that is not the tip answers `None`.

    Its own parent is indexed already (genesis), so this never reaches
    `prev-blk-not-found`; not extending the active chain's own tip, it
    also never reaches `_validate_extending_tip`, and is merely stored --
    the active chain itself does not move.
    """
    node = regtest_node()
    chain = generate_random_chain(2, node.chain.genesis.hash)
    connect(node, chain)
    fork = build_block(node.chain.genesis.hash, [generate_coinbase(height=1)], height=5)

    result = submit_block(node, _CONN, [fork.serialize(check_validity=False).hex()])

    assert result is None
    block_info = node.chainstate.block_index.get_block_info(fork.header.hash)
    assert block_info.downloaded
    assert node.block_db.get_block(fork.header.hash) is not None
    assert node.chainstate.block_index.active_chain[-1] == chain[1].header.hash


def test_submit_block_answers_duplicate_for_a_block_already_downloaded(
    regtest_node: Callable[..., Node],
) -> None:
    """A block this node already holds answers Core's own `"duplicate"`."""
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    connect(node, chain)

    result = submit_block(node, _CONN, [chain[0].serialize(check_validity=False).hex()])

    assert result == "duplicate"


@pytest.mark.parametrize("body", ["another-coinbase", "coinbase-twice"])
def test_submit_block_refuses_a_body_its_stored_header_does_not_commit_to(
    regtest_node: Callable[..., Node], body: str
) -> None:
    """ISS 1346: Core's `CheckBlock` runs before the block is found stored.

    Measured against bitcoind v31.1: `bad-txnmrklroot` for both bodies,
    where this node answers btclib's own reason. The stored block and its
    header are left as they were.
    """
    node = regtest_node()
    (block,) = generate_random_chain(1, node.chain.genesis.hash)
    raw = block.serialize(check_validity=False).hex()
    assert submit_block(node, _CONN, [raw]) is None
    transactions = (
        [generate_coinbase(value=1, height=1)]
        if body == "another-coinbase"
        else [*block.transactions, *block.transactions]
    )
    other = Block(block.header, transactions, check_validity=False)

    result = submit_block(node, _CONN, [other.serialize(check_validity=False).hex()])

    assert isinstance(result, str)
    assert result not in (None, "duplicate")
    assert node.block_db.get_block(block.header.hash) == block
    block_info = node.chainstate.block_index.get_block_info(block.header.hash)
    assert block_info.downloaded
    assert block_info.status != BlockStatus.invalid
    assert submit_block(node, _CONN, [raw]) == "duplicate"


def test_submit_block_answers_prev_blk_not_found_for_an_orphan(
    regtest_node: Callable[..., Node],
) -> None:
    """A block whose parent this node has never indexed names Core's reason.

    `validation.cpp:4225`'s own `"prev-blk-not-found"`
    (at bitcoin/bitcoin@bb529657), the one reject reason this tree
    reproduces literally -- `submit_block`'s own docstring is where that
    is argued against the rest of Core's own vocabulary.
    """
    node = regtest_node()
    orphan = generate_random_chain(1, b"\x22" * 32)[0]

    result = submit_block(node, _CONN, [orphan.serialize(check_validity=False).hex()])

    assert result == "prev-blk-not-found"


def test_submit_block_answers_decode_failed_for_unparsable_hex(
    regtest_node: Callable[..., Node],
) -> None:
    """Core's own exact text for a `hexdata` that does not decode at all."""
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        submit_block(node, _CONN, ["not hex"])
    assert raised.value.code == RPCErrorCode.DESERIALIZATION_ERROR
    assert raised.value.message == "Block decode failed"


def test_submit_block_with_no_arguments_is_answered_with_the_usage(
    regtest_node: Callable[..., Node],
) -> None:
    """`submitblock` with no arguments at all is refused with its own help."""
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        submit_block(node, _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["submitblock"]


def test_submit_block_refuses_a_hexdata_of_the_wrong_json_type(
    regtest_node: Callable[..., Node],
) -> None:
    """A `hexdata` that is not a string is named, not coerced."""
    node = regtest_node()
    with pytest.raises(RpcError) as raised:
        submit_block(node, _CONN, [12345])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR


def test_submit_block_completes_a_block_whose_header_alone_was_already_known(
    regtest_node: Callable[..., Node],
) -> None:
    """A block this node knows only the header of is stored, not `"duplicate"`.

    `"duplicate"` is for a body already downloaded; a header sync ahead
    of block download, matching `p2p.callbacks.block`'s own identical
    case, is not that.
    """
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    node.chainstate.block_index.add_headers([chain[0].header])

    result = submit_block(node, _CONN, [chain[0].serialize(check_validity=False).hex()])

    assert result is None
    assert node.chainstate.block_index.get_block_info(chain[0].header.hash).downloaded
    stored = node.block_db.get_block(chain[0].header.hash)
    assert stored is not None


def test_submit_block_answers_a_reason_for_a_header_that_never_gets_indexed(
    regtest_node: Callable[..., Node],
) -> None:
    """A header failing its own range/PoW check is answered, not thrown.

    `block_index.add_headers` raises for exactly this, the same
    `BTClibValueError` `p2p.callbacks.block` lets propagate on the wire
    path -- caught here instead, since `submitblock` has no peer to
    punish for it, only a reason to answer. Never indexed at all: a
    `get_block_info` on this hash still raises `KeyError` afterwards.
    """
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    broken = a_block_claiming_an_easier_target_than_the_chain_allows(chain[0])

    result = submit_block(node, _CONN, [broken.serialize(check_validity=False).hex()])

    assert isinstance(result, str)
    assert result not in (None, "duplicate", "prev-blk-not-found")
    assert broken.header.hash not in node.chainstate.block_index.header_dict
    assert node.block_db.get_block(broken.header.hash) is None


def test_submit_block_answers_bad_prevblk_for_a_block_on_an_invalid_parent(
    regtest_node: Callable[..., Node],
) -> None:
    """ISS 1233: `add_headers` refuses the header, `bad-prevblk`.

    Measured against bitcoind v31.1, a block on a header `invalidateblock`
    marked: `bad-prevblk`.
    Neither the header nor the block is kept.
    """
    node = regtest_node()
    parent, child = generate_random_chain(2, node.chain.genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([parent.header])
    block_index.invalidate(parent.header.hash)

    result = submit_block(node, _CONN, [child.serialize(check_validity=False).hex()])

    assert result == "bad-prevblk"
    assert child.header.hash not in block_index.header_dict
    assert node.block_db.get_block(child.header.hash) is None


def test_submit_block_leaves_valid_a_header_its_body_does_not_match(
    regtest_node: Callable[..., Node],
) -> None:
    """ISS 1242: a body the merkle root does not match says nothing of it.

    Core's `AcceptBlock` marks a block failed unless the failure is
    `BLOCK_MUTATED`, so the honest body is still accepted afterwards.
    The header is indexed first, through a `headers` announcement this
    node has already accepted -- a mismatched body under a header this
    node has never indexed leaves the header unindexed instead (ISS
    1339), which is covered where that fix is.
    """
    node = regtest_node()
    chain = generate_random_chain(1, node.chain.genesis.hash)
    node.chainstate.block_index.add_headers([chain[0].header])
    # a differently-valued coinbase: structurally valid on its own, and
    # not the one the header's own merkle root actually commits to
    mismatched = Block(
        chain[0].header,
        [generate_coinbase(value=999, height=1)],
        check_validity=False,
    )

    result = submit_block(
        node, _CONN, [mismatched.serialize(check_validity=False).hex()]
    )

    assert isinstance(result, str)
    assert result not in (None, "duplicate", "prev-blk-not-found")
    block_info = node.chainstate.block_index.get_block_info(mismatched.header.hash)
    assert block_info.status != BlockStatus.invalid
    assert not block_info.downloaded
    assert node.block_db.get_block(mismatched.header.hash) is None
    honest = chain[0].serialize(check_validity=False).hex()
    assert submit_block(node, _CONN, [honest]) is None


def a_disconnecting_node(
    peers: dict[int, Any], pending: dict[int, Any] | None = None
) -> tuple[Any, list[int]]:
    """Build a node double whose `remove_connection` records what it drops."""
    node = a_node(peers, pending=pending)
    removed: list[int] = []
    node.p2p_manager.remove_connection = removed.append
    return node, removed


def test_disconnectnode_drops_the_peer_getpeerinfo_names_by_that_address() -> None:
    """ISS 1193: Core's `address`, matched against `getpeerinfo`'s `addr`.

    A peer whose socket is gone is passed over, and the first match is
    the one dropped.
    """
    peers = {
        1: a_peer(peer="1.2.3.4"),
        2: a_peer(gone=True),
        3: a_peer(peer="5.5.5.5"),
        4: a_peer(peer="5.5.5.5"),
    }
    node, removed = a_disconnecting_node(peers)
    (info,) = [info for info in get_peer_info(node, _CONN, []) if info["id"] == 3]
    disconnect_node(node, _CONN, [info["addr"]])
    assert removed == [3]


@pytest.mark.parametrize("address", ["", None])
def test_disconnectnode_drops_a_pending_peer_by_its_id(address: str | None) -> None:
    """ISS 1193: Core's `nodeid`, with `address` empty or null.

    A connection short of `verack` is found, as it is in Core's `m_nodes`.
    """
    node, removed = a_disconnecting_node({1: a_peer()}, {2: a_peer()})
    disconnect_node(node, _CONN, [address, 2])
    assert removed == [2]


_DISCONNECT_ONE_OF = (
    RPCErrorCode.INVALID_PARAMS,
    "Only one of address and nodeid should be provided.",
)
_DISCONNECT_NOT_FOUND = (
    RPCErrorCode.CLIENT_NODE_NOT_CONNECTED,
    "Node not found in connected nodes",
)


def _wrong_type(*entries: str) -> tuple[RPCErrorCode, str]:
    return (
        RPCErrorCode.TYPE_ERROR,
        "Wrong type passed:\n{\n" + ",\n".join(entries) + "\n}",
    )


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ([], _DISCONNECT_ONE_OF),
        ([None], _DISCONNECT_ONE_OF),
        ([None, None], _DISCONNECT_ONE_OF),
        (["1.2.3.4:8333", 1], _DISCONNECT_ONE_OF),
        ([""], _DISCONNECT_NOT_FOUND),
        (["", None], _DISCONNECT_NOT_FOUND),
        (["1.2.3.4"], _DISCONNECT_NOT_FOUND),
        (["", 99], _DISCONNECT_NOT_FOUND),
        (["", -1], _DISCONNECT_NOT_FOUND),
        (
            [1],
            _wrong_type(
                '    "Position 1 (address)": "JSON value of type number is not'
                ' of expected type string"'
            ),
        ),
        (
            ["", True],
            _wrong_type(
                '    "Position 2 (nodeid)": "JSON value of type bool is not of'
                ' expected type number"'
            ),
        ),
        (
            [1, "x"],
            _wrong_type(
                '    "Position 1 (address)": "JSON value of type number is not'
                ' of expected type string"',
                '    "Position 2 (nodeid)": "JSON value of type string is not'
                ' of expected type number"',
            ),
        ),
        (["", 1.5], (RPCErrorCode.MISC_ERROR, "JSON integer out of range")),
        (["", 2**63], (RPCErrorCode.MISC_ERROR, "JSON integer out of range")),
        (["", -(2**63) - 1], (RPCErrorCode.MISC_ERROR, "JSON integer out of range")),
    ],
)
def test_disconnectnode_answers_what_bitcoind_answers(
    params: list[Any], expected: tuple[RPCErrorCode, str]
) -> None:
    """ISS 1193: each code and message as a regtest bitcoind v31.1.0 gave it.

    Nothing is dropped. The integer bounds are `int64_t`'s, one past
    either end.
    """
    node, removed = a_disconnecting_node({1: a_peer()})
    with pytest.raises(RpcError) as refused:
        disconnect_node(node, _CONN, params)
    assert (refused.value.code, refused.value.message) == expected
    assert removed == []


def test_disconnectnode_no_longer_checks_its_own_upper_bound() -> None:
    """`rpc.main._execute` refuses a call over the count now, generically.

    ISS 1424 moved the check every other callback lacked into `_execute`,
    for every method `arg_names` declares -- `disconnect_node` dropped
    the one it used to be the only callback here to carry, so calling it
    directly, bypassing that layer, no longer refuses a third positional
    argument on its own: the third position here is read by nothing,
    and the call runs to completion on its first two, dropping the peer
    `nodeid` names -- proving the callback's own body runs rather than
    refusing on a count it no longer checks. `main_test.py`'s own
    coverage of `_execute` is where that refusal is tested now.
    """
    node, removed = a_disconnecting_node({1: a_peer()})
    disconnect_node(node, _CONN, ["", 1, None])
    assert removed == [1]


def a_block_marked_invalid(node: Node, block: Block) -> str:
    """Index `block`'s header, mark it invalid, and return the block's hex."""
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header])
    block_index.invalidate(block.header.hash)
    return block.serialize(check_validity=False).hex()


@pytest.mark.parametrize(
    ("body", "answer"),
    [
        ("valid", "duplicate-invalid"),
        ("over-the-weight", "duplicate-invalid"),
        ("bad-cb-multiple", "more than one coinbase"),
    ],
)
def test_submit_block_answers_duplicate_invalid_where_check_block_passes(
    regtest_node: Callable[..., Node], body: str, answer: str
) -> None:
    """ISS 1344: Core's `CheckBlock`, then `AcceptBlockHeader`'s cached state.

    Measured against bitcoind v31.1, each body under a header
    `invalidateblock` marked: `duplicate-invalid`, `duplicate-invalid`,
    `bad-cb-multiple`. Nothing is stored.
    """
    node = regtest_node()
    block = {
        "valid": lambda: generate_random_chain(1, RegTest().genesis.hash)[0],
        "over-the-weight": lambda: generate_segwit_block(
            witness=bytes(MAX_BLOCK_WEIGHT)
        ),
        "bad-cb-multiple": lambda: generate_segwit_block(generate_coinbase(height=1)),
    }[body]()
    assert submit_block(node, _CONN, [a_block_marked_invalid(node, block)]) == answer
    assert node.block_db.get_block(block.header.hash) is None


def test_submit_block_answers_duplicate_invalid_for_a_stored_block_marked_since(
    regtest_node: Callable[..., Node],
) -> None:
    """ISS 1344: not `duplicate`, Core's header state being asked first."""
    node = regtest_node()
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    raw = block.serialize(check_validity=False).hex()
    assert submit_block(node, _CONN, [raw]) is None
    node.chainstate.block_index.invalidate(block.header.hash)
    assert submit_block(node, _CONN, [raw]) == "duplicate-invalid"


def test_submit_block_leaves_valid_a_committed_body_failing_check_block(
    regtest_node: Callable[..., Node],
) -> None:
    """ISS 1242: Core's `ProcessNewBlock` never marks a `CheckBlock` failure.

    Measured against bitcoind v31.1: `bad-cb-multiple`, and the header
    absent from `getchaintips` -- `headers-only` there where
    `submitheader` indexed it first. This node's own header is absent
    from `block_index` too, its own equivalent read (ISS 1339).
    """
    node = regtest_node()
    twice = generate_segwit_block(generate_coinbase(height=1))

    result = submit_block(node, _CONN, [twice.serialize(check_validity=False).hex()])

    assert result == "more than one coinbase"
    assert twice.header.hash not in node.chainstate.block_index.header_dict


def test_submit_block_answers_check_block_failure_over_prev_blk_not_found(
    regtest_node: Callable[..., Node],
) -> None:
    """ISS 1339: `CheckBlock` runs before the header is even looked up.

    Core's `ProcessNewBlock` asks `CheckBlock` unconditionally, before
    `AcceptBlock` ever reaches `AcceptBlockHeader`
    (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), so a body failing it is refused for that reason even where
    the header's own parent is unknown too -- `prev-blk-not-found` is
    never reached, and nothing about the unknown parent is looked at.
    """
    node = regtest_node()
    orphan = build_block(
        b"\x22" * 32, [generate_coinbase(height=1), generate_coinbase(height=1)], 0
    )

    result = submit_block(node, _CONN, [orphan.serialize(check_validity=False).hex()])

    assert result == "more than one coinbase"
    assert orphan.header.hash not in node.chainstate.block_index.header_dict


@pytest.mark.parametrize(
    ("nonfinal_spend", "phrase"),
    [(False, "bad-cb-height"), (True, "bad-txns-nonfinal")],
)
def test_submit_block_answers_a_contextual_failure_for_a_block_extending_the_tip(
    regtest_node: Callable[..., Node],
    nonfinal_spend: bool,  # noqa: FBT001
    phrase: str,
) -> None:
    """ISS 1335: Core's own reasons, where btclib-node used to answer `None`.

    Measured against bitcoind v31.1: `bad-cb-height` for a coinbase
    committing to no height, and `bad-txns-nonfinal` where that same
    coinbase sits beside a non-final transaction too -- Core checks
    finality before the coinbase height commitment
    (`ContextualCheckBlock`, `src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    node = regtest_node()
    chain = generate_random_chain(COINBASE_MATURITY, node.chain.genesis.hash)
    connect(node, chain)

    # no height commitment at all: BIP34 refuses it regardless of the
    # non-final spend the parametrized case adds beside it
    transactions = [generate_coinbase()]
    if nonfinal_spend:
        funding = chain[0].transactions[0]
        transactions.append(
            locked_spend(
                funding, funding.vout[0].value, lock_time=2_000_000_000, sequence=0
            )
        )
    bad = build_block(
        node.chainstate.block_index.active_chain[-1], transactions, len(chain)
    )

    result = submit_block(node, _CONN, [bad.serialize(check_validity=False).hex()])

    assert result == phrase
    assert bad.header.hash not in node.chainstate.block_index.active_chain
    block_info = node.chainstate.block_index.get_block_info(bad.header.hash)
    assert block_info.status == BlockStatus.invalid
    assert node.block_db.get_block(bad.header.hash) is not None


def test_submit_block_answers_a_script_failure_for_a_block_extending_the_tip(
    regtest_node: Callable[..., Node],
) -> None:
    """ISS 1390: Core's `ConnectBlock`, run synchronously where the tip moves.

    Measured against bitcoind v31.1: `block-script-verify-flag-failed
    (<reason>)`. This tree does not reproduce Core's own vocabulary for
    the text inside the parentheses -- `submit_block`'s own docstring
    argues the same divergence for every other reject reason it has no
    literal word for.
    """
    node = regtest_node()
    chain = generate_random_chain(COINBASE_MATURITY, node.chain.genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    unspendable = spend(
        funding,
        funding.vout[0].value,
        script_sig=script.serialize(["OP_RETURN"]),
    )
    bad = build_block(
        node.chainstate.block_index.active_chain[-1],
        [generate_coinbase(height=len(chain) + 1), unspendable],
        len(chain),
    )

    result = submit_block(node, _CONN, [bad.serialize(check_validity=False).hex()])

    assert isinstance(result, str)
    assert result.startswith("block-script-verify-flag-failed")
    assert "OP_RETURN" in result
    assert bad.header.hash not in node.chainstate.block_index.active_chain
    block_info = node.chainstate.block_index.get_block_info(bad.header.hash)
    assert block_info.status == BlockStatus.invalid
    assert node.block_db.get_block(bad.header.hash) is not None


def test_submit_block_stops_the_node_where_update_chain_finds_storage_unsafe(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure `update_chain` does not swallow propagates and stops `Node`.

    `ChainstateInconsistencyError` is not one of `_CONTENT_FAILURE`'s
    three types, so `main.update_chain` re-raises it rather than
    recording a rejection -- `Node._step_chain`'s own scheduled call
    catches that and stops `Node.run`'s loop for it; calling
    `update_chain` here instead, ahead of that pass, must not let the
    loop run on regardless.
    """
    node = regtest_node()
    chain = generate_random_chain(2, node.chain.genesis.hash)
    connect(node, chain[:1])
    new_block = chain[1]

    def boom(*_args: object, **_kwargs: object) -> None:
        err_msg = "boom"
        raise ChainstateInconsistencyError(err_msg)

    monkeypatch.setattr(node.chainstate.utxo_index, "add_block", boom)

    assert not node.terminate_flag.is_set()
    with pytest.raises(ChainstateInconsistencyError, match="boom"):
        submit_block(node, _CONN, [new_block.serialize(check_validity=False).hex()])
    assert node.terminate_flag.is_set()


@pytest.mark.parametrize(("segwit_height", "invalid"), [(1, True), (2, False)])
def test_submit_block_invalidates_a_committed_body_over_the_weight(
    regtest_node: Callable[..., Node],
    monkeypatch: pytest.MonkeyPatch,
    segwit_height: int,
    invalid: bool,  # noqa: FBT001
) -> None:
    """ISS 1242: `bad-blk-weight` marks the block, its witness committed to.

    Where segwit binds only after the block's own height, its witness makes
    the body mutated and the header is left alone.
    """
    node = regtest_node()
    consensus = replace(node.chain.consensus, segwit_height=segwit_height)
    monkeypatch.setattr(
        node,
        "chain",
        SimpleNamespace(pow_limit_bits=node.chain.pow_limit_bits, consensus=consensus),
    )
    over = generate_segwit_block(witness=bytes(MAX_BLOCK_WEIGHT))

    result = submit_block(node, _CONN, [over.serialize(check_validity=False).hex()])

    assert isinstance(result, str)
    assert result.startswith("invalid weight")
    block_info = node.chainstate.block_index.get_block_info(over.header.hash)
    assert (block_info.status == BlockStatus.invalid) is invalid


# A regtest header at height 1 of version -1, as a bitcoind v31.1.0 run with
# `-testactivationheight=bip34@100` (and `dersig`, `cltv` at 100) took it
# through `submitblock` and answered it back through `getblockheader false`.
_A_VERSION_MINUS_ONE_HEADER = (
    "ffffffff06226e46111a0b59caaf126043eb5bbf28c34f3a5e332a1fc7b2b73cf188910f"
    "a7c0dbac4920cf8d62f0cb6d2efaa0105c5d6bbd3552da2c805dc60856589631dfefb76a"
    "ffff7f2001000000"
)


def test_a_stored_version_minus_one_header_is_answered_as_bitcoind_answers() -> None:
    """ISS 1262: the index stores it below BIP34's height, as Core does.

    bitcoind answered the raw header with these same octets, `version`
    -1 and `versionHex` "ffffffff", Core's `%08x` of its `int32_t`.
    """
    header = BlockHeader.parse(
        bytes.fromhex(_A_VERSION_MINUS_ONE_HEADER), check_validity=False
    )
    node = cast(
        "Node",
        SimpleNamespace(
            chainstate=SimpleNamespace(block_index=a_block_index([header]))
        ),
    )
    raw = get_block_header(node, _CONN, [header.hash.hex(), False])
    assert raw == _A_VERSION_MINUS_ONE_HEADER
    verbose = get_block_header(node, _CONN, [header.hash.hex()])
    assert isinstance(verbose, dict)
    assert (verbose["version"], verbose["versionHex"]) == (-1, "ffffffff")


def test_a_feeler_has_no_tx_relay() -> None:
    """ISS 1096: Core builds no `TxRelay` for a feeler either."""
    peer = a_peer(inbound=False, automatic=True, feeler=True, relay=True)
    (info,) = get_peer_info(a_node({7: peer}), _CONN, [])
    assert info["relaytxes"] is False


def test_help_rpc_answers_through_answer_help() -> None:
    """ISS 1405: `callbacks["help"]` is `help_rpc`, and defers to `rpc.help`.

    `help_test.py` covers `answer_help` itself, never through the
    dispatch table; this is the one place `help_rpc`'s own wrapper --
    node-free, unlike every other entry in `callbacks` -- is ever
    called at all, so what it checks is the delegation rather than
    `answer_help`'s own content.
    """
    assert callbacks["help"] is help_rpc
    node = cast("Node", SimpleNamespace())
    for params in ([], ["stop"]):
        assert help_rpc(node, _CONN, params) == answer_help(params)
