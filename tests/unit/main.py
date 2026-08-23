# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.block import Block
from btclib.p2p.inventory import Headers, Inv, Inventory, InventoryType
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node, main
from btclib_node.chains import RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.block_index import BlockIndex, BlockStatus
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.interpreter import check_transactions
from btclib_node.main import update_chain, verify_mempool_acceptance
from tests.helpers import (
    build_block,
    generate_coinbase,
    generate_random_chain,
    generate_random_header_chain,
    generate_random_transaction,
)

if TYPE_CHECKING:
    from btclib_node.p2p.connection import Connection


def regtest_node(tmp_path: Path) -> Node:
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )
    node.status = NodeStatus.HeaderSynced
    return node


def connect(node: Node, chain: list[Block]) -> BlockIndex:
    """Offer a chain to the node and drive it to connect what it will."""
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for hash in block_index.header_dict:
        block_index.set_downloaded(hash)
    for block in chain:
        node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(node)
    return block_index


def test_chain(tmp_path: Path) -> None:
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )
    node.status = NodeStatus.HeaderSynced
    length = 2000 * 1  # 2000
    chain = generate_random_chain(length, RegTest().genesis.hash)
    headers = [block.header for block in chain]
    block_index = node.chainstate.block_index
    for start in range(0, length, 2000):
        block_index.add_headers(headers[start : start + 2000])
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(node)
    assert len(block_index.active_chain) == length + 1


def spend(prevout_tx: Tx, value: int, script_sig: bytes | None = None) -> Tx:
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(prevout_tx.id, 0),
                script_sig=script_sig
                if script_sig is not None
                else script.serialize([b"\x11" * 32]),
                sequence=0xFFFFFFFF,
            )
        ],
        vout=[
            TxOut(value=value, script_pub_key=script.serialize([b"\x22" * 32])),
        ],
    )


def test_reject_block_that_prints_money(tmp_path: Path) -> None:
    # Script validation never reads the amounts except through the
    # sig_hash, so nothing in the engine notices an output larger than
    # the input it spends.
    node = regtest_node(tmp_path)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[-1].transactions[0]
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(), spend(funding, funding.vout[0].value + 1)],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected


def test_reject_block_with_a_failing_script(tmp_path: Path) -> None:
    # An input that does not verify has to fail the block. It used to be
    # written to errors/ and swallowed, inside a worker pool, so nothing
    # reached update_chain and the block was connected anyway.
    node = regtest_node(tmp_path)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[-1].transactions[0]
    unspendable = spend(
        funding,
        funding.vout[0].value,
        script_sig=script.serialize(["OP_RETURN"]),
    )
    bad = build_block(
        chain[-1].header.hash, [generate_coinbase(), unspendable], len(chain)
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected


def test_add_tx(tmp_path: Path) -> None:
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )
    node.status = NodeStatus.HeaderSynced
    chain = generate_random_chain(10, RegTest().genesis.hash)
    headers = [block.header for block in chain]
    block_index = node.chainstate.block_index
    block_index.add_headers(headers)
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(node)

    with pytest.raises(MissingPrevoutError):
        invalid_tx = generate_random_transaction()
        verify_mempool_acceptance(node, invalid_tx)

    tx1 = generate_random_transaction(chain[-1].transactions[0].id)
    tx2 = generate_random_transaction(tx1.id)

    verify_mempool_acceptance(node, tx1)

    # We can't find the prevouts
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, tx2)

    # tx1 needs to be added to the mempool
    node.mempool.add_tx(tx1)
    verify_mempool_acceptance(node, tx2)


def test_a_candidate_whose_block_has_not_arrived_is_not_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # headers run ahead of blocks for the whole of a sync, so the
    # commonest state of a candidate is one whose block is still being
    # fetched. It is declined before block_db is asked for anything:
    # asking and rolling back reaches the same chain, but by way of an
    # exception, on every pass of a loop that runs until the block
    # arrives.
    node = regtest_node(tmp_path)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    asked: list[bytes] = []
    monkeypatch.setattr(node.block_db, "get_block", asked.append)
    update_chain(node)
    assert not asked
    assert block_index.active_chain == [RegTest().genesis.hash]
    assert node.status == NodeStatus.HeaderSynced


def test_a_hole_behind_a_downloaded_tip_does_not_block_a_complete_branch(
    tmp_path: Path,
) -> None:
    # get_first_candidate used to ask only whether a candidate's own tip
    # had arrived, so a branch missing a block *behind* its downloaded
    # tip still passed it -- and then update_chain found the hole and
    # gave up the whole pass, leaving that same candidate at the front
    # of the queue next time: btclib-org/btclib-node#121
    node = regtest_node(tmp_path)
    block_index = node.chainstate.block_index

    hole = generate_random_chain(2, RegTest().genesis.hash)
    block_index.add_headers([block.header for block in hole])
    for block in hole:
        node.block_db.add_block(block)
    block_index.set_downloaded(hole[-1].header.hash)  # the tip alone

    complete = generate_random_chain(1, RegTest().genesis.hash)
    block_index.add_headers([block.header for block in complete])
    for block in complete:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    update_chain(node)
    assert block_index.active_chain[1:] == hashes(complete)


def test_update_chain_refuses_a_block_marked_downloaded_but_missing(
    tmp_path: Path,
) -> None:
    # the download manager and block_db agree by construction; this is
    # the state they would be in if they did not
    node = regtest_node(tmp_path)
    chain = generate_random_chain(1, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for hash in block_index.header_dict:
        block_index.set_downloaded(hash)
    # deliberately not added to node.block_db
    with pytest.raises(Exception, match="just checked downloaded is missing"):
        update_chain(node)


def hashes(chain: list[Block]) -> list[bytes]:
    return [block.header.hash for block in chain]


def test_a_heavier_fork_replaces_the_chain_the_node_was_on(tmp_path: Path) -> None:
    # more than one block on the branch being left, because that is the
    # shallowest branch whose blocks have to be undone in an order: an
    # output block N created and block N+1 spent is gone from the utxo
    # set by the time N comes to be undone
    node = regtest_node(tmp_path)
    first = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, first)
    assert block_index.active_chain[1:] == hashes(first)

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)
    assert block_index.active_chain[1:] == hashes(second)
    for block_hash in hashes(first):
        assert block_hash not in block_index.active_chain


def test_a_reorg_refuses_a_missing_reverse_patch(tmp_path: Path) -> None:
    # every block on the active chain has one, by construction; this is
    # the state block_db would be in if it did not
    node = regtest_node(tmp_path)
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.block_db.rev_patches.pop(first[-1].header.hash)

    second = generate_random_chain(3, RegTest().genesis.hash)
    with pytest.raises(Exception, match="no reverse patch"):
        connect(node, second)


def test_a_reorg_refuses_a_missing_removed_block(tmp_path: Path) -> None:
    # the reverse patch of the block being undone is enough to roll the
    # chainstate back; giving the transactions of that same block back
    # to the mempool needs the block itself, which is the gap this pins
    node = regtest_node(tmp_path)
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.block_db.blocks.pop(first[-1].header.hash)

    second = generate_random_chain(3, RegTest().genesis.hash)
    with pytest.raises(Exception, match="block just removed is missing"):
        connect(node, second)


def test_a_reorg_gives_the_orphaned_transactions_back_to_the_mempool(
    tmp_path: Path,
) -> None:
    # only once the node is synced: while it is still catching up, a
    # transaction from a block it steps off is not worth relaying
    node = regtest_node(tmp_path)
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    assert node.status == NodeStatus.BlockSynced

    orphaned = first[-1].transactions[1]
    assert not node.mempool.contains_tx(orphaned)

    # held before the reorg and confirmed by it, so that taking it out
    # of the mempool is something the reorg has to do rather than
    # something that was never needed
    second = generate_random_chain(3, RegTest().genesis.hash)
    confirmed = second[-1].transactions[1]
    node.mempool.add_tx(confirmed)
    assert node.mempool.contains_tx(confirmed)

    connect(node, second)

    # #85: the orphan spends the abandoned branch's own coinbase, so it
    # can never be valid again -- it goes back in all the same, which is
    # what this pins and what that issue is about
    assert node.mempool.contains_tx(orphaned)
    assert not node.mempool.contains_tx(confirmed)


def test_a_reorg_before_the_node_is_synced_leaves_the_mempool_alone(
    tmp_path: Path,
) -> None:
    node = regtest_node(tmp_path)
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.status = NodeStatus.HeaderSynced

    second = generate_random_chain(3, RegTest().genesis.hash)
    block_index = connect(node, second)
    # the reorg happened, and left the mempool out of it
    assert block_index.active_chain[1:] == hashes(second)
    assert node.mempool.size == 0


def test_a_newly_connected_block_is_announced_to_every_connected_peer(
    tmp_path: Path,
) -> None:
    # only once the node is synced, the same gate the mempool bookkeeping
    # above already uses: an accepted block used to reach nobody, by
    # either shape. btclib-org/btclib-node#202
    node = regtest_node(tmp_path)
    first = generate_random_chain(1, RegTest().genesis.hash)
    connect(node, first)
    assert node.status == NodeStatus.BlockSynced

    header_sent: list[Any] = []
    inv_sent: list[Any] = []
    node.p2p_manager.connections[1] = cast(
        "Connection", SimpleNamespace(prefers_headers=True, send=header_sent.append)
    )
    node.p2p_manager.connections[2] = cast(
        "Connection", SimpleNamespace(prefers_headers=False, send=inv_sent.append)
    )

    second = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, second)

    (sent,) = header_sent
    assert isinstance(sent, Headers)
    assert list(sent.headers) == [block.header for block in second]

    (sent,) = inv_sent
    assert isinstance(sent, Inv)
    assert sent.items == tuple(
        Inventory(InventoryType.MSG_BLOCK, block.header.hash) for block in second
    )


def test_a_reorg_before_the_node_is_synced_announces_nothing(tmp_path: Path) -> None:
    node = regtest_node(tmp_path)
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.status = NodeStatus.HeaderSynced

    sent: list[Any] = []
    node.p2p_manager.connections[1] = cast(
        "Connection",
        # finish_sync's stop_all fires once this reorg catches the node
        # back up to BlockSynced, and reaches every connection whether
        # or not the diff under test ever sends it anything
        SimpleNamespace(prefers_headers=True, send=sent.append, stop=lambda: None),
    )

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)
    assert not sent


def test_a_refused_branch_invalidates_only_the_block_that_failed(
    tmp_path: Path,
) -> None:
    # the branch is tried as a unit: its tip is what get_first_candidate
    # offers, so the blocks under it connect in the same pass the tip is
    # refused in, and the utxo set and the filter index are rolled back.
    # Neither rollback reaches the block index; what does is
    # update_header_index, on the one block whose own contextual check
    # raised -- the ones under it never failed anything and stay
    # valid_header, ready to connect if a different tip is built on them.
    node = regtest_node(tmp_path)
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)

    below = generate_random_chain(2, RegTest().genesis.hash)
    prints_money = build_block(
        below[-1].header.hash,
        [
            generate_coinbase(),
            spend(below[-1].transactions[0], 50 * 10**8 + 1),
        ],
        len(below),
    )
    fork = [*below, prints_money]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == prints_money.header.hash

    update_chain(node)
    assert block_index.active_chain[1:] == hashes(active)
    for block in below:
        info = block_index.get_block_info(block.header.hash)
        assert info.status == BlockStatus.valid_header
    assert (
        block_index.get_block_info(prints_money.header.hash).status
        == BlockStatus.invalid
    )
    # the doomed tip no longer weighs on what get_first_candidate offers
    assert block_index.get_first_candidate() is None

    node.chainstate.close()
    reopened = Chainstate(node.data_dir, RegTest(), node.logger)
    for block in below:
        info = reopened.block_index.get_block_info(block.header.hash)
        assert info.status == BlockStatus.valid_header
    assert (
        reopened.block_index.get_block_info(prints_money.header.hash).status
        == BlockStatus.invalid
    )
    reopened.close()


def test_a_refused_branch_leaves_no_reverse_patches_in_the_block_store(
    tmp_path: Path,
) -> None:
    # active outweighs below's own two blocks individually, so only
    # prints_money -- the fork's tip -- is its own candidate and the
    # whole fork connects in one trial. below's two blocks validate and
    # each generate a reverse patch before prints_money fails and the
    # trial is rolled back: btclib-org/btclib-node#200
    node = regtest_node(tmp_path)
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)

    below = generate_random_chain(2, RegTest().genesis.hash)
    prints_money = build_block(
        below[-1].header.hash,
        [
            generate_coinbase(),
            spend(below[-1].transactions[0], 50 * 10**8 + 1),
        ],
        len(below),
    )
    fork = [*below, prints_money]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == prints_money.header.hash

    update_chain(node)

    assert block_index.active_chain[1:] == hashes(active)
    for block in below:
        assert node.block_db.get_rev_block(block.header.hash) is None
        assert block.header.hash not in node.block_db.rev_patches
    assert node.block_db.pending_rev_blocks == {}


def test_a_refused_branch_invalidates_headers_that_were_never_candidates(
    tmp_path: Path,
) -> None:
    # neither the block that fails nor a sibling built on it has to have
    # individually outweighed the active chain to be real: only the
    # branch's own tip does, for update_chain to try connecting it at
    # all. Both are hidden from block_candidates and only reachable by
    # walking BlockIndex.children -- proves the cascade through the real
    # update_chain -> update_header_index -> invalidate call chain, not
    # just the isolated BlockIndex-level call: btclib-org/btclib-node#125
    node = regtest_node(tmp_path)
    active = generate_random_chain(6, RegTest().genesis.hash)
    block_index = connect(node, active)

    below = generate_random_chain(2, RegTest().genesis.hash)
    prints_money = build_block(
        below[-1].header.hash,
        [
            generate_coinbase(),
            spend(below[-1].transactions[0], 50 * 10**8 + 1),
        ],
        len(below),
    )
    # more, structurally fine, blocks on top of the doomed one -- their
    # combined chainwork is what makes the branch's tip outweigh active,
    # not prints_money on its own. Built with an explicit, increasing
    # height rather than generate_random_chain's own (which restarts at
    # 0 for any start): a header's timestamp has to beat the median of
    # its ancestors, and build_block's is derived from the height alone
    continuation: list[Block] = []
    previous = prints_money
    for height in range(len(below) + 1, len(below) + 5):
        previous = build_block(previous.header.hash, [generate_coinbase()], height)
        continuation.append(previous)
    fork = [*below, prints_money, *continuation]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    # a sibling of the continuation, off prints_money, real and indexed
    # but never downloaded and never its own block_candidates entry
    sibling = generate_random_header_chain(
        1, prints_money.header.hash, prints_money.header.time
    )
    block_index.add_headers(sibling)
    # the branch's own tip is the one candidate entry: everything below
    # it, prints_money included, never individually outweighed active
    # on its own
    hidden = {prints_money.header.hash, sibling[0].hash}
    hidden.update(block.header.hash for block in continuation[:-1])
    assert not hidden & {h for h, _ in block_index.block_candidates}

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == continuation[-1].header.hash

    update_chain(node)
    assert block_index.active_chain[1:] == hashes(active)
    for hash in {*hidden, continuation[-1].header.hash}:
        assert block_index.get_block_info(hash).status == BlockStatus.invalid


def test_a_stop_mid_reorg_rolls_the_trial_back_without_invalidating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `terminate_flag` is read between the blocks of `to_add`, so a
    # shutdown requested during a reorg is noticed after the block being
    # validated when it arrived rather than after the whole fork:
    # btclib-org/btclib-node#139. Nothing update_chain buffers along the
    # way reaches disk until every block of the fork has validated, so
    # the state this pins is not "stopped partway, with some of the fork
    # applied" -- there is no such state to reach -- but "stopped with
    # none of it applied, and the block it stopped on left alone", which
    # is what tells this apart from a block that failed its own check.
    node = regtest_node(tmp_path)
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)
    active_chain_before = list(block_index.active_chain)

    fork = generate_random_chain(4, RegTest().genesis.hash)
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    # get_first_candidate offers the shallowest block that already
    # outweighs active, not necessarily the fork's own tip -- to_add is
    # whatever get_fork_details returns for that candidate, and this
    # pins the trial to stop inside it rather than assuming it is the
    # whole of `fork`
    candidate = block_index.get_first_candidate()
    assert candidate is not None
    to_add_hash, _ = block_index.get_fork_details(candidate.header.hash)
    assert len(to_add_hash) >= 3

    calls = 0

    def stop_after_the_second_block(
        transaction_data: list[tuple[list[TxOut], Tx]], index: int, node: Node
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            node.terminate_flag.set()
        return check_transactions(transaction_data, index, node)

    monkeypatch.setattr(main, "check_transactions", stop_after_the_second_block)

    update_chain(node)

    # stopped between the second and the third block of the trial, not
    # partway through validating either and not at its end
    assert calls == 2
    assert block_index.active_chain == active_chain_before
    # a shutdown is not a defect in the block it landed on: none of the
    # fork's blocks is marked invalid, and the same candidate is still
    # offered whole
    for block in fork:
        info = block_index.get_block_info(block.header.hash)
        assert info.status != BlockStatus.invalid
    stopped_candidate = block_index.get_first_candidate()
    assert stopped_candidate is not None
    assert stopped_candidate.header.hash == candidate.header.hash
    # every buffer the trial writes into on its way to `finalize` is
    # back to empty, the same as after a block that failed its own check
    assert node.chainstate.utxo_index.updated_utxo_set == {}
    assert node.chainstate.utxo_index.removed_utxos == set()
    assert node.chainstate.filter_index.pending == {}
    assert node.block_db.pending_rev_blocks == {}

    # nothing here is stuck: a run with nothing asking it to stop
    # connects the whole fork, the same number of passes connect() takes
    # to drive any other fork of this length
    node.terminate_flag.clear()
    for _ in range(len(fork)):
        update_chain(node)
    assert block_index.active_chain[1:] == hashes(fork)
