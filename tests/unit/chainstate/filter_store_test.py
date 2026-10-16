# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The filters' store of their own, its locator, and the catch-up after it.

`filter_index`'s module docstring is what is checked: the filters are in
`indexes/blockfilter`, apart from the chainstate; the store's locator
sits on the active tip after a flush; a stop between the chainstate's
write and the filters' leaves the locator behind, and a reorg that stop
interrupted leaves it on the other branch, and the next start catches
up from the fork point; a datadir whose filters are still in the
chainstate's store is moved; `-reindex-chainstate` keeps the store; a start
whose catch-up fails leaves the locator where the index had got to.
"""

import shutil
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

import btclib_node.chainstate.filter_index as filter_index_module
from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.filter_index import FilterIndex
from btclib_node.config import Config
from btclib_node.db import KeyValueStore
from btclib_node.exceptions import ChainstateInconsistencyError
from btclib_node.log import Logger
from btclib_node.main import prune_up_to_height, update_chain
from tests import generate_random_chain
from tests.unit.chainstate.filter_index_test import a_chain
from tests.unit.main_test import connect

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from btclib.block import Block

GENESIS = RegTest().genesis
_BEST_BLOCK = b"best-block"


class Crash(Exception):  # noqa: N818
    """Stands in for the process dying where it is raised."""


def stored_filters(path: Path) -> dict[bytes, bytes]:
    """Read every filter of the closed store at `path`, by block hash."""
    store = KeyValueStore(path)
    try:
        return {
            key[len(b"cfilter-") :]: value
            for key, value in store.scan_prefix(b"cfilter-")
        }
    finally:
        store.close()


def locator(path: Path) -> bytes | None:
    """Read the locator of the closed store at `path`."""
    store = KeyValueStore(path)
    try:
        return store.get_meta(_BEST_BLOCK)
    finally:
        store.close()


def stop(node: Node) -> None:
    """Close the stores of `node` without the flush `Chainstate.close` makes."""
    node.p2p_manager.peer_db.close()
    node.chainstate.db.close()
    node.chainstate.filter_db.close()
    node.block_db.close()


def filters_dir(node: Node) -> Path:
    """Return the directory of the filters' store."""
    return node.data_dir / "indexes" / "blockfilter"


def stage(node: Node, chain: list[Block]) -> None:
    """Connect `chain` in memory, flushing nothing."""
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(node)


def test_the_filters_are_in_a_store_of_their_own(
    regtest_node: Callable[[], Node],
) -> None:
    """A block's filter is in `indexes/blockfilter`, not in the chainstate."""
    node = regtest_node()
    chain = a_chain(node, 2)
    for block in chain:
        key = b"cfilter-" + block.header.hash
        assert node.chainstate.filter_db.get(key) is not None
        assert node.chainstate.db.get(key) is None
        assert node.chainstate.db.get(b"cfheader-" + block.header.hash) is None
    assert filters_dir(node).is_dir()


def test_a_flush_puts_the_locator_on_the_active_tip(
    regtest_node: Callable[[], Node],
) -> None:
    """After `Chainstate.flush` the stored locator is the active tip."""
    node = regtest_node()
    chain = a_chain(node, 3)
    tip = chain[-1].header.hash
    assert node.chainstate.filter_index.best_block == tip
    assert node.chainstate.filter_db.get_meta(_BEST_BLOCK) == tip


def test_a_fresh_store_is_located_on_genesis(
    regtest_node: Callable[[], Node],
) -> None:
    """With no block connected the locator is genesis, whose filter is built."""
    node = regtest_node()
    assert node.chainstate.filter_index.best_block == GENESIS.hash
    assert node.chainstate.filter_db.get_meta(_BEST_BLOCK) == GENESIS.hash


def test_a_stop_between_the_chainstate_write_and_the_filters_is_caught_up(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop between the two writes is caught up at the next start.

    The process dies after the chainstate's batch and before the
    filters': the filter store is as the last flush left it, and the
    block index holds the new tip. The reopened node builds the filters
    the stop cost, identical to the ones that were lost, and moves the
    locator onto the tip.
    """
    node = regtest_node()
    chain = generate_random_chain(5, GENESIS.hash)
    first, second = chain[:2], chain[2:]
    connect(node, first)
    stage(node, second)
    lost = {
        block.header.hash: node.chainstate.filter_index.get_filter(block.header.hash)
        for block in second
    }
    assert None not in lost.values()

    def die(*_args: object, **_kwargs: object) -> None:
        raise Crash

    monkeypatch.setattr(node.chainstate.filter_index, "finalize", die)
    with pytest.raises(Crash):
        node.chainstate.flush()
    stop(node)

    path = filters_dir(node)
    assert locator(path) == first[-1].header.hash
    assert not set(lost) & set(stored_filters(path))

    reopened = regtest_node()
    tip = second[-1].header.hash
    assert reopened.chainstate.block_index.active_chain[-1] == tip
    assert reopened.chainstate.filter_index.best_block == tip
    for block_hash, filter_bytes in lost.items():
        assert reopened.chainstate.filter_index.get_filter(block_hash) == filter_bytes


def test_a_write_that_dies_inside_its_batch_leaves_the_store_as_it_was(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filters and the locator are one batch: all of it lands, or none."""
    node = regtest_node()
    chain = generate_random_chain(3, GENESIS.hash)
    first, second = chain[:1], chain[1:]
    connect(node, first)
    stage(node, second)

    def die(*_args: object, **_kwargs: object) -> None:
        raise Crash

    monkeypatch.setattr(node.chainstate.filter_db, "put_meta", die)
    with pytest.raises(Crash):
        node.chainstate.flush()
    stop(node)

    path = filters_dir(node)
    assert locator(path) == first[-1].header.hash
    stored = stored_filters(path)
    assert first[0].header.hash in stored
    assert not {block.header.hash for block in second} & set(stored)


def test_a_reorg_moves_the_locator_onto_the_new_branch(
    regtest_node: Callable[[], Node],
) -> None:
    """A reorg keeps both branches' filters and puts the locator on the tip."""
    node = regtest_node()
    old = a_chain(node, 2)
    new = generate_random_chain(3, GENESIS.hash)
    connect(node, new)
    block_index = node.chainstate.block_index
    assert block_index.active_chain[1:] == [block.header.hash for block in new]

    filter_index = node.chainstate.filter_index
    assert filter_index.best_block == new[-1].header.hash
    assert node.chainstate.filter_db.get_meta(_BEST_BLOCK) == new[-1].header.hash
    for block in [*old, *new]:
        assert filter_index.get_filter(block.header.hash) is not None


def test_a_stop_that_interrupts_a_reorg_is_caught_up_from_the_fork_point(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locator on the abandoned branch costs only the new branch's filters.

    The chainstate holds the longer branch and the filter store is
    located on the shorter one, the two meeting at genesis. The reopened
    node builds the filters of the new branch from there and keeps the
    old branch's.
    """
    node = regtest_node()
    old = a_chain(node, 2)
    new = generate_random_chain(3, GENESIS.hash)
    stage(node, new)
    assert node.chainstate.block_index.active_chain[-1] == new[-1].header.hash
    built = {
        block.header.hash: node.chainstate.filter_index.get_filter(block.header.hash)
        for block in new
    }

    def die(*_args: object, **_kwargs: object) -> None:
        raise Crash

    monkeypatch.setattr(node.chainstate.filter_index, "finalize", die)
    with pytest.raises(Crash):
        node.chainstate.flush()
    stop(node)
    assert locator(filters_dir(node)) == old[-1].header.hash

    reopened = regtest_node()
    filter_index = reopened.chainstate.filter_index
    assert filter_index.best_block == new[-1].header.hash
    for block_hash, filter_bytes in built.items():
        assert filter_index.get_filter(block_hash) == filter_bytes
    for block in old:
        assert filter_index.get_filter(block.header.hash) is not None


def test_a_catch_up_resumes_from_the_locator_not_from_genesis(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block below the locator is not even looked at."""
    node = regtest_node()
    chain = a_chain(node, 3)
    filter_index = node.chainstate.filter_index
    asked: list[bytes] = []
    real = filter_index.get_filter

    def watched(block_hash: bytes) -> bytes | None:
        asked.append(block_hash)
        return real(block_hash)

    filter_index.best_block = chain[0].header.hash
    monkeypatch.setattr(filter_index, "get_filter", watched)
    assert filter_index.catch_up(node.chainstate.block_index, node.block_db) == 0
    assert asked == [chain[1].header.hash, chain[2].header.hash]
    assert filter_index.best_block == chain[-1].header.hash


def test_a_locator_the_block_index_does_not_know_is_refused(
    regtest_node: Callable[[], Node],
) -> None:
    """A locator the block index lacks is refused, as in Core."""
    node = regtest_node()
    filter_index = node.chainstate.filter_index
    filter_index.best_block = b"\x01" * 32
    with pytest.raises(ChainstateInconsistencyError, match="indexes/blockfilter"):
        filter_index.catch_up(node.chainstate.block_index, node.block_db)


def legacy_datadir(node: Node, chain: list[Block]) -> dict[bytes, tuple[bytes, bytes]]:
    """Put the filters in the chainstate's store, as an old datadir has them."""
    found = {}
    for block_hash in [GENESIS.hash, *[block.header.hash for block in chain]]:
        filter_bytes = node.chainstate.filter_db.get(b"cfilter-" + block_hash)
        header = node.chainstate.filter_db.get(b"cfheader-" + block_hash)
        assert filter_bytes is not None
        assert header is not None
        node.chainstate.db.put(b"cfilter-" + block_hash, filter_bytes)
        node.chainstate.db.put(b"cfheader-" + block_hash, header)
        found[block_hash] = (filter_bytes, header)
    return found


def test_a_datadir_with_the_filters_in_the_chainstate_is_migrated(
    regtest_node: Callable[[], Node],
) -> None:
    """An older datadir's filters move to the new store and leave the old."""
    node = regtest_node()
    chain = a_chain(node, 3)
    expected = legacy_datadir(node, chain)
    # the node is stopped as an old build would have left it: the filter
    # store did not exist, and the locator with it
    stop(node)
    shutil.rmtree(filters_dir(node))

    reopened = regtest_node()
    filter_index = reopened.chainstate.filter_index
    for block_hash, (filter_bytes, header) in expected.items():
        assert filter_index.get_filter(block_hash) == filter_bytes
        assert filter_index.get_header(block_hash) == header
        assert reopened.chainstate.db.get(b"cfilter-" + block_hash) is None
        assert reopened.chainstate.db.get(b"cfheader-" + block_hash) is None
    assert filter_index.best_block == chain[-1].header.hash


@contextmanager
def a_chainstate(tmp_path: Path, **kwargs: bool) -> Iterator[Chainstate]:
    """Open a chainstate of the regtest chain on `tmp_path`, closed after."""
    logger = Logger(tmp_path / "history.log", debug=True)
    chainstate = Chainstate(tmp_path, RegTest(), logger, **kwargs)
    try:
        yield chainstate
    finally:
        chainstate.close()
        logger.close()


def test_a_migration_that_stopped_after_the_copy_runs_again(tmp_path: Path) -> None:
    """Copying twice changes nothing: the old keys are removed last."""
    with a_chainstate(tmp_path) as chainstate:
        old = {
            key: value
            for key, value in chainstate.filter_db
            if key.startswith((b"cfilter-", b"cfheader-"))
        }
        for key, value in old.items():
            chainstate.db.put(key, value)
        logger = chainstate.logger
        db = chainstate.db
        assert FilterIndex.adopt_legacy(db, chainstate.filter_db, logger) == 1
        # the removal never ran
        for key, value in old.items():
            db.put(key, value)
        assert FilterIndex.adopt_legacy(db, chainstate.filter_db, logger) == 1
        assert dict(chainstate.filter_db.scan_prefix(b"cf")) == old


def test_half_a_legacy_pair_is_dropped(tmp_path: Path) -> None:
    """A header with no filter, and a filter with no header, move nothing."""
    with a_chainstate(tmp_path) as chainstate:
        db = chainstate.db
        db.put(b"cfheader-" + b"\x07" * 32, b"\x00" * 32)
        db.put(b"cfilter-" + b"\x08" * 32, b"\x00")
        assert (
            FilterIndex.adopt_legacy(db, chainstate.filter_db, chainstate.logger) == 0
        )
        assert db.get(b"cfheader-" + b"\x07" * 32) is None
        assert db.get(b"cfilter-" + b"\x08" * 32) is None
        assert chainstate.filter_db.get(b"cfilter-" + b"\x08" * 32) is None


def test_a_header_alone_is_enough_to_run_the_migration(tmp_path: Path) -> None:
    """The filter prefix being empty does not hide a header left behind."""
    with a_chainstate(tmp_path) as chainstate:
        db = chainstate.db
        db.put(b"cfheader-" + b"\x07" * 32, b"\x00" * 32)
        FilterIndex.adopt_legacy(db, chainstate.filter_db, chainstate.logger)
        assert db.get(b"cfheader-" + b"\x07" * 32) is None


def test_a_migration_copies_in_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy writes a batch at a time rather than holding the whole index."""
    monkeypatch.setattr(filter_index_module, "_CATCH_UP_BATCH", 2)
    with a_chainstate(tmp_path) as chainstate:
        db = chainstate.db
        batches: list[int] = []
        real = chainstate.filter_db.write_batch

        @contextmanager
        def counting() -> Iterator[KeyValueStore]:
            with real() as wb:
                batches.append(1)
                yield wb

        monkeypatch.setattr(chainstate.filter_db, "write_batch", counting)
        for index in range(5):
            block_hash = bytes([index]) * 32
            db.put(b"cfilter-" + block_hash, b"\x00")
            db.put(b"cfheader-" + block_hash, bytes([index]) * 32)
        moved = FilterIndex.adopt_legacy(db, chainstate.filter_db, chainstate.logger)
        assert moved == 5
        assert len(batches) == 3


def test_reindex_chainstate_keeps_the_filters(tmp_path: Path) -> None:
    """The chainstate is wiped and the filter store is not, as in Core."""
    with a_chainstate(tmp_path) as chainstate:
        chainstate.filter_db.put(b"cfilter-" + b"\x09" * 32, b"\x01")

    with a_chainstate(tmp_path, wipe=True) as wiped:
        assert wiped.filter_db.get(b"cfilter-" + b"\x09" * 32) == b"\x01"


def started(tmp_path: Path, *, pruned: bool = False) -> Node:
    """Build a node on `tmp_path` and run `_load_or_abort` on it."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            pruned=pruned,
        )
    )
    node.loaded_ok = node._load_or_abort()  # type: ignore[attr-defined]
    return node


def put_away(node: Node) -> None:
    """Close what a node `_load_or_abort` ran on holds."""
    if node.loaded_ok:  # type: ignore[attr-defined]
        node.chainstate.close()
        node.block_db.close()
        node.p2p_manager.peer_db.close()
    node._close_worker_pool()
    node.p2p_manager.loop.close()
    node.rpc_manager.loop.close()
    node.logger.close()


def test_a_start_that_fails_catching_up_does_not_move_the_locator(
    regtest_node: Callable[..., Node],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flush on the way out of a failed start leaves the locator behind.

    With the filter store deleted and the catch-up failing, the chainstate
    closes with the tip ahead of the filters. The locator must not claim
    the tip, or the next start would trust filters that are not there.
    """
    node = regtest_node()
    chain = a_chain(node, 3)
    stop(node)
    shutil.rmtree(filters_dir(node))

    def fail(*_args: object, **_kwargs: object) -> int:
        err_msg = "catch-up failed"
        raise ChainstateInconsistencyError(err_msg)

    with monkeypatch.context() as patch:
        patch.setattr(FilterIndex, "catch_up", fail)
        failed = started(tmp_path)
        assert not failed.loaded_ok  # type: ignore[attr-defined]
        put_away(failed)
    assert locator(filters_dir(node)) == GENESIS.hash
    assert not {b.header.hash for b in chain} & set(stored_filters(filters_dir(node)))

    again = started(tmp_path)
    assert again.loaded_ok  # type: ignore[attr-defined]
    filter_index = again.chainstate.filter_index
    assert filter_index.best_block == chain[-1].header.hash
    for block in chain:
        assert filter_index.get_filter(block.header.hash) is not None
    put_away(again)


def test_a_pruned_node_missing_its_filters_refuses_every_start(
    regtest_node: Callable[..., Node], tmp_path: Path
) -> None:
    """The blocks are gone, so no start may pretend the filters are there."""
    node = regtest_node(pruned=True)
    chain = generate_random_chain(4, GENESIS.hash)
    connect(node, chain)
    prune_up_to_height(node, 1)
    stop(node)
    shutil.rmtree(filters_dir(node))

    for _ in range(2):
        refused = started(tmp_path, pruned=True)
        assert not refused.loaded_ok  # type: ignore[attr-defined]
        assert "cannot build the block filter index" in refused.init_errors[0]
        put_away(refused)
    assert locator(filters_dir(node)) == GENESIS.hash
