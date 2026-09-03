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
from btclib.consensus import CONSENSUS_PARAMS
from btclib.ecc import dsa, ssa
from btclib.exceptions import BTClibValueError, ScriptError
from btclib.hashes import hash160, sha256
from btclib.script import script, sig_hash, taproot
from btclib.script.engine import verify_input as btclib_verify_input
from btclib.script.engine import verify_transaction
from btclib.script.engine.flags import ALL_FLAGS, NO_FLAGS, ScriptFlag
from btclib.script.script_pub_key import ScriptPubKey
from btclib.script.taproot import output_prvkey
from btclib.script.witness import Witness
from btclib.to_pub_key import point_from_pub_key, pub_keyinfo_from_prv_key
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node.block_db import Coin
from btclib_node.chains import RegTest
from btclib_node.exceptions import NonStandardTxError
from btclib_node.interpreter import (
    STANDARD_FLAGS,
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


def coins(tx_outs: list[TxOut], *, is_coinbase: bool = False) -> list[Coin]:
    """Wrap plain prevouts as `Coin`s, the shape `check_transactions` takes."""
    return [Coin(tx_out, height=1, is_coinbase=is_coinbase) for tx_out in tx_outs]


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


# an ordinary hash: RegTest's own `script_flag_exceptions` is empty, so
# no by-hash lookup below ever matches it, and every test using it is
# about the height-gated flags rather than about the exception table,
# which is btclib's own to test
_A_BLOCK_HASH = bytes(32)


def test_nothing_to_check_is_not_an_error() -> None:
    """An empty transaction list returns without touching the pool."""
    check_transactions([], 1, make_node(), _A_BLOCK_HASH)


def test_a_prevout_count_that_does_not_match_the_inputs_is_refused() -> None:
    """More inputs than prevouts raises `PrevoutCountMismatchError`."""
    # one input, no prevout for it: the caller built the pair wrong, and
    # verifying nothing would look like verifying everything
    with pytest.raises(ValueError, match="prevout count does not match input count"):
        check_transactions([([], spend(b""))], 1, make_node(), _A_BLOCK_HASH)


def test_a_transaction_that_prints_money_is_refused() -> None:
    """An output worth more than its prevout raises before script checks run."""
    tx = spend(script.serialize([b"\x11" * 32]), value=51 * 10**8)
    with pytest.raises(BTClibValueError, match="Invalid transaction amounts"):
        check_transactions([(coins([prevout()]), tx)], 1, make_node(), _A_BLOCK_HASH)


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
    check_transaction(prevouts, tx)
    assert tx.serialize(include_witness=True) == before


def test_get_flags_is_the_chains_consensus_row_asked_by_height_and_hash() -> None:
    """`get_flags` is `chain.consensus.script_flags_at`, both args forwarded.

    Which flags bind at a height, and which handful of blocks are
    exempted by hash, is `btclib.consensus.ConsensusParams.script_flags_at`'s
    own rule, tested there; what this call site owes is that `index` and
    `block_hash` reach it unchanged, `block_hash` included where it is
    left at its default. Asserted against a second, direct call to the
    same method rather than against a literal answer, so this stays a
    test of the delegation and not a second copy of btclib's own table.
    """
    config = SimpleNamespace(chain=RegTest())
    consensus = RegTest().consensus
    assert get_flags(cast("Config", config), 10) == consensus.script_flags_at(10)
    block_hash = b"\x11" * 32
    assert get_flags(cast("Config", config), 10, block_hash) == (
        consensus.script_flags_at(10, block_hash)
    )


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
    check_transactions([(coins(prevouts), tx)], 1, make_node(), _A_BLOCK_HASH)


def test_check_transactions_still_raises_when_one_input_does_not_verify() -> None:
    """One tampered signature among several inputs raises `ScriptError`."""
    # the same refusal the per-input dispatch gave: a bad input has to
    # reach main.update_chain whichever input in the transaction it is
    prevouts, tx = _multi_input_p2wpkh_spend(3)
    sig, pub = tx.vin[2].script_witness.stack
    tx.vin[2].script_witness = Witness([bytes([sig[0] ^ 1]) + sig[1:], pub])
    with pytest.raises(ScriptError):
        check_transactions([(coins(prevouts), tx)], 1, make_node(), _A_BLOCK_HASH)


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
        (coins(prevouts), tx)
        for prevouts, tx in (
            _multi_input_p2wpkh_spend(1),
            _multi_input_p2wpkh_spend(7),
            _multi_input_p2wpkh_spend(20),
        )
    ]
    count = _count_transaction_wide_serializations(monkeypatch)
    check_transactions(transaction_data, 1, make_node(), _A_BLOCK_HASH)
    # three transactions, three serializers each called once per
    # transaction by PrecomputedTxData.__init__ -- not once per input,
    # whichever of the 1, 7 or 20 inputs each transaction carries
    assert count() == len(transaction_data) * 3


# `verify_mempool_acceptance` checks a candidate against STANDARD_FLAGS
# and never against `get_flags`, so the reason it may skip Core's own
# `ConsensusScriptChecks` is that the first set contains the second.
# Asserted over every network btclib's table names rather than over
# RegTest alone: the exception rows are mainnet's and testnet3's, and a
# row whose exception named a standardness flag would break the claim
# without RegTest noticing.
_GATED_HEIGHT_FIELDS = ("bip65_height", "bip66_height", "csv_height", "segwit_height")


def _every_consensus_set() -> list[ScriptFlag]:
    """Return every answer `script_flags_at` gives on any network."""
    sets = []
    for params in CONSENSUS_PARAMS.values():
        heights = {0, 1, 2**31}
        for field in _GATED_HEIGHT_FIELDS:
            height = getattr(params, field)
            heights |= {max(height - 1, 0), height, height + 1}
        hashes: list[bytes | None] = [None]
        hashes += [h for h, _ in params.script_flag_exceptions]
        sets += [
            params.script_flags_at(height, block_hash)
            for height in heights
            for block_hash in hashes
        ]
    return sets


def test_the_standard_set_contains_every_consensus_set() -> None:
    """No `script_flags_at` answer holds a rule `STANDARD_FLAGS` omits."""
    consensus_sets = _every_consensus_set()
    # the control on the comparison below, which would answer the same
    # for a subset test that cannot fail: SIGPUSHONLY is a member
    # STANDARD_FLAGS does not carry, so the same expression answers no
    assert ScriptFlag.SIGPUSHONLY & STANDARD_FLAGS == NO_FLAGS
    for flags in consensus_sets:
        assert flags & STANDARD_FLAGS == flags


def test_the_full_consensus_set_contains_every_height_gated_one() -> None:
    """No `script_flags_at` answer holds a rule `ALL_FLAGS` omits.

    What `interpreter._consensus_accepts` rests on: it classifies a
    refused candidate against `ALL_FLAGS` and never against a height,
    so a transaction it takes has to be one a block at any height
    carries, or a peer is discouraged for relaying a transaction some
    chain would have held.
    """
    # the same control the test above uses, and it decides the same
    # question: a subset test that cannot fail would answer alike
    assert ScriptFlag.SIGPUSHONLY & ALL_FLAGS == NO_FLAGS
    for flags in _every_consensus_set():
        assert flags & ALL_FLAGS == flags


def test_every_script_flag_is_decided_one_way_or_the_other() -> None:
    """`SIGPUSHONLY` is the one `ScriptFlag` member `STANDARD_FLAGS` omits.

    Core's `STANDARD_SCRIPT_VERIFY_FLAGS` omits it too
    (`src/policy/policy.h:118-131`, at bitcoin/bitcoin@9be056a8a7). What
    this pins is that a member btclib adds later cannot be left out of
    the set in silence: it is either relayed against or named here.
    """
    omitted = [x for x in ScriptFlag if x & STANDARD_FLAGS == NO_FLAGS]
    assert omitted == [ScriptFlag.SIGPUSHONLY]


def _spend(script_pub_key: bytes, script_sig: bytes = b"") -> tuple[list[TxOut], Tx]:
    """Return a prevout of that shape and the transaction spending it.

    `_spend_of` above with a script_sig filled in: the spends below turn
    on the script the prevout carries and on what is offered to it,
    where the signed spends that helper serves turn on the preimage.
    """
    prevouts, tx = _spend_of(script_pub_key)
    tx.vin[0].script_sig = script_sig
    return prevouts, tx


def _taproot_spend(
    leaf_version: int, leaf_script: bytes, stack: list[bytes]
) -> tuple[list[TxOut], Tx]:
    """Return a p2tr prevout and the script-path spend of its one leaf."""
    merkle_root = taproot.leaf_hash(leaf_version, leaf_script)
    output_key, parity = taproot.output_pubkey_from_merkle_root(_PUB[1:33], merkle_root)
    prevouts, tx = _spend(script.serialize(["OP_1", output_key.hex()]))
    # BIP341's control block for a tree of one leaf: the parity bit and
    # the leaf version, the x-only internal key, and an empty path
    control = bytes([parity + leaf_version]) + _PUB[1:33]
    tx.vin[0].script_witness = Witness([*stack, leaf_script, control])
    return prevouts, tx


def a_non_canonical_sighash_byte() -> tuple[list[TxOut], Tx]:
    """Return a p2pkh spend whose sighash byte is not a defined type."""
    hash_type = 0x05
    prevouts, tx = _spend(ScriptPubKey.p2pkh(_PUB).script)
    signature = dsa.sign_(sig_hash.from_tx(prevouts, tx, 0, hash_type), _PRV)
    tx.vin[0].script_sig = script.serialize(
        [signature.serialize().hex() + f"{hash_type:02x}", _PUB.hex()]
    )
    return prevouts, tx


def a_high_s_signature() -> tuple[list[TxOut], Tx]:
    """Return a p2pkh spend whose signature carries the negated s."""
    prevouts, tx = _spend(ScriptPubKey.p2pkh(_PUB).script)
    signature = dsa.sign_(sig_hash.from_tx(prevouts, tx, 0, 1), _PRV)
    high_s = dsa.Sig(signature.r, signature.ec.n - signature.s)
    tx.vin[0].script_sig = script.serialize(
        [high_s.serialize().hex() + "01", _PUB.hex()]
    )
    return prevouts, tx


def a_non_minimal_push() -> tuple[list[TxOut], Tx]:
    """Return a spend whose script_sig pushes one octet through OP_PUSHDATA1."""
    # 4c 01 01: OP_PUSHDATA1, one byte of data, the byte 0x01 -- where
    # OP_1 is the push MINIMALDATA asks for
    return _spend(script.serialize(["OP_DROP", "OP_1"]), script_sig=b"\x4c\x01\x01")


def an_upgradable_nop() -> tuple[list[TxOut], Tx]:
    """Return a spend of a script_pub_key executing a reserved NOP."""
    return _spend(script.serialize(["OP_NOP1", "OP_1"]))


def a_leftover_stack_element() -> tuple[list[TxOut], Tx]:
    """Return a spend leaving the script_sig's own push under its result."""
    return _spend(script.serialize(["OP_1"]), script_sig=script.serialize(["OP_1"]))


def a_non_minimal_if_condition() -> tuple[list[TxOut], Tx]:
    """Return a p2wsh spend whose OP_IF condition is neither empty nor `01`."""
    witness_script = script.serialize(["OP_IF", "OP_1", "OP_ELSE", "OP_1", "OP_ENDIF"])
    prevouts, tx = _spend(script.serialize(["OP_0", sha256(witness_script).hex()]))
    tx.vin[0].script_witness = Witness([b"\x02", witness_script])
    return prevouts, tx


def a_failed_check_with_a_signature_on_it() -> tuple[list[TxOut], Tx]:
    """Return a spend whose OP_CHECKSIG fails on a non-empty signature."""
    prevouts, tx = _spend(script.serialize([_PUB.hex(), "OP_CHECKSIG", "OP_NOT"]))
    # well-formed and over another message entirely, so the check fails
    # rather than the encoding rules refusing it first
    signature = dsa.sign_(b"\x33" * 32, _PRV)
    tx.vin[0].script_sig = script.serialize([signature.serialize().hex() + "01"])
    return prevouts, tx


def an_uncompressed_key_in_a_witness_script() -> tuple[list[TxOut], Tx]:
    """Return a p2wpkh spend whose public key is the uncompressed form."""
    uncompressed = pub_keyinfo_from_prv_key(_PRV, compressed=False)[0]
    prevouts, tx = _spend(
        script.serialize(["OP_0", hash160(uncompressed).hex()]),
    )
    signature = dsa.sign_(sig_hash.from_tx(prevouts, tx, 0, 1), _PRV)
    tx.vin[0].script_witness = Witness([signature.serialize() + b"\x01", uncompressed])
    return prevouts, tx


def a_signature_check_in_the_script_sig() -> tuple[list[TxOut], Tx]:
    """Return a spend whose script_sig carries an OP_CHECKSIG of its own."""
    return _spend(
        script.serialize(["OP_NOT"]),
        script_sig=script.serialize(["OP_0", "OP_0", "OP_CHECKSIG"]),
    )


def an_upgradable_witness_program() -> tuple[list[TxOut], Tx]:
    """Return a spend of a witness program whose version no BIP defines."""
    prevouts, tx = _spend(script.serialize(["OP_2", (b"\x44" * 32).hex()]))
    tx.vin[0].script_witness = Witness([b"\x01"])
    return prevouts, tx


def an_upgradable_taproot_leaf_version() -> tuple[list[TxOut], Tx]:
    """Return a taproot script path whose leaf version is not 0xc0."""
    return _taproot_spend(0xC2, script.serialize(["OP_1"]), [])


def an_op_success_op_code() -> tuple[list[TxOut], Tx]:
    """Return a tapscript spend holding an op code BIP342 reserved."""
    # 0x50 is OP_SUCCESS80, and a tapscript holding one succeeds whole
    return _taproot_spend(0xC0, b"\x50", [])


def an_upgradable_taproot_public_key_type() -> tuple[list[TxOut], Tx]:
    """Return a tapscript spend checking a key length BIP342 left open."""
    leaf_script = script.serialize([(b"\x33" * 33).hex(), "OP_CHECKSIG"])
    # not empty, so the check leaves a true on the stack rather than a false
    return _taproot_spend(0xC0, leaf_script, [b"\x01" * 64])


# One spend per rule `STANDARD_FLAGS` adds to the consensus set, each
# breaking that rule and no other: a block carrying it connects, and
# `check_transaction` refuses to hold it.
_NON_STANDARD = (
    ("STRICTENC", a_non_canonical_sighash_byte),
    ("LOW_S", a_high_s_signature),
    ("MINIMALDATA", a_non_minimal_push),
    ("DISCOURAGE_UPGRADABLE_NOPS", an_upgradable_nop),
    ("CLEANSTACK", a_leftover_stack_element),
    ("MINIMALIF", a_non_minimal_if_condition),
    ("NULLFAIL", a_failed_check_with_a_signature_on_it),
    ("WITNESS_PUBKEYTYPE", an_uncompressed_key_in_a_witness_script),
    ("CONST_SCRIPTCODE", a_signature_check_in_the_script_sig),
    (
        "DISCOURAGE_UPGRADABLE_WITNESS_PROGRAM",
        an_upgradable_witness_program,
    ),
    ("DISCOURAGE_UPGRADABLE_TAPROOT_VERSION", an_upgradable_taproot_leaf_version),
    ("DISCOURAGE_OP_SUCCESS", an_op_success_op_code),
    ("DISCOURAGE_UPGRADABLE_PUBKEYTYPE", an_upgradable_taproot_public_key_type),
)


@pytest.mark.parametrize(
    "build", [build for _, build in _NON_STANDARD], ids=[n for n, _ in _NON_STANDARD]
)
def test_a_spend_a_block_would_carry_is_refused_from_the_mempool(
    build: Callable[[], tuple[list[TxOut], Tx]],
) -> None:
    """Each standardness rule refuses a spend the consensus rules accept."""
    prevouts, tx = build()
    # the control, and what makes the refusal below a standardness one:
    # the flags a block connecting at this height is checked with take
    # the very same transaction
    verify_transaction(prevouts, tx, get_flags(cast("Config", make_node().config), 1))
    # NonStandardTxError and not the bare BTClibValueError the engine
    # raises: `p2p.callbacks.tx` catches this class alone, and a refusal
    # arriving as anything else discourages the peer that relayed the
    # transaction
    with pytest.raises(NonStandardTxError):
        check_transaction(prevouts, tx)


def test_a_spend_no_block_could_carry_is_refused_as_itself() -> None:
    """A consensus refusal reaches the caller as the engine raised it.

    `OP_0` alone leaves a false on the stack, which no flag set forgives,
    so the classification `check_transaction` makes has to answer the
    other way: `NonStandardTxError` here would tell `p2p.callbacks.tx`
    to keep a peer that relayed a transaction no chain will ever hold.
    """
    prevouts, tx = _spend(script.serialize(["OP_0"]))
    # the control, mirroring the one above: these are the flags a block
    # connecting at this height is checked with, and they refuse it too
    with pytest.raises(BTClibValueError):
        verify_transaction(
            prevouts, tx, get_flags(cast("Config", make_node().config), 1)
        )
    with pytest.raises(BTClibValueError) as refusal:
        check_transaction(prevouts, tx)
    assert not isinstance(refusal.value, NonStandardTxError)
