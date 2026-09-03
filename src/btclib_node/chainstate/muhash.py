# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`CoinStats`: the UTXO set's per-block MuHash commitment.

The accumulator itself, `btclib.muhash.MuHash3072`, is btclib's own --
[btclib#1122](https://github.com/btclib-org/btclib/issues/1122) moved
the construction, the per-element hash and the ChaCha20 block function
this module used to carry, and `btclib.muhash`'s own module docstring is
where all three are now argued, RFC 8439's own vectors and Core's own
`muhash_tests` (`src/test/crypto_tests.cpp`) included. What stays here
is `CoinStats`: `insert`/`remove` are exact inverses of each other on
the same bytes, in either order and regardless of anything else
inserted or removed meanwhile, which `UtxoIndex.rollback` below relies
on to undo a staged insert with a remove and vice versa, without
recording what the accumulator's own state was before either.

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

## What is inserted, and what is not

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
block (91842, 91880 -- `IsBIP30Repeat`, the pair `btclib.consensus`'s
own `Chain.consensus.bip30_exceptions` names, which is what
`_check_bip30` on the *second* occurrence is waived against, not this
one).
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
tree's own test suite runs (`Chain.consensus.bip30_exceptions` is empty
on every chain but mainnet), so this exclusion is matched against
Core's source rather than against a live run of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from btclib.muhash import MuHash3072

if TYPE_CHECKING:
    from btclib_node.block_db import Coin

__all__ = [
    "CoinStats",
    "is_bip30_unspendable",
    "is_unspendable",
    "tx_out_ser",
]

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
    why, and `btclib.consensus`'s own `Chain.consensus.bip30_exceptions`
    is the *different* pair this is not.
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
        """Return the 32-byte commitment -- `self.muhash.digest`."""
        return self.muhash.digest

    def serialize(self) -> bytes:
        """Return this tree's own shape: the muhash, then three counters.

        Each counter is 8 bytes, signed, big-endian. `KeyValueStore`'s
        own meta column family is where `UtxoIndex.finalize` writes
        this, in the same `write_batch` as the coins it commits to
        (`db.py`'s docstring argues why).
        """
        out = self.muhash.serialize
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
