# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The send-side bounds, against a peer on a real socket that stops reading.

`MAX_GETDATA_INFLIGHT_BYTES` and `MAX_CFILTERS_INFLIGHT_BYTES`
(`btclib_node/p2p/callbacks.py`) pause a half-served answer, and
`MAX_QUEUED_SEND_BYTES` (`btclib_node/p2p/connection.py`) drops a
connection whose queue would grow past it. All three engage only against
a peer that stops draining what it was sent, and a well-behaved daemon
always reads: pointing a bitcoind at this node cannot reach any of them,
so this half of the question wants a synthetic peer and no daemon at
all. The receive-side half is `tests/integration/backpressure_test.py`,
which does want one. btclib-org/btclib-node#492

The peer here completes the handshake and then never calls `recv`
again. Nothing it was sent is lost: those octets sit in the two kernel
buffers until the window closes behind them, and what will not fit is
what stands in this node's own `queued_send_bytes`.

The blocks a `getdata` asks for go straight into `block_db` without the
header chain that would ordinarily carry them: `advance_getdata` serves
an item out of that store alone, so connecting them would add block
validation to a fixture whose subject is the send queue. The filter test
below does connect its own blocks, a block's filter being built as it
connects.
"""

from __future__ import annotations

import secrets
import socket
import time
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest
from btclib.block import Block, BlockHeader, merkle_root_and_mutated_from_transactions
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.p2p.block_filters import BlockFilterType, GetCFilters
from btclib.p2p.data import BlockPayload as BlockMsg
from btclib.p2p.handshake import Verack, Version
from btclib.p2p.inventory import GetData, Inventory, InventoryType
from btclib.p2p.message import Message
from btclib.p2p.negotiation import WtxidRelay
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus, P2pConnStatus, ProtocolVersion
from btclib_node.p2p.callbacks import MAX_CFILTERS_INFLIGHT_BYTES
from btclib_node.p2p.connection import MAX_QUEUED_SEND_BYTES
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

# One served block, and how many of them the `getdata` below asks for.
# The product is several times `MAX_QUEUED_SEND_BYTES`, so the answer
# cannot fit however much of it this node schedules ahead of the peer's
# own draining.
#
# A megabyte is what makes the pause itself the subject rather than the
# room above it: `MAX_QUEUED_SEND_BYTES` is sized for one block past
# `MAX_GETDATA_INFLIGHT_BYTES` and no more (`p2p/connection.py`), so at
# this size an answer scheduling items past its own bound spends that
# room and gets the peer dropped --
# btclib-org/btclib-node#512, and what
# `test_a_getdata_answer_pauses_rather_than_filling_the_send_queue`
# below stands against. It is an ordinary size to be asked for, too: a
# peer in initial block download asks for
# `MAX_BLOCKS_PER_GETDATA_BURST` blocks of up to
# `MAX_PROTOCOL_MESSAGE_LENGTH` (`btclib_node/download.py`), which is
# what this node asks its own peers for.
_SERVED_BLOCK_BYTES = 1_000_000
_BLOCKS_ASKED_FOR = 3 * MAX_QUEUED_SEND_BYTES // _SERVED_BLOCK_BYTES

# What the filter test queues at the connection before it asks for a
# filter at all, and how many blocks it then asks about. A filter's size
# follows the number of scripts in its block rather than the block's own
# size, and a block carries few enough of those that reaching
# `MAX_CFILTERS_INFLIGHT_BYTES` in filters alone wants a chain long
# enough to cost minutes in block validation. That bound is not about
# filters, though: it is how far ahead of a peer's own draining a filter
# answer may schedule, so a connection put that far behind by ordinary
# traffic is the same state, reached in seconds. Comfortably past that
# bound, and comfortably short of `MAX_QUEUED_SEND_BYTES`, which would
# drop the peer instead of pausing it.
_BLOCKS_QUEUED_AHEAD = 4
_FILTERED_BLOCKS = 4


def a_block(previous_block_hash: bytes, height: int, outputs: list[TxOut]) -> Block:
    """Return a solved regtest block whose one transaction pays `outputs`."""
    coinbase = Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(),
                script_sig=script.serialize([secrets.token_bytes(32)]),
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
    wants without giving it transactions to validate.
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


def a_served_node(tmp_path: Path, chain: list[Block]) -> Node:
    """Return a started node holding `chain` in its block store."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            p2p_port=get_random_port(),
            allow_rpc=False,
            debug=True,
        )
    )
    node.start()
    wait_until_listening(node.p2p_manager)
    for block in chain:
        node.block_db.add_block(block)
    return node


class DeafPeer:
    """A peer that finishes the handshake and then never reads again.

    Sending is all it does afterwards, so everything this node answers
    stays in the socket -- the state all three send-side bounds are
    about, and the one no daemon enters.
    """

    def __init__(self, node: Node) -> None:
        """Dial `node`'s own p2p port and hold the socket."""
        self.magic = node.chain.magic
        # cast, for the reason rpc/connections_test.py gives at its own:
        # a Node without the p2p listener carries no port, and this one
        # is built with it
        port = cast("int", node.p2p_port)
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=30)

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
                version=ProtocolVersion,
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
        # `wtxidrelay` ahead of `verack`: `callbacks.verack` discourages
        # and drops a connection whose `wtxidrelay_received` is still
        # false when the `verack` arrives
        self.send(WtxidRelay())
        self.send(Verack())

    def close(self) -> None:
        """Drop the socket, whatever is still queued behind it."""
        self.socket.close()


@pytest.fixture
def deaf_peer(tmp_path: Path) -> Iterator[tuple[Node, DeafPeer, list[Block]]]:
    """Give a node holding a served chain, and a peer of it that never reads."""
    chain = blocks_of(_BLOCKS_ASKED_FOR, _SERVED_BLOCK_BYTES)
    node = a_served_node(tmp_path, chain)
    peer = DeafPeer(node)
    try:
        peer.shake_hands()
        wait_until(lambda: len(node.p2p_manager.connections) == 1)
        yield node, peer, chain
    finally:
        peer.close()
        node.stop()


def the_connection(node: Node) -> Connection:
    """Return the one connection the node holds."""
    return next(iter(node.p2p_manager.connections.values()))


def test_a_getdata_answer_pauses_rather_than_filling_the_send_queue(
    deaf_peer: tuple[Node, DeafPeer, list[Block]],
) -> None:
    """A `getdata` past the send queue leaves the rest on `pending_getdata`.

    An entry there is what says `MAX_GETDATA_INFLIGHT_BYTES` engaged:
    `advance_getdata`'s only way out with items still to serve is its
    own check against that bound, so the entry cannot be reached without
    `queued_send_bytes` having crossed it. The connection is still
    `Connected` afterwards, which is the difference between pacing a peer
    and dropping one.
    """
    node, peer, chain = deaf_peer
    peer.send(
        GetData(
            [Inventory(InventoryType.MSG_BLOCK, block.header.hash) for block in chain]
        )
    )
    wait_until(lambda: node.pending_getdata)

    connection = the_connection(node)
    assert connection.status == P2pConnStatus.Connected
    _, items = node.pending_getdata[connection.id]
    assert items


def test_the_send_queue_bound_drops_a_peer_it_has_no_way_to_pace(
    deaf_peer: tuple[Node, DeafPeer, list[Block]],
) -> None:
    """Queued past `MAX_QUEUED_SEND_BYTES`, the connection is stopped.

    Handed to `Connection.send` directly rather than asked for through a
    `getdata`: what this node sends of its own accord -- a block
    announcement, an `addr`, a `headers` answer -- is counted against
    this bound with no pacing point in front of it, and this bound is
    the only thing underneath. `queued_send_bytes` never crosses it, the
    refusal coming before the message is counted.
    """
    node, _, chain = deaf_peer
    connection = the_connection(node)
    for block in chain:
        connection.send(BlockMsg(block, include_witness=True, check_validity=False))
    wait_until(lambda: connection.status == P2pConnStatus.Closed)
    assert connection.queued_send_bytes <= MAX_QUEUED_SEND_BYTES


def test_a_getcfilters_answer_will_not_schedule_ahead_of_a_peer_that_is_behind(
    deaf_peer: tuple[Node, DeafPeer, list[Block]],
) -> None:
    """A connection already past `MAX_CFILTERS_INFLIGHT_BYTES` gets no filters.

    `node.pending_cfilters` carries the same evidence `pending_getdata`
    does in
    `test_a_getdata_answer_pauses_rather_than_filling_the_send_queue`
    above: `advance_cfilters` leaves heights behind only where it stopped
    at its own bound. The connection stays `Connected`, the whole point
    of the pause being that a peer this far behind is served later rather
    than dropped.
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

    for block in chain[:_BLOCKS_QUEUED_AHEAD]:
        connection.send(BlockMsg(block, include_witness=True, check_validity=False))
    wait_until(lambda: connection.queued_send_bytes >= MAX_CFILTERS_INFLIGHT_BYTES)

    peer.send(GetCFilters(BlockFilterType.BASIC, 1, filtered[-1].header.hash))
    wait_until(lambda: node.pending_cfilters)

    assert connection.status == P2pConnStatus.Connected
    _, heights = node.pending_cfilters[connection.id]
    assert heights
