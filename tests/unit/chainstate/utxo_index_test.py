# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Unit tests for `btclib_node.chainstate.utxo_index`.

Covers `UtxoIndex.add_block` staging spends and creations, its refusals
on a double spend and a missing prevout, `apply_rev_block` undoing a
block either still staged or already written, and persistence across a
restart.
"""

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node.block_db import RevBlock
from btclib_node.chains import RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.exceptions import ChainstateInconsistencyError, InvalidBlockInputError
from btclib_node.log import Logger
from tests import generate_random_chain

if TYPE_CHECKING:
    from pathlib import Path


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
    rather than the database, and apply_rev_block pops it from there
    without touching `removed_utxos` at all.
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


def test_add_block_skips_bip30_when_asked_to(tmp_path: Path) -> None:
    """`check_bip30=False` is what `Chain.bip30_exceptions`' two blocks use.

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
