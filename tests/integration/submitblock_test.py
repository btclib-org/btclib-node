# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`submitblock`'s answers, held equal to bitcoind's for the same blocks.

Each case hands the same headers and blocks, in the same order, to
bitcoind and to the node, each over its own RPC, and holds both answers
to the one Core gives, then both tips to the one Core ends on. The two
are not connected to each other, so each answer is that daemon's own.
"""

from typing import TYPE_CHECKING

import pytest
from btclib.block import Block, merkle_root_and_mutated_from_transactions
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from tests import brute_force_nonce, get_random_port, rpc_client, wait_until_listening
from tests.integration.reorg_test import a_block, a_chain

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

# what each daemon is asked, the block it is asked about, and Core's
# answer: `submitheader` sends the block's header alone
type Step = tuple[str, Block, str | None]


def _greedy(previous_block_hash: bytes, height: int) -> Block:
    """Return a block whose coinbase claims a satoshi past the subsidy.

    `bad-cb-amount` is a rule of `ConnectBlock`, so the block is stored
    and refused only once something tries to connect it.
    """
    block = a_block(previous_block_hash, height, funding=True)
    coinbase = block.transactions[0]
    paid = coinbase.vout[0]
    coinbase.vout[0] = TxOut(paid.value + 1, paid.script_pub_key)
    header = block.header
    header.merkle_root = merkle_root_and_mutated_from_transactions(block.transactions)[
        0
    ]
    brute_force_nonce(header)
    return block


def _a_tie_then_its_child() -> tuple[list[Step], Block]:
    a1, a2 = a_chain(2)
    rival = a_block(a1.header.hash, 2)
    child = a_block(rival.header.hash, 3)
    steps: list[Step] = [
        ("submitblock", a1, None),
        ("submitblock", a2, None),
        ("submitblock", rival, "inconclusive"),
        ("submitblock", child, None),
    ]
    return steps, child


def _a_rival_whose_child_fails_after_it_connects() -> tuple[list[Step], Block]:
    a1, a2 = a_chain(2)
    rival = a_block(a1.header.hash, 2)
    greedy = _greedy(rival.header.hash, 3)
    steps: list[Step] = [
        ("submitblock", a1, None),
        ("submitblock", a2, None),
        ("submitheader", rival, None),
        ("submitblock", greedy, "inconclusive"),
        ("submitblock", rival, None),
    ]
    return steps, a2


def _a_heavier_block_whose_parent_has_no_data() -> tuple[list[Step], Block]:
    a1, a2 = a_chain(2)
    rival = a_block(a1.header.hash, 2)
    child = a_block(rival.header.hash, 3)
    steps: list[Step] = [
        ("submitblock", a1, None),
        ("submitblock", a2, None),
        ("submitheader", rival, None),
        ("submitblock", child, "inconclusive"),
    ]
    return steps, a2


def _a_bad_tie_then_its_child() -> tuple[list[Step], Block]:
    a1, a2 = a_chain(2)
    greedy = _greedy(a1.header.hash, 2)
    child = a_block(greedy.header.hash, 3)
    steps: list[Step] = [
        ("submitblock", a1, None),
        ("submitblock", a2, None),
        ("submitblock", greedy, "inconclusive"),
        ("submitblock", child, "inconclusive"),
    ]
    return steps, a2


def _a_valid_rival_then_a_greedy_child() -> tuple[list[Step], Block]:
    a1, a2 = a_chain(2)
    rival = a_block(a1.header.hash, 2)
    greedy = _greedy(rival.header.hash, 3)
    steps: list[Step] = [
        ("submitblock", a1, None),
        ("submitblock", a2, None),
        ("submitblock", rival, "inconclusive"),
        ("submitblock", greedy, "bad-cb-amount"),
    ]
    return steps, a2


def _a_reorg_three_deep() -> tuple[list[Step], Block]:
    chain = a_chain(3)
    branch: list[Block] = []
    for height in range(1, 5):
        previous = branch[-1].header.hash if branch else RegTest().genesis.hash
        branch.append(a_block(previous, height))
    steps: list[Step] = [("submitblock", block, None) for block in chain]
    steps += [("submitblock", block, "inconclusive") for block in branch[:3]]
    steps.append(("submitblock", branch[3], None))
    return steps, branch[3]


@pytest.mark.parametrize(
    "case",
    [
        _a_tie_then_its_child,
        _a_rival_whose_child_fails_after_it_connects,
        _a_heavier_block_whose_parent_has_no_data,
        _a_bad_tie_then_its_child,
        _a_valid_rival_then_a_greedy_child,
        _a_reorg_three_deep,
    ],
)
def test_the_node_answers_as_bitcoind_does(
    bitcoind: Bitcoind,
    tmp_path: Path,
    case: Callable[[], tuple[list[Step], Block]],
) -> None:
    """Each step is answered alike, and both end on Core's tip.

    `"inconclusive"` is Core's answer for a block nothing connects, a
    tie included, and `None` for one a step connects, even where a later
    block of the same branch then fails and the tip goes back
    (btclib-org/btclib-node#2419).
    """
    steps, tip = case()
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
        for method, block, answer in steps:
            if method == "submitheader":
                raw = block.header.serialize().hex()
            else:
                raw = block.serialize(check_validity=False).hex()
            answers = (bitcoind.rpc(method, [raw]), client.call(method, [raw]))
            assert answers == (answer, answer), (method, block.header.hash.hex())
        tips = (bitcoind.rpc("getbestblockhash"), client.call("getbestblockhash"))
        assert tips == (tip.header.hash.hex(), tip.header.hash.hex())
    finally:
        node.stop()
        node.join()
