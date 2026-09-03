# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`update_chain`/`verify_mempool_acceptance`: connect, reorg, reject."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.exceptions import BTClibValueError
from btclib.p2p.inventory import Headers, Inv, Inventory, InventoryType
from btclib.script import script
from btclib.script.engine.flags import ScriptFlag
from btclib.tx.limits import COINBASE_MATURITY
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node, main
from btclib_node.chains import RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate import utxo_index as utxo_index_module
from btclib_node.chainstate.block_index import BlockIndex, BlockInfo, BlockStatus
from btclib_node.config import Config
from btclib_node.constants import (
    MAX_TIP_AGE,
    MIN_BLOCKS_TO_KEEP,
    MIN_PRUNE_TARGET_MIB,
    NodeStatus,
)
from btclib_node.exceptions import (
    ChainstateInconsistencyError,
    MissingPrevoutError,
    NonStandardTxError,
)
from btclib_node.interpreter import check_transactions, get_flags
from btclib_node.main import update_chain, verify_mempool_acceptance
from tests import (
    build_block,
    generate_coinbase,
    generate_random_chain,
    generate_random_header_chain,
    generate_random_transaction,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from btclib.block import Block

    from btclib_node.block_db import Coin
    from btclib_node.p2p.connection import Connection


@pytest.fixture
def node(regtest_node: Callable[[], Node]) -> Node:
    """Give one header-synced regtest node, built fresh for the test."""
    return regtest_node()


def connect(node: Node, chain: list[Block]) -> BlockIndex:
    """Offer a chain to the node, drive it to connect what it will, and flush.

    The flush is what most callers of this helper actually want: a
    result on disk, staged nowhere, to make assertions against --
    `Chainstate.flush`'s own bound (`UtxoIndex._FLUSH_BOUND`) is a
    throughput question for a real sync and not one this helper's own
    short chains are testing.
    """
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(node)
    node.chainstate.flush()
    return block_index


def rejected_because(node: Node, block: Block, phrase: str) -> None:
    """Assert `block` is `node`'s own last rejection, and that `phrase` is why.

    `Node.last_rejected_block` pairs the hash `update_chain`'s trial
    loop was on with the exception it raised; matching both is what
    tells a block refused for its own rule apart from one refused for a
    different rule ranked ahead of it in the same per-block gate --
    the gap btclib-org/btclib-node#587 is about, where any raise
    anywhere in `_validate_block`/`check_transactions` satisfied a bare
    `not in active_chain`. `phrase` is checked with `in` rather than
    `==`: the exact wording is btclib's or this tree's own to change,
    not an interface either promises to keep, and a substring naming
    the rule is what a future rewording is least likely to break.
    """
    assert node.last_rejected_block is not None
    failed_hash, exc = node.last_rejected_block
    assert failed_hash == block.header.hash
    assert phrase in str(exc), str(exc)


def test_chain(node: Node) -> None:
    """A chain of headers added in batches of at most 2000 all connect."""
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
    """Return a transaction spending `prevout_tx`'s first output for `value`."""
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


def test_reject_block_that_prints_money(node: Node) -> None:
    """A block whose output exceeds its input's value fails to connect."""
    # Script validation never reads the amounts except through the
    # sig_hash, so nothing in the engine notices an output larger than
    # the input it spends. The chain is COINBASE_MATURITY long and the
    # spend is chain[0]'s own coinbase, not chain[-1]'s: a fresher one
    # would be refused for prematurity before ever reaching the rule
    # this test is about.
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    bad = build_block(
        chain[-1].header.hash,
        [
            generate_coinbase(height=len(chain) + 1),
            spend(funding, funding.vout[0].value + 1),
        ],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "Invalid transaction amounts")


def test_reject_block_with_a_failing_script(node: Node) -> None:
    """A block with an input that fails script validation fails to connect."""
    # An input that does not verify has to fail the block. It used to be
    # written to errors/ and swallowed, inside a worker pool, so nothing
    # reached update_chain and the block was connected anyway. The chain
    # is COINBASE_MATURITY long and the spend is chain[0]'s own coinbase
    # for the same reason as the sibling test above.
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    unspendable = spend(
        funding,
        funding.vout[0].value,
        script_sig=script.serialize(["OP_RETURN"]),
    )
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), unspendable],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "OP_RETURN")


def test_reject_block_whose_coinbase_pays_more_than_subsidy_plus_fees(
    node: Node,
) -> None:
    """A coinbase paying far more than subsidy plus fees fails to connect."""
    # btclib-org/btclib-node#568: nothing used to compare a coinbase
    # against what it is allowed to pay, so this connected.
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    printed = 21_000_000 * 10**8
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(printed, height=len(chain) + 1)],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-cb-amount")


def test_reject_block_whose_coinbase_does_not_commit_to_its_height(
    node: Node,
) -> None:
    """A coinbase committing to no height at all fails to connect (BIP34)."""
    # btclib-org/btclib-node#571: Block.assert_valid_contextual was never
    # called, so this connected -- regtest enforces BIP34 from height 1.
    chain = generate_random_chain(1, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    bad = build_block(chain[-1].header.hash, [generate_coinbase()], len(chain))
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "invalid coinbase height")


def test_reject_block_spending_a_coinbase_one_short_of_maturity(node: Node) -> None:
    """A spend of a coinbase `COINBASE_MATURITY - 1` deep fails to connect.

    ISS 569: the UTXO record carried neither a coin's height nor whether
    it came from a coinbase, so nothing on this path could tell a fresh
    coinbase from one old enough to spend -- this connected exactly as
    the one `COINBASE_MATURITY` blocks old does in the test right after
    this one.
    """
    chain = generate_random_chain(COINBASE_MATURITY - 1, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    bad = build_block(
        chain[-1].header.hash,
        [
            generate_coinbase(height=len(chain) + 1),
            spend(funding, funding.vout[0].value),
        ],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-txns-premature-spend-of-coinbase")


def test_a_coinbase_spend_at_exactly_maturity_connects(node: Node) -> None:
    """A spend of a coinbase exactly `COINBASE_MATURITY` blocks old connects."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    good = build_block(
        chain[-1].header.hash,
        [
            generate_coinbase(height=len(chain) + 1),
            spend(funding, funding.vout[0].value),
        ],
        len(chain),
    )
    connect(node, [good])

    assert good.header.hash in block_index.active_chain
    assert len(block_index.active_chain) == connected + 1


def test_reject_a_mempool_spend_of_an_immature_coinbase(node: Node) -> None:
    """`verify_mempool_acceptance` refuses the same premature spend.

    Core enforces `COINBASE_MATURITY` at both call sites -- `ConnectBlock`
    and mempool acceptance's own `AcceptToMemoryPoolWorker`
    (`src/validation.cpp:897` and `:2544`, at bitcoin/bitcoin@204256c73f)
    -- so a mempool that only enforced it on the block-connection path
    would relay a spend no peer accepting it into a block ever will.
    """
    chain = generate_random_chain(COINBASE_MATURITY - 1, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    premature = generate_random_transaction(funding.id, value=funding.vout[0].value)
    with pytest.raises(BTClibValueError, match="bad-txns-premature-spend-of-coinbase"):
        verify_mempool_acceptance(node, premature)


def locked_spend(
    prevout_tx: Tx,
    value: int,
    lock_time: int,
    sequence: int,
    *,
    version: int = 1,
    script_sig: bytes | None = None,
) -> Tx:
    """Return a transaction spending `prevout_tx`'s first output."""
    return Tx(
        version=version,
        lock_time=lock_time,
        vin=[
            TxIn(
                prev_out=OutPoint(prevout_tx.id, 0),
                script_sig=script_sig
                if script_sig is not None
                else script.serialize([b"\x11" * 32]),
                sequence=sequence,
            )
        ],
        vout=[
            TxOut(value=value, script_pub_key=script.serialize([b"\x22" * 32])),
        ],
    )


def relative_locked_spend(prevout_tx: Tx, value: int, sequence: int) -> Tx:
    """Return a version-2 transaction spending `prevout_tx`, sequence set.

    Version 2, not 1: BIP68 binds a relative lock only from that version
    on (`btclib.tx.tx_context.assert_sequence_locks`' own docstring says
    why).
    """
    return locked_spend(prevout_tx, value, lock_time=0, sequence=sequence, version=2)


def test_reject_block_whose_coinbase_duplicates_an_unspent_txid(node: Node) -> None:
    """Two blocks sharing a coinbase: the second is refused for BIP30.

    ISS 570 / CVE-2012-1909's shape: nothing checked whether a
    coinbase's own txid already named an unspent output, so the second
    block's own write silently overwrote the first's, and a reorg away
    from it would have deleted an output the first block's own branch
    still carries. `UtxoIndex.add_block`'s own BIP30 check runs before
    `block.assert_valid_contextual` (BIP34), so this is refused for
    BIP30 regardless of whether the reused coinbase would also fail
    BIP34's own `bad-cb-height` at this height.
    """
    genesis_hash = RegTest().genesis.hash
    duplicate = generate_coinbase(height=1)
    first = build_block(genesis_hash, [duplicate], 0)
    connect(node, [first])
    block_index = node.chainstate.block_index
    assert first.header.hash in block_index.active_chain

    bad = build_block(first.header.hash, [duplicate], 1)
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    rejected_because(node, bad, "bad-txns-BIP30")

    # the first block's own coinbase output survives the refused
    # duplicate -- the CVE's actual danger, and not covered by the
    # refusal alone
    out_point = OutPoint(duplicate.id, 0)
    key = b"utxo-" + out_point.serialize(check_validity=False)
    assert node.chainstate.utxo_index.db.get(key) is not None


def test_reject_block_with_a_transaction_locked_to_the_future(node: Node) -> None:
    """A transaction locked to a 2033 timestamp fails to connect.

    ISS 572's own probe: `sequence=0` -- not `SEQUENCE_FINAL` -- so
    Core's own escape hatch does not rescue it.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    nonfinal = locked_spend(
        funding, funding.vout[0].value, lock_time=2_000_000_000, sequence=0
    )
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), nonfinal],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-txns-nonfinal")


def test_a_transaction_locked_to_an_already_reached_height_connects(
    node: Node,
) -> None:
    """A transaction whose height-based lock_time has passed connects."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    final = locked_spend(funding, funding.vout[0].value, lock_time=1, sequence=0)
    good = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), final],
        len(chain),
    )
    connect(node, [good])

    assert good.header.hash in block_index.active_chain
    assert len(block_index.active_chain) == connected + 1


def test_reject_block_whose_relative_lock_is_not_satisfied(node: Node) -> None:
    """A BIP68 relative lock a hundred blocks away fails to connect.

    `funding` is `COINBASE_MATURITY` blocks old by the time this
    connects -- exactly mature enough to spend, and not old enough for
    a relative lock fifty blocks past that.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=COINBASE_MATURITY + 50
    )
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), unmet],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-txns-nonfinal")


def test_relative_lock_is_not_enforced_when_bip113_is_not_active(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same unmet relative lock connects once BIP113 reads as inactive.

    `_validate_block` skips `assert_sequence_locks` entirely rather than
    calling it with a flag, `btclib.tx.tx_context`'s own module
    docstring says why -- so what this proves is the skip itself, not
    the rule `test_reject_block_whose_relative_lock_is_not_satisfied`
    above already covers. `main.get_flags` is patched to answer as if
    RegTest had not yet activated CHECKSEQUENCEVERIFY, which it does
    from height 1 on (`chains.RegTest`'s own docstring), so there is no
    real chain height this scenario reaches otherwise.
    """

    def flags_without_csv(*args: Any, **kwargs: Any) -> ScriptFlag:
        return get_flags(*args, **kwargs) & ~ScriptFlag.CHECKSEQUENCEVERIFY

    monkeypatch.setattr(main, "get_flags", flags_without_csv)

    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=COINBASE_MATURITY + 50
    )
    good = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), unmet],
        len(chain),
    )
    connect(node, [good])

    assert good.header.hash in block_index.active_chain
    assert len(block_index.active_chain) == connected + 1


def test_a_relative_lock_satisfied_by_elapsed_blocks_connects(node: Node) -> None:
    """A BIP68 relative lock already satisfied by elapsed blocks connects."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    met = relative_locked_spend(funding, funding.vout[0].value, sequence=50)
    good = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), met],
        len(chain),
    )
    connect(node, [good])

    assert good.header.hash in block_index.active_chain
    assert len(block_index.active_chain) == connected + 1


def test_reject_block_whose_time_based_relative_lock_is_not_satisfied(
    node: Node,
) -> None:
    """A BIP68 time-based relative lock far in the future fails to connect.

    Unlike the height-based pair above, this exercises `_validate_block`'s
    own `ancestor_median_time_past` closure -- `header_at_height` walking
    back through real headers rather than a stub -- since a height-based
    lock never reaches it.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    type_flag = 1 << 22
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=type_flag | 1000
    )
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), unmet],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-txns-nonfinal")


def test_a_time_based_relative_lock_satisfied_by_elapsed_time_connects(
    node: Node,
) -> None:
    """A BIP68 time-based relative lock of zero units connects immediately."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    type_flag = 1 << 22
    met = relative_locked_spend(funding, funding.vout[0].value, sequence=type_flag | 0)
    good = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), met],
        len(chain),
    )
    connect(node, [good])

    assert good.header.hash in block_index.active_chain
    assert len(block_index.active_chain) == connected + 1


def test_reject_a_mempool_spend_that_is_not_final(node: Node) -> None:
    """`verify_mempool_acceptance` refuses the same non-final transaction.

    Core checks finality in the mempool too, against the tip rather
    than the connecting block -- a mempool that skipped this would
    relay a transaction it would then refuse to connect.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    nonfinal = locked_spend(
        funding, funding.vout[0].value, lock_time=2_000_000_000, sequence=0
    )
    with pytest.raises(BTClibValueError, match="bad-txns-nonfinal"):
        verify_mempool_acceptance(node, nonfinal)


def test_a_nonfinal_unverifiable_mempool_spend_is_refused_as_nonfinal(
    node: Node,
) -> None:
    """A candidate both non-final and script-invalid is refused as non-final.

    Asserting the verdict alone -- `BTClibValueError` -- passes under
    either order, a failing script raising it too. Matching on the
    finality message pins the order instead: refused any other way, the
    script check ran ahead of the two cheap lock checks (ISS 829).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    nonfinal_and_unverifiable = locked_spend(
        funding,
        funding.vout[0].value,
        lock_time=2_000_000_000,
        sequence=0,
        script_sig=script.serialize(["OP_RETURN"]),
    )
    with pytest.raises(BTClibValueError, match="bad-txns-nonfinal"):
        verify_mempool_acceptance(node, nonfinal_and_unverifiable)


def test_a_mempool_spend_locked_to_an_already_reached_height_is_accepted(
    node: Node,
) -> None:
    """`verify_mempool_acceptance` accepts a transaction already final."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    final = locked_spend(funding, funding.vout[0].value, lock_time=1, sequence=0)
    fee = verify_mempool_acceptance(node, final)
    assert fee >= 0


def test_reject_a_mempool_spend_whose_relative_lock_is_not_satisfied(
    node: Node,
) -> None:
    """`verify_mempool_acceptance` refuses the same unmet BIP68 lock."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=COINBASE_MATURITY + 50
    )
    with pytest.raises(BTClibValueError, match="bad-txns-nonfinal"):
        verify_mempool_acceptance(node, unmet)


def test_a_mempool_spend_whose_relative_lock_is_satisfied_is_accepted(
    node: Node,
) -> None:
    """`verify_mempool_acceptance` accepts a satisfied BIP68 relative lock."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    met = relative_locked_spend(funding, funding.vout[0].value, sequence=50)
    fee = verify_mempool_acceptance(node, met)
    assert fee >= 0


def test_reject_a_mempool_spend_whose_time_based_relative_lock_is_not_satisfied(
    node: Node,
) -> None:
    """`verify_mempool_acceptance` refuses the same unmet time-based lock.

    Exercises `verify_mempool_acceptance`'s own `ancestor_median_time_past`
    closure, which the height-based pair above never reaches.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    type_flag = 1 << 22
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=type_flag | 1000
    )
    with pytest.raises(BTClibValueError, match="bad-txns-nonfinal"):
        verify_mempool_acceptance(node, unmet)


def test_a_mempool_spend_whose_time_based_relative_lock_is_satisfied(
    node: Node,
) -> None:
    """`verify_mempool_acceptance` accepts a satisfied time-based lock."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    type_flag = 1 << 22
    met = relative_locked_spend(funding, funding.vout[0].value, sequence=type_flag | 0)
    fee = verify_mempool_acceptance(node, met)
    assert fee >= 0


def test_a_mempool_chained_spend_s_zero_relative_lock_is_satisfied(
    node: Node,
) -> None:
    """A relative lock of zero against an unconfirmed parent is trivially met.

    `verify_mempool_acceptance`'s own `prevout_coins` stands an
    unconfirmed parent's own height in for Core's `MEMPOOL_HEIGHT`
    convention -- assumed to confirm in the very next block, i.e. at
    `spend_height` itself.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    parent = generate_random_transaction(funding.id, value=funding.vout[0].value)
    verify_mempool_acceptance(node, parent)
    node.mempool.add_tx(parent)

    child = relative_locked_spend(parent, parent.vout[0].value, sequence=0)
    fee = verify_mempool_acceptance(node, child)
    assert fee >= 0


def test_a_mempool_chained_spend_s_relative_lock_cannot_yet_be_met(
    node: Node,
) -> None:
    """A nonzero relative lock against an unconfirmed parent is never met.

    The parent is assumed to confirm alongside this transaction at the
    earliest, so any lock asking for a block *after* that can never be
    satisfied while the parent is still unconfirmed.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    parent = generate_random_transaction(funding.id, value=funding.vout[0].value)
    verify_mempool_acceptance(node, parent)
    node.mempool.add_tx(parent)

    child = relative_locked_spend(parent, parent.vout[0].value, sequence=1)
    with pytest.raises(BTClibValueError, match="bad-txns-nonfinal"):
        verify_mempool_acceptance(node, child)


def test_add_tx(node: Node) -> None:
    """`verify_mempool_acceptance` accepts a prevout from chain or mempool."""
    # COINBASE_MATURITY long, and tx1 spends chain[0]'s own coinbase: a
    # fresher one would be refused for prematurity, which is a different
    # test (test_reject_a_mempool_spend_of_an_immature_coinbase, below).
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    headers = [block.header for block in chain]
    block_index = node.chainstate.block_index
    block_index.add_headers(headers)
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(node)

    invalid_tx = generate_random_transaction()
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, invalid_tx)

    tx1 = generate_random_transaction(chain[0].transactions[0].id)
    tx2 = generate_random_transaction(tx1.id)

    verify_mempool_acceptance(node, tx1)

    # We can't find the prevouts
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, tx2)

    # tx1 needs to be added to the mempool
    node.mempool.add_tx(tx1)
    verify_mempool_acceptance(node, tx2)


def test_a_mempool_candidate_is_read_against_relay_policy(node: Node) -> None:
    """`verify_mempool_acceptance` refuses a spend only standardness refuses.

    The two spends below differ in one push: `4c 01 01` is OP_PUSHDATA1
    over the byte `01`, where OP_1 is the push MINIMALDATA asks for, and
    that rule is not a consensus one -- a block carrying the first spend
    connects. `interpreter.STANDARD_FLAGS` is what refuses it here, and
    the second one being accepted is what says the refusal is that push
    and not something else about the pair. `NonStandardTxError` is the
    class the refusal reaches a caller as, which is what keeps the peer
    that relayed the transaction (`p2p/callbacks_test.py`).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    coinbase = chain[0].transactions[0]

    non_minimal = generate_random_transaction(coinbase.id)
    non_minimal.vin[0].script_sig = b"\x4c\x01\x01"
    with pytest.raises(NonStandardTxError, match="non-minimal push"):
        verify_mempool_acceptance(node, non_minimal)

    minimal = generate_random_transaction(coinbase.id)
    minimal.vin[0].script_sig = script.serialize(["OP_1"])
    assert verify_mempool_acceptance(node, minimal) == 0


def test_a_stored_coin_that_wont_parse_looks_missing_to_the_mempool(
    node: Node,
) -> None:
    """A broken `utxo-` record answers absent, the same as no record at all.

    The record corrupted here is one only this node's own earlier
    `connect` (via `UtxoIndex.finalize`) ever wrote -- `tx` below never
    supplies these bytes, so what this proves is over storage this
    node owns, not over `tx`'s own content. `UtxoIndex.get_coin` now
    matches `CDBWrapper::Read`/`CCoinsViewDB::GetCoin`'s own "absent"
    for a checksum-clean record `Coin.parse` still cannot read, since
    RocksDB's own checksum (btclib-org/btclib-node#641) is what now
    catches an actually corrupted record before this call is ever
    reached: `get_coin` answers `None`, the mempool has never heard of
    `coinbase.id` either, so `verify_mempool_acceptance` raises the
    same `MissingPrevoutError` it would over a prevout this node never
    had at all (btclib-org/btclib-node#631, btclib-org/btclib-node#650).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    coinbase = chain[0].transactions[0]
    key = b"utxo-" + OutPoint(coinbase.id, 0).serialize(check_validity=False)
    original = node.chainstate.utxo_index.db.get(key)
    assert original is not None
    node.chainstate.db.put(key, original[:1])

    tx = generate_random_transaction(coinbase.id)
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, tx)


def test_a_candidate_whose_block_has_not_arrived_is_not_connected(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`update_chain` declines a candidate without asking `block_db` a thing."""
    # headers run ahead of blocks for the whole of a sync, so the
    # commonest state of a candidate is one whose block is still being
    # fetched. It is declined before block_db is asked for anything:
    # asking and rolling back reaches the same chain, but by way of an
    # exception, on every pass of a loop that runs until the block
    # arrives.
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
    node: Node,
) -> None:
    """A complete branch connects while a separate, incomplete one is queued."""
    # get_first_candidate used to ask only whether a candidate's own tip
    # had arrived, so a branch missing a block *behind* its downloaded
    # tip still passed it -- and then update_chain found the hole and
    # gave up the whole pass, leaving that same candidate at the front
    # of the queue next time: btclib-org/btclib-node#121
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
    node: Node,
) -> None:
    """`update_chain` raises on a downloaded-but-missing block."""
    # the download manager and block_db agree by construction; this is
    # the state they would be in if they did not
    chain = generate_random_chain(1, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    # deliberately not added to node.block_db
    with pytest.raises(
        ChainstateInconsistencyError, match="just checked downloaded is missing"
    ):
        update_chain(node)


def hashes(chain: list[Block]) -> list[bytes]:
    """Return every block's own header hash, in the chain's own order."""
    return [block.header.hash for block in chain]


def settle(node: Node) -> None:
    """Drive `update_chain` until nothing outweighs the active chain further."""
    # get_first_candidate offers the shallowest block that already
    # outweighs active, not necessarily a longer fork's own tip, so one
    # call connects only as far as that block; this drives update_chain
    # until nothing outweighs active any more, the same thing connect()
    # does for a chain built from genesis
    block_index = node.chainstate.block_index
    while block_index.get_first_candidate() is not None:
        update_chain(node)


def test_a_heavier_fork_replaces_the_chain_the_node_was_on(node: Node) -> None:
    """A heavier fork replaces every block of the chain it outweighs."""
    # more than one block on the branch being left, because that is the
    # shallowest branch whose blocks have to be undone in an order: an
    # output block N created and block N+1 spent is gone from the utxo
    # set by the time N comes to be undone
    first = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, first)
    assert block_index.active_chain[1:] == hashes(first)

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)
    assert block_index.active_chain[1:] == hashes(second)
    for block_hash in hashes(first):
        assert block_hash not in block_index.active_chain


def test_a_reorg_refuses_a_missing_reverse_patch(node: Node) -> None:
    """A missing reverse patch raises `ChainstateInconsistencyError`."""
    # every block on the active chain has one, by construction; this is
    # the state block_db would be in if it did not
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.block_db.rev_patches.pop(first[-1].header.hash)

    second = generate_random_chain(3, RegTest().genesis.hash)
    with pytest.raises(ChainstateInconsistencyError, match="no reverse patch"):
        connect(node, second)


def test_a_reorg_refuses_a_missing_removed_block(node: Node) -> None:
    """A missing removed block raises `ChainstateInconsistencyError`."""
    # the reverse patch of the block being undone is enough to roll the
    # chainstate back; giving the transactions of that same block back
    # to the mempool needs the block itself, which is the gap this pins
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.block_db.blocks.pop(first[-1].header.hash)

    second = generate_random_chain(3, RegTest().genesis.hash)
    with pytest.raises(
        ChainstateInconsistencyError, match="block just removed is missing"
    ):
        connect(node, second)


def test_a_reorg_whose_own_undo_raises_propagates_and_names_no_new_block(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollback failing on the fork's own undo stops the node, blames nothing.

    `_resolve_trial_exception` (`main.py`) re-raises here rather than
    swallowing: `ChainstateInconsistencyError` is not one of
    `_CONTENT_FAILURE`'s three types, so this is this node's own
    storage proving itself inconsistent, not a new candidate's content,
    the same distinction `_CONTENT_FAILURE`'s own comment and Core's
    `ActivateBestChainStep` (`src/validation.cpp`, at
    bitcoin/bitcoin@b91d983f66) draw for a failed `DisconnectTip`. The
    rollback still runs first -- `active_chain_before` is unchanged --
    and `_resolve_trial_exception` never reaches `_record_rejection`
    here, so `Node.last_rejected_block` stays unset the same as
    `failed_hash` -- still `None` at this point in the trial, exactly
    as `update_header_index`'s own guard reads it, so none of the
    fork's own candidate blocks is invalidated either.
    """
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)
    active_chain_before = list(block_index.active_chain)

    heavier = generate_random_chain(3, RegTest().genesis.hash)
    block_index.add_headers([block.header for block in heavier])
    for block in heavier:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    def boom(rev_block: object) -> None:
        err_msg = "boom"
        raise ChainstateInconsistencyError(err_msg)

    monkeypatch.setattr(node.chainstate.utxo_index, "apply_rev_block", boom)

    with pytest.raises(ChainstateInconsistencyError, match="boom"):
        update_chain(node)

    assert block_index.active_chain == active_chain_before
    assert node.last_rejected_block is None
    for block in heavier:
        info = block_index.get_block_info(block.header.hash)
        assert info.status != BlockStatus.invalid


def test_an_io_fault_writing_the_reverse_patch_does_not_invalidate_the_block(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`block_db.add_rev_block` raising propagates, and blames no block.

    `add_rev_block` itself is a pure in-memory buffer -- `self.pending_
    rev_blocks[hash] = rev_block`, no I/O at all; the write `OSError`
    would actually come from happens later, inside `finalize`. What
    this test pins is the classification, not the origin: whatever
    `add_rev_block` raises, `OSError` here standing in for it, is not
    one of `_CONTENT_FAILURE`'s three types, so `update_chain`'s own
    except re-raises it rather than treating it as this candidate's own
    content -- the same distinction
    `test_a_reorg_whose_own_undo_raises_names_no_new_block` above pins
    for the trial's other loop.
    """
    candidate = generate_random_chain(1, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    active_chain_before = list(block_index.active_chain)
    block_index.add_headers([block.header for block in candidate])
    for block in candidate:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    def boom(rev_block: object) -> None:
        err_msg = "disk full"
        raise OSError(err_msg)

    monkeypatch.setattr(node.block_db, "add_rev_block", boom)

    with pytest.raises(OSError, match="disk full"):
        update_chain(node)

    assert block_index.active_chain == active_chain_before
    info = block_index.get_block_info(candidate[0].header.hash)
    assert info.status != BlockStatus.invalid
    assert node.last_rejected_block is None
    assert node.chainstate.utxo_index.updated_utxo_set == {}
    assert node.chainstate.utxo_index.removed_utxos == set()
    assert node.chainstate.filter_index.pending == {}
    assert node.block_db.pending_rev_blocks == {}


def test_an_io_fault_indexing_the_block_filter_does_not_invalidate_the_block(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`filter_index.add_connected_block` raising propagates the same way.

    The sibling of the test above, over the trial's other storage call:
    `add_connected_block` reads the parent's filter header off
    `KeyValueStore.get`, which is exactly the read
    btclib-org/btclib-node#620 is about.
    """
    candidate = generate_random_chain(1, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    active_chain_before = list(block_index.active_chain)
    block_index.add_headers([block.header for block in candidate])
    for block in candidate:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    def boom(block: object, rev_block: object) -> None:
        err_msg = "database is locked"
        raise OSError(err_msg)

    monkeypatch.setattr(node.chainstate.filter_index, "add_connected_block", boom)

    with pytest.raises(OSError, match="database is locked"):
        update_chain(node)

    assert block_index.active_chain == active_chain_before
    info = block_index.get_block_info(candidate[0].header.hash)
    assert info.status != BlockStatus.invalid
    assert node.last_rejected_block is None
    assert node.chainstate.utxo_index.updated_utxo_set == {}
    assert node.chainstate.utxo_index.removed_utxos == set()
    assert node.chainstate.filter_index.pending == {}
    assert node.block_db.pending_rev_blocks == {}


def test_a_stored_utxo_record_that_wont_parse_is_treated_as_absent(
    node: Node,
) -> None:
    """A truncated `utxo-` record now rejects the block, not the node.

    No monkeypatch: `node.chainstate.db.put` overwrites the same
    `utxo-` key `UtxoIndex.finalize` wrote, with a truncated copy of
    what was already there -- a checksum-clean record RocksDB's own
    read never flags, exactly the fault `_bip30_violation`'s own
    `db.get` truthiness check cannot tell from a healthy one. `bad`
    then spends that same, otherwise unspent, output: `Coin.parse`
    inside `add_block`'s prevout resolution raises `BTClibValueError`
    over bytes this node wrote for itself, not over anything `bad`
    supplied, and `add_block` now answers that the same way it answers
    a genuinely missing prevout -- `InvalidBlockInputError("prevout
    not found")` -- matching `CDBWrapper::Read`/`CCoinsViewDB::GetCoin`'s
    own "absent" rather than raising `ChainstateInconsistencyError`,
    now that RocksDB's own checksum (btclib-org/btclib-node#641) is
    what catches an actually corrupted record before this line is
    ever reached (btclib-org/btclib-node#620, btclib-org/btclib-node#650).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)

    funding = chain[0].transactions[0]
    key = b"utxo-" + OutPoint(funding.id, 0, check_validity=False).serialize(
        check_validity=False
    )
    original = node.chainstate.db.get(key)
    assert original is not None
    node.chainstate.db.put(key, original[:1])

    bad = build_block(
        chain[-1].header.hash,
        [
            generate_coinbase(height=len(chain) + 1),
            spend(funding, funding.vout[0].value),
        ],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    rejected_because(node, bad, "prevout not found")


def test_a_reorg_evicts_a_transaction_the_reorg_itself_invalidated(
    node: Node,
) -> None:
    """A reorg does not re-add a tx whose own coinbase it just abandoned."""
    # first is COINBASE_MATURITY + 1 long, so its own last block already
    # carries a second transaction -- generate_random_chain's own rule
    # -- spending first[0]'s coinbase, confirmed rather than merely
    # offered. second outweighs it and abandons the whole branch, first[0]
    # included, so _reconcile_mempool_for_reorg's own oldest-abandoned-
    # block-first walk reaches orphaned only after the coinbase it spent
    # is already undone: #85's MissingPrevoutError, not a second
    # implementation of it here, is what that walk's own except catches
    # and skips rather than re-adding.
    first = generate_random_chain(COINBASE_MATURITY + 1, RegTest().genesis.hash)
    connect(node, first)
    assert node.status == NodeStatus.BlockSynced

    orphaned = first[-1].transactions[1]
    assert orphaned.vin[0].prev_out.tx_id == first[0].transactions[0].id

    second = generate_random_chain(COINBASE_MATURITY + 2, RegTest().genesis.hash)
    connect(node, second)

    # #85: orphaned spent the abandoned branch's own coinbase, which no
    # longer exists on any chain once the reorg undoes it -- it is
    # rejected the same way any other entrant into the mempool would be,
    # and does not go back in
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, orphaned)
    assert not node.mempool.contains_tx(orphaned)


def test_a_connected_block_restarts_the_mempool_s_decay_clock(node: Node) -> None:
    """Connecting a block restarts the mempool's rolling-minimum decay clock."""
    # note_block_connected runs once per block update_chain connects to the
    # active chain, restarting Mempool.get_min_fee_rate's own decay clock --
    # Core's own removeForBlock (src/txmempool.cpp:405-427,
    # at bitcoin/bitcoin@58a7869f86) does this for every block regardless of
    # what it held. btclib-org/btclib-node#294
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    assert node.status == NodeStatus.BlockSynced

    node.mempool._rolling_min_fee_rate = 5000.0
    node.mempool._block_since_last_rolling_fee_bump = False
    node.mempool._last_rolling_fee_update = 0.0

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)

    assert node.mempool._block_since_last_rolling_fee_bump is True
    assert node.mempool._last_rolling_fee_update > 0.0


def _extend(previous_hash: bytes, start_height: int, count: int) -> list[Block]:
    # generate_random_chain restarts its own height at 0 for any start,
    # which is a timestamp that has to beat the median of *these*
    # ancestors, not a fresh chain's -- explicit, increasing heights are
    # what test_a_refused_branch_invalidates_headers_that_were_never_
    # candidates uses for the same reason
    continuation: list[Block] = []
    for height in range(start_height, start_height + count):
        block = build_block(
            previous_hash, [generate_coinbase(height=height + 1)], height
        )
        continuation.append(block)
        previous_hash = block.header.hash
    return continuation


def test_a_reorg_still_resurrects_a_transaction_its_prevout_survives(
    node: Node,
) -> None:
    """A confirmed tx whose prevout survives the reorg re-enters the mempool."""
    # #85's fix checks every re-added transaction rather than trusting
    # it: this is the other side of that, a transaction that spent an
    # output the reorg does not touch and is still good on the chain
    # that replaces the one it was confirmed on. common is
    # COINBASE_MATURITY long so that resurrectable, spending its own
    # first block's coinbase once abandoned extends past the tip, is a
    # spend this rule accepts rather than one it refuses on its own.
    common = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, common)

    resurrectable = generate_random_transaction(common[0].transactions[0].id)
    abandoned = build_block(
        common[-1].header.hash,
        [generate_coinbase(height=len(common) + 1), resurrectable],
        len(common),
    )
    fork = [*common, abandoned]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(fork)

    heavier = [*common, *_extend(common[-1].header.hash, len(common), 2)]
    block_index.add_headers([block.header for block in heavier[1:]])
    for block in heavier[1:]:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(heavier)

    assert node.mempool.contains_tx(resurrectable)


def test_a_reorg_re_adds_abandoned_transactions_parent_first(
    node: Node,
) -> None:
    """A reorg re-adds an abandoned parent before the child that spends it."""
    # a chain of two transactions confirmed only on the branch being
    # abandoned: the second spends the first's own output, which exists
    # nowhere but the mempool once the reorg undoes both blocks, so it
    # has to find its parent already there. Processed tip-first --
    # to_remove's own order, kept for the utxo undo above it -- the
    # child is checked before the parent it depends on ever returns,
    # and verify_mempool_acceptance drops it as a missing prevout for
    # good; Core's own MaybeUpdateMempoolForReorg re-adds oldest first
    # for the same reason (src/validation.cpp). common is
    # COINBASE_MATURITY long for the same reason as the sibling test
    # above: parent spends its own first block's coinbase, and that has
    # to be old enough by the time older connects.
    common = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, common)

    parent = generate_random_transaction(common[0].transactions[0].id)
    older = build_block(
        common[-1].header.hash,
        [generate_coinbase(height=len(common) + 1), parent],
        len(common),
    )
    child = generate_random_transaction(parent.id)
    newer = build_block(
        older.header.hash,
        [generate_coinbase(height=len(common) + 2), child],
        len(common) + 1,
    )
    fork = [*common, older, newer]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(fork)

    heavier = [*common, *_extend(common[-1].header.hash, len(common), 3)]
    block_index.add_headers([block.header for block in heavier[1:]])
    for block in heavier[1:]:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(heavier)

    assert node.mempool.contains_tx(parent)
    assert node.mempool.contains_tx(child)


def test_a_reorg_before_the_node_is_synced_leaves_the_mempool_alone(
    node: Node,
) -> None:
    """A reorg while still syncing does not reconcile the mempool at all."""
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.status = NodeStatus.HeaderSynced

    second = generate_random_chain(3, RegTest().genesis.hash)
    block_index = connect(node, second)
    # the reorg happened, and left the mempool out of it
    assert block_index.active_chain[1:] == hashes(second)
    assert node.mempool.size == 0


def test_a_newly_connected_block_is_announced_to_every_connected_peer(
    node: Node,
) -> None:
    """A connected block reaches every peer, by header or inventory."""
    # only once the node is synced, the same gate the mempool bookkeeping
    # above already uses: an accepted block used to reach nobody, by
    # either shape. btclib-org/btclib-node#202
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


def test_a_reorg_before_the_node_is_synced_announces_nothing(node: Node) -> None:
    """A reorg while still syncing sends no connected peer anything."""
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.status = NodeStatus.HeaderSynced

    sent: list[Any] = []
    node.p2p_manager.connections[1] = cast(
        "Connection",
        SimpleNamespace(prefers_headers=True, send=sent.append),
    )

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)
    assert not sent


def test_a_refused_branch_invalidates_only_the_block_that_failed(
    node: Node,
) -> None:
    """A failing tip is marked invalid; blocks under it stay `valid_header`."""
    # the branch is tried as a unit: its tip is what get_first_candidate
    # offers, so the blocks under it connect in the same pass the tip is
    # refused in, and the utxo set and the filter index are rolled back.
    # Neither rollback reaches the block index; what does is
    # update_header_index, on the one block whose own contextual check
    # raised -- the ones under it never failed anything and stay
    # valid_header, ready to connect if a different tip is built on them.
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)

    # a coinbase paying more than its own subsidy, not a spend of
    # below's own tip: that coinbase is not yet COINBASE_MATURITY deep,
    # and a spend of it would be refused for prematurity before ever
    # reaching the block-index machinery this test is about
    below = generate_random_chain(2, RegTest().genesis.hash)
    bad_tip = build_block(
        below[-1].header.hash,
        [generate_coinbase(50 * 10**8 + 1, height=len(below) + 1)],
        len(below),
    )
    fork = [*below, bad_tip]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == bad_tip.header.hash

    update_chain(node)
    assert block_index.active_chain[1:] == hashes(active)
    for block in below:
        info = block_index.get_block_info(block.header.hash)
        assert info.status == BlockStatus.valid_header
    assert block_index.get_block_info(bad_tip.header.hash).status == BlockStatus.invalid
    # the doomed tip no longer weighs on what get_first_candidate offers
    assert block_index.get_first_candidate() is None

    node.chainstate.close()
    reopened = Chainstate(node.data_dir, RegTest(), node.logger)
    for block in below:
        info = reopened.block_index.get_block_info(block.header.hash)
        assert info.status == BlockStatus.valid_header
    assert (
        reopened.block_index.get_block_info(bad_tip.header.hash).status
        == BlockStatus.invalid
    )
    reopened.close()


def test_a_refused_branch_leaves_no_reverse_patches_in_the_block_store(
    node: Node,
) -> None:
    """A rolled-back trial leaves no reverse patch behind for any block."""
    # active outweighs below's own two blocks individually, so only
    # bad_tip -- the fork's tip -- is its own candidate and the
    # whole fork connects in one trial. below's two blocks validate and
    # each generate a reverse patch before bad_tip fails and the
    # trial is rolled back: btclib-org/btclib-node#200
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)

    # a coinbase paying more than its own subsidy, not a spend of
    # below's own tip: that coinbase is not yet COINBASE_MATURITY deep,
    # and a spend of it would be refused for prematurity before ever
    # reaching the block-index machinery this test is about
    below = generate_random_chain(2, RegTest().genesis.hash)
    bad_tip = build_block(
        below[-1].header.hash,
        [generate_coinbase(50 * 10**8 + 1, height=len(below) + 1)],
        len(below),
    )
    fork = [*below, bad_tip]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == bad_tip.header.hash

    update_chain(node)

    assert block_index.active_chain[1:] == hashes(active)
    for block in below:
        assert node.block_db.get_rev_block(block.header.hash) is None
        assert block.header.hash not in node.block_db.rev_patches
    assert node.block_db.pending_rev_blocks == {}


def test_a_refused_branch_invalidates_headers_that_were_never_candidates(
    node: Node,
) -> None:
    """Invalidation cascades to blocks that never outweighed active alone."""
    # neither the block that fails nor a sibling built on it has to have
    # individually outweighed the active chain to be real: only the
    # branch's own tip does, for update_chain to try connecting it at
    # all. Both are hidden from block_candidates and only reachable by
    # walking BlockIndex.children -- proves the cascade through the real
    # update_chain -> update_header_index -> invalidate call chain, not
    # just the isolated BlockIndex-level call: btclib-org/btclib-node#125
    active = generate_random_chain(6, RegTest().genesis.hash)
    block_index = connect(node, active)

    # a coinbase paying more than its own subsidy, not a spend of
    # below's own tip: that coinbase is not yet COINBASE_MATURITY deep,
    # and a spend of it would be refused for prematurity before ever
    # reaching the block-index machinery this test is about
    below = generate_random_chain(2, RegTest().genesis.hash)
    bad_tip = build_block(
        below[-1].header.hash,
        [generate_coinbase(50 * 10**8 + 1, height=len(below) + 1)],
        len(below),
    )
    # more, structurally fine, blocks on top of the doomed one -- their
    # combined chainwork is what makes the branch's tip outweigh active,
    # not bad_tip on its own. Built with an explicit, increasing
    # height rather than generate_random_chain's own (which restarts at
    # 0 for any start): a header's timestamp has to beat the median of
    # its ancestors, and build_block's is derived from the height alone
    continuation: list[Block] = []
    previous = bad_tip
    for height in range(len(below) + 1, len(below) + 5):
        previous = build_block(previous.header.hash, [generate_coinbase()], height)
        continuation.append(previous)
    fork = [*below, bad_tip, *continuation]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    # a sibling of the continuation, off bad_tip, real and indexed
    # but never downloaded and never its own block_candidates entry
    sibling = generate_random_header_chain(1, bad_tip.header.hash, bad_tip.header.time)
    block_index.add_headers(sibling)
    # the branch's own tip is the one candidate entry: everything below
    # it, bad_tip included, never individually outweighed active
    # on its own
    hidden = {bad_tip.header.hash, sibling[0].hash}
    hidden.update(block.header.hash for block in continuation[:-1])
    assert not hidden & {h for h, _ in block_index.block_candidates}

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == continuation[-1].header.hash

    update_chain(node)
    assert block_index.active_chain[1:] == hashes(active)
    for block_hash in {*hidden, continuation[-1].header.hash}:
        assert block_index.get_block_info(block_hash).status == BlockStatus.invalid


def test_a_stop_mid_reorg_rolls_the_trial_back_without_invalidating_it(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shutdown mid-trial rolls back cleanly, marking nothing invalid."""
    # `terminate_flag` is read between the blocks of `to_add`, so a
    # shutdown requested during a reorg is noticed after the block being
    # validated when it arrived rather than after the whole fork:
    # btclib-org/btclib-node#139. Nothing update_chain buffers along the
    # way reaches disk until every block of the fork has validated, so
    # the state this pins is not "stopped partway, with some of the fork
    # applied" -- there is no such state to reach -- but "stopped with
    # none of it applied, and the block it stopped on left alone", which
    # is what tells this apart from a block that failed its own check.
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
        transaction_data: list[tuple[list[Coin], Tx]],
        index: int,
        node: Node,
        block_hash: bytes,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            node.terminate_flag.set()
        return check_transactions(transaction_data, index, node, block_hash)

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


def stored_status(chainstate: Chainstate, block_hash: bytes) -> BlockStatus:
    """Read a block's own status off the store, not off `header_dict`.

    `KeyValueStore.get` answers `bytes | None`, and a caller of this
    helper already knows the record is there -- the point of every one
    below is that it either is or is not yet, never that it might not
    parse.
    """
    data = chainstate.db.get(b"blkinfo-" + block_hash)
    assert data is not None
    return BlockInfo.deserialize(data, check_validity=False).status


def test_the_utxo_cache_stays_staged_until_the_bound_then_flushes_all_three(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below `UtxoIndex`'s own bound nothing reaches disk; at it, all three do.

    `_FLUSH_BOUND` is lowered to 2 rather than exercised at its real
    size (btclib-org/btclib-node#586): each of these two blocks is a
    bare coinbase, staging exactly one entry, so the first leaves the
    bound unmet and the second reaches it -- the same shape a mainnet
    block reaches it in, only smaller.
    """
    monkeypatch.setattr(utxo_index_module, "_FLUSH_BOUND", 2)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    filter_index = node.chainstate.filter_index
    block_index.add_headers([block.header for block in chain])
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    first_out = OutPoint(chain[0].transactions[0].id, 0).serialize(check_validity=False)
    second_out = OutPoint(chain[1].transactions[0].id, 0).serialize(
        check_validity=False
    )

    update_chain(node)
    # one entry staged, one short of the bound: nothing below is on disk,
    # even though the in-memory chain already reflects the connection
    assert block_index.active_chain[1:] == [chain[0].header.hash]
    assert node.chainstate.db.get(b"utxo-" + first_out) is None
    assert (
        stored_status(node.chainstate, chain[0].header.hash) == BlockStatus.valid_header
    )
    assert node.chainstate.db.get(b"cfilter-" + chain[0].header.hash) is None
    # still answers correctly, staged rather than written
    assert filter_index.get_filter(chain[0].header.hash) is not None

    update_chain(node)
    # the second block's own entry reaches the bound, and the flush that
    # trips writes both blocks' status, both filters and both coins --
    # never only the block that happened to cross it
    assert node.chainstate.db.get(b"utxo-" + first_out) is not None
    assert node.chainstate.db.get(b"utxo-" + second_out) is not None
    for block in chain:
        assert (
            stored_status(node.chainstate, block.header.hash)
            == BlockStatus.in_active_chain
        )
        assert node.chainstate.db.get(b"cfilter-" + block.header.hash) is not None


def test_a_store_closed_without_a_flush_redoes_only_what_was_never_flushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unclean stop costs the blocks since the last flush, and nothing else.

    `Chainstate.close` is what flushes on a clean stop -- skipped here,
    on purpose, to stand in for a kill or a crash that never reaches it.
    The reopened store is not corrupted by that: it simply holds
    whatever the last actual flush wrote, `db.py`'s own docstring
    argues why, and driving it through `update_chain` again reaches the
    same chain a clean run would have, recomputed rather than read back.
    """
    monkeypatch.setattr(utxo_index_module, "_FLUSH_BOUND", 2)
    config = Config(
        chain="regtest", data_dir=tmp_path, allow_p2p=False, allow_rpc=False, debug=True
    )
    first = Node(config)
    first.status = NodeStatus.HeaderSynced

    chain = generate_random_chain(3, RegTest().genesis.hash)
    block_index = first.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block in chain:
        first.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    for _ in range(len(chain)):
        update_chain(first)
    # all three connected, in memory -- one flush happened along the way
    # (the second block reached the bound), the third block's own entry
    # left staged rather than written
    assert block_index.active_chain[1:] == [block.header.hash for block in chain]

    # an unclean stop: the connection closes with nothing flushed for
    # it, unlike Chainstate.close, which always flushes first. Every
    # other handle this node opened is still closed explicitly, the same
    # teardown tests/conftest.py's own unstarted_node_context uses,
    # since only the flush is what this test means to skip.
    first._close_worker_pool()
    first.p2p_manager.peer_db.close()
    first.chainstate.db.close()
    first.block_db.close()
    first.p2p_manager.loop.close()
    first.rpc_manager.loop.close()
    first.logger.close()

    reopened = Node(config)
    reopened.status = NodeStatus.HeaderSynced
    # the store opens without error, and reflects only the one flush
    # that actually happened: fewer than all three blocks are durable
    assert len(reopened.chainstate.block_index.active_chain) < len(chain) + 1

    for _ in range(len(chain)):
        update_chain(reopened)
    assert reopened.chainstate.block_index.active_chain[1:] == [
        block.header.hash for block in chain
    ]
    for block in chain:
        assert (
            reopened.chainstate.filter_index.get_filter(block.header.hash) is not None
        )

    reopened._close_worker_pool()
    reopened.p2p_manager.peer_db.close()
    reopened.chainstate.close()
    reopened.block_db.close()
    reopened.p2p_manager.loop.close()
    reopened.rpc_manager.loop.close()
    reopened.logger.close()


def test_a_reorg_restored_prevout_can_be_legitimately_spent_again(node: Node) -> None:
    """A reorg's own restore does not strand a later, heavier fork as invalid.

    `apply_rev_block` restoring a prevout that was durable when the
    block being undone spent it left the outpoint marked in
    `removed_utxos` even after putting it back into `updated_utxo_set` --
    staging now survives across trial boundaries (this is ISS 586's own
    point), so nothing erases that stale flag at the next trial's own
    boundary the way the old, per-trial `finalize` used to. A later,
    legitimate spend of the same output then hit `add_block`'s own
    "prevout already spent in this batch" guard, and
    `update_header_index` invalidated the block carrying it -- and
    everything built on it -- for good, stranding the node on a fork it
    could never leave (btclib-org/btclib-node#586). common is
    COINBASE_MATURITY long so that common[0]'s own coinbase, `funding`,
    is mature and spendable the moment a fork extends it.

    Three forks off `common`'s own tip, each heavier than the last:
    `fork_a` spends `funding` and connects; `fork_b`, two blocks not
    touching `funding`, outweighs it and reorgs it away, restoring
    `funding` -- staged, unflushed; `fork_c`, three blocks whose first
    legitimately re-spends the now-restored `funding`, outweighs
    `fork_b` in turn and has to connect rather than being refused as a
    double spend and invalidated.
    """
    common = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, common)
    funding = common[0].transactions[0]

    fork_a = [
        *common,
        build_block(
            common[-1].header.hash,
            [
                generate_coinbase(height=len(common) + 1),
                generate_random_transaction(funding.id),
            ],
            len(common),
        ),
    ]
    block_index.add_headers([block.header for block in fork_a])
    for block in fork_a:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(fork_a)

    fork_b = [*common, *_extend(common[-1].header.hash, len(common), 2)]
    block_index.add_headers([block.header for block in fork_b])
    for block in fork_b:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(fork_b)

    fork_c_first = build_block(
        common[-1].header.hash,
        [
            generate_coinbase(height=len(common) + 1),
            generate_random_transaction(funding.id),
        ],
        len(common),
    )
    fork_c = [
        *common,
        fork_c_first,
        *_extend(fork_c_first.header.hash, len(common) + 1, 2),
    ]
    block_index.add_headers([block.header for block in fork_c])
    for block in fork_c:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)

    assert block_index.active_chain[1:] == hashes(fork_c)
    assert (
        block_index.get_block_info(fork_c_first.header.hash).status
        != BlockStatus.invalid
    )


def an_ibd_node(
    *,
    chainwork: int,
    tip_time: datetime,
    minimum_chain_work: int = 0,
    is_initial_block_download: bool = True,
) -> Node:
    """Build a node carrying just what `main.update_ibd_status` reads."""
    tip_hash = b"\x11" * 32
    return cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(
                consensus=SimpleNamespace(minimum_chain_work=minimum_chain_work)
            ),
            chainstate=SimpleNamespace(
                block_index=SimpleNamespace(
                    active_chain=[tip_hash],
                    chainwork={tip_hash: chainwork},
                    header_dict={
                        tip_hash: SimpleNamespace(header=SimpleNamespace(time=tip_time))
                    },
                )
            ),
            is_initial_block_download=is_initial_block_download,
        ),
    )


def test_update_ibd_status_stays_true_below_the_chain_s_minimum_work() -> None:
    """Below `Chain.consensus.minimum_chain_work`, the tip's age is unread."""
    node = an_ibd_node(chainwork=5, minimum_chain_work=10, tip_time=datetime.now(UTC))
    main.update_ibd_status(node)
    assert node.is_initial_block_download is True


def test_update_ibd_status_stays_true_past_max_tip_age() -> None:
    """Enough work, but a tip older than `MAX_TIP_AGE`: still in IBD."""
    node = an_ibd_node(
        chainwork=10,
        minimum_chain_work=10,
        tip_time=datetime.now(UTC) - MAX_TIP_AGE - timedelta(seconds=1),
    )
    main.update_ibd_status(node)
    assert node.is_initial_block_download is True


def test_update_ibd_status_latches_off_and_never_back_as_the_tip_ages() -> None:
    """Both conditions met flips the latch; a stale re-check cannot undo it."""
    node = an_ibd_node(chainwork=10, minimum_chain_work=10, tip_time=datetime.now(UTC))
    main.update_ibd_status(node)
    assert node.is_initial_block_download is False

    # the tip itself never moves in this test; only time passes, past
    # what MAX_TIP_AGE would tolerate on a fresh check -- and
    # update_ibd_status's own first line returns before it reads the
    # tip a second time once the flag is already False
    stale_header = node.chainstate.block_index.header_dict[b"\x11" * 32].header
    stale_header.time = datetime.now(UTC) - MAX_TIP_AGE - timedelta(days=1)
    main.update_ibd_status(node)
    assert node.is_initial_block_download is False


def test_an_unpruned_node_never_calls_prune_up_to(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_prune_chain` is a no-op unless `Config.pruned` is set."""
    node = regtest_node(pruned=False)
    calls: list[int] = []
    monkeypatch.setattr(
        node.block_db, "prune_up_to", lambda target, _hash: calls.append(target)
    )
    connect(
        node, generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    )
    assert calls == []


def test_a_pruned_node_with_no_mib_target_never_calls_prune_up_to(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Config.prune_target_mib` unset is Core's own manual pruning, `-prune=1`.

    Nothing is deleted on its own here; only `rpc.callbacks.prune_blockchain`
    does, matching `node::ApplyArgsManOptions`'s own
    `PRUNE_TARGET_MANUAL` (`node/blockmanager_args.cpp:28-29`, at
    bitcoin/bitcoin@ca7162cde5): `FindFilesToPrune` is never called for a
    manually-pruned chainstate, only `FindFilesToPruneManual` is, and
    only from the RPC.
    """
    node = regtest_node(pruned=True, prune_target_mib=None)
    calls: list[int] = []
    monkeypatch.setattr(
        node.block_db, "prune_up_to", lambda target, _hash: calls.append(target)
    )
    connect(
        node, generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    )
    assert calls == []


def _always_over_target(node: Node, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force `_prune_to_target`'s stop condition to never fire.

    `node.block_db.current_usage` answering a constant far above any
    `prune_target_mib` in this file keeps `_prune_to_target`'s own
    while-loop running for every height `MIN_BLOCKS_TO_KEEP` allows --
    the real chains these tests build are a few kilobytes, orders of
    magnitude under even the smallest MiB target, so this is what
    stands in for a node whose actual disk usage stays over target
    throughout, the case `test_a_pruned_node_stops_once_under_target`
    below is the other side of.
    """
    monkeypatch.setattr(node.block_db, "current_usage", lambda: 2**40)


def test_a_pruned_node_over_target_deletes_past_the_retained_depth_never_further(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pruned node over target keeps only the last `MIN_BLOCKS_TO_KEEP`.

    Core's own retained depth (`constants.MIN_BLOCKS_TO_KEEP`'s own
    citation): a fork replacing the tip's own last `MIN_BLOCKS_TO_KEEP`
    blocks still finds what it needs, which is exactly what an ordinary
    connect through `update_chain` -- one block at a time, this test's
    own shape -- never has reason to reach further back than.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    _always_over_target(node, monkeypatch)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)
    tip_height = len(block_index.active_chain) - 1

    assert node.block_db.pruned_up_to == tip_height - MIN_BLOCKS_TO_KEEP
    oldest_kept = tip_height - MIN_BLOCKS_TO_KEEP + 1
    assert node.block_db.get_block(block_index.active_chain[oldest_kept - 1]) is None
    assert node.block_db.get_block(block_index.active_chain[oldest_kept]) is not None
    assert node.block_db.get_block(block_index.active_chain[-1]) is not None


def test_a_pruned_node_under_target_deletes_nothing(
    regtest_node: Callable[..., Node],
) -> None:
    """A pruned node under its own MiB target the whole time deletes nothing.

    No monkeypatch here: these chains are a few kilobytes, and
    `MIN_PRUNE_TARGET_MIB` (550) is never crossed by them, so this is
    `_prune_to_target`'s own real `block_db.current_usage` read, not a
    forced one.
    """
    node = regtest_node(pruned=True, prune_target_mib=MIN_PRUNE_TARGET_MIB)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)

    assert node.block_db.pruned_up_to == -1
    assert node.block_db.get_block(block_index.active_chain[1]) is not None


def test_a_pruned_node_stops_once_under_target(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_prune_to_target` stops as soon as usage drops under the target.

    Core's own `FindFilesToPrune` breaks out of its own file loop "are
    we below our target?" (`node/blockstorage.cpp:373-375`, at
    bitcoin/bitcoin@ca7162cde5) rather than reaching every file the
    retained-depth bound would otherwise allow; matched here by a
    `current_usage` that answers over target for the first three calls
    and under it from the fourth call on, so pruning reaches height 3
    and no further, well short of the `MIN_BLOCKS_TO_KEEP` floor this
    chain's own length would otherwise allow.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    usages = iter([2**40, 2**40, 2**40, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    monkeypatch.setattr(node.block_db, "current_usage", lambda: next(usages))
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)

    assert node.block_db.pruned_up_to == 3
    assert node.block_db.get_block(block_index.active_chain[3]) is None
    assert node.block_db.get_block(block_index.active_chain[4]) is not None


def test_a_pruned_node_still_prunes_when_usage_exactly_equals_the_target(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Usage exactly at the target still prunes, matching Core's own `>=`.

    Core's own check is `nCurrentUsage + nBuffer >= target`
    (`node/blockstorage.cpp:373`, at bitcoin/bitcoin@ca7162cde5) --
    inclusive, not `>`, so a node sitting precisely on its own target
    still prunes one more height rather than stopping short of it.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    target_bytes = 1 * 1024 * 1024
    monkeypatch.setattr(node.block_db, "current_usage", lambda: target_bytes)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    connect(node, chain)

    assert node.block_db.pruned_up_to >= 0


def test_a_pruned_node_over_target_leaves_headers_and_the_active_chain_untouched(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning never drops a header, a status, or the active chain itself.

    `BlockIndex` keeps every header this node has ever seen regardless
    of `Config.pruned`, matching Core's own block index -- pruning
    unlinks the file on disk (`node/blockstorage.cpp`'s
    `UnlinkPrunedFiles`, at bitcoin/bitcoin@ca7162cde5), it does not
    drop the `CBlockIndex` entry itself. What that entry's own
    `BLOCK_HAVE_DATA`/`BLOCK_HAVE_UNDO` do on prune is the next test.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    _always_over_target(node, monkeypatch)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)

    assert len(block_index.active_chain) == len(chain) + 1
    for block_hash in block_index.active_chain:
        assert block_index.get_block_info(block_hash) is not None


def test_a_pruned_node_over_target_clears_downloaded_for_every_block_it_deletes(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning clears `BlockInfo.downloaded`, matching Core's own index entry.

    Core's own `BlockManager::PruneOneBlockFile`
    (`node/blockstorage.cpp:270-286`, at bitcoin/bitcoin@ca7162cde5)
    clears `BLOCK_HAVE_DATA`/`BLOCK_HAVE_UNDO` on the `CBlockIndex`
    entry it prunes -- "any block we prune would have to be downloaded
    again in order to consider its chain" -- matched here by
    `BlockInfo.downloaded`, which `prune_up_to_height` clears for the
    same range `block_db.prune_up_to` deletes. Genesis (height 0) is the
    one exception: it is seeded `downloaded=True` and never written to
    `block_db` in the first place, so it stays `True` regardless of how
    deep pruning otherwise reaches.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    _always_over_target(node, monkeypatch)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)
    tip_height = len(block_index.active_chain) - 1
    oldest_kept = tip_height - MIN_BLOCKS_TO_KEEP + 1

    assert block_index.get_block_info(block_index.active_chain[0]).downloaded is True
    for height in range(1, oldest_kept):
        block_hash = block_index.active_chain[height]
        assert block_index.get_block_info(block_hash).downloaded is False
    for height in range(oldest_kept, tip_height + 1):
        block_hash = block_index.active_chain[height]
        assert block_index.get_block_info(block_hash).downloaded is True


def test_a_fork_longer_than_the_retained_depth_prunes_correctly_on_disk(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork connected in one `update_chain` call still persists a clear.

    `_finalize_fork`'s own `to_add` loop is not bounded by
    `MIN_BLOCKS_TO_KEEP` anywhere, and `connect` below marks every
    header downloaded before its first `update_chain` call, so the
    whole `MIN_BLOCKS_TO_KEEP + 5`-block chain connects as one trial,
    one `_finalize_fork_and_prune` call -- `chainstate.flush()` never
    fires mid-trial on a chain this size, `should_flush`'s own
    `_FLUSH_BOUND` being far past what a handful of coinbase-only blocks
    touch, so every hash `prune_up_to_height` clears is still one
    `stage_status` staged into `pending`, unflushed, in the very same
    call. `get_block_info` reads `header_dict`, updated the same way
    whether `set_downloaded` writes through or folds into `pending`, so
    it cannot tell the two apart -- only what actually reaches the store
    can, which is why this closes and reopens `Chainstate` rather than
    reading `block_index` again.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    _always_over_target(node, monkeypatch)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)
    tip_height = len(block_index.active_chain) - 1
    oldest_kept = tip_height - MIN_BLOCKS_TO_KEEP + 1
    pruned_hashes = block_index.active_chain[1:oldest_kept]
    kept_hashes = block_index.active_chain[oldest_kept:]
    assert pruned_hashes

    node.chainstate.close()
    reopened = Chainstate(node.data_dir, RegTest(), node.logger)
    for block_hash in pruned_hashes:
        assert reopened.block_index.get_block_info(block_hash).downloaded is False
    for block_hash in kept_hashes:
        assert reopened.block_index.get_block_info(block_hash).downloaded is True
    reopened.close()
