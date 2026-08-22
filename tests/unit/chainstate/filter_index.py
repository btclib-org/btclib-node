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

import pytest
from btclib.block import Block
from btclib.block.block_filter import BasicBlockFilter, filter_header
from btclib.script import script

import btclib_node.chainstate.filter_index as filter_index_module
from btclib_node.chains import RegTest, TestNet
from btclib_node.main import update_chain
from tests import load, vector_id
from tests.helpers import build_block, generate_coinbase, generate_random_chain
from tests.unit.main import connect, regtest_node, spend

GENESIS = RegTest().genesis
NO_PREVIOUS = b"\x00" * 32


def offer(node, chain):
    """Give the node the headers and the blocks, and connect nothing."""
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block_hash in block_index.header_dict:
        block_info = block_index.get_block_info(block_hash)
        block_info.downloaded = True
        block_index.insert_block_info(block_info)
    for block in chain:
        node.block_db.add_block(block)


def a_chain(node, length):
    chain = generate_random_chain(length, GENESIS.hash)
    connect(node, chain)
    return chain


def recomputed_header(node, block_hash):
    """Chain the filter headers from genesis the way a peer would."""
    active_chain = node.chainstate.block_index.active_chain
    filter_index = node.chainstate.filter_index
    header = NO_PREVIOUS
    for hash_ in active_chain[: active_chain.index(block_hash) + 1]:
        header = filter_header(filter_index.get_filter_hash(hash_), header)
    return header


def test_the_genesis_filter_is_indexed_before_any_block_arrives(tmp_path):
    node = regtest_node(tmp_path)
    filter_index = node.chainstate.filter_index
    # no peer serves the genesis block, so it is not indexed by the
    # connect path and would be the one hole in the chain of headers
    block_filter = BasicBlockFilter.parse(
        filter_index.get_filter(GENESIS.hash), GENESIS.hash
    )
    assert block_filter == BasicBlockFilter.from_block(RegTest().genesis_block, [])
    assert filter_index.get_header(GENESIS.hash) == filter_header(
        block_filter.hash, NO_PREVIOUS
    )


def test_a_connected_block_is_indexed_as_it_connects(tmp_path):
    node = regtest_node(tmp_path)
    chain = a_chain(node, 3)
    filter_index = node.chainstate.filter_index
    for block in chain:
        assert filter_index.get_filter(block.header.hash) is not None


def test_the_filter_holds_what_the_block_pays_to_and_what_it_spends(tmp_path):
    node = regtest_node(tmp_path)
    chain = a_chain(node, 2)
    spending = chain[1]
    filter_index = node.chainstate.filter_index
    block_filter = BasicBlockFilter.parse(
        filter_index.get_filter(spending.header.hash), spending.header.hash
    )

    paid_to = [out.script_pub_key.script for out in spending.transactions[0].vout]
    assert block_filter.match_any(paid_to)
    # and the output it spends, which is in the block before it: a
    # filter built from the block alone would miss every input, and a
    # client watching an address would never be told to fetch the block
    # that emptied it
    spent = [out.script_pub_key.script for out in chain[0].transactions[0].vout]
    assert block_filter.match_any(spent)
    assert not block_filter.match(b"\x51" * 20)


def test_every_header_chains_onto_the_one_before_it(tmp_path):
    node = regtest_node(tmp_path)
    chain = a_chain(node, 4)
    filter_index = node.chainstate.filter_index
    for block in chain:
        block_hash = block.header.hash
        assert filter_index.get_header(block_hash) == recomputed_header(
            node, block_hash
        )


def test_a_block_whose_parent_has_no_filter_is_refused(tmp_path):
    node = regtest_node(tmp_path)
    orphan = generate_random_chain(1, b"\x11" * 32)[0]
    with pytest.raises(Exception, match="no filter header for the parent"):
        node.chainstate.filter_index.add_block(orphan, [])


def test_a_block_already_indexed_is_not_built_twice(tmp_path):
    node = regtest_node(tmp_path)
    (block,) = a_chain(node, 1)
    filter_index = node.chainstate.filter_index
    filter_index.add_block(block, [])
    # the second call would build from the wrong prevouts and answer a
    # different filter; the stored one is the one that stays
    assert not filter_index.pending


def test_a_block_offered_twice_before_the_batch_is_written_is_built_once(tmp_path):
    # blocks connect in one write batch, so what is held for writing is
    # what a second offer of the same block has to be answered from:
    # rebuilding it would use whatever prevouts the second caller had
    node = regtest_node(tmp_path)
    filter_index = node.chainstate.filter_index
    (block,) = generate_random_chain(1, GENESIS.hash)
    filter_index.add_block(block, [])
    built = filter_index.pending[block.header.hash]
    filter_index.add_block(block, [])
    assert filter_index.pending[block.header.hash] is built


def test_what_is_pending_is_dropped_on_a_rollback(tmp_path):
    node = regtest_node(tmp_path)
    filter_index = node.chainstate.filter_index
    (block,) = generate_random_chain(1, GENESIS.hash)
    filter_index.add_block(block, [])
    assert filter_index.pending
    filter_index.rollback()
    assert not filter_index.pending
    assert filter_index.get_filter(block.header.hash) is None


def test_a_block_stepped_over_keeps_the_filter_it_had(tmp_path):
    # a filter is a function of its block and its ancestry, neither of
    # which a reorg changes, so there is nothing here to undo -- and a
    # branch that comes back is not rebuilt
    node = regtest_node(tmp_path)
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


def test_nothing_is_answered_for_a_block_that_is_not_indexed(tmp_path):
    node = regtest_node(tmp_path)
    filter_index = node.chainstate.filter_index
    unknown = b"\x11" * 32
    assert filter_index.get_filter(unknown) is None
    assert filter_index.get_header(unknown) is None
    assert filter_index.get_filter_hash(unknown) is None


def test_a_chain_indexed_before_the_index_existed_is_caught_up(tmp_path):
    node = regtest_node(tmp_path)
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


def test_a_chain_that_is_already_indexed_is_not_rebuilt(tmp_path):
    node = regtest_node(tmp_path)
    a_chain(node, 2)
    assert (
        node.chainstate.filter_index.catch_up(
            node.chainstate.block_index.active_chain, node.block_db
        )
        == 0
    )


def test_a_catch_up_that_cannot_reach_a_block_says_so(tmp_path):
    # the block is on the active chain and its octets are not on disk,
    # so no filter can be built for it and none for anything after it:
    # the next block's header chains onto this one's
    node = regtest_node(tmp_path)
    (block,) = a_chain(node, 1)
    filter_index = node.chainstate.filter_index
    filter_index.db.delete(b"cfilter-" + block.header.hash)
    filter_index.db.delete(b"cfheader-" + block.header.hash)
    block_db = node.block_db
    block_db.blocks.pop(block.header.hash)

    with pytest.raises(Exception, match="cannot build the block filter index"):
        filter_index.catch_up(node.chainstate.block_index.active_chain, block_db)


def test_an_index_survives_the_node_being_closed_and_opened(tmp_path):
    node = regtest_node(tmp_path)
    (block,) = a_chain(node, 1)
    header = node.chainstate.filter_index.get_header(block.header.hash)
    node.chainstate.close()
    node.block_db.close()

    reopened = regtest_node(tmp_path)
    assert reopened.chainstate.filter_index.get_header(block.header.hash) == header
    reopened.chainstate.close()
    reopened.block_db.close()


def test_a_filter_index_reads_only_its_own_keys(tmp_path):
    # BlockIndex.init_from_db walks the whole database and stops at the
    # first key that is not a `blkinfo-`, so what this writes has to
    # sort after those or the block index would stop short of its own
    node = regtest_node(tmp_path)
    a_chain(node, 1)
    node.chainstate.close()
    node.block_db.close()

    reopened = regtest_node(tmp_path)
    # genesis plus the one block: the index read everything of its own
    assert len(reopened.chainstate.block_index.active_chain) == 2
    reopened.chainstate.close()
    reopened.block_db.close()


def test_the_index_is_caught_up_before_the_node_is_built(tmp_path):
    # Node.__init__ is where the two databases meet, and a node that
    # advertises the service bit with a half-built index would be
    # promising filters it cannot serve
    node = regtest_node(tmp_path)
    chain = a_chain(node, 2)
    filter_index = node.chainstate.filter_index
    for block in chain:
        filter_index.db.delete(b"cfilter-" + block.header.hash)
        filter_index.db.delete(b"cfheader-" + block.header.hash)
    node.chainstate.close()
    node.block_db.close()

    reopened = regtest_node(tmp_path)
    for block in chain:
        assert (
            reopened.chainstate.filter_index.get_filter(block.header.hash) is not None
        )
    reopened.chainstate.close()
    reopened.block_db.close()


def test_a_block_that_does_not_connect_leaves_no_filter_behind(tmp_path):
    # the index is written in the chainstate's own write batch, so a
    # block that fails validation has to leave it as it was: a filter
    # held for a block the chain does not have would be answered to a
    # peer asking about the block that did connect at that height
    node = regtest_node(tmp_path)
    chain = a_chain(node, 2)
    filter_index = node.chainstate.filter_index

    funding = chain[-1].transactions[0]
    unspendable = spend(
        funding, funding.vout[0].value, script_sig=script.serialize(["OP_RETURN"])
    )
    bad = build_block(
        chain[-1].header.hash, [generate_coinbase(), unspendable], len(chain)
    )
    connect(node, [bad])

    assert bad.header.hash not in node.chainstate.block_index.active_chain
    assert not filter_index.pending
    assert filter_index.get_filter(bad.header.hash) is None
    assert filter_index.get_header(bad.header.hash) is None


def test_a_batch_that_fails_partway_leaves_nothing_of_the_blocks_before_it(tmp_path):
    # Several blocks reach the chainstate in one write batch only as a
    # fork: a block on the tip is a candidate on its own and connects
    # by itself. So the node is put on a short chain and offered a
    # heavier branch whose last block does not validate -- the two
    # before it are indexed and pending when it raises, and the whole
    # batch has to go, filters included.
    node = regtest_node(tmp_path)
    a_chain(node, 2)
    on_chain = list(node.chainstate.block_index.active_chain)

    branch = generate_random_chain(2, GENESIS.hash)
    funding = branch[-1].transactions[0]
    unspendable = spend(
        funding, funding.vout[0].value, script_sig=script.serialize(["OP_RETURN"])
    )
    bad = build_block(
        branch[-1].header.hash, [generate_coinbase(), unspendable], len(branch)
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


@pytest.mark.parametrize("vector", _CORE_VECTORS, ids=_VECTOR_IDS)
def test_the_index_holds_the_filters_bitcoin_core_computed(tmp_path, vector):
    """Hold the index to Core's numbers rather than to btclib's.

    `btclib.block.block_filter` is tested against this same file inside
    btclib, and what is checked here is the layer this tree adds: that
    the octets stored for a block are the filter Core computed, that
    they come back unchanged, and that the header chained onto the one
    before is Core's header. A filter that goes into this index and
    comes out a different filter is a node telling a light client
    something it cannot catch until it fetches the block.
    """
    _, block_hash, block_hex, prevout_scripts, previous, expected, header = vector[:7]
    block = Block.parse(bytes.fromhex(block_hex), check_validity=False)
    assert block.header.hash.hex() == block_hash

    node = regtest_node(tmp_path)
    filter_index = node.chainstate.filter_index
    # a testnet block whose parent this node has never connected, so the
    # header before it comes from the row: what is under test here is
    # the chaining and the storage, not where the parent came from
    filter_index.db.put(
        b"cfheader-" + block.header.previous_block_hash, bytes.fromhex(previous)
    )
    filter_index.add_block(block, [bytes.fromhex(script) for script in prevout_scripts])
    filter_index.finalize()

    assert filter_index.get_filter(block.header.hash).hex() == expected
    assert filter_index.get_header(block.header.hash).hex() == header
    assert (
        filter_index.get_filter_hash(block.header.hash)
        == BasicBlockFilter.parse(bytes.fromhex(expected), block.header.hash).hash
    )


def test_the_testnet_genesis_block_this_node_builds_is_the_one_core_filtered():
    # chains.py builds the genesis block out of a coinbase it writes
    # rather than parsing one, and row 0 of Core's file is that block:
    # what the filter says is that the transaction this node made is the
    # transaction the network has. It is the one block a peer never
    # serves, so nothing else would ever catch it being wrong.
    _, block_hash, block_hex, _, previous, expected, header = _CORE_VECTORS[0][:7]
    genesis_block = TestNet().genesis_block
    assert genesis_block.header.hash.hex() == block_hash
    assert genesis_block.serialize(check_validity=False).hex() == block_hex

    block_filter = BasicBlockFilter.from_block(genesis_block, [])
    assert block_filter.serialize().hex() == expected
    assert block_filter.header(previous).hex() == header


def test_the_header_is_written_before_the_filter(tmp_path):
    # both skip guards ask get_filter, so a filter on disk without its
    # header is the state nothing repairs: the block is skipped for
    # ever, its child cannot be indexed, and the datadir will not open.
    # The pair goes in one atomic write, and the order is what keeps the
    # survivable half survivable if it ever stops being one.
    node = regtest_node(tmp_path)
    filter_index = node.chainstate.filter_index
    (block,) = generate_random_chain(1, GENESIS.hash)
    filter_index.add_block(block, [])

    written = []
    filter_index._write(SimpleNamespace(put=lambda key, _: written.append(key[:-32])))
    assert written == [b"cfheader-", b"cfilter-"]


def test_the_pair_reaches_the_database_as_one_write(tmp_path):
    node = regtest_node(tmp_path)
    filter_index = node.chainstate.filter_index
    (block,) = generate_random_chain(1, GENESIS.hash)
    filter_index.add_block(block, [])

    real = filter_index.db
    batches = []

    class CountingDb:
        """The chainstate database, counting the batches asked of it."""

        def write_batch(self, **kwargs):
            batches.append(kwargs)
            return real.write_batch(**kwargs)

    filter_index.db = CountingDb()
    try:
        filter_index.finalize()
    finally:
        filter_index.db = real

    # one batch, and one that rolls back rather than half-applying
    assert batches == [{"transaction": True}]
    assert filter_index.get_filter(block.header.hash) is not None
    assert filter_index.get_header(block.header.hash) is not None


def test_a_header_left_without_its_filter_is_simply_rebuilt(tmp_path):
    # the survivable half of the pair, and the reason the order above is
    # the one it is: the block is built again and answers the same
    node = regtest_node(tmp_path)
    (block,) = a_chain(node, 1)
    filter_index = node.chainstate.filter_index
    header = filter_index.get_header(block.header.hash)
    filter_index.db.delete(b"cfilter-" + block.header.hash)

    assert filter_index.catch_up(
        node.chainstate.block_index.active_chain, node.block_db
    )
    assert filter_index.get_header(block.header.hash) == header


def test_a_long_catch_up_writes_as_it_goes(tmp_path, monkeypatch):
    # it runs inside Node.__init__, so holding the whole index in memory
    # is what a mainnet-sized chain cannot afford, and writing nothing
    # until the end is what makes an interrupted catch-up start over
    node = regtest_node(tmp_path)
    chain = a_chain(node, 4)
    filter_index = node.chainstate.filter_index
    active_chain = node.chainstate.block_index.active_chain
    for block in chain:
        filter_index.db.delete(b"cfilter-" + block.header.hash)
        filter_index.db.delete(b"cfheader-" + block.header.hash)

    monkeypatch.setattr(filter_index_module, "_CATCH_UP_BATCH", 2)
    held = []
    real_add = filter_index.add_connected_block

    def watched(block, rev_block):
        real_add(block, rev_block)
        held.append(len(filter_index.pending))

    filter_index.add_connected_block = watched
    assert filter_index.catch_up(active_chain, node.block_db) == len(chain)

    # never more than the batch held at once, and something on disk
    # before the walk was over
    assert max(held) <= 2
    for block in chain:
        assert filter_index.db.get(b"cfilter-" + block.header.hash) is not None


def test_the_filters_of_a_connected_batch_go_in_the_chainstate_s_own_write(tmp_path):
    # the filters and the chain advance are one fact, so they are one
    # write: finalized with the batch update_chain opened rather than
    # straight to the database beside it, where a crash between the two
    # would leave a chain whose tip has no filter
    node = regtest_node(tmp_path)
    filter_index = node.chainstate.filter_index
    given = []
    real = filter_index.finalize
    filter_index.finalize = lambda wb=None: given.append(wb) or real(wb)

    a_chain(node, 1)

    filter_index.finalize = real
    assert given and all(wb is not None for wb in given)
