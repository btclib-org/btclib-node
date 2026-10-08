# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The fee estimates and `fee_estimates.dat`, against bitcoind's.

Both nodes are given the same transactions, by `sendrawtransaction`, and
the same blocks, mined by bitcoind. After each block both answer every
estimate alike. Then each is stopped, the two files are compared, and
each file is read by a fresh node of the other kind.
"""

import random
from contextlib import ExitStack
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.p2p.address import peer_address
from tests import get_random_port, rpc_client, wait_until, wait_until_listening
from tests.integration.conftest import _started_bitcoind
from tests.integration.prioritise_test import _FUND, _FUNDED, _answer, _spend
from tests.integration.reorg_test import a_chain, submit
from tests.unit.fee_estimator_test import _CORE_EMPTY_FILE_SHA256

if TYPE_CHECKING:
    from pathlib import Path

    from btclib.tx.tx import Tx

    from tests.integration.conftest import Bitcoind

_ROUNDS = 30
_PER_ROUND = 20
# sat/vB: what a sender pays, and the lowest rate each block takes in turn
_RATES = (1, 2, 3, 5, 8, 12, 20, 35, 60, 100)
_CUTOFFS = (5, 12, 3, 20, 8, 35)

_SMART = [
    [target, *mode]
    for target in (1, 2, 3, 5, 6, 10, 12, 24, 48, 100, 500, 1008)
    for mode in ([], ["economical"], ["conservative"])
]
_RAW = [
    [target, *threshold]
    for target in (1, 2, 6, 12, 13, 24, 48, 49, 144, 500, 1008)
    for threshold in ([], [0.5], [0.85], [0.0], [1.0])
]


def _calls() -> list[tuple[str, list[Any]]]:
    return [("estimatesmartfee", p) for p in _SMART] + [
        ("estimaterawfee", p) for p in _RAW
    ]


def _answers(call: Any) -> list[Any]:
    return [_answer(call, method, params) for method, params in _calls()]


def _start_node(data_dir: Path, *, p2p: bool = True) -> Node:
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=data_dir,
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            allow_p2p=p2p,
        )
    )
    node.start()
    wait_until_listening(node.rpc_manager)
    return node


def _stop(node: Node) -> None:
    node.stop()
    node.join()


def _paying(spent: tuple[bytes, int], value: int, rate: int) -> Tx:
    """Return a spend of `spent` paying `rate` sat/vB."""
    unpaid = _spend([spent], 1, value)
    return _spend([spent], 1, value - rate * unpaid.vsize)


def _holds(client: Any, pool: list[str]) -> bool:
    """Whether the node's mempool is `pool`, sorted."""
    return sorted(client.call("getrawmempool", [])) == pool


def _mined_descriptor(bitcoind: Bitcoind) -> str:
    info = cast("Any", bitcoind.rpc("getdescriptorinfo", [f"raw({_FUNDED.hex()})"]))
    return str(info["descriptor"])


def test_estimates_and_their_file_are_bitcoind_s(  # noqa: PLR0915
    bitcoind_path: str, tmp_path: Path
) -> None:
    """Every estimate after every block, then each file read by the other."""
    chain = a_chain(100)
    fan_out = _spend(
        [(chain[0].transactions[0].id, 0)],
        _ROUNDS * _PER_ROUND,
        (_FUND - 200_000) // (_ROUNDS * _PER_ROUND),
    )
    coins = [(fan_out.id, vout) for vout in range(len(fan_out.vout))]
    value = fan_out.vout[0].value
    rng = random.Random(1543)
    rates: dict[str, int] = {}

    with ExitStack() as stack:
        bitcoind = stack.enter_context(
            _started_bitcoind(bitcoind_path, tmp_path / "first")
        )
        submit(bitcoind, chain)
        node = _start_node(tmp_path / "node")
        stack.callback(_stop, node)
        wait_until_listening(node.p2p_manager)
        node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))
        block_index = node.chainstate.block_index
        client = rpc_client(node)
        descriptor = _mined_descriptor(bitcoind)

        def mine(txids: list[str]) -> None:
            bitcoind.rpc("generateblock", [descriptor, txids])
            tip = bytes.fromhex(str(bitcoind.rpc("getbestblockhash")))
            wait_until(lambda: block_index.active_chain[-1] == tip)

        def agree() -> list[Any]:
            ours = _answers(client.call)
            assert ours == _answers(bitcoind.rpc)
            return ours

        def send(tx: Tx) -> None:
            raw = tx.serialize(include_witness=True).hex()
            assert client.call("sendrawtransaction", [raw]) == tx.id.hex()
            assert bitcoind.rpc("sendrawtransaction", [raw]) == tx.id.hex()

        wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)
        send(fan_out)
        mine([fan_out.id.hex()])
        estimated = 0
        for round_number in range(_ROUNDS):
            for _ in range(_PER_ROUND):
                rate = rng.choice(_RATES)
                tx = _paying(coins.pop(), value, rate)
                send(tx)
                rates[tx.id.hex()] = rate
            pool = sorted(cast("list[str]", bitcoind.rpc("getrawmempool")))
            wait_until(partial(_holds, client, pool))
            cutoff = _CUTOFFS[round_number % len(_CUTOFFS)]
            mine([txid for txid in pool if rates[txid] >= cutoff])
            answers = agree()
            estimated += sum("feerate" in str(answer) for answer in answers)
        # the comparison is not between two empty answers
        assert estimated > 0
        held = cast("list[str]", bitcoind.rpc("getrawmempool"))
        assert held
        stack.close()

    theirs = tmp_path / "first" / "bitcoind" / "regtest" / "fee_estimates.dat"
    ours = node.data_dir / "fee_estimates.dat"
    assert ours.read_bytes() == theirs.read_bytes()
    assert sha256(ours.read_bytes()).hexdigest() != _CORE_EMPTY_FILE_SHA256

    # each file read by a fresh node of the other kind: the same estimates
    swapped = tmp_path / "second" / "bitcoind" / "regtest"
    swapped.mkdir(parents=True)
    (swapped / "fee_estimates.dat").write_bytes(ours.read_bytes())
    (tmp_path / "fresh" / "regtest").mkdir(parents=True)
    (tmp_path / "fresh" / "regtest" / "fee_estimates.dat").write_bytes(
        theirs.read_bytes()
    )
    with ExitStack() as stack:
        bitcoind = stack.enter_context(
            _started_bitcoind(bitcoind_path, tmp_path / "second")
        )
        fresh = _start_node(tmp_path / "fresh", p2p=False)
        stack.callback(_stop, fresh)
        answers = _answers(rpc_client(fresh).call)
        assert answers == _answers(bitcoind.rpc)
        assert any("feerate" in str(answer) for answer in answers)
