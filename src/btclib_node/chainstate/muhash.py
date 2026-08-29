# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A rolling, order-independent commitment to the whole UTXO set.

Core's `MuHash3072` (`src/crypto/muhash.h`/`.cpp`, at
bitcoin/bitcoin@ca7162cde5) represents a multiset as a fraction of two
3072-bit numbers modulo the largest 3072-bit safe prime,
`2**3072 - 1103717`: inserting an element multiplies it into the
numerator, removing one multiplies it into the denominator, and the two
operations are exact inverses of each other regardless of order or of
what else has been inserted or removed meanwhile -- the property
`UtxoIndex` below relies on for `rollback`. `MuHash3072` here is that
same construction, `Insert`/`Remove`/`Finalize` renamed `insert`/
`remove`/`digest` -- lowercase, matched against Core's own
`muhash_tests` (`src/test/crypto_tests.cpp`) and against RFC 8439's own
ChaCha20 vectors before anything is built on it (`tests/unit/chainstate
/muhash_test.py`).

The arithmetic is native Python `int`: `pow`, `%` and `pow(x, -1, m)`
for the modular inverse `Finalize` needs, in place of Core's own
limb-by-limb `Num3072` -- a fixed-width C++ integer split into 32- or
64-bit limbs for a CPU that has no 3072-bit register, which Python's
own arbitrary-precision `int` already is without that machinery. This
is the Python-native licence `CLAUDE.md`'s own *Following Bitcoin Core*
gives: the arithmetic is native rather than reimplementing Core's own
carry-and-reduce trick, and what is committed to -- the modulus, the
per-element hash, the byte order -- is unchanged. One divergence this
buys for free rather than by design: `Num3072`'s own "overflow" state
(a value held between the modulus and `2**3072`, only reduced lazily)
has no counterpart here, because `% _MODULUS` after every multiply
keeps `_numerator`/`_denominator` always fully reduced -- cheaper in
Python than replicating the lazy reduction, and `crypto_tests.cpp`'s
own overflow vector is still matched (`muhash_test.py`), since a value
Core would carry unreduced and only fold in at `Finalize` is folded in
here immediately instead, with the same result either way.

## The per-element hash

`_num3072`, matching `MuHash3072::ToNum3072`: SHA256 of the element's
own bytes (`HashWriter::GetSHA256`, a single SHA256, not Bitcoin's usual
double one) keys a ChaCha20 stream cipher seeded at nonce zero and block
counter zero (`ChaCha20Aligned`'s own default, `SetKey`), whose first
384 bytes of keystream are read as one little-endian 3072-bit integer --
`Num3072::ToBytes`/its constructor pack each 64-bit limb little-endian
and the limbs least-significant first, which is exactly
`int.from_bytes(data, "little")` over the whole 384 bytes at once.
`_chacha20_block` below is RFC 8439's block function -- the same
`QUARTERROUND` rotation amounts (16, 12, 8, 7) and the same
column-then-diagonal ordering `chacha20.cpp`'s own unrolled `REPEAT10`
carries -- fed the block counter in word 12 and an all-zero 96-bit
nonce in words 13-15, matching `ChaCha20Aligned::Seek`'s layout at the
`(0, 0)` nonce and `0` counter `ToNum3072` never overrides. Six blocks
(`_KEYSTREAM_BLOCKS`) cover the 384 bytes `Num3072::BYTE_SIZE` names;
the 32-bit block counter never overflows into the nonce word Core's own
`++j12; if (!j12) ++j13;` carries into, six being nowhere near 2**32.

**This is the dominant cost of connecting a block**, measured directly
against a block the shape btclib-org/btclib-node#586 already measured
(964,000's own 7,778 spends and 8,100 creations): the SHA256-then-six-
ChaCha20-blocks this section builds, purely in Python, costs
low-hundreds of microseconds per coin, on the same order as or larger
than `add_block`'s own remaining work for a block that dense. Left as
pure Python for now regardless -- `CLAUDE.md`'s own Python-native
licence -- with the number itself in the pull request rather than here,
where it would age; a compiled ChaCha20 is a decision for whoever reads
that number next, not one this module makes for them.

## What is inserted, and what is not

`CoinStats` bundles the accumulator with the three running counters
Core's own `-coinstatsindex` (`src/index/coinstatsindex.cpp`, same sha)
keeps beside it -- `transaction_output_count`, `total_amount`,
`bogo_size` -- because `gettxoutsetinfo` reports all four from the same
incrementally-maintained state rather than a fresh scan
(`rpc/callbacks.py`'s own `get_tx_out_set_info` is where the two are
told apart). `tx_out_ser` matches `TxOutSer`
(`src/kernel/coinstats.cpp`): the outpoint (36 bytes, already this
tree's own wire serialization), the packed `(height << 1) | coinbase`
as a **fixed 4-byte little-endian `uint32`** -- unlike `Coin.serialize`
in `block_db/__init__.py`, which packs the same field as a `var_int` for
storage density, Core's `TxOutSer` uses `ss << (uint32_t)packed`, fixed
width, and the two encodings are not interchangeable -- and the output
itself, `TxOut.serialize`, byte for byte `CTxOut`'s own. `bogo_size`
matches `GetBogoSize` (`kernel/coinstats.cpp`): a fixed 50 bytes plus
the script's own length, not the script's actual serialized size
(Core's own comment calls it a "database-independent metric" for a
reason -- it is not meant to reproduce a wire size).

`is_unspendable` matches `CScript::IsUnspendable`
(`script/script.h:564-567`, same sha): a leading `OP_RETURN` (`0x6a`) or
a script over `MAX_SCRIPT_SIZE` (10,000 bytes). `CCoinsViewCache
::AddCoin` (`coins.cpp:82`) returns without ever adding such an output
to Core's own UTXO set in the first place, so `ApplyCoinHash` never
sees one either. `CoinStats.insert`/`remove` gate on it independently
of what `UtxoIndex`'s own store keeps -- which is what already made
the digest and the three counters match Core's on a block carrying an
`OP_RETURN` output before `UtxoIndex`'s own `add_block` gated on it too
(btclib-org/btclib-node#667): even while the two disagreed on
membership, neither ever offered an unspendable output to
`ApplyCoinHash`/`CoinStats.insert`, so the two accumulators agreed
regardless. `UtxoIndex.add_block` now also skips storing such an
output under a `utxo-` key at all, matching `AddCoin`'s own refusal
directly rather than only agreeing with it through `CoinStats`'s own
independent gate (`utxo_index.py`'s own `_stage_creation` is where
that gate, and `apply_rev_block`'s own consequence -- an output never
stored is never restored -- are argued); `gettxoutsetinfo`'s `txouts`
and `bogosize` (`rpc/callbacks.py`) were always `CoinStats`'s own
count, never a scan of the `utxo-` namespace, so this changes nothing
either answers.

## The two blocks history exempts

`IsBIP30Unspendable` (`validation.cpp:6224-6228`, same sha) names two
mainnet blocks, 91722 and 91812, each mined before BIP34 gave a
coinbase's own outpoint a height it could never collide with,
whose coinbase transaction was later duplicated verbatim by a different
block (91842, 91880 -- `IsBIP30Repeat`, the pair `chains.py`'s own
`Chain.bip30_exceptions` names, which is what `_check_bip30` on the
*second* occurrence is waived against, not this one).
`CoinStatsIndex::CustomAppend` skips a duplicated coinbase's own outputs
entirely -- never inserted, so never later removed either -- on
whichever of the two connects *first* carrying that flag: the first
occurrence is never hashed, so the second occurrence's own ordinary
insertion is the only one the accumulator ever carries, and the
outpoint's later spend correctly cancels exactly that one. Reproduced
here as `_BIP30_UNSPENDABLE_ORIGINALS`, checked once per connecting
block's own coinbase in `UtxoIndex.add_block` -- `chains.py` is other
work's own region for this branch, so the pair is local rather than a
new `Chain` attribute. Both blocks are ninety-odd thousand mainnet
blocks deep and neither height nor hash is reachable on any chain this
tree's own test suite runs (`Chain.bip30_exceptions` is empty on every
chain but mainnet), so this exclusion is matched against Core's source
rather than against a live run of it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btclib_node.block_db import Coin

__all__ = [
    "CoinStats",
    "MuHash3072",
    "chacha20_keystream",
    "is_bip30_unspendable",
    "is_unspendable",
    "tx_out_ser",
]

# 2**3072 - 1103717, the largest 3072-bit safe prime -- muhash.cpp's own
# MAX_PRIME_DIFF, at bitcoin/bitcoin@ca7162cde5
_MODULUS = (1 << 3072) - 1_103_717

# Num3072::BYTE_SIZE (muhash.h) and ChaCha20Aligned::BLOCKLEN (64):
# 384 / 64 == 6, the module docstring's own "six blocks" above
_BYTE_SIZE = 384
_KEYSTREAM_BLOCKS = _BYTE_SIZE // 64

_MASK32 = 0xFFFF_FFFF

# the ASCII string "expand 32-byte k", split into four 4-byte
# little-endian words -- ChaCha20's own constant, chacha20.cpp:84-87
_CONSTANTS = (0x6170_7865, 0x3320_646E, 0x7962_2D32, 0x6B20_6574)


def _rotl32(x: int, n: int) -> int:
    """Rotate the low 32 bits of `x` left by `n`, RFC 8439's own `<<<=`."""
    return ((x << n) | (x >> (32 - n))) & _MASK32


def _quarter_round(a: int, b: int, c: int, d: int) -> tuple[int, int, int, int]:
    """One ChaCha20 quarter round -- chacha20.cpp's own `QUARTERROUND` macro."""
    a = (a + b) & _MASK32
    d = _rotl32(d ^ a, 16)
    c = (c + d) & _MASK32
    b = _rotl32(b ^ c, 12)
    a = (a + b) & _MASK32
    d = _rotl32(d ^ a, 8)
    c = (c + d) & _MASK32
    b = _rotl32(b ^ c, 7)
    return a, b, c, d


def _chacha20_block(
    key_words: tuple[int, ...], nonce_words: tuple[int, int, int], counter: int
) -> bytes:
    """Build one 64-byte ChaCha20 block, RFC 8439's own block function.

    `ChaCha20Aligned::Keystream` (chacha20.cpp), unrolled the same way:
    ten double-rounds, columns then diagonals, the original state added
    back into the working state before the block is serialized
    little-endian word by word. `nonce_words` are state words 13-15 --
    `ChaCha20Aligned::Seek`'s own `nonce.first`, then the low and high
    32 bits of `nonce.second` -- and `counter` is word 12.
    """
    x0, x1, x2, x3 = _CONSTANTS
    x4, x5, x6, x7, x8, x9, x10, x11 = key_words
    x12, x13, x14, x15 = counter, *nonce_words

    for _ in range(10):
        x0, x4, x8, x12 = _quarter_round(x0, x4, x8, x12)
        x1, x5, x9, x13 = _quarter_round(x1, x5, x9, x13)
        x2, x6, x10, x14 = _quarter_round(x2, x6, x10, x14)
        x3, x7, x11, x15 = _quarter_round(x3, x7, x11, x15)
        x0, x5, x10, x15 = _quarter_round(x0, x5, x10, x15)
        x1, x6, x11, x12 = _quarter_round(x1, x6, x11, x12)
        x2, x7, x8, x13 = _quarter_round(x2, x7, x8, x13)
        x3, x4, x9, x14 = _quarter_round(x3, x4, x9, x14)

    words = (
        (x0 + _CONSTANTS[0]) & _MASK32,
        (x1 + _CONSTANTS[1]) & _MASK32,
        (x2 + _CONSTANTS[2]) & _MASK32,
        (x3 + _CONSTANTS[3]) & _MASK32,
        (x4 + key_words[0]) & _MASK32,
        (x5 + key_words[1]) & _MASK32,
        (x6 + key_words[2]) & _MASK32,
        (x7 + key_words[3]) & _MASK32,
        (x8 + key_words[4]) & _MASK32,
        (x9 + key_words[5]) & _MASK32,
        (x10 + key_words[6]) & _MASK32,
        (x11 + key_words[7]) & _MASK32,
        (x12 + counter) & _MASK32,
        (x13 + nonce_words[0]) & _MASK32,
        (x14 + nonce_words[1]) & _MASK32,
        (x15 + nonce_words[2]) & _MASK32,
    )
    return b"".join(w.to_bytes(4, "little") for w in words)


def chacha20_keystream(
    key: bytes,
    blocks: int,
    *,
    nonce_words: tuple[int, int, int] = (0, 0, 0),
    counter: int = 0,
) -> bytes:
    """`blocks` blocks (`blocks * 64` bytes) of ChaCha20 keystream.

    `nonce_words` and `counter` default to zero -- `ChaCha20Aligned`'s
    own default, matching `MuHash3072::ToNum3072`'s usage
    (`ChaCha20Aligned{key}.Keystream(...)`, no `Seek` call), which is
    the only way `_num3072` below ever calls this. The two arguments
    exist so `muhash_test.py` can drive this same function against
    `crypto_tests.cpp`'s own RFC 7539/8439 vectors, seeked to a real
    nonce and counter this tree never needs otherwise -- including the
    32-bit counter overflow that carries into `nonce_words[0]`, matching
    `chacha20.cpp`'s own `++j12; if (!j12) ++j13;`.
    """
    key_words = tuple(
        int.from_bytes(key[4 * i : 4 * i + 4], "little") for i in range(8)
    )
    out = bytearray()
    for i in range(blocks):
        # ++j12; if (!j12) ++j13; (chacha20.cpp): the 32-bit block
        # counter wraps into the nonce's own first word. total >> 32 is
        # 0 for every block this tree's own six-block calls ever reach,
        # and above 0 only for the overflow vector this exercises it
        # against.
        total = counter + i
        block_counter = total & _MASK32
        word0 = (nonce_words[0] + (total >> 32)) & _MASK32
        out += _chacha20_block(
            key_words, (word0, nonce_words[1], nonce_words[2]), block_counter
        )
    return bytes(out)


def _num3072(data: bytes) -> int:
    """`MuHash3072::ToNum3072`: SHA256(data) keys 384 bytes of keystream."""
    key = hashlib.sha256(data).digest()
    keystream = chacha20_keystream(key, _KEYSTREAM_BLOCKS)
    return int.from_bytes(keystream, "little")


class MuHash3072:
    """A running numerator/denominator over `_MODULUS` -- Core's `MuHash3072`.

    The module docstring above is where the construction, the per-element
    hash and the "no overflow state" divergence are all argued.
    """

    __slots__ = ("_denominator", "_numerator")

    def __init__(self, numerator: int = 1, denominator: int = 1) -> None:
        """Start at the empty set by default -- `Num3072`'s own `SetToOne`."""
        self._numerator = numerator % _MODULUS
        self._denominator = denominator % _MODULUS

    @classmethod
    def singleton(cls, data: bytes) -> MuHash3072:
        """Build a set holding exactly `data` -- the `MuHash3072(span)` ctor."""
        return cls(numerator=_num3072(data))

    def insert(self, data: bytes) -> None:
        """Multiply `data` into the numerator -- `MuHash3072::Insert`."""
        self._numerator = (self._numerator * _num3072(data)) % _MODULUS

    def remove(self, data: bytes) -> None:
        """Multiply `data` into the denominator -- `MuHash3072::Remove`.

        The exact inverse of `insert` on the same bytes, in either
        order and regardless of anything else inserted or removed
        meanwhile: `insert`/`remove` only ever multiply the numerator
        and the denominator independently, and a factor common to both
        cancels out at `digest`'s own division whatever else multiplied
        either one in between. `UtxoIndex.rollback` below relies on
        exactly this to undo a staged insert with a remove and vice
        versa, without recording what the accumulator's own state was
        before either.
        """
        self._denominator = (self._denominator * _num3072(data)) % _MODULUS

    def digest(self) -> bytes:
        """Return the 32-byte commitment -- `MuHash3072::Finalize`.

        Core's own `Finalize` divides the numerator by the denominator
        in place and resets the denominator to one -- a normalization
        that leaves the represented *value* unchanged (its own comment
        says so) but is otherwise pure bookkeeping, so this returns the
        digest without mutating `self`: every caller here (`CoinStats
        .digest`, `muhash_test.py`) reads a value that keeps
        accumulating afterwards, unlike Core's own single-use `MuHash3072
        acc` locals in `crypto_tests.cpp`.
        """
        value = (self._numerator * pow(self._denominator, -1, _MODULUS)) % _MODULUS
        return hashlib.sha256(value.to_bytes(_BYTE_SIZE, "little")).digest()

    def serialize(self) -> bytes:
        """768 bytes: the numerator, then the denominator, each 384 bytes LE.

        This tree's own on-disk shape, not Core's -- `MuHash3072
        ::SERIALIZE_METHODS` serializes the same two numbers in the same
        order, matched here only because the shape is the obvious one,
        not because anything reads this file's bytes with Core's own
        code.
        """
        return self._numerator.to_bytes(
            _BYTE_SIZE, "little"
        ) + self._denominator.to_bytes(_BYTE_SIZE, "little")

    @classmethod
    def deserialize(cls, data: bytes) -> MuHash3072:
        """Parse the 768 bytes `serialize` produced."""
        numerator = int.from_bytes(data[:_BYTE_SIZE], "little")
        denominator = int.from_bytes(data[_BYTE_SIZE:], "little")
        return cls(numerator, denominator)


# CScript::IsUnspendable's own two thresholds (script/script.h:41,564-567,
# at bitcoin/bitcoin@ca7162cde5): a leading OP_RETURN, or a script over
# this many bytes.
_OP_RETURN = 0x6A
_MAX_SCRIPT_SIZE = 10_000


def is_unspendable(script: bytes) -> bool:
    """Report whether `script` can never be spent -- `IsUnspendable`."""
    return (len(script) > 0 and script[0] == _OP_RETURN) or len(
        script
    ) > _MAX_SCRIPT_SIZE


def tx_out_ser(out_point_bytes: bytes, coin: Coin) -> bytes:
    """Return the bytes `ApplyCoinHash`/`RemoveCoinHash` insert -- `TxOutSer`.

    `out_point_bytes` is the 36-byte wire outpoint a caller here already
    has (`OutPoint.serialize`, byte for byte `COutPoint`'s own); the
    packed `(height << 1) | coinbase` is a fixed 4-byte little-endian
    `uint32`, not the `var_int` `Coin.serialize` (`block_db/__init__.py`)
    packs the same field as for on-disk density -- the module docstring
    above is where that divergence from this tree's own storage format
    is argued. `coin.tx_out.serialize` closes it out, byte for byte
    `CTxOut`'s own.
    """
    packed = (coin.height << 1) | int(coin.is_coinbase)
    return (
        out_point_bytes
        + packed.to_bytes(4, "little")
        + coin.tx_out.serialize(check_validity=False)
    )


def _bogo_size(script: bytes) -> int:
    """Return `GetBogoSize`'s own fixed shape, not the script's wire size."""
    return 32 + 4 + 4 + 8 + 2 + len(script)


# The two mainnet blocks IsBIP30Unspendable names (validation.cpp
# :6224-6228, at bitcoin/bitcoin@ca7162cde5) -- the module docstring's
# own "The two blocks history exempts" is where the pair, and why it is
# local to this file rather than a Chain attribute, is argued. Core's
# own literal there is a `uint256`, which reverses relative to its own
# display hex (uint256.h's own "Hex representation"); `BlockHeader.hash`
# (btclib) is already the reversed, display-order form -- confirmed
# directly against `Main().genesis.hash.hex()`, the well-known genesis
# hash -- so the bytes compared against it here are `bytes.fromhex` of
# the same display string with no further reversal.
_BIP30_UNSPENDABLE_ORIGINALS = frozenset(
    {
        (
            91722,
            bytes.fromhex(
                "00000000000271a2dc26e7667f8419f2e15416dc6955e5a6c6cdf3f2574dd08e"
            ),
        ),
        (
            91812,
            bytes.fromhex(
                "00000000000af0aed4792b1acee3d966af36cf5def14935db8de83d6f9306f2f"
            ),
        ),
    }
)


def is_bip30_unspendable(height: int, block_hash: bytes) -> bool:
    """Report whether `block_hash`/`height` is one `CoinStatsIndex` skips.

    The module docstring's own "The two blocks history exempts" argues
    why, and `chains.py`'s own `Chain.bip30_exceptions` is the
    *different* pair this is not.
    """
    return (height, block_hash) in _BIP30_UNSPENDABLE_ORIGINALS


@dataclass
class CoinStats:
    """The MuHash accumulator plus Core's own three running counters.

    `transaction_output_count`, `total_amount` and `bogo_size` are
    `CoinStatsIndex`'s own `m_transaction_output_count`, `m_total_amount`
    and `m_bogo_size` (`index/coinstatsindex.cpp`, same sha) --
    maintained the same way the accumulator is, incrementally, rather
    than by scanning the set `rpc/callbacks.py`'s `get_tx_out_set_info`
    answers from.
    """

    muhash: MuHash3072 = field(default_factory=MuHash3072)
    transaction_output_count: int = 0
    total_amount: int = 0
    bogo_size: int = 0

    def insert(self, out_point_bytes: bytes, coin: Coin) -> bool:
        """Insert `coin`; `False` and a no-op if it is unspendable.

        The module docstring's own "What is inserted, and what is not"
        argues the gate; `UtxoIndex._hash_insert` is the one caller, and
        `remove` below is its exact undo regardless of this return
        value -- a coin this skips is skipped identically on the way
        back out.
        """
        script = coin.tx_out.script_pub_key.script
        if is_unspendable(script):
            return False
        self.muhash.insert(tx_out_ser(out_point_bytes, coin))
        self.transaction_output_count += 1
        self.total_amount += coin.tx_out.value
        self.bogo_size += _bogo_size(script)
        return True

    def remove(self, out_point_bytes: bytes, coin: Coin) -> bool:
        """Remove `coin`; `False` and a no-op if it is unspendable."""
        script = coin.tx_out.script_pub_key.script
        if is_unspendable(script):
            return False
        self.muhash.remove(tx_out_ser(out_point_bytes, coin))
        self.transaction_output_count -= 1
        self.total_amount -= coin.tx_out.value
        self.bogo_size -= _bogo_size(script)
        return True

    def digest(self) -> bytes:
        """Return the 32-byte commitment -- `self.muhash.digest()`."""
        return self.muhash.digest()

    def serialize(self) -> bytes:
        """Return this tree's own shape: the muhash, then three counters.

        Each counter is 8 bytes, signed, big-endian. `KeyValueStore`'s
        own meta column family is where `UtxoIndex.finalize` writes
        this, in the same `write_batch` as the coins it commits to
        (`db.py`'s docstring argues why).
        """
        out = self.muhash.serialize()
        out += self.transaction_output_count.to_bytes(8, "big", signed=True)
        out += self.total_amount.to_bytes(8, "big", signed=True)
        out += self.bogo_size.to_bytes(8, "big", signed=True)
        return out

    @classmethod
    def deserialize(cls, data: bytes) -> CoinStats:
        """Parse the bytes `serialize` produced."""
        muhash = MuHash3072.deserialize(data[:768])
        count = int.from_bytes(data[768:776], "big", signed=True)
        amount = int.from_bytes(data[776:784], "big", signed=True)
        bogo_size = int.from_bytes(data[784:792], "big", signed=True)
        return cls(muhash, count, amount, bogo_size)
