# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The send-side bound, against a peer on a real socket that stops reading.

Past `Connection.send_buffer_max_size` (`btclib_node/p2p/connection.py`),
`-maxsendbuffer`, a connection sets `pause_send`, Core's `fPauseSend`: a
half-served answer pauses, the peer's later messages wait, and the peer
is not dropped for it. The bound engages only against a peer that stops
draining what it was sent, and a well-behaved daemon always reads:
pointing a bitcoind at this node cannot reach it, so this half of the
question wants a synthetic peer and no daemon at all. The receive-side
half is `tests/integration/backpressure_test.py`, which does want one;
the last test here sets `-maxreceivebuffer` low enough for this peer to
reach it. btclib-org/btclib-node#492

The peer here completes the handshake and then never calls `recv`
again. Nothing it was sent is lost: those octets sit in the two kernel
buffers until the window closes behind them, and what will not fit is
what stands in this node's own `send_memusage`.

The blocks a `getdata` asks for go straight into `block_db` without the
header chain that would ordinarily carry them: `advance_getdata` serves
an item out of that store alone, so connecting them would add block
validation to a fixture whose subject is the send queue. The filter test
below does connect its own blocks, a block's filter being built as it
connects.
"""

import secrets
import socket
import time
from contextlib import contextmanager, suppress
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from btclib.block import (
    Block,
    BlockHeader,
    bip34_commitment,
    merkle_root_and_mutated_from_transactions,
)
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.p2p.block_filters import BlockFilterType, GetCFilters
from btclib.p2p.data import BlockPayload as BlockMsg
from btclib.p2p.handshake import Verack, Version
from btclib.p2p.inventory import GetData, Inventory, InventoryType
from btclib.p2p.keepalive import Ping
from btclib.p2p.limits import PROTOCOL_VERSION
from btclib.p2p.message import Message
from btclib.p2p.negotiation import WtxidRelay
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node, cli
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import (
    DEFAULT_MAXRECEIVEBUFFER,
    DEFAULT_MAXSENDBUFFER,
    NodeStatus,
    P2pConnStatus,
)
from btclib_node.p2p import connection as connection_module
from btclib_node.p2p import manager as manager_module
from btclib_node.p2p.transport import SerializedMessage
from tests import (
    GENESIS_TIME,
    brute_force_nonce,
    get_random_port,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from btclib.p2p.payload import Payload

    from btclib_node.p2p.connection import Connection

# What a regtest coinbase may pay in total: a block paying more than this
# is refused by `update_chain`, which the filtered chain below goes
# through.
_SUBSIDY = 50 * 10**8

# One served block. A megabyte is an ordinary size to be asked for: a
# peer in initial block download asks for `MAX_BLOCKS_IN_TRANSIT_PER_PEER`
# blocks of up to `MAX_PROTOCOL_MESSAGE_LENGTH` (`btclib_node/download.py`),
# which is what this node asks its own peers for.
#
# Just short of a megabyte, not a whole one: `a_block`'s one coinbase
# carries this whole payload in its own output, and `Tx.assert_valid`
# (btclib 2026.9.30) now refuses a transaction whose stripped
# serialization, times `WITNESS_SCALE_FACTOR`, exceeds
# `MAX_BLOCK_WEIGHT` -- Core's own `CheckTransaction`
# `bad-txns-oversize` (`consensus/tx_check.cpp`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which already refused a
# transaction this large (btclib-org/btclib#2417, btclib-org/btclib#2420).
# A full megabyte crossed that bound by the coinbase's own ~100 bytes
# of non-payload fields; this leaves headroom for them at every height
# `blocks_of` below ever reaches.
_SERVED_BLOCK_BYTES = 999_000

# A connection's `send_buffer_max_size` unless `-maxsendbuffer` sets it.
_SEND_BUFFER_MAX_SIZE = 1000 * DEFAULT_MAXSENDBUFFER

# What each end of the connection may hold in its kernel socket buffer,
# set on both ends below. `send_memusage` counts only what the socket
# has not yet taken (`Connection._drain_outbox` subtracts a message once
# `sock_sendall` returns), so octets a buffer swallows never stand
# against `send_buffer_max_size`. Left to the kernel that is several
# megabytes to a peer that never reads -- autotuned, and larger on some
# runners -- which left the pause below unreached on CI. Fixed at this
# size, the two ends together absorb well under
# `_KERNEL_BUFFER_ALLOWANCE`, whatever the platform's default; the
# kernel may round it up, Linux doubling it.
_SOCKET_BUFFER_BYTES = 65_536

# A generous bound on what the two buffers above can hold between them,
# several times their own size, so the pause does not depend on the
# rounding either.
_KERNEL_BUFFER_ALLOWANCE = 4_000_000

# How many blocks put `send_memusage` past `_SEND_BUFFER_MAX_SIZE`
# whatever the two kernel buffers take, with one of margin: the blocks
# `test_a_getdata_answer_pauses_...` below connects, and the ones the
# other tests queue. Connecting more paid for `update_chain`'s own block
# validation over blocks the pause never reaches, slow enough to time out
# under load (btclib-org/btclib-node#1518).
_BLOCKS_PAST_THE_BOUND = (
    _SEND_BUFFER_MAX_SIZE + _KERNEL_BUFFER_ALLOWANCE
) // _SERVED_BLOCK_BYTES + 2

# What the `getdata` below asks for: more than it can serve before the
# pause, the ones past those not on the active chain.
_BLOCKS_ASKED_FOR = 2 * _BLOCKS_PAST_THE_BOUND
_FILTERED_BLOCKS = 4


def a_block(previous_block_hash: bytes, height: int, outputs: list[TxOut]) -> Block:
    """Return a solved regtest block whose one transaction pays `outputs`.

    `height` is the parent's, matching `tests.build_block`'s own
    convention, so the coinbase commits (BIP34) to `height + 1` -- this
    block's own real height, and what regtest enforces from height 1 on
    for the one caller (`blocks_of`, below) that connects what it builds
    rather than serving it straight out of `block_db`.
    """
    coinbase = Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(),
                script_sig=bip34_commitment(height + 1)
                + script.serialize([secrets.token_bytes(32)]),
                sequence=0xFFFFFFFF,
            )
        ],
        vout=outputs,
    )
    header = BlockHeader(
        version=70015,
        previous_block_hash=previous_block_hash,
        merkle_root=merkle_root_and_mutated_from_transactions([coinbase])[0],
        time=GENESIS_TIME + timedelta(seconds=height + 1),
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)
    # the reason `tests.build_block` gives for the same call:
    # `Block.__init__` checks the header against mainnet's pow limit,
    # which no regtest block meets, and `brute_force_nonce` has already
    # checked it against the limit that does apply to it
    return Block(header, [coinbase], check_validity=False)


def blocks_of(count: int, payload_bytes: int) -> list[Block]:
    """Return `count` solved blocks off the genesis, sized by `payload_bytes`.

    Each pays the whole subsidy to one output whose script is that many
    random octets, which is what makes a block as large as a caller
    wants without giving it transactions to validate. One coinbase, not
    several transactions: `update_chain` -- which `test_a_getdata_answer_
    pauses_once_the_send_buffer_is_full` and `test_a_getcfilters_from_a_
    paused_peer_waits_unread` below both
    run a handful of these blocks through, to put them on the active
    chain -- validates a non-coinbase input's prevout against the UTXO
    set, which nothing here ever populates; a coinbase has none to
    check. `_SERVED_BLOCK_BYTES`'s own comment is where this output's
    upper bound comes from.
    """
    chain: list[Block] = []
    previous_block_hash = RegTest().genesis.hash
    for height in range(count):
        output = TxOut(
            value=_SUBSIDY,
            script_pub_key=script.serialize([secrets.token_bytes(payload_bytes)]),
        )
        block = a_block(previous_block_hash, height, [output])
        previous_block_hash = block.header.hash
        chain.append(block)
    return chain


@contextmanager
def a_served_node(
    tmp_path: Path, chain: list[Block], config: Config | None = None
) -> Iterator[Node]:
    """Give a started node holding `chain` in its store, stopped on exit.

    `peerblockfilters=True`: `deaf_peer` below feeds a BIP157 test, and
    `-peerblockfilters` is off by default (ISS 1395). `v1transport=True`:
    the peer below speaks v1 on a raw socket, which a node without it
    drops. A `config` given replaces the whole of this one.
    """
    node = Node(
        config=config
        or Config(
            chain="regtest",
            data_dir=tmp_path,
            p2p_port=get_random_port(),
            allow_rpc=False,
            debug=True,
            peerblockfilters=True,
            v1transport=True,
        )
    )
    node.start()
    try:
        wait_until_listening(node.p2p_manager)
        for block in chain:
            node.block_db.add_block(block)
        yield node
    finally:
        node.stop()


class DeafPeer:
    """A peer that finishes the handshake and then never reads again.

    Sending is all it does afterwards, so everything this node answers
    stays in the socket -- the state the send-side bound is about, and
    the one no daemon enters.
    """

    def __init__(self, node: Node) -> None:
        """Dial `node`'s own p2p port and hold the socket."""
        self.magic = node.chain.magic
        # the receive buffer is set ahead of the connect, where the
        # window it advertises is fixed
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(
            socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCKET_BUFFER_BYTES
        )
        self.socket.settimeout(30)
        self.socket.connect(("127.0.0.1", node.p2p_port))

    def send(self, payload: Payload) -> None:
        """Frame `payload` with this network's own magic and write it."""
        message = Message(
            self.magic, payload.command, payload.serialize(check_validity=False)
        )
        self.socket.sendall(message.serialize())

    def shake_hands(self) -> None:
        """Send what `callbacks.verack` requires before it will promote."""
        services = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS
        self.send(
            Version(
                version=PROTOCOL_VERSION,
                services=services,
                timestamp=int(time.time()),
                addr_recv=NetworkAddress(services=services, port=0),
                addr_from=NetworkAddress(services=services, port=0),
                nonce=secrets.randbelow(2**64),
                user_agent=b"/deaf/",
                start_height=0,
                relay=True,
            )
        )
        # `wtxidrelay` ahead of `verack`, where BIP339 places it
        self.send(WtxidRelay())
        self.send(Verack())

    def close(self) -> None:
        """Drop the socket, whatever is still queued behind it."""
        self.socket.close()


@contextmanager
def a_deaf_peer(
    tmp_path: Path, config: Config | None = None
) -> Iterator[tuple[Node, DeafPeer, list[Block]]]:
    """Give a node holding a served chain, and a peer of it that never reads."""
    chain = blocks_of(_BLOCKS_ASKED_FOR, _SERVED_BLOCK_BYTES)
    with a_served_node(tmp_path, chain, config) as node:
        peer = DeafPeer(node)
        try:
            peer.shake_hands()
            wait_until(lambda: len(node.p2p_manager.connections) == 1)
            the_connection(node).client.setsockopt(
                socket.SOL_SOCKET, socket.SO_SNDBUF, _SOCKET_BUFFER_BYTES
            )
            yield node, peer, chain
        finally:
            peer.close()


@pytest.fixture
def deaf_peer(tmp_path: Path) -> Iterator[tuple[Node, DeafPeer, list[Block]]]:
    """Give `a_deaf_peer` with the node's default configuration."""
    with a_deaf_peer(tmp_path) as served:
        yield served


def a_node_config(tmp_path: Path, *options: str) -> Config:
    """Build a node's configuration from its command line, as `main` does."""
    return cli.build_config(
        [
            f"-datadir={tmp_path}",
            "-regtest",
            f"-port={get_random_port()}",
            "-server=0",
            "-v1transport",
            *options,
        ]
    )


def the_connection(node: Node) -> Connection:
    """Return the one connection the node holds."""
    return next(iter(node.p2p_manager.connections.values()))


def block_messages(chain: list[Block]) -> list[BlockMsg]:
    """Return `chain` as the `block` messages this node serves it in."""
    return [
        BlockMsg(block, include_witness=True, check_validity=False) for block in chain
    ]


def weight(payload: Payload) -> int:
    """Return what `payload` adds to `send_memusage` once queued."""
    serialized = payload.serialize(check_validity=False)
    return connection_module._send_memusage(
        SerializedMessage(payload.command, serialized)
    )


def held_commands(node: Node, connection: Connection) -> list[str]:
    """Return the commands of the messages held unread for `connection`."""
    return [held[0] for held in node.tx_checks.waiting.get(connection.id, ())]


def test_a_getdata_answer_pauses_once_the_send_buffer_is_full(
    deaf_peer: tuple[Node, DeafPeer, list[Block]],
) -> None:
    """ISS 1805: a `getdata` answer stops at `pause_send`, one block past it.

    An entry on `pending_getdata` with `pause_send` set says the answer
    stopped at the bound rather than ran out of items. The buffer is
    past the bound by less than the last block served, and the
    connection is still `Connected`.
    """
    node, peer, chain = deaf_peer
    # connected, as a block off the active chain and not validated is
    # ignored (`_block_request_allowed`)
    connected = chain[:_BLOCKS_PAST_THE_BOUND]
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in connected])
    node.status = NodeStatus.HeaderSynced
    for block in connected:
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == len(connected) + 1)
    peer.send(
        GetData(
            [Inventory(InventoryType.MSG_BLOCK, block.header.hash) for block in chain]
        )
    )
    connection = the_connection(node)
    wait_until(lambda: connection.pause_send and node.pending_getdata)

    assert connection.status == P2pConnStatus.Connected
    _, items = node.pending_getdata[connection.id]
    assert items
    one_block = max(weight(message) for message in block_messages(connected))
    assert connection.send_memusage <= _SEND_BUFFER_MAX_SIZE + one_block


def test_a_peer_that_does_not_read_is_paused_not_dropped(
    deaf_peer: tuple[Node, DeafPeer, list[Block]],
) -> None:
    """ISS 1805: queued past `send_buffer_max_size`, the peer stays connected.

    Handed to `Connection.send` directly rather than asked for: what this
    node sends of its own accord -- a block announcement, an `addr` --
    has no pause in front of it, here or in Core. Every message is
    queued and none is refused, and what the peer sends meanwhile waits
    unread.
    """
    node, peer, chain = deaf_peer
    connection = the_connection(node)
    messages = block_messages(chain[:_BLOCKS_PAST_THE_BOUND])
    for message in messages:
        connection.send(message)
    wait_until(lambda: connection.pause_send)
    peer.send(Ping(1))
    wait_until(lambda: held_commands(node, connection) == ["ping"])

    assert connection.status == P2pConnStatus.Connected
    assert connection.pause_send
    assert connection.send_memusage <= sum(weight(m) for m in messages)


def test_a_peer_that_does_not_read_is_dropped_by_the_ping_timeout(
    deaf_peer: tuple[Node, DeafPeer, list[Block]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1805: what bounds a peer that never reads is `_keep_alive`.

    The peer keeps sending, so it is not silent; it reads nothing, so no
    whole message reaches it and its `ping` goes unanswered. Past
    `_TIMEOUT_INTERVAL`, shortened here, it is dropped, as Core's
    `InactivityCheck` drops it.
    """
    node, peer, chain = deaf_peer
    monkeypatch.setattr(manager_module, "_TIMEOUT_INTERVAL", 2)
    connection = the_connection(node)
    for message in block_messages(chain[:_BLOCKS_PAST_THE_BOUND]):
        connection.send(message)
    wait_until(lambda: connection.pause_send)

    def dropped() -> bool:
        # the node may have closed the socket already, and a send to it
        # then fails; only the node's own table says it dropped the peer
        with suppress(BrokenPipeError, ConnectionResetError):
            peer.send(Ping(1))
        return connection.id not in node.p2p_manager.connections

    wait_until(dropped, timeout=30)
    assert connection.status == P2pConnStatus.Closed


def test_a_getcfilters_from_a_paused_peer_waits_unread(
    deaf_peer: tuple[Node, DeafPeer, list[Block]],
) -> None:
    """A `getcfilters` from a peer past `send_buffer_max_size` is held.

    Core's `ProcessMessages` reads nothing from a peer with `fPauseSend`
    set (btclib-org/btclib-node#1796), so no filter is queued for it and
    the connection stays `Connected`: a peer this far behind is served
    later, not dropped.
    """
    node, peer, chain = deaf_peer
    connection = the_connection(node)
    filtered = blocks_of(_FILTERED_BLOCKS, 32)
    for block in filtered:
        node.block_db.add_block(block)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in filtered])
    node.status = NodeStatus.HeaderSynced
    for block in filtered:
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == _FILTERED_BLOCKS + 1)

    for message in block_messages(chain[:_BLOCKS_PAST_THE_BOUND]):
        connection.send(message)
    wait_until(lambda: connection.pause_send)

    peer.send(GetCFilters(BlockFilterType.BASIC, 1, filtered[-1].header.hash))
    wait_until(lambda: held_commands(node, connection) == ["getcfilters"])

    assert connection.status == P2pConnStatus.Connected
    assert connection.id not in node.pending_cfilters


def test_maxsendbuffer_sets_the_send_bound(tmp_path: Path) -> None:
    """ISS 1812: past Core's default `-maxsendbuffer`, within this one.

    The blocks that pause a connection at the default leave one at
    `-maxsendbuffer=8000` running: they weigh less than its 8,000,000
    bytes, and more than the default's bound whatever the kernel takes.
    """
    config = a_node_config(tmp_path, "-maxsendbuffer=8000")
    with a_deaf_peer(tmp_path, config) as (node, _, chain):
        connection = the_connection(node)
        assert connection.send_buffer_max_size == 8_000_000
        messages = block_messages(chain[:_BLOCKS_PAST_THE_BOUND])
        assert sum(weight(m) for m in messages) <= connection.send_buffer_max_size
        for message in messages:
            connection.send(message)
        assert connection.send_memusage > _SEND_BUFFER_MAX_SIZE
        assert not connection.pause_send


def test_maxreceivebuffer_sets_the_receive_bound(tmp_path: Path) -> None:
    """ISS 1812: at `-maxreceivebuffer=1` a few held `ping`s stop the reads.

    The send buffer is filled first, so what the peer sends is held
    unread and weighs on `queued_recv_bytes`. Reads stop once it passes
    1,000 bytes, far short of the default's bound.
    """
    config = a_node_config(tmp_path, "-maxreceivebuffer=1")
    with a_deaf_peer(tmp_path, config) as (node, peer, chain):
        connection = the_connection(node)
        assert connection.recv_flood_size == 1000
        for message in block_messages(chain[:_BLOCKS_PAST_THE_BOUND]):
            connection.send(message)
        wait_until(lambda: connection.pause_send)
        for nonce in range(10):
            peer.send(Ping(nonce))
        wait_until(lambda: not connection._recv_resume.is_set())
        assert connection.queued_recv_bytes > connection.recv_flood_size
        assert connection.queued_recv_bytes < 1000 * DEFAULT_MAXRECEIVEBUFFER
