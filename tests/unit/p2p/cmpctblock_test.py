# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A block received as a `cmpctblock`, and the peers asked to send them.

Core's `CMPCTBLOCK` and `BLOCKTXN` handlers, `BlockChecked` and
`MaybeSetPeerAsAnnouncingHeaderAndIDs` (`net_processing.cpp`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag), over a real regtest node
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
    SendCmpct,
)
from btclib.p2p.data import BlockPayload
from btclib.p2p.inventory import GetData, GetHeaders, InventoryType
from btclib.p2p.limits import MAX_BLOCK_TX_INDEX
from btclib.script.script_pub_key import ScriptPubKey
from btclib.tx.limits import COINBASE_MATURITY
from btclib.tx.tx import Tx

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.exceptions import MisbehavingError
from btclib_node.main import update_chain, verify_mempool_acceptance
from btclib_node.mining import create_new_block
from btclib_node.p2p.block_availability import in_flight_from
from btclib_node.p2p.callbacks import block as block_callback
from btclib_node.p2p.callbacks import blocktxn, cmpctblock
from btclib_node.p2p.compact_block import (
    block_checked,
    compact_block,
    maybe_set_peer_as_announcing_header_and_ids,
)
from btclib_node.p2p.main import _dispatch
from tests import (
    anyone_can_spend,
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
    """Return a connected peer that sent `sendcmpct(2)` and serves witnesses.

    It is marked high-bandwidth, so that a block nobody asked of it is taken.
    """
    services = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS
    peer = a_peer(
        **{
            "id": conn_id,
            "status": P2pConnStatus.Connected,
            "provides_cmpctblocks": True,
            "bip152_highbandwidth_to": True,
            "version_message": a_version(services),
            **attributes,
        }
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


def test_a_cmpctblock_from_a_peer_that_never_sent_sendcmpct_is_ignored(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's return at `net_processing.cpp:4798-4801`, before the parse.

    Read at bitcoin/bitcoin@f0de87f040, the v32.1 tag.
    """
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node, provides_cmpctblocks=False)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    cmpctblock(node, b"not a cmpctblock", peer)
    assert not peer.sent
    assert block.header.hash not in node.chainstate.block_index.header_dict
    assert not held(node, block)


@pytest.mark.parametrize("requested", [False, True], ids=["unasked", "asked"])
def test_a_cmpctblock_nobody_asked_of_a_peer_not_high_bandwidth_is_ignored(
    regtest_node: Callable[[], Node],
    requested: bool,  # noqa: FBT001
) -> None:
    """Core's return at `net_processing.cpp:4887-4890`, after the header.

    Read at bitcoin/bitcoin@f0de87f040, the v32.1 tag. The header is
    taken, as Core takes it. Where the block was asked of the peer, it
    is rebuilt whatever the peer's bandwidth.
    """
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node, bip152_highbandwidth_to=False)
    if requested:
        peer.download_queue.append(block.header.hash)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert block.header.hash in node.chainstate.block_index.header_dict
    assert bool(sent(peer, GetBlockTxn)) is requested
    assert peer.download_queue == ([block.header.hash] if requested else [])


def test_a_cmpctblock_nobody_asked_of_a_high_bandwidth_peer_is_taken(
    regtest_node: Callable[[], Node],
) -> None:
    """A peer this node sent `sendcmpct(1)` announces blocks unasked."""
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node, bip152_highbandwidth_to=True)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert len(sent(peer, GetBlockTxn)) == 1
    assert peer.download_queue == [block.header.hash]


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


def test_a_compact_block_and_its_transactions_with_octets_past_them_are_taken(
    regtest_node: Callable[[], Node],
) -> None:
    """ISS 2097: Core's `CMPCTBLOCK` and `BLOCKTXN` read no further."""
    node, _ = a_node(regtest_node)
    missing = generate_random_transaction()
    block = next_block(node, missing)
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(block, 7).serialize() + b"\x00", peer)
    (request,) = sent(peer, GetBlockTxn)
    assert list(request.indexes) == [1]
    blocktxn(node, BlockTxn(block.header.hash, [missing]).serialize() + b"\x00", peer)
    assert held(node, block)


def test_a_block_with_octets_past_it_is_stored_without_them(
    regtest_node: Callable[[], Node],
) -> None:
    """ISS 2097: Core's `BLOCK` reads the block; nothing after it is kept."""
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node)
    peer.download_queue.append(block.header.hash)
    payload = BlockPayload(block, include_witness=True, check_validity=False)
    block_callback(node, payload.serialize(check_validity=False) + b"\x00", peer)
    assert held(node, block)
    stored = node.block_db.get_raw_block(block.header.hash)
    assert stored == block.serialize(check_validity=False)


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


def test_a_collision_reaches_the_dispatcher_as_no_failure(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Btclib raises `ShortIdCollisionError`, which is not a failure to log.

    Dispatched as a received message, the collision asks for the block:
    nothing is logged as an exception, and the peer stays connected.
    """
    node, _ = a_node(regtest_node)
    block = next_block(
        node, generate_random_transaction(), generate_random_transaction()
    )
    compact = compact_block(block, 7)
    short_id = compact.short_ids[0]
    collided = CmpctBlock(
        compact.header, compact.nonce, [short_id, short_id], compact.prefilled_txns
    )
    peer = a_compact_peer(node)
    failures: list[object] = []
    monkeypatch.setattr(node.logger, "exception", lambda *args: failures.append(args))
    payload = collided.serialize()
    _dispatch(node, peer, peer.id, "cmpctblock", payload)
    assert not failures
    (getdata,) = sent(peer, GetData)
    assert [item.hash for item in getdata.items] == [block.header.hash]
    assert node.p2p_manager.connections[peer.id] is peer


@pytest.mark.parametrize(
    "case", ["empty", "index past", "index past 65535", "null prefilled"]
)
def test_a_compact_block_init_data_refuses_is_misbehaviour(
    regtest_node: Callable[[], Node], case: str
) -> None:
    """Core's `InitData` answers INVALID, and the request is dropped.

    No transaction, a prefilled index past the short ids (one of them past
    65535, which btclib's parse keeps), or a prefilled transaction with
    neither input nor output.
    """
    node, _ = a_node(regtest_node)
    block = next_block(node)
    coinbase = block.transactions[0]
    null = Tx(version=1, lock_time=0, vin=[], vout=[], check_validity=False)
    prefilled = {
        "empty": [],
        "index past": [PrefilledTransaction(1, coinbase, check_validity=False)],
        "index past 65535": [
            PrefilledTransaction(0, coinbase, check_validity=False),
            PrefilledTransaction(
                MAX_BLOCK_TX_INDEX + 1, coinbase, check_validity=False
            ),
        ],
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
    """Core's deserializer throws "indexes overflowed 16 bits", unpunished.

    btclib's parse refuses the count, so the message is its own.
    """
    node, _ = a_node(regtest_node)
    block = next_block(node)
    prefilled = [PrefilledTransaction(0, block.transactions[0])]
    compact = CmpctBlock(block.header, 7, range(65535), prefilled, check_validity=False)
    peer = a_compact_peer(node)
    with pytest.raises(BTClibValueError, match="invalid transaction count") as refusal:
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
    peer = a_compact_peer(node, bip152_highbandwidth_to=False)
    peer.download_queue.append(block.header.hash)
    peer.block_availability.request_order[block.header.hash] = 5
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


def test_a_reconstruction_refusal_other_than_a_collision_is_not_swallowed(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a short-id collision is Core's FAILED, answered with the block."""

    def refuse(*_args: object) -> None:
        err_msg = "not a collision"
        raise BTClibValueError(err_msg)

    monkeypatch.setattr("btclib_node.p2p.callbacks.reconstruct", refuse)
    node, _ = a_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node)
    with pytest.raises(BTClibValueError, match="not a collision"):
        cmpctblock(node, compact_block(block, 7).serialize(), peer)


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
    peer = a_compact_peer(node, bip152_highbandwidth_to=False)
    peer.download_queue.append(block.header.hash)
    peer.block_availability.request_order[block.header.hash] = 5
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert peer.sent == []
    assert peer.download_queue == []


def sendcmpcts(peer: Any) -> list[bool]:
    """Return the `announce` of every `sendcmpct` `peer` was sent."""
    return [message.announce for message in sent(peer, SendCmpct)]


def test_a_peer_that_gave_a_new_block_is_asked_for_high_bandwidth(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `BlockChecked`: the block connected, its peer is chosen.

    Its source is forgotten once the block is checked, which its message
    does before it returns.
    """
    node, _ = a_node(regtest_node)
    block = next_block(node)
    peer = a_compact_peer(node, inbound=True, bip152_highbandwidth_to=False)
    peer.download_queue.append(block.header.hash)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert node.chainstate.block_index.active_chain[-1] == block.header.hash
    assert sendcmpcts(peer) == [True]
    assert peer.bip152_highbandwidth_to
    assert node.download_manager.hb_peers == [peer.id]
    assert node.download_manager.block_source == {}


@pytest.mark.parametrize("inbound", [False, True], ids=["outbound", "inbound"])
@pytest.mark.parametrize("via", ["cmpctblock", "block"])
def test_a_block_that_fails_to_connect_costs_a_peer_that_sent_it_whole(
    regtest_node: Callable[[], Node],
    via: str,
    inbound: bool,  # noqa: FBT001
) -> None:
    """Core's `BlockChecked` punishes, except a block that came compact.

    The block spends an output nobody has, which only connecting finds,
    before its message returns. Its peer is not chosen as high-bandwidth,
    and its source is forgotten.
    """
    node, _ = a_node(regtest_node)
    extra = generate_random_transaction()
    node.download_manager.extra_txns.append(extra)
    block = next_block(node, extra)
    # automatic: a manual outbound peer is never dropped
    peer = a_compact_peer(node, inbound=inbound, automatic=not inbound)
    if via == "cmpctblock":
        cmpctblock(node, compact_block(block, 7).serialize(), peer)
    else:
        peer.download_queue.append(block.header.hash)
        payload = BlockPayload(block, include_witness=True, check_validity=False)
        block_callback(node, payload.serialize(check_validity=False), peer)
    assert node.chainstate.block_index.active_chain[-1] != block.header.hash
    assert node.download_manager.block_source == {}
    assert not sent(peer, SendCmpct)
    assert peer.stopped == ([True] if via == "block" else [])


@pytest.mark.parametrize("via", ["cmpctblock", "block"])
def test_a_block_is_the_tip_once_its_message_is_handled(
    regtest_node: Callable[[], Node], via: str
) -> None:
    """Core's `ProcessNewBlock` connects the block before it returns.

    So the next block mined, as `generatetoaddress` builds it, extends it
    (btclib-org/btclib-node#2172).
    """
    node, _ = a_node(regtest_node)
    block = next_block(node)
    peer = a_compact_peer(node)
    if via == "cmpctblock":
        cmpctblock(node, compact_block(block, 7).serialize(), peer)
    else:
        peer.download_queue.append(block.header.hash)
        payload = BlockPayload(block, include_witness=True, check_validity=False)
        block_callback(node, payload.serialize(check_validity=False), peer)
    assert node.chainstate.block_index.active_chain[-1] == block.header.hash
    template = create_new_block(node, ScriptPubKey(anyone_can_spend(), "regtest"))
    assert template.block.header.previous_block_hash == block.header.hash


def announced_blocks(node: Node, tip: Block, peer: Any) -> list[Block]:
    """Return three blocks on `tip`, their headers indexed, asked of `peer`."""
    blocks: list[Block] = []
    for height in range(2, 5):
        time = datetime.now(UTC) + timedelta(seconds=height)
        parent = blocks[-1] if blocks else tip
        coinbase = generate_coinbase(height=height)
        blocks.append(build_block(parent.header.hash, [coinbase], height, time))
    node.chainstate.block_index.add_headers([block.header for block in blocks])
    peer.download_queue.extend(block.header.hash for block in blocks)
    return blocks


def send_block(node: Node, block: Block, peer: Any) -> None:
    """Hand `block` to the `block` callback, as `peer` sent it."""
    payload = BlockPayload(block, include_witness=True, check_validity=False)
    block_callback(node, payload.serialize(check_validity=False), peer)


def test_blocks_whose_headers_came_first_are_the_tip_one_message_at_a_time(
    regtest_node: Callable[[], Node],
) -> None:
    """Each block is the tip once its own message is handled.

    The shape of Core's `p2p_sendheaders`, whose peer announces blocks
    by their headers and sends them, then has the node mine on them.
    """
    node, chain = a_node(regtest_node)
    peer = a_compact_peer(node)
    for block in announced_blocks(node, chain[-1], peer):
        send_block(node, block, peer)
        assert node.chainstate.block_index.active_chain[-1] == block.header.hash


def test_a_block_that_fills_a_gap_connects_the_blocks_stored_past_it(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `ActivateBestChain` goes to the most-work chain, not a step on.

    The blocks past the gap wait for it, unconnected.
    """
    node, chain = a_node(regtest_node)
    peer = a_compact_peer(node)
    first, *rest = announced_blocks(node, chain[-1], peer)
    for block in rest:
        send_block(node, block, peer)
    block_index = node.chainstate.block_index
    assert block_index.active_chain[-1] == chain[-1].header.hash
    send_block(node, first, peer)
    expected = [block.header.hash for block in (first, *rest)]
    assert block_index.active_chain[-3:] == expected


def a_block_on(parent: bytes, height: int, *transactions: Tx) -> Block:
    """Return a block on `parent` at `height`, `height` seconds from now."""
    time = datetime.now(UTC) + timedelta(seconds=height)
    coinbase = generate_coinbase(height=height)
    return build_block(parent, [coinbase, *transactions], height, time)


def test_a_fork_that_fails_to_connect_gives_way_to_the_next_candidate(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `ActivateBestChain` looks for the best chain again on a failure.

    The heavier fork fails at its first block, which spends an output
    nobody has, and the lighter one connects before the message that let
    both connect returns.
    """
    node, chain = a_node(regtest_node)
    joint = a_block_on(chain[-1].header.hash, 2)
    invalid = a_block_on(joint.header.hash, 3, generate_random_transaction())
    heavier = a_block_on(invalid.header.hash, 4)
    lighter = a_block_on(joint.header.hash, 3)
    blocks = (joint, invalid, heavier, lighter)
    block_index = node.chainstate.block_index
    block_index.add_headers([joint.header, invalid.header, heavier.header])
    block_index.add_headers([lighter.header])
    peer = a_compact_peer(node)
    peer.download_queue.extend(block.header.hash for block in blocks)
    for block in (invalid, heavier, lighter, joint):
        send_block(node, block, peer)
    assert block_index.active_chain[-1] == lighter.header.hash


def store_unconnected(node: Node, block: Block) -> None:
    """Index, store and mark `block` downloaded, without connecting it."""
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header])
    node.block_db.add_block(block)
    block_index.set_downloaded(block.header.hash)


@pytest.mark.parametrize("case", ["held", "refused"])
def test_a_block_not_stored_still_activates_the_best_chain(
    regtest_node: Callable[[], Node], case: str
) -> None:
    """Core's `AcceptBlock` answers `true`, and `ActivateBestChain` runs.

    A block already held, and one not asked for with less work than the
    tip: either connects a block stored and not yet connected.
    """
    node, chain = a_node(regtest_node, 2)
    waiting = a_block_on(chain[-1].header.hash, 3)
    store_unconnected(node, waiting)
    peer = a_compact_peer(node)
    if case == "held":
        sent_block = waiting
    else:
        sent_block = a_block_on(RegTest().genesis.hash, 1)
        node.chainstate.block_index.add_headers([sent_block.header])
    block_index = node.chainstate.block_index
    assert block_index.active_chain[-1] == chain[-1].header.hash
    send_block(node, sent_block, peer)
    assert block_index.active_chain[-1] == waiting.header.hash
    stored = block_index.get_block_info(sent_block.header.hash).downloaded
    assert stored is (case == "held")


def test_a_block_whose_activation_fails_stops_the_node(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure activating the best chain stops `Node`, as in `getblocks`."""

    def boom(_node: object) -> None:
        err_msg = "boom"
        raise OSError(err_msg)

    monkeypatch.setattr("btclib_node.p2p.callbacks.activate_best_chain", boom)
    node, _ = a_node(regtest_node)
    block = next_block(node)
    peer = a_compact_peer(node)
    peer.download_queue.append(block.header.hash)
    payload = BlockPayload(block, include_witness=True, check_validity=False)
    with pytest.raises(OSError, match="boom"):
        block_callback(node, payload.serialize(check_validity=False), peer)
    assert node.terminate_flag.is_set()


def test_a_peer_gone_pays_nothing_for_a_block_that_fails_to_connect(
    regtest_node: Callable[[], Node],
) -> None:
    """Core punishes a peer it still has (`State(nodeid)`)."""
    node, _ = a_node(regtest_node)
    peer = a_compact_peer(node, inbound=True)
    node.download_manager.block_source[b"\x01" * 32] = (peer.id, True)
    del node.p2p_manager.connections[peer.id]
    block_checked(node, b"\x01" * 32, valid=False)
    assert peer.stopped == []
    assert node.download_manager.block_source == {}


@pytest.mark.parametrize("case", ["ibd", "another in flight", "no source"])
def test_a_block_connected_otherwise_chooses_nobody(
    regtest_node: Callable[[], Node], case: str
) -> None:
    """Core asks out of initial block download, and of the best block only.

    The best block being the one with nothing else in flight.
    """
    node, _ = a_node(regtest_node)
    peer = a_compact_peer(node)
    block = next_block(node)
    if case == "ibd":
        node.is_initial_block_download = True
    other = a_compact_peer(node, 2)
    if case == "another in flight":
        other.download_queue.append(b"\x01" * 32)
    if case != "no source":
        node.download_manager.block_source[block.header.hash] = (peer.id, True)
    block_checked(node, block.header.hash, valid=True)
    assert peer.sent == []
    assert node.download_manager.hb_peers == []
    assert node.download_manager.block_source == {}


def test_an_invalid_block_chooses_nobody(regtest_node: Callable[[], Node]) -> None:
    """Core's `BlockChecked` asks a valid block; its source goes either way."""
    node, _ = a_node(regtest_node)
    peer = a_compact_peer(node)
    node.download_manager.block_source[b"\x01" * 32] = (peer.id, False)
    block_checked(node, b"\x01" * 32, valid=False)
    assert peer.sent == []
    assert node.download_manager.block_source == {}


def test_a_block_not_new_forgets_its_source(regtest_node: Callable[[], Node]) -> None:
    """Core's `ProcessBlock` erases the source of a block it did not store.

    The first peer to give a block stays its source. The block rivals the
    tip with the same work, so it stays unconnected: connecting it would
    check it, which forgets its source too.
    """
    node, chain = a_node(regtest_node, 2)
    time = datetime.now(UTC) + timedelta(seconds=1)
    block = build_block(chain[0].header.hash, [generate_coinbase(height=2)], 2, time)
    first, second = a_compact_peer(node), a_compact_peer(node, 2)
    first.download_queue.append(block.header.hash)
    payload = BlockPayload(block, include_witness=True, check_validity=False)
    block_callback(node, payload.serialize(check_validity=False), first)
    assert node.chainstate.block_index.active_chain[-1] == chain[-1].header.hash
    assert node.download_manager.block_source == {block.header.hash: (first.id, True)}
    second.download_queue.append(block.header.hash)
    block_callback(
        node,
        BlockPayload(block, include_witness=True, check_validity=False).serialize(
            check_validity=False
        ),
        second,
    )
    assert node.download_manager.block_source == {}


def test_three_peers_are_high_bandwidth_at_once(
    regtest_node: Callable[[], Node],
) -> None:
    """BIP152's three: a fourth drops the oldest, sent `sendcmpct(0)`.

    One chosen again moves to the end, and is sent nothing.
    """
    node, _ = a_node(regtest_node)
    peers = [
        a_compact_peer(node, conn_id, bip152_highbandwidth_to=False)
        for conn_id in (1, 2, 3, 4)
    ]
    for peer in peers[:3]:
        maybe_set_peer_as_announcing_header_and_ids(node, peer.id)
    maybe_set_peer_as_announcing_header_and_ids(node, 1)
    assert node.download_manager.hb_peers == [2, 3, 1]
    maybe_set_peer_as_announcing_header_and_ids(node, 4)
    assert node.download_manager.hb_peers == [3, 1, 4]
    assert [sendcmpcts(peer) for peer in peers] == [
        [True],
        [True, False],
        [True],
        [True],
    ]
    assert [peer.bip152_highbandwidth_to for peer in peers] == [
        True,
        False,
        True,
        True,
    ]


def test_the_new_peer_is_asked_before_the_oldest_is_dropped(
    regtest_node: Callable[[], Node],
) -> None:
    """Core sends the fourth `sendcmpct(1)`, then the first `sendcmpct(0)`."""
    node, _ = a_node(regtest_node)
    sends: list[tuple[int, bool]] = []
    for conn_id in (1, 2, 3, 4):
        a_compact_peer(
            node,
            conn_id,
            send=lambda message, conn_id=conn_id: sends.append(
                (conn_id, message.announce)
            ),
        )
        maybe_set_peer_as_announcing_header_and_ids(node, conn_id)
    assert sends == [(1, True), (2, True), (3, True), (4, True), (1, False)]


def test_a_new_peer_not_reached_drops_nobody(
    regtest_node: Callable[[], Node],
) -> None:
    """Core drops the oldest only where the new peer was added."""
    node, _ = a_node(regtest_node)
    peers = [a_compact_peer(node, conn_id) for conn_id in (1, 2, 3)]
    for peer in peers:
        maybe_set_peer_as_announcing_header_and_ids(node, peer.id)
    newcomer = a_compact_peer(node, 4, status=P2pConnStatus.Open)
    maybe_set_peer_as_announcing_header_and_ids(node, 4)
    assert node.download_manager.hb_peers == [1, 2, 3]
    assert [sendcmpcts(peer) for peer in peers] == [[True], [True], [True]]
    assert newcomer.sent == []


def test_an_inbound_peer_does_not_take_the_last_outbound_slot(
    regtest_node: Callable[[], Node],
) -> None:
    """Core swaps the last outbound peer out of the front before dropping it."""
    node, _ = a_node(regtest_node)
    outbound = a_compact_peer(node, 1, inbound=False, bip152_highbandwidth_to=False)
    inbound = [a_compact_peer(node, conn_id, inbound=True) for conn_id in (2, 3, 4)]
    for conn_id in (1, 2, 3, 4):
        maybe_set_peer_as_announcing_header_and_ids(node, conn_id)
    assert node.download_manager.hb_peers == [1, 3, 4]
    assert outbound.bip152_highbandwidth_to
    assert sendcmpcts(inbound[0]) == [True, False]


def test_an_inbound_peer_drops_the_oldest_where_two_outbound_are_chosen(
    regtest_node: Callable[[], Node],
) -> None:
    """Core keeps the front only where it is the one outbound peer chosen."""
    node, _ = a_node(regtest_node)
    for conn_id, inbound in ((1, False), (2, False), (3, True), (4, True)):
        a_compact_peer(node, conn_id, inbound=inbound)
        maybe_set_peer_as_announcing_header_and_ids(node, conn_id)
    assert node.download_manager.hb_peers == [2, 3, 4]


def test_an_inbound_peer_drops_the_oldest_where_no_outbound_is_chosen(
    regtest_node: Callable[[], Node],
) -> None:
    """With no outbound peer to keep, the oldest goes, as for any other."""
    node, _ = a_node(regtest_node)
    for conn_id in (1, 2, 3, 4):
        a_compact_peer(node, conn_id, inbound=True)
        maybe_set_peer_as_announcing_header_and_ids(node, conn_id)
    assert node.download_manager.hb_peers == [2, 3, 4]


@pytest.mark.parametrize("case", ["no sendcmpct", "gone", "not connected"])
def test_a_peer_that_cannot_be_asked_is_not_chosen(
    regtest_node: Callable[[], Node], case: str
) -> None:
    """Core asks only a connected peer that sent `sendcmpct(2)`."""
    node, _ = a_node(regtest_node)
    peer = a_compact_peer(
        node,
        provides_cmpctblocks=case != "no sendcmpct",
        status=(
            P2pConnStatus.Open if case == "not connected" else P2pConnStatus.Connected
        ),
    )
    if case == "gone":
        del node.p2p_manager.connections[peer.id]
    maybe_set_peer_as_announcing_header_and_ids(node, peer.id)
    assert peer.sent == []
    assert node.download_manager.hb_peers == []


def test_a_peer_gone_is_dropped_from_the_front_unsent(
    regtest_node: Callable[[], Node],
) -> None:
    """Core keeps a gone peer's id, and pops it when its turn comes."""
    node, _ = a_node(regtest_node)
    for conn_id in (1, 2, 3):
        a_compact_peer(node, conn_id)
        maybe_set_peer_as_announcing_header_and_ids(node, conn_id)
    del node.p2p_manager.connections[1]
    newcomer = a_compact_peer(node, 4)
    maybe_set_peer_as_announcing_header_and_ids(node, 4)
    assert node.download_manager.hb_peers == [2, 3, 4]
    assert sendcmpcts(newcomer) == [True]
