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
