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
import threading
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast

import pytest
from btclib.amount import sats_from_btc
from btclib.block import Block, BlockHeader
from btclib.exceptions import BTClibException, BTClibValueError
from btclib.hashes import hash256
from btclib.p2p.address import Addr, NetworkAddress, ServiceFlags
from btclib.p2p.addrv2 import (
    AddrV2,
    BIP155Network,
    NetworkAddressV2,
    SendAddrV2,
    addr_entry,
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
    MAX_ADDR_TO_SEND,
    MAX_GETCFHEADERS_SIZE,
    MAX_GETCFILTERS_SIZE,
    PROTOCOL_VERSION,
)
from btclib.p2p.negotiation import FeeFilter, GetAddr, SendHeaders, WtxidRelay
from btclib.p2p.reject import Reject, RejectCode
from btclib.script.witness import Witness

import btclib_node.p2p.callbacks as cb
from btclib_node.chains import RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.block_index import BlockStatus
from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE
from btclib_node.constants import MIN_BLOCKS_TO_KEEP, NodeStatus, P2pConnStatus
from btclib_node.exceptions import (
    ChainstateInconsistencyError,
    MissingPrevoutError,
    NonStandardTxError,
)
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from btclib_node.p2p.address import PeerDB, endpoint_key, peer_address
from btclib_node.p2p.callbacks import (
    MAX_CFILTERS_INFLIGHT_BYTES,
    MAX_GETDATA_INFLIGHT_BYTES,
    MAX_PENDING_CFILTERS_HEIGHTS,
    addr,
    addrv2,
    advance_cfilters,
    advance_getdata,
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
from tests import (
    generate_random_chain,
    generate_random_header_chain,
    generate_random_transaction,
    log_recorder,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from btclib.fee import FeeRate
    from btclib.tx.tx import Tx

    from btclib_node.chains import Chain
    from btclib_node.p2p.manager import P2pManager

# BIP155's table, for the networks these tests build an address of
_ADDRESS_SIZE = {
    BIP155Network.IPV4: 4,
    BIP155Network.IPV6: 16,
    BIP155Network.TORV3: 32,
}


def an_address(
    n: int = 0, network_id: BIP155Network = BIP155Network.IPV4
) -> NetworkAddressV2:
    """Build an active address of `network_id`, distinguished by `n`.

    Seen just now: an address the node would not serve is a different
    test, in tests/unit/p2p/address_test.py.
    """
    return NetworkAddressV2(
        timestamp=int(time.time()),
        services=0,
        network_id=network_id,
        address=n.to_bytes(_ADDRESS_SIZE[network_id], "big"),
        port=18444,
    )


def a_version_address(services: int = 0) -> NetworkAddress:
    """Build an unroutable `NetworkAddress`, the shape a `version` carries.

    A `version` message's own address field has no timestamp, unlike
    `NetworkAddressV2`.
    """
    return NetworkAddress(services, "0.0.0.0", 18444)  # noqa: S104


def make_node(
    addresses: Sequence[NetworkAddressV2], *, prefer_addressv2: bool = False
) -> tuple[Any, Any, list[Any]]:
    """Build a node with `peer_db` addresses active, and a peer stand-in."""
    peer_db = PeerDB(cast("Chain", None), cast("Path", None))
    for address in addresses:
        peer_db.active_addresses.append(address)
    sent: list[Any] = []
    conn = SimpleNamespace(
        prefer_addressv2=prefer_addressv2, send=sent.append, answered_getaddr=False
    )
    node = SimpleNamespace(p2p_manager=SimpleNamespace(peer_db=peer_db))
    return node, conn, sent


def test_an_ipv4_address_is_answered_in_an_addr() -> None:
    """A `getaddr` from a peer that has not asked for addrv2 gets an `Addr`."""
    address = an_address()
    node, conn, sent = make_node([address])
    getaddr(node, b"", conn)
    (answer,) = sent
    assert isinstance(answer, Addr)
    assert answer.addresses == (addr_entry(address),)
    # and it survives the wire, which is what the network filter is for
    assert Addr.parse(answer.serialize()).addresses == answer.addresses


def test_a_peer_that_asked_for_addrv2_gets_addrv2() -> None:
    """A `getaddr` from a peer with `prefer_addressv2` gets an `AddrV2`."""
    address = an_address()
    node, conn, sent = make_node([address], prefer_addressv2=True)
    getaddr(node, b"", conn)
    (answer,) = sent
    assert isinstance(answer, AddrV2)
    assert answer.addresses == (address,)


def test_an_address_addr_version_1_cannot_carry_is_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `Addr` answer to an addrv1 peer leaves out an address it cannot carry.

    An onion address has no addr version 1 entry to be built into, so
    one of them among the active addresses would cost the whole answer.
    The sample itself is a different test, below, so this patches it to
    the identity to isolate the addr-v1 filter it is testing.
    """
    monkeypatch.setattr(cb, "_addresses_to_send", lambda active: active)
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
    """An address addrv1 could not carry still reaches an addrv2 peer."""
    onion = an_address(network_id=BIP155Network.TORV3)
    node, conn, sent = make_node([onion], prefer_addressv2=True)
    getaddr(node, b"", conn)
    (answer,) = sent
    assert answer.addresses == (onion,)


def test_nothing_active_is_answered_with_nothing() -> None:
    """A `getaddr` against an empty active table gets no answer at all."""
    node, conn, sent = make_node([])
    getaddr(node, b"", conn)
    assert not sent


def test_nothing_active_is_answered_with_nothing_over_addrv2_either() -> None:
    """The same silence holds for an addrv2 peer, not only an addrv1 one."""
    node, conn, sent = make_node([], prefer_addressv2=True)
    getaddr(node, b"", conn)
    assert not sent


def test_a_getaddr_answer_is_a_sample_not_the_whole_table() -> None:
    """A `getaddr` answer is a 23% sample of the active table, not all of it.

    #71: Core's own reason for not serving the live table is that doing
    so tells anyone who asks the complete set of peers this node knows
    of. 500 addresses, 23% rounded up is 115 -- under MAX_ADDR_TO_SEND,
    so this also proves one sample answers in one message rather than
    several.
    """
    addresses = [an_address(n) for n in range(500)]
    node, conn, sent = make_node(addresses)
    getaddr(node, b"", conn)
    (answer,) = sent
    assert len(answer.addresses) == 115
    # a sample of what is active, not addresses invented for the answer
    assert set(answer.addresses) <= {addr_entry(address) for address in addresses}
    # drawn without replacement
    assert len(set(answer.addresses)) == len(answer.addresses)


def test_a_getaddr_answer_is_capped_at_max_addr_to_send() -> None:
    """A large active table is answered up to `MAX_ADDR_TO_SEND`, not 23% of it.

    #71: the chunking Core itself misbehaves a peer over is right at
    1000 -- 23% of 10000 is 2300, so the cap and not the percentage is
    what bounds this answer.
    """
    addresses = [an_address(n) for n in range(10000)]
    node, conn, sent = make_node(addresses, prefer_addressv2=True)
    getaddr(node, b"", conn)
    (answer,) = sent
    assert len(answer.addresses) == MAX_ADDR_TO_SEND


def test_a_second_getaddr_on_the_same_connection_is_ignored() -> None:
    """A peer asking `getaddr` twice on one connection is served the table once.

    #71: a peer asking in a loop is served the table once.
    """
    address = an_address()
    node, conn, sent = make_node([address])
    getaddr(node, b"", conn)
    getaddr(node, b"", conn)
    assert len(sent) == 1


def another_conn(sent: list[Any]) -> Any:
    """Build a second peer stand-in sharing `sent` with `make_node`'s own."""
    return SimpleNamespace(
        prefer_addressv2=False, send=sent.append, answered_getaddr=False
    )


def test_two_connections_close_together_are_answered_the_same_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two peers asking `getaddr` close together get the identical sample.

    #71: a fresh `secrets.SystemRandom().sample` per connection would
    let two peers connecting close together compare answers and infer
    what changed between them, which answering once per connection
    alone does not stop -- a new connection still draws fresh.
    """
    draws: list[list[NetworkAddressV2]] = []

    def counting_sample(active: list[NetworkAddressV2]) -> list[NetworkAddressV2]:
        draws.append(active)
        return list(active)

    monkeypatch.setattr(cb, "_addresses_to_send", counting_sample)
    address = an_address()
    node, conn1, sent = make_node([address])
    conn2 = another_conn(sent)
    getaddr(node, b"", conn1)
    getaddr(node, b"", conn2)
    assert len(draws) == 1
    assert sent[0].addresses == sent[1].addresses


def test_the_cached_sample_is_redrawn_once_it_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past `_ADDR_SAMPLE_LIFETIME` plus jitter, a new `getaddr` draws fresh."""
    draws: list[list[NetworkAddressV2]] = []

    def counting_sample(active: list[NetworkAddressV2]) -> list[NetworkAddressV2]:
        draws.append(active)
        return list(active)

    monkeypatch.setattr(cb, "_addresses_to_send", counting_sample)
    address = an_address()
    node, conn1, sent = make_node([address])
    conn2 = another_conn(sent)
    base_time = time.time()
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: base_time)
        getaddr(node, b"", conn1)
    with monkeypatch.context() as patch:
        # past the lifetime and the jitter both, so this is past the
        # expiration however the jitter draw landed
        future = base_time + cb._ADDR_SAMPLE_LIFETIME + cb._ADDR_SAMPLE_JITTER + 1
        patch.setattr(time, "time", lambda: future)
        getaddr(node, b"", conn2)
    assert len(draws) == 2


def a_version(
    *,
    protocol: int = PROTOCOL_VERSION,
    services: ServiceFlags = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS,
    nonce: int = 7,
    relay: bool | None = True,
    addr_from_port: int = 18444,
) -> bytes:
    """Build a serialized `version`, as the `version` callback receives one."""
    return a_parsed_version(
        protocol=protocol,
        services=services,
        nonce=nonce,
        relay=relay,
        addr_from_port=addr_from_port,
    ).serialize()


def a_parsed_version(
    *,
    protocol: int = PROTOCOL_VERSION,
    services: ServiceFlags = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS,
    nonce: int = 7,
    relay: bool | None = True,
    # a different host than "1.2.3.4", a_peer()'s own address: proof
    # that verack takes only the port from here, for an inbound peer,
    # and not the address -- btclib-org/btclib-node#70
    addr_from_port: int = 18444,
) -> Version:
    """Return the parsed `Version` `a_version` serializes, for field reads."""
    return Version(
        version=protocol,
        services=services,
        timestamp=1,
        addr_recv=a_version_address(),
        addr_from=NetworkAddress(services, "5.6.7.8", addr_from_port),
        nonce=nonce,
        user_agent=b"/Btclib/",
        start_height=0,
        relay=relay,
    )


def a_peer(**attributes: Any) -> Any:
    """Build a `Connection` double at every field a callback may read or write.

    `**attributes` overrides any default, for the one or two fields a given test
    needs to start from something other than the ordinary just-connected shape.
    """
    sent: list[Any] = []
    stopped = []
    peer = SimpleNamespace(
        id=0,
        send=sent.append,
        sent=sent,
        stop=lambda: stopped.append(True),
        stopped=stopped,
        status=P2pConnStatus.Open,
        # what Connection starts every fresh connection at, and what
        # `advance_cfilters` reads to pace a `getcfilters` answer: never
        # written here, so it never trips that pacing bound, the same
        # way a real connection whose peer reads promptly never would
        queued_send_bytes=0,
        version_message=None,
        # what `Connection` starts every fresh connection at, and what
        # `callbacks.version`/`callbacks.headers` overwrite -- 0 rather
        # than `None`, matching the real field (`p2p/connection.py`).
        # btclib-org/btclib-node#706
        best_known_height=0,
        wtxidrelay_received=False,
        prefer_addressv2=False,
        prefers_headers=False,
        # what Connection sets, and what the version callback overwrites
        relay_tx=True,
        download_queue=[],
        pending_eviction=False,
        last_block_timestamp=0,
        tx_requested={},
        ping_sent=0,
        ping_nonce=0,
        latency=0,
        _ping_lock=threading.Lock(),
        send_ping=lambda: sent.append("ping"),
        client=SimpleNamespace(getpeername=lambda: ("1.2.3.4", 18444)),
        inbound=False,
        address=peer_address("1.2.3.4", 18444),
    )
    peer.__dict__.update(attributes)
    return peer


def a_handshake_node(
    *,
    pending_outbound_nonces: Sequence[int] = (),
    status: NodeStatus = NodeStatus.HeaderSynced,
    peer_db: Any = None,
    promote_connection: Any = None,
    min_relay_feerate: FeeRate = DEFAULT_MIN_RELAY_FEERATE,
) -> Any:
    """Build a node double with just what handshake callbacks read or write."""
    discouraged: list[Any] = []
    own_nonces = set(pending_outbound_nonces)
    return SimpleNamespace(
        status=status,
        config=SimpleNamespace(min_relay_feerate=min_relay_feerate, pruned=False),
        p2p_manager=SimpleNamespace(
            pending_outbound_nonces=own_nonces,
            is_self_connect_nonce=own_nonces.__contains__,
            peer_db=peer_db,
            promote_connection=promote_connection or (lambda conn_id: None),
            discourage=discouraged.append,
            discouraged=discouraged,
        ),
        chainstate=SimpleNamespace(
            block_index=SimpleNamespace(get_block_locator_hashes=lambda: [b"\x00" * 32])
        ),
        logger=SimpleNamespace(
            info=lambda *a: None, warning=lambda *a: None, debug=lambda *a: None
        ),
    )


def commands(peer: Any) -> list[str]:
    """Return the command a message travels under, or itself if a string."""
    return [
        message if isinstance(message, str) else type(message).__name__
        for message in peer.sent
    ]


def test_a_version_is_answered_with_what_this_node_speaks() -> None:
    """A compatible `version` gets this node's own handshake trio in reply."""
    node = a_handshake_node()
    peer = a_peer()
    version(node, a_version(), peer)
    assert commands(peer) == ["WtxidRelay", "SendAddrV2", "Verack"]
    assert isinstance(peer.sent[0], WtxidRelay)
    assert isinstance(peer.sent[1], SendAddrV2)
    assert isinstance(peer.sent[2], Verack)
    assert peer.relay_tx is True
    assert not peer.stopped
    assert not node.p2p_manager.discouraged


def test_a_second_version_ahead_of_verack_is_ignored_outright() -> None:
    """A repeat `version`, `version_message` already set, gets no reply at all.

    Core's own guard against `pfrom.nVersion != 0`: no `WtxidRelay`,
    `SendAddrV2` or `Verack` resent, no discouragement, no drop --
    matching a peer's second `verack` and `wtxidrelay`, each already
    idempotent for the same reason. btclib-org/btclib-node#482
    """
    node = a_handshake_node()
    peer = a_peer(version_message=a_parsed_version())
    version(node, a_version(), peer)
    assert not peer.sent
    assert not peer.stopped
    assert not node.p2p_manager.discouraged


def test_a_version_carrying_our_own_nonce_is_this_node_calling_itself() -> None:
    """A `version` carrying this node's own nonce is a self-connection, dropped.

    #283: an incompatibility, not a protocol violation, and still cause to
    discourage.
    """
    node = a_handshake_node(pending_outbound_nonces=[7])
    peer = a_peer()
    version(node, a_version(nonce=7), peer)
    assert peer.stopped == [True]
    assert not peer.sent
    # #283: an incompatibility, not a protocol violation, and still cause
    assert node.p2p_manager.discouraged == [peer.address]


def test_a_peer_speaking_an_older_protocol_is_let_go() -> None:
    """A `version` below `PROTOCOL_VERSION` is refused, the peer discouraged."""
    node = a_handshake_node()
    peer = a_peer()
    version(node, a_version(protocol=PROTOCOL_VERSION - 1), peer)
    assert peer.stopped == [True]
    assert node.p2p_manager.discouraged == [peer.address]  # #283


def test_a_peer_without_the_witness_service_is_let_go() -> None:
    """A peer never advertising `NODE_WITNESS` is refused and discouraged."""
    node = a_handshake_node()
    peer = a_peer()
    version(node, a_version(services=ServiceFlags.NODE_NETWORK), peer)
    assert peer.stopped == [True]
    assert node.p2p_manager.discouraged == [peer.address]  # #283


def test_a_pruned_peer_is_let_go_only_once_the_blocks_are_synced() -> None:
    """A peer missing `NODE_NETWORK` is tolerated until this node needs blocks.

    Before `BlockSynced`, a peer that carries `NODE_WITNESS` alone can
    still serve this node headers, so it is kept; once blocks are
    wanted, the same peer is refused and discouraged for lacking the
    full-history service this node now needs.
    """
    pruned = ServiceFlags.NODE_WITNESS
    node = a_handshake_node(status=NodeStatus.HeaderSynced)
    peer = a_peer()
    version(node, a_version(services=pruned), peer)
    assert not peer.stopped
    assert not node.p2p_manager.discouraged

    node = a_handshake_node(status=NodeStatus.BlockSynced)
    peer = a_peer()
    version(node, a_version(services=pruned), peer)
    assert peer.stopped == [True]
    assert node.p2p_manager.discouraged == [peer.address]  # #283


def test_a_node_network_limited_only_dialled_peer_is_kept_once_synced() -> None:
    """A `NODE_NETWORK_LIMITED`-only peer this node dialled is not dropped.

    Block-capable by this tree's own `_can_serve_blocks` (download.py)
    and by Core's own `CanServeBlocks`, and accepted by Core's
    `GetDesirableServiceFlags` (net_processing.cpp:1861-1869) once close
    to the tip -- exactly the condition `BlockSynced` stands in for.
    Closes the gap #725's round 1 review found: this used to test
    `NODE_NETWORK` alone and refused such a peer.
    """
    limited = ServiceFlags.NODE_NETWORK_LIMITED | ServiceFlags.NODE_WITNESS
    node = a_handshake_node(status=NodeStatus.BlockSynced)
    peer = a_peer(inbound=False)
    version(node, a_version(services=limited), peer)
    assert not peer.stopped
    assert not node.p2p_manager.discouraged


def test_an_inbound_peer_with_neither_service_is_kept() -> None:
    """An inbound peer is never disconnected for its services, Core's own scope.

    `ExpectServicesFromConn` (net.h:847-856, at bitcoin/bitcoin@ca7162cde5)
    is `false` for an inbound connection -- this node accepted it, and
    never asked it for any particular service, so a missing one is not
    grounds to drop it, however synced this node is.
    """
    pruned = ServiceFlags.NODE_WITNESS
    node = a_handshake_node(status=NodeStatus.BlockSynced)
    peer = a_peer(inbound=True)
    version(node, a_version(services=pruned), peer)
    assert not peer.stopped
    assert not node.p2p_manager.discouraged


def test_a_dialled_peer_with_neither_service_is_dropped_once_synced() -> None:
    """A peer this node dialled, offering neither service, is refused.

    The pruned-peer test above already covers this with `a_peer()`'s
    own default `inbound=False`; spelled out explicitly here as the
    third of the three cases #725's round 1 review asked for, beside
    the two above.
    """
    pruned = ServiceFlags.NODE_WITNESS
    node = a_handshake_node(status=NodeStatus.BlockSynced)
    peer = a_peer(inbound=False)
    version(node, a_version(services=pruned), peer)
    assert peer.stopped == [True]
    assert node.p2p_manager.discouraged == [peer.address]


def test_a_version_that_says_it_relays_nothing_is_taken_at_its_word() -> None:
    """A `version` with `relay=False` sets `relay_tx` false, right attribute.

    The flag went to the attribute `Connection` defines, and nowhere else: the
    near miss that dropped it before was one letter long, `relay_txs` rather
    than `relay_tx`.
    """
    peer = a_peer()
    version(a_handshake_node(), a_version(relay=False), peer)
    assert peer.relay_tx is False
    # the flag went to the attribute Connection defines, and nowhere
    # else: the near miss that dropped it was one letter long
    assert not hasattr(peer, "relay_txs")


def test_a_version_without_the_relay_flag_is_a_peer_asking_for_relay() -> None:
    """A `version` with no relay flag is read as BIP37's own default: true.

    BIP37's default is what a peer older than the flag relies on: read as a
    false, it would be recorded as asking for the opposite.
    """
    peer = a_peer(relay_tx=False)
    version(a_handshake_node(), a_version(relay=None), peer)
    assert peer.relay_tx is True


def test_a_version_with_a_trailing_octet_still_costs_the_peer() -> None:
    """A `version` with a stray octet still raises, unlike `addr`/`addrv2`.

    Issue #149 leaves this one asymmetric on purpose: addr and addrv2 (below)
    accept a BinaryData stream, and btclib's own assert_no_trailing treats one
    as "the caller's", nothing past it checked -- Core's own leniency, reached
    with no second parser. Version.parse takes Octets alone (its own docstring:
    "the envelope is what says where a payload ends"), because its optional
    relay byte is detected by whether one more byte is there at all; handing it
    a stream that could plausibly hold more would make a genuinely unknown
    trailing octet misread as that flag. There is no btclib mechanism this node
    can lean on for `version` without a private copy of its field-by-field
    parse, so this still raises out of the callback -- main.handle_p2p_handshake
    is what turns that into conn.stop(), covered by
    tests/unit/p2p/main_test.py's own coverage of that generic behaviour.
    """
    peer = a_peer()
    with pytest.raises(BTClibValueError):
        version(a_handshake_node(), a_version() + b"\x00", peer)


def test_a_relay_octet_that_is_neither_0_nor_1_still_costs_the_peer() -> None:
    """A relay byte that is neither 0 nor 1 raises, unlike Core's own leniency.

    Issue #149's second half, closed on this: Core's own
    Unserialize<bool> (serialize.h) reads any nonzero octet as true,
    where Version.parse raises for anything but 0x00/0x01. Reaching
    Core's leniency here would mean either replaying Version.parse's
    whole field walk (the fixed fields, both NetworkAddress entries and
    the var_bytes user agent) to find where the flag sits in the
    payload, or matching the wording of the BTClibValueError it raises
    -- both bind this node to btclib's private shape rather than its
    public contract, unlike the stream-based leniency addr/addrv2 use
    above. And Core's own encoder (Serialize<bool>, the same
    serialize.h) can never write anything but 0x00/0x01: a bool
    converts to 0 or 1, nothing else, so no peer running Core -- or
    this node's own Version.serialize -- ever reaches this path, only
    an adversarial or already-broken one does. Disconnecting it is the
    policy kept.
    """
    peer = a_peer()
    # relay=None serializes nothing, so appending one raw octet is the
    # only relay byte this payload carries
    with pytest.raises(BTClibValueError):
        version(a_handshake_node(), a_version(relay=None) + b"\x02", peer)


def a_real_connection() -> Connection:
    """Build a real `Connection`, not a stand-in, for a defect a double misses.

    Not a stand-in: the defect this is about was a callback writing an attribute
    no `Connection` has, which a `SimpleNamespace` peer takes without a word and
    a `Connection` takes just as quietly -- Python attribute assignment does not
    check the name either way. What a real one adds is that the attribute
    asserted on below is the one the rest of the node reads.
    """
    manager = SimpleNamespace(node=a_handshake_node(), loop=None, peer_db=None)
    unroutable = peer_address("0.0.0.0", 18444)  # noqa: S104
    connection = Connection(
        cast("P2pManager", manager), socket.socket(), unroutable, 0, inbound=False
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
    *, relay: bool | None, wanted: bool
) -> None:
    """`version`'s relay flag lands on `Connection.relay_tx` at every value."""
    connection = a_real_connection()
    with connection.client:
        assert connection.relay_tx is True  # BIP37's default until told
        version(a_handshake_node(), a_version(relay=relay), connection)
        assert connection.relay_tx is wanted


def test_a_verack_completes_the_handshake() -> None:
    """A valid `verack` promotes the peer and sends the post-handshake messages.

    `promote_connection`, moving the peer out of `pending_connections`
    and into `connections`, runs right where `P2pConnStatus.Connected`
    is set (btclib-org/btclib-node#131).
    """
    promoted: list[int] = []
    peer = a_peer(id=9, version_message=a_parsed_version(), wtxidrelay_received=True)
    peer_db = PeerDB(cast("Chain", None), cast("Path", None))
    node = a_handshake_node(promote_connection=promoted.append, peer_db=peer_db)
    verack(node, b"", peer)
    assert peer.status == P2pConnStatus.Connected
    assert commands(peer) == [
        "SendHeaders",
        "SendCmpct",
        "ping",
        "GetAddr",
        "GetHeaders",
    ]
    assert isinstance(peer.sent[0], SendHeaders)
    assert isinstance(peer.sent[1], SendCmpct)
    assert isinstance(peer.sent[3], GetAddr)
    assert isinstance(peer.sent[4], GetHeaders)
    assert not peer.stopped
    # out of P2pManager.pending_connections and into connections, right
    # where P2pConnStatus.Connected is set: btclib-org/btclib-node#131
    assert promoted == [9]


# No FeeFilter is sent from verack itself: DownloadManager.
# _send_due_feefilters (download_test.py) is what tells a peer this
# node's own relay floor, reached from Node.run's own loop rather than
# a one-time handshake action, matching Core's own MaybeSendFeefilter.
# btclib-org/btclib-node#275


def test_an_outbound_handshake_records_the_address_dialled() -> None:
    """A completed outbound handshake records the address this node dialled.

    #70: evidence this node dialled it and a socket answered, not the
    peer's own unauthenticated word for its address, and the live
    handshake's own services rather than whatever an earlier gossip of
    the same peer happened to carry.
    """
    # #70: evidence this node dialled it and a socket answered, not the
    # peer's own unauthenticated word for its address
    dialled = peer_address("1.2.3.4", 18444)
    peer = a_peer(
        version_message=a_parsed_version(services=ServiceFlags.NODE_NETWORK),
        wtxidrelay_received=True,
        inbound=False,
        address=dialled,
    )
    peer_db = PeerDB(cast("Chain", None), cast("Path", None))
    verack(a_handshake_node(peer_db=peer_db), b"", peer)
    (recorded,) = peer_db.active_addresses
    assert recorded.address == dialled.address
    assert recorded.port == dialled.port
    # the live handshake's own services, not whatever the address was
    # last recorded with
    assert recorded.services == ServiceFlags.NODE_NETWORK
    # and the connection's own idea of its peer moves to the same
    # endpoint, or manager.py's already-connected check keeps comparing
    # against the address dialled with -- never what a later gossip of
    # this same peer draws back
    assert endpoint_key(peer.address) == endpoint_key(recorded)


def test_an_inbound_handshake_records_the_peers_announced_port() -> None:
    """An inbound handshake records the port the peer's own `version` names.

    #70: `sock_accept`'s own port is the peer's ephemeral one, never one
    anything could dial back on -- only the peer's own `addr_from` names
    a listening port, and it is that port, not the ephemeral one, that
    is recorded.
    """
    # #70: sock_accept's own port is the peer's ephemeral one, never one
    # anything could dial back on -- only the peer's own version names a
    # listening port
    accepted = peer_address("1.2.3.4", 55555)
    peer = a_peer(
        version_message=a_parsed_version(addr_from_port=8333),
        wtxidrelay_received=True,
        inbound=True,
        address=accepted,
    )
    peer_db = PeerDB(cast("Chain", None), cast("Path", None))
    verack(a_handshake_node(peer_db=peer_db), b"", peer)
    (recorded,) = peer_db.active_addresses
    # the accepted connection's own address, proven reachable by the TCP
    # handshake -- and not addr_from's own "5.6.7.8", which nothing here
    # ever connected to
    assert recorded.address == accepted.address
    assert recorded.port == 8333
    assert endpoint_key(peer.address) == endpoint_key(recorded)


def test_an_inbound_peer_naming_no_port_is_not_recorded() -> None:
    """#70: a `version` naming port zero is not evidence of a listening one."""
    # #70: a port of zero is not evidence of a listening one
    peer = a_peer(
        version_message=a_parsed_version(addr_from_port=0),
        wtxidrelay_received=True,
        inbound=True,
    )
    peer_db = PeerDB(cast("Chain", None), cast("Path", None))
    verack(a_handshake_node(peer_db=peer_db), b"", peer)
    assert peer_db.active_addresses == []


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
    """The connected-peer log line uses `getpeername`'s own host, IPv4 or IPv6.

    A v4-mapped IPv6 address is logged as the plain IPv4 form
    `getpeerinfo` would answer with, and a genuine IPv6 host is
    bracketed the way an endpoint with a port needs to be.
    """
    logged, info = log_recorder()
    node = a_handshake_node(peer_db=PeerDB(cast("Chain", None), cast("Path", None)))
    node.logger.info = info
    peer = a_peer(
        version_message=a_parsed_version(),
        wtxidrelay_received=True,
        client=SimpleNamespace(getpeername=lambda: (host, 18444)),
    )
    verack(node, b"", peer)
    assert logged == [f"Connected to {endpoint}, connection 0"]


def test_the_handshake_asks_the_socket_for_the_peer_once() -> None:
    """`getpeername` is called exactly once while completing a handshake.

    A second lookup is a second chance for the peer to have gone,
    raising `OSError` where the first answered.
    """
    # A second lookup is a second chance for the peer to have gone,
    # raising OSError where the first answered.
    sockaddr = ("1.2.3.4", 18444)
    lookups: list[tuple[str, int]] = []

    def getpeername() -> tuple[str, int]:
        lookups.append(sockaddr)
        return sockaddr

    peer = a_peer(
        version_message=a_parsed_version(),
        wtxidrelay_received=True,
        client=SimpleNamespace(getpeername=getpeername),
    )
    peer_db = PeerDB(cast("Chain", None), cast("Path", None))
    verack(a_handshake_node(peer_db=peer_db), b"", peer)
    assert lookups == [sockaddr]


def test_a_verack_before_the_version_is_let_go() -> None:
    """A `verack` reaching a peer that never sent its `version` is let go.

    Nothing to promote and nothing to record: `verack` before `version`
    is a peer out of protocol order, dropped and discouraged -- #283.
    """
    promoted: list[int] = []
    node = a_handshake_node(promote_connection=promoted.append)
    peer = a_peer(wtxidrelay_received=True)
    verack(node, b"", peer)
    assert peer.stopped == [True]
    assert peer.status == P2pConnStatus.Open
    assert promoted == []
    assert node.p2p_manager.discouraged == [peer.address]  # #283


def test_a_verack_from_a_peer_that_never_asked_for_wtxid_relay_is_let_go() -> None:
    """A `verack` from a peer that skipped `wtxidrelay` is let go, not promoted.

    Every peer this node still talks to negotiates wtxid relay first;
    one that reaches `verack` without it is refused rather than
    promoted -- #283.
    """
    promoted: list[int] = []
    node = a_handshake_node(promote_connection=promoted.append)
    peer = a_peer(version_message=object())
    verack(node, b"", peer)
    assert peer.stopped == [True]
    assert promoted == []
    assert node.p2p_manager.discouraged == [peer.address]  # #283


def test_the_flags_a_peer_sets_on_this_connection() -> None:
    """`wtxidrelay`, `sendaddrv2` and `sendheaders` each set their own flag.

    None of the three carries a payload; receiving one at all is what
    the flag records.
    """
    peer = a_peer()
    wtxidrelay(a_handshake_node(), b"", peer)
    sendaddrv2(a_handshake_node(), b"", peer)
    sendheaders(a_handshake_node(), b"", peer)
    assert peer.wtxidrelay_received
    assert peer.prefer_addressv2
    assert peer.prefers_headers


def test_a_feefilter_lands_on_the_connection() -> None:
    """An ordinary `feefilter` sets `peer.feefilter` to the rate it carries."""
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
    """A `feefilter` naming a rate outside MoneyRange reads as no filter.

    Core acts on a received rate only within MoneyRange -- 0 to MAX_MONEY
    inclusive (net_processing.cpp's NetMsgType::FEEFILTER, consensus/amount.h's
    MoneyRange) -- and leaves either side of it parsed but unused, rather than
    turning it into a filter nothing a real, non-negative fee rate could ever
    fail.
    """
    # Core acts on a received rate only within MoneyRange -- 0 to
    # MAX_MONEY inclusive (net_processing.cpp's NetMsgType::FEEFILTER,
    # consensus/amount.h's MoneyRange) -- and leaves either side of it
    # parsed but unused, rather than turning it into a filter nothing
    # a real, non-negative fee rate could ever fail
    peer = a_peer()
    feefilter(a_handshake_node(), FeeFilter(feerate).serialize(), peer)
    assert peer.feefilter == 0


def test_a_feefilter_at_the_edge_of_the_money_range_is_kept() -> None:
    """A `feefilter` naming exactly MAX_MONEY is kept, the bound inclusive."""
    # the bound is inclusive, so exactly MAX_MONEY is still a filter
    peer = a_peer()
    at_the_edge = sats_from_btc(Decimal(21_000_000))
    feefilter(a_handshake_node(), FeeFilter(at_the_edge).serialize(), peer)
    assert peer.feefilter == at_the_edge


def test_a_ping_is_answered_with_the_nonce_it_carried() -> None:
    """A `ping` is answered with a `pong` carrying the same nonce back."""
    peer = a_peer()
    ping(a_handshake_node(), Ping(1234).serialize(), peer)
    (answer,) = peer.sent
    assert isinstance(answer, Pong)
    assert answer.nonce == 1234


def test_a_pong_answering_our_ping_is_a_latency_measurement() -> None:
    """A `pong` carrying the outstanding nonce clears it and records a latency.

    `ping_sent` and `ping_nonce` are reset to zero once answered, so a
    second, unrelated `pong` cannot be mistaken for answering the same
    round trip again.
    """
    node = a_handshake_node()
    peer = a_peer(ping_sent=time.time() - 0.5, ping_nonce=1234)
    pong(node, Pong(1234).serialize(), peer)
    assert peer.latency > 0
    assert peer.ping_sent == 0
    assert peer.ping_nonce == 0
    assert not peer.stopped
    assert not node.p2p_manager.discouraged


def test_a_pong_with_the_wrong_nonce_is_a_peer_not_speaking_the_protocol() -> None:
    """A `pong` answering a nonce this node never sent drops and discourages.

    A mismatched nonce cannot be an honest race, since only one `ping`
    is outstanding at a time -- #283.
    """
    node = a_handshake_node()
    peer = a_peer(ping_sent=time.time(), ping_nonce=1234)
    pong(node, Pong(4321).serialize(), peer)
    assert peer.stopped == [True]
    assert node.p2p_manager.discouraged == [peer.address]  # #283


def test_a_pong_nobody_pinged_for_is_ignored() -> None:
    """A `pong` with no `ping` outstanding at all is ignored, not discouraged.

    `ping_nonce` starts at zero, so this is the case above with no
    outstanding round trip to mismatch against.
    """
    node = a_handshake_node()
    peer = a_peer()
    pong(node, Pong(1234).serialize(), peer)
    assert not peer.stopped
    assert peer.latency == 0
    assert not node.p2p_manager.discouraged


def test_the_addresses_a_peer_sends_are_kept() -> None:
    """Both `addr` and `addrv2` land the same addresses in the peer database.

    BIP155's record either way, the addr version 1 entry being
    translated back into one; and without the timestamp the peer
    quoted, which is `PeerDB.add_addresses`'s own doing.
    """
    given = [an_address(1), an_address(2)]
    for callback, message in (
        (addr, Addr([addr_entry(address) for address in given])),
        (addrv2, AddrV2(given)),
    ):
        peer_db = PeerDB(cast("Chain", None), cast("Path", None))
        node = a_handshake_node(peer_db=peer_db)
        callback(node, message.serialize(), a_peer())
        # BIP155's record either way, the addr version 1 entry being
        # translated back into one; and without the timestamp the peer
        # quoted, which is PeerDB.add_addresses' doing
        assert peer_db.addresses == {replace(address, timestamp=0) for address in given}


def test_an_octet_past_an_addr_or_addrv2_no_longer_costs_the_peer() -> None:
    """A trailing octet on `addr` or `addrv2` is parsed past, not a disconnect.

    issue #149: btclib's own assert_no_trailing raises out of
    Addr.parse/AddrV2.parse for exactly this, which main.handle_p2p
    turns into a disconnect if the callback lets it through. Core does
    not disconnect here (net_processing.cpp's ProcessMessage reads what
    it wants out of vRecv and never checks for anything left), and this
    node now matches that for the two of the three messages issue #149
    is about where it can without a second copy of btclib's codec --
    Addr and AddrV2 accept a stream, and btclib's own assert_no_trailing
    docstring calls a stream "the caller's", nothing past it checked.
    """
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
        peer_db = PeerDB(cast("Chain", None), cast("Path", None))
        node = a_handshake_node(peer_db=peer_db)
        peer = a_peer()
        callback(node, message.serialize() + b"\x00", peer)
        assert peer_db.addresses == {replace(address, timestamp=0) for address in given}
        assert not peer.stopped


def test_an_address_of_a_network_nobody_here_has_heard_of_is_kept() -> None:
    """An addrv2 network id nobody recognizes is stored rather than rejected.

    What this used to cost: the network id was an enumeration over the
    ids BIP155 had assigned, so a yggdrasil peer raised out of the
    parser and p2p.main turned that into a disconnect. The whole point
    of the format is that a new network needs no new message.
    """
    # what this used to cost: the network id was an enumeration over the
    # ids BIP155 had assigned, so a yggdrasil peer raised out of the
    # parser and p2p.main turned that into a disconnect. The whole point
    # of the format is that a new network needs no new message.
    yggdrasil = NetworkAddressV2(0, 0, 7, b"\x02" + b"\x22" * 15, 18444)
    unassigned = NetworkAddressV2(0, 0, 250, b"\x33" * 8, 18444)
    peer_db = PeerDB(cast("Chain", None), cast("Path", None))
    node = a_handshake_node(peer_db=peer_db)
    peer = a_peer()
    addrv2(node, AddrV2([yggdrasil, unassigned]).serialize(), peer)
    assert peer_db.addresses == {yggdrasil, unassigned}
    assert not peer.stopped
    # and neither is dialled or gossiped on, there being no address of
    # either kind this node knows what to do with
    assert peer_db.random_address() is None


def test_a_notfound_is_logged_rather_than_held_against_the_peer() -> None:
    """A `notfound` is logged as a warning, costing the peer nothing."""
    logged, warning = log_recorder()
    node = a_handshake_node()
    node.logger.warning = warning
    peer = a_peer()
    not_found(
        node,
        NotFound([Inventory(InventoryType.MSG_TX, b"\x11" * 32)]).serialize(),
        peer,
    )
    assert logged
    assert not peer.stopped


def test_a_notfound_frees_the_transaction_it_names_to_be_asked_of_someone_else() -> (
    None
):
    """A `notfound` clears the tx entry named, leaving a block entry alone.

    `DownloadManager.tx_download`'s own in-flight table, so an ask this peer
    will not answer is not held against it forever; a `MSG_BLOCK` item carries
    no such bookkeeping to clear, blocks never having been requested through a
    mechanism a `notfound` could complete.
    """
    # `DownloadManager.tx_download`'s own in-flight table, so an ask
    # this peer will not answer is not held against it forever
    node = a_handshake_node()
    peer = a_peer(tx_requested={b"\x11" * 32: 0.0, b"\x22" * 32: 0.0})
    not_found(
        node,
        NotFound(
            [
                Inventory(InventoryType.MSG_WTX, b"\x11" * 32),
                Inventory(InventoryType.MSG_BLOCK, b"\x22" * 32),
            ]
        ).serialize(),
        peer,
    )
    assert peer.tx_requested == {b"\x22" * 32: 0.0}


def test_a_reject_names_the_transaction_it_is_about() -> None:
    """A `reject` is logged with its code, reason and the txid it is about."""
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


def test_a_reject_names_a_reserved_code_by_number() -> None:
    """A code BIP61 reserves without naming logs as the bare number.

    `Reject.code` is a `RejectCode` where a member names the value and
    a plain `int` where none does (`btclib.p2p.reject`'s own module
    docstring): 0x44 falls in the 0x40-0x4f "Server policy rule" range
    BIP61 reserves beside `nonstandard`, `dust`, `insufficientfee` and
    `checkpoint`, and no member of `RejectCode` answers to it.
    """
    logged: list[str] = []
    node = a_handshake_node()
    node.logger.warning = logged.append
    peer = a_peer()
    txid = bytes(range(32))
    message = Reject("tx", 0x44, "reserved code", txid)
    reject(node, message.serialize(), peer)
    (line,) = logged
    assert line == f"Reject received: 68, reserved code, {txid.hex()}"
    assert not peer.stopped


def a_transaction() -> Tx:
    """Build a random transaction carrying a witness.

    With a witness, so that a txid and a wtxid are different bytes and
    an answer naming the wrong one cannot pass.
    """
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
    """Build a node whose mempool, chain and download manager are real enough.

    `BlockSynced` by default, since a transaction callback only accepts once the
    chain is caught up; `status` moves that to test the gate.
    """
    node = a_handshake_node(status=status)
    node.mempool = mempool if mempool is not None else Mempool(Logger(debug=True))
    node.chain = RegTest()
    node.block_db = block_db
    node.download_manager = SimpleNamespace(received_txs=[], inv_txs=[])
    # written by `getdata` only where `advance_getdata` pauses; empty
    # here for every test that never trips that pacing bound
    node.pending_getdata = {}
    if block_index is not None:
        node.chainstate.block_index = block_index
    return node


def test_a_transaction_that_verifies_is_kept_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transaction `verify_mempool_acceptance` accepts is kept and reported.

    Kept in the mempool, and reported to `download_manager.received_txs`
    keyed on the sending peer's id, for the download manager's own
    announce-to-others bookkeeping.
    """
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
    """`MissingPrevoutError` out of verification drops the transaction quietly.

    This node lacking the parent is not a reason to keep asking or to
    report anything: it is simply not kept.
    """

    def missing(node: Any, transaction: Any) -> NoReturn:
        raise MissingPrevoutError

    monkeypatch.setattr(cb, "verify_mempool_acceptance", missing)
    transaction = a_transaction()
    node = a_data_node()
    tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert not node.mempool.contains_tx(transaction)
    assert node.download_manager.received_txs == []


def test_a_transaction_only_relay_policy_refuses_costs_the_peer_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`NonStandardTxError` out of verification drops the transaction alone.

    Core's own rule above the flag set `interpreter.STANDARD_FLAGS`
    copies: "we do not ban/disconnect nodes that forward txs violating
    the additional (non-mandatory) rules here, to improve forwards and
    backwards compatibility" (`src/policy/policy.h:112-117`,
    at bitcoin/bitcoin@9be056a8a7). The class is a `BTClibValueError`,
    so letting it out of `tx` would reach `handle_p2p`'s own
    `isinstance(e, BTClibException)` and discourage the peer --
    `test_a_callback_that_raises_a_btclib_exception_costs_the_peer`
    (`p2p/main_test.py`) is that half, by type -- which is what the
    catch this pins exists to prevent.
    """

    def non_standard(node: Any, transaction: Any) -> NoReturn:
        err_msg = "non-minimal push"
        raise NonStandardTxError(err_msg)

    monkeypatch.setattr(cb, "verify_mempool_acceptance", non_standard)
    transaction = a_transaction()
    node = a_data_node()
    # the class is on the discouraging side of that test, so this catch
    # is the whole of what keeps the peer
    assert issubclass(NonStandardTxError, BTClibException)
    tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert not node.mempool.contains_tx(transaction)
    assert node.download_manager.received_txs == []


def test_a_transaction_failing_a_consensus_check_costs_the_peer_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare `BTClibValueError` out of verification drops the transaction.

    Unlike `NonStandardTxError` above, this is the consensus half: Core
    does not discourage a peer for *any* transaction failure -- "Tx
    failures never trigger disconnections/bans ... either due to
    non-consensus relay policies ... or due to new consensus rules
    introduced in soft forks" (`src/validation.cpp:2112-2117`, at
    bitcoin/bitcoin@4519933391) -- and there is no `MaybePunishNodeForTx`
    at all, where `MaybePunishNodeForBlock` exists and is called.
    `test_a_consensus_invalid_transaction_costs_the_peer_nothing`
    (`p2p/main_test.py`) is the same claim through `handle_p2p`'s own
    dispatch rather than by calling `tx` directly.
    btclib-org/btclib-node#843
    """

    def consensus_invalid(node: Any, transaction: Any) -> NoReturn:
        err_msg = "bad-txns-nonfinal"
        raise BTClibValueError(err_msg)

    monkeypatch.setattr(cb, "verify_mempool_acceptance", consensus_invalid)
    transaction = a_transaction()
    node = a_data_node()
    tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert not node.mempool.contains_tx(transaction)
    assert node.download_manager.received_txs == []


def test_a_refused_transaction_is_not_reverified_on_resubmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second copy of a refused wtxid is dropped before verification reruns.

    `interpreter.check_transaction` pays for two full engine runs on the
    failing path (`_consensus_accepts`'s own second `verify_transaction`
    call), so a peer resending a candidate this node has already refused
    is what `Mempool.mark_rejected` bounds. btclib-org/btclib-node#845
    """
    calls: list[bytes] = []

    def consensus_invalid(node: Any, transaction: Any) -> NoReturn:
        calls.append(transaction.hash)
        err_msg = "bad-txns-nonfinal"
        raise BTClibValueError(err_msg)

    monkeypatch.setattr(cb, "verify_mempool_acceptance", consensus_invalid)
    transaction = a_transaction()
    node = a_data_node()
    payload = TxMsg(transaction, include_witness=True).serialize()
    tx(node, payload, a_peer(id=3))
    tx(node, payload, a_peer(id=4))
    assert calls == [transaction.hash]
    assert not node.mempool.contains_tx(transaction)


def test_a_transaction_missing_its_parent_is_reverified_on_resubmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MissingPrevoutError` is not recorded, so a resend tries again.

    Unlike a genuine refusal: the missing parent can arrive on its own,
    with no block having to connect first, so nothing tells
    `Mempool`'s reject cache when a `MissingPrevoutError` might stop
    holding -- Core's identical exemption is `TX_MISSING_INPUTS`, never
    added to `m_recent_rejects` either.
    """
    calls: list[bytes] = []

    def missing(node: Any, transaction: Any) -> NoReturn:
        calls.append(transaction.hash)
        raise MissingPrevoutError

    monkeypatch.setattr(cb, "verify_mempool_acceptance", missing)
    transaction = a_transaction()
    node = a_data_node()
    payload = TxMsg(transaction, include_witness=True).serialize()
    tx(node, payload, a_peer(id=3))
    tx(node, payload, a_peer(id=4))
    assert calls == [transaction.hash, transaction.hash]


def test_a_transaction_already_held_skips_reverification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wtxid already in the mempool is dropped before verification runs.

    `test_a_transaction_already_held_is_not_reported_twice` below already
    covers the outcome; this is the same case for the cost, which
    `contains_tx`'s own pre-verification check in `tx` is what spares.
    """
    # a single-line lambda rather than a `def`: this is asserted never to
    # run, and a `def` whose body sits on its own line is a statement
    # coverage counts separately from the assignment that defines it, so
    # a call that never happens would leave that line permanently unmet
    # by the 100% floor rather than proving the point the test makes
    calls: list[bytes] = []
    monkeypatch.setattr(
        cb,
        "verify_mempool_acceptance",
        lambda node, transaction: calls.append(transaction.hash),
    )
    transaction = a_transaction()
    node = a_data_node()
    node.mempool.add_tx(transaction)
    tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert calls == []


def test_a_corrupted_stored_record_propagates_out_of_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tx` has no catch of its own for `ChainstateInconsistencyError`.

    Unlike `MissingPrevoutError` above, it propagates uncaught, for
    `handle_p2p`'s own generic catch to stop the one connection without
    discouraging the peer --
    `test_a_callback_that_raises_drops_the_peer` (`p2p/main_test.py`)
    already proves that half by type, `ChainstateInconsistencyError`
    being a plain `RuntimeError`. btclib-org/btclib-node#631
    """

    def corrupted(node: Any, transaction: Any) -> NoReturn:
        err_msg = "stored utxo- record failed to parse"
        raise ChainstateInconsistencyError(err_msg)

    monkeypatch.setattr(cb, "verify_mempool_acceptance", corrupted)
    transaction = a_transaction()
    node = a_data_node()
    with pytest.raises(ChainstateInconsistencyError):
        tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert not node.mempool.contains_tx(transaction)
    assert node.download_manager.received_txs == []


def test_a_transaction_received_before_the_node_is_synced_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transaction arriving before `BlockSynced` is dropped, not the peer.

    #129: a peer's version now always asks for relay, so a
    transaction sent while this node is still syncing is possible --
    Core's own reason for the same drop is that the utxo set is not
    caught up enough to check it, not that the peer misbehaved.
    verify_mempool_acceptance is patched to accept unconditionally,
    so the mempool staying empty is the gate firing rather than a
    coincidental rejection.
    """
    # #129: a peer's version now always asks for relay, so a
    # transaction sent while this node is still syncing is possible --
    # Core's own reason for the same drop is that the utxo set is not
    # caught up enough to check it, not that the peer misbehaved.
    # verify_mempool_acceptance is patched to accept unconditionally,
    # so the mempool staying empty is the gate firing rather than a
    # coincidental rejection
    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 0)
    transaction = a_transaction()
    node = a_data_node(status=NodeStatus.HeaderSynced)
    tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert not node.mempool.contains_tx(transaction)
    assert node.download_manager.received_txs == []


def test_a_transaction_already_held_is_not_reported_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transaction already in the mempool is not reported a second time.

    `contains_tx` gates the report ahead of `add_tx`, so a duplicate
    never reaches `download_manager.received_txs` even though
    `add_tx` itself would be a harmless no-op for it.
    """
    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 0)
    transaction = a_transaction()
    node = a_data_node()
    node.mempool.add_tx(transaction)
    tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert node.download_manager.received_txs == []


def test_a_transaction_a_full_mempool_declined_is_not_reported_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transaction the mempool evicts right back out is not reported either.

    add_tx adds this provisionally and evicts it right back out for
    being the only, and so the worst, transaction held once past
    bytesize_limit (Mempool._evict_to_limit, btclib-org/btclib-node#294)
    -- the same transaction the pre-call contains_tx check alone
    cannot see either way, since it answers False both before the call
    and after. Reporting it here would queue it for announcement to
    every other peer, and one that then asks for it gets `notfound`
    for a transaction this node never actually kept.
    btclib-org/btclib-node#277
    """
    # add_tx adds this provisionally and evicts it right back out for
    # being the only, and so the worst, transaction held once past
    # bytesize_limit (Mempool._evict_to_limit, btclib-org/btclib-node#294)
    # -- the same transaction the pre-call contains_tx check alone
    # cannot see either way, since it answers False both before the call
    # and after. Reporting it here would queue it for announcement to
    # every other peer, and one that then asks for it gets `notfound`
    # for a transaction this node never actually kept.
    # btclib-org/btclib-node#277
    monkeypatch.setattr(cb, "verify_mempool_acceptance", lambda node, tx: 0)
    transaction = a_transaction()
    node = a_data_node()
    node.mempool.bytesize_limit = 0
    tx(node, TxMsg(transaction, include_witness=True).serialize(), a_peer(id=3))
    assert not node.mempool.contains_tx(transaction)
    assert node.download_manager.received_txs == []


class FakeBlockIndex:
    """A block index stand-in with fixed answers and recorded calls.

    `header_dict` is `infos` itself, the same way the real `BlockIndex`'s
    `get_block_info` reads `self.header_dict[block_hash]` -- a block
    hash this stand-in was built with is "known" on both counts at once,
    matching the real object rather than needing a second table kept in
    step with the first.
    """

    def __init__(
        self, infos: dict[bytes, Any], *, accepts_headers: bool = True
    ) -> None:
        """Answer `get_block_info`, record `marked`/`invalidated` calls.

        `accepts_headers` is what `add_headers` answers with for a
        header naming a hash `infos` does not already carry: `True`
        indexes it (`get_block_info` sees it from then on), `False`
        answers `None`, `BlockIndex.add_headers`'s own answer for a
        single header whose own parent this index does not know either
        -- btclib-org/btclib-node#711's own case.
        """
        self.infos = infos
        self.header_dict = infos
        self.marked: list[bytes] = []
        self.invalidated: list[bytes] = []
        self.accepts_headers = accepts_headers
        self.added_headers: list[BlockHeader] = []

    def get_block_info(self, block_hash: bytes) -> Any:
        """Return the fixed info this block hash was constructed with."""
        return self.infos[block_hash]

    def set_downloaded(self, block_hash: bytes) -> None:
        """Record that this block hash was marked downloaded."""
        self.marked.append(block_hash)

    def invalidate(self, block_hash: bytes) -> None:
        """Record that this block hash was invalidated."""
        self.invalidated.append(block_hash)

    def add_headers(self, headers: list[BlockHeader]) -> bytes | None:
        """Index the one header `block` ever calls this with, or refuse it.

        `BlockIndex.add_headers`'s own contract for a single header:
        the hash it just indexed, or `None` for a header this stand-in
        was built to refuse.
        """
        self.added_headers.extend(headers)
        (header,) = headers
        if not self.accepts_headers:
            return None
        self.infos[header.hash] = SimpleNamespace(downloaded=False)
        return header.hash


def a_block() -> Block:
    """Build one valid block extending regtest's genesis."""
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    return block


def test_a_block_that_was_asked_for_is_stored_and_marked_downloaded() -> None:
    """A block matching a pending download-queue entry is stored and marked.

    The peer's own bookkeeping clears too: the hash leaves
    `download_queue`, `last_block_timestamp` advances, and
    `pending_eviction` drops, since the block this peer was about to be
    evicted for being slow on has now arrived.
    """
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
    """A block already downloaded is a no-op: not re-added, not re-marked."""
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
    """Rebuild `block` with `bits` set past regtest's own proof-of-work limit.

    regtest's limit is 7fffff00..., and a target has 32 octets to fit
    in: 800000... is the next one up that still does.
    """
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
    """A block failing proof of work is invalidated, not stored, and re-raised.

    The raise still reaches main.handle_p2p, which drops the peer; invalidate is
    what keeps the next one from being asked to send the same block again:
    btclib-org/btclib-node#77.
    """
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


def test_an_unsolicited_block_extending_a_known_parent_is_indexed_and_stored() -> None:
    """A block this node never separately indexed is not a `KeyError`.

    `a_block()` extends regtest's own genesis, so its own header's
    parent is known even though this test's own `FakeBlockIndex` starts
    with nothing in it -- the shape a block arrives in unannounced by a
    prior `headers` round, over `inv`/`getdata` alone, or the exact
    reproduction issue #711 itself carries: before the fix, `block`
    called `get_block_info` on a hash this stand-in had no entry for at
    all, `KeyError` and not `BTClibValueError` -- an exception `main.
    handle_p2p`'s own `except Exception` still catches, but never
    discourages the peer for, `isinstance(e, BTClibException)` being
    `False` for it. This is the accepting half of #711's fix; the
    refusing half is
    `test_an_unsolicited_block_with_an_unknown_parent_is_refused` below.
    """
    block = a_block()
    index = FakeBlockIndex({})
    added: list[Block] = []
    node = a_data_node(
        block_index=index, block_db=SimpleNamespace(add_block=added.append)
    )
    payload = BlockMsg(block, include_witness=True, check_validity=False).serialize(
        check_validity=False
    )
    block_callback(node, payload, a_peer())
    assert added == [block]
    assert index.marked == [block.header.hash]
    assert index.invalidated == []
    assert [header.hash for header in index.added_headers] == [block.header.hash]


def test_an_unsolicited_block_with_an_unknown_parent_is_refused() -> None:
    """A block naming a parent this node has never heard of is not a `KeyError`.

    Core's own `AcceptBlockHeader` (`validation.cpp`, at
    bitcoin/bitcoin@ca7162cde5) refuses exactly this shape with
    `BLOCK_MISSING_PREV`, and `MaybePunishNodeForBlock`
    (`net_processing.cpp`, same sha) calls `Misbehaving` for it --
    `BTClibValueError` here is what `main.handle_p2p`'s own `except`
    reads the same way, discouraging and dropping the peer
    (`test_a_callback_that_raises_drops_the_peer`, `p2p/main_test.py`,
    already covers that mechanics generically). Before the fix this
    raised `KeyError` instead, which is not discouraged.
    btclib-org/btclib-node#711
    """
    (orphan,) = generate_random_chain(1, b"\x11" * 32)
    index = FakeBlockIndex({}, accepts_headers=False)
    added: list[Block] = []
    node = a_data_node(
        block_index=index, block_db=SimpleNamespace(add_block=added.append)
    )
    payload = BlockMsg(orphan, include_witness=True, check_validity=False).serialize(
        check_validity=False
    )
    with pytest.raises(BTClibValueError):
        block_callback(node, payload, a_peer())
    assert added == []
    assert index.marked == []
    assert orphan.header.hash not in index.infos


def test_an_inventory_is_ignored_until_the_blocks_are_synced() -> None:
    """An `inv` before `BlockSynced` is ignored: nothing is asked for it yet."""
    node = a_data_node(status=NodeStatus.HeaderSynced)
    peer = a_peer()
    inv(node, Inv([Inventory(InventoryType.MSG_BLOCK, b"\x11" * 32)]).serialize(), peer)
    assert not peer.sent


def test_a_block_announced_is_answered_with_a_getheaders() -> None:
    """An `inv` naming blocks gets `getheaders` stopping at the last one.

    The last one announced: the headers between are what we are after.
    """
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
    """A `wtx` `inv` for a transaction not held is queued to be asked for.

    Queued onto `download_manager.inv_txs` keyed by the announcing peer's id,
    and no answer sent directly -- the download manager decides who to ask.
    """
    transaction = a_transaction()
    node = a_data_node()
    peer = a_peer(id=4)
    items = [Inventory(InventoryType.MSG_WTX, transaction.hash)]
    inv(node, Inv(items).serialize(), peer)
    assert node.download_manager.inv_txs == [(4, transaction.hash)]
    assert not peer.sent


def test_a_transaction_announced_that_we_hold_is_not_wanted() -> None:
    """A `wtx` `inv` for a transaction already in the mempool is not queued."""
    transaction = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(transaction)
    node = a_data_node(mempool=mempool)
    items = [Inventory(InventoryType.MSG_WTX, transaction.hash)]
    inv(node, Inv(items).serialize(), a_peer(id=4))
    assert node.download_manager.inv_txs == []


def test_a_transaction_this_node_holds_is_served() -> None:
    """A `getdata` for a held transaction is served, witness included as asked.

    Which identifier the peer asked by, and whether the answer carries the
    witness, are two different questions and the codes answer both.
    """
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
    """A `getdata` naming the wrong identifier for a held tx gets `notfound`.

    `MSG_TX` by wtxid, and `MSG_WTX` by txid: the mempool is indexed on the
    identifier each type actually names, not on either interchangeably.
    """
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
    """A `getdata` for a transaction this node never held gets `notfound`.

    Core's own answer to a `getdata` `FindTxForGetData` cannot serve:
    `vNotFound` in `ProcessGetData`, src/net_processing.cpp.
    """
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
    """One `getdata` naming a hit and misses answers both, misses batched.

    The held transaction is sent directly and the misses collect into a single
    `notfound`, rather than one `notfound` per missing item.
    """
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
    """A peer with `relay_tx` false gets no answer to a `getdata` for a tx.

    Every code it could ask by, because gating one of the three is a peer that
    gets the same answer by asking a different way -- not even `notfound`,
    matching Core's own silence on this path.
    """
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
    """A peer with `relay_tx` false still gets a block it asked for, but no tx.

    One getdata carrying both kinds, so the assertion is that the
    answer is the block and nothing beside it: relay declined only
    gates transactions, never blocks.
    """
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
    """A `getdata` for a held block is served, witness included when asked."""
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
    """A `getdata` for a block this node lacks gets silence, matching Core."""
    node = a_data_node(block_db=SimpleNamespace(get_block=lambda h: None))
    peer = a_peer()
    items = [Inventory(InventoryType.MSG_BLOCK, b"\x11" * 32)]
    getdata(node, GetData(items).serialize(), peer)
    assert not peer.sent


def a_tall_block_index(length: int) -> Any:
    """Build a `block_index` double `length` blocks tall, height by hash."""
    active_chain = [height.to_bytes(32, "big") for height in range(length)]
    header_dict = {
        block_hash: SimpleNamespace(index=height)
        for height, block_hash in enumerate(active_chain)
    }
    return SimpleNamespace(active_chain=active_chain, header_dict=header_dict)


def test_a_pruned_node_disconnects_a_getdata_below_its_own_retained_depth() -> None:
    """A pruned node disconnects rather than silently drops a stale request.

    Core's own `ProcessGetBlockData` (`net_processing.cpp`, at
    bitcoin/bitcoin@ca7162cde5): "Avoid leaking prune-height by never
    sending blocks below the NODE_NETWORK_LIMITED threshold" -- fired on
    height alone, whether or not this node still happens to hold the
    block asked for, which is why `get_block` here still answers one.
    """
    block_index = a_tall_block_index(MIN_BLOCKS_TO_KEEP + 10)
    node = a_data_node(
        block_index=block_index, block_db=SimpleNamespace(get_block=lambda h: a_block())
    )
    node.config.pruned = True
    peer = a_peer()
    old_hash = block_index.active_chain[0]
    items = [Inventory(InventoryType.MSG_BLOCK, old_hash)]
    getdata(node, GetData(items).serialize(), peer)
    assert not peer.sent
    assert peer.stopped == [True]


def test_a_pruned_node_serves_a_block_within_its_own_retained_depth() -> None:
    """A pruned node still answers a `getdata` inside `MIN_BLOCKS_TO_KEEP`."""
    block_index = a_tall_block_index(MIN_BLOCKS_TO_KEEP + 10)
    block = a_block()
    node = a_data_node(
        block_index=block_index, block_db=SimpleNamespace(get_block=lambda h: block)
    )
    node.config.pruned = True
    peer = a_peer()
    recent_hash = block_index.active_chain[-1]
    items = [Inventory(InventoryType.MSG_BLOCK, recent_hash)]
    getdata(node, GetData(items).serialize(), peer)
    (answer,) = peer.sent
    assert isinstance(answer, BlockMsg)
    assert not peer.stopped


def test_an_unpruned_node_never_disconnects_over_a_stale_getdata() -> None:
    """`Config.pruned=False` never reaches `_below_prune_threshold` at all."""
    block_index = a_tall_block_index(MIN_BLOCKS_TO_KEEP + 10)
    node = a_data_node(
        block_index=block_index, block_db=SimpleNamespace(get_block=lambda h: None)
    )
    peer = a_peer()
    old_hash = block_index.active_chain[0]
    items = [Inventory(InventoryType.MSG_BLOCK, old_hash)]
    getdata(node, GetData(items).serialize(), peer)
    assert not peer.sent
    assert not peer.stopped


def test_a_pruned_node_ignores_a_hash_it_has_never_indexed() -> None:
    """A hash the block index has never seen is silence, not a disconnect.

    Core's own `!pindex: return;`, immediately ahead of the check
    `_below_prune_threshold` mirrors.
    """
    block_index = a_tall_block_index(MIN_BLOCKS_TO_KEEP + 10)
    node = a_data_node(
        block_index=block_index, block_db=SimpleNamespace(get_block=lambda h: None)
    )
    node.config.pruned = True
    peer = a_peer()
    items = [Inventory(InventoryType.MSG_BLOCK, b"\xff" * 32)]
    getdata(node, GetData(items).serialize(), peer)
    assert not peer.sent
    assert not peer.stopped


def test_a_pruned_node_s_own_threshold_is_strictly_greater_than_the_buffer() -> None:
    """`MIN_BLOCKS_TO_KEEP + 2` behind the tip is still served, one past is not.

    Core's own check is `>`, not `>=` (`ProcessGetBlockData`, at
    bitcoin/bitcoin@ca7162cde5), so a block sitting exactly at the "+ 2 for
    possible races" buffer is inside it rather than past it.
    """
    length = MIN_BLOCKS_TO_KEEP + 10
    block_index = a_tall_block_index(length)
    block = a_block()
    node = a_data_node(
        block_index=block_index, block_db=SimpleNamespace(get_block=lambda h: block)
    )
    node.config.pruned = True
    tip_index = length - 1

    peer = a_peer()
    at_buffer_hash = block_index.active_chain[tip_index - (MIN_BLOCKS_TO_KEEP + 2)]
    getdata(
        node,
        GetData([Inventory(InventoryType.MSG_BLOCK, at_buffer_hash)]).serialize(),
        peer,
    )
    assert peer.sent
    assert not peer.stopped

    peer = a_peer()
    past_buffer_hash = block_index.active_chain[tip_index - (MIN_BLOCKS_TO_KEEP + 3)]
    getdata(
        node,
        GetData([Inventory(InventoryType.MSG_BLOCK, past_buffer_hash)]).serialize(),
        peer,
    )
    assert not peer.sent
    assert peer.stopped == [True]


def test_an_inventory_of_neither_kind_is_skipped() -> None:
    """A `getdata` item neither a tx type nor a block type is skipped."""
    node = a_data_node(block_db=SimpleNamespace(get_block=lambda h: None))
    peer = a_peer()
    items = [Inventory(InventoryType.MSG_FILTERED_BLOCK, b"\x11" * 32)]
    getdata(node, GetData(items).serialize(), peer)
    assert not peer.sent


def test_getdata_pauses_once_the_queue_is_full_and_registers_the_rest() -> None:
    """`getdata` stops serving once `conn` is at its pacing bound.

    Nothing is sent -- the peer was already at the bound before this
    request arrived -- and the item is left on `node.pending_getdata`,
    keyed by the connection's own id, for `p2p.main.resume_getdata` to
    pick up later.
    """
    transaction = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(transaction)
    node = a_data_node(mempool=mempool)
    peer = a_peer(queued_send_bytes=MAX_GETDATA_INFLIGHT_BYTES)
    item = Inventory(InventoryType.MSG_WTX, transaction.hash)
    getdata(node, GetData([item]).serialize(), peer)
    assert not peer.sent
    conn, items = node.pending_getdata[peer.id]
    assert conn is peer
    assert list(items) == [item]


def test_a_paused_getdata_answer_resumes_once_the_queue_drains() -> None:
    """Calling `advance_getdata` again once the queue drains finishes it.

    The same call `p2p.main.resume_getdata` makes on a later pass of
    `Node`'s own loop, driven directly here: what is owed to this
    module is that it picks the paused request up correctly, not the
    polling loop around it, which `tests/unit/p2p/main_test.py` already
    covers.
    """
    transaction = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(transaction)
    node = a_data_node(mempool=mempool)
    peer = a_peer(queued_send_bytes=MAX_GETDATA_INFLIGHT_BYTES)
    item = Inventory(InventoryType.MSG_WTX, transaction.hash)
    getdata(node, GetData([item]).serialize(), peer)
    assert not peer.sent
    _conn, items = node.pending_getdata[peer.id]

    peer.queued_send_bytes = 0
    assert advance_getdata(node, peer, items) is True
    assert not items
    (answer,) = peer.sent
    assert isinstance(answer, TxMsg)
    assert answer.tx == transaction


def test_a_second_getdata_while_the_first_is_still_paused_is_not_lost() -> None:
    """A second `getdata` arriving while the first is paused extends it.

    Neither request is dropped: both are served in full, in the order
    the two arrived, once the connection's own queue drains -- the same
    rule `get_cfilters`'s own pending range follows.
    """
    first = a_transaction()
    second = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(first)
    mempool.add_tx(second)
    node = a_data_node(mempool=mempool)
    peer = a_peer(queued_send_bytes=MAX_GETDATA_INFLIGHT_BYTES)
    item1 = Inventory(InventoryType.MSG_WTX, first.hash)
    item2 = Inventory(InventoryType.MSG_WTX, second.hash)
    getdata(node, GetData([item1]).serialize(), peer)
    assert not peer.sent
    getdata(node, GetData([item2]).serialize(), peer)
    _conn, items = node.pending_getdata[peer.id]
    assert list(items) == [item1, item2]

    peer.queued_send_bytes = 0
    assert advance_getdata(node, peer, items) is True
    assert [msg.tx for msg in peer.sent] == [first, second]


def test_getdata_notfound_covers_only_what_a_call_actually_served() -> None:
    """`notfound` batches misses served this call, not ones still pending.

    A miss found before the pacing bound trips is reported; an item
    never reached because the bound tripped first is left on
    `node.pending_getdata` instead, unreported until a later call
    actually gets to it -- matching Core's own `vNotFound`, built fresh
    by every `ProcessGetData` call rather than carried across them.
    """
    held = a_transaction()
    mempool = Mempool(Logger(debug=True))
    mempool.add_tx(held)
    node = a_data_node(mempool=mempool)
    missing = Inventory(InventoryType.MSG_TX, b"\x11" * 32)
    hit = Inventory(InventoryType.MSG_WTX, held.hash)
    never_reached = Inventory(InventoryType.MSG_TX, b"\x22" * 32)

    peer = a_peer(queued_send_bytes=0)
    sent = peer.sent

    def send_then_fill(msg: Any) -> None:
        sent.append(msg)
        # stands in for what `Connection.send` would actually do: this
        # send is what fills the connection's own queue up to the bound,
        # tripping the pause before `never_reached` is looked at
        peer.queued_send_bytes = MAX_GETDATA_INFLIGHT_BYTES

    peer.send = send_then_fill
    getdata(node, GetData([missing, hit, never_reached]).serialize(), peer)

    tx_answer, notfound_answer = peer.sent
    assert isinstance(tx_answer, TxMsg)
    assert isinstance(notfound_answer, NotFound)
    assert notfound_answer.items == (missing,)
    _conn, items = node.pending_getdata[peer.id]
    assert list(items) == [never_reached]


def test_getdata_stops_sending_once_the_connection_closes_mid_answer() -> None:
    """`getdata` stops serving once the peer's own connection has closed.

    The same shape `get_cfilters`'s own pacing already has: `conn.status`
    turns `P2pConnStatus.Closed` partway through the request, and
    nothing further in it is worth serializing -- and a connection
    found closed is dropped rather than parked, nothing more ever being
    owed to it.
    """
    blocks = [a_block() for _ in range(4)]
    lookup = {b.header.hash: b for b in blocks}
    node = a_data_node(block_db=SimpleNamespace(get_block=lookup.get))
    peer = a_peer()
    sent = peer.sent

    def send_then_close(msg: Any) -> None:
        sent.append(msg)
        if len(sent) == 2:
            peer.status = P2pConnStatus.Closed

    peer.send = send_then_close
    items = [Inventory(InventoryType.MSG_BLOCK, b.header.hash) for b in blocks]
    getdata(node, GetData(items).serialize(), peer)
    assert len(peer.sent) == 2
    assert peer.id not in node.pending_getdata


def test_a_getdata_past_the_pending_cap_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A third request stacked past `MAX_PENDING_GETDATA_ITEMS` is silent.

    The same answer `get_cfilters` already gives a request past its own
    `MAX_PENDING_CFILTERS_HEIGHTS`, and `getdata`'s own docstring is
    where the reasoning behind it, and Core's own different one, are
    argued.

    `MAX_PENDING_GETDATA_ITEMS` is `2 * MAX_INV_SZ` -- fifty thousand
    apiece, where `get_cfilters`'s own cap is two full requests of
    `MAX_GETCFILTERS_SIZE` (one thousand) -- so this test monkeypatches
    it down rather than actually building on the order of a hundred
    thousand `Inventory` entries to reach the same branch.
    """
    monkeypatch.setattr(cb, "MAX_PENDING_GETDATA_ITEMS", 4)
    node = a_data_node(block_db=SimpleNamespace(get_block=lambda h: None))
    peer = a_peer(queued_send_bytes=MAX_GETDATA_INFLIGHT_BYTES)
    hashes = [bytes([i]) * 32 for i in range(5)]
    first = [Inventory(InventoryType.MSG_BLOCK, h) for h in hashes[:2]]
    second = [Inventory(InventoryType.MSG_BLOCK, h) for h in hashes[2:4]]
    getdata(node, GetData(first).serialize(), peer)
    getdata(node, GetData(second).serialize(), peer)
    _conn, items = node.pending_getdata[peer.id]
    assert len(items) == 4

    third = [Inventory(InventoryType.MSG_BLOCK, hashes[4])]
    getdata(node, GetData(third).serialize(), peer)
    assert not peer.sent
    _conn, items = node.pending_getdata[peer.id]
    assert len(items) == 4


class FakeHeaderIndex:
    """A block index stand-in with fixed `add_headers` answer and tip status."""

    def __init__(
        self,
        tip: bytes | None = None,
        *,
        refuse: bool = False,
        header_index_tip: bytes = b"\xff" * 32,
        tip_status: BlockStatus = BlockStatus.valid_header,
    ) -> None:
        """Fix `add_headers`'s return, whether it raises, and the tip."""
        self.tip = tip
        self.refuse = refuse
        self.header_index = [header_index_tip]
        self.tip_status = tip_status
        self.given: list[BlockHeader] | None = None

    def add_headers(self, headers: Iterable[BlockHeader]) -> bytes | None:
        """Record the headers given, then answer `tip` or raise if `refuse`."""
        self.given = list(headers)
        if self.refuse:
            err_msg = "a header failing on its own terms"
            raise BTClibValueError(err_msg)
        return self.tip

    def get_block_info(self, block_hash: bytes) -> SimpleNamespace:
        """Answer every hash with the same fixed `tip_status`, and index 0.

        `index` is never asserted against by a test built on this double
        -- `headers`'s own `conn.best_known_height` update
        (btclib-org/btclib-node#706) is the only reader, and every test
        here only checks `Connection.sent`/`node.status`, not the height
        that update leaves behind.
        """
        return SimpleNamespace(status=self.tip_status, index=0)

    def get_block_locator_hashes(self) -> list[bytes]:
        """Return the one fixed locator hash this stand-in ever answers with."""
        return [b"\x00" * 32]


def test_a_full_batch_extending_the_best_chain_uses_the_usual_locator() -> None:
    """A full batch extending the best chain gets the ordinary locator.

    header_index already reaches an ordinary batch's own tip -- #122 is about a
    fork below it, not this case -- so nothing here should narrow the richer,
    multi-entry locator to a single hash.
    """
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
    """A full batch on a fork below `header_index`'s tip asks from that tip.

    header_index does not move for a fork arriving below its own tip, so its own
    locator would ask for this same batch again: btclib-org/btclib-node#122.
    """
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
    """A full batch on an already-invalid fork falls back to the usual locator.

    A batch built on a parent this node already proved invalid is a fork by the
    header_index test above, but not one worth asking a peer for more of:
    nothing in this tree scores or bans a peer that keeps sending it, so the
    locator falls back rather than naming that fork's own tip back to it.
    """
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
    """A full batch connecting to nothing known asks from what this node has.

    `add_headers` answers `tip=None` for a batch with no known ancestor, which
    asks with the ordinary locator rather than one built from a tip that was
    never reached.
    """
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
    """A short, unconnecting batch still gets a `getheaders`, not silence.

    A short batch is the ordinary shape of a BIP130 announcement, and unlike the
    full-batch case above the pre-existing code never sent anything for it: the
    `len(headers) == 2000` guard was the only place a follow-up GetHeaders was
    built. btclib-org/btclib-node#233
    """
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
    """A batch on an already-invalid parent falls back to the usual locator.

    add_headers has no reason to refuse this batch -- every header in it still
    passes its own checks on its own terms, invalid parent or not -- so avoiding
    a request for more of a branch this node has already proved bad is
    callbacks.headers's own contract, proved here through the real BlockIndex
    and not a fake standing in for it. btclib-org/btclib-node#122
    """
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
    """A batch `add_headers` refuses re-raises, not the ordinary end of a sync.

    A batch refused for a bad proof of work is a misbehaving peer, not the
    ordinary end of a sync: the raise reaches handle_p2p, which drops the
    connection instead of this node believing itself caught up. btclib-
    org/btclib-node#75
    """
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
    """A connecting batch shorter than a full one moves sync to `HeaderSynced`.

    Shorter than `MAX_HEADERS_RESULTS` and still connecting is the peer
    signalling it has nothing more to give.
    """
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    node = a_data_node(status=NodeStatus.SyncingHeaders)
    node.chainstate.block_index = FakeHeaderIndex(tip=chain[-1].hash)
    peer = a_peer()
    headers(node, Headers(chain).serialize(), peer)
    assert not peer.sent
    assert node.status == NodeStatus.HeaderSynced


def test_a_short_batch_when_the_headers_are_already_synced_changes_nothing() -> None:
    """A short connecting batch once headers are already synced changes nothing.

    Only `SyncingHeaders` moves to `HeaderSynced`; a batch arriving once the
    node is already past that is not a status change.
    """
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    node = a_data_node(status=NodeStatus.BlockSynced)
    node.chainstate.block_index = FakeHeaderIndex(tip=chain[-1].hash)
    peer = a_peer()
    headers(node, Headers(chain).serialize(), peer)
    assert not peer.sent
    assert node.status == NodeStatus.BlockSynced


def test_this_node_answers_a_getheaders_from_what_it_knows() -> None:
    """A `getheaders` reaches `get_headers_from_locators` unchanged.

    The peer's question reaches the index as the peer asked it, which a locator
    and a stop of the same value could not tell apart.
    """
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
    getheaders(node, GetHeaders(PROTOCOL_VERSION, locator, stop).serialize(), peer)
    # the peer's question reaches the index as the peer asked it, which
    # a locator and a stop of the same value could not tell
    assert asked == [(locator, stop)]
    (sent,) = peer.sent
    assert isinstance(sent, Headers)
    assert list(sent.headers) == chain


def test_a_getheaders_this_node_cannot_answer_is_not_answered() -> None:
    """A `getheaders` resolving to nothing gets no answer, not a refusal."""
    node = a_data_node()
    node.chainstate.block_index = SimpleNamespace(
        get_headers_from_locators=lambda locator, stop: []
    )
    peer = a_peer()
    getheaders(
        node,
        GetHeaders(PROTOCOL_VERSION, [b"\x11" * 32], b"\x00" * 32).serialize(),
        peer,
    )
    assert not peer.sent


def a_filter_hash(height: int) -> bytes:
    """Return the made-up filter hash the stand-in answers for `height`."""
    return (height + 1).to_bytes(32, "big")


def a_filters_node(
    length: int = 8, *, stale: Mapping[bytes, Any] | Iterable[Any] = ()
) -> Any:
    """Build a node whose chain is `length` blocks, each with a canned filter.

    The filters are made up: what is this node's in BIP157 is which
    blocks a range names and what is refused, and a real filter would
    say nothing about either. `tests/unit/chainstate/filter_index_test.py`
    is where the filters themselves are tested.

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
        # written by `get_cfilters` only where `advance_cfilters` pauses;
        # empty here for every test that never trips that pacing bound
        pending_cfilters={},
    )


def a_getcfilters(
    node: Any,
    peer: Any,
    start: int,
    stop_height: int,
    filter_type: BlockFilterType = BlockFilterType.BASIC,
) -> None:
    """Drive `get_cfilters` for a height range on `node`'s active chain."""
    stop_hash = node.chainstate.block_index.active_chain[stop_height]
    get_cfilters(node, GetCFilters(filter_type, start, stop_hash).serialize(), peer)


def test_a_range_of_filters_is_answered_one_message_per_block() -> None:
    """A `getcfilters` range gets one `cfilter` per block, in height order.

    "sequentially in order by block height", and the whole range
    including both ends.
    """
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
    """A `getcfilters` naming one block at both ends answers with one filter."""
    node = a_filters_node()
    peer = a_peer()
    a_getcfilters(node, peer, 3, 3)
    (msg,) = peer.sent
    assert msg.block_hash == (3).to_bytes(32, "big")


def test_get_cfilters_pauses_once_the_queue_is_full_and_registers_the_rest() -> None:
    """`get_cfilters` stops scheduling once `conn` is at its pacing bound.

    Nothing is sent -- the peer was already at the bound before this
    request arrived -- and every height is left on `node.pending_cfilters`,
    keyed by the connection's own id, for `p2p.main.resume_cfilters` to
    pick up later.
    """
    node = a_filters_node(length=8)
    peer = a_peer(queued_send_bytes=MAX_CFILTERS_INFLIGHT_BYTES)
    a_getcfilters(node, peer, 2, 5)
    assert not peer.sent
    conn, heights = node.pending_cfilters[peer.id]
    assert conn is peer
    assert list(heights) == [2, 3, 4, 5]


def test_a_paused_answer_resumes_once_the_queue_drains() -> None:
    """Calling `advance_cfilters` again once the queue drains finishes it.

    The same call `p2p.main.resume_cfilters` makes on a later pass of
    `Node`'s own loop, driven directly here: what is owed to this module
    is that it picks the paused range up correctly, not the polling
    loop around it, which `tests/unit/p2p/main_test.py` already covers.
    """
    node = a_filters_node(length=8)
    peer = a_peer(queued_send_bytes=MAX_CFILTERS_INFLIGHT_BYTES)
    a_getcfilters(node, peer, 2, 5)
    assert not peer.sent
    _conn, heights = node.pending_cfilters[peer.id]

    peer.queued_send_bytes = 0
    assert advance_cfilters(node, peer, heights) is True
    assert not heights
    assert [msg.block_hash for msg in peer.sent] == [
        h.to_bytes(32, "big") for h in range(2, 6)
    ]


def test_a_second_getcfilters_while_the_first_is_still_paused_is_not_lost() -> None:
    """A second `getcfilters` arriving while the first is paused extends it.

    Neither range is dropped: both are answered in full, in the order
    the two requests arrived, once the connection's own queue drains --
    rather than the second overwriting `node.pending_cfilters`'s entry
    for this connection and discarding the first range's own remaining
    heights, which is what a plain assignment there used to do.
    """
    node = a_filters_node(length=20)
    peer = a_peer(queued_send_bytes=MAX_CFILTERS_INFLIGHT_BYTES)
    a_getcfilters(node, peer, 0, 5)
    assert not peer.sent
    a_getcfilters(node, peer, 10, 12)
    _conn, heights = node.pending_cfilters[peer.id]
    assert list(heights) == [0, 1, 2, 3, 4, 5, 10, 11, 12]

    peer.queued_send_bytes = 0
    assert advance_cfilters(node, peer, heights) is True
    assert [msg.block_hash for msg in peer.sent] == [
        h.to_bytes(32, "big") for h in (0, 1, 2, 3, 4, 5, 10, 11, 12)
    ]


def test_a_getcfilters_past_the_pending_cap_is_silent() -> None:
    """A third request stacked past `MAX_PENDING_CFILTERS_HEIGHTS` is silent.

    Two requests of `MAX_GETCFILTERS_SIZE` heights apiece -- `_filter_range`'s
    own bound on any one of them -- already reach the cap between them; a
    third is refused whole rather than partially extending it.

    `_filter_range` already answers a request it will not serve with
    silence rather than an error -- an unknown filter type, an unknown
    stop hash, a range too long -- and a peer pipelining past what this
    connection still extends for is the same kind of request this node
    will not serve, for lack of a defined refusal message BIP157 leaves
    it to send instead.
    """
    node = a_filters_node(length=MAX_PENDING_CFILTERS_HEIGHTS + 20)
    peer = a_peer(queued_send_bytes=MAX_CFILTERS_INFLIGHT_BYTES)
    a_getcfilters(node, peer, 0, MAX_GETCFILTERS_SIZE - 1)
    a_getcfilters(node, peer, MAX_GETCFILTERS_SIZE, MAX_PENDING_CFILTERS_HEIGHTS - 1)
    _conn, heights = node.pending_cfilters[peer.id]
    assert len(heights) == MAX_PENDING_CFILTERS_HEIGHTS

    a_getcfilters(
        node,
        peer,
        MAX_PENDING_CFILTERS_HEIGHTS,
        MAX_PENDING_CFILTERS_HEIGHTS,
    )
    assert not peer.sent
    _conn, heights = node.pending_cfilters[peer.id]
    assert len(heights) == MAX_PENDING_CFILTERS_HEIGHTS


def test_get_cfilters_stops_once_the_connection_closes_mid_answer() -> None:
    """`get_cfilters` stops sending once the peer's own connection has closed.

    What Connection.send's own send-buffer bound (#101) looks like
    from here: conn.status turns P2pConnStatus.Closed partway through
    the range, and nothing further in it is worth serializing.
    """
    # what Connection.send's own send-buffer bound (#101) looks like
    # from here: conn.status turns P2pConnStatus.Closed partway through
    # the range, and nothing further in it is worth serializing
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
    """A missing filter mid-range raises, rather than answering `notfound`.

    BIP157's service bit promises a filter for every block of the active chain;
    a gap here is the index breaking that promise rather than a request this
    node can decline.
    """
    # BIP157's service bit promises a filter for every block of the
    # active chain; a gap here is the index breaking that promise
    # rather than a request this node can decline
    node = a_filters_node()
    node.chainstate.filter_index.get_filter = lambda h: None
    peer = a_peer()
    with pytest.raises(ChainstateInconsistencyError, match="no filter for a block"):
        a_getcfilters(node, peer, 2, 2)


def test_a_filter_type_this_node_does_not_serve_is_not_answered() -> None:
    """A `getcfilters` naming an unserved filter type gets no answer.

    BIP158 defines the basic filter and nothing else, so any other code is a
    type no node has; BIP157 says answer with nothing.
    """
    # BIP158 defines the basic filter and nothing else, so any other
    # code is a type no node has; BIP157 says answer with nothing
    node = a_filters_node()
    peer = a_peer()
    a_getcfilters(node, peer, 0, 1, filter_type=cast("BlockFilterType", 1))
    assert not peer.sent


def test_a_stop_hash_this_node_never_heard_of_is_not_answered() -> None:
    """A `getcfilters` naming an unknown stop hash gets no answer."""
    node = a_filters_node()
    peer = a_peer()
    get_cfilters(
        node, GetCFilters(BlockFilterType.BASIC, 0, b"\x11" * 32).serialize(), peer
    )
    assert not peer.sent


def test_a_stop_hash_off_the_active_chain_is_not_answered() -> None:
    """A `getcfilters` naming a stop hash off the active chain gets no answer.

    A block this node knows and did not keep: its height is a height
    on the branch it left, and answering would send the filters of
    blocks the peer did not ask about.
    """
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
    """A `getcfilters` naming a stop past the chain's tip is not answered."""
    node = a_filters_node(length=4, stale={b"\x33" * 32: SimpleNamespace(index=9)})
    peer = a_peer()
    get_cfilters(
        node, GetCFilters(BlockFilterType.BASIC, 0, b"\x33" * 32).serialize(), peer
    )
    assert not peer.sent


def test_a_range_that_runs_backwards_is_not_answered() -> None:
    """A range whose start is past its stop gets nothing, from either message.

    And the same range asked of getcfheaders, which is the half that can tell:
    an empty range sends no cfilter either way, where a cfheaders of no hashes
    is a message the peer would have to read.
    """
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
    """A range one block short of the limit is answered; exactly at it is not.

    BIP157 bounds the difference and bounds it strictly, so a range
    whose ends differ by exactly the limit is one block too many.
    """
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
    """A `getcfheaders` answer carries the range's hashes plus one header.

    The header of the block before the range: what the hashes below chain onto,
    and without it a client could check nothing.
    """
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
    """A `getcfheaders` range at genesis answers thirty-two zero octets.

    BIP157 defines the header before the genesis block's filter as thirty-two
    zero octets, and there is no block to read one off.
    """
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
    """A `getcfheaders` naming an unknown stop hash gets no answer."""
    node = a_filters_node()
    peer = a_peer()
    get_cfheaders(
        node, GetCFHeaders(BlockFilterType.BASIC, 0, b"\x11" * 32).serialize(), peer
    )
    assert not peer.sent


def test_get_cfheaders_refuses_a_gap_in_the_header_before_the_range() -> None:
    """A missing filter header before the range raises, not `notfound`."""
    node = a_filters_node()
    node.chainstate.filter_index.get_header = lambda h: None
    peer = a_peer()
    with pytest.raises(
        ChainstateInconsistencyError, match="no filter header for the parent"
    ):
        get_cfheaders(
            node,
            GetCFHeaders(BlockFilterType.BASIC, 3, (5).to_bytes(32, "big")).serialize(),
            peer,
        )


def test_get_cfheaders_refuses_a_gap_in_a_promised_index() -> None:
    """A missing filter hash in the range raises, not a `notfound`."""
    node = a_filters_node()
    node.chainstate.filter_index.get_filter_hash = lambda h: None
    peer = a_peer()
    with pytest.raises(ChainstateInconsistencyError, match="no filter for a block"):
        get_cfheaders(
            node,
            GetCFHeaders(BlockFilterType.BASIC, 0, (2).to_bytes(32, "big")).serialize(),
            peer,
        )


def test_the_checkpoints_are_every_thousandth_block_and_not_the_first() -> None:
    """`getcfcheckpt` answers with the header at every interval to the stop.

    "a multiple of 1,000 greater than 0": the genesis block is not a checkpoint,
    and the stop block is one only if it falls on the interval itself.
    """
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
    """A stop block whose height falls on the interval is itself a checkpoint.

    The boundary the rule is most specific about: "each block ... where the
    block height is a multiple of 1,000 greater than 0" includes the block the
    range terminates at, when that is what its height is.
    """
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
    """A chain shorter than the interval answers with an empty message.

    An answer, and an empty one: a client that asked has been told there is
    nothing to check against, which is not the same as having been ignored.
    """
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
    """A missing filter header at a checkpoint raises, not `notfound`."""
    node = a_filters_node(length=CFCHECKPT_INTERVAL + 1)
    node.chainstate.filter_index.get_header = lambda h: None
    peer = a_peer()
    stop_hash = CFCHECKPT_INTERVAL.to_bytes(32, "big")
    with pytest.raises(
        ChainstateInconsistencyError, match="no filter header for a block"
    ):
        get_cfcheckpt(
            node, GetCFCheckpt(BlockFilterType.BASIC, stop_hash).serialize(), peer
        )


def test_a_getcfcheckpt_this_node_cannot_answer_is_not_answered() -> None:
    """A `getcfcheckpt` this node cannot answer for any reason gets no answer.

    Three different reasons in one test: a stop hash never heard of, one
    off the active chain, and a filter type nobody serves -- all silent.
    """
    node = a_filters_node(length=4, stale={b"\x44" * 32: SimpleNamespace(index=2)})
    for stop_hash, filter_type in (
        (b"\x11" * 32, BlockFilterType.BASIC),  # never heard of
        (b"\x44" * 32, BlockFilterType.BASIC),  # off the active chain
        ((1).to_bytes(32, "big"), 1),  # a filter type nobody serves
    ):
        peer = a_peer()
        get_cfcheckpt(node, GetCFCheckpt(filter_type, stop_hash).serialize(), peer)
        assert not peer.sent, stop_hash.hex()
