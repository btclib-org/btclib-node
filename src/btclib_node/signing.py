# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Signing a transaction with the keys a caller hands over, as Core does.

Core's `src/script/sign.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag: `SignTransaction`, `ProduceSignature` with its `SignStep`,
`DataFromTransaction` and `SignatureData::MergeSignatureData`, for what a
`FlatSigningProvider` can do. That provider holds private keys and the
scripts a caller names, with no key origin, taproot spend data or MuSig2,
so a taproot output is signed only where a key is its output key itself.

Signatures are made and checked with `btclib_ecc`, the signature hashes are
`btclib.script.sig_hash`'s, a script is verified and evaluated by
`btclib.script.engine` and a witness script that is a miniscript is satisfied by
`btclib_wallet.descriptors.miniscript`. What is Core's here is the order
of the steps, which decides what a partly signed input holds and so what
`combinerawtransaction` can merge, and the words of the refusals.

`signrawtransactionwithkey` and `combinerawtransaction` run on `Node`'s
thread, and a transaction of many inputs holds it while its scripts run.
"""

from copy import copy
from itertools import chain
from typing import TYPE_CHECKING

from btclib.exceptions import BTClibException, ScriptError, ScriptErrorCode
from btclib.hashes import hash160, ripemd160
from btclib.script import sig_hash
from btclib.script.engine import verify_input
from btclib.script.engine.flags import ScriptFlag
from btclib.script.engine.script import eval_script
from btclib.script.script import serialize
from btclib.script.witness import Witness
from btclib.tx.tx_out import TxOut
from btclib_ecc.curves import bytes_from_prv_key_int
from btclib_ecc.ecc import dsa, ssa
from btclib_wallet.descriptors.miniscript import SpendContext, from_script

from btclib_node.interpreter import STANDARD_FLAGS
from btclib_node.rpc.solver import solver

if TYPE_CHECKING:
    from collections.abc import Sequence

    from btclib.alias import Octets
    from btclib.tx.tx import Tx

__all__ = [
    "MAX_MONEY",
    "MISSING_AMOUNT",
    "SIGHASH_DEFAULT",
    "InputSigner",
    "KeyStore",
    "SignatureData",
    "combine_input",
    "data_from_transaction",
    "produce_signature",
    "sign_transaction",
]

SIGHASH_DEFAULT = 0
_SIGHASH_ALL = 1
_SIGHASH_SINGLE = 3
_SIGHASH_ANYONECANPAY = 0x80

# `MAX_MONEY` (`src/consensus/amount.h`): the amount of a previous output
# whose amount the caller did not give
MAX_MONEY = 21_000_000 * 100_000_000

# what an input signed with that amount is refused with: its segwit
# signature commits to an amount that is not the output's
MISSING_AMOUNT = "Missing amount"

_BASE = "base"
_WITNESS_V0 = "witness_v0"

_OP_1NEGATE = 0x4F
_OP_1 = 0x51
_OP_16 = 0x60
# the number a one-byte push of 0x81 is
_MINUS_ONE = 0x81


class SignatureData:
    """What is known of an input's solution, as Core's `SignatureData` has it.

    `signatures` is keyed by the id of the public key, the hash160 of it,
    and holds the key and the signature, as Core's `SigPair` does. These
    are plain classes: Sphinx cannot read the `<factory>` default a
    dataclass signature shows, and splits the annotation at its comma.
    """

    def __init__(
        self, script_sig: bytes = b"", script_witness: Sequence[bytes] = ()
    ) -> None:
        """Hold the script_sig and witness the input has, and nothing else."""
        self.complete = False
        self.witness = False
        self.script_sig = script_sig
        self.script_witness = list(script_witness)
        self.redeem_script = b""
        self.witness_script = b""
        self.signatures: dict[bytes, tuple[bytes, bytes]] = {}


class KeyStore:
    """A `FlatSigningProvider`: private keys and scripts, found by hash."""

    def __init__(self) -> None:
        """Hold no key and no script."""
        # key id -> (private key, whether its public key is compressed)
        self.keys: dict[bytes, tuple[int, bool]] = {}
        self.pubkeys: dict[bytes, bytes] = {}
        self.scripts: dict[bytes, bytes] = {}

    def add_key(self, secret: int, *, compressed: bool) -> None:
        """Add a key under the id of the public key it has."""
        pubkey = bytes_from_prv_key_int(secret, compressed=compressed)
        key_id = hash160(pubkey)
        self.pubkeys[key_id] = pubkey
        self.keys[key_id] = (secret, compressed)

    def add_script(self, script: bytes) -> None:
        """Add a script under its hash160, Core's `CScriptID`."""
        self.scripts[hash160(script)] = script


def _push_all(values: Sequence[bytes]) -> bytes:
    """Return `PushAll`: one-byte numbers as op codes, the rest as pushes."""
    out = bytearray()
    for value in values:
        size = len(value)
        if size == 0:
            out.append(0)
        elif size == 1 and 1 <= value[0] <= _OP_16 - _OP_1 + 1:
            out.append(_OP_1 + value[0] - 1)
        elif size == 1 and value[0] == _MINUS_ONE:
            out.append(_OP_1NEGATE)
        else:
            # `result << v`: the shortest push operator, PUSHDATA4 included
            out += serialize([value])
    return bytes(out)


def _unsignable(tx: Tx) -> Tx:
    """Return `tx` with the lock time changed, which fails every signature.

    A signature hash commits to the lock time, so no signature made for
    `tx` verifies against the copy. Core's `DataFromTransaction` reads an
    input with a checker that has no transaction data, which fails the
    signatures of a taproot spend. The engine of btclib takes the
    transaction and nothing else to check against.
    """
    other = copy(tx)
    other.lock_time = tx.lock_time ^ 1
    return other


def _script_sig_stack(script_sig: bytes) -> list[bytes]:
    """Return the stack where the evaluation of a script_sig stops.

    Core's `Stacks` runs `EvalScript` over the whole of it with
    `SCRIPT_VERIFY_STRICTENC`, keeps what is on the stack where it fails
    and reads no error.
    """
    return eval_script(script_sig, flags=ScriptFlag.STRICTENC)[0]


class InputSigner:
    """`MutableTransactionSignatureCreator` and its checker, for one input.

    `prevouts` holds the output each input spends, an empty one where the
    caller named none, and `precomputed` is built only where every output
    is known. The transaction is changed in place as inputs are signed;
    no signature hash reads another input's script_sig or witness.
    """

    def __init__(
        self,
        tx: Tx,
        vin_i: int,
        prevouts: list[TxOut],
        precomputed: sig_hash.PrecomputedTxData | None,
        hash_type: int,
    ) -> None:
        """Hold what signing input `vin_i` of `tx` reads."""
        self.tx = tx
        self.vin_i = vin_i
        self.prevouts = prevouts
        self.precomputed = precomputed
        self.hash_type = hash_type

    @property
    def spent_script(self) -> bytes:
        """The script the input spends."""
        return self.prevouts[self.vin_i].script_pub_key.script

    def _sig_hash(self, script_code: bytes, sig_version: str, hash_type: int) -> bytes:
        """Return the hash an ECDSA signature of `hash_type` commits to."""
        if sig_version == _WITNESS_V0:
            return sig_hash.segwit_v0(
                script_code,
                self.tx,
                self.vin_i,
                hash_type,
                self.prevouts[self.vin_i].value,
                self.precomputed,
            )
        return sig_hash.legacy(script_code, self.tx, self.vin_i, hash_type)

    def create_sig(
        self,
        keystore: KeyStore,
        key_id: bytes,
        script_code: bytes,
        sig_version: str,
    ) -> bytes | None:
        """Sign for `key_id` as `MutableTransactionSignatureCreator::CreateSig`.

        A key whose public key is uncompressed signs no witness script,
        and `SIGHASH_DEFAULT` is `SIGHASH_ALL` for ECDSA.
        """
        entry = keystore.keys.get(key_id)
        if entry is None:
            return None
        secret, compressed = entry
        if sig_version == _WITNESS_V0 and not compressed:
            return None
        hash_type = (
            _SIGHASH_ALL if self.hash_type == SIGHASH_DEFAULT else self.hash_type
        )
        digest = self._sig_hash(script_code, sig_version, hash_type)
        return dsa.sign_(digest, secret).serialize() + bytes([hash_type])

    def create_schnorr_sig(self, keystore: KeyStore, x_only: bytes) -> bytes | None:
        """Sign a taproot key path spend for the key with x coordinate `x_only`.

        `CreateSchnorrSig` with no merkle root: the key signs untweaked, so
        it is the output key itself, which is all a provider with no taproot
        data finds by `GetKeyByXOnly`. The signature hash needs every spent
        output, and a hash type SINGLE needs the output to match.
        """
        entry = keystore.keys.get(hash160(b"\x02" + x_only)) or keystore.keys.get(
            hash160(b"\x03" + x_only)
        )
        if entry is None or self.precomputed is None:
            return None
        digest = sig_hash.taproot(
            self.tx,
            self.vin_i,
            self.prevouts,
            self.hash_type,
            0,
            b"",
            b"",
            self.precomputed,
        )
        signature = ssa.sign_(digest, entry[0], b"\x00" * 32).serialize()
        return signature + bytes([self.hash_type]) if self.hash_type else signature

    def check_ecdsa(
        self, signature: bytes, pubkey: bytes, script_code: bytes, sig_version: str
    ) -> bool:
        """Answer `TransactionSignatureChecker::CheckECDSASignature`."""
        if not signature:
            return False
        digest = self._sig_hash(script_code, sig_version, signature[-1])
        try:
            parsed = dsa.Sig.parse(signature[:-1], check_validity=False, strict=False)
            return dsa.verify_(digest, pubkey, parsed)
        except ValueError:
            return False

    def verify_script(
        self,
        script_sig: bytes,
        witness: Sequence[bytes],
        *,
        taproot_data: bool = True,
        signatures: list[tuple[bytes, bytes]] | None = None,
    ) -> ScriptErrorCode | None:
        """Run `VerifyScript` over the input with this solution.

        The code of the error that refuses it, `None` where it is accepted.
        Without `taproot_data` the checker is `DataFromTransaction`'s, which
        has none and fails every signature of a taproot spend. The ECDSA
        signatures the run accepts are appended to `signatures`, as
        `SignatureExtractorChecker` collects them, even where the input is
        then refused.
        """
        tx, precomputed = self.tx, self.precomputed
        if not taproot_data and solver(self.spent_script)[0] == "witness_v1_taproot":
            tx, precomputed = _unsignable(tx), None
        txin = self.tx.vin[self.vin_i]
        held = txin.script_sig, txin.script_witness
        txin.script_sig, txin.script_witness = script_sig, Witness(witness)
        try:
            verify_input(
                self.prevouts,
                tx,
                self.vin_i,
                STANDARD_FLAGS,
                precomputed,
                signatures=signatures,
            )
        except ScriptError as error:
            return error.code
        finally:
            txin.script_sig, txin.script_witness = held
        return None


class _Production:
    """`ProduceSignature`'s state: provider, creator and solution."""

    def __init__(
        self, keystore: KeyStore, signer: InputSigner, sigdata: SignatureData
    ) -> None:
        self.keystore = keystore
        self.signer = signer
        self.sigdata = sigdata

    def get_script(self, script_id: bytes) -> bytes | None:
        """Return the script `script_id` hashes, as `GetCScript` finds it.

        The store first, then the redeem and witness scripts found so far.
        """
        script = self.keystore.scripts.get(script_id)
        if script is not None:
            return script
        for held in (self.sigdata.redeem_script, self.sigdata.witness_script):
            if hash160(held) == script_id:
                return held
        return None

    def get_pubkey(self, key_id: bytes) -> bytes | None:
        """Return the public key of `key_id`, found in a signature or stored."""
        found = self.sigdata.signatures.get(key_id)
        return found[0] if found is not None else self.keystore.pubkeys.get(key_id)

    def create_sig(
        self, pubkey: bytes, script_code: bytes, sig_version: str
    ) -> bytes | None:
        """Return the signature of `pubkey`: one already found, or a new one."""
        key_id = hash160(pubkey)
        found = self.sigdata.signatures.get(key_id)
        if found is not None:
            return found[1]
        signature = self.signer.create_sig(
            self.keystore, key_id, script_code, sig_version
        )
        if signature is not None:
            self.sigdata.signatures[key_id] = (pubkey, signature)
        return signature

    def sign_multisig(
        self, script_code: bytes, solutions: list[bytes], sig_version: str
    ) -> tuple[bool, list[bytes]]:
        """Sign the keys of a multisig, as `SignStep` does.

        Every key is tried, so that `sigdata` holds every signature there is
        to make, and the first `required` of them are pushed after the dummy
        element of the CHECKMULTISIG bug. Core's loop that pads the rest adds
        its counter to a size that grows with it, so it pads half of what is
        missing, rounded up.
        """
        required = solutions[0][0]
        stack: list[bytes] = [b""]
        for pubkey in solutions[1:-1]:
            signature = self.create_sig(pubkey, script_code, sig_version)
            if signature is not None and len(stack) < required + 1:
                stack.append(signature)
        solved = len(stack) == required + 1
        padding = 0
        while padding + len(stack) < required + 1:
            stack.append(b"")
            padding += 1
        return solved, stack

    def sign_step(
        self, script_pub_key: bytes, sig_version: str
    ) -> tuple[bool, str, list[bytes]]:
        """Run `SignStep`: is the script solved, its type, what it pushes."""
        which, solutions = solver(script_pub_key)
        solved, stack = False, []
        if which == "pubkey":
            signature = self.create_sig(solutions[0], script_pub_key, sig_version)
            solved, stack = signature is not None, [signature] if signature else []
        elif which == "pubkeyhash":
            pubkey = self.get_pubkey(solutions[0])
            signature = (
                None
                if pubkey is None
                else self.create_sig(pubkey, script_pub_key, sig_version)
            )
            if pubkey is not None and signature is not None:
                solved, stack = True, [signature, pubkey]
        elif which in {"scripthash", "witness_v0_scripthash"}:
            script_id = (
                solutions[0] if which == "scripthash" else ripemd160(solutions[0])
            )
            script = self.get_script(script_id)
            solved, stack = script is not None, [] if script is None else [script]
        elif which == "multisig":
            solved, stack = self.sign_multisig(script_pub_key, solutions, sig_version)
        elif which == "witness_v0_keyhash":
            solved, stack = True, [solutions[0]]
        elif which == "witness_v1_taproot":
            signature = self.signer.create_schnorr_sig(self.keystore, solutions[0])
            solved, stack = signature is not None, [signature] if signature else []
        elif which == "anchor":
            solved = True
        return solved, which, stack

    def witness_step(
        self, which: str, result: list[bytes], *, solved: bool, wrapped: bool
    ) -> tuple[bool, list[bytes]]:
        """Run the segwit half of `ProduceSignature`.

        The solution of a witness program goes to the witness, and what is
        left for the script_sig is the answer.
        """
        sigdata = self.sigdata
        if solved and which == "witness_v0_keyhash":
            witness_script = b"\x76\xa9\x14" + result[0] + b"\x88\xac"
            solved, _, result = self.sign_step(witness_script, _WITNESS_V0)
            sigdata.script_witness = result
            sigdata.witness = True
            result = []
        elif solved and which == "witness_v0_scripthash":
            witness_script = result[0]
            sigdata.witness_script = witness_script
            solved, sub_type, result = self.sign_step(witness_script, _WITNESS_V0)
            solved = solved and sub_type not in {
                "scripthash",
                "witness_v0_scripthash",
                "witness_v0_keyhash",
            }
            if not solved and not result:
                # a partly signed multisig has a stack, which is not to be
                # thrown away: what is merged is read from it
                solved, result = self.satisfy_miniscript(witness_script)
            result.append(witness_script)
            sigdata.script_witness = result
            sigdata.witness = True
            result = []
        elif which == "witness_v1_taproot" and not wrapped:
            sigdata.witness = True
            if solved:
                sigdata.script_witness = result
            result = []
        if not sigdata.witness:
            sigdata.script_witness = []
        return solved, result

    def satisfy_miniscript(self, witness_script: bytes) -> tuple[bool, list[bytes]]:
        """Satisfy a witness script that is a miniscript, as Core does.

        Core signs where `Satisfy` asks for a signature. Here every key held
        signs first and `satisfy` takes the signatures the best non-malleable
        satisfaction needs, its lock times being the transaction's and the
        input's.
        """
        found = ((key_id, pair[0]) for key_id, pair in self.sigdata.signatures.items())
        key_hashes: dict[Octets, Octets] = dict(
            chain(self.keystore.pubkeys.items(), found)
        )
        try:
            miniscript = from_script(witness_script, "P2WSH", key_hashes)
        except BTClibException:
            return False, []
        signatures: dict[Octets, Octets] = {}
        for key in miniscript.key_expressions:
            pubkey = key.sec()
            signature = self.create_sig(pubkey, witness_script, _WITNESS_V0)
            if signature is not None:
                signatures[pubkey] = signature
        tx = self.signer.tx
        spend = SpendContext(
            locktime=tx.lock_time,
            sequence=tx.vin[self.signer.vin_i].sequence,
            version=tx.version,
        )
        try:
            return True, miniscript.satisfy(signatures, spend)
        except BTClibException:
            return False, []


def produce_signature(
    keystore: KeyStore,
    signer: InputSigner,
    from_pub_key: bytes,
    sigdata: SignatureData,
) -> bool:
    """Run `ProduceSignature`: fill `sigdata` for `from_pub_key`.

    The answer is whether the input is complete, which is whether the
    solution found passes the standard script checks.
    """
    if sigdata.complete:
        return True
    production = _Production(keystore, signer, sigdata)
    solved, which, result = production.sign_step(from_pub_key, _BASE)
    subscript = b""
    pay_to_script_hash = solved and which == "scripthash"
    if pay_to_script_hash:
        subscript = result[0]
        sigdata.redeem_script = subscript
        solved, which, result = production.sign_step(subscript, _BASE)
        solved = solved and which != "scripthash"
    solved, result = production.witness_step(
        which, result, solved=solved, wrapped=pay_to_script_hash
    )
    if pay_to_script_hash:
        result.append(subscript)
    sigdata.script_sig = _push_all(result)
    sigdata.complete = (
        solved
        and signer.verify_script(sigdata.script_sig, sigdata.script_witness) is None
    )
    return sigdata.complete


def data_from_transaction(signer: InputSigner) -> SignatureData:
    """Run `DataFromTransaction`: what the input already holds.

    The signatures its first `VerifyScript` accepts are kept, as Core's
    `SignatureExtractorChecker` keeps them, even where the input is refused.

    An input that passes the script checks is complete. Otherwise the
    scripts are read back and, of a partly signed multisig, the signatures,
    by trying each against the keys in order.
    """
    txin = signer.tx.vin[signer.vin_i]
    data = SignatureData(
        script_sig=txin.script_sig, script_witness=txin.script_witness.stack
    )
    accepted: list[tuple[bytes, bytes]] = []
    code = signer.verify_script(
        data.script_sig, data.script_witness, taproot_data=False, signatures=accepted
    )
    for pubkey, signature in accepted:
        data.signatures.setdefault(hash160(pubkey), (pubkey, signature))
    if code is None:
        data.complete = True
        return data
    script_stack = _script_sig_stack(data.script_sig)
    witness_stack = list(data.script_witness)
    which, solutions = solver(signer.spent_script)
    sig_version = _BASE
    next_script = signer.spent_script
    if which == "scripthash" and script_stack and script_stack[-1]:
        next_script = script_stack.pop()
        data.redeem_script = next_script
        which, solutions = solver(next_script)
    if which == "witness_v0_scripthash" and witness_stack and witness_stack[-1]:
        next_script = witness_stack.pop()
        data.witness_script = next_script
        which, solutions = solver(next_script)
        script_stack, witness_stack = witness_stack, []
        sig_version = _WITNESS_V0
    if which == "multisig" and script_stack:
        pubkeys = solutions[1:-1]
        last_success = 0
        for signature in script_stack:
            for i in range(last_success, len(pubkeys)):
                key_id = hash160(pubkeys[i])
                if key_id in data.signatures or signer.check_ecdsa(
                    signature, pubkeys[i], next_script, sig_version
                ):
                    data.signatures.setdefault(key_id, (pubkeys[i], signature))
                    last_success = i + 1
                    break
    return data


def _merge(into: SignatureData, other: SignatureData) -> None:
    """Run `SignatureData::MergeSignatureData`: take `other` into `into`."""
    if into.complete:
        return
    if other.complete:
        vars(into).update(vars(other))
        return
    into.redeem_script = into.redeem_script or other.redeem_script
    into.witness_script = into.witness_script or other.witness_script
    for key_id, pair in other.signatures.items():
        into.signatures.setdefault(key_id, pair)


def combine_input(signer: InputSigner, variants: Sequence[Tx]) -> SignatureData:
    """Merge what every variant of the transaction holds for the input.

    `combinerawtransaction`'s loop: each variant's input is read by
    `data_from_transaction` and merged, then `produce_signature` runs with
    no keys, only to put what was found in its place. The variants have the
    same inputs, which the caller has checked.
    """
    merged = SignatureData()
    for variant in variants:
        other = InputSigner(
            variant,
            signer.vin_i,
            signer.prevouts,
            signer.precomputed,
            signer.hash_type,
        )
        _merge(merged, data_from_transaction(other))
    produce_signature(KeyStore(), signer, signer.spent_script, merged)
    return merged


def sign_transaction(
    tx: Tx, keystore: KeyStore, coins: Sequence[TxOut | None], hash_type: int
) -> dict[int, str]:
    """Run `SignTransaction` over every input of `tx`, in place.

    `coins` holds the output each input spends, `None` where there is none.
    The answer is the error of each input left unsigned, by index, and is
    empty where all are signed.
    """
    single = hash_type & ~_SIGHASH_ANYONECANPAY == _SIGHASH_SINGLE
    prevouts = [coin if coin is not None else TxOut(0, b"") for coin in coins]
    precomputed = (
        None
        if any(coin is None for coin in coins)
        else sig_hash.PrecomputedTxData(tx, prevouts)
    )
    errors: dict[int, str] = {}
    for i, coin in enumerate(coins):
        if coin is None:
            errors[i] = "Input not found or already spent"
            continue
        signer = InputSigner(tx, i, prevouts, precomputed, hash_type)
        sigdata = data_from_transaction(signer)
        if not single or i < len(tx.vout):
            produce_signature(keystore, signer, coin.script_pub_key.script, sigdata)
        txin = tx.vin[i]
        txin.script_sig = sigdata.script_sig
        txin.script_witness = Witness(sigdata.script_witness)
        if coin.value == MAX_MONEY and txin.script_witness.stack:
            errors[i] = MISSING_AMOUNT
            continue
        code = (
            None
            if sigdata.complete
            else signer.verify_script(txin.script_sig, txin.script_witness.stack)
        )
        if code is None:
            continue
        if code == ScriptErrorCode.INVALID_STACK_OPERATION:
            errors[i] = (
                "Unable to sign input, invalid stack size (possibly missing key)"
            )
        elif code == ScriptErrorCode.SIG_NULLFAIL:
            errors[i] = (
                "CHECK(MULTI)SIG failing with non-zero signature "
                "(possibly need more signatures)"
            )
        else:
            errors[i] = code.description
    return errors
