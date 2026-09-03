# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Unit tests for `btclib_node.chainstate.muhash`.

The accumulator itself, `btclib.muhash.MuHash3072`, is btclib's own to
test -- `tests/muhash_test.py` there is where it is checked against
`crypto_tests.cpp`'s own vectors. What is tested here is what stayed:
the unspendable-output gate, `is_bip30_unspendable`'s own two mainnet
blocks, `tx_out_ser`'s packed field, and `CoinStats`, including the
on-disk round trip `UtxoIndex.finalize`/`__init__` rely on.
"""

import pytest
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx_out import TxOut

from btclib_node.block_db import Coin
from btclib_node.chainstate import muhash as muhash_module
from btclib_node.chainstate.muhash import (
    CoinStats,
    is_bip30_unspendable,
    is_unspendable,
    tx_out_ser,
)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", False),
        (b"\x51", False),  # OP_1, ordinary and spendable
        (b"\x6a", True),  # a bare OP_RETURN
        (b"\x6a\x04data", True),  # OP_RETURN with a data push
        (b"\x00" * 10_000, False),  # exactly MAX_SCRIPT_SIZE: not over it
        (b"\x00" * 10_001, True),  # one byte over MAX_SCRIPT_SIZE
    ],
    # explicit, short and by length rather than pytest's own default --
    # a `bytes` id escapes every byte, and PYTEST_CURRENT_TEST cannot be
    # set past 32767 characters on Windows (issue #701)
    ids=[
        "empty",
        "op_1",
        "bare_op_return",
        "op_return_with_data",
        "at_max_script_size",
        "over_max_script_size",
    ],
)
def test_is_unspendable_matches_core(data: bytes, *, expected: bool) -> None:
    """`CScript::IsUnspendable`'s own two conditions, each tried alone."""
    assert is_unspendable(data) is expected


def test_tx_out_ser_packs_height_and_coinbase_as_a_fixed_le_uint32() -> None:
    """`tx_out_ser` differs from `Coin.serialize` in the packed field alone."""
    tx_out = TxOut(value=5000, script_pub_key=script.serialize(["OP_1"]))
    out_point_bytes = OutPoint(b"\x11" * 32, 3, check_validity=False).serialize(
        check_validity=False
    )
    coin = Coin(tx_out, height=7, is_coinbase=True)

    packed = tx_out_ser(out_point_bytes, coin)

    packed_height_coinbase = (7 << 1) | 1
    expected = (
        out_point_bytes
        + packed_height_coinbase.to_bytes(4, "little")
        + tx_out.serialize(check_validity=False)
    )
    assert packed == expected
    # a Coin differing only in height serializes differently
    other = Coin(tx_out, height=8, is_coinbase=True)
    assert tx_out_ser(out_point_bytes, other) != packed
    # a Coin differing only in the coinbase bit serializes differently too
    non_coinbase = Coin(tx_out, height=7, is_coinbase=False)
    assert tx_out_ser(out_point_bytes, non_coinbase) != packed


@pytest.mark.parametrize(
    ("height", "block_hash_hex", "expected"),
    [
        (
            91722,
            "00000000000271a2dc26e7667f8419f2e15416dc6955e5a6c6cdf3f2574dd08e",
            True,
        ),
        (
            91812,
            "00000000000af0aed4792b1acee3d966af36cf5def14935db8de83d6f9306f2f",
            True,
        ),
        # the *other* BIP30 pair -- IsBIP30Repeat's own two blocks, which
        # btclib.consensus's own Chain.consensus.bip30_exceptions names
        # and this function does not
        (
            91842,
            "00000000000a4d0a398161ffc163c503763b1f4360639393e0e4c8e300e0caec",
            False,
        ),
        # right hash, wrong height
        (
            0,
            "00000000000271a2dc26e7667f8419f2e15416dc6955e5a6c6cdf3f2574dd08e",
            False,
        ),
        # right height, wrong hash
        (91722, "00" * 32, False),
    ],
)
def test_is_bip30_unspendable(
    height: int, block_hash_hex: str, *, expected: bool
) -> None:
    """Only the two blocks `IsBIP30Unspendable` names answer `True`."""
    assert is_bip30_unspendable(height, bytes.fromhex(block_hash_hex)) is expected


def _a_coin(value: int = 1000, *, unspendable: bool = False) -> tuple[bytes, Coin]:
    script_pub_key = (
        script.serialize(["OP_RETURN"]) if unspendable else script.serialize(["OP_1"])
    )
    tx_out = TxOut(value=value, script_pub_key=script_pub_key)
    out_point_bytes = OutPoint(b"\x22" * 32, 0, check_validity=False).serialize(
        check_validity=False
    )
    return out_point_bytes, Coin(tx_out, height=1, is_coinbase=False)


def test_coin_stats_insert_updates_all_three_counters() -> None:
    """A spendable coin moves the muhash, the count, the amount and the size."""
    stats = CoinStats()
    out_point_bytes, coin = _a_coin(value=1234)
    assert stats.insert(out_point_bytes, coin) is True
    assert stats.transaction_output_count == 1
    assert stats.total_amount == 1234
    assert stats.bogo_size == 50 + len(coin.tx_out.script_pub_key.script)
    assert stats.digest() != CoinStats().digest()


def test_coin_stats_remove_undoes_insert_exactly() -> None:
    """`remove` after `insert` on the same coin restores every counter."""
    stats = CoinStats()
    out_point_bytes, coin = _a_coin()
    stats.insert(out_point_bytes, coin)
    assert stats.remove(out_point_bytes, coin) is True
    empty = CoinStats()
    assert stats.digest() == empty.digest()
    assert stats.transaction_output_count == empty.transaction_output_count
    assert stats.total_amount == empty.total_amount
    assert stats.bogo_size == empty.bogo_size


def test_coin_stats_skips_an_unspendable_coin_entirely() -> None:
    """An `OP_RETURN` coin changes none of the four fields, either way."""
    stats = CoinStats()
    out_point_bytes, coin = _a_coin(unspendable=True)
    assert stats.insert(out_point_bytes, coin) is False
    empty = CoinStats()
    assert stats.digest() == empty.digest()
    assert stats.transaction_output_count == 0
    assert stats.total_amount == 0
    assert stats.bogo_size == 0
    assert stats.remove(out_point_bytes, coin) is False
    assert stats.digest() == empty.digest()


def test_coin_stats_serialize_round_trips() -> None:
    """`CoinStats.deserialize(serialize())` reproduces every field."""
    stats = CoinStats()
    stats.insert(*_a_coin(value=777))
    restored = CoinStats.deserialize(stats.serialize())
    assert restored.digest() == stats.digest()
    assert restored.transaction_output_count == stats.transaction_output_count
    assert restored.total_amount == stats.total_amount
    assert restored.bogo_size == stats.bogo_size


def test_module_exports_match_what_the_suite_and_utxo_index_use() -> None:
    """A quick guard against `__all__` drifting from what is actually used."""
    for name in muhash_module.__all__:
        assert hasattr(muhash_module, name)
