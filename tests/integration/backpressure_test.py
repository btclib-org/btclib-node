# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`MAX_QUEUED_RECV_BYTES` watched against a real bitcoind serving big blocks.

The receive-side bound is the one half of this node's backpressure a
daemon can actually reach: a peer serving a chain faster than
`Node._drain_message_queues` empties what it queued is what
`Connection.run`'s own pause exists for, and Core is the peer that does
it. The send-side bounds are the other half and want a peer that stops
reading, which Core never is --
`tests/functional/p2p/backpressure_test.py` is where those are.
btclib-org/btclib-node#492

`bitcoind_test.py` beside this asks whether the handshake and a short
sync work at all; what is added here is scale. The blocks are built here
and handed to bitcoind through `submitblock` rather than mined by its
wallet: what a wallet can put in one block is bounded by mempool policy
-- a cluster's own weight in this release -- so a megabyte of it costs
hundreds of transactions and a coinbase maturity to fund them, where a
coinbase paying one large unspendable output is a megabyte on its own,
consensus-valid, and needs no policy relaxed on the command line.
"""

import asyncio
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING, override

from btclib.block import Block, BlockHeader, merkle_root_and_mutated_from_transactions
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.connection import MAX_QUEUED_RECV_BYTES
from tests import (
    GENESIS_TIME,
    brute_force_nonce,
    get_random_port,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path

    from btclib_node.p2p.connection import Connection
    from tests.integration.conftest import Bitcoind

# How much of one block is the unspendable payload, how many outputs
# follow it, and how many such blocks the chain carries.
#
# A block's serialized size is what `MAX_QUEUED_RECV_BYTES` is measured
# against, and consensus caps a witnessless block's own weight at four
# times that size -- so a megabyte is the most one of these can be, and
# the payload is sized just under it. `_BLOCKS` is comfortably past what
# `MAX_BLOCKS_PER_GETDATA_BURST` (`btclib_node/download.py`) asks one
# peer for at a time, so the sync is several bursts rather than one.
#
# The trailing outputs are what make the node's own validation of a
# block cost more than bitcoind's serving of it: without them the two
# race, and whether a burst's sixth block arrives before the first is
# drained -- which is what decides whether the bound is crossed at all
# -- comes out differently from run to run. With them the arrival is
# comfortably the faster of the two, which is the case this bound is
# about in the first place.
_PAYLOAD_BYTES = 980_000
_OUTPUTS_PER_BLOCK = 100
_BLOCKS = 20


class RecordingResume(asyncio.Event):
    """A `Connection._recv_resume` that counts the pauses it was cleared for.

    `_weigh_against_recv_bound` clears this event only where
    `queued_recv_bytes` has just crossed `MAX_QUEUED_RECV_BYTES`, so a
    count of clears is a count of pauses -- taken on the loop's own
    thread as it happens, rather than by a poll from the test's, which
    would have to catch a pause that lasts milliseconds.
    """

    def __init__(self) -> None:
        """Start set, the way a fresh connection's own event does."""
        super().__init__()
        self.pauses = 0
        self.set()

    @override
    def clear(self) -> None:
        self.pauses += 1
        super().clear()


def a_big_block(previous_block_hash: bytes, height: int) -> Block:
    """Return a solved regtest block bitcoind will accept at `height`.

    The coinbase's own scriptSig opens with the height, which is what
    BIP34 requires and what Core checks against `CScript() << nHeight` --
    a bare `OP_1`..`OP_16` for the heights this chain reaches, and not
    the one-octet push of the same number, which is a different
    encoding. One octet follows it because a coinbase scriptSig shorter
    than two is refused.
    """
    coinbase = Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(),
                script_sig=bytes([0x50 + height, 0x00])
                if height <= 16
                else bytes([0x01, height, 0x00]),
                sequence=0xFFFFFFFF,
            )
        ],
        # nothing is paid out: a coinbase may claim less than the
        # subsidy, and no test here spends one
        vout=[
            TxOut(
                value=0,
                script_pub_key=b"\x6a"
                + script.serialize([secrets.token_bytes(_PAYLOAD_BYTES)]),
            ),
            *(
                TxOut(
                    value=0,
                    script_pub_key=script.serialize([secrets.token_bytes(32)]),
                )
                for _ in range(_OUTPUTS_PER_BLOCK)
            ),
        ],
    )
    header = BlockHeader(
        version=0x20000000,
        previous_block_hash=previous_block_hash,
        merkle_root=merkle_root_and_mutated_from_transactions([coinbase])[0],
        # ten minutes apart, so every header beats the median of the
        # eleven before it and none is ahead of the clock
        time=GENESIS_TIME + timedelta(seconds=600 * height),
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)
    # `Block.__init__` checks the header against mainnet's pow limit,
    # which no regtest block meets; `brute_force_nonce` has already
    # checked it against the limit that does apply, the same reason
    # `tests.build_block` gives
    return Block(header, [coinbase], check_validity=False)


def fill(bitcoind: Bitcoind) -> None:
    """Build the chain and submit it, failing on the first block refused."""
    previous_block_hash = RegTest().genesis.hash
    for height in range(1, _BLOCKS + 1):
        block = a_big_block(previous_block_hash, height)
        previous_block_hash = block.header.hash
        # `submitblock` answers `None` on acceptance and a reason
        # otherwise, rather than raising: an unread answer here would
        # leave the chain short and the assertion below blaming the sync
        refused = bitcoind.rpc(
            "submitblock", [block.serialize(check_validity=False).hex()]
        )
        assert refused is None, refused


def the_connection(node: Node) -> Connection:
    """Return the one connection the node holds, wherever it currently sits."""
    manager = node.p2p_manager
    held = {**manager.pending_connections, **manager.connections}
    return next(iter(held.values()))


def test_a_node_paces_a_bitcoind_serving_it_megabyte_blocks(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """The receive bound engages during the sync, and the sync still finishes.

    The pause is the point and so is what follows it: a bound that
    stopped reading and never resumed would show exactly the same count
    here, so the tip is what says the connection came back rather than
    stalling. `_recv_resume` is swapped for a counting event as soon as
    the connection object exists, which is before the handshake, let
    alone before a block.
    """
    fill(bitcoind)
    tip = bitcoind.rpc("getbestblockhash")

    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node.start()
    wait_until_listening(node.p2p_manager)
    node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))

    wait_until(
        lambda: node.p2p_manager.pending_connections or node.p2p_manager.connections
    )
    connection = the_connection(node)
    recv_resume = RecordingResume()
    connection._recv_resume = recv_resume

    block_index = node.chainstate.block_index
    wait_until(lambda: len(block_index.active_chain) == _BLOCKS + 1)
    wait_until(lambda: node.status == NodeStatus.BlockSynced)

    assert block_index.active_chain[-1].hex() == tip
    assert recv_resume.pauses
    assert connection.status == P2pConnStatus.Connected
    assert connection.queued_recv_bytes <= MAX_QUEUED_RECV_BYTES

    node.stop()
    node.join()
