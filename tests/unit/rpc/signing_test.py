# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What the four signing RPCs answer, called as `rpc.main` calls them.

The node is a stub holding the coins a test names. A signing case spends
one output, which the call is told of or finds on the stub's chain, and
`VECTORS` holds what `bitcoind` v31.1.0's `signrawtransactionwithkey`
answered for the same transaction, keys and `prevtxs`: the hex and the
error of the input, if it is left unsigned. `REFUSALS` is what it refuses
and how. `tests/integration/signing_test.py` asks a live `bitcoind` for
both again.
"""

import base64
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import pytest
from btclib import base58
from btclib.b58 import p2pkh, wif_from_prv_key
from btclib.exceptions import ScriptError, ScriptErrorCode
from btclib.hashes import hash160, sha256
from btclib.key import PubKeyData
from btclib.script.engine import verify_input
from btclib.script.script_pub_key import ScriptPubKey
from btclib.script.witness import Witness
from btclib.tx import OutPoint, Tx, TxIn, TxOut
from btclib_ecc.curves import bytes_from_prv_key_int, secp256k1

from btclib_node.interpreter import STANDARD_FLAGS
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import HELP_TEXT
from btclib_node.rpc.signing import (
    combine_raw_transaction,
    sign_message_with_privkey,
    sign_raw_transaction_with_key,
    verify_message,
)
from btclib_node.signing import _push_all, _script_sig_stack

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

CONN = cast("RpcConnection", None)

# Core's own `rpc_signmessagewithprivkey.py` key, message and signature
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

HANDLERS: dict[str, Callable[..., Any]] = {
    "signmessagewithprivkey": sign_message_with_privkey,
    "verifymessage": verify_message,
    "signrawtransactionwithkey": sign_raw_transaction_with_key,
    "combinerawtransaction": combine_raw_transaction,
}


class Utxos:
    """The chain's coins, found by the serialized outpoint."""

    def __init__(self, coins: dict[tuple[bytes, int], TxOut]) -> None:
        """Hold `coins`, each under the serialized outpoint."""
        self.coins = {OutPoint(*key).serialize(): out for key, out in coins.items()}

    def get_coin(self, key: bytes) -> SimpleNamespace | None:
        """Return the coin at `key`, `None` where the chain has none."""
        out = self.coins.get(key)
        return None if out is None else SimpleNamespace(tx_out=out)


def a_node(
    coins: dict[tuple[bytes, int], TxOut] | None = None,
    mempool: dict[bytes, Tx] | None = None,
) -> Node:
    """Return a regtest node stub holding `coins` and `mempool`."""
    held = mempool or {}
    return cast(
        "Node",
        SimpleNamespace(
            chain=SimpleNamespace(name="regtest"),
            mempool=SimpleNamespace(get_tx=held.get),
            chainstate=SimpleNamespace(utxo_index=Utxos(coins or {})),
        ),
    )


NODE = a_node()


def call(method: str, params: list[Any], node: Node = NODE) -> Any:
    """Call the handler of `method` as `rpc.main` does."""
    return HANDLERS[method](node, CONN, params)


def refusal(method: str, params: list[Any], node: Node = NODE) -> tuple[int, str]:
    """Return the code and message of the `RpcError` a call raises."""
    with pytest.raises(RpcError) as raised:
        call(method, params, node)
    return int(raised.value.code), raised.value.message


def wif(secret: int, *, compressed: bool = True) -> str:
    """Return the regtest WIF of `secret`."""
    return wif_from_prv_key(secret, "regtest", compressed)


def pub(secret: int, *, compressed: bool = True) -> bytes:
    """Return the public key of `secret`."""
    return bytes_from_prv_key_int(secret, compressed=compressed)


def multisig(required: int, *keys: bytes) -> bytes:
    """Return the script of a `required`-of-`len(keys)` multisig."""
    return (
        bytes([0x50 + required])
        + b"".join(bytes([len(key)]) + key for key in keys)
        + bytes([0x50 + len(keys), 0xAE])
    )


def p2wsh(script: bytes) -> bytes:
    """Return the output script paying to the witness script `script`."""
    return b"\x00\x20" + sha256(script)


def p2sh(script: bytes) -> bytes:
    """Return the output script paying to the script `script`."""
    return b"\xa9\x14" + hash160(script) + b"\x87"


def p2pkh_script(pubkey: bytes) -> bytes:
    """Return the output script paying to the key `pubkey`."""
    return b"\x76\xa9\x14" + hash160(pubkey) + b"\x88\xac"


FUNDING = b"\x01" * 32
AMOUNT = 100_000
A, B, C = 11, 12, 13
KA, KB, KC, KU = wif(A), wif(B), wif(C), wif(A, compressed=False)
PUB_A, PUB_B, PUB_C, PUB_U = pub(A), pub(B), pub(C), pub(A, compressed=False)
PKH_A = p2pkh_script(PUB_A)
PKH_U = p2pkh_script(PUB_U)
WPKH_A = b"\x00\x14" + hash160(PUB_A)
THREE = multisig(2, PUB_A, PUB_B, PUB_C)
# fifteen keys, a script of 513 bytes: the longest one P2SH takes
FIFTEEN = multisig(15, *(pub(k) for k in range(21, 36)))
# what a miniscript wants, `and_v(v:pk(A),pk(B))`
MINISCRIPT = b"\x21" + PUB_A + b"\xad\x21" + PUB_B + b"\xac"
ANCHOR = b"\x51\x02\x4e\x73"


class Case(NamedTuple):
    """An output to spend, the scripts `prevtxs` adds, and the keys to sign."""

    script: bytes
    extra: dict[str, str]
    keys: tuple[str, ...]
    script_sig: bytes = b""


CASES = {
    "p2pk": Case(b"\x21" + PUB_A + b"\xac", {}, (KA,)),
    "p2pkh": Case(PKH_A, {}, (KA,)),
    "p2pkh-uncompressed": Case(PKH_U, {}, (KU,)),
    "p2pkh-no-key": Case(PKH_A, {}, (KB,)),
    "p2wpkh": Case(WPKH_A, {}, (KA,)),
    "p2sh-p2wpkh": Case(p2sh(WPKH_A), {"redeemScript": WPKH_A.hex()}, (KA,)),
    "p2tr": Case(b"\x51\x20" + PUB_A[1:], {}, (KA,)),
    "p2tr-other-key": Case(b"\x51\x20" + PUB_A[1:], {}, (KB,)),
    "anchor": Case(ANCHOR, {}, ()),
    "anchor-with-script-sig": Case(ANCHOR, {}, (), b"\x01\x01"),
    "witness-unknown": Case(b"\x52\x02\x01\x02", {}, ()),
    "nonstandard": Case(b"\x51", {}, ()),
    "p2sh": Case(p2sh(THREE), {"redeemScript": THREE.hex()}, (KA, KC)),
    "p2sh-one-signed": Case(p2sh(THREE), {"redeemScript": THREE.hex()}, (KB,)),
    "p2sh-unsigned": Case(p2sh(THREE), {"redeemScript": THREE.hex()}, ()),
    "p2sh-garbage-signature": Case(
        p2sh(THREE),
        {"redeemScript": THREE.hex()},
        (KA,),
        b"\x00\x05hello\x4c\x69" + THREE,
    ),
    "p2sh-fifteen": Case(
        p2sh(FIFTEEN),
        {"redeemScript": FIFTEEN.hex()},
        tuple(wif(k) for k in range(21, 36)),
    ),
    "p2wsh": Case(p2wsh(THREE), {"witnessScript": THREE.hex()}, (KA, KB)),
    "p2wsh-one-signed": Case(p2wsh(THREE), {"witnessScript": THREE.hex()}, (KC,)),
    "p2sh-p2wsh": Case(
        p2sh(p2wsh(THREE)),
        {"redeemScript": p2wsh(THREE).hex(), "witnessScript": THREE.hex()},
        (KB, KC),
    ),
    "p2sh-p2wsh-witness-script-only": Case(
        p2sh(p2wsh(THREE)), {"witnessScript": THREE.hex()}, (KB, KC)
    ),
    "p2wsh-pubkey-hash": Case(p2wsh(PKH_A), {"witnessScript": PKH_A.hex()}, (KA,)),
    "p2wsh-uncompressed-key": Case(p2wsh(PKH_U), {"witnessScript": PKH_U.hex()}, (KU,)),
    "p2wsh-garbage": Case(p2wsh(b"\x6a"), {"witnessScript": "6a"}, (KA,)),
    "miniscript": Case(
        p2wsh(MINISCRIPT), {"witnessScript": MINISCRIPT.hex()}, (KA, KB)
    ),
    "miniscript-one-key": Case(
        p2wsh(MINISCRIPT), {"witnessScript": MINISCRIPT.hex()}, (KA,)
    ),
    "miniscript-no-key": Case(
        p2wsh(MINISCRIPT), {"witnessScript": MINISCRIPT.hex()}, (KC,)
    ),
}


def a_spend(
    *inputs: tuple[bytes, int],
    values: tuple[int, ...] = (AMOUNT - 1000,),
    script_sig: bytes = b"",
) -> Tx:
    """Return an unsigned transaction spending `inputs`, paying `values`."""
    return Tx(
        2,
        0,
        [TxIn(OutPoint(txid, vout), script_sig, 0xFFFFFFFD) for txid, vout in inputs],
        [TxOut(value, WPKH_A) for value in values],
    )


def hex_of(tx: Tx) -> str:
    """Return the hex of `tx`, witness included."""
    return tx.serialize(include_witness=True).hex()


def prevtx(
    script: bytes, extra: dict[str, str] | None = None, amount: int = AMOUNT
) -> dict[str, Any]:
    """Return the `prevtxs` entry for the funding output."""
    return {
        "txid": FUNDING.hex(),
        "vout": 0,
        "scriptPubKey": script.hex(),
        "amount": str(Decimal(amount) / 10**8),
        **(extra or {}),
    }


def case_call(case: Case) -> tuple[str, list[Any]]:
    """Return the method and the parameters that sign what `case` spends."""
    raw = hex_of(a_spend((FUNDING, 0), script_sig=case.script_sig))
    return "signrawtransactionwithkey", [
        raw,
        list(case.keys),
        [prevtx(case.script, case.extra)],
    ]


def sign_case(name: str) -> dict[str, Any]:
    """Sign what case `name` spends, through the handler."""
    method, params = case_call(CASES[name])
    return cast("dict[str, Any]", call(method, params))


def accepted(hex_tx: str, script: bytes, amount: int = AMOUNT) -> bool:
    """Run the script of the only input of `hex_tx` against its output."""
    verify_input(
        [TxOut(amount, script)], Tx.parse(bytes.fromhex(hex_tx)), 0, STANDARD_FLAGS
    )
    return True


# what `bitcoind` v31.1.0 answered for each case above, called as
# `case_call` calls it: the hex, and the error where the input is unsigned
VECTORS: dict[str, tuple[str, str]] = {
    "p2pk": (
        "02000000010101010101010101010101010101010101010101010101010101010101010101000000004847304402207d6a480ec929e5303b0e0322f798997d093ccdf5b9836b0477b6c30cc54412ab02200296320386c06b5ad889c9c8321400a0fef187b0bcb4ef9f94cbd1e0ffcd120301fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "",
    ),
    "p2pkh": (
        "02000000010101010101010101010101010101010101010101010101010101010101010101000000006a47304402207c1f5a56d0b0a2013d237162f9609b83d154c5184d94bf0dcef19a1c0ab1a2cc02206d4743cc676d899a79b75a7232808256c64a96db7aefa8c4ea632f4542139b5f012103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cbfdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "",
    ),
    "p2pkh-uncompressed": (
        "02000000010101010101010101010101010101010101010101010101010101010101010101000000008a47304402201b89191010e82664921b280a198ab81315c818986155923675afaa666519b74f02207937063265204267d251ec8f69edc6ccf068b9147dc6c7a92497e2bb895fd94b014104774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cbd984a032eb6b5e190243dd56d7b7b365372db1e2dff9d6a8301d74c9c953c61bfdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "",
    ),
    "p2pkh-no-key": (
        "020000000101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "Unable to sign input, invalid stack size (possibly missing key)",
    ),
    "p2wpkh": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d2024730440220148a7b7b433ee2f69baac2ce463f1e4d5111dd9ddbdec0f984f3c86ad1fcf0d20220692ecd21c0f01d3e61548c4d26ef21bfe0a500df3d6885d25a187a1698272e90012103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb00000000",
        "",
    ),
    "p2sh-p2wpkh": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000017160014362995a6e6922a04e0b832a80bc56c33709a42d2fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d2024730440220148a7b7b433ee2f69baac2ce463f1e4d5111dd9ddbdec0f984f3c86ad1fcf0d20220692ecd21c0f01d3e61548c4d26ef21bfe0a500df3d6885d25a187a1698272e90012103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb00000000",
        "",
    ),
    "p2tr": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d2014064afdc63edf509fd305517020c216cb19605e10a9e7bb3e776f14f6450c6b6a6a4770d80a10651cb8f94f326cb13e66719b6ac5d2877f0b4821fd49cb945c7d100000000",
        "",
    ),
    "p2tr-other-key": (
        "020000000101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "Witness program was passed an empty witness",
    ),
    "anchor": (
        "020000000101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "",
    ),
    "anchor-with-script-sig": (
        "020000000101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "",
    ),
    "witness-unknown": (
        "020000000101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "Witness version reserved for soft-fork upgrades",
    ),
    "nonstandard": (
        "020000000101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "",
    ),
    "p2sh": (
        "0200000001010101010101010101010101010101010101010101010101010101010101010100000000fc00473044022053b14b331730df2a73da6d8b198fc54c7459ec28f7c3e2ff350958388132de8402202fe8bed7fc914f724bc8b6961b553bbf986d4c70d6a571c037095b5a5d96a6d001473044022071bb3c693e1aea88f2f1743f391e9e70a8816526a56265229d042014c33fd9af022029ddf6101fff9c875ccffc5cc284f67d113dc1203fdbeef338690961802f9179014c69522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853aefdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "",
    ),
    "p2sh-one-signed": (
        "0200000001010101010101010101010101010101010101010101010101010101010101010100000000b50047304402207143a6ac6b76f69702346c161094abb6c2fee647793c518df0ab570925739e1902203ecb841f2db22f28b6ed9ded92b2234a818ebb703780b5bfb44b00dcab2fb75701004c69522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853aefdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "CHECK(MULTI)SIG failing with non-zero signature (possibly need more signatures)",
    ),
    "p2sh-unsigned": (
        "02000000010101010101010101010101010101010101010101010101010101010101010101000000006d00004c69522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853aefdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "Unable to sign input, invalid stack size (possibly missing key)",
    ),
    "p2sh-garbage-signature": (
        "0200000001010101010101010101010101010101010101010101010101010101010101010100000000b500473044022053b14b331730df2a73da6d8b198fc54c7459ec28f7c3e2ff350958388132de8402202fe8bed7fc914f724bc8b6961b553bbf986d4c70d6a571c037095b5a5d96a6d001004c69522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853aefdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "CHECK(MULTI)SIG failing with non-zero signature (possibly need more signatures)",
    ),
    "p2sh-fifteen": (
        "0200000001010101010101010101010101010101010101010101010101010101010101010100000000fd3d0600473044022018b4f6f1edb3a6f315ae6d54e239d48247c5c36d4fa587c27566871525a86f9402203bbea4c1734f9f9326a9b349cba2a457da68fc729f700a89d8a201f126bc41150147304402201d310777bb93835090f4635ebb23ec7e11e7fbcf4265b4617c929d2fd9e54f3c02201f57f09ab5fad42f660fd4c92ac295bce0e8fc20c6453770a3e5e5abfd9199ac01473044022047ceb3df378df8bdc2d8e7fff949c9a2843dd7ff957fd4a4664fb97554d74cd702200db8eec78319d711161e52f751aac431228fde204f80693d3c138d69aa418efc014730440220730633f9e5820ee4313fa45630e7d809413573b4ddc205106b0626aa357e561d0220339ae80728cac0a0746e146feaaba6474ed4c425b1867ee1b26ac7b9ea40f7380147304402201dd02fad394e7804af293e48fc102bdd8446bd293cb815e4c31c609cda7482b2022008a8e56a2d5f0b18c1e402367b5e64b3a8b868d4a7df6d5387e7df726fcc74b70147304402206e7c6cf28c373113b5017e7b73fcef68ea0d33337ab05055ff5ec0aea168a0470220408bf03894be31f4152518fe04353b14ad163cfa2777f09a2c8b432202e8d92b014730440220074201ed59fdc005fbcc9a5b573ff1211fb89669ea0bf194cb5605ae819f3467022027adba6a285ea0997ea5cde7178fc6712562bc4fb798d2a0ac4f98e7fb2eb2a50147304402205c8ed526851f6ff12814e6660969705ab996500e0a35d053e51a94f3107e38c40220437618c3227f790fac59ba8e11a35225841f43b1340c7a1a2674a5b863174c1501473044022009cd21bf34d1b4dd2e2193f486d0e22db42910537b5cae8ff9e1f81045d9aa1402202db4b9b99fb89b4124379ddbf5b74a333694d40606c00dc0edaef1cf65d7f26d0147304402201640f4371854c490b808fc9396b27eb75852d2eb7b353610333b8fb5d1208fa402200cee974f647fd01c77c36481e742c1be3e903ea9c8bbb3636193762460276469014730440220656acfc2d6eb9733e2cd3a93d52e4f46e845cbeb25ebbebe3e78055b41a4da63022051f32f2fc0ae6567a09d868e076d77f70f0884f61852b6bd8faddeefc07335c60147304402201e3511e6bb3f67a969d30be4beb77002010841c77fd595ebd3ed958d6e00b1bc0220297fcc0df6c2f5bd19e1c541913fe71e8b6064ca649d87440f1c7032329a047c01473044022007d32fb489dea53f895c72e4bb3624f950ee5f0d71bc7c755e13596739e382d002206f535d50e28b5b1078381deeafb52c403f74162423479309507714b82d0bf5f8014730440220334ec4688b498082c64e8b6aab5a5e735a5d47b79b7a6486cfc2a1dcf2d2450f02203b9ea50d605dd8ebf5c7e94d5aa9975d53517f2158ab3e554223d6a96c8c757001473044022002b032f667729448eb5ad77dabb9070e563eda51dfaf31d81f7fe001a5ba2b0f022019eb7ffee147d5e14bd21a09c9343515daee07183b7675741fc9ce066e1c0778014d01025f2102352bbf4a4cdd12564f93fa332ce333301d9ad40271f8107181340aef25be59d52103421f5fc9a21065445c96fdb91c0c1e2f2431741c72713b4b99ddcb316f31e9fc21032fa2104d6b38d11b0230010559879124e42ab8dfeff5ff29dc9cdadd4ecacc3f2103fe72c435413d33d48ac09c9161ba8b09683215439d62b7940502bda8b202e6ce21029248279b09b4d68dab21a9b066edda83263c3d84e09572e269ca0cd7f545371421026687cdb5b650d558f40cbdefc8e40997c03fe1b2abb840885e5cad81710c4c8a2103daed4f2be3a8bf278e70132fb0beb7522f570e144bf615c07e996d443dee8729210255eb67d7b7238a70a7fa6f64d5dc3c826b31536da6eb344dc39a66f904f979682102c44d12c7065d812e8acf28d7cbb19f9011ecd9e9fdf281b0e6a3b5e87d22e7db21036d2b085e9e382ed10b69fc311a03f8641ccfff21574de0927513a49d9a688a0021026a245bf6dc698504c89a20cfded60853152b695336c28063b61c65cbd269e6b42103d30199d74fb5a22d47b6e054e2f378cedacffcb89904a61d75d0dbd407143e6521021697ffa6fd9de627c077e3d2fe541084ce13300b0bec1146f95ae57f0d0bd6a521031be68a5a028f2601d0e80d468c344ba331d611b96c358b6032e8b4da0547fc112103605bdb019981718b986d0f07e834cb0d9deb8360ffb7f61df982345ef27a74795faefdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000",
        "",
    ),
    "p2wsh": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d2040047304402206dfd167d5830d43ba0408d6c3d0797eba9caedf06175a029252df3d748ead60202202a34076583e4bcc2d67ab731169922b8d74c5a93b2371ed4210a190b0f9654a0014730440220553ae4f8c37cd376333e86ef565e87388efdbbb5919bd283d200b4e8e697c2060220145ce527137cdae856e66f9215652aa2bc6144f39551d6c6bb9f100af9e981fc0169522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853ae00000000",
        "",
    ),
    "p2wsh-one-signed": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d2040047304402200157cb4e77b4e11cf2d2a113b3de4081ad57835a611b611e80362ee8db9543d302203a0d5b08fb9a53d575112be68ba7d3086e7928c1ad85f1136d6d1f02a23fbd90010069522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853ae00000000",
        "CHECK(MULTI)SIG failing with non-zero signature (possibly need more signatures)",
    ),
    "p2sh-p2wsh": (
        "02000000000101010101010101010101010101010101010101010101010101010101010101010100000000232200203523c780cf9450e9f486ff10b4c9f15912faba29f3d5f81594a037d7853c1e44fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d204004730440220553ae4f8c37cd376333e86ef565e87388efdbbb5919bd283d200b4e8e697c2060220145ce527137cdae856e66f9215652aa2bc6144f39551d6c6bb9f100af9e981fc0147304402200157cb4e77b4e11cf2d2a113b3de4081ad57835a611b611e80362ee8db9543d302203a0d5b08fb9a53d575112be68ba7d3086e7928c1ad85f1136d6d1f02a23fbd900169522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853ae00000000",
        "",
    ),
    "p2sh-p2wsh-witness-script-only": (
        "02000000000101010101010101010101010101010101010101010101010101010101010101010100000000232200203523c780cf9450e9f486ff10b4c9f15912faba29f3d5f81594a037d7853c1e44fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d204004730440220553ae4f8c37cd376333e86ef565e87388efdbbb5919bd283d200b4e8e697c2060220145ce527137cdae856e66f9215652aa2bc6144f39551d6c6bb9f100af9e981fc0147304402200157cb4e77b4e11cf2d2a113b3de4081ad57835a611b611e80362ee8db9543d302203a0d5b08fb9a53d575112be68ba7d3086e7928c1ad85f1136d6d1f02a23fbd900169522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853ae00000000",
        "",
    ),
    "p2wsh-pubkey-hash": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d2034730440220148a7b7b433ee2f69baac2ce463f1e4d5111dd9ddbdec0f984f3c86ad1fcf0d20220692ecd21c0f01d3e61548c4d26ef21bfe0a500df3d6885d25a187a1698272e90012103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb1976a914362995a6e6922a04e0b832a80bc56c33709a42d288ac00000000",
        "",
    ),
    "p2wsh-uncompressed-key": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d2011976a914ef31f6db0c690071007448c034d35e1d4ef2ec4a88ac00000000",
        "Unable to sign input, invalid stack size (possibly missing key)",
    ),
    "p2wsh-garbage": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d201016a00000000",
        "OP_RETURN was encountered",
    ),
    "miniscript": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d2034730440220101323da051ff468b62acda2cdaa6c95946b8a9b4ba3b2a982fa0d4dbdf1c37702204af91a40d1d2c7a4e5c61267e1904a15bbe0ef0eb7e906a7262fe46ea88931b501473044022029816f3e91e77abe931aae44413f85a0d37c8e739942f290aa23100c408db28d022027bf9de6ae1787b63852fb3ad4662ed1b94d0477f5d149228dc764f084692fde01462103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cbad2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85aac00000000",
        "",
    ),
    "miniscript-one-key": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d201462103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cbad2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85aac00000000",
        "Unable to sign input, invalid stack size (possibly missing key)",
    ),
    "miniscript-no-key": (
        "0200000000010101010101010101010101010101010101010101010101010101010101010101010000000000fdffffff01b882010000000000160014362995a6e6922a04e0b832a80bc56c33709a42d201462103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cbad2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85aac00000000",
        "Unable to sign input, invalid stack size (possibly missing key)",
    ),
}

RAW = hex_of(a_spend((FUNDING, 0)))
PREV = prevtx(WPKH_A)
UNEXPECTED_OBJECT = 'expected object with {"txid\'","vout","scriptPubKey"}'
DECODE_FAILED = "TX decode failed. Make sure the tx has at least one input."


def wrong_type(position: int, name: str, kind: str, expected: str) -> str:
    """Return Core's refusal of the argument `name` for being of type `kind`."""
    return (
        "Wrong type passed:\n{\n"
        f'    "Position {position} ({name})": "JSON value of type {kind} '
        f'is not of expected type {expected}"\n}}'
    )


def a_wif(payload: bytes) -> str:
    """Return the base58check string of a regtest secret key `payload`."""
    return base58.encode(b"\xef" + payload).decode()


def entry(**changes: Any) -> dict[str, Any]:
    """Return `PREV` with `changes`, a `None` removing the key."""
    return {k: v for k, v in {**PREV, **changes}.items() if v is not None}


def prev_refusal(
    prev_out: object, code: int, message: str
) -> tuple[str, list[Any], int, str]:
    """Return the refusal of a `prevtxs` entry."""
    return "signrawtransactionwithkey", [RAW, [], [prev_out]], code, message


SCRIPT_HASH = p2sh(THREE).hex()

# method, parameters, and the code and message `bitcoind` v31.1.0 refuses with
REFUSALS: list[tuple[str, list[Any], int, str]] = [
    ("signmessagewithprivkey", [], -1, HELP_TEXT["signmessagewithprivkey"]),
    ("signmessagewithprivkey", [KEY], -1, HELP_TEXT["signmessagewithprivkey"]),
    (
        "signmessagewithprivkey",
        [1, MESSAGE],
        -3,
        wrong_type(1, "privkey", "number", "string"),
    ),
    ("signmessagewithprivkey", ["x", MESSAGE], -5, "Invalid private key"),
    (
        "signmessagewithprivkey",
        [base58.encode(b"\x80" + b"\x01".rjust(32, b"\0")).decode(), MESSAGE],
        -5,
        "Invalid private key",
    ),
    (
        "signmessagewithprivkey",
        [a_wif(b"\x00" * 32), MESSAGE],
        -5,
        "Invalid private key",
    ),
    (
        "signmessagewithprivkey",
        [a_wif(secp256k1.n.to_bytes(32, "big")), MESSAGE],
        -5,
        "Invalid private key",
    ),
    (
        "signmessagewithprivkey",
        [a_wif(b"\x01".rjust(32, b"\0") + b"\x02"), MESSAGE],
        -5,
        "Invalid private key",
    ),
    (
        "signmessagewithprivkey",
        [a_wif(b"\x01".rjust(33, b"\0") + b"\x01"), MESSAGE],
        -5,
        "Invalid private key",
    ),
    ("verifymessage", [ADDRESS, SIGNATURE], -1, HELP_TEXT["verifymessage"]),
    (
        "verifymessage",
        [1, SIGNATURE, MESSAGE],
        -3,
        wrong_type(1, "address", "number", "string"),
    ),
    ("verifymessage", ["x", SIGNATURE, MESSAGE], -5, "Invalid address"),
    (
        "verifymessage",
        [p2pkh(PubKeyData(PUB_A, "mainnet")), SIGNATURE, MESSAGE],
        -5,
        "Invalid address",
    ),
    (
        "verifymessage",
        [ScriptPubKey(WPKH_A, "regtest").address, SIGNATURE, MESSAGE],
        -3,
        "Address does not refer to key",
    ),
    (
        "verifymessage",
        [ScriptPubKey(WPKH_A, "regtest").address.upper(), SIGNATURE, MESSAGE],
        -3,
        "Address does not refer to key",
    ),
    (
        "verifymessage",
        [ScriptPubKey(p2sh(THREE), "regtest").address, SIGNATURE, MESSAGE],
        -3,
        "Address does not refer to key",
    ),
    (
        "verifymessage",
        [ADDRESS, SIGNATURE[:-1], MESSAGE],
        -3,
        "Malformed base64 encoding",
    ),
    (
        "verifymessage",
        [ADDRESS, SIGNATURE[:-2] + "!=", MESSAGE],
        -3,
        "Malformed base64 encoding",
    ),
    (
        "verifymessage",
        [ADDRESS, " " + SIGNATURE, MESSAGE],
        -3,
        "Malformed base64 encoding",
    ),
    (
        "verifymessage",
        [ADDRESS, SIGNATURE[:-2] + "T=", MESSAGE],
        -3,
        "Malformed base64 encoding",
    ),
    ("signrawtransactionwithkey", [RAW], -1, HELP_TEXT["signrawtransactionwithkey"]),
    (
        "signrawtransactionwithkey",
        [1, []],
        -3,
        wrong_type(1, "hexstring", "number", "string"),
    ),
    (
        "signrawtransactionwithkey",
        [RAW, "x"],
        -3,
        wrong_type(2, "privkeys", "string", "array"),
    ),
    (
        "signrawtransactionwithkey",
        [RAW, [], [], 1],
        -3,
        wrong_type(4, "sighashtype", "number", "string"),
    ),
    ("signrawtransactionwithkey", ["zz", []], -22, DECODE_FAILED),
    ("signrawtransactionwithkey", ["", []], -22, DECODE_FAILED),
    ("signrawtransactionwithkey", ["00", []], -22, DECODE_FAILED),
    ("signrawtransactionwithkey", [RAW, ["x"]], -5, "Invalid private key"),
    (
        "signrawtransactionwithkey",
        [RAW, [1]],
        -3,
        "JSON value of type number is not of expected type string",
    ),
    prev_refusal(1, -22, UNEXPECTED_OBJECT),
    prev_refusal({}, -3, "Missing scriptPubKey"),
    prev_refusal(entry(scriptPubKey=None), -3, "Missing scriptPubKey"),
    prev_refusal(
        entry(scriptPubKey=1),
        -3,
        "JSON value of type number for field scriptPubKey is not of expected "
        "type string",
    ),
    prev_refusal(
        entry(vout="0"),
        -3,
        "JSON value of type string for field vout is not of expected type number",
    ),
    prev_refusal(entry(txid="ab"), -8, "txid must be of length 64 (not 2, for 'ab')"),
    prev_refusal(
        entry(txid="zz" * 32),
        -8,
        f"txid must be hexadecimal string (not '{'zz' * 32}')",
    ),
    prev_refusal(entry(vout=1.5), -1, "JSON integer out of range"),
    prev_refusal(entry(vout=2**31), -1, "JSON integer out of range"),
    prev_refusal(entry(vout=-1), -22, "vout cannot be negative"),
    prev_refusal(
        entry(scriptPubKey="zz"),
        -8,
        "scriptPubKey must be hexadecimal string (not 'zz')",
    ),
    prev_refusal({**PREV, "amount": None}, -3, "Amount is not a number or string"),
    prev_refusal(entry(amount=True), -3, "Amount is not a number or string"),
    prev_refusal(entry(amount="x"), -3, "Invalid amount"),
    prev_refusal(entry(amount="NaN"), -3, "Invalid amount"),
    prev_refusal(entry(amount="1."), -3, "Invalid amount"),
    prev_refusal(entry(amount=".5"), -3, "Invalid amount"),
    prev_refusal(entry(amount=" 1"), -3, "Invalid amount"),
    prev_refusal(entry(amount="0.000000001"), -3, "Invalid amount"),
    prev_refusal(entry(amount="-1"), -3, "Amount out of range"),
    prev_refusal(entry(amount="21000000.00000001"), -3, "Amount out of range"),
    prev_refusal(
        entry(scriptPubKey=SCRIPT_HASH), -8, "Missing redeemScript/witnessScript"
    ),
    prev_refusal(
        entry(scriptPubKey=SCRIPT_HASH, redeemScript=1),
        -3,
        "JSON value of type number for field redeemScript is not of expected "
        "type string",
    ),
    prev_refusal(
        entry(scriptPubKey=SCRIPT_HASH, redeemScript="zz"),
        -8,
        "redeemScript must be hexadecimal string (not 'zz')",
    ),
    prev_refusal(
        entry(scriptPubKey=p2wsh(THREE).hex(), witnessScript="zz"),
        -8,
        "witnessScript must be hexadecimal string (not 'zz')",
    ),
    prev_refusal(
        entry(scriptPubKey=SCRIPT_HASH, redeemScript=THREE.hex(), witnessScript="51"),
        -8,
        "redeemScript does not correspond to witnessScript",
    ),
    prev_refusal(
        entry(scriptPubKey=SCRIPT_HASH, redeemScript="51"),
        -8,
        "redeemScript/witnessScript does not match scriptPubKey",
    ),
    prev_refusal(
        entry(scriptPubKey=p2wsh(THREE).hex(), redeemScript="51"),
        -8,
        "redeemScript/witnessScript does not match scriptPubKey",
    ),
    (
        "signrawtransactionwithkey",
        [RAW, [], [PREV], "NOPE"],
        -8,
        "'NOPE' is not a valid sighash parameter.",
    ),
    (
        "signrawtransactionwithkey",
        [RAW, [KA], [entry(amount=None)]],
        -3,
        (
            "Missing amount for CTxOut(nValue=21000000.00000000, "
            f"scriptPubKey={WPKH_A.hex()[:30]})"
        ),
    ),
    ("combinerawtransaction", [], -1, HELP_TEXT["combinerawtransaction"]),
    ("combinerawtransaction", ["x"], -3, wrong_type(1, "txs", "string", "array")),
    (
        "combinerawtransaction",
        [[RAW, 1]],
        -3,
        "JSON value of type number is not of expected type string",
    ),
    (
        "combinerawtransaction",
        [[RAW, "zz"]],
        -22,
        "TX decode failed for tx 1. Make sure the tx has at least one input.",
    ),
    ("combinerawtransaction", [[RAW, RAW]], -25, "Input not found or already spent"),
]

# what Core's master refuses and v31.1 does not
MASTER_REFUSALS: list[tuple[str, list[Any], int, str]] = [
    (
        "combinerawtransaction",
        [[RAW]],
        -22,
        "Missing transactions. At least two transactions required.",
    ),
    (
        "combinerawtransaction",
        [[RAW, hex_of(a_spend((FUNDING, 1)))]],
        -8,
        "Transaction number 2 not compatible with first transaction",
    ),
    # fewer than two is refused before any is decoded
    (
        "combinerawtransaction",
        [["zz"]],
        -22,
        "Missing transactions. At least two transactions required.",
    ),
    # every transaction is decoded before the first is compared
    (
        "combinerawtransaction",
        [[RAW, hex_of(a_spend((FUNDING, 1))), "zz"]],
        -22,
        "TX decode failed for tx 2. Make sure the tx has at least one input.",
    ),
]


@pytest.mark.parametrize(
    ("method", "params", "code", "message"), REFUSALS + MASTER_REFUSALS
)
def test_a_call_is_refused_as_core_refuses_it(
    method: str, params: list[Any], code: int, message: str
) -> None:
    """The code and the message are Core's."""
    assert refusal(method, params) == (code, message)


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_a_case_is_signed_as_core_signs_it(name: str) -> None:
    """The hex and the error equal Core's, byte for byte."""
    result = sign_case(name)
    hex_tx, error = VECTORS[name]
    assert result["hex"] == hex_tx
    assert result["complete"] is (not error)
    assert [e["error"] for e in result.get("errors", [])] == ([error] if error else [])


@pytest.mark.parametrize("name", [n for n in sorted(VECTORS) if not VECTORS[n][1]])
def test_a_complete_case_passes_the_script_checks(name: str) -> None:
    """A transaction answered complete is accepted by the script engine."""
    assert accepted(sign_case(name)["hex"], CASES[name].script)


@pytest.mark.parametrize("name", ["p2pk", "p2pkh", "p2wsh-pubkey-hash"])
def test_a_signature_of_a_refused_input_is_kept(name: str) -> None:
    """An extra push fails CLEANSTACK after the signature has been checked.

    Core keeps every signature its first script run accepts, so the merge
    and a call with an unrelated key give the input back complete.
    """
    case = CASES[name]
    complete = VECTORS[name][0]
    padded = Tx.parse(bytes.fromhex(complete))
    txin = padded.vin[0]
    if txin.script_witness.stack:
        stack = txin.script_witness.stack
        txin.script_witness = Witness([b"\x01", *stack])
    else:
        txin.script_sig = b"\x51" + txin.script_sig
    with pytest.raises(ScriptError) as refused:
        verify_input([TxOut(AMOUNT, case.script)], padded, 0, STANDARD_FLAGS)
    assert refused.value.code == ScriptErrorCode.CLEANSTACK
    node = a_node({(FUNDING, 0): TxOut(AMOUNT, case.script)})
    answers = [
        call("combinerawtransaction", [pair], node)
        for pair in ([hex_of(padded), RAW], [RAW, hex_of(padded)])
    ]
    answers.append(
        call(
            "signrawtransactionwithkey",
            [hex_of(padded), [KB], [prevtx(case.script, case.extra)]],
        )
    )
    assert answers == [complete, complete, {"hex": complete, "complete": True}]


# a bare 2-of-2 that lists one key twice, so two signatures of it are valid
TWICE = multisig(2, PUB_A, PUB_A)
ALL_OR_ANYONE = ("ALL", "ALL|ANYONECANPAY")


def stack_of(hex_tx: str) -> list[bytes]:
    """Return the stack the script_sig of the first input leaves."""
    return _script_sig_stack(Tx.parse(bytes.fromhex(hex_tx)).vin[0].script_sig)


def two_signatures(raw: str, prevtxs: list[dict[str, Any]], key: str) -> list[bytes]:
    """Return `key`'s signature of `raw` under ALL, then ALL|ANYONECANPAY."""
    return [
        stack_of(
            call("signrawtransactionwithkey", [raw, [key], prevtxs, hash_type])["hex"]
        )[1]
        for hash_type in ALL_OR_ANYONE
    ]


def test_the_first_signature_accepted_for_a_key_is_kept() -> None:
    """Two signatures of one key pass the first run; the first it checks stays.

    The run checks the last signature first, so it is the ANYONECANPAY one,
    which a call with an unrelated key gives back. CLEANSTACK refuses the
    extra push, and Core's `SignatureExtractorChecker` keeps the first it
    accepts for each key.
    """
    prevtxs = [prevtx(TWICE)]
    first, second = two_signatures(RAW, prevtxs, KA)
    assert first != second
    padded = hex_of(
        a_spend(
            (FUNDING, 0),
            script_sig=b"\x51\x00"
            + bytes([len(first)])
            + first
            + bytes([len(second)])
            + second,
        )
    )
    answer = call("signrawtransactionwithkey", [padded, [KU], prevtxs])
    assert answer["complete"] is True
    assert stack_of(answer["hex"])[1:] == [second, second]


def test_the_merge_keeps_the_first_signature_of_a_key() -> None:
    """Of two signatures of one key, the first transaction's is the merge's."""
    script = multisig(2, PUB_A, PUB_B)
    prevtxs = [prevtx(script)]
    first, second = two_signatures(RAW, prevtxs, KA)
    assert first != second
    halves = [
        call("signrawtransactionwithkey", [RAW, [key], prevtxs, hash_type])["hex"]
        for key, hash_type in ((KA, "ALL"), (KA, "ALL|ANYONECANPAY"), (KB, "ALL"))
    ]
    node = a_node({(FUNDING, 0): TxOut(AMOUNT, script)})
    merged = call("combinerawtransaction", [halves], node)
    assert stack_of(merged)[1] == first


def test_the_cases_are_all_in_the_vectors() -> None:
    """A case without a vector is one nothing holds to Core."""
    assert sorted(CASES) == sorted(VECTORS)


def test_an_unsigned_input_is_answered_as_it_stands() -> None:
    """The error entry names the input, as `TxInErrorToJSON` does."""
    result = sign_case("p2pkh-no-key")
    assert result["errors"] == [
        {
            "txid": FUNDING.hex(),
            "vout": 0,
            "witness": [],
            "scriptSig": "",
            "sequence": 0xFFFFFFFD,
            "error": "Unable to sign input, invalid stack size (possibly missing key)",
        }
    ]


# -- the messages


def test_messages_are_signed_and_verified() -> None:
    """Core's vector, both ways, and a message that is another's."""
    assert call("signmessagewithprivkey", [KEY, MESSAGE]) == SIGNATURE
    assert call("verifymessage", [ADDRESS, SIGNATURE, MESSAGE]) is True
    assert call("verifymessage", [ADDRESS, SIGNATURE, "other"]) is False


def test_an_uncompressed_key_signs_for_its_own_address() -> None:
    """The header byte tells the key's compression, so each has an address."""
    signature = call("signmessagewithprivkey", [wif(5, compressed=False), "m"])
    address = p2pkh(PubKeyData(pub(5, compressed=False), "regtest"))
    assert base64.b64decode(signature)[0] in range(27, 31)
    assert call("verifymessage", [address, signature, "m"]) is True
    assert call("verifymessage", [ADDRESS, signature, "m"]) is False


def test_the_white_space_around_an_address_is_skipped() -> None:
    """`DecodeBase58` skips it, so `DecodeDestination` does."""
    assert call("verifymessage", [f" \t{ADDRESS}\n", SIGNATURE, MESSAGE]) is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda sig: sig[:-1],
        lambda sig: sig + b"\x00",
        lambda sig: b"\x01" + sig[1:],
        lambda sig: b"\xff" + sig[1:],
        lambda sig: b"\x23" + sig[1:],
        lambda sig: sig[:1] + b"\x00" * 64,
        lambda sig: sig[:1] + b"\xff" * 64,
    ],
)
def test_a_signature_that_recovers_no_key_of_the_address_is_false(
    mutate: Callable[[bytes], bytes],
) -> None:
    """Whatever the bytes are, the answer is `false` and not an error."""
    signature = base64.b64encode(mutate(base64.b64decode(SIGNATURE))).decode()
    assert call("verifymessage", [ADDRESS, signature, MESSAGE]) is False


# -- the transactions


def test_every_sighash_type_is_signed_with_its_byte() -> None:
    """The last byte of the signature is the type, `DEFAULT` being `ALL`."""
    types = {
        "DEFAULT": 1,
        "ALL": 1,
        "ALL|ANYONECANPAY": 0x81,
        "NONE": 2,
        "NONE|ANYONECANPAY": 0x82,
        "SINGLE": 3,
        "SINGLE|ANYONECANPAY": 0x83,
    }
    for name, byte in types.items():
        result = call("signrawtransactionwithkey", [RAW, [KA], [PREV], name])
        witness = Tx.parse(bytes.fromhex(result["hex"])).vin[0].script_witness.stack
        assert witness[0][-1] == byte
        assert result["complete"] is True


def test_a_schnorr_signature_has_a_type_byte_unless_it_is_default() -> None:
    """BIP341: 64 bytes for `SIGHASH_DEFAULT`, 65 for another type."""
    prev = prevtx(CASES["p2tr"].script)
    sizes = {}
    for sighash in ("DEFAULT", "ALL"):
        result = call("signrawtransactionwithkey", [RAW, [KA], [prev], sighash])
        witness = Tx.parse(bytes.fromhex(result["hex"])).vin[0].script_witness.stack
        sizes[sighash] = len(witness[0])
    assert sizes == {"DEFAULT": 64, "ALL": 65}


def test_single_signs_no_input_without_an_output_of_its_number() -> None:
    """Core leaves it, and reports it as it reports any unsigned input."""
    spent = (FUNDING, 0), (b"\x02" * 32, 0)
    coins = {key: TxOut(AMOUNT, WPKH_A) for key in spent}
    result = call(
        "signrawtransactionwithkey",
        [hex_of(a_spend(*spent)), [KA], None, "SINGLE"],
        a_node(coins),
    )
    assert result["complete"] is False
    assert [e["txid"] for e in result["errors"]] == [spent[1][0].hex()]
    signed = Tx.parse(bytes.fromhex(result["hex"]))
    assert [bool(txin.script_witness.stack) for txin in signed.vin] == [True, False]


def test_an_output_is_found_on_the_chain() -> None:
    """With no `prevtxs`, as `FindCoins` reads the chain."""
    node = a_node({(FUNDING, 0): TxOut(AMOUNT, WPKH_A)})
    result = call("signrawtransactionwithkey", [RAW, [KA]], node)
    assert result["complete"] is True
    assert accepted(result["hex"], WPKH_A)


def test_an_output_is_found_in_the_mempool_before_the_chain() -> None:
    """A mempool transaction answers for its outputs, spent or not."""
    parent = a_spend((b"\x03" * 32, 0))
    node = a_node({(parent.id, 0): TxOut(5, PKH_A)}, {parent.id: parent})
    raw = hex_of(a_spend((parent.id, 0)))
    result = call("signrawtransactionwithkey", [raw, [KA]], node)
    assert result["complete"] is True
    assert accepted(result["hex"], WPKH_A, AMOUNT - 1000)


def test_an_output_the_mempool_transaction_lacks_is_not_found() -> None:
    """An output number past its last is no coin, nor the chain's."""
    parent = a_spend((b"\x03" * 32, 0))
    node = a_node({(parent.id, 1): TxOut(AMOUNT, WPKH_A)}, {parent.id: parent})
    raw = hex_of(a_spend((parent.id, 1)))
    result = call("signrawtransactionwithkey", [raw, [KA]], node)
    assert [e["error"] for e in result["errors"]] == [
        "Input not found or already spent"
    ]


def test_an_unknown_output_is_unsigned_with_its_own_error() -> None:
    """No `prevtxs` and no coin: the input, not the call, is refused."""
    result = call("signrawtransactionwithkey", [RAW, [KA]])
    assert result["complete"] is False
    assert result["hex"] == RAW
    assert [e["error"] for e in result["errors"]] == [
        "Input not found or already spent"
    ]


def test_a_redeem_script_over_65535_bytes_is_pushed_with_pushdata4() -> None:
    """`PushAll` writes it as `CScript::operator<<` does, an error at most."""
    script = b"\x61" * 70_000
    entry = prevtx(p2sh(script), {"redeemScript": script.hex()})
    result = call("signrawtransactionwithkey", [RAW, [KEY], [entry]])
    assert result["complete"] is False
    assert [e["error"] for e in result["errors"]] == ["Script is too big"]


def test_prevtxs_replace_the_amount_of_the_coin_found() -> None:
    """What a caller names is what is signed, as in Core."""
    node = a_node({(FUNDING, 0): TxOut(AMOUNT, WPKH_A)})
    prev = prevtx(WPKH_A, amount=7777)
    result = call("signrawtransactionwithkey", [RAW, [KA], [prev]], node)
    assert result["complete"] is True
    assert accepted(result["hex"], WPKH_A, 7777)


def test_prevtxs_of_another_script_than_the_coin_are_refused() -> None:
    """The two scripts are shown as `ScriptToAsmStr` writes them."""
    node = a_node({(FUNDING, 0): TxOut(AMOUNT, PKH_A)})
    key_hash = hash160(PUB_A).hex()
    assert refusal("signrawtransactionwithkey", [RAW, [KA], [PREV]], node) == (
        -22,
        (
            "Previous output scriptPubKey mismatch:\n"
            f"OP_DUP OP_HASH160 {key_hash} OP_EQUALVERIFY OP_CHECKSIG\n"
            f"vs:\n0 {key_hash}"
        ),
    )


def test_a_null_argument_is_the_default() -> None:
    """`null` for `prevtxs` and for `sighashtype` is no value."""
    node = a_node({(FUNDING, 0): TxOut(AMOUNT, WPKH_A)})
    assert call("signrawtransactionwithkey", [RAW, [KA], None, None], node)["complete"]


# -- signing in steps, and merging


def sign_with(case: Case, keys: tuple[str, ...], raw: str = RAW) -> str:
    """Sign `raw`, a spend of the output of `case`, with `keys` alone."""
    prevtxs = [prevtx(case.script, case.extra)]
    result = call("signrawtransactionwithkey", [raw, list(keys), prevtxs])
    return cast("str", result["hex"])


MULTISIGS = ["p2sh", "p2wsh", "p2sh-p2wsh", "p2sh-fifteen"]


@pytest.mark.parametrize("name", MULTISIGS)
def test_a_partial_signature_is_completed_by_another_call(name: str) -> None:
    """The second call keeps what the first signed: one call's result."""
    case = CASES[name]
    partial = sign_with(case, case.keys[:1])
    assert sign_with(case, case.keys[1:], partial) == sign_with(case, case.keys)


@pytest.mark.parametrize("name", MULTISIGS)
def test_the_halves_of_a_multisig_are_combined(name: str) -> None:
    """Each half holds some signatures and the merge holds all, in any order."""
    case = CASES[name]
    half = len(case.keys) // 2
    halves = [sign_with(case, case.keys[:half]), sign_with(case, case.keys[half:])]
    node = a_node({(FUNDING, 0): TxOut(AMOUNT, case.script)})
    whole = sign_with(case, case.keys)
    for order in (halves, halves[::-1]):
        assert call("combinerawtransaction", [order], node) == whole


@pytest.mark.parametrize("name", ["p2sh", "p2wsh"])
def test_a_complete_transaction_wins_the_merge_wherever_it_is(name: str) -> None:
    """Merging into a complete input, or from a complete one, keeps it."""
    case = CASES[name]
    whole = sign_with(case, case.keys)
    partial = sign_with(case, case.keys[:1])
    node = a_node({(FUNDING, 0): TxOut(AMOUNT, case.script)})
    assert call("combinerawtransaction", [[whole, partial]], node) == whole
    assert call("combinerawtransaction", [[partial, whole]], node) == whole


@pytest.mark.parametrize("name", ["p2sh", "p2wsh", "p2sh-p2wsh"])
def test_the_first_complete_transaction_is_the_merge(name: str) -> None:
    """Two complete ones with other signatures: the first is kept."""
    case = CASES[name]
    first = sign_with(case, (KA, KB))
    second = sign_with(case, (KB, KC))
    assert first != second
    node = a_node({(FUNDING, 0): TxOut(AMOUNT, case.script)})
    assert call("combinerawtransaction", [[first, second]], node) == first
    assert call("combinerawtransaction", [[second, first]], node) == second


def test_the_merge_of_unsigned_transactions_is_the_first() -> None:
    """Nothing to take from the others."""
    node = a_node({(FUNDING, 0): TxOut(AMOUNT, CASES["p2sh"].script)})
    assert call("combinerawtransaction", [[RAW, RAW]], node) == RAW


def test_the_merge_takes_the_output_from_the_mempool() -> None:
    """As the signing does, before the chain."""
    case = CASES["p2wsh"]
    parent = a_spend((b"\x03" * 32, 0))
    parent.vout[0] = TxOut(AMOUNT, case.script)
    node = a_node(mempool={parent.id: parent})
    raw = hex_of(a_spend((parent.id, 0)))
    prevtxs = [{**prevtx(case.script, case.extra), "txid": parent.id.hex()}]
    halves = [
        call("signrawtransactionwithkey", [raw, [key], prevtxs])["hex"]
        for key in case.keys
    ]
    assert halves[0] != halves[1]
    assert call("combinerawtransaction", [halves], node) != raw


# -- what is Core's alone to say


def test_pushes_are_the_shortest_that_hold_the_value() -> None:
    """`PushAll`: numbers for one byte of 1 to 16 and 0x81, else the length."""
    values = [b"", b"\x01", b"\x10", b"\x11", b"\x81"]
    values += [b"\xaa" * size for size in (75, 76, 255, 256, 65535, 65536)]
    assert _push_all(values) == (
        b"\x00\x51\x60\x01\x11\x4f"
        b"\x4b"
        + b"\xaa" * 75
        + b"\x4c\x4c"
        + b"\xaa" * 76
        + b"\x4c\xff"
        + b"\xaa" * 255
        + b"\x4d\x00\x01"
        + b"\xaa" * 256
        + b"\x4d\xff\xff"
        + b"\xaa" * 65535
        + b"\x4e\x00\x00\x01\x00"
        + b"\xaa" * 65536
    )


def test_the_stack_of_a_script_sig_is_where_its_evaluation_stops() -> None:
    """`Stacks` runs the pushes and the ops, to the first failure."""

    def stack(script_sig: str) -> list[str]:
        return [e.hex() for e in _script_sig_stack(bytes.fromhex(script_sig))]

    assert stack("4f5201aa4c01bb4d0100cc") == ["81", "02", "aa", "bb", "cc"]
    assert stack("5175" + "01aa") == ["aa"]
    assert stack("01aa76") == ["aa", "aa"]
    # OP_RETURN fails, and what was pushed before it is kept
    assert stack("01aa6a01bb") == ["aa"]
    # the push that runs past the end of the script fails the same way
    assert stack("01aa4c") == ["aa"]
    # a check that fails keeps its operands, and no signature is valid
    assert stack("01aa01bbac01cc") == ["aa", "bb"]
    # an op that tests the result of another leaves it false
    assert stack("01aa01bb8801cc") == [""]
    assert stack("00690161") == [""]
    # OP_PICK has popped its index where that is past the stack
    assert stack("01aa537a") == ["aa"]
    assert stack("01aa5379") == ["aa"]
    # a number of five bytes is no operand, and nothing is popped
    assert stack("050101010101" + "5193") == ["0101010101", "01"]
    # one operand short, in the first half of OP_EQUALVERIFY
    assert stack("01aa88") == ["aa"]
    assert stack("88") == []


# what `bitcoind` v31.1.0 answered for a taproot output mined to, spent
# and signed with the key of its output: the transaction, signed with
# SIGHASH_DEFAULT and with ALL, and its answers
TAPROOT_FUNDING = bytes.fromhex(
    "8888b889c774f2900b98c82fdb95d95026dbb4ac1e1fb6a78056a679423e5c44"
)
TAPROOT_VALUE = 5_000_000_000
TAPROOT_RAW = "0200000001445c3e4279a65680a7b61f1eacb4db2650d995db2fc8980b90f274c789b888880000000000fdffffff0118ee052a01000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000"
TAPROOT_DEFAULT = "02000000000101445c3e4279a65680a7b61f1eacb4db2650d995db2fc8980b90f274c789b888880000000000fdffffff0118ee052a01000000160014362995a6e6922a04e0b832a80bc56c33709a42d2014013fa78124c7b02a14a221111fa3f156bd8fa203a16f1d8537149818780372e0fd0f47d6e57b143bafe51ca79c4b917ed45711fb14f3abf05395b3c355d0c315100000000"
TAPROOT_ALL = "02000000000101445c3e4279a65680a7b61f1eacb4db2650d995db2fc8980b90f274c789b888880000000000fdffffff0118ee052a01000000160014362995a6e6922a04e0b832a80bc56c33709a42d20141f1f4945a6c379f3252f610704e3ffb511349de010eb906d90469e12be607f0e0aeaf39942a2e075ce5c5931e03e9a3e43044c2d1482442ef5a58197cf8dd8b170100000000"
# `bitcoind` v31.1.0's `signrawtransactionwithkey` of TAPROOT_ALL with the key
TAPROOT_RESIGNED = "02000000000101445c3e4279a65680a7b61f1eacb4db2650d995db2fc8980b90f274c789b888880000000000fdffffff0118ee052a01000000160014362995a6e6922a04e0b832a80bc56c33709a42d2014013fa78124c7b02a14a221111fa3f156bd8fa203a16f1d8537149818780372e0fd0f47d6e57b143bafe51ca79c4b917ed45711fb14f3abf05395b3c355d0c315100000000"


def test_a_signed_taproot_input_is_never_complete_to_the_merge() -> None:
    """`DataFromTransaction` has no transaction data to check a signature."""
    node = a_node({(TAPROOT_FUNDING, 0): TxOut(TAPROOT_VALUE, b"Q " + PUB_A[1:])})
    for pair in (
        [TAPROOT_DEFAULT, TAPROOT_RAW],
        [TAPROOT_RAW, TAPROOT_DEFAULT],
        [TAPROOT_DEFAULT, TAPROOT_DEFAULT],
        [TAPROOT_ALL, TAPROOT_DEFAULT],
    ):
        assert call("combinerawtransaction", [pair], node) == TAPROOT_RAW


def test_a_taproot_input_signed_with_all_is_signed_again_by_default() -> None:
    """Not complete, so the key signs it as an input with no signature."""
    node = a_node({(TAPROOT_FUNDING, 0): TxOut(TAPROOT_VALUE, b"Q " + PUB_A[1:])})
    resigned = call("signrawtransactionwithkey", [TAPROOT_ALL, [KA]], node)
    assert resigned == {"hex": TAPROOT_RESIGNED, "complete": True}
    assert TAPROOT_RESIGNED == TAPROOT_DEFAULT


# `bitcoind` v31.1.0's `signrawtransactionwithkey` of a spend of a P2SH
# 2-of-3 with the key A, and with the key B, and what its
# `combinerawtransaction` answers for them: A_AFTER has `OP_1 OP_DROP`
# before its script_sig, which `Stacks` evaluates
SCRIPT_SIG_FUNDING = bytes.fromhex(
    "de41dcc11fdce3c89bd54e8ccc37e24f5e8ee60fed8a15ddf62ff9c8dd5d8eda"
)
SCRIPT_SIG_RAW = "0200000001da8e5dddc8f92ff6dd158aed0fe68e5e4fe237cc8c4ed59bc8e3dc1fc1dc41de0000000000fdffffff0118ee052a01000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000"
SCRIPT_SIG_A_PREFIXED = "0200000001da8e5dddc8f92ff6dd158aed0fe68e5e4fe237cc8c4ed59bc8e3dc1fc1dc41de00000000b751750047304402207b3eebfc3bd7b9b26966cd9193bc670ae2b876972d83b97cfb8453c88f38a0970220433347c8a1f0708c9efed2226263f566982c2ebb4db3b120aa7c1747658a822c01004c69522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853aefdffffff0118ee052a01000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000"
SCRIPT_SIG_B = "0200000001da8e5dddc8f92ff6dd158aed0fe68e5e4fe237cc8c4ed59bc8e3dc1fc1dc41de00000000b50047304402206e196e41bc8c26bb8e5651b34f288903dff908b07643e454aefe50231a2c06fe02202a1a7c014fba2f4babbe4f741924552f20498ef586327ee463f89778cd7db22101004c69522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853aefdffffff0118ee052a01000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000"
SCRIPT_SIG_COMBINED = "0200000001da8e5dddc8f92ff6dd158aed0fe68e5e4fe237cc8c4ed59bc8e3dc1fc1dc41de00000000fc0047304402207b3eebfc3bd7b9b26966cd9193bc670ae2b876972d83b97cfb8453c88f38a0970220433347c8a1f0708c9efed2226263f566982c2ebb4db3b120aa7c1747658a822c0147304402206e196e41bc8c26bb8e5651b34f288903dff908b07643e454aefe50231a2c06fe02202a1a7c014fba2f4babbe4f741924552f20498ef586327ee463f89778cd7db221014c69522103774ae7f858a9411e5ef4246b70c65aac5649980be5c17891bbec17895da008cb2103d01115d548e7561b15c38f004d734633687cf4419620095bc5b0f47070afe85a2103f28773c2d975288bc7d1d205c3748651b075fbc6610e58cddeeddf8f19405aa853aefdffffff0118ee052a01000000160014362995a6e6922a04e0b832a80bc56c33709a42d200000000"


def test_the_merge_reads_a_script_sig_that_has_ops_before_its_pushes() -> None:
    """Core evaluates the script_sig, and the signatures it holds are merged."""
    node = a_node({(SCRIPT_SIG_FUNDING, 0): TxOut(TAPROOT_VALUE, CASES["p2sh"].script)})
    for pair in (
        [SCRIPT_SIG_A_PREFIXED, SCRIPT_SIG_B],
        [SCRIPT_SIG_B, SCRIPT_SIG_A_PREFIXED],
    ):
        assert call("combinerawtransaction", [pair], node) == SCRIPT_SIG_COMBINED
    assert SCRIPT_SIG_RAW != SCRIPT_SIG_COMBINED
