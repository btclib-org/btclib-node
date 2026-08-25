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
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.ecc import dsa, ssa
from btclib.exceptions import BTClibValueError, ScriptError
from btclib.script import script, sig_hash
from btclib.script.engine import verify_input as btclib_verify_input
from btclib.script.script_pub_key import ScriptPubKey
from btclib.script.taproot import output_prvkey
from btclib.script.witness import Witness
from btclib.to_pub_key import point_from_pub_key, pub_keyinfo_from_prv_key
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node.chains import RegTest
from btclib_node.interpreter import (
    check_transaction,
    check_transactions,
    f,
    get_flags,
    warm,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib.alias import Octets

    from btclib_node.config import Config


def spend(script_sig: bytes, value: int = 49 * 10**8) -> Tx:
    """Return a one-input, one-output transaction spending `prevout`."""
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


def prevout(script_pub_key: bytes | None = None, value: int = 50 * 10**8) -> TxOut:
    """Return the prevout `spend`'s default transaction is built to spend."""
    return TxOut(
        value=value,
        script_pub_key=script_pub_key or script.serialize([b"\x33" * 32]),
    )


def _precomputed_for(prevouts: list[TxOut], tx: Tx) -> sig_hash.PrecomputedTxData:
    """Return the `PrecomputedTxData` `f` needs, for `tx` and `prevouts`."""
    return sig_hash.PrecomputedTxData(tx, prevouts)


def test_an_input_that_verifies_returns_quietly() -> None:
    """`f` returns `None` and raises nothing for an input that verifies."""
    prevouts, tx = [prevout()], spend(script.serialize([b"\x11" * 32]))
    f(prevouts, tx, 0, (), _precomputed_for(prevouts, tx))


def test_an_input_that_does_not_verify_raises() -> None:
    """`f` raises `ScriptError` for an input that does not verify."""
    # the property update_chain rests on: this used to be written to
    # errors/ and swallowed, inside a worker pool, so the block was
    # connected anyway
    prevouts, tx = [prevout(script.serialize(["OP_RETURN"]))], spend(b"")
    with pytest.raises(ScriptError):
        f(prevouts, tx, 0, (), _precomputed_for(prevouts, tx))


def test_warm_does_nothing() -> None:
    """Calling `warm` directly raises nothing, imports included."""
    # dispatched through Pool.starmap in a worker process, so what
    # matters is only that a worker importing this module to run it
    # does not itself raise; called directly here for the same reason
    # f is above
    warm()


def make_node() -> Any:
    """Return a `Node` stand-in whose `worker_pool.starmap` runs in-process.

    Running them synchronously rather than through a real `Pool` is
    what lets `check_transactions`'s raise reach the caller directly,
    and what lets `monkeypatch` see calls a real process pool would
    hide inside a worker.
    """
    return SimpleNamespace(
        config=SimpleNamespace(chain=RegTest()),
        worker_pool=SimpleNamespace(starmap=lambda fn, args: [fn(*a) for a in args]),
    )


def test_nothing_to_check_is_not_an_error() -> None:
    """An empty transaction list returns without touching the pool."""
    check_transactions([], 1, make_node())


def test_a_prevout_count_that_does_not_match_the_inputs_is_refused() -> None:
    """More inputs than prevouts raises `PrevoutCountMismatchError`."""
    # one input, no prevout for it: the caller built the pair wrong, and
    # verifying nothing would look like verifying everything
    with pytest.raises(ValueError, match="prevout count does not match input count"):
        check_transactions([([], spend(b""))], 1, make_node())


def test_a_transaction_that_prints_money_is_refused() -> None:
    """An output worth more than its prevout raises before script checks run."""
    tx = spend(script.serialize([b"\x11" * 32]), value=51 * 10**8)
    with pytest.raises(BTClibValueError, match="Invalid transaction amounts"):
        check_transactions([([prevout()], tx)], 1, make_node())


_PRV = 0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF
_PUB = pub_keyinfo_from_prv_key(_PRV)[0]

# ALL, NONE, SINGLE and each of them with ANYONECANPAY: the branches the
# legacy preimage blanks the transaction differently for, and the ones
# BIP143 and BIP341 carry over. DEFAULT is taproot's alone, and is the
# empty suffix rather than an octet
_HASH_TYPES = (1, 2, 3, 0x81, 0x82, 0x83)
_TAPROOT_HASH_TYPES = (0, *_HASH_TYPES)


def _spend_of(script_pub_key: ScriptPubKey | Octets) -> tuple[list[TxOut], Tx]:
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


def a_p2pkh_spend(hash_type: int) -> tuple[list[TxOut], Tx]:
    """Return a p2pkh prevout and the legacy-preimage spend of it."""
    prevouts, tx = _spend_of(ScriptPubKey.p2pkh(_PUB))
    signature = dsa.sign_(sig_hash.from_tx(prevouts, tx, 0, hash_type), _PRV)
    tx.vin[0].script_sig = script.serialize(
        [signature.serialize().hex() + f"{hash_type:02x}", _PUB.hex()]
    )
    return prevouts, tx


def a_p2wpkh_spend(hash_type: int) -> tuple[list[TxOut], Tx]:
    """Return a p2wpkh prevout and the BIP143-preimage spend of it."""
    prevouts, tx = _spend_of(ScriptPubKey.p2wpkh(_PUB))
    signature = dsa.sign_(sig_hash.from_tx(prevouts, tx, 0, hash_type), _PRV)
    tx.vin[0].script_witness = Witness(
        [signature.serialize() + bytes([hash_type]), _PUB]
    )
    return prevouts, tx


def a_p2tr_spend(hash_type: int) -> tuple[list[TxOut], Tx]:
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
def test_the_transaction_checked_is_left_as_it_was(
    build: Callable[[int], tuple[list[TxOut], Tx]], hash_type: int
) -> None:
    """`check_transaction` leaves a signed transaction byte-identical."""
    # what the deepcopy here used to be for, and it was for a defect
    # btclib does not have: sig_hash builds the blanked transaction each
    # preimage commits to rather than editing the one it is handed.
    # Signed inputs, because an unsigned one reaches no preimage at all
    # and would say nothing -- and one of every preimage, because the
    # blanking differs between them and between hash types.
    prevouts, tx = build(hash_type)
    before = tx.serialize(include_witness=True)
    check_transaction(prevouts, tx, 1, make_node())
    assert tx.serialize(include_witness=True) == before


def test_the_flags_are_the_forks_active_at_that_height() -> None:
    """`get_flags` returns flags activated at or before the given height."""
    config = SimpleNamespace(
        chain=SimpleNamespace(flags=[(0, "P2SH"), (10, "WITNESS"), (20, "TAPROOT")])
    )
    assert get_flags(cast("Config", config), 0) == ("P2SH",)
    assert get_flags(cast("Config", config), 10) == ("P2SH", "WITNESS")
    assert get_flags(cast("Config", config), 25) == ("P2SH", "WITNESS", "TAPROOT")


def _multi_input_p2wpkh_spend(n: int) -> tuple[list[TxOut], Tx]:
    """N independent p2wpkh prevouts, and the transaction spending them."""
    prevouts = [TxOut(50 * 10**8, ScriptPubKey.p2wpkh(_PUB)) for _ in range(n)]
    tx = Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(bytes([j]) * 32, 0),
                script_sig=b"",
                sequence=0xFFFFFFFF,
            )
            for j in range(n)
        ],
        vout=[TxOut(49 * 10**8 * n, ScriptPubKey.p2wpkh(_PUB))],
    )
    for i in range(n):
        signature = dsa.sign_(sig_hash.from_tx(prevouts, tx, i, 1), _PRV)
        tx.vin[i].script_witness = Witness([signature.serialize() + b"\x01", _PUB])
    return prevouts, tx


def test_check_transactions_verifies_every_input_of_a_multi_input_transaction() -> None:
    """`check_transactions` raises nothing when every input verifies."""
    prevouts, tx = _multi_input_p2wpkh_spend(3)
    check_transactions([(prevouts, tx)], 1, make_node())


def test_check_transactions_still_raises_when_one_input_does_not_verify() -> None:
    """One tampered signature among several inputs raises `ScriptError`."""
    # the same refusal the per-input dispatch gave: a bad input has to
    # reach main.update_chain whichever input in the transaction it is
    prevouts, tx = _multi_input_p2wpkh_spend(3)
    sig, pub = tx.vin[2].script_witness.stack
    tx.vin[2].script_witness = Witness([bytes([sig[0] ^ 1]) + sig[1:], pub])
    with pytest.raises(ScriptError):
        check_transactions([(prevouts, tx)], 1, make_node())


def _count_transaction_wide_serializations(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], int]:
    """Wrap sig_hash's three transaction-wide serializers and return a counter.

    Not `PrecomputedTxData` construction itself: a per-input dispatch
    with no `precomputed` never constructs one at all for a segwit v0
    spend (`segwit_v0` calls these three functions directly in that
    case), so that counter would answer zero under the very dispatch
    this measures against and the zero would look like the fix rather
    than like a counter measuring nothing. These three are called
    exactly once each by `PrecomputedTxData.__init__` and are the ones
    `segwit_v0` falls back to per input when handed no `precomputed`, so
    they see every call either dispatch makes.
    """
    calls = [0]
    for name in (
        "_serialized_prevouts",
        "_serialized_sequences",
        "_serialized_outputs",
    ):
        original = getattr(sig_hash, name)

        def counting(
            *args: object, _original: Callable[..., bytes] = original
        ) -> bytes:
            calls[0] += 1
            return _original(*args)

        monkeypatch.setattr(sig_hash, name, counting)
    return lambda: calls[0]


def test_a_positive_control_proves_the_counter_can_answer_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-input `verify_input` with no `precomputed` re-serializes 3n times."""
    # the old per-input dispatch, reproduced directly: no precomputed, so
    # every one of the n inputs re-serializes the transaction on its own.
    # Built before the count starts, so building it -- which signs every
    # input, itself calling these three functions once per input -- is
    # not what the counter below is measuring.
    n = 5
    prevouts, tx = _multi_input_p2wpkh_spend(n)
    count = _count_transaction_wide_serializations(monkeypatch)
    for i in range(n):
        # WITNESS, or verify_input treats the witness program as the
        # anyone-can-spend an unenforced BIP141 makes it and never runs
        # segwit_v0 at all -- the flags check_transactions itself passes
        btclib_verify_input(prevouts, tx, i, ("WITNESS",))
    assert count() == n * 3


def test_check_transactions_builds_the_precomputed_data_once_per_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check_transactions` re-serializes three times per tx, not per input."""
    # built before the count starts, for the same reason as the positive
    # control above: building signs every input, and that signing is not
    # what this measures
    transaction_data = [
        _multi_input_p2wpkh_spend(1),
        _multi_input_p2wpkh_spend(7),
        _multi_input_p2wpkh_spend(20),
    ]
    count = _count_transaction_wide_serializations(monkeypatch)
    check_transactions(transaction_data, 1, make_node())
    # three transactions, three serializers each called once per
    # transaction by PrecomputedTxData.__init__ -- not once per input,
    # whichever of the 1, 7 or 20 inputs each transaction carries
    assert count() == len(transaction_data) * 3
