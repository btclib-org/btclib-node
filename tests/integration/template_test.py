# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`getblocktemplate`'s transactions, against bitcoind's.

Both mempools hold the same clusters, one of them taken in another order
by chunk than by ancestor package. The template's transactions, in order
and with their fees, and its coinbase value are held equal to bitcoind's,
before and after fee deltas.
"""

import time
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.p2p.address import peer_address
from tests import get_random_port, rpc_client, wait_until, wait_until_listening
from tests.integration.cluster_test import _txs
from tests.integration.prioritise_test import _FUNDED, _spend
from tests.integration.reorg_test import a_chain, submit

if TYPE_CHECKING:
    from pathlib import Path

    from btclib.tx.tx import Tx

    from tests.integration.conftest import Bitcoind

_FUND = 50 * 10**8
_RULES: list[object] = [{"rules": ["segwit"]}]


def _ahead(fan: Tx) -> tuple[list[Tx], Tx]:
    """Return a parent with two children, and a transaction on its own.

    The three are one chunk, and `alone` pays a feerate between it and the
    parent with one child: an ancestor package would take `alone` first.
    """
    value = fan.vout[2].value
    parent = _spend([(fan.id, 2)], 2, (value - 200) // 2)
    children = [
        _spend([(parent.id, vout)], 1, parent.vout[vout].value - 10_000)
        for vout in (0, 1)
    ]
    alone = _spend([(fan.id, 3)], 1, fan.vout[3].value - 5_300)
    one = Fraction(200 + 10_000, parent.vsize + children[0].vsize)
    both = Fraction(200 + 20_000, parent.vsize + children[0].vsize + children[1].vsize)
    assert one < Fraction(5_300, alone.vsize) < both
    return [parent, *children], alone


def _agree(client: Any, bitcoind: Bitcoind) -> list[str]:
    """Assert both templates hold the same transactions; return their txids."""
    ours = client.call("getblocktemplate", _RULES)
    theirs: Any = bitcoind.rpc("getblocktemplate", _RULES)
    assert ours["transactions"] == theirs["transactions"]
    assert ours["coinbasevalue"] == theirs["coinbasevalue"]
    return [tx["txid"] for tx in ours["transactions"]]


def test_a_template_takes_the_chunks_bitcoind_takes(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """The template's transactions and fees, before and after deltas."""
    chain = a_chain(100)
    # confirmed in a block of bitcoind's, so that its outputs start clusters
    fan = _spend([(chain[0].transactions[0].id, 0)], 4, (_FUND - 4_000) // 4)
    cluster, alone = _ahead(fan)
    txs = [*_txs(fan), *cluster, alone]
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
        for tx in txs:
            raw = tx.serialize(include_witness=True).hex()
            assert client.call("sendrawtransaction", [raw]) == tx.id.hex()
            assert bitcoind.rpc("sendrawtransaction", [raw]) == tx.id.hex()

        order = _agree(client, bitcoind)
        assert sorted(order) == sorted(tx.id.hex() for tx in txs)
        assert order.index(cluster[-1].id.hex()) < order.index(alone.id.hex())

        # bitcoind builds a new template only once five seconds have passed
        mock = int(time.time())
        for tx, delta in [(cluster[2], -9_900), (txs[1], 200_000), (txs[6], -3_000)]:
            params: list[object] = [tx.id.hex(), None, delta]
            assert client.call("prioritisetransaction", params) is True
            assert bitcoind.rpc("prioritisetransaction", params) is True
            mock += 6
            bitcoind.rpc("setmocktime", [mock])
            _agree(client, bitcoind)
    finally:
        node.stop()
        node.join()
