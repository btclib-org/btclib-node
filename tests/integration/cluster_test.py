# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`getmempoolcluster` and the chunk fields, against bitcoind's.

Both mempools hold the same transactions, and each answer is held equal
to bitcoind's: the cluster of every transaction, its entry, the feerate
diagram and `optimal`, before and after fee deltas, and the refusals.
"""

import secrets
import time
from typing import TYPE_CHECKING, Any

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.p2p.address import peer_address
from tests import get_random_port, rpc_client, wait_until, wait_until_listening
from tests.integration.prioritise_test import _FUNDED, _answer, _spend
from tests.integration.reorg_test import a_chain, submit

if TYPE_CHECKING:
    from pathlib import Path

    from btclib.tx.tx import Tx

    from tests.integration.conftest import Bitcoind

_FUND = 50 * 10**8


def _txs(fan: Tx) -> list[Tx]:
    """Return two clusters, with chunks of several transactions and ties.

    The first is a diamond whose `right` pays for `root`, and a child of
    `tip` that pays as much as `left`, so that the order of equal chunks
    is asked. The second is a chain of three at one fee each.
    """
    root = _spend([(fan.id, 0)], 2, (fan.vout[0].value - 2_000) // 2)
    left = _spend([(root.id, 0)], 1, root.vout[0].value - 5_000)
    right = _spend([(root.id, 1)], 1, root.vout[1].value - 90_000)
    tip = _spend(
        [(left.id, 0), (right.id, 0)],
        1,
        left.vout[0].value + right.vout[0].value - 3_000,
    )
    leaf = _spend([(tip.id, 0)], 1, tip.vout[0].value - 5_000)
    txs = [root, left, right, tip, leaf]
    spent, value = (fan.id, 1), fan.vout[1].value
    for _ in range(3):
        value -= 4_000
        txs.append(_spend([spent], 1, value))
        spent = (txs[-1].id, 0)
    return txs


def _agree(
    client: Any, bitcoind: Bitcoind, txs: list[Tx], sent: dict[str, range]
) -> None:
    """Assert both answer each cluster, entry and the diagram alike.

    The two nodes stamp an entry on their own clocks, so its `time` is held
    to the seconds in `sent` and every other field to bitcoind's.
    """
    for tx in txs:
        for method in ("getmempoolcluster", "getmempoolentry"):
            ours = _answer(client.call, method, [tx.id.hex()])
            theirs = _answer(bitcoind.rpc, method, [tx.id.hex()])
            if method == "getmempoolentry":
                # Core 32's fields, which bitcoind v31.1 does not answer
                del ours[1]["vsize_adjusted"], ours[1]["vsize_bip141"]
                for answer in (ours, theirs):
                    assert answer[1].pop("time") in sent[tx.id.hex()], answer
            assert ours == theirs, (method, tx.id.hex())
    for method in ("getmempoolfeeratediagram", "getmempoolinfo"):
        ours = _answer(client.call, method, [])
        theirs = _answer(bitcoind.rpc, method, [])
        if method == "getmempoolinfo":
            ours, theirs = ours[1]["optimal"], theirs[1]["optimal"]
        assert ours == theirs, method


def test_clusters_are_answered_as_bitcoind_does(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Each cluster, before and after fee deltas, and the refusals."""
    chain = a_chain(100)
    # confirmed in a block of bitcoind's, so that its outputs start two clusters
    fan = _spend([(chain[0].transactions[0].id, 0)], 2, (_FUND - 2_000) // 2)
    txs = _txs(fan)
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
        raw = fan.serialize(include_witness=True).hex()
        assert bitcoind.rpc("sendrawtransaction", [raw]) == fan.id.hex()
        info: Any = bitcoind.rpc("getdescriptorinfo", [f"raw({_FUNDED.hex()})"])
        bitcoind.rpc("generatetodescriptor", [1, info["descriptor"]])
        wait_until(lambda: len(block_index.active_chain) == len(chain) + 2)
        client = rpc_client(node)
        sent: dict[str, range] = {}
        for tx in txs:
            raw = tx.serialize(include_witness=True).hex()
            first = int(time.time())
            assert client.call("sendrawtransaction", [raw]) == tx.id.hex()
            assert bitcoind.rpc("sendrawtransaction", [raw]) == tx.id.hex()
            sent[tx.id.hex()] = range(first, int(time.time()) + 1)
        _agree(client, bitcoind, txs, sent)

        for tx, delta in [(txs[1], 200_000), (txs[6], -3_000), (txs[0], 1_000)]:
            params: list[object] = [tx.id.hex(), None, delta]
            assert client.call("prioritisetransaction", params) is True
            assert bitcoind.rpc("prioritisetransaction", params) is True
            _agree(client, bitcoind, txs, sent)

        refusals: list[list[object]] = [
            [],
            [secrets.token_hex(32)],
            ["foo"],
            [12],
            [None],
            ["aa"],
        ]
        for refused in refusals:
            ours = _answer(client.call, "getmempoolcluster", refused)
            assert ours == _answer(bitcoind.rpc, "getmempoolcluster", refused), refused
    finally:
        node.stop()
        node.join()
