# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The signing RPCs that need no wallet.

`signmessagewithprivkey` and `verifymessage` are Core's
`src/rpc/signmessage.cpp`, over `MessageSign` and `MessageVerify`
(`src/common/signmessage.cpp`). `signrawtransactionwithkey` and
`combinerawtransaction` are Core's `src/rpc/rawtransaction.cpp`, with
`ParsePrevouts` and `SignTransaction` of `src/rpc/rawtransaction_util.cpp`;
`btclib_node.signing` is the signing itself. All are read at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag, except where a docstring says
otherwise. Each handler has `rpc.callbacks`' signature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RPCErrorCode
from btclib import b58
from btclib.ecc import bms
from btclib.exceptions import BTClibException
from btclib.hashes import hash160, sha256
from btclib.script.script import script_to_asm
from btclib.script.sig_hash import PrecomputedTxData
from btclib.script.witness import Witness
from btclib.tx import Tx
from btclib.tx.out_point import OutPoint
from btclib.tx.tx_out import TxOut

from btclib_node.rpc.errors import RpcError, is_hex, json_type_name, type_errors
from btclib_node.rpc.help import HELP_TEXT
from btclib_node.rpc.solver import solver
from btclib_node.signing import (
    MAX_MONEY,
    MISSING_AMOUNT,
    SIGHASH_DEFAULT,
    InputSigner,
    KeyStore,
    combine_input,
    sign_transaction,
)

if TYPE_CHECKING:
    from btclib.key import PrvKeyData

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

__all__ = [
    "combine_raw_transaction",
    "sign_message_with_privkey",
    "sign_raw_transaction_with_key",
    "verify_message",
]

_COIN = 100_000_000
_TXID_HEX_SIZE = 64
# `getInt<int>()`'s own range
_INT_RANGE = range(-(2**31), 2**31)
_SIGHASH_TYPES = {
    "DEFAULT": SIGHASH_DEFAULT,
    "ALL": 0x01,
    "ALL|ANYONECANPAY": 0x81,
    "NONE": 0x02,
    "NONE|ANYONECANPAY": 0x82,
    "SINGLE": 0x03,
    "SINGLE|ANYONECANPAY": 0x83,
}


def _check_types(
    params: list[Any], arguments: tuple[tuple[str, str], ...], required: int
) -> None:
    """Refuse every argument of the wrong JSON type at once.

    `RPCMethod::HandleRequest` does it before any handler runs. `arguments`
    are the names and JSON types, `required` the number of those that
    must be given: a null optional one is not checked, a null required one
    is.
    """
    mismatches = [
        (i + 1, name, params[i], expected)
        for i, (name, expected) in enumerate(arguments[: len(params)])
        if json_type_name(params[i]) != expected
        and (params[i] is not None or i < required)
    ]
    if mismatches:
        raise type_errors(*mismatches)


def _get_str(value: object) -> str:
    """Return `value` where it is a string, else `get_str`'s refusal."""
    if not isinstance(value, str):
        raise RpcError(
            RPCErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(value)} "
            "is not of expected type string",
        )
    return value


def _decode_secret(node: Node, text: str) -> PrvKeyData | None:
    """Return the key `text` spells on this chain, `None` where it spells none.

    Core's `DecodeSecret` (`src/key_io.cpp`).
    """
    try:
        return b58.prv_key_data_from_wif(text, node.chain.name)
    except BTClibException:
        return None


def sign_message_with_privkey(
    node: Node, conn: RpcConnection, params: list[Any]
) -> str:
    """Answer `signmessagewithprivkey`: the base64 signature of a message.

    The header byte is 27 plus the recovery id, and 4 more for a compressed
    key, as `CKey::SignCompact` writes it. `-5` for a key `DecodeSecret`
    refuses.
    """
    if len(params) < 2:  # noqa: PLR2004
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["signmessagewithprivkey"])
    _check_types(params, (("privkey", "string"), ("message", "string")), required=2)
    key = _decode_secret(node, params[0])
    if key is None:
        raise RpcError(RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Invalid private key")
    return bms.sign(params[1].encode(), key).b64encode()


def verify_message(node: Node, conn: RpcConnection, params: list[Any]) -> bool:
    """Answer `verifymessage`: whether a signature opens to a P2PKH address.

    Core's `MessageVerify`. `-5` for an address that does not decode, `-3`
    for one that is not P2PKH and for a signature that is not base64;
    `false` for one that recovers no key or another key's.
    """
    if len(params) < 3:  # noqa: PLR2004
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["verifymessage"])
    _check_types(
        params,
        (("address", "string"), ("signature", "string"), ("message", "string")),
        required=3,
    )
    result = bms.message_verify(
        params[2].encode(), params[0], params[1], network=node.chain.name
    )
    if result == bms.MessageVerificationResult.ERR_INVALID_ADDRESS:
        raise RpcError(RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Invalid address")
    if result == bms.MessageVerificationResult.ERR_ADDRESS_NO_KEY:
        raise RpcError(RPCErrorCode.TYPE_ERROR, "Address does not refer to key")
    if result == bms.MessageVerificationResult.ERR_MALFORMED_SIGNATURE:
        raise RpcError(RPCErrorCode.TYPE_ERROR, "Malformed base64 encoding")
    return result == bms.MessageVerificationResult.OK


def _parse_hex_tx(text: str) -> Tx | None:
    """Decode a hex transaction as `DecodeHexTx` does, `None` where it fails."""
    if not is_hex(text):
        return None
    try:
        return Tx.parse(bytes.fromhex(text), check_validity=False)
    except BTClibException:
        return None


def _amount_from_value(value: object) -> int:
    """Core's `AmountFromValue`, which is `rpc.callbacks`' `_amount_param`.

    A null is no amount, where `_amount_param` takes it as an absent one.
    The import is here because `rpc.callbacks` imports this module.
    """
    from btclib_node.rpc.callbacks import _amount_param  # noqa: PLC0415

    if value is None:
        raise RpcError(RPCErrorCode.TYPE_ERROR, "Amount is not a number or string")
    return _amount_param([value], 0, name="amount", default=0)


def _parse_sighash(value: object) -> int:
    """Core's `ParseSighashString`: `SIGHASH_DEFAULT` for none."""
    if value is None:
        return SIGHASH_DEFAULT
    text = _get_str(value)
    if text not in _SIGHASH_TYPES:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"'{text}' is not a valid sighash parameter.",
        )
    return _SIGHASH_TYPES[text]


def _find_coin(node: Node, txid: bytes, vout: int) -> TxOut | None:
    """Return the output `vout` of `txid` as `CCoinsViewMemPool` finds it.

    A transaction in the mempool answers for its outputs first, whether or
    not another mempool transaction spends one; then the chain's coins.
    """
    tx = node.mempool.get_tx(txid)
    if tx is not None:
        return tx.vout[vout] if vout < len(tx.vout) else None
    coin = node.chainstate.utxo_index.get_coin(
        OutPoint(txid, vout, check_validity=False).serialize(check_validity=False)
    )
    return None if coin is None else coin.tx_out


def _hash_o(prev_out: dict[str, Any], key: str) -> bytes:
    """Core's `ParseHashO`: 32 bytes from 64 hexadecimal digits."""
    text = _get_str(prev_out.get(key))
    if len(text) != _TXID_HEX_SIZE:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"{key} must be of length 64 (not {len(text)}, for '{text}')",
        )
    return _hex_v(text, key)


def _hex_v(value: object, name: str) -> bytes:
    """Core's `ParseHexV`: a value that is no string is an empty one, no hex."""
    text = value if isinstance(value, str) else ""
    if not is_hex(text):
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"{name} must be hexadecimal string (not '{text}')",
        )
    return bytes.fromhex(text)


def _check_obj(
    obj: dict[str, Any], fields: tuple[tuple[str, str], ...], *, allow_null: bool
) -> None:
    """Core's `RPCTypeCheckObj`: the JSON type of each field, in this order."""
    for key, expected in fields:
        value = obj.get(key)
        if value is None:
            if not allow_null:
                raise RpcError(RPCErrorCode.TYPE_ERROR, f"Missing {key}")
        elif json_type_name(value) != expected:
            raise RpcError(
                RPCErrorCode.TYPE_ERROR,
                f"JSON value of type {json_type_name(value)} for field {key} "
                f"is not of expected type {expected}",
            )


def _add_scripts(
    keystore: KeyStore, prev_out: dict[str, Any], script_pub_key: bytes
) -> None:
    """Take the redeem and witness script of a `prevtxs` entry that needs one.

    A script is kept under its hash, and under that of the P2WSH output
    script that wraps it, for a P2SH-P2WSH output.
    """
    which = solver(script_pub_key)[0]
    if which not in {"scripthash", "witness_v0_scripthash"}:
        return
    _check_obj(
        prev_out,
        (("redeemScript", "string"), ("witnessScript", "string")),
        allow_null=True,
    )
    redeem, witness = prev_out.get("redeemScript"), prev_out.get("witnessScript")
    if redeem is None and witness is None:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER, "Missing redeemScript/witnessScript"
        )
    script = (
        _hex_v(witness, "witnessScript")
        if witness is not None
        else _hex_v(redeem, "redeemScript")
    )
    keystore.add_script(script)
    wrapper = b"\x00\x20" + sha256(script)
    keystore.add_script(wrapper)
    if (
        witness is not None
        and redeem is not None
        and witness != redeem
        and _hex_v(redeem, "redeemScript") != wrapper
    ):
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "redeemScript does not correspond to witnessScript",
        )
    p2sh = {b"\xa9\x14" + hash160(s) + b"\x87" for s in (script, wrapper)}
    if script_pub_key not in (p2sh if which == "scripthash" else {wrapper}):
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "redeemScript/witnessScript does not match scriptPubKey",
        )


def _parse_prevouts(
    prev_txs: list[Any],
    keystore: KeyStore,
    coins: dict[tuple[bytes, int], TxOut | None],
) -> None:
    """Core's `ParsePrevouts`: what the caller says of the outputs spent.

    What an entry names replaces what was found, amount included.
    """
    for prev_out in prev_txs:
        if not isinstance(prev_out, dict):
            raise RpcError(
                RPCErrorCode.DESERIALIZATION_ERROR,
                'expected object with {"txid\'","vout","scriptPubKey"}',
            )
        _check_obj(
            prev_out,
            (("scriptPubKey", "string"), ("txid", "string"), ("vout", "number")),
            allow_null=False,
        )
        txid = _hash_o(prev_out, "txid")
        vout = prev_out["vout"]
        if isinstance(vout, float) or vout not in _INT_RANGE:
            raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
        if vout < 0:
            raise RpcError(
                RPCErrorCode.DESERIALIZATION_ERROR, "vout cannot be negative"
            )
        script_pub_key = _hex_v(prev_out["scriptPubKey"], "scriptPubKey")
        found = coins.get((txid, vout))
        if found is not None and found.script_pub_key.script != script_pub_key:
            raise RpcError(
                RPCErrorCode.DESERIALIZATION_ERROR,
                "Previous output scriptPubKey mismatch:\n"
                f"{script_to_asm(found.script_pub_key.script)}\nvs:\n"
                f"{script_to_asm(script_pub_key)}",
            )
        amount = (
            _amount_from_value(prev_out["amount"])
            if "amount" in prev_out
            else MAX_MONEY
        )
        coins[txid, vout] = TxOut(amount, script_pub_key, check_validity=False)
        _add_scripts(keystore, prev_out, script_pub_key)


def _error_entry(tx: Tx, i: int, message: str) -> dict[str, Any]:
    """Core's `TxInErrorToJSON`: input `i` as it stands, and its error."""
    txin = tx.vin[i]
    return {
        "txid": txin.prev_out.tx_id.hex(),
        "vout": txin.prev_out.vout,
        "witness": [item.hex() for item in txin.script_witness.stack],
        "scriptSig": txin.script_sig.hex(),
        "sequence": txin.sequence,
        "error": message,
    }


def _out_string(out: TxOut) -> str:
    """Core's `CTxOut::ToString`: the amount and 30 digits of the script."""
    return (
        f"CTxOut(nValue={out.value // _COIN}.{out.value % _COIN:08d}, "
        f"scriptPubKey={out.script_pub_key.script.hex()[:30]})"
    )


def sign_raw_transaction_with_key(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `signrawtransactionwithkey`: sign what the given keys can.

    Each input's output is looked up in the mempool, then the chain, and
    `prevtxs` replaces what was found. The answer is the transaction,
    whether it is complete, and each input still unsigned with its error.
    Where an input has a witness and its output no amount, the refusal is
    the call's (`-3`), not the input's.
    """
    if len(params) < 2:  # noqa: PLR2004
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["signrawtransactionwithkey"])
    _check_types(
        params,
        (
            ("hexstring", "string"),
            ("privkeys", "array"),
            ("prevtxs", "array"),
            ("sighashtype", "string"),
        ),
        required=2,
    )
    tx = _parse_hex_tx(params[0])
    if tx is None:
        raise RpcError(
            RPCErrorCode.DESERIALIZATION_ERROR,
            "TX decode failed. Make sure the tx has at least one input.",
        )
    keystore = KeyStore()
    for item in params[1]:
        key = _decode_secret(node, _get_str(item))
        if key is None:
            raise RpcError(RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Invalid private key")
        keystore.add_key(key.q, compressed=key.compressed)
    coins = {
        (txin.prev_out.tx_id, txin.prev_out.vout): _find_coin(
            node, txin.prev_out.tx_id, txin.prev_out.vout
        )
        for txin in tx.vin
    }
    prev_txs, sighash = [*params, None, None][2:4]
    _parse_prevouts(prev_txs or [], keystore, coins)
    hash_type = _parse_sighash(sighash)

    spent = [coins[txin.prev_out.tx_id, txin.prev_out.vout] for txin in tx.vin]
    errors = sign_transaction(tx, keystore, spent, hash_type)
    for i, message in sorted(errors.items()):
        out = spent[i]
        if message == MISSING_AMOUNT and out is not None:
            raise RpcError(
                RPCErrorCode.TYPE_ERROR, f"Missing amount for {_out_string(out)}"
            )
    result: dict[str, Any] = {
        "hex": tx.serialize(include_witness=True, check_validity=False).hex(),
        "complete": not errors,
    }
    if errors:
        result["errors"] = [_error_entry(tx, i, m) for i, m in sorted(errors.items())]
    return result


def _stripped_id(tx: Tx) -> bytes:
    """Return the txid of `tx` with every script_sig and witness removed."""
    stripped = Tx.parse(
        tx.serialize(include_witness=True, check_validity=False), check_validity=False
    )
    for txin in stripped.vin:
        txin.script_sig = b""
        txin.script_witness = Witness()
    return stripped.id


def _decoded_variants(texts: list[Any]) -> list[Tx]:
    """Decode the transactions to combine, and hold each to the first."""
    variants = []
    for idx, item in enumerate(texts):
        tx = _parse_hex_tx(_get_str(item))
        if tx is None:
            raise RpcError(
                RPCErrorCode.DESERIALIZATION_ERROR,
                f"TX decode failed for tx {idx}. "
                "Make sure the tx has at least one input.",
            )
        variants.append(tx)
    first_id = _stripped_id(variants[0])
    for number, tx in enumerate(variants[1:], start=2):
        if _stripped_id(tx) != first_id:
            raise RpcError(
                RPCErrorCode.INVALID_PARAMETER,
                f"Transaction number {number} not compatible with first transaction",
            )
    return variants


def _spent_outputs(node: Node, tx: Tx) -> list[TxOut]:
    """Return the output each input of `tx` spends, refusing a spent one."""
    spent = []
    for txin in tx.vin:
        out = _find_coin(node, txin.prev_out.tx_id, txin.prev_out.vout)
        if out is None:
            raise RpcError(
                RPCErrorCode.VERIFY_ERROR, "Input not found or already spent"
            )
        spent.append(out)
    return spent


def combine_raw_transaction(node: Node, conn: RpcConnection, params: list[Any]) -> str:
    """Answer `combinerawtransaction`: one transaction holding every signature.

    Read at Core's `master`, bitcoin/bitcoin@6d86184a8bcc, not at v31.1: at
    least two transactions are needed, and each must be the first one once
    its signatures are stripped. The v31.1 tag does neither, and copies an
    unrelated transaction's complete inputs onto the first one.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["combinerawtransaction"])
    _check_types(params, (("txs", "array"),), required=1)
    if len(params[0]) < 2:  # noqa: PLR2004
        raise RpcError(
            RPCErrorCode.DESERIALIZATION_ERROR,
            "Missing transactions. At least two transactions required.",
        )
    variants = _decoded_variants(params[0])
    merged = variants[0]
    spent = _spent_outputs(node, merged)
    precomputed = PrecomputedTxData(merged, spent)
    solutions = [
        combine_input(InputSigner(merged, i, spent, precomputed, 1), variants)
        for i in range(len(merged.vin))
    ]
    for txin, data in zip(merged.vin, solutions, strict=True):
        txin.script_sig = data.script_sig
        txin.script_witness = Witness(data.script_witness)
    return merged.serialize(include_witness=True, check_validity=False).hex()
