# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The signing RPCs, live: sign, merge and spend over the socket.

A coinbase is paid to a key, spent into a 2-of-3 P2WSH output that two
calls sign with a key each, and `combinerawtransaction` merges the two,
which `sendrawtransaction` then accepts. The first spend finds its output
on the chain, the second in the mempool.
"""

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from btclib.b58 import p2pkh, wif_from_prv_key
from btclib.hashes import hash160, sha256
from btclib.key import PubKeyData
from btclib.script.script_pub_key import ScriptPubKey
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut
from btclib_ecc.curves import bytes_from_prv_key_int

from tests import anyone_can_spend, rpc_client, wait_until_listening

if TYPE_CHECKING:
    from btclib_node import Node

# Core's own regtest key of `rpc_signmessagewithprivkey.py`, and the
# message and the signature that test expects of it
KEY = wif_from_prv_key(
    0xD2B8A0116D641FE7D3036F8464628FB595B480414C13A301B3D4038C811C28B0,
    "regtest",
    compressed=True,
)
ADDRESS = "mpLQjfK79b7CCV4VMJWEWAj5Mpx8Up5zxB"
MESSAGE = "This is just a test message"
SIGNATURE = (
    "INbVnW4e6PeRmsv2Qgu8NuopvrVjkcxob+sX8OcZG0SALhWybUjzMLPdAsXI46YZGb0KQTRii+wWIQ"
    "zRpG/U+S0="
)


def _wif(secret: int) -> str:
    """Return the regtest WIF of a compressed key."""
    return wif_from_prv_key(secret, "regtest", compressed=True)


def test_messages_are_signed_and_verified(rpc_node: Node) -> None:
    """`signmessagewithprivkey` answers Core's signature, which opens."""
    wait_until_listening(rpc_node.rpc_manager)
    client = rpc_client(rpc_node)

    def call(method: str, params: list[object] | dict[str, Any]) -> Any:
        _, body = client.call_raw(method, params, jsonrpc="1.0", request_timeout=10)
        return body

    assert call("signmessagewithprivkey", [KEY, MESSAGE])["result"] == SIGNATURE
    assert call("verifymessage", [ADDRESS, SIGNATURE, MESSAGE])["result"] is True
    assert call("verifymessage", [ADDRESS, SIGNATURE, MESSAGE + "!"])["result"] is False
    # named arguments, and one too many
    named = {"address": ADDRESS, "signature": SIGNATURE, "message": MESSAGE}
    assert call("verifymessage", named)["result"] is True
    error = call("verifymessage", [ADDRESS, SIGNATURE, MESSAGE, 1])["error"]
    assert error["code"] == -1
    assert error["message"].startswith('verifymessage "address" "signature"')
    assert call("signmessagewithprivkey", ["x", MESSAGE])["error"] == {
        "code": -5,
        "message": "Invalid private key",
    }


def test_a_multisig_is_signed_in_two_calls_and_merged(rpc_node: Node) -> None:
    """Two partial signatures merge into one the mempool accepts."""
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    client = rpc_client(node)

    def call(method: str, *params: object) -> Any:
        _, body = client.call_raw(
            method, list(params), jsonrpc="1.0", request_timeout=60
        )
        assert body["error"] is None, body
        return body["result"]

    secret = 5
    address = p2pkh(
        PubKeyData(bytes_from_prv_key_int(secret, compressed=True), "regtest")
    )
    burn = ScriptPubKey(anyone_can_spend(), "regtest").address
    first = call("generatetoaddress", 1, address)[0]
    call("generatetoaddress", 100, burn)
    coinbase = call("getblock", first, 2)["tx"][0]
    assert coinbase["vout"][0]["scriptPubKey"]["address"] == address
    value = round(Decimal(str(coinbase["vout"][0]["value"])) * 10**8)

    # a 2-of-3 witness script, and the transaction that pays to it
    secrets_3 = [11, 12, 13]
    pubkeys = [bytes_from_prv_key_int(k, compressed=True) for k in secrets_3]
    witness_script = b"\x52" + b"".join(b"\x21" + k for k in pubkeys) + b"\x53\xae"
    output_script = b"\x00\x20" + sha256(witness_script)
    funding = Tx(
        2,
        0,
        [TxIn(OutPoint(bytes.fromhex(coinbase["txid"]), 0), b"", 0xFFFFFFFD)],
        [TxOut(value - 1000, output_script)],
    )
    signed = call(
        "signrawtransactionwithkey",
        funding.serialize(include_witness=True).hex(),
        [_wif(secret)],
    )
    assert signed["complete"] is True
    assert "errors" not in signed
    funding_id = call("sendrawtransaction", signed["hex"])

    # the output is in the mempool only: each call names its script and amount
    spend = Tx(
        2,
        0,
        [TxIn(OutPoint(bytes.fromhex(funding_id), 0), b"", 0xFFFFFFFD)],
        [TxOut(value - 2000, b"\x00\x14" + hash160(pubkeys[0]))],
    )
    prevtxs = [
        {
            "txid": funding_id,
            "vout": 0,
            "scriptPubKey": output_script.hex(),
            "witnessScript": witness_script.hex(),
            "amount": str(Decimal(value - 1000) / 10**8),
        }
    ]
    raw = spend.serialize(include_witness=True).hex()
    halves = [
        call("signrawtransactionwithkey", raw, [_wif(secrets_3[i])], prevtxs)
        for i in (0, 2)
    ]
    assert [half["complete"] for half in halves] == [False, False]
    assert halves[0]["errors"][0]["error"] == (
        "CHECK(MULTI)SIG failing with non-zero signature (possibly need more signatures)"
    )
    merged = call("combinerawtransaction", [half["hex"] for half in halves])
    assert call("testmempoolaccept", [merged])[0]["allowed"]
    spent = call("sendrawtransaction", merged)
    assert call("getrawtransaction", spent)
