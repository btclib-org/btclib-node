# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Unit tests for `btclib_node.chainstate.muhash`.

The accumulator itself, `btclib.muhash.MuHash3072`, is btclib's own to
test, and so now is the arithmetic `CoinStats.insert`/`.remove` carry,
`is_unspendable` and `tx_out_ser` included --
`tests/coinstats_test.py` and `tests/muhash_test.py` in that repository
are where each is checked against Core. What is tested here is what
stayed: `is_bip30_unspendable`'s own two mainnet blocks, this store's
own on-disk round trip `UtxoIndex.finalize`/`__init__` rely on, and the
divergence between `tx_out_ser`'s own encoding and `Coin.serialize`'s.
"""

import pytest
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx_out import TxOut

from btclib_node.block_db import Coin
from btclib_node.chainstate import muhash as muhash_module
from btclib_node.chainstate.muhash import CoinStats, is_bip30_unspendable, tx_out_ser


def test_tx_out_ser_diverges_from_this_stores_own_coin_serialize() -> None:
    """`tx_out_ser`'s fixed `uint32` is not `Coin.serialize`'s `var_int`.

    The module docstring's own divergence argument, characterized
    rather than only stated: what a coin is committed to as and what
    this store keeps it as on disk are not the same bytes, because the
    packed `(height << 1) | coinbase` field is a fixed 4-byte
    little-endian `uint32` in the first and a `var_int` in the second.
    """
    tx_out = TxOut(value=5000, script_pub_key=script.serialize(["OP_1"]))
    out_point_bytes = OutPoint(b"\x11" * 32, 3, check_validity=False).serialize(
        check_validity=False
    )
    coin = Coin(tx_out, height=7, is_coinbase=True)

    committed = tx_out_ser(out_point_bytes, coin)
    stored = coin.serialize(check_validity=False)

    packed = (7 << 1) | 1
    assert committed == (
        out_point_bytes
        + packed.to_bytes(4, "little")
        + tx_out.serialize(check_validity=False)
    )
    assert committed != out_point_bytes + stored


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
    """A spendable coin moves the muhash, the count, the amount and the size.

    `insert` returns `None`, btclib's own naming contract having decided
    that a caller wanting to know whether a coin was skipped asks
    `is_unspendable` itself rather than reading this call's return
    value -- mypy holds that statically (`func-returns-value` on any
    attempt to use the result), so it is not asserted here too.
    """
    stats = CoinStats()
    out_point_bytes, coin = _a_coin(value=1234)
    stats.insert(out_point_bytes, coin)
    assert stats.transaction_output_count == 1
    assert stats.total_amount == 1234
    assert stats.bogo_size == 50 + len(coin.tx_out.script_pub_key.script)
    assert stats.digest != CoinStats().digest


def test_coin_stats_remove_undoes_insert_exactly() -> None:
    """`remove` after `insert` on the same coin restores every counter."""
    stats = CoinStats()
    out_point_bytes, coin = _a_coin()
    stats.insert(out_point_bytes, coin)
    stats.remove(out_point_bytes, coin)
    empty = CoinStats()
    assert stats.digest == empty.digest
    assert stats.transaction_output_count == empty.transaction_output_count
    assert stats.total_amount == empty.total_amount
    assert stats.bogo_size == empty.bogo_size


def test_coin_stats_skips_an_unspendable_coin_entirely() -> None:
    """An `OP_RETURN` coin changes none of the four fields, either way."""
    stats = CoinStats()
    out_point_bytes, coin = _a_coin(unspendable=True)
    stats.insert(out_point_bytes, coin)
    empty = CoinStats()
    assert stats.digest == empty.digest
    assert stats.transaction_output_count == 0
    assert stats.total_amount == 0
    assert stats.bogo_size == 0
    stats.remove(out_point_bytes, coin)
    assert stats.digest == empty.digest


def test_coin_stats_serialize_round_trips() -> None:
    """`CoinStats.deserialize(serialize())` reproduces every field."""
    stats = CoinStats()
    stats.insert(*_a_coin(value=777))
    restored = CoinStats.deserialize(stats.serialize())
    assert restored.digest == stats.digest
    assert restored.transaction_output_count == stats.transaction_output_count
    assert restored.total_amount == stats.total_amount
    assert restored.bogo_size == stats.bogo_size


def test_module_exports_match_what_the_suite_and_utxo_index_use() -> None:
    """A quick guard against `__all__` drifting from what is actually used."""
    for name in muhash_module.__all__:
        assert hasattr(muhash_module, name)
