# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`prioritisetransaction` and `getprioritisedtransactions` against bitcoind's.

Each call is answered by this node and by a real bitcoind, and the answers
are held equal: the refusals with their codes and words, and the list of
deltas with its order. A second test holds a diamond of transactions in both
mempools and compares their fees after each delta and after a block.
"""

import secrets
from typing import TYPE_CHECKING, Any, cast

from bitcoin_core_rpc import RpcError
from btclib.hashes import hash160
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.p2p.address import peer_address
from tests import get_random_port, rpc_client, wait_until, wait_until_listening
from tests.integration.reorg_test import a_chain, submit

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_TXID = "1d1d4e24ed99057e84c3f80fd8fbec79ed9e1acee37da269356ecea000000000"
_MAX, _MIN = 2**63 - 1, -(2**63)

_CALLS: list[list[Any]] = [
    [],
    [_TXID],
    [_TXID, 0],
    [_TXID, 0, 5, 7],
    ["", None, 5],
    ["foo", None, 5],
    ["Z" + _TXID[1:], None, 5],
    [12, None, 5],
    [None, None, 5],
    [_TXID, "foo", 5],
    ["foo", "x", "y"],
    [_TXID, None, "foo"],
    [_TXID, None, None],
    [_TXID, None, True],
    [_TXID, None, [5]],
    [_TXID, 1, 5],
    [_TXID, -1, 5],
    [_TXID, 0.5, 5],
    [_TXID, 1, 1.5],
    ["foo", 1, 1.5],
    [_TXID, None, 1.5],
    [_TXID, None, 5.0],
    [_TXID, None, 1e3],
    [_TXID, None, _MAX + 1],
    [_TXID, None, _MIN - 1],
    [_TXID, None, 5],
    [_TXID, 0, -10],
    [_TXID, 0.0, 1],
    [_TXID, -0.0, 0],
    [_TXID, None, _MAX],
    [_TXID, None, 5],
    [_TXID, None, _MIN],
    [_TXID, None, -5],
    [_TXID, None, 4],
    [_TXID, None, 1],
]


def _answer(call: Callable[[str, list[Any]], Any], method: str, params: Any) -> Any:
    """Return what `call` answers, or the error as its code and message."""
    try:
        return ("result", call(method, params))
    except RpcError as error:
        # past the url, which names each side's own port
        return ("error", error.code, error.args[0].split(": ", 1)[1])


def test_prioritisetransaction_answers_as_bitcoind_does(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Each call is answered alike, and so is the list of deltas after it.

    The calls cover the arguments' types, their order of refusal, the
    `int64` range with its saturation, and a delta that returns to zero.
    The same is asked by name, and with random txids for the list's order.
    """
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
        wait_until_listening(node.rpc_manager)
        client = rpc_client(node)
        calls: list[tuple[str, Any]] = [
            ("prioritisetransaction", params) for params in _CALLS
        ]
        calls += [
            (
                "prioritisetransaction",
                {"txid": _TXID, "dummy": 1, "fee_delta": 3},
            ),
            ("prioritisetransaction", {"txid": _TXID, "fee_delta": 3}),
            ("prioritisetransaction", {"txid": _TXID, "fee_delta": 3, "foo": 1}),
            ("getprioritisedtransactions", []),
            ("getprioritisedtransactions", [True]),
        ]
        txids = [secrets.token_hex(32) for _ in range(8)]
        calls += [
            ("prioritisetransaction", [txid, None, number])
            for number, txid in enumerate(txids, start=1)
        ]
        calls.append(("getprioritisedtransactions", []))
        for method, params in calls:
            ours = _answer(client.call, method, params)
            theirs = _answer(bitcoind.rpc, method, params)
            assert ours == theirs, (method, params)
        listed = _answer(client.call, "getprioritisedtransactions", [])[1]
        assert list(listed) == list(
            _answer(bitcoind.rpc, "getprioritisedtransactions", [])[1]
        )
    finally:
        node.stop()
        node.join()


# P2SH over `OP_DROP OP_1` and the input that spends it, which
# `reorg_test.py` explains: standard, and satisfied by anyone.
_REDEEM = script.serialize(["OP_DROP", "OP_1"])
_FUNDED = script.serialize(["OP_HASH160", hash160(_REDEEM), "OP_EQUAL"])
_SPENDS_IT = script.serialize([b"\x11" * 32, _REDEEM])

_FUND = 50 * 10**8
_FEE = 10_000


def _spend(spent: list[tuple[bytes, int]], outputs: int, value: int) -> Tx:
    """Return a spend of `spent` paying `value` to each of `outputs`."""
    return Tx(
        version=2,
        lock_time=0,
        vin=[
            TxIn(OutPoint(txid, vout), _SPENDS_IT, 0xFFFFFFFF) for txid, vout in spent
        ],
        vout=[TxOut(value, _FUNDED) for _ in range(outputs)],
    )


def _agree(client: Any, bitcoind: Bitcoind, txs: list[Tx]) -> None:
    """Assert both hold `txs` with the same fees, and list the same deltas."""
    for tx in txs:
        ours = client.call("getmempoolentry", [tx.id.hex()])["fees"]
        theirs = cast("Any", bitcoind.rpc("getmempoolentry", [tx.id.hex()]))["fees"]
        theirs.pop("chunk")
        assert ours == theirs, tx.id.hex()
    listed = client.call("getprioritisedtransactions", [])
    assert listed == bitcoind.rpc("getprioritisedtransactions", [])
    assert list(listed) == list(cast("Any", bitcoind.rpc("getprioritisedtransactions")))
    assert sorted(client.call("getrawmempool", [])) == sorted(
        cast("Any", bitcoind.rpc("getrawmempool", []))
    )


def test_a_mempool_answers_with_the_deltas_as_bitcoind_does(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """A diamond of held transactions, with deltas given before and after.

    Both hold `root`, `left`, `right` and `tip`, as Core's
    `mining_prioritisetransaction.py` `test_diamond` does. The fees of
    each entry, with the ancestor and descendant sums, and the deltas
    listed are held equal to bitcoind's after each step, and a block
    that confirms the four leaves no delta of theirs in either.
    """
    chain = a_chain(100)
    root = _spend([(chain[0].transactions[0].id, 0)], 2, (_FUND - _FEE) // 2)
    left = _spend([(root.id, 0)], 1, root.vout[0].value - _FEE)
    right = _spend([(root.id, 1)], 1, root.vout[1].value - _FEE)
    tip = _spend([(left.id, 0), (right.id, 0)], 1, left.vout[0].value * 2 - _FEE)
    txs = [root, left, right, tip]
    submit(bitcoind, chain)

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
        block_index = node.chainstate.block_index
        wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)
        client = rpc_client(node)
        for tx in txs:
            raw = tx.serialize(include_witness=True).hex()
            assert client.call("sendrawtransaction", [raw]) == tx.id.hex()
            assert bitcoind.rpc("sendrawtransaction", [raw]) == tx.id.hex()
        _agree(client, bitcoind, txs)

        unheld = secrets.token_hex(32)
        deltas = [
            (left.id.hex(), 9_999),
            (right.id.hex(), -1_234),
            (right.id.hex(), 8_888),
            (tip.id.hex(), 100),
            (unheld, 77),
        ]
        for txid, delta in deltas:
            params: list[object] = [txid, None, delta]
            assert client.call("prioritisetransaction", params) is True
            assert bitcoind.rpc("prioritisetransaction", params) is True
            _agree(client, bitcoind, txs)

        info = cast("Any", bitcoind.rpc("getdescriptorinfo", [f"raw({_FUNDED.hex()})"]))
        bitcoind.rpc("generatetodescriptor", [1, info["descriptor"]])
        wait_until(lambda: len(block_index.active_chain) == len(chain) + 2)
        assert block_index.active_chain[-1].hex() == bitcoind.rpc("getbestblockhash")
        assert client.call("getrawmempool", []) == []
        listed = client.call("getprioritisedtransactions", [])
        assert list(listed) == [unheld]
        assert listed == bitcoind.rpc("getprioritisedtransactions", [])
    finally:
        node.stop()
        node.join()
