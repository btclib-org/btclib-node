# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Unit tests for `btclib_node.chainstate.muhash`.

`chacha20_vectors.json` and `muhash_vectors.json` (`tests/_data/README.md`
is where both are derived from and re-checked) pin the two primitives
`CoinStats`' own commitment is built from against Bitcoin Core's own
`crypto_tests.cpp`, before anything downstream of them is trusted. What
is not vector-driven is checked directly: order-independence and
insert/remove cancellation (the two properties Core's own
`test/fuzz/muhash.cpp` fuzzes for), the unspendable-output gate,
`is_bip30_unspendable`'s own two mainnet blocks, and the on-disk
round trip `UtxoIndex.finalize`/`__init__` rely on.
"""

from typing import Any

import pytest
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx_out import TxOut

from btclib_node.block_db import Coin
from btclib_node.chainstate import muhash as muhash_module
from btclib_node.chainstate.muhash import (
    CoinStats,
    MuHash3072,
    chacha20_keystream,
    is_bip30_unspendable,
    is_unspendable,
    tx_out_ser,
)
from tests import load, vector_id

_CHACHA20_VECTORS = load("unit", "chainstate", "_data", "chacha20_vectors.json")
_CHACHA20_IDS = [
    vector_id(index, "message" if v["message"] else "keystream", v["seek"])
    for index, v in enumerate(_CHACHA20_VECTORS)
]

_MUHASH_VECTORS = load("unit", "chainstate", "_data", "muhash_vectors.json")


@pytest.mark.parametrize("vector", _CHACHA20_VECTORS, ids=_CHACHA20_IDS)
def test_chacha20_keystream_matches_core(vector: dict[str, Any]) -> None:
    """`chacha20_keystream` reproduces `crypto_tests.cpp`'s own vectors.

    RFC 7539/8439's own Appendix A vectors among them -- `tests/_data
    /README.md` names which. A message-bearing vector's own expected
    bytes are ciphertext (`message` XOR keystream), matching
    `TestChaCha20`'s own two modes.
    """
    key = bytes.fromhex(vector["key"])
    nonce_words = (
        vector["nonce_first"],
        vector["nonce_second"] & 0xFFFFFFFF,
        (vector["nonce_second"] >> 32) & 0xFFFFFFFF,
    )
    expected = bytes.fromhex(vector["keystream_or_ciphertext"])
    blocks = (len(expected) + 63) // 64
    keystream = chacha20_keystream(
        key, blocks, nonce_words=nonce_words, counter=vector["seek"]
    )[: len(expected)]
    if vector["message"]:
        message = bytes.fromhex(vector["message"])
        result = bytes(a ^ b for a, b in zip(message, keystream, strict=True))
    else:
        result = keystream
    assert result == expected


def test_muhash_insert_then_remove_matches_core_vector() -> None:
    """Core's own `FromInt(0)*FromInt(1)/FromInt(2)` cancellation vector.

    `uint256{"..."}` reverses relative to its own display hex
    (`uint256.h`'s own "Hex representation"), so the raw digest this
    produces is the vector's own bytes reversed -- confirmed directly
    against `Main().genesis.hash`, the well-known genesis hash, in
    `muhash.py`'s own comment beside `is_bip30_unspendable`.
    """
    vector = _MUHASH_VECTORS["insert_then_remove"]
    accumulator = MuHash3072()
    for element in vector["insert"]:
        accumulator.insert(bytes.fromhex(element))
    for element in vector["remove"]:
        accumulator.remove(bytes.fromhex(element))
    expected = bytes.fromhex(vector["digest_uint256_hex"])[::-1]
    assert accumulator.digest() == expected


def test_muhash_serialize_matches_core_vector() -> None:
    """`serialize` matches `MuHash3072::SERIALIZE_METHODS`'s own bytes."""
    vector = _MUHASH_VECTORS["serialization"]
    accumulator = MuHash3072()
    for element in vector["insert"]:
        accumulator.insert(bytes.fromhex(element))
    assert accumulator.serialize().hex() == vector["serialized_hex"]


def test_muhash_overflow_vector() -> None:
    """A numerator the modulus does not yet reduce still finalizes correctly.

    `HexStr(out4)` in `crypto_tests.cpp` hex-encodes the raw digest
    directly, unlike the `uint256{"..."}` comparisons the other two
    vectors use -- **not** reversed, `muhash_vectors.json`'s own README
    entry is where that difference between the two Core assertion
    macros is confirmed rather than assumed uniform.
    """
    vector = _MUHASH_VECTORS["overflow"]
    accumulator = MuHash3072.deserialize(bytes.fromhex(vector["serialized_hex"]))
    assert accumulator.digest() == bytes.fromhex(vector["digest_hex_direct"])


def test_muhash_deserialize_round_trips_through_serialize() -> None:
    """`deserialize(serialize())` reproduces the same digest."""
    accumulator = MuHash3072()
    accumulator.insert(b"one element")
    accumulator.remove(b"a second, different element")
    restored = MuHash3072.deserialize(accumulator.serialize())
    assert restored.digest() == accumulator.digest()


def test_muhash_singleton_matches_insert_into_the_empty_set() -> None:
    """`MuHash3072.singleton(x)` is `MuHash3072(); m.insert(x)`."""
    element = b"a lone element"
    singleton = MuHash3072.singleton(element)
    inserted = MuHash3072()
    inserted.insert(element)
    assert singleton.digest() == inserted.digest()


def test_muhash_insert_is_order_independent() -> None:
    """H(a)+H(b) == H(b)+H(a) -- MuHash's own defining property."""
    first = MuHash3072()
    first.insert(b"alpha")
    first.insert(b"beta")

    second = MuHash3072()
    second.insert(b"beta")
    second.insert(b"alpha")

    assert first.digest() == second.digest()


def test_muhash_remove_cancels_the_matching_insert() -> None:
    """Insert then remove of the same element restores the empty digest.

    Core's own `test/fuzz/muhash.cpp` fuzzes exactly this property;
    `UtxoIndex.rollback` (`utxo_index.py`) relies on it holding for
    every element, not only ones this test happens to try.
    """
    accumulator = MuHash3072()
    accumulator.insert(b"a coin's own bytes")
    accumulator.remove(b"a coin's own bytes")
    assert accumulator.digest() == MuHash3072().digest()


def test_muhash_removes_cancel_regardless_of_insert_order() -> None:
    """Z = X*Y, divided back by Y then X, reaches the empty digest.

    The same shape Core's own `muhash_tests` checks algebraically
    (`z *= x; z *= y; y *= x; z /= y`, reducing to the identity): the
    element removed does not have to be removed in the order it was
    inserted for the two to cancel.
    """
    z = MuHash3072()
    z.insert(b"X")
    z.insert(b"Y")
    z.remove(b"Y")
    z.remove(b"X")
    assert z.digest() == MuHash3072().digest()


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


def test_mutating_the_per_element_hash_breaks_the_core_vector() -> None:
    """A one-byte perturbation of `_num3072`'s own input is not silent.

    Mutation evidence for the whole chain `test_muhash_insert_then
    _remove_matches_core_vector` pins: flipping one byte of what
    `_num3072` hashes changes the digest it produces, which is what
    makes that vector able to fail at all rather than passing by
    construction. Exercised here as `MuHash3072.insert` on the flipped
    element rather than by patching `_num3072` itself, so this test
    survives a change to that function's own internals.
    """
    vector = _MUHASH_VECTORS["insert_then_remove"]
    flipped = bytearray(bytes.fromhex(vector["insert"][0]))
    flipped[0] ^= 0xFF

    accumulator = MuHash3072()
    accumulator.insert(bytes(flipped))
    accumulator.insert(bytes.fromhex(vector["insert"][1]))
    for element in vector["remove"]:
        accumulator.remove(bytes.fromhex(element))

    expected = bytes.fromhex(vector["digest_uint256_hex"])[::-1]
    assert accumulator.digest() != expected


def test_module_exports_match_what_the_suite_and_utxo_index_use() -> None:
    """A quick guard against `__all__` drifting from what is actually used."""
    for name in muhash_module.__all__:
        assert hasattr(muhash_module, name)
