# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The signing RPCs' answers, this node's against a real bitcoind's.

Each case and each refusal `tests/unit/rpc/signing_test.py` holds is put to
both, and the answers are held to be equal, signature for signature: ECDSA
signatures are deterministic and so are the BIP340 ones with no auxiliary
data. Outputs found on the chain are read from a block mined to them.
"""

from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import pytest
from bitcoin_core_rpc import HttpError, RpcError
from btclib.tx import Tx, TxOut

from tests.unit.rpc.signing_test import (
    ADDRESS,
    CASES,
    KA,
    KB,
    KC,
    KEY,
    KU,
    MESSAGE,
    MULTISIGS,
    RAW,
    REFUSALS,
    SIGNATURE,
    a_node,
    a_spend,
    call,
    case_call,
    hex_of,
    prevtx,
)

PARSE_ERROR = -32700

if TYPE_CHECKING:
    from tests.integration.conftest import Bitcoind


def test_every_case_is_signed_as_bitcoind_signs_it(bitcoind: Bitcoind) -> None:
    """The same hex, the same completeness, the same errors."""
    for name, case in CASES.items():
        method, params = case_call(case)
        assert call(method, params) == bitcoind.rpc(method, params), name


def test_every_refusal_is_bitcoinds(bitcoind: Bitcoind) -> None:
    """The same code and the same message."""
    for method, params, code, message in REFUSALS:
        if code == PARSE_ERROR:
            # answered with HTTP 500, which the client raises as it is
            with pytest.raises(HttpError, match="HTTP 500"):
                bitcoind.rpc(method, params)
            continue
        with pytest.raises(RpcError) as raised:
            bitcoind.rpc(method, params)
        # past the url, which names bitcoind's own port
        assert (raised.value.code, raised.value.args[0].split(": ", 1)[1]) == (
            code,
            message,
        ), params


def test_messages_are_signed_and_verified_as_bitcoind_does(
    bitcoind: Bitcoind,
) -> None:
    """Three keys, and a signature bitcoind made is opened here."""
    for key in (KEY, KA, KU):
        params: list[Any] = [key, MESSAGE]
        assert call("signmessagewithprivkey", params) == bitcoind.rpc(
            "signmessagewithprivkey", params
        )
    verify: list[Any] = [ADDRESS, SIGNATURE, MESSAGE]
    assert call("verifymessage", verify) is bitcoind.rpc("verifymessage", verify)


def mined_to(bitcoind: Bitcoind, script: bytes) -> tuple[bytes, TxOut]:
    """Mine a block paying its coinbase to `script`, and return that output."""
    block = cast(
        "dict[str, Any]", bitcoind.rpc("generateblock", [f"raw({script.hex()})", []])
    )
    answered = cast("dict[str, Any]", bitcoind.rpc("getblock", [block["hash"], 2]))
    coinbase = answered["tx"][0]
    value = int(Decimal(str(coinbase["vout"][0]["value"])) * 10**8)
    return bytes.fromhex(coinbase["txid"]), TxOut(value, script)


def test_an_output_on_the_chain_is_signed_and_merged_as_bitcoind_does(
    bitcoind: Bitcoind,
) -> None:
    """With the scripts only where they are needed, and halves merged."""
    for name in ("p2pk", "p2pkh", "p2wpkh", "p2tr", *MULTISIGS):
        case = CASES[name]
        txid, out = mined_to(bitcoind, case.script)
        node = a_node({(txid, 0): out})
        raw = hex_of(a_spend((txid, 0), values=(out.value - 1000,)))
        prevtxs = (
            [{**prevtx(case.script, case.extra, out.value), "txid": txid.hex()}]
            if case.extra
            else []
        )
        params: list[Any] = [raw, list(case.keys), prevtxs]
        assert call("signrawtransactionwithkey", params, node) == bitcoind.rpc(
            "signrawtransactionwithkey", params
        ), name
        if name in MULTISIGS:
            halves = [
                cast(
                    "dict[str, Any]",
                    bitcoind.rpc("signrawtransactionwithkey", [raw, [key], prevtxs]),
                )["hex"]
                for key in case.keys[:2]
            ]
            assert call("combinerawtransaction", [halves], node) == bitcoind.rpc(
                "combinerawtransaction", [halves]
            ), name
            if name != "p2sh-fifteen":
                # two complete ones, signed by other keys: the first is kept
                fulls = [
                    cast(
                        "dict[str, Any]",
                        bitcoind.rpc("signrawtransactionwithkey", [raw, keys, prevtxs]),
                    )["hex"]
                    for keys in ([KA, KB], [KB, KC])
                ]
                for order in (fulls, fulls[::-1]):
                    assert call("combinerawtransaction", [order], node) == (
                        bitcoind.rpc("combinerawtransaction", [order])
                    ), name


def test_a_signed_taproot_input_is_never_complete_to_either(
    bitcoind: Bitcoind,
) -> None:
    """The merge is unsigned, and a key signs it again whatever signed it."""
    case = CASES["p2tr"]
    txid, out = mined_to(bitcoind, case.script)
    node = a_node({(txid, 0): out})
    raw = hex_of(a_spend((txid, 0), values=(out.value - 1000,)))
    signed, signed_all = (
        cast(
            "dict[str, Any]",
            bitcoind.rpc("signrawtransactionwithkey", [raw, list(case.keys), [], *kind]),
        )["hex"]
        for kind in ([], ["ALL"])
    )
    for pair in (
        [signed, raw],
        [raw, signed],
        [signed, signed],
        [signed_all, signed],
    ):
        assert call("combinerawtransaction", [pair], node) == bitcoind.rpc(
            "combinerawtransaction", [pair]
        ), pair
    for signed_before in (signed, signed_all):
        params: list[Any] = [signed_before, list(case.keys)]
        assert call("signrawtransactionwithkey", params, node) == bitcoind.rpc(
            "signrawtransactionwithkey", params
        )


# what `EvalScript` of a script_sig keeps where it stops, one op code after
# the pushes of a half signature
STACK_ENDS = {
    "no ops": b"",
    "OP_1 OP_DROP before": b"\x51\x75",
    "OP_CHECKSIG": b"\x01\xaa\x01\xbb\xac",
    "OP_ADD": b"\x93",
    "OP_EQUALVERIFY": b"\x88",
    "OP_EQUALVERIFY of two": b"\x01\xaa\x01\xbb\x88",
    "OP_VERIFY of false": b"\x00\x69",
    "OP_PICK past the stack": b"\x01\x63\x79",
    "OP_RETURN": b"\x6a",
    "a push cut short": b"\x4c",
}


def test_a_script_sig_that_is_evaluated_is_merged_as_bitcoind_merges_it(
    bitcoind: Bitcoind,
) -> None:
    """Half a signature, with an op code before or after its pushes."""
    case = CASES["p2sh"]
    txid, out = mined_to(bitcoind, case.script)
    node = a_node({(txid, 0): out})
    raw = hex_of(a_spend((txid, 0), values=(out.value - 1000,)))
    prevtxs = [{**prevtx(case.script, case.extra, out.value), "txid": txid.hex()}]
    halves = [
        cast(
            "dict[str, Any]",
            bitcoind.rpc("signrawtransactionwithkey", [raw, [key], prevtxs]),
        )["hex"]
        for key in case.keys[:2]
    ]
    half = Tx.parse(bytes.fromhex(halves[0]))
    pushes = half.vin[0].script_sig
    for name, op_codes in STACK_ENDS.items():
        for script_sig in (op_codes + pushes, pushes + op_codes):
            half.vin[0].script_sig = script_sig
            changed = half.serialize(include_witness=True).hex()
            for pair in ([changed, halves[1]], [halves[1], changed]):
                assert call("combinerawtransaction", [pair], node) == (
                    bitcoind.rpc("combinerawtransaction", [pair])
                ), name


def test_an_unknown_output_is_unsigned_in_both(bitcoind: Bitcoind) -> None:
    """The input is answered unsigned, with the same error."""
    params: list[Any] = [RAW, [KA]]
    assert call("signrawtransactionwithkey", params) == bitcoind.rpc(
        "signrawtransactionwithkey", params
    )
