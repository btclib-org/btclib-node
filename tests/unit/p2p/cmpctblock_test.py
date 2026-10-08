# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A block received as a `cmpctblock`, and finished by a `blocktxn`.

Core's `CMPCTBLOCK` and `BLOCKTXN` handlers (`net_processing.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag), over a real regtest node
whose tip is recent.
"""

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import TYPE_CHECKING, Any

import pytest
from btclib.exceptions import BTClibValueError
from btclib.p2p.address import ServiceFlags
from btclib.p2p.compact_blocks import (
    BlockTxn,
    CmpctBlock,
    GetBlockTxn,
    PrefilledTransaction,
)
from btclib.p2p.inventory import GetData, GetHeaders, InventoryType
from btclib.tx.limits import COINBASE_MATURITY
from btclib.tx.tx import Tx

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus
from btclib_node.exceptions import MisbehavingError
from btclib_node.main import update_chain, verify_mempool_acceptance
from btclib_node.p2p.block_availability import in_flight_from
from btclib_node.p2p.callbacks import blocktxn, cmpctblock
from btclib_node.p2p.compact_block import compact_block
from tests import (
    build_block,
    generate_coinbase,
    generate_random_chain,
    generate_random_transaction,
)
from tests.unit.download_test import a_version
from tests.unit.p2p.callbacks_test import a_peer

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib.block import Block, BlockHeader

    from btclib_node import Node


def a_node(
    regtest_node: Callable[[], Node], length: int = 1, *, recent: bool = True
) -> tuple[Node, list[Block]]:
    """Return a node out of initial block download, and its chain past genesis.

    The tip is dated now where `recent`, so that a block nobody asked for
    is wanted.
    """
    node = regtest_node()
    tip_time = datetime.now(UTC) if recent else None
    chain = generate_random_chain(length, RegTest().genesis.hash, tip_time=tip_time)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    for _ in chain:
        update_chain(node)
    assert block_index.active_chain[-1] == chain[-1].header.hash
    node.status = NodeStatus.BlockSynced
    node.is_initial_block_download = False
    node.warm_worker_pool = lambda: None  # type: ignore[method-assign]
    return node, chain


def next_block(node: Node, *transactions: Tx) -> Block:
    """Return a block on `node`'s tip carrying `transactions`.

    Dated a second past now, so that it is past the tip of a short chain,
    which is the median time past.
    """
    block_index = node.chainstate.block_index
    tip = block_index.active_chain[-1]
    height = len(block_index.active_chain)
    coinbase = generate_coinbase(height=height)
    time = datetime.now(UTC) + timedelta(seconds=1)
    return build_block(tip, [coinbase, *transactions], height, time=time)


def a_compact_peer(node: Node, conn_id: int = 1, **attributes: Any) -> Any:
    """Return a connected peer that sent `sendcmpct(2)` and serves witnesses."""
    services = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS
    peer = a_peer(
        id=conn_id,
        provides_cmpctblocks=True,
        version_message=a_version(services),
        **attributes,
    )
    node.p2p_manager.connections[conn_id] = peer
    return peer


def held(node: Node, block: Block) -> bool:
    """Answer whether `node` stored `block`."""
    info = node.chainstate.block_index.header_dict.get(block.header.hash)
    return info is not None and info.downloaded


def sent(peer: Any, kind: type) -> list[Any]:
    """Return what `peer` was sent of one message type."""
    return [message for message in peer.sent if isinstance(message, kind)]


def test_a_block_whose_transactions_are_all_held_is_taken_at_once(
    regtest_node: Callable[[], Node],
) -> None:
    """Rebuilt from the mempool and the extra transactions, nothing asked."""
    node, chain = a_node(regtest_node, COINBASE_MATURITY)
    funding = chain[0].transactions[0]
    pooled = generate_random_transaction(funding.id, funding.vout[0].value - 1_000)
    assert node.mempool.add_tx(
        pooled, *verify_mempool_acceptance(node, pooled, bypass_limits=True)
    )
    extra = generate_random_transaction()
    node.download_manager.extra_txns.append(extra)
    block = next_block(node, pooled, extra)
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert held(node, block)
    assert not sent(peer, GetBlockTxn)
    assert peer.download_queue == []
    assert block.header.hash not in peer.block_availability.partial_blocks


def test_a_block_short_of_transactions_asks_for_them_and_takes_them(
    regtest_node: Callable[[], Node],
) -> None:
    """The missing indexes go in a `getblocktxn`, and a `blocktxn` ends it."""
    node, _ = a_node(regtest_node)
    known = generate_random_transaction()
    node.download_manager.extra_txns.append(known)
    first, second = generate_random_transaction(), generate_random_transaction()
    block = next_block(node, first, known, second)
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    (request,) = sent(peer, GetBlockTxn)
    assert request.block_hash == block.header.hash
    assert list(request.indexes) == [1, 3]
    assert peer.download_queue == [block.header.hash]
    assert not held(node, block)
    blocktxn(node, BlockTxn(block.header.hash, [first, second]).serialize(), peer)
    assert held(node, block)
    assert peer.download_queue == []


def test_transactions_other_than_those_missing_are_misbehaviour(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `READ_STATUS_INVALID` out of `FillBlock`: too few, or too many."""
    node, _ = a_node(regtest_node)
    missing = generate_random_transaction()
    block = next_block(node, missing)
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    with pytest.raises(MisbehavingError, match="non-matching block transactions"):
        blocktxn(node, BlockTxn(block.header.hash, []).serialize(), peer)
    assert peer.download_queue == []
    assert not held(node, block)


def test_a_block_its_header_does_not_commit_to_asks_for_the_block(
    regtest_node: Callable[[], Node],
) -> None:
    """A wrong transaction may be a short-id collision: Core's FAILED.

    The peer asked first is asked for the whole block, and a second
    `blocktxn` for it is misbehaviour, the partial block spent.
    """
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    wrong = BlockTxn(block.header.hash, [generate_random_transaction()]).serialize()
    blocktxn(node, wrong, peer)
    (getdata,) = sent(peer, GetData)
    assert [(item.type_code, item.hash) for item in getdata.items] == [
        (InventoryType.MSG_WITNESS_BLOCK, block.header.hash)
    ]
    assert peer.download_queue == [block.header.hash]
    assert not held(node, block)
    with pytest.raises(MisbehavingError, match="reconstruction attempt failed"):
        blocktxn(node, wrong, peer)
    assert peer.download_queue == []


def test_a_compact_block_whose_short_ids_collide_asks_for_the_block(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `InitData` answers FAILED, and the block is asked for whole.

    The peer is kept. Core keeps the prefilled transactions too, so a
    `blocktxn` carrying every other transaction still ends it.
    """
    node, _ = a_node(regtest_node)
    first, second = generate_random_transaction(), generate_random_transaction()
    block = next_block(node, first, second)
    compact = compact_block(block, 7)
    short_id = compact.short_ids[0]
    collided = CmpctBlock(
        compact.header, compact.nonce, [short_id, short_id], compact.prefilled_txns
    )
    peer = a_compact_peer(node)
    cmpctblock(node, collided.serialize(), peer)
    (getdata,) = sent(peer, GetData)
    assert [item.hash for item in getdata.items] == [block.header.hash]
    assert not sent(peer, GetBlockTxn)
    assert peer.download_queue == [block.header.hash]
    blocktxn(node, BlockTxn(block.header.hash, [first, second]).serialize(), peer)
    assert held(node, block)


@pytest.mark.parametrize("case", ["empty", "index past", "null prefilled"])
def test_a_compact_block_init_data_refuses_is_misbehaviour(
    regtest_node: Callable[[], Node], case: str
) -> None:
    """Core's `InitData` answers INVALID, and the request is dropped.

    No transaction, a prefilled index past the short ids, or a prefilled
    transaction with neither input nor output.
    """
    node, _ = a_node(regtest_node)
    block = next_block(node)
    coinbase = block.transactions[0]
    null = Tx(version=1, lock_time=0, vin=[], vout=[], check_validity=False)
    prefilled = {
        "empty": [],
        "index past": [PrefilledTransaction(1, coinbase, check_validity=False)],
        "null prefilled": [PrefilledTransaction(0, null, check_validity=False)],
    }[case]
    compact = CmpctBlock(block.header, 7, [], prefilled, check_validity=False)
    peer = a_compact_peer(node)
    with pytest.raises(MisbehavingError, match="invalid compact block"):
        cmpctblock(node, compact.serialize(check_validity=False), peer)
    assert peer.download_queue == []


def solves(header: BlockHeader, nonce: int) -> bool:
    """Answer whether `nonce` meets `header`'s target, setting it."""
    header.nonce = nonce
    return header.hash <= header.target


def test_a_compact_block_header_too_old_is_misbehaviour(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `ProcessNewBlockHeaders` punishes `time-too-old`.

    The header is dated before genesis, which a checked parse refused
    and kept the peer for.
    """
    node, _ = a_node(regtest_node)
    header = next_block(node).header
    header.time = datetime(2000, 1, 1, tzinfo=UTC)
    # btclib's `mine` refuses the date, so the search is this one
    header.nonce = next(n for n in count() if solves(header, n))
    compact = CmpctBlock(header, 7, [1], [], check_validity=False)
    peer = a_compact_peer(node)
    with pytest.raises(MisbehavingError, match="time-too-old"):
        cmpctblock(node, compact.serialize(check_validity=False), peer)


def test_a_compact_block_of_more_than_65535_transactions_keeps_the_peer(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's deserializer throws "indexes overflowed 16 bits", unpunished."""
    node, _ = a_node(regtest_node)
    block = next_block(node)
    prefilled = [PrefilledTransaction(0, block.transactions[0])]
    compact = CmpctBlock(block.header, 7, range(65535), prefilled, check_validity=False)
    peer = a_compact_peer(node)
    with pytest.raises(BTClibValueError, match="overflowed") as refusal:
        cmpctblock(node, compact.serialize(check_validity=False), peer)
    assert not isinstance(refusal.value, MisbehavingError)
    assert block.header.hash not in node.chainstate.block_index.header_dict


def test_an_invalid_block_received_compact_costs_the_peer_nothing(
    regtest_node: Callable[[], Node],
) -> None:
    """BIP152 lets a peer relay a block whose header alone it checked.

    Core's `MaybePunishNodeForBlock` punishes no `via_compact_block`
    failure of `CheckBlock`, here a second coinbase: a plain refusal, not
    a `MisbehavingError`.
    """
    node, _ = a_node(regtest_node)
    second_coinbase = generate_coinbase(height=2)
    block = next_block(node, second_coinbase)
    peer = a_compact_peer(node)
    node.download_manager.extra_txns.append(second_coinbase)
    with pytest.raises(BTClibValueError) as refusal:
        cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert not isinstance(refusal.value, MisbehavingError)
    assert not held(node, block)


def test_a_block_with_less_work_than_the_tip_is_left_alone(
    regtest_node: Callable[[], Node],
) -> None:
    """Nothing is asked for a sibling of the tip that nobody asked for."""
    node, _ = a_node(regtest_node, 2)
    block_index = node.chainstate.block_index
    sibling = build_block(
        block_index.active_chain[-3],
        [generate_coinbase(height=1), generate_random_transaction()],
        5,
    )
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(sibling, 7).serialize(), peer)
    assert peer.sent == []
    assert peer.download_queue == []
    assert sibling.header.hash in block_index.header_dict


def test_a_block_asked_of_others_is_taken_only_if_rebuilt_whole(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's optimistic reconstruction, where this peer has no room.

    Three peers already have it in flight, so this one is not asked:
    a block it cannot rebuild alone is left to them.
    """
    node, _ = a_node(regtest_node)
    unknown = generate_random_transaction()
    short = next_block(node, unknown)
    for conn_id in (2, 3, 4):
        holder = a_compact_peer(node, conn_id)
        holder.download_queue.append(short.header.hash)
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(short, 7).serialize(), peer)
    assert peer.sent == []
    assert peer.download_queue == []
    assert not held(node, short)
    node.download_manager.extra_txns.append(unknown)
    cmpctblock(node, compact_block(short, 8).serialize(), peer)
    assert held(node, short)
    assert peer.sent == []


@pytest.mark.parametrize("ibd", [False, True], ids=["synced", "ibd"])
def test_a_block_on_an_unknown_parent_asks_for_headers_out_of_ibd(
    regtest_node: Callable[[], Node],
    ibd: bool,  # noqa: FBT001
) -> None:
    """Core's "Doesn't connect (or is genesis)": a `getheaders` alone."""
    node, _ = a_node(regtest_node)
    node.is_initial_block_download = ibd
    orphan = build_block(
        b"\x01" * 32, [generate_coinbase(height=5), generate_random_transaction()], 5
    )
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(orphan, 7).serialize(), peer)
    assert [type(message) for message in peer.sent] == ([] if ibd else [GetHeaders])
    assert orphan.header.hash not in node.chainstate.block_index.header_dict


def test_a_block_below_the_anti_dos_work_is_ignored(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `min_pow_checked`: its header is not even indexed."""
    node, _ = a_node(regtest_node)
    node.config.minimum_chain_work = 2**200
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert peer.sent == []
    assert block.header.hash not in node.chainstate.block_index.header_dict


@pytest.mark.parametrize("requested", [False, True], ids=["unasked", "asked"])
def test_a_block_more_than_two_past_the_tip_is_not_rebuilt(
    regtest_node: Callable[[], Node],
    requested: bool,  # noqa: FBT001
) -> None:
    """Asked for in full where it was asked of the peer, a header otherwise.

    Read as a header, it is fetched as `headers` fetches it: every block
    up to it, in full.
    """
    node, chain = a_node(regtest_node)
    block_index = node.chainstate.block_index
    previous, now = chain[-1].header.hash, datetime.now(UTC)
    between = []
    for height in (2, 3):
        time = now + timedelta(seconds=height)
        coinbase = generate_coinbase(height=height)
        header = build_block(previous, [coinbase], height, time=time).header
        block_index.add_headers([header])
        previous = header.hash
        between.append(previous)
    far = build_block(
        previous, [generate_coinbase(height=4)], 4, time=now + timedelta(seconds=4)
    )
    peer = a_compact_peer(node)
    if requested:
        peer.download_queue.append(far.header.hash)
    cmpctblock(node, compact_block(far, 7).serialize(), peer)
    assert not sent(peer, GetBlockTxn)
    asked = [
        (item.type_code, item.hash)
        for message in sent(peer, GetData)
        for item in message.items
    ]
    assert peer.block_availability.partial_blocks == {}
    fetched = [far.header.hash] if requested else [*between, far.header.hash]
    assert asked == [(InventoryType.MSG_WITNESS_BLOCK, hash_) for hash_ in fetched]


def test_a_peer_asked_after_another_does_not_ask_for_what_is_missing(
    regtest_node: Callable[[], Node],
) -> None:
    """Only the peer asked first, or a high-bandwidth one, sends `getblocktxn`.

    Otherwise the block is left to the first, and no longer asked of this
    peer.
    """
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    first = a_compact_peer(node, 2)
    first.download_queue.append(block.header.hash)
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert peer.sent == []
    assert peer.download_queue == []
    high_bandwidth = a_compact_peer(node, 3, bip152_highbandwidth_to=True)
    cmpctblock(node, compact_block(block, 7).serialize(), high_bandwidth)
    (request,) = sent(high_bandwidth, GetBlockTxn)
    assert list(request.indexes) == [1]
    assert high_bandwidth.download_queue == [block.header.hash]
    assert in_flight_from(node.p2p_manager.connections.values(), block.header.hash) == [
        first,
        high_bandwidth,
    ]


def test_a_second_cmpctblock_for_a_block_being_rebuilt_is_ignored(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's "Peer sent us compact block we were already syncing!"."""
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    partial = peer.block_availability.partial_blocks[block.header.hash]
    cmpctblock(node, compact_block(block, 8).serialize(), peer)
    assert len(sent(peer, GetBlockTxn)) == 1
    assert peer.block_availability.partial_blocks[block.header.hash] is partial


def test_block_transactions_nobody_asked_for_are_ignored(
    regtest_node: Callable[[], Node],
) -> None:
    """Core logs "block we weren't expecting" and keeps the peer."""
    node, _ = a_node(regtest_node)
    missing = generate_random_transaction()
    block = next_block(node, missing)
    peer = a_compact_peer(node)
    blocktxn(node, BlockTxn(block.header.hash, [missing]).serialize(), peer)
    peer.download_queue.append(block.header.hash)
    blocktxn(node, BlockTxn(block.header.hash, [missing]).serialize(), peer)
    assert peer.sent == []
    assert peer.download_queue == [block.header.hash]


def test_a_block_asked_for_with_less_work_than_the_tip_is_asked_in_full(
    regtest_node: Callable[[], Node],
) -> None:
    """Core asks for the block itself, which it may have pruned."""
    node, _ = a_node(regtest_node, 2)
    block_index = node.chainstate.block_index
    sibling = build_block(
        block_index.active_chain[-3],
        [generate_coinbase(height=1), generate_random_transaction()],
        5,
    )
    block_index.add_headers([sibling.header])
    peer = a_compact_peer(node)
    peer.download_queue.append(sibling.header.hash)
    cmpctblock(node, compact_block(sibling, 7).serialize(), peer)
    (getdata,) = sent(peer, GetData)
    assert [item.hash for item in getdata.items] == [sibling.header.hash]


def test_a_block_nobody_asked_for_behind_a_stale_tip_is_left_alone(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `CanDirectFetch`: only its header is taken."""
    node, _ = a_node(regtest_node, recent=False)
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert peer.sent == []
    assert peer.download_queue == []
    assert block.header.hash in node.chainstate.block_index.header_dict


def test_a_block_queued_behind_another_keeps_the_wait_on_the_first(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `BlockRequested` starts the clock only for an empty queue."""
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node)
    peer.download_queue.append(b"\x01" * 32)
    peer.block_availability.downloading_since = 5.0
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert peer.download_queue == [b"\x01" * 32, block.header.hash]
    assert peer.block_availability.downloading_since == 5.0


def test_a_collision_from_a_peer_asked_second_is_left_to_the_first(
    regtest_node: Callable[[], Node],
) -> None:
    """Core asks the block of the peer asked first alone."""
    node, _ = a_node(regtest_node)
    block = next_block(
        node, generate_random_transaction(), generate_random_transaction()
    )
    compact = compact_block(block, 7)
    short_id = compact.short_ids[0]
    collided = CmpctBlock(
        compact.header, compact.nonce, [short_id, short_id], compact.prefilled_txns
    )
    first = a_compact_peer(node, 2)
    first.download_queue.append(block.header.hash)
    peer = a_compact_peer(node)
    cmpctblock(node, collided.serialize(), peer)
    assert peer.sent == []
    assert peer.download_queue == []


def test_an_optimistic_reconstruction_that_fails_is_ignored(
    regtest_node: Callable[[], Node],
) -> None:
    """Core ignores what its optimistic reconstruction cannot use.

    A compact block of no transactions costs the peer nothing there, and
    a block its header does not commit to is not taken.
    """
    node, _ = a_node(regtest_node)
    known = generate_random_transaction()
    node.download_manager.extra_txns.append(known)
    block = next_block(node, known)
    other = next_block(node, generate_random_transaction())
    for conn_id in (2, 3, 4):
        holder = a_compact_peer(node, conn_id)
        holder.download_queue.append(other.header.hash)
    peer = a_compact_peer(node)
    empty = CmpctBlock(other.header, 7, [], [], check_validity=False)
    cmpctblock(node, empty.serialize(check_validity=False), peer)
    keyed = CmpctBlock(other.header, 7, (), (), check_validity=False)
    wrong = CmpctBlock(
        other.header,
        7,
        [keyed.short_id(known.hash)],
        compact_block(block, 7).prefilled_txns,
    )
    cmpctblock(node, wrong.serialize(), peer)
    assert peer.sent == []
    assert not held(node, other)


def test_a_wrong_blocktxn_from_a_peer_asked_second_is_left_to_the_first(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's FAILED out of `FillBlock` leaves the block to the first peer."""
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    first = a_compact_peer(node, 2)
    first.download_queue.append(block.header.hash)
    peer = a_compact_peer(node, bip152_highbandwidth_to=True)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert sent(peer, GetBlockTxn)
    wrong = BlockTxn(block.header.hash, [generate_random_transaction()])
    blocktxn(node, wrong.serialize(), peer)
    assert not sent(peer, GetData)
    assert peer.download_queue == []


@pytest.mark.parametrize(
    ("holders", "asks"),
    [
        ([True], True),
        ([True, True], False),
        ([False, True], True),
    ],
    ids=["one inbound asked", "two inbound asked", "an outbound asked"],
)
def test_an_inbound_high_bandwidth_peer_keeps_the_last_slot_for_an_outbound(
    regtest_node: Callable[[], Node],
    holders: list[bool],
    asks: bool,  # noqa: FBT001
) -> None:
    """Core's rule for an inbound high-bandwidth peer asked after others.

    It sends `getblocktxn` where an outbound peer is asked already, or
    where the slot it takes is not the last one.
    """
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    for conn_id, inbound in enumerate(holders, start=2):
        holder = a_compact_peer(node, conn_id, inbound=inbound)
        holder.download_queue.append(block.header.hash)
    peer = a_compact_peer(node, inbound=True, bip152_highbandwidth_to=True)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert bool(sent(peer, GetBlockTxn)) is asks
    assert peer.download_queue == ([block.header.hash] if asks else [])


def test_a_peer_asked_second_does_not_ask_for_what_is_missing(
    regtest_node: Callable[[], Node],
) -> None:
    """A peer the block was asked of after another is not first in flight."""
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    first = a_compact_peer(node, 2)
    first.download_queue.append(block.header.hash)
    peer = a_compact_peer(node)
    peer.download_queue.append(block.header.hash)
    peer.block_availability.request_order[block.header.hash] = 5
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert peer.sent == []
    assert peer.download_queue == []
