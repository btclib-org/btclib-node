# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

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
from tests.helpers import generate_random_chain

if TYPE_CHECKING:
    from pathlib import Path


def test_long_init(tmp_path: Path) -> None:
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    chain = generate_random_chain(20000, RegTest().genesis.hash)
    for block in chain:
        utxo_index.add_block(block)
    utxo_index.finalize()
    utxo_dict = dict(utxo_index.db)
    chainstate.close()
    new_chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    new_utxo_index = new_chainstate.utxo_index
    new_utxo_dict = dict(new_utxo_index.db)
    new_chainstate.close()
    assert utxo_dict == new_utxo_dict


def test_rev_patch(tmp_path: Path) -> None:
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    chain = generate_random_chain(20000, RegTest().genesis.hash)
    rev_patches = []
    for block in chain:
        _, rev_patch = utxo_index.add_block(block)
        rev_patches.append(rev_patch)
    rev_patches.reverse()
    for rev_patch in rev_patches:
        utxo_index.apply_rev_block(rev_patch)
    assert utxo_index.updated_utxo_set == {}
    chainstate.close()


def one_tx_block(txs: list[Tx], block_hash: bytes = b"\x00" * 32) -> Any:
    """The shape UtxoIndex.add_block reads: a header hash, and txs."""
    return SimpleNamespace(header=SimpleNamespace(hash=block_hash), transactions=txs)


def coinbase(tag: bytes) -> Tx:
    return Tx(
        version=1,
        lock_time=0,
        # a coinbase script is two to a hundred octets
        vin=[TxIn(prev_out=OutPoint(), script_sig=tag * 8, sequence=0xFFFFFFFF)],
        vout=[TxOut(value=50 * 10**8, script_pub_key=script.serialize([tag]))],
    )


def spending(prev_out: OutPoint, tag: bytes) -> Tx:
    return Tx(
        version=1,
        lock_time=0,
        vin=[TxIn(prev_out=prev_out, script_sig=tag, sequence=0xFFFFFFFF)],
        vout=[TxOut(value=49 * 10**8, script_pub_key=script.serialize([tag]))],
    )


def test_spending_an_output_the_batch_already_spent_is_refused(tmp_path: Path) -> None:
    # `removed_utxos` holds what has been taken from the database but
    # not yet written back, so a second spend of the same outpoint
    # inside one batch is a double spend the database cannot yet see.
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index

    funding = coinbase(b"\x01")
    utxo_index.add_block(one_tx_block([funding], b"\x01" * 32))
    utxo_index.finalize()

    out = OutPoint(funding.id, 0)
    utxo_index.add_block(
        one_tx_block([coinbase(b"\x02"), spending(out, b"\x02")], b"\x02" * 32)
    )
    with pytest.raises(InvalidBlockInputError, match="already spent"):
        utxo_index.add_block(
            one_tx_block([coinbase(b"\x03"), spending(out, b"\x03")], b"\x03" * 32)
        )
    chainstate.close()


def test_spending_an_output_nobody_has_is_refused(tmp_path: Path) -> None:
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    nowhere = OutPoint(b"\x11" * 32, 0)
    with pytest.raises(InvalidBlockInputError, match="not found"):
        utxo_index.add_block(
            one_tx_block([coinbase(b"\x04"), spending(nowhere, b"\x04")])
        )
    chainstate.close()


def test_a_rev_block_that_removes_what_is_not_there_is_refused(tmp_path: Path) -> None:
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
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x05")
    utxo_index.add_block(one_tx_block([funding], b"\x05" * 32))
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
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x06")
    utxo_index.add_block(one_tx_block([funding], b"\x06" * 32))
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
    # the outpoint is in `removed_utxos`: the batch has taken it from
    # the database and not written back, so removing it again is the
    # same double spend from the other direction.
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    utxo_index = chainstate.utxo_index
    funding = coinbase(b"\x07")
    utxo_index.add_block(one_tx_block([funding], b"\x07" * 32))
    utxo_index.finalize()

    out = OutPoint(funding.id, 0)
    utxo_index.add_block(
        one_tx_block([coinbase(b"\x08"), spending(out, b"\x08")], b"\x08" * 32)
    )
    assert out.serialize(check_validity=False) in utxo_index.removed_utxos
    with pytest.raises(ChainstateInconsistencyError, match="already removed"):
        utxo_index.apply_rev_block(
            RevBlock(hash=b"\x08" * 32, to_add=[], to_remove=[out])
        )
    chainstate.close()
