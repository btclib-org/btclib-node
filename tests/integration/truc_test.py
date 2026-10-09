# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A TRUC refusal names the held parent bitcoind names.

`SingleTRUCChecks` (`src/policy/truc_policy.cpp`, at
bitcoin/bitcoin@9be056a8a7) walks the held parents in the order of
`CTxMemPool::GetParents`, a `std::set<Txid>`, and refuses on the first
that breaks the version rule: the smallest txid, compared as the bytes
are stored, the reverse of the displayed hex. btclib-org/btclib-node#1783
"""

from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.hashes import sha256
from btclib.script.witness import Witness
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.p2p.address import peer_address
from tests import (
    get_random_port,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_OP_TRUE = b"\x51"
_P2WSH = bytes.fromhex("0020") + sha256(_OP_TRUE)
_WITNESS = Witness([_OP_TRUE])


def _spend(version: int, prev_outs: list[OutPoint], value: int) -> Tx:
    """Return a transaction of `version` spending each of `prev_outs`."""
    vin = [TxIn(prev_out, b"", 0xFFFFFFFD, _WITNESS) for prev_out in prev_outs]
    return Tx(version, 0, vin, [TxOut(value, _P2WSH)])


def _hex(tx: Tx) -> str:
    return tx.serialize(include_witness=True, check_validity=False).hex()


@pytest.mark.parametrize("input_order_is_txid_order", [False, True])
def test_a_refusal_names_the_parent_bitcoind_names(
    bitcoind: Bitcoind, tmp_path: Path, *, input_order_is_txid_order: bool
) -> None:
    """A version-2 child of two held version-3 parents, in either input order.

    Both parents break the rule, so the refusal names the one the order of
    the parents picks. The second parent's fee is nudged until the
    displayed txids and the stored ones order the two differently, which
    a comparison of the displayed hex would get wrong.
    """
    descriptor = cast(
        "dict[str, str]",
        bitcoind.rpc("getdescriptorinfo", [f"raw({_P2WSH.hex()})"]),
    )["descriptor"]
    bitcoind.rpc("generatetodescriptor", [2, descriptor])
    bitcoind.rpc("generatetodescriptor", [100, descriptor])
    tip = cast("int", bitcoind.rpc("getblockcount"))
    coinbases = []
    for height in (1, 2):
        block = cast(
            "dict[str, Any]",
            bitcoind.rpc("getblock", [bitcoind.rpc("getblockhash", [height]), 2]),
        )
        coinbases.append(bytes.fromhex(block["tx"][0]["txid"]))
    value = 49_99_990_000
    one = _spend(3, [OutPoint(coinbases[0], 0)], value)
    other = next(
        candidate
        for nudge in range(64)
        if (
            one.id
            < (candidate := _spend(3, [OutPoint(coinbases[1], 0)], value - nudge)).id
        )
        != (one.id[::-1] < candidate.id[::-1])
    )
    first, second = sorted([one, other], key=lambda tx: tx.id[::-1])
    assert first.id > second.id
    ordered = [first, second] if input_order_is_txid_order else [second, first]
    child = _spend(2, [OutPoint(tx.id, 0) for tx in ordered], value - 10_000)

    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
        )
    )
    node.start()
    try:
        wait_until_listening(node.p2p_manager)
        wait_until_listening(node.rpc_manager)
        node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))
        wait_until(lambda: len(node.chainstate.block_index.active_chain) == tip + 1)
        wait_until(lambda: node.status == NodeStatus.BlockSynced)
        client = rpc_client(node)
        for parent in (first, second):
            assert client.call("sendrawtransaction", [_hex(parent)]) == parent.id.hex()
            assert bitcoind.rpc("sendrawtransaction", [_hex(parent)]) == parent.id.hex()

        theirs = bitcoind.rpc("testmempoolaccept", [[_hex(child)]])
        ours = client.call("testmempoolaccept", [[_hex(child)]])
        assert cast("list[dict[str, Any]]", theirs)[0]["reject-reason"] == (
            "TRUC-violation"
        )
        assert (
            first.id.hex() in cast("list[dict[str, Any]]", theirs)[0]["reject-details"]
        )
        assert ours == theirs
    finally:
        node.stop()
        node.join()
