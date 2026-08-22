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
from btclib.ecc import dsa, ssa
from btclib.exceptions import BTClibValueError, ScriptError
from btclib.script import script, sig_hash
from btclib.script.script_pub_key import ScriptPubKey
from btclib.script.taproot import output_prvkey
from btclib.script.witness import Witness
from btclib.to_pub_key import point_from_pub_key, pub_keyinfo_from_prv_key
from btclib.tx.tx import Tx, TxIn, TxOut
from btclib.tx.tx_in import OutPoint

from btclib_node.chains import RegTest
from btclib_node.interpreter import (
    check_transaction,
    check_transactions,
    f,
    get_flags,
)


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


_PRV = 0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF
_PUB = pub_keyinfo_from_prv_key(_PRV)[0]

# ALL, NONE, SINGLE and each of them with ANYONECANPAY: the branches the
# legacy preimage blanks the transaction differently for, and the ones
# BIP143 and BIP341 carry over. DEFAULT is taproot's alone, and is the
# empty suffix rather than an octet
_HASH_TYPES = (1, 2, 3, 0x81, 0x82, 0x83)
_TAPROOT_HASH_TYPES = (0, *_HASH_TYPES)


def _spend_of(script_pub_key):
    """Return a prevout of that kind and the unsigned spend of it."""
    prevouts = [TxOut(50 * 10**8, script_pub_key)]
    tx = Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(b"\x11" * 32, 0),
                script_sig=b"",
                sequence=0xFFFFFFFF,
            )
        ],
        vout=[TxOut(49 * 10**8, script_pub_key)],
    )
    return prevouts, tx


def a_p2pkh_spend(hash_type):
    """Return a p2pkh prevout and the legacy-preimage spend of it."""
    prevouts, tx = _spend_of(ScriptPubKey.p2pkh(_PUB))
    signature = dsa.sign_(sig_hash.from_tx(prevouts, tx, 0, hash_type), _PRV)
    tx.vin[0].script_sig = script.serialize(
        [signature.serialize().hex() + f"{hash_type:02x}", _PUB.hex()]
    )
    return prevouts, tx


def a_p2wpkh_spend(hash_type):
    """Return a p2wpkh prevout and the BIP143-preimage spend of it."""
    prevouts, tx = _spend_of(ScriptPubKey.p2wpkh(_PUB))
    signature = dsa.sign_(sig_hash.from_tx(prevouts, tx, 0, hash_type), _PRV)
    tx.vin[0].script_witness = Witness(
        [signature.serialize() + bytes([hash_type]), _PUB]
    )
    return prevouts, tx


def a_p2tr_spend(hash_type):
    """Return a p2tr prevout and the key-path spend of it."""
    prevouts, tx = _spend_of(ScriptPubKey.p2tr(point_from_pub_key(_PUB)))
    # BIP341's preimage reads the witness stack -- for the annex, and for
    # how many items are on it -- so there has to be one before the
    # signature that goes on it exists
    tx.vin[0].script_witness = Witness([bytes(64)])
    signature = ssa.sign_(
        sig_hash.from_tx(prevouts, tx, 0, hash_type), output_prvkey(_PRV)
    )
    # the hash type is appended, and DEFAULT is appended as nothing
    suffix = b"" if hash_type == 0 else bytes([hash_type])
    tx.vin[0].script_witness = Witness([signature.serialize() + suffix])
    return prevouts, tx


# every preimage this node's script flags reach: the legacy one, BIP143's
# and BIP341's, each over every hash type it defines
_SPENDS = [
    (f"{kind}-{hash_type:02x}", build, hash_type)
    for kind, build, hash_types in (
        ("p2pkh", a_p2pkh_spend, _HASH_TYPES),
        ("p2wpkh", a_p2wpkh_spend, _HASH_TYPES),
        ("p2tr", a_p2tr_spend, _TAPROOT_HASH_TYPES),
    )
    for hash_type in hash_types
]


@pytest.mark.parametrize(
    ("build", "hash_type"),
    [(build, hash_type) for _, build, hash_type in _SPENDS],
    ids=[name for name, _, _ in _SPENDS],
)
def test_the_transaction_checked_is_left_as_it_was(build, hash_type):
    # what the deepcopy here used to be for, and it was for a defect
    # btclib does not have: sig_hash builds the blanked transaction each
    # preimage commits to rather than editing the one it is handed.
    # Signed inputs, because an unsigned one reaches no preimage at all
    # and would say nothing -- and one of every preimage, because the
    # blanking differs between them and between hash types.
    prevouts, tx = build(hash_type)
    before = tx.serialize(True)
    check_transaction(prevouts, tx, 1, make_node())
    assert tx.serialize(True) == before


def test_the_flags_are_the_forks_active_at_that_height():
    config = SimpleNamespace(
        chain=SimpleNamespace(flags=[(0, "P2SH"), (10, "WITNESS"), (20, "TAPROOT")])
    )
    assert get_flags(config, 0) == ("P2SH",)
    assert get_flags(config, 10) == ("P2SH", "WITNESS")
    assert get_flags(config, 25) == ("P2SH", "WITNESS", "TAPROOT")
