# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What this node answers a peer with, message by message.

`main.handle_p2p` turns a callback that raises into a disconnect, and
`handle_p2p_handshake` does the same, so what a callback does with a
message it dislikes is the difference between refusing the message and
losing the peer. The functional tests drive two cooperating nodes, which
is the path where every message is welcome; these are the rest.
"""

import socket
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast

import pytest
from btclib.amount import sats_from_btc
from btclib.block import Block, BlockHeader
from btclib.exceptions import BTClibValueError
from btclib.fee import FeeRate
from btclib.hashes import hash256
from btclib.p2p.address import Addr, NetworkAddress, ServiceFlags
from btclib.p2p.addrv2 import (
    AddrV2,
    BIP155Network,
    NetworkAddressV2,
    SendAddrV2,
)
from btclib.p2p.block_filters import (
    BlockFilterType,
    CFCheckpt,
    CFHeaders,
    CFilter,
    GetCFCheckpt,
    GetCFHeaders,
    GetCFilters,
)
from btclib.p2p.compact_blocks import SendCmpct
from btclib.p2p.data import BlockPayload as BlockMsg
from btclib.p2p.data import TxPayload as TxMsg
from btclib.p2p.handshake import Verack, Version
from btclib.p2p.inventory import (
    GetData,
    GetHeaders,
    Headers,
    Inv,
    Inventory,
    InventoryType,
    NotFound,
)
from btclib.p2p.keepalive import Ping, Pong
from btclib.p2p.limits import (
    CFCHECKPT_INTERVAL,
    MAX_GETCFHEADERS_SIZE,
    MAX_GETCFILTERS_SIZE,
)
from btclib.p2p.negotiation import FeeFilter, GetAddr, SendHeaders, WtxidRelay
from btclib.script.witness import Witness
from btclib.tx.tx import Tx

from btclib_node.chains import RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.block_index import BlockStatus
from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE
from btclib_node.constants import NodeStatus, P2pConnStatus, ProtocolVersion
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from btclib_node.p2p.address import PeerDB, addr_entry, peer_address
from btclib_node.p2p.callbacks import (
    addr,
    addrv2,
    feefilter,
    get_cfcheckpt,
    get_cfheaders,
    get_cfilters,
    getaddr,
    getdata,
    getheaders,
    headers,
    inv,
    not_found,
    ping,
    pong,
    reject,
    sendaddrv2,
    sendheaders,
    tx,
    verack,
    version,
    wtxidrelay,
)
from btclib_node.p2p.callbacks import block as block_callback
from btclib_node.p2p.connection import Connection
from btclib_node.p2p.manager import P2pManager
from btclib_node.p2p.messages.errors import Reject, RejectCode
from tests.helpers import (
    generate_random_chain,
    generate_random_header_chain,
    generate_random_transaction,
)

if TYPE_CHECKING:
    from btclib_node.chains import Chain

# BIP155's table, for the networks these tests build an address of
_ADDRESS_SIZE = {
    BIP155Network.IPV4: 4,
    BIP155Network.IPV6: 16,
    BIP155Network.TORV3: 32,
}


def an_address(
    n: int = 0, network_id: BIP155Network = BIP155Network.IPV4
) -> NetworkAddressV2:
    # seen just now: an address the node would not serve is a different
    # test, in tests/unit/p2p/address.py
    return NetworkAddressV2(
        timestamp=int(time.time()),
        services=0,
        network_id=network_id,
        address=n.to_bytes(_ADDRESS_SIZE[network_id], "big"),
        port=18444,
    )


def a_version_address(services: int = 0) -> NetworkAddress:
    # unroutable, and a `version` message's address, which carries no
    # timestamp: the narrowest of btclib's address types
    return NetworkAddress(services, "0.0.0.0", 18444)  # noqa: S104


def make_node(
    addresses: Sequence[NetworkAddressV2], *, prefer_addressv2: bool = False
) -> tuple[Any, Any, list[Any]]:
    peer_db = PeerDB(cast("Chain", None), cast(Path, None))
    for address in addresses:
        peer_db.active_addresses.append(address)
    sent: list[Any] = []
    conn = SimpleNamespace(prefer_addressv2=prefer_addressv2, send=sent.append)
    node = SimpleNamespace(p2p_manager=SimpleNamespace(peer_db=peer_db))
    return node, conn, sent


def test_an_ipv4_address_is_answered_in_an_addr() -> None:
    address = an_address()
    node, conn, sent = make_node([address])
    getaddr(node, b"", conn)
    (answer,) = sent
    assert isinstance(answer, Addr)
    assert answer.addresses == (addr_entry(address),)
    # and it survives the wire, which is what the network filter is for
    assert Addr.parse(answer.serialize()).addresses == answer.addresses


def test_a_peer_that_asked_for_addrv2_gets_addrv2() -> None:
    address = an_address()
    node, conn, sent = make_node([address], prefer_addressv2=True)
    getaddr(node, b"", conn)
    (answer,) = sent
    assert isinstance(answer, AddrV2)
    assert answer.addresses == (address,)


def test_an_address_addr_version_1_cannot_carry_is_left_out() -> None:
    # an onion address has no addr version 1 entry to be built into, so
    # one of them among the active addresses would cost the whole answer
    onion = an_address(network_id=BIP155Network.TORV3)
    ipv4 = an_address()
    ipv6 = an_address(network_id=BIP155Network.IPV6)
    node, conn, sent = make_node([onion, ipv4, ipv6])
    getaddr(node, b"", conn)
    (answer,) = sent
    # ipv6 is carried by addr version 1, and only the network id says so
    assert answer.addresses == (addr_entry(ipv4), addr_entry(ipv6))
    answer.serialize()


def test_the_same_address_reaches_a_peer_that_can_take_it() -> None:
    onion = an_address(network_id=BIP155Network.TORV3)
    node, conn, sent = make_node([onion], prefer_addressv2=True)
    getaddr(node, b"", conn)
    (answer,) = sent
    assert answer.addresses == (onion,)


def test_nothing_active_is_answered_with_nothing() -> None:
    node, conn, sent = make_node([])
    getaddr(node, b"", conn)
    assert not sent


def test_more_addresses_than_fit_one_message_are_split() -> None:
    addresses = [an_address(n) for n in range(2001)]
    node, conn, sent = make_node(addresses)
    getaddr(node, b"", conn)
    assert [len(answer.addresses) for answer in sent] == [1000, 1000, 1]
    served = [address for answer in sent for address in answer.addresses]
    assert served == [addr_entry(address) for address in addresses]


def a_version(
    *,
    protocol: int = ProtocolVersion,
    services: ServiceFlags = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS,
    nonce: int = 7,
    relay: bool | None = True,
) -> bytes:
    return Version(
        version=protocol,
        services=services,
        timestamp=1,
        addr_recv=a_version_address(),
        addr_from=a_version_address(services),
        nonce=nonce,
        user_agent=b"/Btclib/",
        start_height=0,
        relay=relay,
    ).serialize()


def a_peer(**attributes: Any) -> Any:
    sent: list[Any] = []
    stopped = []
    peer = SimpleNamespace(
        id=0,
        send=sent.append,
        sent=sent,
        stop=lambda: stopped.append(True),
        stopped=stopped,
        status=P2pConnStatus.Open,
        version_message=None,
        wtxidrelay_received=False,
        prefer_addressv2=False,
        prefers_headers=False,
        # what Connection sets, and what the version callback overwrites
        relay_tx=True,
        download_queue=[],
        pending_eviction=False,
        last_block_timestamp=0,
        ping_sent=0,
        ping_nonce=0,
        latency=0,
        send_ping=lambda: sent.append("ping"),
        client=SimpleNamespace(getpeername=lambda: ("1.2.3.4", 18444)),
    )
    peer.__dict__.update(attributes)
    return peer


def a_handshake_node(
    *,
    nonces: Sequence[int] = (),
    status: NodeStatus = NodeStatus.HeaderSynced,
    peer_db: Any = None,
    promote_connection: Any = None,
    min_relay_feerate: FeeRate = DEFAULT_MIN_RELAY_FEERATE,
) -> Any:
    return SimpleNamespace(
        status=status,
        config=SimpleNamespace(min_relay_feerate=min_relay_feerate),
        p2p_manager=SimpleNamespace(
            nonces=list(nonces),
            peer_db=peer_db,
            promote_connection=promote_connection or (lambda conn_id: None),
        ),
        chainstate=SimpleNamespace(
            block_index=SimpleNamespace(get_block_locator_hashes=lambda: [b"\x00" * 32])
        ),
        logger=SimpleNamespace(
            info=lambda *a: None, warning=lambda *a: None, debug=lambda *a: None
        ),
    )


def commands(peer: Any) -> list[str]:
    return [
        message if isinstance(message, str) else type(message).__name__
        for message in peer.sent
    ]


def test_a_version_is_answered_with_what_this_node_speaks() -> None:
    peer = a_peer()
    version(a_handshake_node(), a_version(), peer)
    assert commands(peer) == ["WtxidRelay", "SendAddrV2", "Verack"]
    assert isinstance(peer.sent[0], WtxidRelay)
    assert isinstance(peer.sent[1], SendAddrV2)
    assert isinstance(peer.sent[2], Verack)
    assert peer.relay_tx is True
    assert not peer.stopped


def test_a_version_carrying_our_own_nonce_is_this_node_calling_itself() -> None:
    peer = a_peer()
    version(a_handshake_node(nonces=[7]), a_version(nonce=7), peer)
    assert peer.stopped == [True]
    assert not peer.sent


def test_a_peer_speaking_an_older_protocol_is_let_go() -> None:
    peer = a_peer()
    version(a_handshake_node(), a_version(protocol=ProtocolVersion - 1), peer)
    assert peer.stopped == [True]


def test_a_peer_without_the_witness_service_is_let_go() -> None:
    peer = a_peer()
    version(a_handshake_node(), a_version(services=ServiceFlags.NODE_NETWORK), peer)
    assert peer.stopped == [True]


def test_a_pruned_peer_is_let_go_only_once_the_blocks_are_synced() -> None:
    pruned = ServiceFlags.NODE_WITNESS
    peer = a_peer()
    version(
        a_handshake_node(status=NodeStatus.HeaderSynced),
        a_version(services=pruned),
        peer,
    )
    assert not peer.stopped

    peer = a_peer()
    version(
        a_handshake_node(status=NodeStatus.BlockSynced),
        a_version(services=pruned),
        peer,
    )
    assert peer.stopped == [True]


def test_a_version_that_says_it_relays_nothing_is_taken_at_its_word() -> None:
    peer = a_peer()
    version(a_handshake_node(), a_version(relay=False), peer)
    assert peer.relay_tx is False
    # the flag went to the attribute Connection defines, and nowhere
    # else: the near miss that dropped it was one letter long
    assert not hasattr(peer, "relay_txs")


def test_a_version_without_the_relay_flag_is_a_peer_asking_for_relay() -> None:
    # BIP37's default, which a peer older than the flag relies on: read
    # as a false, it would be recorded as asking for the opposite
    peer = a_peer(relay_tx=False)
    version(a_handshake_node(), a_version(relay=None), peer)
    assert peer.relay_tx is True


def test_a_version_with_a_trailing_octet_still_costs_the_peer() -> None:
    # issue #149 leaves this one asymmetric on purpose: addr and addrv2
    # (below) accept a BinaryData stream, and btclib's own
    # assert_no_trailing treats one as "the caller's", nothing past it
    # checked -- Core's own leniency, reached with no second parser.
    # Version.parse takes Octets alone (its own docstring: "the envelope
    # is what says where a payload ends"), because its optional relay
    # byte is detected by whether one more byte is there at all; handing
    # it a stream that could plausibly hold more would make a genuinely
    # unknown trailing octet misread as that flag. There is no btclib
    # mechanism this node can lean on for `version` without a private
    # copy of its field-by-field parse, so this still raises out of the
    # callback -- main.handle_p2p_handshake is what turns that into
    # conn.stop(), covered by tests/unit/p2p/main.py's own coverage of
    # that generic behaviour.
    peer = a_peer()
    with pytest.raises(BTClibValueError):
        version(a_handshake_node(), a_version() + b"\x00", peer)


def test_a_relay_octet_that_is_neither_0_nor_1_still_costs_the_peer() -> None:
    # issue #149's second half, closed on this: Core's own
    # Unserialize<bool> (serialize.h) reads any nonzero octet as true,
    # where Version.parse raises for anything but 0x00/0x01. Reaching
    # Core's leniency here would mean either replaying Version.parse's
    # whole field walk (the fixed fields, both NetworkAddress entries and
    # the var_bytes user agent) to find where the flag sits in the
    # payload, or matching the wording of the BTClibValueError it raises
    # -- both bind this node to btclib's private shape rather than its
    # public contract, unlike the stream-based leniency addr/addrv2 use
    # above. And Core's own encoder (Serialize<bool>, the same
    # serialize.h) can never write anything but 0x00/0x01: a bool
    # converts to 0 or 1, nothing else, so no peer running Core -- or
    # this node's own Version.serialize -- ever reaches this path, only
    # an adversarial or already-broken one does. Disconnecting it is the
    # policy kept.
    peer = a_peer()
    # relay=None serializes nothing, so appending one raw octet is the
    # only relay byte this payload carries
    with pytest.raises(BTClibValueError):
        version(a_handshake_node(), a_version(relay=None) + b"\x02", peer)


def a_real_connection() -> Connection:
    # not a stand-in: the defect this is about was a callback writing an
    # attribute no Connection has, which a SimpleNamespace peer takes
    # without a word and a Connection takes just as quietly. What a real
    # one adds is that the attribute asserted on below is the one the
    # rest of the node reads.
    manager = SimpleNamespace(node=a_handshake_node(), loop=None, peer_db=None)
    unroutable = peer_address("0.0.0.0", 18444)  # noqa: S104
    connection = Connection(
        cast("P2pManager", manager), socket.socket(), unroutable, 0, False
    )
    # Connection.send hands the message to an event loop this test does
    # not run; what it would send is tested above
    connection.send = lambda msg: None  # type: ignore[method-assign]
    return connection


@pytest.mark.parametrize(
    ("relay", "wanted"),
    [(True, True), (False, False), (None, True)],
    ids=["true", "false", "absent"],
)
def test_what_a_peer_said_about_relay_lands_on_the_connection(
    relay: bool | None, wanted: bool
) -> None:
    connection = a_real_connection()
    with connection.client:
        assert connection.relay_tx is True  # BIP37's default until told
        version(a_handshake_node(), a_version(relay=relay), connection)
        assert connection.relay_tx is wanted


def test_a_verack_completes_the_handshake() -> None:
    promoted: list[int] = []
    peer = a_peer(id=9, version_message=object(), wtxidrelay_received=True)
    verack(a_handshake_node(promote_connection=promoted.append), b"", peer)
    assert peer.status == P2pConnStatus.Connected
    assert commands(peer) == [
        "SendHeaders",
        "SendCmpct",
        "FeeFilter",
        "ping",
        "GetAddr",
        "GetHeaders",
    ]
    assert isinstance(peer.sent[0], SendHeaders)
    assert isinstance(peer.sent[1], SendCmpct)
    assert isinstance(peer.sent[2], FeeFilter)
    assert isinstance(peer.sent[4], GetAddr)
    assert isinstance(peer.sent[5], GetHeaders)
    assert not peer.stopped
    # out of P2pManager.pending_connections and into connections, right
    # where P2pConnStatus.Connected is set: btclib-org/btclib-node#131
    assert promoted == [9]


def test_a_verack_tells_the_peer_this_nodes_own_relay_floor() -> None:
    # issue #94: the value is Config.min_relay_feerate, not a constant
    # of this module's own
    peer = a_peer(version_message=object(), wtxidrelay_received=True)
    node = a_handshake_node(min_relay_feerate=FeeRate(sats_per_kvbyte=500))
    verack(node, b"", peer)
    (feefilter_msg,) = [m for m in peer.sent if isinstance(m, FeeFilter)]
    assert feefilter_msg.feerate == 500
    # and it survives the wire like every other payload this node sends
    assert FeeFilter.parse(feefilter_msg.serialize()).feerate == 500


@pytest.mark.parametrize(
    ("host", "endpoint"),
    [
        ("1.2.3.4", "1.2.3.4:18444"),
        ("::ffff:1.2.3.4", "1.2.3.4:18444"),
        ("2001:db8::1", "[2001:db8::1]:18444"),
    ],
    ids=["ipv4", "v4-mapped", "ipv6"],
)
def test_the_handshake_logs_the_endpoint_getpeerinfo_answers_with(
    host: str, endpoint: str
) -> None:
    logged: list[str] = []
    node = a_handshake_node()
    node.logger.info = logged.append
    peer = a_peer(
        version_message=object(),
        wtxidrelay_received=True,
        client=SimpleNamespace(getpeername=lambda: (host, 18444)),
    )
    verack(node, b"", peer)
    assert logged == [f"Connected to {endpoint}"]


def test_the_handshake_asks_the_socket_for_the_peer_once() -> None:
    # A second lookup is a second chance for the peer to have gone,
    # raising OSError where the first answered.
    sockaddr = ("1.2.3.4", 18444)
    lookups: list[tuple[str, int]] = []

    def getpeername() -> tuple[str, int]:
        lookups.append(sockaddr)
        return sockaddr

    peer = a_peer(
        version_message=object(),
        wtxidrelay_received=True,
        client=SimpleNamespace(getpeername=getpeername),
    )
    verack(a_handshake_node(), b"", peer)
    assert lookups == [sockaddr]


def test_a_verack_before_the_version_is_let_go() -> None:
    promoted: list[int] = []
    peer = a_peer(wtxidrelay_received=True)
    verack(a_handshake_node(promote_connection=promoted.append), b"", peer)
    assert peer.stopped == [True]
    assert peer.status == P2pConnStatus.Open
    assert promoted == []


def test_a_verack_from_a_peer_that_never_asked_for_wtxid_relay_is_let_go() -> None:
    promoted: list[int] = []
    peer = a_peer(version_message=object())
    verack(a_handshake_node(promote_connection=promoted.append), b"", peer)
    assert peer.stopped == [True]
    assert promoted == []


def test_the_flags_a_peer_sets_on_this_connection() -> None:
    peer = a_peer()
    wtxidrelay(a_handshake_node(), b"", peer)
    sendaddrv2(a_handshake_node(), b"", peer)
    sendheaders(a_handshake_node(), b"", peer)
    assert peer.wtxidrelay_received
    assert peer.prefer_addressv2
    assert peer.prefers_headers


def test_a_feefilter_lands_on_the_connection() -> None:
    peer = a_peer()
    feefilter(a_handshake_node(), FeeFilter(500).serialize(), peer)
    assert peer.feefilter == 500


@pytest.mark.parametrize(
    "feerate",
    [-500, sats_from_btc(Decimal(21_000_000)) + 1],
    ids=["negative", "above-max-money"],
)
def test_a_feefilter_outside_the_money_range_is_read_as_no_filter(
    feerate: int,
) -> None:
    # Core acts on a received rate only within MoneyRange -- 0 to
    # MAX_MONEY inclusive (net_processing.cpp's NetMsgType::FEEFILTER,
    # consensus/amount.h's MoneyRange) -- and leaves either side of it
    # parsed but unused, rather than turning it into a filter nothing
    # a real, non-negative fee rate could ever fail
    peer = a_peer()
    feefilter(a_handshake_node(), FeeFilter(feerate).serialize(), peer)
    assert peer.feefilter == 0


def test_a_feefilter_at_the_edge_of_the_money_range_is_kept() -> None:
    # the bound is inclusive, so exactly MAX_MONEY is still a filter
    peer = a_peer()
    at_the_edge = sats_from_btc(Decimal(21_000_000))
    feefilter(a_handshake_node(), FeeFilter(at_the_edge).serialize(), peer)
    assert peer.feefilter == at_the_edge


def test_a_ping_is_answered_with_the_nonce_it_carried() -> None:
    peer = a_peer()
    ping(a_handshake_node(), Ping(1234).serialize(), peer)
    (answer,) = peer.sent
    assert isinstance(answer, Pong)
    assert answer.nonce == 1234


def test_a_pong_answering_our_ping_is_a_latency_measurement() -> None:
    peer = a_peer(ping_sent=time.time() - 0.5, ping_nonce=1234)
    pong(a_handshake_node(), Pong(1234).serialize(), peer)
    assert peer.latency > 0
    assert peer.ping_sent == 0
    assert peer.ping_nonce == 0
    assert not peer.stopped


def test_a_pong_with_the_wrong_nonce_is_a_peer_not_speaking_the_protocol() -> None:
    peer = a_peer(ping_sent=time.time(), ping_nonce=1234)
    pong(a_handshake_node(), Pong(4321).serialize(), peer)
    assert peer.stopped == [True]


def test_a_pong_nobody_pinged_for_is_ignored() -> None:
    peer = a_peer()
    pong(a_handshake_node(), Pong(1234).serialize(), peer)
    assert not peer.stopped
    assert peer.latency == 0


def test_the_addresses_a_peer_sends_are_kept() -> None:
    given = [an_address(1), an_address(2)]
    for callback, message in (
        (addr, Addr([addr_entry(address) for address in given])),
        (addrv2, AddrV2(given)),
    ):
        peer_db = PeerDB(cast("Chain", None), cast(Path, None))
        node = a_handshake_node(peer_db=peer_db)
        callback(node, message.serialize(), a_peer())
        # BIP155's record either way, the addr version 1 entry being
        # translated back into one; and without the timestamp the peer
        # quoted, which is PeerDB.add_addresses' doing
        assert peer_db.addresses == {replace(address, timestamp=0) for address in given}


def test_an_octet_past_an_addr_or_addrv2_no_longer_costs_the_peer() -> None:
    # issue #149: btclib's own assert_no_trailing raises out of
    # Addr.parse/AddrV2.parse for exactly this, which main.handle_p2p
    # turns into a disconnect if the callback lets it through. Core does
    # not disconnect here (net_processing.cpp's ProcessMessage reads what
    # it wants out of vRecv and never checks for anything left), and this
    # node now matches that for the two of the three messages issue #149
    # is about where it can without a second copy of btclib's codec --
    # Addr and AddrV2 accept a stream, and btclib's own assert_no_trailing
    # docstring calls a stream "the caller's", nothing past it checked.
    given = [an_address(1)]
    for callback, message in (
        (addr, Addr([addr_entry(address) for address in given])),
        (addrv2, AddrV2(given)),
    ):
        peer_db = PeerDB(cast("Chain", None), cast(Path, None))
        node = a_handshake_node(peer_db=peer_db)
        peer = a_peer()
        callback(node, message.serialize() + b"\x00", peer)
        assert peer_db.addresses == {replace(address, timestamp=0) for address in given}
        assert not peer.stopped


def test_an_address_of_a_network_nobody_here_has_heard_of_is_kept() -> None:
    # what this used to cost: the network id was an enumeration over the
    # ids BIP155 had assigned, so a yggdrasil peer raised out of the
    # parser and p2p.main turned that into a disconnect. The whole point
    # of the format is that a new network needs no new message.
    yggdrasil = NetworkAddressV2(0, 0, 7, b"\x02" + b"\x22" * 15, 18444)
    unassigned = NetworkAddressV2(0, 0, 250, b"\x33" * 8, 18444)
    peer_db = PeerDB(cast("Chain", None), cast(Path, None))
    node = a_handshake_node(peer_db=peer_db)
    peer = a_peer()
    addrv2(node, AddrV2([yggdrasil, unassigned]).serialize(), peer)
    assert peer_db.addresses == {yggdrasil, unassigned}
    assert not peer.stopped
    # and neither is dialled or gossiped on, there being no address of
    # either kind this node knows what to do with
    assert peer_db.random_address() is None


def test_a_notfound_is_logged_rather_than_held_against_the_peer() -> None:
    logged: list[str] = []
    node = a_handshake_node()
    node.logger.warning = logged.append
    peer = a_peer()
    not_found(
        node,
        NotFound([Inventory(InventoryType.MSG_TX, b"\x11" * 32)]).serialize(),
        peer,
    )
    assert logged
    assert not peer.stopped


def test_a_reject_names_the_transaction_it_is_about() -> None:
    logged: list[str] = []
    node = a_handshake_node()
    node.logger.warning = logged.append
    peer = a_peer()
    txid = bytes(range(32))
    message = Reject("tx", RejectCode.insufficientfee, "min relay fee not met", txid)
    reject(node, message.serialize(), peer)
    (line,) = logged
    assert "insufficientfee" in line
    assert "min relay fee not met" in line
    assert txid.hex() in line
    assert not peer.stopped


def test_a_reject_survives_the_wire() -> None:
    # the hash is what a reject is about, and a symmetric one would not
    # notice it coming back reversed
    message = Reject("tx", RejectCode.insufficientfee, "no", bytes(range(32)))
    assert Reject.parse(message.serialize()) == message


def a_transaction() -> Tx:
    # with a witness, so that a txid and a wtxid are different bytes and
    # an answer naming the wrong one cannot pass
    transaction = generate_random_transaction()
    transaction.vin[0].script_witness = Witness([b"\x11" * 32])
    return transaction


def a_data_node(
    *,
    mempool: Mempool | None = None,
    block_index: Any = None,
    block_db: Any = None,
    status: NodeStatus = NodeStatus.BlockSynced,
) -> Any:
    node = a_handshake_node(status=status)
    node.mempool = mempool if mempool is not None else Mempool(Logger(debug=True))
    node.chain = RegTest()
    node.block_db = block_db
    node.download_manager = SimpleNamespace(received_txs=[], inv_txs=[])
    if block_index is not None:
        node.chainstate.block_index = block_index
    return node


def test_a_transaction_that_verifies_is_kept_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import btclib_node.p2p.callbacks as cb

    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 0)
    transaction = a_transaction()
    node = a_data_node()
    peer = a_peer(id=3)
    tx(node, TxMsg(transaction, include_witness=True).serialize(), peer)
    assert node.mempool.contains_tx(transaction)
    assert node.download_manager.received_txs == [(3, transaction.hash)]


def test_a_transaction_whose_parents_are_missing_is_not_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import btclib_node.p2p.callbacks as cb

    def missing(node: Any, transaction: Any) -> NoReturn:
        raise MissingPrevoutError

    monkeypatch.setattr(cb, "verify_mempool_acceptance", missing)
    transaction = a_transaction()
    node = a_data_node()
    tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert not node.mempool.contains_tx(transaction)
    assert node.download_manager.received_txs == []


def test_a_transaction_received_before_the_node_is_synced_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #129: a peer's version now always asks for relay, so a
    # transaction sent while this node is still syncing is possible --
    # Core's own reason for the same drop is that the utxo set is not
    # caught up enough to check it, not that the peer misbehaved.
    # verify_mempool_acceptance is patched to accept unconditionally,
    # so the mempool staying empty is the gate firing rather than a
    # coincidental rejection
    import btclib_node.p2p.callbacks as cb

    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 0)
    transaction = a_transaction()
    node = a_data_node(status=NodeStatus.HeaderSynced)
    tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert not node.mempool.contains_tx(transaction)
    assert node.download_manager.received_txs == []


def test_a_transaction_already_held_is_not_reported_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import btclib_node.p2p.callbacks as cb

    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 0)
    transaction = a_transaction()
    node = a_data_node()
    node.mempool.add_tx(transaction)
    tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert node.download_manager.received_txs == []


class FakeBlockIndex:
    def __init__(self, infos: dict[bytes, Any]) -> None:
        self.infos = infos
        self.marked: list[bytes] = []
        self.invalidated: list[bytes] = []

    def get_block_info(self, block_hash: bytes) -> Any:
        return self.infos[block_hash]

    def set_downloaded(self, block_hash: bytes) -> None:
        self.marked.append(block_hash)

    def invalidate(self, block_hash: bytes) -> None:
        self.invalidated.append(block_hash)


def a_block() -> Block:
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    return block


def test_a_block_that_was_asked_for_is_stored_and_marked_downloaded() -> None:
    block = a_block()
    index = FakeBlockIndex({block.header.hash: SimpleNamespace(downloaded=False)})
    added: list[Block] = []
    node = a_data_node(
        block_index=index, block_db=SimpleNamespace(add_block=added.append)
    )
    peer = a_peer(
        download_queue=[block.header.hash],
        last_block_timestamp=0,
        pending_eviction=True,
    )
    block_callback(
        node,
        BlockMsg(block, include_witness=True, check_validity=False).serialize(
            check_validity=False
        ),
        peer,
    )
    assert peer.download_queue == []
    assert peer.last_block_timestamp > 0
    assert peer.pending_eviction is False
    assert added == [block]
    assert index.marked == [block.header.hash]
    assert index.invalidated == []


def test_a_block_already_stored_is_not_stored_again() -> None:
    block = a_block()
    index = FakeBlockIndex({block.header.hash: SimpleNamespace(downloaded=True)})
    added: list[Block] = []
    node = a_data_node(
        block_index=index, block_db=SimpleNamespace(add_block=added.append)
    )
    block_callback(
        node,
        BlockMsg(block, include_witness=True, check_validity=False).serialize(
            check_validity=False
        ),
        a_peer(),
    )
    assert added == []
    assert index.marked == []


def a_block_claiming_an_easier_target_than_the_chain_allows(block: Block) -> Block:
    # regtest's limit is 7fffff00..., and a target has 32 octets to fit
    # in: 800000... is the next one up that still does
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


def test_a_block_whose_proof_of_work_does_not_hold_up_is_refused() -> None:
    # the raise still reaches main.handle_p2p, which drops the peer;
    # invalidate is what keeps the next one from being asked to send the
    # same block again: btclib-org/btclib-node#77
    added: list[Block] = []
    broken = a_block_claiming_an_easier_target_than_the_chain_allows(a_block())
    index = FakeBlockIndex({broken.header.hash: SimpleNamespace(downloaded=False)})
    node = a_data_node(
        block_index=index, block_db=SimpleNamespace(add_block=added.append)
    )
    payload = BlockMsg(broken, include_witness=True, check_validity=False).serialize(
        check_validity=False
    )
    with pytest.raises(BTClibValueError):
        block_callback(node, payload, a_peer())
    assert added == []
    assert index.marked == []
    assert index.invalidated == [broken.header.hash]


def test_an_inventory_is_ignored_until_the_blocks_are_synced() -> None:
    node = a_data_node(status=NodeStatus.HeaderSynced)
    peer = a_peer()
    inv(node, Inv([Inventory(InventoryType.MSG_BLOCK, b"\x11" * 32)]).serialize(), peer)
    assert not peer.sent


def test_a_block_announced_is_answered_with_a_getheaders() -> None:
    node = a_data_node()
    peer = a_peer()
    hashes = [b"\x11" * 32, b"\x22" * 32]
    items = [Inventory(InventoryType.MSG_BLOCK, h) for h in hashes]
    inv(node, Inv(items).serialize(), peer)
    (answer,) = peer.sent
    assert isinstance(answer, GetHeaders)
    # the last one announced: the headers between are what we are after
    assert answer.hash_stop == hashes[-1]


def test_a_transaction_announced_that_we_lack_is_wanted() -> None:
    transaction = a_transaction()
    node = a_data_node()
    peer = a_peer(id=4)
    items = [Inventory(InventoryType.MSG_WTX, transaction.hash)]
    inv(node, Inv(items).serialize(), peer)
    assert node.download_manager.inv_txs == [(4, transaction.hash)]
    assert not peer.sent


def test_a_transaction_announced_that_we_hold_is_not_wanted() -> None:
    transaction = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(transaction)
    node = a_data_node(mempool=mempool)
    items = [Inventory(InventoryType.MSG_WTX, transaction.hash)]
    inv(node, Inv(items).serialize(), a_peer(id=4))
    assert node.download_manager.inv_txs == []


def test_a_transaction_this_node_holds_is_served() -> None:
    transaction = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(transaction)
    node = a_data_node(mempool=mempool)
    # which identifier the peer asked by, and whether the answer carries
    # the witness, are two different questions and the codes answer both
    for type_code, identifier, with_witness in (
        (InventoryType.MSG_TX, transaction.id, False),
        (InventoryType.MSG_WITNESS_TX, transaction.id, True),
        (InventoryType.MSG_WTX, transaction.hash, True),
    ):
        peer = a_peer()
        getdata(node, GetData([Inventory(type_code, identifier)]).serialize(), peer)
        (answer,) = peer.sent
        assert isinstance(answer, TxMsg)
        assert answer.tx == transaction
        assert answer.include_witness is with_witness


def test_a_transaction_is_not_found_under_the_other_identifier() -> None:
    transaction = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(transaction)
    node = a_data_node(mempool=mempool)
    for type_code, identifier in (
        (InventoryType.MSG_TX, transaction.hash),
        (InventoryType.MSG_WTX, transaction.id),
    ):
        peer = a_peer()
        item = Inventory(type_code, identifier)
        getdata(node, GetData([item]).serialize(), peer)
        (answer,) = peer.sent
        assert isinstance(answer, NotFound)
        assert answer.items == (item,)


def test_a_transaction_this_node_does_not_hold_gets_a_notfound() -> None:
    # Core's own answer to a `getdata` `FindTxForGetData` cannot serve:
    # `vNotFound` in `ProcessGetData`, src/net_processing.cpp
    node = a_data_node()
    peer = a_peer()
    item = Inventory(InventoryType.MSG_TX, b"\x11" * 32)
    getdata(node, GetData([item]).serialize(), peer)
    (answer,) = peer.sent
    assert isinstance(answer, NotFound)
    assert answer.items == (item,)


def test_several_misses_batch_into_one_notfound_alongside_the_hits() -> None:
    transaction = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(transaction)
    node = a_data_node(mempool=mempool)
    peer = a_peer()
    held = Inventory(InventoryType.MSG_WTX, transaction.hash)
    missing = [
        Inventory(InventoryType.MSG_TX, b"\x11" * 32),
        Inventory(InventoryType.MSG_WTX, b"\x22" * 32),
    ]
    getdata(node, GetData([held, *missing]).serialize(), peer)
    tx_answer, notfound_answer = peer.sent
    assert isinstance(tx_answer, TxMsg)
    assert tx_answer.tx == transaction
    assert isinstance(notfound_answer, NotFound)
    assert notfound_answer.items == tuple(missing)


def test_a_peer_that_declined_relay_is_not_served_a_transaction_it_asks_for() -> None:
    # every code it could ask by, because gating one of the three is a
    # peer that gets the same answer by asking a different way
    transaction = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(transaction)
    node = a_data_node(mempool=mempool)
    for type_code, identifier in (
        (InventoryType.MSG_TX, transaction.id),
        (InventoryType.MSG_WITNESS_TX, transaction.id),
        (InventoryType.MSG_WTX, transaction.hash),
    ):
        peer = a_peer(relay_tx=False)
        getdata(node, GetData([Inventory(type_code, identifier)]).serialize(), peer)
        assert not peer.sent


def test_a_peer_that_declined_relay_is_still_served_a_block() -> None:
    # one getdata carrying both kinds, so the assertion is that the
    # answer is the block and nothing beside it
    block = a_block()
    transaction = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(transaction)
    node = a_data_node(
        mempool=mempool, block_db=SimpleNamespace(get_block=lambda h: block)
    )
    peer = a_peer(relay_tx=False)
    items = [
        Inventory(InventoryType.MSG_WTX, transaction.hash),
        Inventory(InventoryType.MSG_BLOCK, block.header.hash),
    ]
    getdata(node, GetData(items).serialize(), peer)
    (answer,) = peer.sent
    assert isinstance(answer, BlockMsg)
    assert answer.block.header.hash == block.header.hash


def test_a_block_this_node_holds_is_served() -> None:
    block = a_block()
    node = a_data_node(block_db=SimpleNamespace(get_block=lambda h: block))
    for type_code, with_witness in (
        (InventoryType.MSG_BLOCK, False),
        (InventoryType.MSG_WITNESS_BLOCK, True),
    ):
        peer = a_peer()
        items = [Inventory(type_code, block.header.hash)]
        getdata(node, GetData(items).serialize(), peer)
        (answer,) = peer.sent
        assert isinstance(answer, BlockMsg)
        assert answer.block.header.hash == block.header.hash
        assert answer.include_witness is with_witness


def test_a_block_this_node_does_not_hold_is_not_answered() -> None:
    node = a_data_node(block_db=SimpleNamespace(get_block=lambda h: None))
    peer = a_peer()
    items = [Inventory(InventoryType.MSG_BLOCK, b"\x11" * 32)]
    getdata(node, GetData(items).serialize(), peer)
    assert not peer.sent


def test_an_inventory_of_neither_kind_is_skipped() -> None:
    node = a_data_node(block_db=SimpleNamespace(get_block=lambda h: None))
    peer = a_peer()
    items = [Inventory(InventoryType.MSG_FILTERED_BLOCK, b"\x11" * 32)]
    getdata(node, GetData(items).serialize(), peer)
    assert not peer.sent


class FakeHeaderIndex:
    def __init__(
        self,
        tip: bytes | None = None,
        *,
        refuse: bool = False,
        header_index_tip: bytes = b"\xff" * 32,
        tip_status: BlockStatus = BlockStatus.valid_header,
    ) -> None:
        self.tip = tip
        self.refuse = refuse
        self.header_index = [header_index_tip]
        self.tip_status = tip_status
        self.given: list[BlockHeader] | None = None

    def add_headers(self, headers: Iterable[BlockHeader]) -> bytes | None:
        self.given = list(headers)
        if self.refuse:
            err_msg = "a header failing on its own terms"
            raise BTClibValueError(err_msg)
        return self.tip

    def get_block_info(self, block_hash: bytes) -> SimpleNamespace:
        return SimpleNamespace(status=self.tip_status)

    def get_block_locator_hashes(self) -> list[bytes]:
        return [b"\x00" * 32]


def test_a_full_batch_extending_the_best_chain_uses_the_usual_locator() -> None:
    # header_index already reaches an ordinary batch's own tip -- #122 is
    # about a fork below it, not this case -- so nothing here should
    # narrow the richer, multi-entry locator to a single hash
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    node = a_data_node(status=NodeStatus.SyncingHeaders)
    index = FakeHeaderIndex(tip=chain[-1].hash, header_index_tip=chain[-1].hash)
    node.chainstate.block_index = index
    peer = a_peer()
    headers(node, Headers(chain).serialize(), peer)
    assert index.given == chain
    (answer,) = peer.sent
    assert isinstance(answer, GetHeaders)
    assert answer.locator == (b"\x00" * 32,)
    assert node.status == NodeStatus.SyncingHeaders


def test_a_full_batch_on_a_live_fork_asks_from_the_fork_s_own_tip() -> None:
    # header_index does not move for a fork arriving below its own tip,
    # so its own locator would ask for this same batch again:
    # btclib-org/btclib-node#122
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    node = a_data_node(status=NodeStatus.SyncingHeaders)
    index = FakeHeaderIndex(tip=chain[-1].hash, header_index_tip=b"\xff" * 32)
    node.chainstate.block_index = index
    peer = a_peer()
    headers(node, Headers(chain).serialize(), peer)
    (answer,) = peer.sent
    assert isinstance(answer, GetHeaders)
    assert answer.locator == (chain[-1].hash,)
    assert node.status == NodeStatus.SyncingHeaders


def test_a_full_batch_on_an_invalid_fork_uses_the_usual_locator_instead() -> None:
    # a batch built on a parent this node already proved invalid is a
    # fork by the header_index test above, but not one worth asking a
    # peer for more of: nothing in this tree scores or bans a peer that
    # keeps sending it, so the locator falls back rather than naming that
    # fork's own tip back to it
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    node = a_data_node(status=NodeStatus.SyncingHeaders)
    index = FakeHeaderIndex(
        tip=chain[-1].hash,
        header_index_tip=b"\xff" * 32,
        tip_status=BlockStatus.invalid,
    )
    node.chainstate.block_index = index
    peer = a_peer()
    headers(node, Headers(chain).serialize(), peer)
    (answer,) = peer.sent
    assert isinstance(answer, GetHeaders)
    assert answer.locator == (b"\x00" * 32,)
    assert node.status == NodeStatus.SyncingHeaders


def test_a_full_batch_from_nowhere_known_asks_from_what_this_node_knows() -> None:
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    node = a_data_node(status=NodeStatus.SyncingHeaders)
    index = FakeHeaderIndex(tip=None)
    node.chainstate.block_index = index
    peer = a_peer()
    headers(node, Headers(chain).serialize(), peer)
    (answer,) = peer.sent
    assert isinstance(answer, GetHeaders)
    assert answer.locator == (b"\x00" * 32,)
    assert node.status == NodeStatus.SyncingHeaders


def test_a_short_batch_from_nowhere_known_asks_from_what_this_node_knows() -> None:
    # a short batch is the ordinary shape of a BIP130 announcement, and
    # unlike the full-batch case above the pre-existing code never sent
    # anything for it: the `len(headers) == 2000` guard was the only
    # place a follow-up GetHeaders was built. btclib-org/btclib-node#233
    chain = generate_random_header_chain(4, RegTest().genesis.hash)
    node = a_data_node(status=NodeStatus.SyncingHeaders)
    index = FakeHeaderIndex(tip=None)
    node.chainstate.block_index = index
    peer = a_peer()
    headers(node, Headers(chain).serialize(), peer)
    (answer,) = peer.sent
    assert isinstance(answer, GetHeaders)
    assert answer.locator == (b"\x00" * 32,)
    # not the ordinary end of a sync either: nothing of this batch
    # connected, so there is nothing to have caught up to
    assert node.status == NodeStatus.SyncingHeaders


def test_a_batch_on_an_already_invalid_parent_is_not_asked_for_again(
    tmp_path: Path,
) -> None:
    # add_headers has no reason to refuse this batch -- every header in
    # it still passes its own checks on its own terms, invalid parent or
    # not -- so avoiding a request for more of a branch this node has
    # already proved bad is callbacks.headers's own contract, proved
    # here through the real BlockIndex and not a fake standing in for
    # it. btclib-org/btclib-node#122
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    # heavier than the invalid fork below could ever become, so
    # header_index never shifts onto it and the fallback below is
    # decided by BlockStatus alone, not by tip == header_index[-1]
    active = generate_random_header_chain(3000, RegTest().genesis.hash)
    block_index.add_headers(active)
    for header in active:
        block_index.add_to_active_chain(header.hash)

    victim = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers(victim)
    block_index.invalidate(victim[0].hash)

    extension = generate_random_header_chain(2000, victim[0].hash, victim[0].time)
    node = a_data_node(block_index=block_index, status=NodeStatus.SyncingHeaders)
    peer = a_peer()
    headers(node, Headers(extension).serialize(), peer)

    assert block_index.header_index[-1] == active[-1].hash
    assert block_index.get_block_info(extension[-1].hash).status == BlockStatus.invalid
    (answer,) = peer.sent
    assert isinstance(answer, GetHeaders)
    assert extension[-1].hash not in answer.locator
    assert answer.locator == tuple(block_index.get_block_locator_hashes())
    chainstate.close()


def test_a_refused_batch_is_not_the_end_of_a_sync() -> None:
    # a batch refused for a bad proof of work is a misbehaving peer, not
    # the ordinary end of a sync: the raise reaches handle_p2p, which
    # drops the connection instead of this node believing itself caught
    # up. btclib-org/btclib-node#75
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    node = a_data_node(status=NodeStatus.SyncingHeaders)
    node.chainstate.block_index = FakeHeaderIndex(refuse=True)
    peer = a_peer()
    with pytest.raises(BTClibValueError):
        headers(node, Headers(chain).serialize(), peer)
    assert not peer.sent
    assert node.status == NodeStatus.SyncingHeaders


def test_a_short_batch_means_the_headers_are_synced() -> None:
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    node = a_data_node(status=NodeStatus.SyncingHeaders)
    node.chainstate.block_index = FakeHeaderIndex(tip=chain[-1].hash)
    peer = a_peer()
    headers(node, Headers(chain).serialize(), peer)
    assert not peer.sent
    assert node.status == NodeStatus.HeaderSynced


def test_a_short_batch_when_the_headers_are_already_synced_changes_nothing() -> None:
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    node = a_data_node(status=NodeStatus.BlockSynced)
    node.chainstate.block_index = FakeHeaderIndex(tip=chain[-1].hash)
    peer = a_peer()
    headers(node, Headers(chain).serialize(), peer)
    assert not peer.sent
    assert node.status == NodeStatus.BlockSynced


def test_this_node_answers_a_getheaders_from_what_it_knows() -> None:
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    node = a_data_node()
    asked: list[tuple[list[bytes], bytes]] = []

    def from_locators(locator: Sequence[bytes], stop: bytes) -> list[BlockHeader]:
        asked.append((list(locator), stop))
        return chain

    node.chainstate.block_index = SimpleNamespace(
        get_headers_from_locators=from_locators
    )
    peer = a_peer()
    locator, stop = [b"\x11" * 32, b"\x22" * 32], b"\x33" * 32
    getheaders(node, GetHeaders(ProtocolVersion, locator, stop).serialize(), peer)
    # the peer's question reaches the index as the peer asked it, which
    # a locator and a stop of the same value could not tell
    assert asked == [(locator, stop)]
    (sent,) = peer.sent
    assert isinstance(sent, Headers)
    assert list(sent.headers) == chain


def test_a_getheaders_this_node_cannot_answer_is_not_answered() -> None:
    node = a_data_node()
    node.chainstate.block_index = SimpleNamespace(
        get_headers_from_locators=lambda locator, stop: []
    )
    peer = a_peer()
    getheaders(
        node,
        GetHeaders(ProtocolVersion, [b"\x11" * 32], b"\x00" * 32).serialize(),
        peer,
    )
    assert not peer.sent


def a_filter_hash(height: int) -> bytes:
    return (height + 1).to_bytes(32, "big")


def a_filters_node(
    length: int = 8, *, stale: Mapping[bytes, Any] | Iterable[Any] = ()
) -> Any:
    """A node whose chain is `length` blocks, each with a canned filter.

    The filters are made up: what is this node's in BIP157 is which
    blocks a range names and what is refused, and a real filter would
    say nothing about either. `tests/unit/chainstate/filter_index.py`
    is where the filters themselves are.

    `stale` is blocks the index knows and the active chain does not,
    which is what a peer asking about an abandoned branch looks like.
    """
    active_chain = [(height).to_bytes(32, "big") for height in range(length)]
    header_dict = {
        block_hash: SimpleNamespace(index=height)
        for height, block_hash in enumerate(active_chain)
    }
    header_dict.update(stale)
    filter_index = SimpleNamespace(
        get_filter=lambda h: b"\x01" + h[-1:],
        get_header=lambda h: hash256(h)[::-1],
        get_filter_hash=lambda h: a_filter_hash(int.from_bytes(h, "big")),
    )
    return SimpleNamespace(
        chainstate=SimpleNamespace(
            block_index=SimpleNamespace(
                active_chain=active_chain,
                header_dict=header_dict,
                get_block_info=header_dict.__getitem__,
            ),
            filter_index=filter_index,
        ),
        logger=SimpleNamespace(
            info=lambda *a: None, warning=lambda *a: None, debug=lambda *a: None
        ),
    )


def a_getcfilters(
    node: Any,
    peer: Any,
    start: int,
    stop_height: int,
    filter_type: BlockFilterType = BlockFilterType.BASIC,
) -> None:
    stop_hash = node.chainstate.block_index.active_chain[stop_height]
    get_cfilters(node, GetCFilters(filter_type, start, stop_hash).serialize(), peer)


def test_a_range_of_filters_is_answered_one_message_per_block() -> None:
    node = a_filters_node()
    peer = a_peer()
    a_getcfilters(node, peer, 2, 5)
    # "sequentially in order by block height", and the whole range
    # including both ends
    assert [msg.block_hash for msg in peer.sent] == [
        h.to_bytes(32, "big") for h in range(2, 6)
    ]
    assert all(isinstance(msg, CFilter) for msg in peer.sent)
    assert all(msg.filter_type == BlockFilterType.BASIC for msg in peer.sent)
    assert [msg.filter_bytes for msg in peer.sent] == [
        b"\x01" + h.to_bytes(32, "big")[-1:] for h in range(2, 6)
    ]


def test_one_block_is_a_range_of_one() -> None:
    node = a_filters_node()
    peer = a_peer()
    a_getcfilters(node, peer, 3, 3)
    (msg,) = peer.sent
    assert msg.block_hash == (3).to_bytes(32, "big")


def test_get_cfilters_stops_once_the_connection_closes_mid_answer() -> None:
    # what Connection.async_send's own send-buffer bound (#101) looks
    # like from here: conn.status turns P2pConnStatus.Closed partway
    # through the range, and nothing further in it is worth serializing
    node = a_filters_node(length=10)
    peer = a_peer()
    sent = peer.sent

    def send_then_close(msg: Any) -> None:
        sent.append(msg)
        if len(sent) == 3:
            peer.status = P2pConnStatus.Closed

    peer.send = send_then_close
    a_getcfilters(node, peer, 0, 9)
    assert [msg.block_hash for msg in peer.sent] == [
        h.to_bytes(32, "big") for h in range(3)
    ]


def test_get_cfilters_refuses_a_gap_in_a_promised_index() -> None:
    # BIP157's service bit promises a filter for every block of the
    # active chain; a gap here is the index breaking that promise
    # rather than a request this node can decline
    node = a_filters_node()
    node.chainstate.filter_index.get_filter = lambda h: None
    peer = a_peer()
    with pytest.raises(Exception, match="no filter for a block"):
        a_getcfilters(node, peer, 2, 2)


def test_a_filter_type_this_node_does_not_serve_is_not_answered() -> None:
    # BIP158 defines the basic filter and nothing else, so any other
    # code is a type no node has; BIP157 says answer with nothing
    node = a_filters_node()
    peer = a_peer()
    a_getcfilters(node, peer, 0, 1, filter_type=cast(BlockFilterType, 1))
    assert not peer.sent


def test_a_stop_hash_this_node_never_heard_of_is_not_answered() -> None:
    node = a_filters_node()
    peer = a_peer()
    get_cfilters(
        node, GetCFilters(BlockFilterType.BASIC, 0, b"\x11" * 32).serialize(), peer
    )
    assert not peer.sent


def test_a_stop_hash_off_the_active_chain_is_not_answered() -> None:
    # a block this node knows and did not keep: its height is a height
    # on the branch it left, and answering would send the filters of
    # blocks the peer did not ask about
    stale_hash = b"\x22" * 32
    node = a_filters_node(stale={stale_hash: SimpleNamespace(index=3)})
    peer = a_peer()
    get_cfilters(
        node, GetCFilters(BlockFilterType.BASIC, 0, stale_hash).serialize(), peer
    )
    assert not peer.sent


def test_a_stop_hash_at_a_height_the_chain_has_not_reached_is_not_answered() -> None:
    node = a_filters_node(length=4, stale={b"\x33" * 32: SimpleNamespace(index=9)})
    peer = a_peer()
    get_cfilters(
        node, GetCFilters(BlockFilterType.BASIC, 0, b"\x33" * 32).serialize(), peer
    )
    assert not peer.sent


def test_a_range_that_runs_backwards_is_not_answered() -> None:
    node = a_filters_node()
    peer = a_peer()
    a_getcfilters(node, peer, 5, 2)
    assert not peer.sent

    # and the same range asked of getcfheaders, which is the half that
    # can tell: an empty range sends no cfilter either way, where a
    # cfheaders of no hashes is a message the peer would have to read
    peer = a_peer()
    get_cfheaders(
        node,
        GetCFHeaders(BlockFilterType.BASIC, 5, (2).to_bytes(32, "big")).serialize(),
        peer,
    )
    assert not peer.sent


@pytest.mark.parametrize(
    ("ask", "limit"),
    [(get_cfilters, MAX_GETCFILTERS_SIZE), (get_cfheaders, MAX_GETCFHEADERS_SIZE)],
    ids=["getcfilters", "getcfheaders"],
)
def test_a_range_is_bounded_strictly_below_the_limit(ask: Any, limit: int) -> None:
    # BIP157 bounds the difference and bounds it strictly, so a range
    # whose ends differ by exactly the limit is one block too many
    node = a_filters_node(length=limit + 2)
    request = GetCFilters if ask is get_cfilters else GetCFHeaders

    peer = a_peer()
    ask(
        node,
        request(BlockFilterType.BASIC, 0, (limit - 1).to_bytes(32, "big")).serialize(),
        peer,
    )
    assert peer.sent

    peer = a_peer()
    ask(
        node,
        request(BlockFilterType.BASIC, 0, (limit).to_bytes(32, "big")).serialize(),
        peer,
    )
    assert not peer.sent


def test_the_filter_hashes_of_a_range_are_answered_with_the_header_before_it() -> None:
    node = a_filters_node()
    peer = a_peer()
    stop_hash = (5).to_bytes(32, "big")
    get_cfheaders(
        node, GetCFHeaders(BlockFilterType.BASIC, 3, stop_hash).serialize(), peer
    )
    (msg,) = peer.sent
    assert isinstance(msg, CFHeaders)
    assert msg.stop_hash == stop_hash
    # the header of the block before the range: what the hashes below
    # chain onto, and without it a client could check nothing
    assert msg.previous_filter_header == hash256((2).to_bytes(32, "big"))[::-1]
    assert list(msg.filter_hashes) == [a_filter_hash(h) for h in range(3, 6)]


def test_a_range_that_starts_at_the_genesis_block_has_no_header_before_it() -> None:
    node = a_filters_node()
    peer = a_peer()
    get_cfheaders(
        node,
        GetCFHeaders(BlockFilterType.BASIC, 0, (2).to_bytes(32, "big")).serialize(),
        peer,
    )
    (msg,) = peer.sent
    # BIP157 defines the header before the genesis block's filter as
    # thirty-two zero octets, and there is no block to read one off
    assert msg.previous_filter_header == b"\x00" * 32
    assert list(msg.filter_hashes) == [a_filter_hash(h) for h in range(3)]


def test_a_getcfheaders_this_node_cannot_answer_is_not_answered() -> None:
    node = a_filters_node()
    peer = a_peer()
    get_cfheaders(
        node, GetCFHeaders(BlockFilterType.BASIC, 0, b"\x11" * 32).serialize(), peer
    )
    assert not peer.sent


def test_get_cfheaders_refuses_a_gap_in_the_header_before_the_range() -> None:
    node = a_filters_node()
    node.chainstate.filter_index.get_header = lambda h: None
    peer = a_peer()
    with pytest.raises(Exception, match="no filter header for the parent"):
        get_cfheaders(
            node,
            GetCFHeaders(BlockFilterType.BASIC, 3, (5).to_bytes(32, "big")).serialize(),
            peer,
        )


def test_get_cfheaders_refuses_a_gap_in_a_promised_index() -> None:
    node = a_filters_node()
    node.chainstate.filter_index.get_filter_hash = lambda h: None
    peer = a_peer()
    with pytest.raises(Exception, match="no filter for a block"):
        get_cfheaders(
            node,
            GetCFHeaders(BlockFilterType.BASIC, 0, (2).to_bytes(32, "big")).serialize(),
            peer,
        )


def test_the_checkpoints_are_every_thousandth_block_and_not_the_first() -> None:
    node = a_filters_node(length=2 * CFCHECKPT_INTERVAL + 3)
    peer = a_peer()
    stop_height = 2 * CFCHECKPT_INTERVAL + 1
    stop_hash = stop_height.to_bytes(32, "big")
    get_cfcheckpt(
        node, GetCFCheckpt(BlockFilterType.BASIC, stop_hash).serialize(), peer
    )
    (msg,) = peer.sent
    assert isinstance(msg, CFCheckpt)
    assert msg.stop_hash == stop_hash
    # "a multiple of 1,000 greater than 0": the genesis block is not a
    # checkpoint, and the stop block is one only if it falls on the
    # interval itself
    assert list(msg.filter_headers) == [
        hash256(height.to_bytes(32, "big"))[::-1]
        for height in (CFCHECKPT_INTERVAL, 2 * CFCHECKPT_INTERVAL)
    ]


def test_the_stop_block_is_a_checkpoint_when_its_own_height_is_one() -> None:
    # the boundary the rule is most specific about: "each block ... where
    # the block height is a multiple of 1,000 greater than 0" includes
    # the block the range terminates at, when that is what its height is
    node = a_filters_node(length=CFCHECKPT_INTERVAL + 1)
    peer = a_peer()
    stop_hash = CFCHECKPT_INTERVAL.to_bytes(32, "big")
    get_cfcheckpt(
        node, GetCFCheckpt(BlockFilterType.BASIC, stop_hash).serialize(), peer
    )
    (msg,) = peer.sent
    assert list(msg.filter_headers) == [hash256(stop_hash)[::-1]]


def test_a_chain_shorter_than_the_interval_has_no_checkpoints() -> None:
    node = a_filters_node(length=8)
    peer = a_peer()
    get_cfcheckpt(
        node,
        GetCFCheckpt(BlockFilterType.BASIC, (7).to_bytes(32, "big")).serialize(),
        peer,
    )
    (msg,) = peer.sent
    # an answer, and an empty one: a client that asked has been told
    # there is nothing to check against, which is not the same as
    # having been ignored
    assert not msg.filter_headers


def test_get_cfcheckpt_refuses_a_gap_in_a_promised_index() -> None:
    node = a_filters_node(length=CFCHECKPT_INTERVAL + 1)
    node.chainstate.filter_index.get_header = lambda h: None
    peer = a_peer()
    stop_hash = CFCHECKPT_INTERVAL.to_bytes(32, "big")
    with pytest.raises(Exception, match="no filter header for a block"):
        get_cfcheckpt(
            node, GetCFCheckpt(BlockFilterType.BASIC, stop_hash).serialize(), peer
        )


def test_a_getcfcheckpt_this_node_cannot_answer_is_not_answered() -> None:
    node = a_filters_node(length=4, stale={b"\x44" * 32: SimpleNamespace(index=2)})
    for stop_hash, filter_type in (
        (b"\x11" * 32, BlockFilterType.BASIC),  # never heard of
        (b"\x44" * 32, BlockFilterType.BASIC),  # off the active chain
        ((1).to_bytes(32, "big"), 1),  # a filter type nobody serves
    ):
        peer = a_peer()
        get_cfcheckpt(node, GetCFCheckpt(filter_type, stop_hash).serialize(), peer)
        assert not peer.sent, stop_hash.hex()
