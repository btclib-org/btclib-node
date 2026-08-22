# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What block validation refuses, and what carries the refusal out.

`f` is called through `Pool.starmap`, in a worker process, so what it
does there is invisible both to coverage and to a test that watches the
parent. It is called directly here, which is also the only way to say
plainly that it raises rather than swallowing -- the property
main.update_chain depends on to roll a block back.
"""

from types import SimpleNamespace

import pytest
from btclib.exceptions import BTClibValueError, ScriptError
from btclib.script import script
from btclib.tx.tx import Tx, TxIn, TxOut
from btclib.tx.tx_in import OutPoint

from btclib_node.chains import RegTest
from btclib_node.interpreter import check_transactions, f, get_flags


def spend(script_sig, value=49 * 10**8):
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(b"\x11" * 32, 0),
                script_sig=script_sig,
                sequence=0xFFFFFFFF,
            )
        ],
        vout=[TxOut(value=value, script_pub_key=script.serialize([b"\x22" * 32]))],
    )


def prevout(script_pub_key=None, value=50 * 10**8):
    return TxOut(
        value=value,
        script_pub_key=script_pub_key or script.serialize([b"\x33" * 32]),
    )


def test_an_input_that_verifies_returns_quietly():
    assert f([prevout()], spend(script.serialize([b"\x11" * 32])), 0, ()) is None


def test_an_input_that_does_not_verify_raises():
    # the property update_chain rests on: this used to be written to
    # errors/ and swallowed, inside a worker pool, so the block was
    # connected anyway
    with pytest.raises(ScriptError):
        f([prevout(script.serialize(["OP_RETURN"]))], spend(b""), 0, ())


def make_node():
    return SimpleNamespace(
        config=SimpleNamespace(chain=RegTest()),
        worker_pool=SimpleNamespace(starmap=lambda fn, args: [fn(*a) for a in args]),
    )


def test_nothing_to_check_is_not_an_error():
    assert check_transactions([], 1, make_node()) is None


def test_a_prevout_count_that_does_not_match_the_inputs_is_refused():
    # one input, no prevout for it: the caller built the pair wrong, and
    # verifying nothing would look like verifying everything
    with pytest.raises(ValueError):
        check_transactions([[[], spend(b"")]], 1, make_node())


def test_a_transaction_that_prints_money_is_refused():
    tx = spend(script.serialize([b"\x11" * 32]), value=51 * 10**8)
    with pytest.raises(BTClibValueError, match="Invalid transaction amounts"):
        check_transactions([[[prevout()], tx]], 1, make_node())


def test_the_flags_are_the_forks_active_at_that_height():
    config = SimpleNamespace(
        chain=SimpleNamespace(flags=[(0, "P2SH"), (10, "WITNESS"), (20, "TAPROOT")])
    )
    assert get_flags(config, 0) == ("P2SH",)
    assert get_flags(config, 10) == ("P2SH", "WITNESS")
    assert get_flags(config, 25) == ("P2SH", "WITNESS", "TAPROOT")
