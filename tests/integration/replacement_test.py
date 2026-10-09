# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Replacing a held transaction, against bitcoind's.

Both nodes hold the same chain and mempool and are asked the same
`testmempoolaccept` and `sendrawtransaction`, and the answers are held
equal: each rule's refusal with its words, and each replacement. The
mempools are held equal after each scenario, so a transaction one
replaced and the other kept is found where it happens.
"""

from typing import TYPE_CHECKING, Any

from tests import rpc_client
from tests.integration.submitpackage_test import (
    Ctx,
    _coin,
    _hex,
    _pay,
    _spend,
    a_node,
    fund,
    run,
    sync,
)
from tests.integration.testmempoolaccept_test import asked

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from btclib.tx.tx import Tx

    from tests.integration.conftest import Bitcoind


def sent(ctx: Ctx, tx: Tx) -> Any:
    """Ask both `testmempoolaccept`, then `sendrawtransaction`, of `tx`."""
    asked(ctx, [tx])
    return ctx.agree("sendrawtransaction", [_hex(tx)])


def the_issue_s_replacement(ctx: Ctx) -> None:
    """Replace a spend paying 10000 with one paying 11000."""
    coin = ctx.coin()
    held = _pay(coin, 10_000)
    ctx.send(held)
    rival = _pay(coin, 11_000)
    assert sent(ctx, rival) == ("result", rival.id.hex())


def rules_3_and_4(ctx: Ctx) -> None:
    """Less than the conflict pays, then less than the relay of the increase."""
    coin = ctx.coin()
    ctx.send(_pay(coin, 10_000))
    for fee in [9_999, 10_005]:
        answer = sent(ctx, _pay(coin, fee))
        assert answer[0] == "error", answer


def a_conflict_with_a_descendant(ctx: Ctx) -> None:
    """Count the descendant's fee, and replace it with the conflict."""
    coin = ctx.coin()
    held = _pay(coin, 10_000)
    ctx.send(held, _pay(_coin(held), 10_000))
    assert sent(ctx, _pay(coin, 15_000))[0] == "error"
    rival = _pay(coin, 25_000)
    assert sent(ctx, rival) == ("result", rival.id.hex())


def a_spend_of_its_own_conflict(ctx: Ctx) -> None:
    """Core's `bad-txns-spends-conflicting-tx`, once the fees pass."""
    coin = ctx.coin()
    held = _pay(coin, 1_000, outputs=2)
    ctx.send(held)
    spent = [coin, _coin(held, 1)]
    rival = _spend(spent, [sum(each.value for each in spent) - 50_000])
    assert sent(ctx, rival)[0] == "error"


def a_parent_that_dilutes_the_replacement(ctx: Ctx) -> None:
    """Refuse what a held parent's chunk dilutes, and take it alone.

    The diluted candidate's own feerate is above the conflict's: only its
    chunk with the parent is below it.
    """
    coin = ctx.coin()
    parent = _pay(ctx.coin(), 200)
    held = _pay(coin, 10_000)
    ctx.send(held, parent)
    spent = [coin, _coin(parent)]
    value = sum(each.value for each in spent)
    # by weight, which the diagram compares: by vsize, rounding up can
    # leave the candidate's own feerate under the conflict's
    fee = 10_000 * _spend(spent, [value]).weight // held.weight + 100
    diluted = _spend(spent, [value - fee])
    assert sent(ctx, diluted)[0] == "error"
    alone = _pay(coin, 10_100)
    assert sent(ctx, alone) == ("result", alone.id.hex())


def too_many_clusters(ctx: Ctx) -> None:
    """101 conflicting clusters are too many, 100 are not."""
    fan = _pay(ctx.coin(), 20_000, outputs=101)
    ctx.send(fan)
    ctx.mine()
    coins = [_coin(fan, vout) for vout in range(101)]
    ctx.send(*(_pay(coin, 1_000) for coin in coins))
    value = sum(coin.value for coin in coins)
    assert sent(ctx, _spend(coins, [value - 200_000]))[0] == "error"
    rival = _spend(coins[:100], [value - coins[100].value - 200_000])
    assert sent(ctx, rival) == ("result", rival.id.hex())


def a_second_child_of_a_version_3_parent(ctx: Ctx) -> None:
    """Sibling eviction: short of the fees, then replacing the first child."""
    parent = _pay(ctx.coin(), 2_000, outputs=2, version=3)
    ctx.send(parent, _pay(_coin(parent, 0), 1_000, version=3))
    assert sent(ctx, _pay(_coin(parent, 1), 1_000, version=3))[0] == "error"
    second = _pay(_coin(parent, 1), 5_000, version=3)
    assert sent(ctx, second) == ("result", second.id.hex())


def a_cluster_at_the_limit(ctx: Ctx) -> None:
    """Refuse a replacement that joins a full cluster, `too-large-cluster`."""
    chain = [_pay(ctx.coin(), 1_000)]
    for _ in range(63):
        chain.append(_pay(_coin(chain[-1]), 1_000))
    coin = ctx.coin()
    ctx.send(*chain, _pay(coin, 1_000))
    spent = [coin, _coin(chain[-1])]
    joining = _spend(spent, [sum(each.value for each in spent) - 50_000])
    assert sent(ctx, joining)[0] == "error"


def a_prioritised_conflict(ctx: Ctx) -> None:
    """Count the conflict's delta in what the replacement has to pay."""
    coin = ctx.coin()
    held = _pay(coin, 10_000)
    ctx.send(held)
    ctx.agree("prioritisetransaction", [held.id.hex(), 0, 5_000])
    assert sent(ctx, _pay(coin, 12_000))[0] == "error"


SCENARIOS: list[Callable[[Ctx], None]] = [
    the_issue_s_replacement,
    rules_3_and_4,
    a_conflict_with_a_descendant,
    a_spend_of_its_own_conflict,
    a_parent_that_dilutes_the_replacement,
    too_many_clusters,
    a_second_child_of_a_version_3_parent,
    a_cluster_at_the_limit,
    a_prioritised_conflict,
]


def test_replacement_is_answered_as_bitcoind_does(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Each scenario is answered alike, and leaves the mempools alike."""
    coins = fund(bitcoind)
    node = a_node(tmp_path)
    try:
        sync(node, bitcoind)
        run(bitcoind, rpc_client(node), coins, SCENARIOS)
    finally:
        node.stop()
        node.join()
