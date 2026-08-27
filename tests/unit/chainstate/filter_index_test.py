# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The filter of every connected block, and the header chaining it.

The arithmetic is `btclib.block.block_filter`'s and is tested there.
What is this node's is which blocks are indexed, what the filter of one
is built from -- a block does not carry the outputs it spends -- and
that the header chain a peer would check against is the one BIP157
defines.
"""

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from btclib.block import Block
from btclib.block.block_filter import BasicBlockFilter, filter_header
from btclib.script import script

import btclib_node.chainstate.filter_index as filter_index_module
from btclib_node import Node
from btclib_node.chains import RegTest, TestNet
from btclib_node.exceptions import ChainstateInconsistencyError
from btclib_node.main import update_chain
from tests import (
    build_block,
    generate_coinbase,
    generate_random_chain,
    load,
    vector_id,
)
from tests.unit.main_test import connect, spend

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from btclib_node.block_db import RevBlock
    from btclib_node.db import KeyValueStore

GENESIS = RegTest().genesis
NO_PREVIOUS = b"\x00" * 32


def offer(node: Node, chain: list[Block]) -> None:
    """Give the node the headers and the blocks, and connect nothing."""
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        node.block_db.add_block(block)


def a_chain(node: Node, length: int) -> list[Block]:
    """Build `length` random blocks off the genesis and connect them all."""
    chain = generate_random_chain(length, GENESIS.hash)
    connect(node, chain)
    return chain


def recomputed_header(node: Node, block_hash: bytes) -> bytes:
    """Chain the filter headers from genesis the way a peer would."""
    active_chain = node.chainstate.block_index.active_chain
    filter_index = node.chainstate.filter_index
    header = NO_PREVIOUS
    for hash_ in active_chain[: active_chain.index(block_hash) + 1]:
        filter_hash = filter_index.get_filter_hash(hash_)
        assert filter_hash is not None
        header = filter_header(filter_hash, header)
    return header


def test_the_genesis_filter_is_indexed_before_any_block_arrives(
    regtest_node: Callable[[], Node],
) -> None:
    """A fresh node's filter index already holds the genesis block's filter.

    No peer serves the genesis block, so it never reaches this index
    through the connect path -- `FilterIndex.__init__` builds it
    itself, the one filter every other test here can chain onto.
    """
    node = regtest_node()
    filter_index = node.chainstate.filter_index
    # no peer serves the genesis block, so it is not indexed by the
    # connect path and would be the one hole in the chain of headers
    genesis_filter = filter_index.get_filter(GENESIS.hash)
    assert genesis_filter is not None
    block_filter = BasicBlockFilter.parse(genesis_filter, GENESIS.hash)
    assert block_filter == BasicBlockFilter.from_block(RegTest().genesis_block, [])
    assert filter_index.get_header(GENESIS.hash) == filter_header(
        block_filter.hash, NO_PREVIOUS
    )


def test_a_connected_block_is_indexed_as_it_connects(
    regtest_node: Callable[[], Node],
) -> None:
    """Every block of a chain that connects gets its own filter indexed."""
    node = regtest_node()
    chain = a_chain(node, 3)
    filter_index = node.chainstate.filter_index
    for block in chain:
        assert filter_index.get_filter(block.header.hash) is not None


def test_the_filter_holds_what_the_block_pays_to_and_what_it_spends(
    regtest_node: Callable[[], Node],
) -> None:
    """A block's filter matches both its own outputs and the ones it spends.

    The spent output is paid in the block before it, so a filter built
    from the spending block alone would miss it, and a client watching
    that address would never be told to fetch the block that emptied it.
    """
    node = regtest_node()
    chain = a_chain(node, 2)
    spending = chain[1]
    filter_index = node.chainstate.filter_index
    spending_filter = filter_index.get_filter(spending.header.hash)
    assert spending_filter is not None
    block_filter = BasicBlockFilter.parse(spending_filter, spending.header.hash)

    paid_to = [out.script_pub_key.script for out in spending.transactions[0].vout]
    assert block_filter.match_any(paid_to)
    # and the output it spends, which is in the block before it: a
    # filter built from the block alone would miss every input, and a
    # client watching an address would never be told to fetch the block
    # that emptied it
    spent = [out.script_pub_key.script for out in chain[0].transactions[0].vout]
    assert block_filter.match_any(spent)
    assert not block_filter.match(b"\x51" * 20)


def test_every_header_chains_onto_the_one_before_it(
    regtest_node: Callable[[], Node],
) -> None:
    """Each block's filter header matches the chain a peer would recompute.

    recomputed_header rebuilds it from the genesis forward the way BIP157
    defines, so a match confirms `get_header`'s stored value is chained
    correctly rather than merely present.
    """
    node = regtest_node()
    chain = a_chain(node, 4)
    filter_index = node.chainstate.filter_index
    for block in chain:
        block_hash = block.header.hash
        assert filter_index.get_header(block_hash) == recomputed_header(
            node, block_hash
        )


def test_a_block_whose_parent_has_no_filter_is_refused(
    regtest_node: Callable[[], Node],
) -> None:
    """add_block on a block whose parent is not indexed raises.

    The header chain add_block builds needs the parent's own header
    already stored; a block off an unrelated hash has none.
    """
    node = regtest_node()
    orphan = generate_random_chain(1, b"\x11" * 32)[0]
    with pytest.raises(
        ChainstateInconsistencyError, match="no filter header for the parent"
    ):
        node.chainstate.filter_index.add_block(orphan, [])


def test_a_block_already_indexed_is_not_built_twice(
    regtest_node: Callable[[], Node],
) -> None:
    """add_block on an already-indexed block is a no-op, leaving pending empty.

    A second build would use whatever prevouts this second call was
    given, which could answer a different filter; the stored one is
    the one that stays.
    """
    node = regtest_node()
    (block,) = a_chain(node, 1)
    filter_index = node.chainstate.filter_index
    filter_index.add_block(block, [])
    # the second call would build from the wrong prevouts and answer a
    # different filter; the stored one is the one that stays
    assert not filter_index.pending


def test_a_block_offered_twice_before_the_batch_is_written_is_built_once(
    regtest_node: Callable[[], Node],
) -> None:
    """A block offered twice while still only pending keeps its first build.

    Blocks connect in one write batch, so what is held for writing is
    what a second offer of the same block has to be answered from:
    rebuilding it would use whatever prevouts the second caller had.
    """
    node = regtest_node()
    filter_index = node.chainstate.filter_index
    (block,) = generate_random_chain(1, GENESIS.hash)
    filter_index.add_block(block, [])
    built = filter_index.pending[block.header.hash]
    filter_index.add_block(block, [])
    assert filter_index.pending[block.header.hash] is built


def test_what_is_pending_is_dropped_on_a_rollback(
    regtest_node: Callable[[], Node],
) -> None:
    """`rollback` discards a pending block's filter, leaving nothing indexed."""
    node = regtest_node()
    filter_index = node.chainstate.filter_index
    (block,) = generate_random_chain(1, GENESIS.hash)
    filter_index.add_block(block, [])
    assert filter_index.pending
    filter_index.rollback()
    assert not filter_index.pending
    assert filter_index.get_filter(block.header.hash) is None


def test_a_block_stepped_over_keeps_the_filter_it_had(
    regtest_node: Callable[[], Node],
) -> None:
    """A reorg leaves a stepped-over branch's own filters untouched.

    A filter is a function of its block and its ancestry, neither of
    which a reorg changes, so there is nothing here to undo -- and a
    branch that comes back is not rebuilt.
    """
    node = regtest_node()
    short = a_chain(node, 2)
    filter_index = node.chainstate.filter_index
    before = [filter_index.get_header(b.header.hash) for b in short]

    longer = generate_random_chain(4, GENESIS.hash)
    connect(node, longer)
    assert node.chainstate.block_index.active_chain[-1] == longer[-1].header.hash

    after = [filter_index.get_header(b.header.hash) for b in short]
    assert after == before
    for block in longer:
        assert filter_index.get_header(block.header.hash) is not None


def test_nothing_is_answered_for_a_block_that_is_not_indexed(
    regtest_node: Callable[[], Node],
) -> None:
    """Every getter answers `None` for a hash the index has never seen."""
    node = regtest_node()
    filter_index = node.chainstate.filter_index
    unknown = b"\x11" * 32
    assert filter_index.get_filter(unknown) is None
    assert filter_index.get_header(unknown) is None
    assert filter_index.get_filter_hash(unknown) is None


def test_a_chain_indexed_before_the_index_existed_is_caught_up(
    regtest_node: Callable[[], Node],
) -> None:
    """catch_up rebuilds every filter and header a chain is missing, unchanged.

    With the whole chain's filters and headers deleted, catch_up
    reports building all of them and writes them rather than only
    holding them pending, and each rebuilt header matches what was
    stored the first time.
    """
    node = regtest_node()
    chain = a_chain(node, 3)
    filter_index = node.chainstate.filter_index
    active_chain = node.chainstate.block_index.active_chain

    built_first_time = {
        block.header.hash: filter_index.get_header(block.header.hash) for block in chain
    }
    for block in chain:
        filter_index.db.delete(b"cfilter-" + block.header.hash)
        filter_index.db.delete(b"cfheader-" + block.header.hash)

    assert filter_index.catch_up(active_chain, node.block_db) == len(chain)
    # written, not held: nothing calls finalize after this, so a
    # catch-up that only filled `pending` would be lost on the next
    # rollback and would never reach the disk
    assert not filter_index.pending
    for block_hash, header in built_first_time.items():
        assert filter_index.db.get(b"cfheader-" + block_hash) == header
        # the same filters, so a client that checked this node's header
        # chain before the rebuild is not told a different story after
        assert filter_index.get_header(block_hash) == header


def test_a_chain_that_is_already_indexed_is_not_rebuilt(
    regtest_node: Callable[[], Node],
) -> None:
    """catch_up on a fully indexed chain builds nothing and returns 0."""
    node = regtest_node()
    a_chain(node, 2)
    assert (
        node.chainstate.filter_index.catch_up(
            node.chainstate.block_index.active_chain, node.block_db
        )
        == 0
    )


def test_a_catch_up_that_cannot_reach_a_block_says_so(
    regtest_node: Callable[[], Node],
) -> None:
    """catch_up raises when a block on the active chain has no octets stored.

    The block is on the active chain and its octets are not on disk,
    so no filter can be built for it and none for anything after it:
    the next block's header chains onto this one's.
    """
    node = regtest_node()
    (block,) = a_chain(node, 1)
    filter_index = node.chainstate.filter_index
    filter_index.db.delete(b"cfilter-" + block.header.hash)
    filter_index.db.delete(b"cfheader-" + block.header.hash)
    block_db = node.block_db
    block_db.blocks.pop(block.header.hash)

    with pytest.raises(
        ChainstateInconsistencyError, match="cannot build the block filter index"
    ):
        filter_index.catch_up(node.chainstate.block_index.active_chain, block_db)


def test_an_index_survives_the_node_being_closed_and_opened(
    regtest_node: Callable[[], Node],
) -> None:
    """A filter header persists across the node closing and reopening."""
    node = regtest_node()
    (block,) = a_chain(node, 1)
    header = node.chainstate.filter_index.get_header(block.header.hash)
    node.chainstate.close()
    node.block_db.close()

    reopened = regtest_node()
    assert reopened.chainstate.filter_index.get_header(block.header.hash) == header
    reopened.chainstate.close()
    reopened.block_db.close()


def test_a_filter_index_reads_only_its_own_keys(
    regtest_node: Callable[[], Node],
) -> None:
    """The block index still reads its own chain with a filter index present.

    BlockIndex.init_from_db walks the whole database and stops at the
    first key that is not a `blkinfo-`, so what the filter index writes
    has to sort after those or the block index would stop short of its
    own chain.
    """
    node = regtest_node()
    a_chain(node, 1)
    node.chainstate.close()
    node.block_db.close()

    reopened = regtest_node()
    # genesis plus the one block: the index read everything of its own
    assert len(reopened.chainstate.block_index.active_chain) == 2
    reopened.chainstate.close()
    reopened.block_db.close()


def test_the_index_is_caught_up_before_the_node_is_built(
    regtest_node: Callable[[], Node],
) -> None:
    """Every filter missing at start-up is rebuilt before Node exists.

    Node.__init__ is where the two databases meet, and a node that
    advertises the service bit with a half-built index would be
    promising filters it cannot serve.
    """
    node = regtest_node()
    chain = a_chain(node, 2)
    filter_index = node.chainstate.filter_index
    for block in chain:
        filter_index.db.delete(b"cfilter-" + block.header.hash)
        filter_index.db.delete(b"cfheader-" + block.header.hash)
    node.chainstate.close()
    node.block_db.close()

    reopened = regtest_node()
    for block in chain:
        assert (
            reopened.chainstate.filter_index.get_filter(block.header.hash) is not None
        )
    reopened.chainstate.close()
    reopened.block_db.close()


def test_a_block_that_does_not_connect_leaves_no_filter_behind(
    regtest_node: Callable[[], Node],
) -> None:
    """A block that fails validation leaves no pending or stored filter.

    The index is written in the chainstate's own write batch, so a
    block that fails validation has to leave it as it was: a filter
    held for a block the chain does not have would be answered to a
    peer asking about the block that did connect at that height.
    """
    node = regtest_node()
    chain = a_chain(node, 2)
    filter_index = node.chainstate.filter_index

    funding = chain[-1].transactions[0]
    unspendable = spend(
        funding, funding.vout[0].value, script_sig=script.serialize(["OP_RETURN"])
    )
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), unspendable],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in node.chainstate.block_index.active_chain
    assert not filter_index.pending
    assert filter_index.get_filter(bad.header.hash) is None
    assert filter_index.get_header(bad.header.hash) is None


def test_a_batch_that_fails_partway_leaves_nothing_of_the_blocks_before_it(
    regtest_node: Callable[[], Node],
) -> None:
    """A batch whose last block fails leaves no filter for any block in it.

    Several blocks reach the chainstate in one write batch only as a
    fork: a block on the tip is a candidate on its own and connects by
    itself. So the node is put on a short chain and offered a heavier
    branch whose last block does not validate -- the two before it are
    indexed and pending when it raises, and the whole batch has to go,
    filters included.
    """
    node = regtest_node()
    a_chain(node, 2)
    on_chain = list(node.chainstate.block_index.active_chain)

    branch = generate_random_chain(2, GENESIS.hash)
    funding = branch[-1].transactions[0]
    unspendable = spend(
        funding, funding.vout[0].value, script_sig=script.serialize(["OP_RETURN"])
    )
    bad = build_block(
        branch[-1].header.hash,
        [generate_coinbase(height=len(branch) + 1), unspendable],
        len(branch),
    )
    offer(node, [*branch, bad])

    update_chain(node)

    filter_index = node.chainstate.filter_index
    assert node.chainstate.block_index.active_chain == on_chain
    assert not filter_index.pending
    for block in (*branch, bad):
        assert filter_index.get_filter(block.header.hash) is None


# Bitcoin Core's BIP158 vectors, `src/test/data/blockfilters.json`; the
# revision is pinned in tests/_data/README.md. The header row of column
# names is dropped, and each row after it is a height, a block hash, the
# whole serialized block, the previous output scripts the block does not
# carry, the previous basic filter header, the basic filter and the
# basic header. Every hash in the file is in display order, which is the
# order everything here holds one in.
_CORE_VECTORS = load("unit", "chainstate", "_data", "blockfilters.json")[1:]
_VECTOR_IDS = [
    vector_id(index, row[0], row[7] if len(row) > 7 else "")
    for index, row in enumerate(_CORE_VECTORS)
]


def _index_a_vector_and_check_it(node: Node, vector: list[str]) -> None:
    """Index one row of either vector file and check its filter and header.

    Shared by both parametrized tests below: the row shape is the same,
    and what differs between the two files is where the numbers being
    checked against came from, which each test's own docstring says.
    """
    _, block_hash, block_hex, prevout_scripts, previous, expected, header = vector[:7]
    block = Block.parse(bytes.fromhex(block_hex), check_validity=False)
    assert block.header.hash.hex() == block_hash

    filter_index = node.chainstate.filter_index
    # a testnet block whose parent this node has never connected, so the
    # header before it comes from the row: what is under test here is
    # the chaining and the storage, not where the parent came from
    filter_index.db.put(
        b"cfheader-" + block.header.previous_block_hash, bytes.fromhex(previous)
    )
    filter_index.add_block(block, [bytes.fromhex(script) for script in prevout_scripts])
    filter_index.finalize()

    stored_filter = filter_index.get_filter(block.header.hash)
    stored_header = filter_index.get_header(block.header.hash)
    assert stored_filter is not None
    assert stored_header is not None
    assert stored_filter.hex() == expected
    assert stored_header.hex() == header
    assert (
        filter_index.get_filter_hash(block.header.hash)
        == BasicBlockFilter.parse(bytes.fromhex(expected), block.header.hash).hash
    )


@pytest.mark.parametrize("vector", _CORE_VECTORS, ids=_VECTOR_IDS)
def test_the_index_holds_the_filters_bitcoin_core_computed(
    regtest_node: Callable[[], Node], vector: list[str]
) -> None:
    """Hold the index to Core's numbers rather than to btclib's.

    `btclib.block.block_filter` is tested against this same file inside
    btclib, and what is checked here is the layer this tree adds: that
    the octets stored for a block are the filter Core computed, that
    they come back unchanged, and that the header chained onto the one
    before is Core's header. A filter that goes into this index and
    comes out a different filter is a node telling a light client
    something it cannot catch until it fetches the block.
    """
    _index_a_vector_and_check_it(regtest_node(), vector)


# Two testnet blocks Core's own file above does not reach the scale of:
# height 54499 is forty-odd kilobytes and twenty-four transactions, most
# of them resolving a previous output from elsewhere in the same block,
# and height 54503 is the positive control. tests/_data/README.md says
# where the two blocks came from and how the filters were derived; the
# "previous" and "header" columns are this file's own, chained within
# it alone and read by nothing outside this module.
_SCALE_VECTORS = load("unit", "chainstate", "_data", "testnet_bip158_vectors.json")[1:]
_SCALE_VECTOR_IDS = [
    vector_id(index, row[0], row[7] if len(row) > 7 else "")
    for index, row in enumerate(_SCALE_VECTORS)
]


@pytest.mark.parametrize("vector", _SCALE_VECTORS, ids=_SCALE_VECTOR_IDS)
def test_the_index_holds_the_filters_this_tree_derived_at_scale(
    regtest_node: Callable[[], Node], vector: list[str]
) -> None:
    """Hold the index to a scale Core's own vector file never reaches.

    Neither block's filter was taken on faith: height 54503's,
    `06294070f18c8b0ff84b92738259ca89b4`, matches what an independent
    SipHash-2-4 and Golomb-Rice implementation in Libbitcoin's test
    suite computed for the same block, which is the positive control
    tests/_data/README.md names. Height 54499 has no such external
    check -- most of its non-coinbase inputs spend an output paid to
    earlier in the same block, which is the scenario none of Core's
    ten rows reaches at all.
    """
    _index_a_vector_and_check_it(regtest_node(), vector)


def test_the_testnet_genesis_block_this_node_builds_is_the_one_core_filtered() -> None:
    """The testnet genesis block this node builds matches Core's, byte for byte.

    chains.py builds the genesis block out of a coinbase it writes
    rather than parsing one, and row 0 of Core's file is that block:
    what the filter says is that the transaction this node made is the
    transaction the network has. It is the one block a peer never
    serves, so nothing else would ever catch it being wrong.
    """
    _, block_hash, block_hex, _, previous, expected, header = _CORE_VECTORS[0][:7]
    genesis_block = TestNet().genesis_block
    assert genesis_block.header.hash.hex() == block_hash
    assert genesis_block.serialize(check_validity=False).hex() == block_hex

    block_filter = BasicBlockFilter.from_block(genesis_block, [])
    assert block_filter.serialize().hex() == expected
    assert block_filter.header(previous).hex() == header


def test_the_header_is_written_before_the_filter(
    regtest_node: Callable[[], Node],
) -> None:
    """`_write` puts a block's cfheader key before its cfilter key.

    Both skip guards ask get_filter, so a filter on disk without its
    header is the state nothing repairs: the block is skipped for
    ever, its child cannot be indexed, and the datadir will not open.
    The pair goes in one atomic write, and the order is what keeps the
    survivable half survivable if it ever stops being one.
    """
    node = regtest_node()
    filter_index = node.chainstate.filter_index
    (block,) = generate_random_chain(1, GENESIS.hash)
    filter_index.add_block(block, [])

    written: list[bytes] = []
    filter_index._write(
        cast(
            "KeyValueStore",
            SimpleNamespace(put=lambda key, _: written.append(key[:-32])),
        )
    )
    assert written == [b"cfheader-", b"cfilter-"]


def test_the_pair_reaches_the_database_as_one_write(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`finalize` reaches the database exactly once for one pending block."""
    node = regtest_node()
    filter_index = node.chainstate.filter_index
    (block,) = generate_random_chain(1, GENESIS.hash)
    filter_index.add_block(block, [])

    real = filter_index.db
    batches: list[bool] = []

    class CountingDb:
        """The chainstate database, counting the batches asked of it."""

        def write_batch(self) -> AbstractContextManager[KeyValueStore]:
            batches.append(True)
            return real.write_batch()

    monkeypatch.setattr(filter_index, "db", CountingDb())
    try:
        filter_index.finalize()
    finally:
        filter_index.db = real

    # one batch, and one that rolls back rather than half-applying
    assert batches == [True]
    assert filter_index.get_filter(block.header.hash) is not None
    assert filter_index.get_header(block.header.hash) is not None


def test_a_header_left_without_its_filter_is_simply_rebuilt(
    regtest_node: Callable[[], Node],
) -> None:
    """A header left without its filter is rebuilt by catch_up, unchanged.

    The survivable half of the pair, and the reason the write order in
    finalize is the one it is: the block is built again and answers
    the same header it had before.
    """
    node = regtest_node()
    (block,) = a_chain(node, 1)
    filter_index = node.chainstate.filter_index
    header = filter_index.get_header(block.header.hash)
    filter_index.db.delete(b"cfilter-" + block.header.hash)

    assert filter_index.catch_up(
        node.chainstate.block_index.active_chain, node.block_db
    )
    assert filter_index.get_header(block.header.hash) == header


def test_a_long_catch_up_writes_as_it_goes(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """catch_up flushes at `_CATCH_UP_BATCH` rather than holding it all pending.

    It runs inside Node.__init__, so holding the whole index in memory
    is what a mainnet-sized chain cannot afford, and writing nothing
    until the end is what makes an interrupted catch-up start over.
    With the batch size patched to 2, `pending` never grows past it,
    and something is on disk before the whole walk finishes.
    """
    node = regtest_node()
    chain = a_chain(node, 4)
    filter_index = node.chainstate.filter_index
    active_chain = node.chainstate.block_index.active_chain
    for block in chain:
        filter_index.db.delete(b"cfilter-" + block.header.hash)
        filter_index.db.delete(b"cfheader-" + block.header.hash)

    monkeypatch.setattr(filter_index_module, "_CATCH_UP_BATCH", 2)
    held: list[int] = []
    real_add = filter_index.add_connected_block

    def watched(block: Block, rev_block: RevBlock) -> None:
        real_add(block, rev_block)
        held.append(len(filter_index.pending))

    monkeypatch.setattr(filter_index, "add_connected_block", watched)
    assert filter_index.catch_up(active_chain, node.block_db) == len(chain)

    # never more than the batch held at once, and something on disk
    # before the walk was over
    assert max(held) <= 2
    for block in chain:
        assert filter_index.db.get(b"cfilter-" + block.header.hash) is not None


def test_the_filters_of_a_connected_batch_go_in_the_chainstate_s_own_write(
    regtest_node: Callable[[], Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connecting a block finalizes its filter into the chainstate's own write.

    The filters and the chain advance are one fact, so they are one
    write: finalized with the batch update_chain opened rather than
    straight to the database beside it, where a crash between the two
    would leave a chain whose tip has no filter.
    """
    node = regtest_node()
    filter_index = node.chainstate.filter_index
    given: list[KeyValueStore | None] = []
    real = filter_index.finalize

    def recording_finalize(wb: KeyValueStore | None = None) -> None:
        # a statement rather than `given.append(wb) or real(wb)`: the
        # real call was reached through the right-hand side of an `or`
        # whose left one is `None` every time, which reads as a
        # fallback and is a sequence point
        given.append(wb)
        return real(wb)

    monkeypatch.setattr(filter_index, "finalize", recording_finalize)

    a_chain(node, 1)
    assert given
    assert all(wb is not None for wb in given)
