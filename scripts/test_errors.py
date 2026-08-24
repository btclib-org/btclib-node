# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from pathlib import Path

from btclib.script.engine import verify_transaction
from btclib.tx.tx import Tx
from btclib.tx.tx_out import TxOut
from btclib.utils import bytesio_from_binarydata


def get_error_data(txid: str, i: str) -> tuple[list[TxOut], Tx, tuple[str, ...]]:
    err_dir = Path("errors", txid, str(i))
    with Path(err_dir / "flags").open(encoding="utf-8") as f:
        flags = tuple(
            f.read().replace("'", "").replace("(", "").replace(")", "").split(", ")
        )
    with Path(err_dir / "tx").open("rb") as f:
        tx = Tx.parse(f.read())
    with Path(err_dir / "prevouts").open("rb") as f:
        s = bytesio_from_binarydata(f.read())
        prevouts = []
        while True:
            try:
                prevouts.append(TxOut.parse(s))
            except Exception:
                break
    return prevouts, tx, flags


for x in Path("errors").iterdir():
    txid = x.name
    for y in x.iterdir():
        vin = y.name
        print(txid, vin)
        try:
            verify_transaction(*get_error_data(txid, vin))
        except Exception:
            print("error")
