# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`-reindex-chainstate`: the chainstate wiped, then rebuilt from the blocks.

`chainstate`'s module docstring is what is checked: a node with one
stored branch, restarted with the option, comes back at the same tip,
with the same UTXO set, its commitment and its undo data, the filters it
never wiped, and a stop part-way resumes on a plain start. That start,
like any with stored blocks to connect, is `NodeStatus.Reindexing` until
they are, and holds Core's `LoadingBlocks()` gates meanwhile. Under
`-prune` the option is refused, as Core refuses it. A start without
`-prune` on a pruned directory is refused before either wipe.
"""

import math
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import pytest
from btclib import var_int
from btclib.p2p.compact_blocks import BlockTxn
from btclib.p2p.data import BlockPayload as BlockMsg
from btclib.p2p.inventory import GetHeaders, Headers
from btclib.p2p.limits import MAX_HEADERS_RESULTS, PROTOCOL_VERSION
from btclib.tx.limits import COINBASE_MATURITY

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.block_index import BlockInfo, BlockStatus
from btclib_node.chainstate.utxo_index import UtxoIndex
from btclib_node.cli import _help_message, build_config
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.db import KeyValueStore
from btclib_node.exceptions import MisbehavingError
from btclib_node.log import Logger
from btclib_node.main import (
    activate_best_chain,
    prune_up_to_height,
    update_chain,
    update_ibd_status,
)
from btclib_node.p2p.callbacks import block as block_callback
from btclib_node.p2p.callbacks import blocktxn, cmpctblock, getheaders, headers, inv
from btclib_node.p2p.compact_block import compact_block
from tests import generate_random_chain, generate_random_transaction
from tests.unit.download_test import (
    _OLD,
    HeaderIndex,
    an_extra_peer_manager,
    an_outbound,
    asked,
    make_manager,
)
from tests.unit.p2p.callbacks_test import (
    FakeBlockIndex,
    a_block,
    a_data_node,
    a_peer,
    an_info,
    an_inv_index,
    block_inv,
)
from tests.unit.p2p.cmpctblock_test import a_compact_peer, held, next_block
from tests.unit.p2p.cmpctblock_test import a_node as compact_node

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from btclib.block import Block

_PRUNE_REFUSAL = (
    "Prune mode is incompatible with -reindex-chainstate. Use full -reindex instead."
)


@contextmanager
def _node(
    tmp_path: Path,
    *,
    reindex_chainstate: bool = False,
    persist_mempool: bool = True,
    pruned: bool = False,
) -> Iterator[Node]:
    """Load a regtest node on `tmp_path`, unstarted, and close it after."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            reindex_chainstate=reindex_chainstate,
            persist_mempool=persist_mempool,
            pruned=pruned,
        )
    )
    node.load()
    try:
        yield node
    finally:
        node._close_worker_pool()
        node.p2p_manager.peer_db.close()
        node.chainstate.close()
        node.block_db.close()
        node.p2p_manager.loop.close()
        node.rpc_manager.loop.close()
        node.logger.close()


def _store(node: Node, blocks: list[Block], headers: list[Block]) -> None:
    """Index the headers of `headers`, and store and mark `blocks`."""
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in headers])
    for block in blocks:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)


def _status(node: Node) -> NodeStatus:
    """Read `node.status` afresh, past the narrowing of an earlier assert."""
    return node.status


class _State(NamedTuple):
    """What the rebuild has to give back, read from the disk."""

    active_chain: list[bytes]
    hash_serialized: bytes | None
    coin_stats: bytes
    tip_filter_header: bytes | None
    rev_patches: list[bytes]


def _state(node: Node) -> _State:
    """Flush, then read `_State` back from the stores."""
    node.chainstate.flush()
    utxo_index = node.chainstate.utxo_index
    active_chain = list(node.chainstate.block_index.active_chain)
    return _State(
        active_chain,
        UtxoIndex.serialized_hash(utxo_index.cursor()),
        utxo_index.coin_stats.serialize(),
        node.chainstate.filter_index.get_header(active_chain[-1]),
        sorted(node.block_db.rev_patches),
    )


def test_rebuilds_the_same_tip_utxo_set_and_undo_and_keeps_the_filters(
    tmp_path: Path,
) -> None:
    """The option wipes at load, and the reconnect gives every piece back."""
    regtest = RegTest()
    # past maturity, so that blocks spend as well as create
    chain = generate_random_chain(COINBASE_MATURITY + 5, regtest.genesis.hash)
    with _node(tmp_path) as node:
        _store(node, chain, chain)
        activate_best_chain(node)
        before = _state(node)
        filters = {
            block.header.hash: node.chainstate.filter_index.get_filter(
                block.header.hash
            )
            for block in chain
        }
    assert len(before.active_chain) == len(chain) + 1
    assert len(before.rev_patches) == len(chain)
    assert None not in filters.values()

    with _node(tmp_path, reindex_chainstate=True) as node:
        assert node.status == NodeStatus.Reindexing
        assert node.chainstate.block_index.active_chain == [regtest.genesis.hash]
        assert list(node.chainstate.utxo_index.cursor()) == []
        # Core keeps its block filter index on `-reindex-chainstate`
        assert filters == {
            block_hash: node.chainstate.filter_index.get_filter(block_hash)
            for block_hash in filters
        }
        assert node.block_db.rev_patches == {}
        assert not [n for n in node.block_db.live if n.endswith(".rev")]
        activate_best_chain(node)
        assert _status(node) == NodeStatus.Starting
        assert _state(node) == before
        assert filters == {
            block_hash: node.chainstate.filter_index.get_filter(block_hash)
            for block_hash in filters
        }

    with _node(tmp_path) as node:
        assert _state(node) == before


def test_a_stop_part_way_resumes_on_a_plain_start(tmp_path: Path) -> None:
    """A stop part-way leaves a shorter chain, and a plain start goes on."""
    regtest = RegTest()
    chain = generate_random_chain(COINBASE_MATURITY + 5, regtest.genesis.hash)
    with _node(tmp_path) as node:
        _store(node, chain, chain)
        activate_best_chain(node)
        before = _state(node)

    with _node(tmp_path, reindex_chainstate=True) as node:
        for _ in range(10):
            update_chain(node)
        assert len(node.chainstate.block_index.active_chain) == 11

    with _node(tmp_path) as node:
        assert len(node.chainstate.block_index.active_chain) == 11
        assert node.status == NodeStatus.Reindexing
        activate_best_chain(node)
        assert _status(node) == NodeStatus.Starting
        assert _state(node) == before


def test_a_plain_start_with_nothing_stored_above_the_tip_is_not_reindexing(
    tmp_path: Path,
) -> None:
    """A header with no block behind it is nothing to connect."""
    regtest = RegTest()
    chain = generate_random_chain(3, regtest.genesis.hash)
    with _node(tmp_path) as node:
        _store(node, chain[:-1], chain)
        activate_best_chain(node)

    with _node(tmp_path) as node:
        assert node.chainstate.block_index.active_chain[-1] == chain[-2].header.hash
        assert node.status == NodeStatus.Starting


def test_a_restart_with_unconnected_blocks_reindexes_until_they_connect(
    tmp_path: Path,
) -> None:
    """Blocks stored and not yet connected hold `Reindexing`, as in Core.

    Core's `ImportBlocks` holds `LoadingBlocks()` on every start until
    `ActivateBestChains` has connected them.
    """
    regtest = RegTest()
    chain = generate_random_chain(3, regtest.genesis.hash)
    with _node(tmp_path) as node:
        _store(node, chain, chain)
        assert node.chainstate.block_index.active_chain == [regtest.genesis.hash]

    with _node(tmp_path) as node:
        assert node.status == NodeStatus.Reindexing
        activate_best_chain(node)
        assert node.chainstate.block_index.active_chain[-1] == chain[-1].header.hash
        assert _status(node) == NodeStatus.Starting


def test_the_reindex_ends_where_the_best_branch_is_not_stored(
    tmp_path: Path,
) -> None:
    """A header beyond the stored blocks ends the reindex below it."""
    regtest = RegTest()
    chain = generate_random_chain(4, regtest.genesis.hash)
    with _node(tmp_path) as node:
        _store(node, chain[:-1], chain)
        activate_best_chain(node)

    with _node(tmp_path, reindex_chainstate=True) as node:
        block_index = node.chainstate.block_index
        tip = chain[-1].header.hash
        assert block_index.get_block_info(tip).status == BlockStatus.valid_header
        activate_best_chain(node)
        assert block_index.active_chain[-1] == chain[-2].header.hash
        assert node.status == NodeStatus.Starting


def test_the_reindex_ending_below_a_header_ends_initial_block_download(
    tmp_path: Path,
) -> None:
    """The stage comes back and IBD may end, as after Core's import."""
    regtest = RegTest()
    now = datetime.now(UTC).replace(microsecond=0)
    chain = generate_random_chain(3, regtest.genesis.hash, tip_time=now)
    (header_only,) = generate_random_chain(1, chain[-1].header.hash, tip_time=now)
    with _node(tmp_path) as node:
        _store(node, chain, [*chain, header_only])
        activate_best_chain(node)

    with _node(tmp_path, reindex_chainstate=True) as node:
        activate_best_chain(node)
        assert node.chainstate.block_index.active_chain[-1] == chain[-1].header.hash
        assert _status(node) == NodeStatus.Starting
        assert not node.is_initial_block_download


def test_without_a_mempool_file_the_load_still_waits_for_the_reindex(
    tmp_path: Path,
) -> None:
    """`-persistmempool=0`: the load is tried only once the reindex ends.

    As Core's `initload` runs `LoadMempool` and `SetLoadTried` after
    `ImportBlocks`, so `getmempoolinfo` answers `loaded: false` meanwhile.
    """
    chain = generate_random_chain(2, RegTest().genesis.hash)
    with _node(tmp_path) as node:
        _store(node, chain, chain)
    with _node(tmp_path, reindex_chainstate=True, persist_mempool=False) as node:
        node._start_mempool_load()
        node._resume_mempool_load()
        assert not node.mempool.load_tried
        activate_best_chain(node)
        node._resume_mempool_load()
        assert node.mempool.load_tried


def test_initial_block_download_lasts_while_reindexing(tmp_path: Path) -> None:
    """A recent tip leaves initial block download only once the reindex ends."""
    regtest = RegTest()
    chain = generate_random_chain(
        1, regtest.genesis.hash, tip_time=datetime.now(UTC).replace(microsecond=0)
    )
    with _node(tmp_path) as node:
        _store(node, chain, chain)

    with _node(tmp_path, reindex_chainstate=True) as node:
        # the tip the reindex would reach, put in place by hand
        node.chainstate.block_index.active_chain.append(chain[0].header.hash)
        update_ibd_status(node)
        assert node.is_initial_block_download
        node.status = NodeStatus.SyncingHeaders
        update_ibd_status(node)
        assert not node.is_initial_block_download


def test_the_wipe_keeps_genesis_on_the_active_chain(tmp_path: Path) -> None:
    """A stored genesis stays `in_active_chain`, the chain's one block."""
    regtest = RegTest()
    genesis = regtest.genesis.hash
    chainstate = Chainstate(tmp_path, regtest, Logger(debug=True))
    # writes genesis's own record, as pruning height 0 does
    chainstate.block_index.set_downloaded(genesis)
    chainstate.close()

    chainstate = Chainstate(tmp_path, regtest, Logger(debug=True), wipe=True)
    try:
        assert chainstate.block_index.active_chain == [genesis]
        stored = chainstate.db.get(b"blkinfo-" + genesis)
        assert stored is not None
        status = BlockInfo.deserialize(stored, check_validity=False).status
        assert status == BlockStatus.in_active_chain
    finally:
        chainstate.close()


def test_no_peer_is_newly_asked_for_headers_while_reindexing() -> None:
    """Core's `LoadingBlocks()` gate on starting a header sync."""
    peer = an_outbound(1)
    manager = make_manager(
        [peer], status=NodeStatus.Reindexing, block_index=HeaderIndex(age=_OLD)
    )
    manager.sync_headers()
    assert not asked(peer)
    manager.node.status = NodeStatus.SyncingHeaders
    manager.sync_headers()
    assert asked(peer)


def test_a_getheaders_is_not_answered_while_reindexing(tmp_path: Path) -> None:
    """Core's `LoadingBlocks()` return in `GETHEADERS`."""
    with _node(tmp_path) as real:
        node = a_data_node(
            block_index=real.chainstate.block_index, status=NodeStatus.Reindexing
        )
        request = GetHeaders(PROTOCOL_VERSION, [RegTest().genesis.hash], b"\0" * 32)
        peer = a_peer()
        getheaders(node, request.serialize(), peer)
        assert not peer.sent
        node.status = NodeStatus.BlockSynced
        getheaders(node, request.serialize(), peer)
        assert peer.sent == [Headers([])]


def test_a_headers_is_dropped_unread_while_reindexing() -> None:
    """Core's `LoadingBlocks()` return in `HEADERS`, ahead of the count."""
    node = a_data_node(status=NodeStatus.Reindexing)
    past_the_bound = var_int.serialize(MAX_HEADERS_RESULTS + 1)
    headers(node, past_the_bound, a_peer())
    node.status = NodeStatus.BlockSynced
    with pytest.raises(MisbehavingError):
        headers(node, past_the_bound, a_peer())


def test_a_block_is_dropped_unread_while_reindexing() -> None:
    """Core's `LoadingBlocks()` return in `BLOCK`; the request stays queued."""
    block = a_block()
    block_hash = block.header.hash
    added: list[Block] = []
    node = a_data_node(
        block_index=FakeBlockIndex({block_hash: an_info(downloaded=False, index=1)}),
        block_db=SimpleNamespace(add_block=added.append),
        status=NodeStatus.Reindexing,
    )
    peer = a_peer(download_queue=[block_hash])
    node.p2p_manager.connections = {peer.id: peer}
    payload = BlockMsg(block, include_witness=True, check_validity=False).serialize(
        check_validity=False
    )
    block_callback(node, payload, peer)
    assert added == []
    assert peer.download_queue == [block_hash]
    node.status = NodeStatus.BlockSynced
    block_callback(node, payload, peer)
    assert added == [block]


def test_a_block_inv_asks_for_no_headers_while_reindexing() -> None:
    """Core's `LoadingBlocks()` gate on the `getheaders` of a block `inv`."""
    node = a_data_node(block_index=an_inv_index(), status=NodeStatus.Reindexing)
    peer = a_peer()
    node.download_manager.headers_sync_timeouts[peer.id] = math.inf
    inv(node, block_inv(b"\x11" * 32), peer)
    assert not peer.sent
    node.status = NodeStatus.BlockSynced
    inv(node, block_inv(b"\x11" * 32), peer)
    assert [type(message) for message in peer.sent] == [GetHeaders]


def test_a_cmpctblock_is_dropped_unread_while_reindexing(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `LoadingBlocks()` return in `CMPCTBLOCK`."""
    node, _ = compact_node(regtest_node)
    block = next_block(node, generate_random_transaction())
    peer = a_compact_peer(node)
    node.status = NodeStatus.Reindexing
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert block.header.hash not in node.chainstate.block_index.header_dict
    assert not peer.sent
    node.status = NodeStatus.BlockSynced
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    assert block.header.hash in node.chainstate.block_index.header_dict


def test_a_blocktxn_is_dropped_unread_while_reindexing(
    regtest_node: Callable[[], Node],
) -> None:
    """Core's `LoadingBlocks()` return in `BLOCKTXN`."""
    node, _ = compact_node(regtest_node)
    missing = generate_random_transaction()
    block = next_block(node, missing)
    peer = a_compact_peer(node)
    cmpctblock(node, compact_block(block, 7).serialize(), peer)
    answer = BlockTxn(block.header.hash, [missing]).serialize()
    node.status = NodeStatus.Reindexing
    blocktxn(node, answer, peer)
    assert not held(node, block)
    node.status = NodeStatus.BlockSynced
    blocktxn(node, answer, peer)
    assert held(node, block)


def test_a_stale_tip_asks_for_no_extra_peer_while_reindexing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's `LoadingBlocks()` gate on a stale tip, which clears the flag."""
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    manager = an_extra_peer_manager([])
    manager.node.status = NodeStatus.Reindexing
    p2p_manager = cast("Any", manager.node).p2p_manager
    p2p_manager.try_new_outbound_peer = True
    manager.last_tip_update = now - 1801
    manager._check_for_stale_tip_and_evict_peers()
    assert not p2p_manager.try_new_outbound_peer
    manager.node.status = NodeStatus.BlockSynced
    manager._next_extra_peer_check = 0
    manager._stale_tip_check_time = 0
    manager._check_for_stale_tip_and_evict_peers()
    assert p2p_manager.try_new_outbound_peer


@pytest.mark.parametrize("batched", [False, True])
def test_delete_prefix_removes_that_prefix_and_no_other(
    tmp_path: Path, *, batched: bool
) -> None:
    """The keys either side of the prefix in key order stay."""
    store = KeyValueStore(tmp_path / "store")
    try:
        kept = [b"utxo,", b"utxo", b"utxo.", b"utxo.\x00"]
        removed = [b"utxo-", b"utxo-\x00", b"utxo-\xff\xff"]
        for key in kept + removed:
            store.put(key, b"v")
        if batched:
            with store.write_batch() as wb:
                wb.delete_prefix(b"utxo-")
                assert store.get(b"utxo-") == b"v"
        else:
            store.delete_prefix(b"utxo-")
        assert sorted(key for key, _ in store) == sorted(kept)
    finally:
        store.close()


def test_an_unpruned_reindex_of_a_pruned_directory_is_refused_before_it_wipes(
    tmp_path: Path,
) -> None:
    """ISS 1916: the refusal comes before `-reindex-chainstate`'s wipes.

    Core refuses at `src/node/chainstate.cpp:57`, before `InitCoinsDB`
    wipes. Here the chainstate and the undo data of the blocks pruning
    kept are what a wipe would destroy.
    """
    chain = generate_random_chain(5, RegTest().genesis.hash)
    with _node(tmp_path) as node:
        _store(node, chain, chain)
        activate_best_chain(node)
        prune_up_to_height(node, 2)
        before = _state(node)
    # the patches of heights 3 to 5, which a wipe would delete
    assert len(before.rev_patches) == 3

    refused = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            reindex_chainstate=True,
        )
    )
    try:
        with pytest.raises(ValueError, match="rebuild the database using -reindex"):
            refused.load()
    finally:
        refused._opened.close()
        refused.rpc_manager.loop.close()
        refused.logger.close()

    with _node(tmp_path, pruned=True) as node:
        assert node.block_db.pruned_up_to == 2
        assert _state(node) == before


def test_prune_refuses_the_option(tmp_path: Path) -> None:
    """Core's own refusal, in `Config` and from the command line."""
    with pytest.raises(ValueError, match=_PRUNE_REFUSAL):
        Config(chain="regtest", data_dir=tmp_path, pruned=True, reindex_chainstate=True)
    with pytest.raises(ValueError, match=_PRUNE_REFUSAL):
        build_config(
            ["-regtest", f"-datadir={tmp_path}", "-prune=1", "-reindex-chainstate"]
        )


@pytest.mark.parametrize(
    "args",
    [
        # a negative `-prune` is refused later, while it is parsed
        ["-prune=-1"],
        # refused later, by `AppInitParameterInteraction` itself
        ["-prune=1", "-forcednsseed", "-dnsseed=0"],
    ],
)
def test_prune_refuses_the_option_in_cores_order(
    tmp_path: Path, args: list[str]
) -> None:
    """The refusal comes before what Core refuses after it."""
    argv = ["-regtest", f"-datadir={tmp_path}", "-reindex-chainstate", *args]
    with pytest.raises(ValueError, match=_PRUNE_REFUSAL):
        build_config(argv)


def test_the_option_is_registered(tmp_path: Path) -> None:
    """It reaches `Config`, and the help names it."""
    config = build_config(["-regtest", f"-datadir={tmp_path}", "-reindex-chainstate"])
    assert config.reindex_chainstate is True
    assert build_config(["-regtest", f"-datadir={tmp_path}"]).reindex_chainstate is (
        False
    )
    message = " ".join(_help_message(show_debug=False).split())
    assert (
        "-reindex-chainstate If enabled, wipe chain state, and rebuild it from "
        "the blocks stored on disk." in message
    )
