# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Block assembly, the nonce search and the validity check that stores nothing.

Each test builds blocks on a regtest node with `create_new_block` and
hands them to `accept_block`, the same calls the mining RPCs make, and
asserts on the block or on the chain it leaves. The reasons
`check_block_validity` answers are compared with what `bitcoind` v31.1.0
answers for the same block, run by hand against a regtest node holding the
same chain.
"""

import copy
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.block import Block, witness_commitment_output
from btclib.block.block import (
    bip34_commitment,
    merkle_root_and_mutated_from_transactions,
)
from btclib.block.mining import NONCE_SPACE
from btclib.script.script_pub_key import ScriptPubKey
from btclib.script.witness import Witness
from btclib.tx import OutPoint, Tx, TxIn, TxOut

from btclib_node import Node, mining
from btclib_node.chains import RegTest
from btclib_node.mining import (
    DEFAULT_MAX_TRIES,
    TemplateError,
    accept_block,
    block_with_transactions,
    check_block_validity,
    create_new_block,
    solve_block,
)
from btclib_node.rpc.callbacks import callbacks
from tests import (
    anyone_can_spend,
    anyone_can_spend_script_sig,
    finish,
    generate_random_transaction,
)

if TYPE_CHECKING:
    from btclib_node.rpc.connection import RpcConnection

SCRIPT = ScriptPubKey(anyone_can_spend(), "regtest")

# the subsidy of a regtest block before its first halving
SUBSIDY = 50 * 10**8

# `Config.min_relay_feerate` is 100 sat/kvB, so this is over it for any
# transaction built here
FEE = 1_000


@pytest.fixture
def node(regtest_node: Callable[[], Node]) -> Node:
    """Give one regtest node, built fresh for the test."""
    return regtest_node()


def mine(node: Node, count: int) -> list[bytes]:
    """Mine `count` blocks on the tip the way the mining RPCs do."""
    hashes = []
    for _ in range(count):
        block = mine_template(node, create_new_block(node, SCRIPT).block)
        hashes.append(block.header.hash)
    return hashes


def mine_template(node: Node, block: Block) -> Block:
    """Solve `block`, accept it, and return it."""
    solved, _ = finish(solve_block(node, block, DEFAULT_MAX_TRIES))
    assert solved is not None
    assert accept_block(node, solved) is None
    return solved


def coinbases(node: Node, count: int) -> list[Tx]:
    """Return the coinbases of blocks 1 to `count`."""
    active_chain = node.chainstate.block_index.active_chain
    blocks = [node.block_db.get_block(active_chain[i]) for i in range(1, count + 1)]
    return [block.transactions[0] for block in blocks if block is not None]


@pytest.fixture
def funded(node: Node) -> Node:
    """Give a node whose first coinbases are spendable."""
    mine(node, 101 + 5)
    return node


def spend(
    outpoints: list[OutPoint],
    values: list[int],
    *,
    lock_time: int = 0,
    sequence: int = 0xFFFFFFFF,
) -> Tx:
    """Return a transaction spending `outpoints` to `values`."""
    return Tx(
        version=2,
        lock_time=lock_time,
        vin=[
            TxIn(outpoint, anyone_can_spend_script_sig(), sequence)
            for outpoint in outpoints
        ],
        vout=[TxOut(value, anyone_can_spend()) for value in values],
    )


def pool(node: Node, tx: Tx, fee: int) -> Tx:
    """Put `tx`, paying `fee`, in the mempool, and return it."""
    assert node.mempool.add_tx(tx, fee)
    return tx


def with_consensus(
    monkeypatch: pytest.MonkeyPatch, node: Node, **changes: int | bool
) -> None:
    """Run `node`'s chain on its own consensus rules with `changes`."""
    changed = replace(node.chain.consensus, **cast("Any", changes))
    monkeypatch.setattr(type(node.chain), "consensus", property(lambda _: changed))


def test_a_template_is_the_block_core_assembles(node: Node) -> None:
    """The coinbase and the header are `CreateNewBlock`'s, field by field."""
    template = create_new_block(node, SCRIPT)
    block = template.block
    coinbase = block.transactions[0]
    tip = node.chainstate.block_index.header_dict[RegTest().genesis.hash].header

    assert len(block.transactions) == 1
    assert (template.fees, template.sigops) == ([], [])
    assert coinbase.version == 2
    assert coinbase.lock_time == 0
    assert coinbase.vin[0].sequence == 0xFFFFFFFE
    assert coinbase.vin[0].script_sig == bytes([0x51, 0x00])
    assert coinbase.vin[0].script_witness.stack == (bytes(32),)
    assert [out.value for out in coinbase.vout] == [SUBSIDY, 0]
    assert coinbase.vout[0].script_pub_key.script == anyone_can_spend()
    assert block.header.version == 0x20000000
    assert block.header.previous_block_hash == tip.hash
    assert block.header.bits == RegTest().genesis.bits
    block.assert_valid_structure()
    assert check_block_validity(node, block, check_merkle_root=True) is None


def test_the_coinbase_commits_to_the_height_past_sixteen(node: Node) -> None:
    """A height above 16 is pushed as a number; `lock_time` is `height - 1`."""
    mine(node, 17)

    block = create_new_block(node, SCRIPT).block

    assert block.transactions[0].vin[0].script_sig == bip34_commitment(18) + b"\x00"
    assert block.transactions[0].lock_time == 17
    assert check_block_validity(node, block, check_merkle_root=True) is None


def test_the_reserved_value_is_written_once_segwit_is_active(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before segwit the commitment output is there and the witness is not."""
    with_consensus(monkeypatch, node, segwit_height=1000)

    coinbase = create_new_block(node, SCRIPT).block.transactions[0]

    assert coinbase.vin[0].script_witness.stack == ()
    assert len(coinbase.vout) == 2


def test_the_template_time_is_after_the_median_time_past(node: Node) -> None:
    """Blocks are not dated before `GetMinimumTime`, whatever the clock says."""
    hashes = mine(node, 3)
    second = node.chainstate.block_index.header_dict[hashes[1]].header
    template = create_new_block(node, SCRIPT)

    # the median of genesis and three blocks is the later of the middle two
    assert template.min_time == int(second.time.timestamp()) + 1
    assert int(template.block.header.time.timestamp()) >= template.min_time


def test_a_retarget_block_is_not_dated_before_its_parent_less_the_timewarp_bound(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At a period's first height `GetMinimumTime` is bounded by BIP94."""
    mine(node, 1)
    tip = node.chainstate.block_index.header_dict[
        node.chainstate.block_index.active_chain[-1]
    ].header
    tip_time = int(tip.time.timestamp())

    assert mining._minimum_time(tip, 143, 1_000, node) == 1_001
    assert mining._minimum_time(tip, 144, 1_000, node) == max(1_001, tip_time - 600)
    assert mining._minimum_time(tip, 144, tip_time + 5, node) == tip_time + 6


def test_the_package_with_the_best_feerate_goes_in_first(funded: Node) -> None:
    """Transactions are taken by fee, and what they pay is the coinbase's."""
    cbs = coinbases(funded, 3)
    low = pool(funded, spend([OutPoint(cbs[0].id, 0)], [SUBSIDY - 2_000]), 2_000)
    high = pool(funded, spend([OutPoint(cbs[1].id, 0)], [SUBSIDY - 9_000]), 9_000)
    mid = pool(funded, spend([OutPoint(cbs[2].id, 0)], [SUBSIDY - 5_000]), 5_000)

    template = create_new_block(funded, SCRIPT)

    assert template.block.transactions[1:] == [high, mid, low]
    assert template.fees == [9_000, 5_000, 2_000]
    assert template.block.transactions[0].vout[0].value == SUBSIDY + 16_000
    mine_template(funded, template.block)
    assert funded.mempool.size == 0


def test_a_child_that_pays_well_lifts_its_parent(funded: Node) -> None:
    """A parent is ranked with its child's fee, and goes in before it."""
    cbs = coinbases(funded, 3)
    parent = pool(funded, spend([OutPoint(cbs[0].id, 0)], [SUBSIDY - 100]), 100)
    child = pool(
        funded, spend([OutPoint(parent.id, 0)], [SUBSIDY - 100 - 20_000]), 20_000
    )
    other = pool(funded, spend([OutPoint(cbs[1].id, 0)], [SUBSIDY - 5_000]), 5_000)

    template = create_new_block(funded, SCRIPT)

    assert template.block.transactions[1:] == [parent, child, other]
    mine_template(funded, template.block)


def test_a_child_left_behind_by_its_parent_is_ranked_on_its_own(funded: Node) -> None:
    """Once a parent is in, its child is ranked by what it pays itself."""
    cbs = coinbases(funded, 2)
    parent = pool(funded, spend([OutPoint(cbs[0].id, 0)], [SUBSIDY - 20_000]), 20_000)
    child = pool(funded, spend([OutPoint(parent.id, 0)], [SUBSIDY - 20_100]), 100)
    other = pool(funded, spend([OutPoint(cbs[1].id, 0)], [SUBSIDY - 5_000]), 5_000)

    template = create_new_block(funded, SCRIPT)

    assert template.block.transactions[1:] == [parent, other, child]
    mine_template(funded, template.block)


def test_a_diamond_of_descendants_is_ranked_once_and_connects(funded: Node) -> None:
    """A transaction reached through two parents is ranked again only once."""
    cbs = coinbases(funded, 2)
    root = pool(funded, spend([OutPoint(cbs[0].id, 0)], [SUBSIDY // 2 - 100] * 2), 200)
    left = pool(funded, spend([OutPoint(root.id, 0)], [SUBSIDY // 2 - 1_100]), 1_000)
    right = pool(funded, spend([OutPoint(root.id, 1)], [SUBSIDY // 2 - 2_100]), 2_000)
    bottom = pool(
        funded,
        spend([OutPoint(left.id, 0), OutPoint(right.id, 0)], [SUBSIDY - 3_200 - 5_000]),
        5_000,
    )

    template = create_new_block(funded, SCRIPT)

    chosen = template.block.transactions[1:]
    assert sorted(tx.id for tx in chosen) == sorted(
        tx.id for tx in (root, left, right, bottom)
    )
    assert chosen[0] == root
    assert chosen[-1] == bottom
    mine_template(funded, template.block)


def test_a_package_over_the_weight_limit_is_left_out(
    funded: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transaction that no longer fits is skipped; a smaller one is not."""
    cbs = coinbases(funded, 2)
    padded = spend([OutPoint(cbs[0].id, 0)], [SUBSIDY - 90_000])
    padded.vout.append(TxOut(0, b"j" + bytes(5_000)))
    pool(funded, padded, 90_000)
    small = pool(funded, spend([OutPoint(cbs[1].id, 0)], [SUBSIDY - 1_000]), 1_000)
    monkeypatch.setattr(mining, "MAX_BLOCK_WEIGHT", 8_000 + small.weight + 1)

    template = create_new_block(funded, SCRIPT)

    assert template.block.transactions[1:] == [small]


def test_a_package_over_the_sigop_limit_is_left_out(
    funded: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transaction counted over the sigop limit stays in the mempool."""
    cbs = coinbases(funded, 2)
    heavy = pool(funded, spend([OutPoint(cbs[0].id, 0)], [SUBSIDY - 9_000]), 9_000)
    light = pool(funded, spend([OutPoint(cbs[1].id, 0)], [SUBSIDY - 1_000]), 1_000)
    monkeypatch.setattr(
        mining,
        "sig_op_cost",
        lambda _prev_outputs, tx, _flags: 80_000 if tx is heavy else 1,
    )

    template = create_new_block(funded, SCRIPT)

    assert template.block.transactions[1:] == [light]
    assert template.sigops == [1]


def test_a_package_is_counted_at_its_sigop_adjusted_weight(
    funded: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's chunk limit is `GetAdjustedWeight`, not the weight."""
    cbs = coinbases(funded, 2)
    heavy = pool(funded, spend([OutPoint(cbs[0].id, 0)], [SUBSIDY - 9_000]), 9_000)
    light = pool(funded, spend([OutPoint(cbs[1].id, 0)], [SUBSIDY - 1_000]), 1_000)
    monkeypatch.setattr(
        mining,
        "sig_op_cost",
        lambda _prev_outputs, tx, _flags: 1_000 if tx is heavy else 1,
    )
    # the weight of `heavy` fits, and its 1000 sigops at 20 each do not
    assert heavy.weight < 10_000
    monkeypatch.setattr(mining, "MAX_BLOCK_WEIGHT", 8_000 + 10_000)

    template = create_new_block(funded, SCRIPT)

    assert template.block.transactions[1:] == [light]


def test_a_package_under_one_satoshi_per_kvb_is_left_out(funded: Node) -> None:
    """Core's `addChunks` stops at `DEFAULT_BLOCK_MIN_TX_FEE`, 1 sat/kvB."""
    cbs = coinbases(funded, 4)
    pool(funded, spend([OutPoint(cbs[0].id, 0)], [SUBSIDY]), 0)
    cheap = pool(funded, spend([OutPoint(cbs[1].id, 0)], [SUBSIDY - 1]), 1)
    # one satoshi over more than 1000 vbytes is just under the floor
    long = spend([OutPoint(cbs[2].id, 0)], [SUBSIDY - 1])
    long.vout.append(TxOut(0, b"j" + bytes(1_000)))
    pool(funded, long, 1)
    assert cheap.vsize < 1_000 < long.vsize
    # exactly 1 sat/kvB is kept: Core stops only below it
    exact = spend([OutPoint(cbs[3].id, 0)], [SUBSIDY - 1])
    exact.vout.append(TxOut(0, b"j" + bytes(1_000 - exact.vsize - 30)))
    while exact.vsize < 1_000:
        exact.vout[-1] = TxOut(0, exact.vout[-1].script_pub_key.script + b"\x00")
    assert exact.vsize == 1_000
    pool(funded, exact, 1)

    template = create_new_block(funded, SCRIPT)

    assert template.block.transactions[1:] == [cheap, exact]


def test_a_block_the_chain_would_refuse_is_not_a_template(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `CreateNewBlock` throws `TestBlockValidity failed: <reason>`."""
    monkeypatch.setattr(
        mining, "check_block_validity", lambda *_, **__: "bad-cb-length"
    )

    with pytest.raises(TemplateError, match="TestBlockValidity failed: bad-cb-length"):
        create_new_block(node, SCRIPT)


def test_a_failed_trial_leaves_the_utxo_set_as_it_was(funded: Node) -> None:
    """A spend staged before the block failed can be spent by the next one."""
    cbs = coinbases(funded, 1)
    good = spend([OutPoint(cbs[0].id, 0)], [SUBSIDY - FEE])
    missing = generate_random_transaction()
    block = create_new_block(funded, SCRIPT).block
    block.transactions.extend([good, missing])
    rebuilt(block)

    assert (
        check_block_validity(funded, block, check_merkle_root=True)
        == "bad-txns-inputs-missingorspent"
    )

    block = create_new_block(funded, SCRIPT).block
    block.transactions.append(good)
    rebuilt(block)
    mined = mine_template(funded, block)
    assert funded.chainstate.block_index.active_chain[-1] == mined.header.hash


def test_a_transaction_not_final_in_the_next_block_is_left_out_with_its_children(
    funded: Node,
) -> None:
    """Finality is asked at the next height and the tip's median time past."""
    cbs = coinbases(funded, 2)
    height = len(funded.chainstate.block_index.active_chain)
    waiting = pool(
        funded,
        spend(
            [OutPoint(cbs[0].id, 0)],
            [SUBSIDY - 9_000],
            lock_time=height + 10,
            sequence=0,
        ),
        9_000,
    )
    pool(funded, spend([OutPoint(waiting.id, 0)], [SUBSIDY - 18_000]), 9_000)
    ready = pool(
        funded,
        spend(
            [OutPoint(cbs[1].id, 0)],
            [SUBSIDY - 1_000],
            lock_time=height - 1,
            sequence=0,
        ),
        1_000,
    )

    template = create_new_block(funded, SCRIPT)

    assert template.block.transactions[1:] == [ready]


def test_a_block_of_given_transactions_pays_the_subsidy_alone(funded: Node) -> None:
    """`generateblock`'s block holds the list as given, fees not claimed."""
    cbs = coinbases(funded, 2)
    first = spend([OutPoint(cbs[0].id, 0)], [SUBSIDY - 7_000])
    second = spend([OutPoint(first.id, 0)], [SUBSIDY - 7_000 - 3_000])

    block = block_with_transactions(funded, SCRIPT, [first, second])

    assert block.transactions[1:] == [first, second]
    assert block.transactions[0].vout[0].value == SUBSIDY
    assert check_block_validity(funded, block, check_merkle_root=False) is None


class _Interrupt:
    """What `solve_block` reads of a node: a terminate flag that is set."""

    def __init__(self) -> None:
        self.terminate_flag = threading.Event()
        self.terminate_flag.set()


def unsolvable(node: Node) -> Block:
    """Return a block no nonce solves: a target of one."""
    block = create_new_block(node, SCRIPT).block
    block.header.bits = bytes.fromhex("03000001")
    return block


def test_solving_counts_the_nonces_it_spent(node: Node) -> None:
    """A solved block comes back with the tries left, one per failed nonce."""
    block = create_new_block(node, SCRIPT).block
    nonce = block.header.nonce

    solved, left = finish(solve_block(node, block, 1_000))

    assert solved is block
    assert block.header.hash <= block.header.target
    assert left == 1_000 - (block.header.nonce - nonce)


def test_solving_stops_where_the_tries_are_spent(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The search takes its tries in chunks and gives none back."""
    monkeypatch.setattr(mining, "_NONCE_CHUNK", 4)
    block = unsolvable(node)

    solved, left = finish(solve_block(node, block, 11))

    assert (solved, left) == (None, 0)
    assert block.header.nonce == 11


def test_solving_starts_over_where_the_nonces_run_out(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last nonce is tried, and the caller builds another block."""
    monkeypatch.setattr(mining, "_NONCE_CHUNK", 4)
    block = unsolvable(node)
    block.header.nonce = NONCE_SPACE - 6

    solved, left = finish(solve_block(node, block, 100))

    assert (solved, left) == (None, 94)
    assert block.header.nonce == 0


def test_solving_stops_when_the_node_is_stopping(node: Node) -> None:
    """A node asked to stop gives back what it was given."""
    block = create_new_block(node, SCRIPT).block

    solved, left = finish(
        solve_block(
            _Interrupt(),  # type: ignore[arg-type]
            block,
            1_000,
        )
    )

    assert (solved, left) == (None, 1_000)


def test_solving_hands_the_thread_back_after_each_chunk(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One step searches one chunk, so the loop runs between two of them."""
    monkeypatch.setattr(mining, "_NONCE_CHUNK", 4)
    block = unsolvable(node)
    search = solve_block(node, block, 11)

    assert next(search) is True
    assert block.header.nonce == 4
    assert next(search) is True
    assert block.header.nonce == 8
    assert finish(search) == (None, 0)
    assert block.header.nonce == 11


def test_solving_ends_at_the_next_chunk_once_the_node_stops(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `GenerateBlock` looks at `m_interrupt` on every try."""
    monkeypatch.setattr(mining, "_NONCE_CHUNK", 4)
    block = unsolvable(node)
    search = solve_block(node, block, 11)
    assert next(search) is True

    node.terminate_flag.set()

    assert finish(search) == (None, 7)
    assert block.header.nonce == 4


def test_a_block_whose_parent_was_invalidated_meanwhile_is_refused(
    node: Node,
) -> None:
    """Core's `AcceptBlockHeader` refuses it `bad-prevblk`."""
    mine(node, 1)
    block = create_new_block(node, SCRIPT).block
    tip = node.chainstate.block_index.active_chain[-1]
    callbacks["invalidateblock"](node, cast("RpcConnection", None), [tip.hex()])
    solved, _ = finish(solve_block(node, block, DEFAULT_MAX_TRIES))
    assert solved is not None

    assert accept_block(node, solved) == "bad-prevblk"
    assert solved.header.hash not in node.chainstate.block_index.header_dict


def test_a_block_whose_tip_moved_on_meanwhile_is_stored_on_its_branch(
    node: Node,
) -> None:
    """As Core's `ProcessNewBlock` stores it: the first block seen stays tip."""
    other = ScriptPubKey(bytes.fromhex("0014" + "00" * 20), "regtest")
    block = create_new_block(node, other).block
    [first] = mine(node, 1)
    solved, _ = finish(solve_block(node, block, DEFAULT_MAX_TRIES))
    assert solved is not None

    assert accept_block(node, solved) is None
    assert node.chainstate.block_index.active_chain[-1] == first
    assert node.block_db.get_block(solved.header.hash) is not None


def rebuilt(block: Block) -> Block:
    """Give `block` a merkle root and witness commitment of what it holds."""
    coinbase = block.transactions[0]
    marker = bytes.fromhex("6a24aa21a9ed")
    outputs = [
        o for o in coinbase.vout if not o.script_pub_key.script.startswith(marker)
    ]
    outputs.append(witness_commitment_output(block.transactions, bytes(32)))
    coinbase.vout = outputs
    block.header.merkle_root = merkle_root_and_mutated_from_transactions(
        block.transactions
    )[0]
    return block


Change = Callable[[Block, list[Tx]], object]


def appended(make: Callable[[list[Tx]], Tx]) -> Change:
    """Return a change that adds the transaction `make` builds."""

    def change(block: Block, cbs: list[Tx]) -> None:
        block.transactions.append(make(cbs))
        rebuilt(block)

    return change


def coinbase_script_sig(script_sig: bytes) -> Change:
    """Return a change that gives the coinbase another `script_sig`."""

    def change(block: Block, _cbs: list[Tx]) -> None:
        block.transactions[0].vin[0].script_sig = script_sig
        rebuilt(block)

    return change


def coinbase_witness(stack: list[bytes]) -> Change:
    """Return a change that gives the coinbase another witness stack."""

    def change(block: Block, _cbs: list[Tx]) -> None:
        block.transactions[0].vin[0].script_witness = Witness(
            stack, check_validity=False
        )

    return change


def header(field: str, value: object) -> Change:
    """Return a change that sets a field of the header."""
    return lambda block, _cbs: setattr(block.header, field, value)


def later(delta: timedelta) -> Change:
    """Return a change that moves the header time."""
    return lambda block, _cbs: setattr(block.header, "time", block.header.time + delta)


def richer_coinbase(block: Block, _cbs: list[Tx]) -> None:
    """Claim one satoshi more than the subsidy."""
    coinbase = block.transactions[0]
    coinbase.vout[0] = TxOut(coinbase.vout[0].value + 1, anyone_can_spend())
    rebuilt(block)


def second_coinbase(block: Block, _cbs: list[Tx]) -> None:
    """Add a second coinbase."""
    extra = copy.deepcopy(block.transactions[0])
    extra.vin[0].script_sig += b"\x01"
    block.transactions.append(extra)
    rebuilt(block)


def uncommitted_witness(block: Block, _cbs: list[Tx]) -> None:
    """Add a transaction with a witness to a block with no commitment."""
    tx = generate_random_transaction()
    tx.vin[0].script_witness = Witness([b"\x01"], check_validity=False)
    block.transactions.append(tx)
    block.transactions[0].vout = block.transactions[0].vout[:1]
    block.transactions[0].vin[0].script_witness = Witness([], check_validity=False)
    block.header.merkle_root = merkle_root_and_mutated_from_transactions(
        block.transactions
    )[0]


def twice(block: Block, cbs: list[Tx]) -> None:
    """Add one spend twice."""
    tx = spend([OutPoint(cbs[0].id, 0)], [SUBSIDY - FEE])
    block.transactions.extend([tx, tx])
    rebuilt(block)


def long_block(block: Block, _cbs: list[Tx]) -> None:
    """Add transactions until the block is over a million bytes."""
    block.transactions.extend(
        Tx(
            version=2,
            lock_time=0,
            vin=[TxIn(OutPoint(bytes([i, j]) * 16, 0), b"", 0xFFFFFFFF)],
            vout=[TxOut(0, b"\x6a" + bytes(200_000))],
            check_validity=False,
        )
        for i in range(10)
        for j in range(2)
    )
    rebuilt(block)


def heavy_block(block: Block, _cbs: list[Tx]) -> None:
    """Add transactions whose witnesses take the block past its weight."""
    block.transactions.extend(
        Tx(
            version=2,
            lock_time=0,
            vin=[
                TxIn(
                    OutPoint(bytes([i]) * 32, 0),
                    b"",
                    0xFFFFFFFF,
                    Witness([bytes(200_000)], check_validity=False),
                    check_validity=False,
                )
            ],
            vout=[TxOut(0, b"\x6a")],
            check_validity=False,
        )
        for i in range(20)
    )
    rebuilt(block)


def spend_of(index: int, **kwargs: int) -> Callable[[list[Tx]], Tx]:
    """Return a builder of a spend of the coinbase at `index`."""
    return lambda cbs: spend([OutPoint(cbs[index].id, 0)], [SUBSIDY - FEE], **kwargs)


# every row is a word `bitcoind` v31.1.0 answers for the same block over the
# same chain, run by hand
REASONS: list[tuple[str, Change, str | None]] = [
    ("valid", lambda _block, _cbs: None, None),
    ("merkle", header("merkle_root", bytes(range(32))), "bad-txnmrklroot"),
    ("prev", header("previous_block_hash", bytes(32)), "inconclusive-not-best-prevblk"),
    ("old", later(-timedelta(days=3)), "time-too-old"),
    ("new", later(timedelta(days=3)), "time-too-new"),
    ("bits", header("bits", bytes.fromhex("1d00ffff")), "bad-diffbits"),
    ("version", header("version", 1), "bad-version(0x00000001)"),
    ("empty", lambda block, _cbs: block.transactions.clear(), "bad-txnmrklroot"),
    ("multiple", second_coinbase, "bad-cb-multiple"),
    ("height", coinbase_script_sig(b"\x01\x09\x00"), "bad-cb-height"),
    ("amount", richer_coinbase, "bad-cb-amount"),
    ("nonce", coinbase_witness([bytes(31)]), "bad-witness-nonce-size"),
    ("commitment", coinbase_witness([bytes(range(32))]), "bad-witness-merkle-match"),
    (
        "missing",
        appended(lambda _cbs: generate_random_transaction()),
        "bad-txns-inputs-missingorspent",
    ),
    ("mature", appended(spend_of(0)), None),
    ("immature", appended(spend_of(99)), "bad-txns-premature-spend-of-coinbase"),
    ("twice", twice, "bad-txns-inputs-missingorspent"),
    (
        "unfinal",
        appended(spend_of(0, lock_time=500, sequence=0)),
        "bad-txns-nonfinal",
    ),
    ("unexpected", uncommitted_witness, "unexpected-witness"),
    ("long", long_block, "bad-blk-length"),
    ("heavy", heavy_block, "bad-blk-weight"),
]


def test_each_reason_is_the_word_core_answers(funded: Node) -> None:
    """`check_block_validity` answers Core's word for each block."""
    cbs = coinbases(funded, 100)
    base = create_new_block(funded, SCRIPT).block

    def reason(change: Change) -> str | None:
        block = copy.deepcopy(base)
        change(block, cbs)
        return check_block_validity(funded, block, check_merkle_root=True)

    assert {name: reason(change) for name, change, _ in REASONS} == {
        name: expected for name, _, expected in REASONS
    }


def test_a_block_before_segwit_is_not_asked_for_a_commitment(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before segwit a commitment is not read, and a witness is not allowed."""
    with_consensus(monkeypatch, node, segwit_height=1000)
    block = create_new_block(node, SCRIPT).block
    assert check_block_validity(node, block, check_merkle_root=True) is None

    tx = generate_random_transaction()
    tx.vin[0].script_witness = Witness([b"\x01"], check_validity=False)
    block.transactions.append(tx)
    rebuilt(block)

    assert check_block_validity(node, block, check_merkle_root=True) == (
        "unexpected-witness"
    )


def test_a_period_start_dated_before_its_parent_is_a_timewarp_attack(
    funded: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BIP94 bounds a period's first block, and the template keeps to it."""
    with_consensus(monkeypatch, funded, enforce_bip94=True, pow_target_timespan=1_200)
    # a tip dated far ahead of the median time past, at the height before
    # a period starts: the fixture's 106 blocks leave an odd chain length
    block_index = funded.chainstate.block_index
    assert len(block_index.active_chain) % 2 == 1
    template = create_new_block(funded, SCRIPT)
    template.block.header.time += timedelta(seconds=3_000)
    mine_template(funded, template.block)
    tip = block_index.header_dict[block_index.active_chain[-1]].header

    block = create_new_block(funded, SCRIPT).block

    assert block.header.time == tip.time - timedelta(seconds=600)
    assert check_block_validity(funded, block, check_merkle_root=True) is None
    block.header.time = tip.time - timedelta(seconds=601)
    assert check_block_validity(funded, block, check_merkle_root=True) == (
        "time-timewarp-attack"
    )


def test_a_block_with_nothing_in_it_has_the_null_merkle_root(node: Node) -> None:
    """With no transactions the root is null, and the length is wrong."""
    block = create_new_block(node, SCRIPT).block
    block.transactions.clear()

    block.header.merkle_root = bytes(32)
    assert check_block_validity(node, block, check_merkle_root=True) == "bad-blk-length"
    block.header.merkle_root = bytes(range(32))
    assert (
        check_block_validity(node, block, check_merkle_root=True) == "bad-txnmrklroot"
    )


def test_a_duplicated_transaction_is_not_a_merkle_root_mismatch(funded: Node) -> None:
    """CVE-2012-2459's repeated transactions have their own reason."""
    block = create_new_block(funded, SCRIPT).block
    cbs = coinbases(funded, 2)
    first = spend([OutPoint(cbs[0].id, 0)], [SUBSIDY - FEE])
    second = spend([OutPoint(cbs[1].id, 0)], [SUBSIDY - FEE])
    block.transactions.extend([first, second, second])
    rebuilt(block)

    assert check_block_validity(funded, block, check_merkle_root=True) == (
        "bad-txns-duplicate"
    )
    # the same block, its root not asked for, is refused when it connects
    assert check_block_validity(funded, block, check_merkle_root=False) == (
        "bad-txns-inputs-missingorspent"
    )


def test_a_block_without_a_coinbase_is_refused_before_its_transactions(
    funded: Node,
) -> None:
    """The first transaction has to be the coinbase, and no other is."""
    block = create_new_block(funded, SCRIPT).block
    block.transactions.insert(
        0, spend([OutPoint(coinbases(funded, 1)[0].id, 0)], [SUBSIDY - FEE])
    )
    rebuilt(block)

    assert check_block_validity(funded, block, check_merkle_root=True) == (
        "bad-cb-missing"
    )


def test_a_transaction_that_is_not_valid_alone_is_answered_in_btclibs_words(
    funded: Node,
) -> None:
    """What `Tx.assert_valid` refuses is reported with its own text."""
    block = create_new_block(funded, SCRIPT).block
    block.transactions.append(
        Tx(
            version=2,
            lock_time=0,
            vin=[],
            vout=[TxOut(0, anyone_can_spend())],
            check_validity=False,
        )
    )
    rebuilt(block)

    reason = check_block_validity(funded, block, check_merkle_root=True)

    assert reason == "Missing inputs"


def test_a_block_over_the_sigop_limit_is_refused_by_its_legacy_count(
    funded: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bad-blk-sigops` is asked of the legacy count, before connecting."""
    block = create_new_block(funded, SCRIPT).block
    monkeypatch.setattr(Block, "sig_op_count", property(lambda _: 80_000))

    assert check_block_validity(funded, block, check_merkle_root=True) == (
        "bad-blk-sigops"
    )


def test_outputs_over_the_inputs_are_answered_in_cores_word(funded: Node) -> None:
    """Core's `bad-txns-in-belowout`, without its debug text."""
    block = create_new_block(funded, SCRIPT).block
    block.transactions.append(
        spend([OutPoint(coinbases(funded, 1)[0].id, 0)], [SUBSIDY + 1])
    )
    rebuilt(block)

    reason = check_block_validity(funded, block, check_merkle_root=True)

    assert reason == "bad-txns-in-belowout"


def test_a_block_that_fails_to_connect_is_answered_with_the_reason(
    funded: Node,
) -> None:
    """`accept_block` answers why `update_chain` refused, and leaves the tip."""
    tip = funded.chainstate.block_index.active_chain[-1]
    block = create_new_block(funded, SCRIPT).block
    block.transactions.append(generate_random_transaction())
    rebuilt(block)
    solved, _ = finish(solve_block(funded, block, DEFAULT_MAX_TRIES))
    assert solved is not None

    assert accept_block(funded, solved) == "prevout not found"
    assert funded.chainstate.block_index.active_chain[-1] == tip


def test_a_refusal_with_details_is_answered_with_the_reason_alone(
    funded: Node,
) -> None:
    """`accept_block` answers `GetRejectReason`, not `ToString`."""
    block = create_new_block(funded, SCRIPT).block
    block.transactions.append(
        spend([OutPoint(coinbases(funded, 1)[0].id, 0)], [SUBSIDY + 1])
    )
    rebuilt(block)
    solved, _ = finish(solve_block(funded, block, DEFAULT_MAX_TRIES))
    assert solved is not None

    assert accept_block(funded, solved) == "bad-txns-in-belowout"


def test_a_failure_of_the_node_stops_it_and_is_not_an_answer(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `update_chain` raises is not a verdict on the block."""

    def broken(_: Node) -> None:
        raise OSError("disk")

    monkeypatch.setattr(mining, "update_chain", broken)
    block = create_new_block(node, SCRIPT).block
    solved, _ = finish(solve_block(node, block, DEFAULT_MAX_TRIES))
    assert solved is not None

    with pytest.raises(OSError, match="disk"):
        accept_block(node, solved)

    assert node.terminate_flag.is_set()
