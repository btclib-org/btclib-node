# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Unit tests for `btclib_node.chainstate.utxo_index`.

Covers `UtxoIndex.add_block` staging spends and creations, its refusals
on a double spend and a missing prevout, `apply_rev_block` undoing a
block either still staged or already written, and persistence across a
restart.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from btclib.block import Block
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node.block_db import Coin, RevBlock
from btclib_node.chains import RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.muhash import CoinStats
from btclib_node.exceptions import ChainstateInconsistencyError, InvalidBlockInputError
from btclib_node.log import Logger
from tests import generate_random_chain

# The two mainnet blocks `is_bip30_unspendable` names
# (`chainstate/muhash.py`'s own `_BIP30_UNSPENDABLE_ORIGINALS`), height
# and hash both -- neither reachable on any real chain this suite runs,
# so exercising the branches gated on them means building a block that
# merely carries the same pair rather than a real one.
_BIP30_ORIGINAL_HEIGHT = 91722
_BIP30_ORIGINAL_HASH = bytes.fromhex(
    "00000000000271a2dc26e7667f8419f2e15416dc6955e5a6c6cdf3f2574dd08e"
)

# Core v31.1.0's `gettxoutsetinfo` over the regtest chain these blocks
# make (`tests/_data/README.md` says how it was made)
_CORE_VECTOR = json.loads(
    (Path(__file__).parent / "_data" / "regtest_hash_serialized_3.json").read_text()
)


def test_long_init(tmp_path: Path) -> None:
    """A 20000-block UTXO set reloads from disk into the same key-value pairs.

    Every block is added and finalized, the store closed, and a second
    `Chainstate` opened on the same path reads back the identical
    `utxo-` records.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    chain = generate_random_chain(20000, RegTest().genesis.hash)
    for height, block in enumerate(chain, start=1):
        utxo_index.add_block(block, height)
    utxo_index.finalize()
    utxo_dict = dict(utxo_index.db)
    chainstate.close()
    new_chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    new_utxo_index = new_chainstate.utxo_index
    new_utxo_dict = dict(new_utxo_index.db)
    new_chainstate.close()
    assert utxo_dict == new_utxo_dict


def test_rev_patch(tmp_path: Path) -> None:
    """Undoing a 20000-block chain's own patches, in reverse, empties the set.

    Every block's `RevBlock` is applied back to front, the way a reorg
    would unwind them, and `updated_utxo_set` ends empty rather than
    holding anything still staged.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    chain = generate_random_chain(20000, RegTest().genesis.hash)
    rev_patches = []
    for height, block in enumerate(chain, start=1):
        _, rev_patch = utxo_index.add_block(block, height)
        rev_patches.append(rev_patch)
    rev_patches.reverse()
    for rev_patch in rev_patches:
        utxo_index.apply_rev_block(rev_patch)
    assert utxo_index.updated_utxo_set == {}
    chainstate.close()


def one_tx_block(txs: list[Tx], block_hash: bytes = b"\x00" * 32) -> Any:
    """Build the shape UtxoIndex.add_block reads: a header hash, and txs."""
    return SimpleNamespace(header=SimpleNamespace(hash=block_hash), transactions=txs)


def coinbase(tag: bytes) -> Tx:
    """Build a coinbase transaction paying 50 regtest coins to `tag`."""
    return Tx(
        version=1,
        lock_time=0,
        # a coinbase script is two to a hundred octets
        vin=[TxIn(prev_out=OutPoint(), script_sig=tag * 8, sequence=0xFFFFFFFF)],
        vout=[TxOut(value=50 * 10**8, script_pub_key=script.serialize([tag]))],
    )


def spending(prev_out: OutPoint, tag: bytes) -> Tx:
    """Build a transaction spending `prev_out`, paying 49 coins to `tag`."""
    return Tx(
        version=1,
        lock_time=0,
        vin=[TxIn(prev_out=prev_out, script_sig=tag, sequence=0xFFFFFFFF)],
        vout=[TxOut(value=49 * 10**8, script_pub_key=script.serialize([tag]))],
    )


def test_spending_an_output_the_batch_already_spent_is_refused(tmp_path: Path) -> None:
    """A second block spending what the same batch already spent is refused.

    `removed_utxos` holds what has been taken from the database but
    not yet written back, so a second spend of the same outpoint
    inside one batch is a double spend the database cannot yet see.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index

    funding = coinbase(b"\x01")
    utxo_index.add_block(one_tx_block([funding], b"\x01" * 32), 1)
    utxo_index.finalize()

    out = OutPoint(funding.id, 0)
    utxo_index.add_block(
        one_tx_block([coinbase(b"\x02"), spending(out, b"\x02")], b"\x02" * 32), 2
    )
    with pytest.raises(InvalidBlockInputError, match="already spent"):
        utxo_index.add_block(
            one_tx_block([coinbase(b"\x03"), spending(out, b"\x03")], b"\x03" * 32), 3
        )
    chainstate.close()


def test_spending_an_output_nobody_has_is_refused(tmp_path: Path) -> None:
    """A block spending an outpoint that was never created is refused.

    Neither `updated_utxo_set` nor the database holds it, so
    add_block raises rather than crediting a spend of nothing.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    nowhere = OutPoint(b"\x11" * 32, 0)
    with pytest.raises(InvalidBlockInputError, match="not found"):
        utxo_index.add_block(
            one_tx_block([coinbase(b"\x04"), spending(nowhere, b"\x04")]), 1
        )
    chainstate.close()


def test_a_rev_block_that_removes_what_is_not_there_is_refused(tmp_path: Path) -> None:
    """apply_rev_block raises when asked to remove an outpoint nothing holds.

    Neither `updated_utxo_set` nor the database has it, so undoing a
    creation that never happened is a `ChainstateInconsistencyError`
    rather than a silent no-op.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    missing = OutPoint(b"\x11" * 32, 0)
    with pytest.raises(ChainstateInconsistencyError, match="not found"):
        utxo_index.apply_rev_block(
            RevBlock(hash=b"\x00" * 32, to_add=[], to_remove=[missing])
        )
    chainstate.close()


def test_a_rev_block_that_removes_a_pending_output_takes_it_back(
    tmp_path: Path,
) -> None:
    """Undoing a still-staged creation simply drops it from updated_utxo_set.

    The output was never finalized, so it is in `updated_utxo_set`
    rather than the database, and apply_rev_block pops it from there.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x05")
    utxo_index.add_block(one_tx_block([funding], b"\x05" * 32), 1)
    # not finalized: the output is in updated_utxo_set, not the database
    added = OutPoint(funding.id, 0)
    key = added.serialize(check_validity=False)
    assert key in utxo_index.updated_utxo_set
    utxo_index.apply_rev_block(
        RevBlock(hash=b"\x05" * 32, to_add=[], to_remove=[added])
    )
    assert key not in utxo_index.updated_utxo_set
    chainstate.close()


def _spent_after_a_flush(tmp_path: Path) -> tuple[Any, bytes, RevBlock, Any]:
    """Stage a spend of a coin the store holds, and undo it.

    Block 1 creates the coin and is flushed, block 2 spends it, and
    undoing block 2 stages the coin again while the store keeps its record.
    Return the index, the coin's key, block 2's undo and block 2.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x08")
    utxo_index.add_block(one_tx_block([funding], b"\x08" * 32), 1)
    utxo_index.finalize()
    out_point = OutPoint(funding.id, 0)
    block2 = one_tx_block(
        [coinbase(b"\x09"), spending(out_point, b"\x0a")], b"\x09" * 32
    )
    _, rev2 = utxo_index.add_block(block2, 2)
    utxo_index.apply_rev_block(rev2)
    return chainstate, out_point.serialize(check_validity=False), rev2, block2


def test_a_restored_output_that_the_store_holds_is_removed_from_it(
    tmp_path: Path,
) -> None:
    """Spend, flush, undo the spend, undo the creation: nothing is left.

    The flush writes the creation, the spend stages its deletion, and
    undoing the spend puts the coin back into `updated_utxo_set` while
    the store still holds it. Undoing the creation must delete it from
    the store as well, which `finalize` does for `removed_utxos` only.
    """
    chainstate, key, _, _ = _spent_after_a_flush(tmp_path)
    utxo_index = chainstate.utxo_index
    utxo_index.apply_rev_block(
        RevBlock(hash=b"\x08" * 32, to_add=[], to_remove=[OutPoint.parse(key)])
    )
    utxo_index.finalize()
    assert chainstate.db.get(b"utxo-" + key) is None
    chainstate.close()


def test_a_restored_output_that_is_spent_again_is_removed_from_the_store(
    tmp_path: Path,
) -> None:
    """Spend, flush, undo the spend, spend again: the record is deleted.

    The coin is staged and also stored, so the second spend takes it from
    `updated_utxo_set` and has to mark it removed all the same.
    """
    chainstate, key, _, block2 = _spent_after_a_flush(tmp_path)
    utxo_index = chainstate.utxo_index
    utxo_index.add_block(block2, 2)
    utxo_index.finalize()
    assert chainstate.db.get(b"utxo-" + key) is None
    chainstate.close()


def test_a_restore_is_rolled_back_with_the_rest(tmp_path: Path) -> None:
    """A trial that restored a stored coin leaves no mark of it behind."""
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x0b")
    utxo_index.add_block(one_tx_block([funding], b"\x0b" * 32), 1)
    utxo_index.finalize()
    out_point = OutPoint(funding.id, 0)
    block2 = one_tx_block([coinbase(b"\x0c"), spending(out_point, b"\x0d")])
    _, rev2 = utxo_index.add_block(block2, 2)
    mark = utxo_index.trial_mark()
    utxo_index.apply_rev_block(rev2)
    assert utxo_index._stored_utxos
    utxo_index.rollback(mark)
    assert not utxo_index._stored_utxos
    chainstate.close()


def test_an_output_created_and_spent_in_one_batch_never_reaches_the_store(
    tmp_path: Path,
) -> None:
    """A coin that was only ever staged is not marked removed when spent."""
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x0e")
    utxo_index.add_block(one_tx_block([funding], b"\x0e" * 32), 1)
    tx1 = spending(OutPoint(funding.id, 0), b"\x0f")
    chained = OutPoint(tx1.id, 0)
    tx2 = spending(chained, b"\x10")
    utxo_index.add_block(one_tx_block([coinbase(b"\x11"), tx1, tx2]), 2)
    key = chained.serialize(check_validity=False)
    assert key not in utxo_index.removed_utxos
    utxo_index.finalize()
    assert chainstate.db.get(b"utxo-" + key) is None
    chainstate.close()


def test_a_rev_block_that_removes_a_written_output_marks_it_removed(
    tmp_path: Path,
) -> None:
    """Undoing an already-finalized creation stages it in removed_utxos.

    The output is on disk rather than in `updated_utxo_set`, so
    apply_rev_block cannot simply drop it and instead stages its
    deletion, for `finalize` to write.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x06")
    utxo_index.add_block(one_tx_block([funding], b"\x06" * 32), 1)
    utxo_index.finalize()
    added = OutPoint(funding.id, 0)
    key = added.serialize(check_validity=False)
    utxo_index.apply_rev_block(
        RevBlock(hash=b"\x06" * 32, to_add=[], to_remove=[added])
    )
    assert key in utxo_index.removed_utxos
    chainstate.close()


def test_a_rev_block_that_removes_what_the_batch_already_spent_is_refused(
    tmp_path: Path,
) -> None:
    """Undoing a spend the same batch already staged for removal is refused.

    The outpoint is in `removed_utxos`: the batch has taken it from
    the database and not written back, so removing it again is the
    same double spend from the other direction.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x07")
    utxo_index.add_block(one_tx_block([funding], b"\x07" * 32), 1)
    utxo_index.finalize()

    out = OutPoint(funding.id, 0)
    utxo_index.add_block(
        one_tx_block([coinbase(b"\x08"), spending(out, b"\x08")], b"\x08" * 32), 2
    )
    assert out.serialize(check_validity=False) in utxo_index.removed_utxos
    with pytest.raises(ChainstateInconsistencyError, match="already removed"):
        utxo_index.apply_rev_block(
            RevBlock(hash=b"\x08" * 32, to_add=[], to_remove=[out])
        )
    chainstate.close()


def test_a_rev_block_that_restores_a_written_output_unmarks_it_removed(
    tmp_path: Path,
) -> None:
    """Restoring a durable prevout clears it from removed_utxos too.

    The prevout was durable (on disk, not in `updated_utxo_set`) when
    the block being undone spent it, so `add_block` staged that spend
    with `_mark_removed`. `apply_rev_block`'s own `to_add` loop restores
    it into `updated_utxo_set`, and used to stop there -- leaving the
    same outpoint bytes in `removed_utxos` too, a stale flag nothing
    then erased, because staging now survives across trial boundaries
    (btclib-org/btclib-node#586) rather than being wiped by a per-trial
    `finalize` the way it used to be. A block later, legitimately
    re-spending the restored output must not be refused as a double
    spend by `add_block`'s own `removed_utxos` guard, which is what this
    pins directly, one level below the reorg that reaches it.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x12")
    utxo_index.add_block(one_tx_block([funding], b"\x12" * 32), 1)
    utxo_index.finalize()

    out = OutPoint(funding.id, 0)
    key = out.serialize(check_validity=False)
    _, rev_block = utxo_index.add_block(
        one_tx_block([coinbase(b"\x13"), spending(out, b"\x13")], b"\x13" * 32), 2
    )
    assert key in utxo_index.removed_utxos

    utxo_index.apply_rev_block(rev_block)
    assert key in utxo_index.updated_utxo_set
    assert key not in utxo_index.removed_utxos

    # a later, legitimate re-spend of the restored output is accepted
    # rather than refused as "prevout already spent in this batch"
    utxo_index.add_block(
        one_tx_block([coinbase(b"\x14"), spending(out, b"\x14")], b"\x14" * 32), 3
    )
    chainstate.close()


def test_a_restored_output_is_a_bip30_violation_again(tmp_path: Path) -> None:
    """A block re-mining a restored output's own txid is refused.

    `_unmark_removed` fixes two things, not one: alongside the
    double-spend-guard consequence
    `test_a_rev_block_that_restores_a_written_output_unmarks_it_removed`
    above pins, the same stale flag also hid a genuine BIP30 duplicate.
    `_bip30_violation` reads `removed_utxos` first and answers "no
    violation" on a hit, before ever consulting `updated_utxo_set` or
    the database -- so as long as the restored outpoint sat in
    `removed_utxos`, a block recreating it connected instead of being
    refused, even though `apply_rev_block`'s own `to_add` loop had just
    made that coin real and unspent again. Goes through `add_block`
    rather than calling `_bip30_violation` directly, the same level
    every other BIP30 test in this file exercises the check at, below
    -- and asserts only the raise, not `removed_utxos` itself, which
    `test_a_rev_block_that_restores_a_written_output_unmarks_it_removed`
    above already pins.

    It does not, however, catch a mutation dropping
    `apply_rev_block`'s own `_unmark_removed` call: the `finalize`
    below, which is what makes this test's path genuine, clears
    `removed_utxos` outright on its way out, so by the time
    `apply_rev_block` runs there is nothing left for that call to
    unmark and the test passes either way. The sibling test above,
    which has no intervening `finalize`, is what fails under that
    mutation. So this one pins the rule and that one pins the call
    (btclib-org/btclib-node#586).

    The spend is finalized before the rev block undoes it, so `out`'s
    own `utxo-` record is genuinely deleted from the store by the time
    the rev block restores it into `updated_utxo_set` rather than
    writing it straight back to disk. Without this finalize, `out`'s
    record from height 1 is still durable and never deleted, so
    `_bip30_violation`'s `self.db.get(...)` fallback alone would answer
    `True` for it regardless of `updated_utxo_set` -- the assertion
    below would still pass with `updated_utxo_set` never consulted at
    all, pinning nothing about the path this test is named for
    (btclib-org/btclib-node#586).
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x15")
    utxo_index.add_block(one_tx_block([funding], b"\x15" * 32), 1)
    utxo_index.finalize()

    out = OutPoint(funding.id, 0)
    key = out.serialize(check_validity=False)
    _, rev_block = utxo_index.add_block(
        one_tx_block([coinbase(b"\x16"), spending(out, b"\x16")], b"\x16" * 32), 2
    )
    utxo_index.finalize()
    assert utxo_index.db.get(b"utxo-" + key) is None
    utxo_index.apply_rev_block(rev_block)
    assert utxo_index.db.get(b"utxo-" + key) is None
    assert key in utxo_index.updated_utxo_set

    # the coinbase that originally created `out` duplicates a still-unspent
    # output, restored by the rev block just applied
    with pytest.raises(InvalidBlockInputError, match="bad-txns-BIP30"):
        utxo_index.add_block(one_tx_block([funding], b"\x17" * 32), 3)
    chainstate.close()


def test_add_blocks_own_creation_loops_keep_the_two_dicts_disjoint(
    tmp_path: Path,
) -> None:
    """A block recreating a still-staged spend's own txid stays disjoint.

    `_bip30_violation` reads `removed_utxos` before `updated_utxo_set`
    on the claim that no outpoint bytes value is ever staged in both at
    once. Fund an output and finalize it, spend it -- staged only, via
    `_mark_removed`, with no finalize or rev block in between -- then
    add a later block whose own transaction shares that spent output's
    exact txid. Before `add_block`'s own two creation loops called
    `_unmark_removed` themselves, the recreated outpoint's `_put`
    landed in `updated_utxo_set` while `removed_utxos` still carried it
    from the spend, and a later `apply_rev_block` undoing that
    recreation raised `ChainstateInconsistencyError("output already
    removed")` on a coin that was legitimately staged and unspent
    (btclib-org/btclib-node#586).
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x18")
    utxo_index.add_block(one_tx_block([funding], b"\x18" * 32), 1)
    utxo_index.finalize()

    out = OutPoint(funding.id, 0)
    key = out.serialize(check_validity=False)
    utxo_index.add_block(
        one_tx_block([coinbase(b"\x19"), spending(out, b"\x19")], b"\x19" * 32), 2
    )
    assert key in utxo_index.removed_utxos

    _, rev_block = utxo_index.add_block(one_tx_block([funding], b"\x1a" * 32), 3)
    assert not (key in utxo_index.removed_utxos and key in utxo_index.updated_utxo_set)

    # undoing the recreation does not trip "output already removed" on a
    # coin that is legitimately staged and unspent
    utxo_index.apply_rev_block(rev_block)
    chainstate.close()


def test_a_block_that_duplicates_an_unspent_output_is_refused(tmp_path: Path) -> None:
    """A block whose coinbase duplicates a still-unspent txid is refused.

    Core's `bad-txns-BIP30` (`ConnectBlock`, `src/validation.cpp:2401-2431`,
    at bitcoin/bitcoin@204256c73f), CVE-2012-1909's shape: without this
    check, the second block's own write silently overwrote the first's,
    and a reorg away from it deleted an output the first block's own
    branch still carries.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    duplicate = coinbase(b"\x09")
    utxo_index.add_block(one_tx_block([duplicate], b"\x09" * 32), 1)
    utxo_index.finalize()

    with pytest.raises(InvalidBlockInputError, match="bad-txns-BIP30"):
        utxo_index.add_block(one_tx_block([duplicate], b"\x0a" * 32), 2)
    chainstate.close()


def test_a_block_that_duplicates_a_spent_output_connects(tmp_path: Path) -> None:
    """Reusing a txid whose original output is already spent is no violation.

    BIP30 is about an outpoint still *unspent* -- Core's own `HaveCoin`
    check -- not about a txid ever having existed at all. The spend is
    left staged rather than finalized, so the duplicate coinbase's own
    check reads the outpoint out of `removed_utxos` rather than finding
    it simply absent from both `updated_utxo_set` and the database --
    the other way `_bip30_violation` answers "no".
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    duplicate = coinbase(b"\x0b")
    utxo_index.add_block(one_tx_block([duplicate], b"\x0b" * 32), 1)
    utxo_index.finalize()

    out = OutPoint(duplicate.id, 0)
    utxo_index.add_block(
        one_tx_block([coinbase(b"\x0c"), spending(out, b"\x0c")], b"\x0c" * 32), 2
    )
    assert out.serialize(check_validity=False) in utxo_index.removed_utxos

    # the original output is gone, so the same coinbase reappearing
    # duplicates nothing still on the chain
    utxo_index.add_block(one_tx_block([duplicate], b"\x0d" * 32), 3)
    chainstate.close()


def test_a_refused_duplicate_leaves_the_original_output_untouched(
    tmp_path: Path,
) -> None:
    """A refused duplicate leaves the original output untouched.

    The actual danger CVE-2012-1909 names is not the refusal by itself:
    it is a reorg away from a *connected* duplicate deleting an output
    still on the chain. Refusing before either loop below stages
    anything is what leaves no rev_block for such a reorg to ever apply.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    duplicate = coinbase(b"\x0e")
    utxo_index.add_block(one_tx_block([duplicate], b"\x0e" * 32), 1)
    utxo_index.finalize()

    with pytest.raises(InvalidBlockInputError, match="bad-txns-BIP30"):
        utxo_index.add_block(one_tx_block([duplicate], b"\x0f" * 32), 2)

    out = OutPoint(duplicate.id, 0)
    key = out.serialize(check_validity=False)
    assert utxo_index.db.get(b"utxo-" + key) is not None
    chainstate.close()


def test_apply_rev_block_undoes_an_in_block_chained_transaction(
    tmp_path: Path,
) -> None:
    """Undoing a block whose second transaction spends the first's output.

    An ordinary chained transaction -- one spending an output another
    transaction earlier in the *same* block created -- puts that
    output's outpoint in both `rev_block.to_remove` (created) and
    `rev_block.to_add` (spent before ever reaching disk). Its net effect
    on the persisted set is nothing, both before this block and after
    it, and undoing the block must leave it exactly that absent rather
    than raising on a block that did nothing wrong
    (btclib-org/btclib-node#634).
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index

    funding = coinbase(b"\x01")
    utxo_index.add_block(one_tx_block([funding], b"\x01" * 32), 1)
    utxo_index.finalize()
    out0 = OutPoint(funding.id, 0)

    tx1 = spending(out0, b"\x02")  # spends out0, creates Y
    chained_output = OutPoint(tx1.id, 0)
    tx2 = spending(chained_output, b"\x03")  # spends Y, in the same block
    coinbase2 = coinbase(b"\x02")
    _, rev_block2 = utxo_index.add_block(
        one_tx_block([coinbase2, tx1, tx2], b"\x02" * 32), 2
    )
    utxo_index.finalize()

    utxo_index.apply_rev_block(rev_block2)

    out0_key = out0.serialize(check_validity=False)
    chained_key = chained_output.serialize(check_validity=False)
    coinbase2_key = OutPoint(coinbase2.id, 0).serialize(check_validity=False)
    tx2_out_key = OutPoint(tx2.id, 0).serialize(check_validity=False)

    # out0 is spendable again, restored from the database into staging
    assert utxo_index.updated_utxo_set[out0_key].tx_out == funding.vout[0]
    # the chained output never touches either staging set: its creation
    # and its in-block spend cancel exactly the way add_block computed
    assert chained_key not in utxo_index.updated_utxo_set
    assert chained_key not in utxo_index.removed_utxos
    # block 2's own coinbase and tx2's output were finalized to disk and
    # are staged for deletion
    assert coinbase2_key in utxo_index.removed_utxos
    assert tx2_out_key in utxo_index.removed_utxos
    chainstate.close()


def test_add_block_skips_bip30_when_asked_to(tmp_path: Path) -> None:
    """`check_bip30=False` is what the two `bip30_exceptions` blocks use.

    A block reused rather than a real historical one: regtest carries
    no chain deep enough to name one of its own, and what this proves is
    the skip itself, not which specific block it is for.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    duplicate = coinbase(b"\x10")
    utxo_index.add_block(one_tx_block([duplicate], b"\x10" * 32), 1)
    utxo_index.finalize()

    utxo_index.add_block(one_tx_block([duplicate], b"\x11" * 32), 2, check_bip30=False)
    chainstate.close()


def test_get_coin_answers_none_for_something_removed_in_this_batch(
    tmp_path: Path,
) -> None:
    """`get_coin` reads `removed_utxos` too: a batch's own spend hides a coin.

    Without this check `get_coin` would fall through to the store, which
    still holds the coin until `finalize` deletes it, and answer a spend
    already staged as though it had never happened.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x09")
    utxo_index.add_block(one_tx_block([funding], b"\x09" * 32), 1)
    utxo_index.finalize()

    out = OutPoint(funding.id, 0)
    utxo_index.add_block(
        one_tx_block([coinbase(b"\x0a"), spending(out, b"\x0a")], b"\x0a" * 32), 2
    )
    assert utxo_index.get_coin(out.serialize(check_validity=False)) is None
    chainstate.close()


def test_rollback_restores_a_set_entry_a_second_mutation_had_overwritten(
    tmp_path: Path,
) -> None:
    """Undoing a mark that overwrote an already-present set entry keeps it.

    `_mark_removed` itself carries no guard against marking a key twice
    -- that guard lives in `add_block`/`apply_rev_block`'s own callers,
    which never call it for a key already in `removed_utxos` -- so
    nothing in ordinary use reaches this. `rollback`'s own undo log
    still has to answer it correctly rather than assume it never
    happens: this is that branch, tripped directly.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    key = b"\x0b" * 36
    utxo_index.removed_utxos.add(key)

    mark = utxo_index.trial_mark()
    utxo_index._mark_removed(key)
    utxo_index.rollback(mark)

    assert key in utxo_index.removed_utxos
    chainstate.close()


def test_coin_stats_returns_to_the_empty_digest_after_a_full_reorg(
    tmp_path: Path,
) -> None:
    """Applying every `RevBlock` back to front returns `coin_stats` to empty.

    The same shape `test_rev_patch` above already pins for the two
    staging dicts, extended to `coin_stats`: `MuHash3072.insert` and
    `.remove` are each other's exact inverse regardless of order
    (`chainstate/muhash.py`'s own docstring), so unwinding every block a
    chain ever staged, in reverse, has to land the accumulator back on
    the same digest an empty `CoinStats` starts with -- not merely a
    non-empty one, which a partially-correct undo could also produce.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    chain = generate_random_chain(200, RegTest().genesis.hash)
    rev_patches = []
    for height, block in enumerate(chain, start=1):
        _, rev_patch = utxo_index.add_block(block, height)
        rev_patches.append(rev_patch)
    assert utxo_index.coin_stats.digest != CoinStats().digest

    rev_patches.reverse()
    for rev_patch in rev_patches:
        utxo_index.apply_rev_block(rev_patch)

    assert utxo_index.coin_stats.digest == CoinStats().digest
    chainstate.close()


def test_coin_stats_persists_across_a_restart(tmp_path: Path) -> None:
    """`coin_stats` reloads to the same digest a fresh `Chainstate` reopens.

    `test_long_init` above pins the same round trip for the `utxo-`
    records themselves; `finalize`'s own docstring is where writing
    `coin_stats` into the same batch, rather than a second one, is
    argued.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    chain = generate_random_chain(50, RegTest().genesis.hash)
    for height, block in enumerate(chain, start=1):
        utxo_index.add_block(block, height)
    utxo_index.finalize()
    digest = utxo_index.coin_stats.digest
    chainstate.close()

    new_chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    new_utxo_index = new_chainstate.utxo_index
    assert new_utxo_index.coin_stats.digest == digest
    new_chainstate.close()


def test_add_block_skips_hashing_a_bip30_exempt_coinbase(tmp_path: Path) -> None:
    """A coinbase at one of history's two exempt height/hash pairs is unhashed.

    `add_block`'s own `skip_coinbase_hash` gate, matching
    `CoinStatsIndex::CustomAppend` -- the coinbase is still staged into
    `updated_utxo_set` (spendable, could be spent, undone by a reorg)
    but never reaches `coin_stats`, the same way Core's own index skips
    hashing it.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    exempt = coinbase(b"\x20")
    utxo_index.add_block(
        one_tx_block([exempt], _BIP30_ORIGINAL_HASH),
        _BIP30_ORIGINAL_HEIGHT,
        check_bip30=False,
    )
    assert utxo_index.coin_stats.digest == CoinStats().digest
    out = OutPoint(exempt.id, 0)
    assert utxo_index.get_coin(out.serialize(check_validity=False)) is not None
    chainstate.close()


def test_apply_rev_block_skips_bip30_exempt_coins_on_both_sides(
    tmp_path: Path,
) -> None:
    """`apply_rev_block`'s own `is_bip30_unspendable` gate, both directions.

    `to_add` restoring an exempt coin, and `to_remove` undoing the
    creation of one, both leave `coin_stats` untouched -- neither side
    ever put it there in the first place, `add_block`'s own gate on the
    same pair above being why.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    exempt = coinbase(b"\x21")
    _, rev_block = utxo_index.add_block(
        one_tx_block([exempt], _BIP30_ORIGINAL_HASH),
        _BIP30_ORIGINAL_HEIGHT,
        check_bip30=False,
    )
    assert utxo_index.coin_stats.digest == CoinStats().digest

    # to_remove: undoing the exempt coinbase's own creation
    utxo_index.apply_rev_block(rev_block)
    assert utxo_index.coin_stats.digest == CoinStats().digest

    # to_add: restoring the same exempt coin, the other side of the gate
    out = OutPoint(exempt.id, 0)
    restore = RevBlock(
        hash=_BIP30_ORIGINAL_HASH,
        to_add=[(out, Coin(exempt.vout[0], _BIP30_ORIGINAL_HEIGHT, is_coinbase=True))],
        to_remove=[],
    )
    utxo_index.apply_rev_block(restore)
    assert utxo_index.coin_stats.digest == CoinStats().digest
    chainstate.close()


def test_apply_rev_block_hashes_in_an_ordinary_output_of_the_exempt_block(
    tmp_path: Path,
) -> None:
    """`to_remove` must still un-hash a non-coinbase output of the exempt block.

    `is_bip30_unspendable` alone answers only for the coin's own height
    and the disconnecting block's hash -- never `coin.is_coinbase` --
    so gating `to_remove`'s own `_hash_remove` on it alone would
    wrongly withhold *every* non-coinbase output the exempt block
    itself creates, not only the exempt coinbase: `add_block` stamps
    every output of a connecting block, coinbase or not, with that same
    block's own height (`add_block`'s own docstring). The exempt block
    here carries the exempt coinbase **plus one ordinary transaction**,
    spending an already-finalized, unrelated funding output --
    `add_block` correctly hashes the ordinary output in (`hash_it=True`
    unconditionally for a non-coinbase creation), and only
    `coin.is_coinbase and is_bip30_unspendable(...)` together, matching
    `CoinStatsIndex::CustomAppend`'s own `is_coinbase &&
    IsBIP30Unspendable(...)` (`src/index/coinstatsindex.cpp:129`, at
    bitcoin/bitcoin@ca7162cde5), correctly un-hashes it again on undo.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x25")
    utxo_index.add_block(one_tx_block([funding], b"\x25" * 32), 1)
    utxo_index.finalize()
    before = utxo_index.coin_stats.digest

    exempt_cb = coinbase(b"\x26")
    ordinary = spending(OutPoint(funding.id, 0), b"\x27")
    _, rev_block = utxo_index.add_block(
        one_tx_block([exempt_cb, ordinary], _BIP30_ORIGINAL_HASH),
        _BIP30_ORIGINAL_HEIGHT,
        check_bip30=False,
    )
    assert utxo_index.coin_stats.digest != before

    utxo_index.apply_rev_block(rev_block)
    assert utxo_index.coin_stats.digest == before
    chainstate.close()


def test_apply_rev_block_hashes_in_a_non_coinbase_restore_via_to_add(
    tmp_path: Path,
) -> None:
    """`to_add` must still hash in a non-coinbase coin at the exempt height.

    The `to_remove` side above is what the reviewed reorg actually
    reaches; this isolates `to_add`'s own identical conjunction
    directly, the way the sibling coinbase test above isolates the
    coinbase case -- a synthetic `RevBlock` naming a non-coinbase coin
    at `_BIP30_ORIGINAL_HEIGHT`/`_BIP30_ORIGINAL_HASH`, the only
    combination `is_bip30_unspendable` alone cannot tell from the
    exempt coinbase itself.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    before = utxo_index.coin_stats.digest

    tx_out = TxOut(value=1000, script_pub_key=script.serialize(["OP_1"]))
    out = OutPoint(b"\x28" * 32, 0, check_validity=False)
    restore = RevBlock(
        hash=_BIP30_ORIGINAL_HASH,
        to_add=[(out, Coin(tx_out, _BIP30_ORIGINAL_HEIGHT, is_coinbase=False))],
        to_remove=[],
    )
    utxo_index.apply_rev_block(restore)
    assert utxo_index.coin_stats.digest != before
    chainstate.close()


def test_apply_rev_block_reads_a_flushed_coin_for_coin_stats_too(
    tmp_path: Path,
) -> None:
    """`to_remove`'s own `else` branch parses a durable coin off disk.

    Every earlier `apply_rev_block` test above undoes a still-staged
    creation; this one finalizes first, so `to_remove` finds nothing in
    `updated_utxo_set` and has to `Coin.parse` the stored record --
    the branch `coin_stats`'s own removal now also depends on, since
    the parsed `Coin` (its height and coinbase bit) is what
    `_hash_remove` needs.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x22")
    _, rev_block = utxo_index.add_block(one_tx_block([funding], b"\x22" * 32), 1)
    utxo_index.finalize()
    before = utxo_index.coin_stats.digest
    assert before != CoinStats().digest

    utxo_index.apply_rev_block(rev_block)
    assert utxo_index.coin_stats.digest == CoinStats().digest
    chainstate.close()


def test_a_corrupted_stored_coin_is_this_nodes_own_fault_in_apply_rev_block(
    tmp_path: Path,
) -> None:
    """`apply_rev_block`'s own `to_remove` raises on a corrupted `utxo-` record.

    The record corrupted here is one only this node's own earlier
    `finalize` ever wrote -- `rev_block` below never supplies these
    bytes, so the raise this proves is over storage this node owns,
    mirroring `main_test.py`'s own
    `test_a_corrupted_stored_coin_is_this_nodes_own_fault_not_the_tx_s`
    for this module's third `Coin.parse` call site
    (btclib-org/btclib-node#620, btclib-org/btclib-node#631,
    btclib-org/btclib-node#636).
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x23")
    _, rev_block = utxo_index.add_block(one_tx_block([funding], b"\x23" * 32), 1)
    utxo_index.finalize()

    out = OutPoint(funding.id, 0)
    key = b"utxo-" + out.serialize(check_validity=False)
    original = utxo_index.db.get(key)
    assert original is not None
    utxo_index.db.put(key, original[:1])

    with pytest.raises(ChainstateInconsistencyError, match="stored utxo- record"):
        utxo_index.apply_rev_block(rev_block)
    chainstate.close()


def test_mutating_coin_stats_removal_out_of_apply_rev_block_breaks_the_reorg(
    tmp_path: Path,
) -> None:
    """A reorg that forgets to unwind `coin_stats` is caught, not silently kept.

    Not a mutation test proper -- nothing here is patched -- but a
    direct pin of the property a dropped `_hash_remove`/`_hash_insert`
    call in `apply_rev_block` would break:
    `test_coin_stats_returns_to_the_empty_digest_after_a_full_reorg`
    above is the one this guards, restated at one block so the failure
    a skipped call would cause is legible without a 200-block diff.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x24")
    _, rev_block = utxo_index.add_block(one_tx_block([funding], b"\x24" * 32), 1)
    utxo_index.apply_rev_block(rev_block)
    assert utxo_index.coin_stats.digest == CoinStats().digest
    chainstate.close()


def unspendable_spend(prev_out: OutPoint, tag: bytes) -> Tx:
    """Build a transaction spending `prev_out` into one `OP_RETURN` output."""
    return Tx(
        version=1,
        lock_time=0,
        vin=[TxIn(prev_out=prev_out, script_sig=tag, sequence=0xFFFFFFFF)],
        vout=[TxOut(value=49 * 10**8, script_pub_key=script.serialize(["OP_RETURN"]))],
    )


def test_add_block_does_not_store_a_provably_unspendable_output(
    tmp_path: Path,
) -> None:
    """A provably unspendable output is staged nowhere `add_block` can reach.

    Matches `CCoinsViewCache::AddCoin` (`src/coins.cpp:82`, at
    bitcoin/bitcoin@ca7162cde5), which returns without adding such an
    output to Core's own UTXO set at all (btclib-org/btclib-node#667).
    Checked at every point `_stage_added`'s own gate touches: not in
    `updated_utxo_set`, not in the `RevBlock` a reorg would undo this
    block with, and not in the store once `finalize` writes everything
    still staged.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x40")
    utxo_index.add_block(one_tx_block([funding], b"\x40" * 32), 1)
    utxo_index.finalize()

    unspendable_tx = unspendable_spend(OutPoint(funding.id, 0), b"\x41")
    out = OutPoint(unspendable_tx.id, 0)
    key = out.serialize(check_validity=False)
    _, rev_block = utxo_index.add_block(
        one_tx_block([coinbase(b"\x42"), unspendable_tx], b"\x42" * 32), 2
    )
    assert key not in utxo_index.updated_utxo_set
    assert out not in rev_block.to_remove

    utxo_index.finalize()
    assert utxo_index.db.get(b"utxo-" + key) is None
    assert utxo_index.get_coin(key) is None
    chainstate.close()


def test_apply_rev_block_never_restores_an_output_it_never_stored(
    tmp_path: Path,
) -> None:
    """Undoing the block that "created" an unspendable output stores nothing.

    `to_remove` never names the outpoint in the first place (the test
    above pins that directly), so `apply_rev_block` completes without
    raising and without ever writing it into `updated_utxo_set` --
    `_stage_creation`'s own docstring is where "an output never stored
    is never restored" is argued.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x45")
    utxo_index.add_block(one_tx_block([funding], b"\x45" * 32), 1)
    utxo_index.finalize()

    unspendable_tx = unspendable_spend(OutPoint(funding.id, 0), b"\x46")
    out = OutPoint(unspendable_tx.id, 0)
    key = out.serialize(check_validity=False)
    _, rev_block = utxo_index.add_block(
        one_tx_block([coinbase(b"\x47"), unspendable_tx], b"\x47" * 32), 2
    )

    utxo_index.apply_rev_block(rev_block)
    assert key not in utxo_index.updated_utxo_set
    chainstate.close()


def test_serialized_hash_is_core_s_over_a_chain_with_a_wide_transaction(
    tmp_path: Path,
) -> None:
    """The stored set hashes to what Core's `gettxoutsetinfo` answered.

    One transaction of the chain has 301 outputs, two of them spent in
    the next block: the store sorts an index of 256 or more before one
    of 1, as little-endian bytes, where Core folds them in numerically.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    for height, block_hex in enumerate(_CORE_VECTOR["blocks"], start=1):
        block = Block.parse(bytes.fromhex(block_hex), check_validity=False)
        utxo_index.add_block(block, height)
    utxo_index.finalize()

    expected = _CORE_VECTOR["gettxoutsetinfo"]
    serialized_hash = utxo_index.serialized_hash(utxo_index.cursor())
    assert serialized_hash is not None
    assert serialized_hash[::-1].hex() == expected["hash_serialized_3"]
    assert utxo_index.coin_stats.digest[::-1].hex() == expected["muhash"]
    assert utxo_index.coin_stats.transaction_output_count == expected["txouts"]
    chainstate.close()


def test_serialized_hash_of_an_empty_set_is_the_double_hash_of_nothing(
    tmp_path: Path,
) -> None:
    """No coin is `HashWriter{}`'s `GetHash()`: SHA256d of no bytes."""
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    assert (
        chainstate.utxo_index.serialized_hash(chainstate.utxo_index.cursor())
        == hashlib.sha256(hashlib.sha256(b"").digest()).digest()
    )
    chainstate.close()


def test_serialized_hash_is_none_for_a_stored_coin_that_does_not_parse(
    tmp_path: Path,
) -> None:
    """A checksum-clean record `Coin.parse` cannot read leaves no hash.

    Core's cursor fails to read the value, and `ComputeUTXOStats`
    answers no stats (`src/kernel/coinstats.cpp`).
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x24")
    utxo_index.add_block(one_tx_block([funding], b"\x24" * 32), 1)
    utxo_index.finalize()
    key = b"utxo-" + OutPoint(funding.id, 0).serialize(check_validity=False)
    stored = utxo_index.db.get(key)
    assert stored is not None
    utxo_index.db.put(key, stored[:1])

    assert utxo_index.serialized_hash(utxo_index.cursor()) is None
    chainstate.close()


def test_serialized_hash_calls_the_interruption_point_per_coin(tmp_path: Path) -> None:
    """Core's `interruption_point` runs per coin and ends the walk."""
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    for height, tag in enumerate((b"\x31", b"\x32", b"\x33"), start=1):
        utxo_index.add_block(one_tx_block([coinbase(tag)], tag * 32), height)
    utxo_index.finalize()
    calls: list[int] = []

    def interrupt() -> None:
        calls.append(1)
        if len(calls) == 2:
            err_msg = "interrupted"
            raise RuntimeError(err_msg)

    with pytest.raises(RuntimeError, match="interrupted"):
        utxo_index.serialized_hash(utxo_index.cursor(), interrupt)
    assert len(calls) == 2
    chainstate.close()


def test_the_cursor_is_the_stored_set_as_it_was_opened(tmp_path: Path) -> None:
    """A block written after the cursor is opened is not in what it walks.

    Core's `CCoinsViewDB::Cursor` reads an implicit LevelDB snapshot, and
    this is the same property of a RocksDB iterator: the hash of the
    cursor does not move when the set does.
    """
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x34")
    utxo_index.add_block(one_tx_block([funding], b"\x34" * 32), 1)
    utxo_index.finalize()
    cursor = utxo_index.cursor()
    expected = utxo_index.serialized_hash(utxo_index.cursor())

    utxo_index.add_block(
        one_tx_block(
            [coinbase(b"\x35"), spending(OutPoint(funding.id, 0), b"\x35")],
            b"\x35" * 32,
        ),
        2,
    )
    utxo_index.finalize()

    assert utxo_index.serialized_hash(utxo_index.cursor()) != expected
    assert utxo_index.serialized_hash(cursor) == expected
    chainstate.close()
