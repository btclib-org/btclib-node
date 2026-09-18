# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`CoinStats`: this store's own on-disk shape for the UTXO commitment.

`btclib.coinstats.CoinStats` -- this tree's equivalent of Core's
incrementally-maintained `CoinStatsIndex` (`src/index/coinstatsindex.cpp`)
-- is the accumulator, the three running counters beside it, the bytes
a coin is committed to as (`tx_out_ser`) and the gate on what is
committed to at all (`btclib.script.spendability.is_unspendable`).
[btclib#1623](https://github.com/btclib-org/btclib/issues/1623) is
where moving them out of this module was decided, and
`btclib.coinstats`'s own module docstring is where the construction,
the counters and the gate are all argued, `insert`/`remove` being each
other's exact inverses on the same bytes included. `UtxoIndex.rollback`
relies on that inverse property to undo a staged insert with a remove
and vice versa, without recording what the accumulator's own state was
before either.

What stays here is what btclib's own class carries no opinion on: a
`serialize`/`deserialize` pair for this store's on-disk layout -- the
same seam `btclib.tx.coin.Coin` already draws for the same reason, and
why `block_db.Coin` is the wire-format subclass on that side of it,
this `CoinStats` subclass being the other -- and `is_bip30_unspendable`
with the two-block pair it checks, which is
[btclib#1695](https://github.com/btclib-org/btclib/issues/1695)'s own
follow-up and not btclib's.

## The serialization this store chose, and the one a coin is committed to as

`btclib.coinstats`'s own module docstring already argues why packing
`tx_out_ser`'s `(height << 1) | coinbase` field differently from its
own fixed 4-byte little-endian `uint32` produces a different digest for
the same coin. This store's choice is `Coin.serialize`
(`block_db/__init__.py`): a `var_int` ahead of the output, for storage
density. `CoinStats.insert`/`.remove`, inherited from btclib, build
`tx_out_ser`'s own encoding fresh from `coin.height` and
`coin.is_coinbase` every time, never from `Coin.serialize`'s bytes, so
the two encodings never have to agree.

## What is inserted, and what is not

`is_unspendable` gates `CoinStats.insert`/`.remove` independently of
what `UtxoIndex`'s own store keeps, so the two accumulators agree on a
block carrying an `OP_RETURN` output regardless of whether the store
also refuses to hold one (btclib-org/btclib-node#667).
`UtxoIndex.add_block` also skips storing such an output under a
`utxo-` key at all, matching `CCoinsViewCache::AddCoin`'s own refusal
directly rather than only agreeing with it through `CoinStats`'s own
independent gate (`utxo_index.py`'s own `_stage_creation` is where
that gate, and `apply_rev_block`'s own consequence -- an output never
stored is never restored -- are argued); `gettxoutsetinfo`'s `txouts`
and `bogosize` (`rpc/callbacks.py`) are always `CoinStats`'s own count,
never a scan of the `utxo-` namespace.

## The two blocks history exempts

`IsBIP30Unspendable` (`validation.cpp:6224-6228`, at
bitcoin/bitcoin@ca7162cde5) names two mainnet blocks, 91722 and 91812,
each mined before BIP34 gave a coinbase's own outpoint a height it
could never collide with, whose coinbase transaction was later
duplicated verbatim by a different block (91842, 91880 --
`IsBIP30Repeat`, the pair `btclib.consensus`'s own
`Chain.consensus.bip30_exceptions` names, which is what `_check_bip30`
on the *second* occurrence is waived against, not this one).
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

from btclib.coinstats import CoinStats as _BtclibCoinStats
from btclib.coinstats import tx_out_ser
from btclib.muhash import MuHash3072
from btclib.script.spendability import is_unspendable

__all__ = [
    "CoinStats",
    "is_bip30_unspendable",
    "is_unspendable",
    "tx_out_ser",
]

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


class CoinStats(_BtclibCoinStats):
    """`btclib.coinstats.CoinStats`, plus this store's own on-disk shape.

    The accumulator and the three counters beside it, `insert`,
    `remove` and `digest` included, are all `btclib.coinstats.CoinStats`'s
    own; the module docstring above is where the seam between what
    lives there and what lives here is argued. `serialize` and
    `deserialize` below are this store's own decision: the muhash
    state, then each of the three counters as 8 bytes, signed,
    big-endian. `KeyValueStore`'s own meta column family is where
    `UtxoIndex.finalize` writes this, in the same `write_batch` as the
    coins it commits to (`db.py`'s docstring argues why).
    """

    def serialize(self) -> bytes:
        """Return this tree's own shape: the muhash, then three counters."""
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
