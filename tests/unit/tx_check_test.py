# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The fallbacks of `tx_check`'s translation no parsed transaction reaches.

The words themselves are tested through their callers:
`tests/unit/rpc/callbacks_test.py` for a lone transaction,
`tests/unit/mining_test.py` for a block.
"""

import pytest
from btclib.exceptions import BTClibValueError
from btclib.script.script_pub_key import ScriptPubKey
from btclib.tx import OutPoint, Tx, TxIn, TxOut

from btclib_node.tx_check import _amount_reject_reason, _reject_reason


def a_tx() -> Tx:
    """Build a transaction `CheckTransaction` accepts."""
    return Tx(
        version=2,
        lock_time=0,
        vin=[TxIn(OutPoint(b"\x11" * 32, 0))],
        vout=[TxOut(1, ScriptPubKey(b"\x51"))],
    )


def test_an_unrecognized_assert_valid_message_is_not_swallowed() -> None:
    """`_reject_reason` re-raises what it does not recognize.

    Defensive: every message `Tx.assert_valid` can actually raise for a
    parsed `Tx` is named, so this exercises the fallback with a message
    that is not one of them, standing in for a rule this mapping has
    not been taught yet.
    """
    tx = a_tx()
    error = BTClibValueError("a rule this mapping does not know about")
    with pytest.raises(BTClibValueError):
        _reject_reason(tx, error)


def test_amount_reject_reason_answers_none_when_nothing_violates() -> None:
    """`_amount_reject_reason` falls through its own loop to `None`.

    Defensive, the same reason as the test above: `Tx.assert_valid` only
    ever raises this message when some output really is out of range,
    so this reaches the loop's fall-through directly rather than through
    a real refusal that could not carry it.
    """
    tx = a_tx()
    assert _amount_reject_reason(tx, "invalid satoshi amount: 5") is None
