# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The UTXO set snapshot file `dumptxoutset` writes.

Core's `SnapshotMetadata` (`src/node/utxo_snapshot.h`) and the coin
serialization of `WriteUTXOSnapshot` (`src/rpc/blockchain.cpp`) with
`Coin::Serialize` (`src/coins.h`), at bitcoin/bitcoin@9be056a8a7, the
v31.1 tag.

The file is the metadata, then each transaction's unspent outputs: its
txid, how many there are, and for each the output's index and the coin.
A coin is its height and coinbase flag, its amount and its script, the
last two compressed by `btclib.compressor`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from btclib import var_int
from btclib.compressor import compress_amount, serialize_script, serialize_varint

if TYPE_CHECKING:
    from collections.abc import Iterable

    from btclib_node.block_db import Coin

__all__ = [
    "serialize_coin",
    "serialize_metadata",
    "serialize_tx_coins",
]

# `SNAPSHOT_MAGIC_BYTES` and `SnapshotMetadata::VERSION`
_MAGIC = b"utxo\xff"
_VERSION = 2


def serialize_coin(coin: Coin) -> bytes:
    """Return `Coin::Serialize`: height and coinbase, amount, script."""
    code = coin.height * 2 + int(coin.is_coinbase)
    return (
        serialize_varint(code, 32)
        + serialize_varint(compress_amount(coin.tx_out.value))
        + serialize_script(coin.tx_out.script_pub_key.script)
    )


def serialize_metadata(magic: bytes, base_hash: bytes, coins_count: int) -> bytes:
    """Return `SnapshotMetadata`, `base_hash` as a block hash is displayed."""
    return (
        _MAGIC
        + _VERSION.to_bytes(2, "little")
        + magic
        + base_hash[::-1]
        + coins_count.to_bytes(8, "little")
    )


def serialize_tx_coins(txid: bytes, coins: Iterable[tuple[int, Coin]]) -> bytes:
    """Return one transaction's coins, `txid` as the coins key holds it."""
    rows = [var_int.serialize(n) + serialize_coin(coin) for n, coin in coins]
    return txid + var_int.serialize(len(rows)) + b"".join(rows)
