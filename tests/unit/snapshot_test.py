# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The snapshot file's pieces, against what `bitcoind` v31.1.0 writes.

`tests/integration/dumptxoutset_test.py` holds a whole file to bitcoind's
own. The compression is `btclib.compressor`'s, and its tests.
"""

from btclib.tx.tx_out import TxOut

from btclib_node.block_db import Coin
from btclib_node.snapshot import (
    serialize_coin,
    serialize_metadata,
    serialize_tx_coins,
)

KEY = bytes.fromhex(
    "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
)


def test_a_coin_is_height_and_coinbase_then_amount_then_script() -> None:
    """A coinbase of height 2 paying a key: `05 32` and the key."""
    out = TxOut(5_000_000_000, b"\x21" + KEY + b"\xac")
    coin = Coin(out, 2, is_coinbase=True)
    assert serialize_coin(coin).hex() == "0532" + KEY.hex()
    assert serialize_coin(Coin(out, 2, is_coinbase=False))[0] == 0x04


def test_a_transaction_is_its_txid_and_each_coin_by_index() -> None:
    """A compact size of the count, then each index and coin."""
    out = TxOut(1, b"\x51")
    coin = Coin(out, 1, is_coinbase=False)
    txid = bytes(range(32))
    assert serialize_tx_coins(txid, [(0, coin), (300, coin)]) == (
        txid
        + b"\x02"
        + b"\x00"
        + serialize_coin(coin)
        + b"\xfd\x2c\x01"
        + serialize_coin(coin)
    )


def test_the_metadata_is_magic_version_network_hash_and_count() -> None:
    """The hash is written in the order a transaction id is, not displayed."""
    base = bytes(range(32))
    assert serialize_metadata(b"\xfa\xbf\xb5\xda", base, 6).hex() == (
        "7574786fff0200fabfb5da" + base[::-1].hex() + "0600000000000000"
    )
