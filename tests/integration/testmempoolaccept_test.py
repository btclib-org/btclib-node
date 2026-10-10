# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`testmempoolaccept` of one or several transactions against bitcoind's.

Both nodes hold the same chain and mempool and are asked the same call,
and the answers are held equal key for key, in Core's order. This node
answers `vsize_adjusted` and `vsize_bip141` beside `vsize`, Core 32's sizes
(btclib-org/btclib-node#1757), which bitcoind v31.1 does not: they are
dropped from this node's answer before comparing.
"""

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from btclib.script.witness import Witness

from tests import rpc_client
from tests.integration.submitpackage_test import (
    Coin,
    Ctx,
    _coin,
    _hex,
    _pay,
    _spend,
    a_node,
    fund,
    key_order,
    run,
    sync,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from btclib.tx.tx import Tx

    from tests.integration.conftest import Bitcoind

_CORE_32_SIZES = ("vsize_adjusted", "vsize_bip141")


def without_core_32_sizes(answer: Any) -> Any:
    """Return `answer` without the sizes bitcoind v31.1 does not answer."""
    if answer[0] != "result":
        return answer
    entries = [
        {key: value for key, value in entry.items() if key not in _CORE_32_SIZES}
        for entry in answer[1]
    ]
    return ("result", entries)


def asked(ctx: Ctx, txs: list[Tx], *args: Any) -> Any:
    """Ask both `testmempoolaccept` of `txs`, and return bitcoind's answer."""
    ours, theirs = ctx.ask("testmempoolaccept", [[_hex(tx) for tx in txs], *args])
    if ours is not None:
        ours = without_core_32_sizes(ours)
        assert ours == theirs, (txs, args)
        assert key_order(ours) == key_order(theirs), (txs, args)
    return theirs


def a_parent_and_its_child(ctx: Ctx) -> None:
    """Both allowed, each at its own feerate; and one alone."""
    parent = _pay(ctx.coin(), 1_000)
    child = _pay(_coin(parent), 1_000)
    asked(ctx, [parent, child])
    asked(ctx, [parent])
    first, second = _pay(ctx.coin(), 1_000), _pay(ctx.coin(), 2_000)
    asked(ctx, [first, second])


def a_child_that_would_pay_for_its_parent(ctx: Ctx) -> None:
    """No package feerate: the free parent is refused, the child unvalidated."""
    parent = _pay(ctx.coin(), 0)
    asked(ctx, [parent, _pay(_coin(parent), 50_000)])


def packages_that_are_not_one(ctx: Ctx) -> None:
    """`IsWellFormedPackage`'s refusals, as `package-error` on each."""
    parent = _pay(ctx.coin(), 1_000)
    child = _pay(_coin(parent), 1_000)
    asked(ctx, [child, parent])
    asked(ctx, [parent, parent, child])
    both = _spend([_coin(parent)], [10_000])
    asked(ctx, [parent, replace(both, vin=[*both.vin, *parent.vin])])
    big = _pay(ctx.coin(), 1_000, outputs=1_200)
    asked(ctx, [big, _pay(_coin(big), 1_000, outputs=1_200)])


def a_truc_rule_of_the_package(ctx: Ctx) -> None:
    """Refuse a version 3 parent's child that is not version 3."""
    parent = _pay(ctx.coin(), 1_000, version=3)
    asked(ctx, [parent, _pay(_coin(parent), 1_000)])


def held_transactions(ctx: Ctx) -> None:
    """Refuse a held parent, and a conflict with a held transaction."""
    coin = ctx.coin()
    held = _pay(coin, 1_000)
    ctx.send(held)
    asked(ctx, [held, _pay(_coin(held), 1_000)])
    rival = _pay(coin, 50_000)
    asked(ctx, [rival, _pay(_coin(rival), 1_000)])


def refusals_in_order(ctx: Ctx) -> None:
    """Answer a missing input, then what `CheckTransaction` refuses."""
    ghost = _pay(Coin(b"\x07" * 32, 0, 10**8), 1_000)
    good = _pay(ctx.coin(), 1_000)
    empty = _spend([ctx.coin()], [])
    asked(ctx, [ghost, empty])
    asked(ctx, [good, empty])


def a_script_that_fails(ctx: Ctx) -> None:
    """Allow the parent before a child that fails its scripts."""
    parent = _pay(ctx.coin(), 1_000)
    child = _spend(
        [_coin(parent)], [parent.vout[0].value - 1_000], witness=Witness([b"\x52"])
    )
    asked(ctx, [parent, child])


def the_fee_rate_limit(ctx: Ctx) -> None:
    """Leave the rest unanswered after one over `maxfeerate`."""
    parent = _pay(ctx.coin(), 50_000)
    child = _pay(_coin(parent), 1_000)
    asked(ctx, [parent, child], 0.0001)
    rich = _pay(_coin(parent), 500_000)
    asked(ctx, [parent, rich], 0.01)
    asked(ctx, [parent, rich], 0)
    asked(ctx, [parent, rich])


def the_modified_fee(ctx: Ctx) -> None:
    """Lift a free parent over the floor by a delta, in its feerate too."""
    free = _pay(ctx.coin(), 0)
    ctx.agree("prioritisetransaction", [free.id.hex(), None, 1_000])
    asked(ctx, [free, _pay(_coin(free), 1_000)])


SCENARIOS: list[Callable[[Ctx], None]] = [
    a_parent_and_its_child,
    a_child_that_would_pay_for_its_parent,
    packages_that_are_not_one,
    a_truc_rule_of_the_package,
    held_transactions,
    refusals_in_order,
    a_script_that_fails,
    the_fee_rate_limit,
    the_modified_fee,
]


def test_testmempoolaccept_answers_as_bitcoind_does(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Each scenario is answered alike.

    Both mempools hold the same transactions after it.
    """
    coins = fund(bitcoind)
    node = a_node(tmp_path)
    try:
        sync(node, bitcoind)
        run(bitcoind, rpc_client(node), coins, SCENARIOS)
    finally:
        node.stop()
        node.join()
