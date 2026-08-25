# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Replay every saved script-verification failure under `errors/`, by hand.

Each `errors/<txid>/<i>/` directory holds one previously failing input's
flags, transaction and prevouts; this walks all of them, calls btclib's
`verify_transaction` again and prints which ones still fail. Run
directly (`python scripts/test_errors.py`) from wherever `errors/` sits,
never collected by pytest: it lives under `scripts/`, outside
`testpaths`.
"""

from pathlib import Path

from btclib.exceptions import BTClibException
from btclib.script.engine import verify_transaction
from btclib.tx.tx import Tx
from btclib.tx.tx_out import TxOut
from btclib.utils import bytesio_from_binarydata


def get_error_data(txid: str, i: str) -> tuple[list[TxOut], Tx, tuple[str, ...]]:
    """Read one saved failure's flags, transaction and prevouts."""
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
            # BTClibException, not bare Exception: this is what
            # TxOut.parse raises on a stream too short for one more
            # TxOut, confirmed directly against the installed btclib
            # rather than assumed, and the loop's only exit besides it
            except BTClibException:
                break
    return prevouts, tx, flags


for x in Path("errors").iterdir():
    txid = x.name
    for y in x.iterdir():
        vin = y.name
        print(txid, vin)
        try:
            verify_transaction(*get_error_data(txid, vin))
        # deliberately blind: a driver over a whole directory of local
        # error fixtures of unknown shape, meant to keep going and
        # report the next one whatever kind of failure the last one was
        except Exception:  # noqa: BLE001
            print("error")
