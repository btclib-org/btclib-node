# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`getmempoolcluster` and the chunk fields, against bitcoind's.

Both mempools hold the same transactions, and each answer is held equal
to bitcoind's: the cluster of every transaction, its entry, the feerate
diagram and `optimal`, before and after fee deltas, and the refusals.
Both mempools are trimmed alike by a reorg that passes the limits.
"""

import secrets
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.p2p.address import peer_address
from tests import get_random_port, rpc_client, wait_until, wait_until_listening
from tests.integration.prioritise_test import _FUNDED, _answer, _spend
from tests.integration.reorg_test import a_chain, submit

if TYPE_CHECKING:
    from collections.abc import Iterator
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


def _a_chain_on(spent: tuple[bytes, int], value: int, fee: int, n: int) -> list[Tx]:
    """Return `n` transactions, each spending the last, from `spent`."""
    txs: list[Tx] = []
    for _ in range(n):
        value -= fee
        txs.append(_spend([spent], 1, value))
        spent = (txs[-1].id, 0)
    return txs


class _Bitcoind:
    """bitcoind with blocks paying `_FUNDED`, past the regtest chain."""

    def __init__(self, bitcoind: Bitcoind) -> None:
        self.rpc = bitcoind.rpc
        self.p2p_port = bitcoind.p2p_port
        self.chain = a_chain(100)
        submit(bitcoind, self.chain)
        info: Any = bitcoind.rpc("getdescriptorinfo", [f"raw({_FUNDED.hex()})"])
        self.descriptor = info["descriptor"]

    def mine(self, txs: list[Tx], *, submit: bool = True) -> dict[str, str]:
        """Mine a block of `txs`: its hash, and its hex if not submitted."""
        raws = [tx.serialize(include_witness=True).hex() for tx in txs]
        params: list[object] = [self.descriptor, raws, submit]
        return cast("dict[str, str]", self.rpc("generateblock", params))


@contextmanager
def _a_node(bitcoind: _Bitcoind, tmp_path: Path, tip: str) -> Iterator[Node]:
    """Give a node synced to bitcoind's `tip`, and stop it after."""
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
        _wait_for(node, tip)
        yield node
    finally:
        node.stop()
        node.join()


def _wait_for(node: Node, tip: str) -> None:
    block_index = node.chainstate.block_index
    wait_until(lambda: block_index.active_chain[-1].hex() == tip)


def _send(client: Any, bitcoind: _Bitcoind, txs: list[Tx]) -> None:
    for tx in txs:
        raw = tx.serialize(include_witness=True).hex()
        assert client.call("sendrawtransaction", [raw]) == tx.id.hex()
        assert bitcoind.rpc("sendrawtransaction", [raw]) == tx.id.hex()


def _assert_both_hold(client: Any, bitcoind: _Bitcoind, kept: list[Tx]) -> None:
    expected = sorted(tx.id.hex() for tx in kept)
    assert sorted(cast("Any", bitcoind.rpc("getrawmempool", []))) == expected
    assert sorted(client.call("getrawmempool", [])) == expected


def test_a_reorg_trims_a_cluster_as_bitcoind_does(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Two parents a reorg puts back under the chains that spend them.

    `p` has a chain of 64: its last would be the 65th. `q` has a chain
    of 30 at a low fee, sent first, and one of 40 at a high fee, and
    Core's `Trim` keeps the high one whole.
    """
    core = _Bitcoind(bitcoind)
    fan = _spend([(core.chain[0].transactions[0].id, 0)], 2, (_FUND - 2_000) // 2)
    p = _spend([(fan.id, 0)], 1, fan.vout[0].value - 1_000)
    q = _spend([(fan.id, 1)], 2, (fan.vout[1].value - 1_000) // 2)
    on_p = _a_chain_on((p.id, 0), p.vout[0].value, 1_000, 64)
    low = _a_chain_on((q.id, 0), q.vout[0].value, 1_000, 30)
    high = _a_chain_on((q.id, 1), q.vout[1].value, 5_000, 40)
    core.mine([fan])
    confirmed = core.mine([p, q])["hash"]
    with _a_node(core, tmp_path, confirmed) as node:
        client = rpc_client(node)
        _send(client, core, on_p + low + high)
        core.rpc("invalidateblock", [confirmed])
        core.mine([])
        _wait_for(node, core.mine([])["hash"])
        _assert_both_hold(client, core, [p, q, *on_p[:63], *low[:23], *high])


def test_a_transaction_the_new_chain_confirms_is_not_counted(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """The trim reads the clusters after the new blocks leave the mempool.

    `x` and `p` spend `fan`. A chain of 63 spends `p`, its second
    transaction spending `x` too. `p` is confirmed, and the reorg goes to
    a block confirming `x`: `p` and the chain are 64, which both keep.
    """
    core = _Bitcoind(bitcoind)
    fan = _spend([(core.chain[0].transactions[0].id, 0)], 2, (_FUND - 2_000) // 2)
    x = _spend([(fan.id, 0)], 1, fan.vout[0].value - 1_000)
    p = _spend([(fan.id, 1)], 1, fan.vout[1].value - 1_000)
    first = _spend([(p.id, 0)], 1, p.vout[0].value - 1_000)
    value = first.vout[0].value + x.vout[0].value - 1_000
    second = _spend([(first.id, 0), (x.id, 0)], 1, value)
    on_p = [
        first,
        second,
        *_a_chain_on((second.id, 0), second.vout[0].value, 1_000, 61),
    ]
    core.mine([fan])
    other = core.mine([x], submit=False)
    confirmed = core.mine([p])["hash"]
    with _a_node(core, tmp_path, confirmed) as node:
        client = rpc_client(node)
        _send(client, core, [x, *on_p])
        # stored beside the tip, at its height
        assert core.rpc("submitblock", [other["hex"]]) == "inconclusive"
        core.rpc("preciousblock", [other["hash"]])
        _wait_for(node, core.mine([])["hash"])
        _assert_both_hold(client, core, [p, *on_p])


def test_a_re_added_child_counts_no_held_child_of_its_parent(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """A reorg puts back `p` and `r`, which spends `p`'s second output.

    A chain of 64 spends `p`'s first output. `r` is checked against `p`
    alone, as Core's graph links `p` to the chain only after every
    re-add, and `r` pays more than the chain, so the trim keeps it.
    """
    core = _Bitcoind(bitcoind)
    coinbase = core.chain[0].transactions[0]
    p = _spend([(coinbase.id, 0)], 2, (_FUND - 2_000) // 2)
    r = _spend([(p.id, 1)], 1, p.vout[1].value - 5_000)
    on_p = _a_chain_on((p.id, 0), p.vout[0].value, 1_000, 64)
    confirmed = core.mine([p, r])["hash"]
    with _a_node(core, tmp_path, confirmed) as node:
        client = rpc_client(node)
        _send(client, core, on_p)
        core.rpc("invalidateblock", [confirmed])
        core.mine([])
        _wait_for(node, core.mine([])["hash"])
        _assert_both_hold(client, core, [p, r, *on_p[:62]])
