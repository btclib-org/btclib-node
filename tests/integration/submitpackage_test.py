# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`submitpackage` against bitcoind's.

Both nodes hold the same chain and are asked the same call, in turn, and
the answers are held equal key for key, in Core's order: each refusal with
its code and words, and each shape of `tx-results`. The mempools are held
equal after each call, so a transaction one took in and the other refused
is found where it happens.

Package replacement is not served (btclib-org/btclib-node#1334): a package
that conflicts with a held transaction is held to the answers bitcoind gives
where it refuses the replacement, and what only bitcoind accepts is not
asked.
"""

from dataclasses import replace
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from bitcoin_core_rpc import RpcError
from btclib.hashes import sha256
from btclib.script.witness import Witness
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.p2p.address import peer_address
from tests import get_random_port, rpc_client, wait_until, wait_until_listening

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

# P2WSH over `OP_1`: standard, and spent by a witness of the script alone
_OP_TRUE = b"\x51"
_P2WSH = bytes.fromhex("0020") + sha256(_OP_TRUE)
_SPENDS_IT = Witness([_OP_TRUE])

# the subsidy until regtest's first halving, at height 150, which the blocks
# that pay the coins stay under
_SUBSIDY = 50 * 10**8
_COINS = 80
_MATURITY = 100


class Coin(NamedTuple):
    """An output to spend, and what it holds."""

    txid: bytes
    vout: int
    value: int


def _coin(tx: Tx, vout: int = 0) -> Coin:
    """Return output `vout` of `tx`."""
    return Coin(tx.id, vout, tx.vout[vout].value)


def _spend(
    coins: list[Coin],
    values: list[int],
    *,
    version: int = 2,
    lock_time: int = 0,
    witness: Witness = _SPENDS_IT,
) -> Tx:
    """Return a spend of `coins` paying `values` to anyone."""
    return Tx(
        version=version,
        lock_time=lock_time,
        vin=[
            TxIn(OutPoint(coin.txid, coin.vout), b"", 0xFFFFFFFD, witness)
            for coin in coins
        ],
        vout=[TxOut(value, _P2WSH) for value in values],
        check_validity=False,
    )


def _pay(coin: Coin, fee: int, *, outputs: int = 1, version: int = 2) -> Tx:
    """Return a spend of `coin` paying `fee`, the rest in `outputs` parts."""
    each = (coin.value - fee) // outputs
    values = [each] * outputs
    values[0] += coin.value - fee - each * outputs
    return _spend([coin], values, version=version)


def _hex(tx: Tx) -> str:
    """Return `tx` as `submitpackage` takes it."""
    return tx.serialize(include_witness=True, check_validity=False).hex()


class Ctx:
    """What a scenario holds: both nodes, and the coins not yet spent."""

    def __init__(
        self,
        bitcoind: Bitcoind,
        client: Any | None,
        coins: list[Coin],
    ) -> None:
        """Hold both nodes, `client` being `None` to ask bitcoind only."""
        self.bitcoind = bitcoind
        self.client = client
        self.coins = coins

    def coin(self) -> Coin:
        """Return a coin no scenario has spent."""
        return self.coins.pop()

    def ask(self, method: str, params: Any) -> tuple[Any, Any]:
        """Return what this node and bitcoind answer, each as `answered`."""
        theirs = answered(self.bitcoind.rpc, method, params)
        ours = (
            None if self.client is None else answered(self.client.call, method, params)
        )
        return ours, theirs

    def agree(self, method: str, params: Any) -> Any:
        """Ask both, assert the answers are alike in value and key order."""
        ours, theirs = self.ask(method, params)
        if ours is not None:
            assert ours == theirs, (method, params)
            assert key_order(ours) == key_order(theirs), (method, params)
        return theirs

    def send(self, *txs: Tx) -> None:
        """Hold each of `txs` in both mempools, by `sendrawtransaction`."""
        for tx in txs:
            answer = self.agree("sendrawtransaction", [_hex(tx)])
            assert answer == ("result", tx.id.hex()), answer

    def mine(self) -> None:
        """Mine a block on bitcoind, and wait until this node holds it."""
        info = cast(
            "Any", self.bitcoind.rpc("getdescriptorinfo", [f"raw({_P2WSH.hex()})"])
        )
        self.bitcoind.rpc("generatetodescriptor", [1, info["descriptor"]])
        if self.client is not None:
            client = self.client
            tip = cast("int", self.bitcoind.rpc("getblockcount"))
            wait_until(lambda: client.call("getblockcount", []) == tip)

    def package(self, txs: list[Tx], *args: Any) -> Any:
        """Ask both `submitpackage` of `txs`, and return bitcoind's answer."""
        return self.agree("submitpackage", [[_hex(tx) for tx in txs], *args])


def answered(call: Callable[[str, Any], Any], method: str, params: Any) -> Any:
    """Return `call`'s answer, or its error as a code and the message."""
    try:
        return ("result", call(method, params))
    except RpcError as error:
        # past the url, which names each side's own port
        return ("error", error.code, error.args[0].split(": ", 1)[1])


def key_order(value: Any) -> Any:
    """Return `value` with each object as its keys in order, at any depth.

    A `dict` compares equal whatever its key order, and Core's is what a
    client that reads the JSON as text sees.
    """
    if isinstance(value, dict):
        return [(key, key_order(item)) for key, item in value.items()]
    if isinstance(value, list | tuple):
        return [key_order(item) for item in value]
    return value


def fund(bitcoind: Bitcoind) -> list[Coin]:
    """Mine `_COINS` coinbases to anyone and bury them, and return them."""
    info = cast("Any", bitcoind.rpc("getdescriptorinfo", [f"raw({_P2WSH.hex()})"]))
    descriptor = info["descriptor"]
    bitcoind.rpc("generatetodescriptor", [_COINS, descriptor])
    bitcoind.rpc("generatetodescriptor", [_MATURITY, descriptor])
    coins = []
    for height in range(1, _COINS + 1):
        block = cast(
            "dict[str, Any]",
            bitcoind.rpc("getblock", [bitcoind.rpc("getblockhash", [height]), 2]),
        )
        coinbase = block["tx"][0]
        value = round(coinbase["vout"][0]["value"] * 10**8)
        assert value == _SUBSIDY
        coins.append(Coin(bytes.fromhex(coinbase["txid"]), 0, value))
    return coins


def run(
    bitcoind: Bitcoind,
    client: Any | None,
    coins: list[Coin],
    scenarios: list[Callable[[Ctx], None]],
) -> None:
    """Run each scenario, and compare the mempools after it."""
    ctx = Ctx(bitcoind, client, coins)
    for scenario in scenarios:
        scenario(ctx)
        if client is not None:
            assert sorted(client.call("getrawmempool", [])) == sorted(
                cast("Any", bitcoind.rpc("getrawmempool", []))
            ), scenario.__name__


def sync(node: Node, bitcoind: Bitcoind) -> None:
    """Connect `node` to `bitcoind`, and wait for its chain."""
    node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))
    tip = cast("int", bitcoind.rpc("getblockcount"))
    wait_until(lambda: len(node.chainstate.block_index.active_chain) == tip + 1)


def a_node(tmp_path: Path) -> Node:
    """Start a regtest node that listens for RPC."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
        )
    )
    node.start()
    wait_until_listening(node.p2p_manager)
    wait_until_listening(node.rpc_manager)
    return node


def refusals(ctx: Ctx) -> None:
    """Ask each argument wrong, in the order bitcoind refuses them."""
    coin = ctx.coin()
    tx = _pay(coin, 1_000)
    big = [_hex(tx)] * 26
    for params in [
        [],
        [[]],
        [big],
        ["x"],
        [1],
        [{}],
        [[1]],
        [[None]],
        [["zz"]],
        [[""]],
        [[_hex(tx)], "x"],
        [[_hex(tx)], "1"],
        [[_hex(tx)], 1],
        [[_hex(tx)], True],
        [[_hex(tx)], 0.1, "x"],
        [[_hex(tx)], 1, "x"],
        [[_hex(tx)], 0.1, -1],
        [[_hex(tx)], 0.1, 21_000_001],
        [big, "x"],
        [[1], "x"],
        [[1], 0.1, "x"],
        [[_hex(tx), 1]],
        [[_hex(tx), "zz"], "x"],
        [[_hex(tx)], 0.1, 0, 5],
    ]:
        ctx.agree("submitpackage", params)
    ctx.agree("submitpackage", {"package": [_hex(tx)], "maxfeerate": 0.1})
    ctx.agree("submitpackage", {"package": [_hex(tx)], "foo": 1})
    ctx.agree("submitpackage", [[_hex(tx)], None, None])
    ours, theirs = ctx.ask("help", ["submitpackage"])
    # `help` here answers without the newline that ends Core's text
    assert ours is None or ours == ("result", theirs[1].rstrip("\n"))


def a_single_transaction(ctx: Ctx) -> None:
    """One transaction is taken, and then is in the mempool."""
    tx = _pay(ctx.coin(), 1_000)
    ctx.package([tx])
    # bitcoind does not mark what it accepts unbroadcast, as it has the
    # transaction in the mempool by the time it broadcasts it
    for method, params, key in [
        ("getmempoolentry", [tx.id.hex()], "unbroadcast"),
        ("getmempoolinfo", [], "unbroadcastcount"),
    ]:
        ours, theirs = ctx.ask(method, params)
        assert theirs[1][key] in (False, 0), theirs
        assert ours is None or ours[1][key] == theirs[1][key], (ours, theirs)
    ctx.package([tx])
    ctx.package([tx], 0, 0)
    mutated = replace(
        tx, vin=[replace(tx.vin[0], script_witness=Witness([b"", _OP_TRUE]))]
    )
    assert mutated.id == tx.id
    ctx.package([mutated])


def a_parent_the_child_pays_for(ctx: Ctx) -> None:
    """Take a free parent and its child, with the package feerate for both."""
    parent = _pay(ctx.coin(), 0)
    child = _pay(_coin(parent), 5_000)
    ctx.package([parent, child])
    ctx.package([parent, child])


def two_parents_the_child_pays_for(ctx: Ctx) -> None:
    """Both parents are refused alone, and the package takes all three."""
    first, second = _pay(ctx.coin(), 0), _pay(ctx.coin(), 0)
    child = _spend(
        [_coin(first), _coin(second)],
        [first.vout[0].value + second.vout[0].value - 9_000],
    )
    ctx.package([first, second, child])


def a_parent_that_pays_for_itself(ctx: Ctx) -> None:
    """Take the parent that pays alone, and the other with the child."""
    first, second = _pay(ctx.coin(), 1_000), _pay(ctx.coin(), 0)
    child = _spend(
        [_coin(first), _coin(second)],
        [first.vout[0].value + second.vout[0].value - 9_000],
    )
    ctx.package([first, second, child])


def a_parent_in_the_mempool(ctx: Ctx) -> None:
    """Take the child alone, and then answer every one as held."""
    parent = _pay(ctx.coin(), 1_000)
    ctx.send(parent)
    child = _pay(_coin(parent), 1_000)
    ctx.package([child])
    ctx.package([parent, child])


def a_parent_in_the_mempool_and_another_for_the_package(ctx: Ctx) -> None:
    """Take the parent that is not held with the child, and answer the other."""
    held, free = _pay(ctx.coin(), 1_000), _pay(ctx.coin(), 0)
    ctx.send(held)
    child = _spend(
        [_coin(held), _coin(free)], [held.vout[0].value + free.vout[0].value - 9_000]
    )
    ctx.package([held, free, child])


def a_child_that_pays_nothing(ctx: Ctx) -> None:
    """Alone, the child is under the floor, and so is the package."""
    parent = _pay(ctx.coin(), 1_000)
    ctx.send(parent)
    ctx.package([_pay(_coin(parent), 0)])
    free = _pay(ctx.coin(), 0)
    ctx.package([free, _pay(_coin(free), 0)])


def a_parent_that_fails_for_more_than_its_fee(ctx: Ctx) -> None:
    """End the package at a parent, and still take the one after it."""
    bad = _pay(ctx.coin(), 1_000, version=4)
    good = _pay(ctx.coin(), 1_000)
    child = _spend(
        [_coin(bad), _coin(good)], [bad.vout[0].value + good.vout[0].value - 5_000]
    )
    ctx.package([bad, good, child])


def inputs_that_are_not_there(ctx: Ctx) -> None:
    """Missing inputs, in a parent and in a lone transaction."""
    ghost = _pay(Coin(b"\x07" * 32, 0, 10**8), 1_000)
    ctx.package([ghost])
    ctx.package([ghost, _pay(_coin(ghost), 1_000)])
    short = _spend([ctx.coin()], [10 * _SUBSIDY])
    ctx.package([short])


def a_package_that_is_not_one(ctx: Ctx) -> None:
    """Refuse the topology, then what `IsWellFormedPackage` refuses."""
    first, second = _pay(ctx.coin(), 1_000), _pay(ctx.coin(), 1_000)
    ctx.package([first, second])
    chain = _pay(_coin(first), 1_000)
    tip = _spend([_coin(chain), _coin(second)], [10_000])
    ctx.package([first, chain, tip])
    ctx.package([chain, first])
    parent = _pay(ctx.coin(), 1_000)
    child = _pay(_coin(parent), 1_000)
    ctx.package([child, parent])
    ctx.package([parent, parent, child])
    ctx.package([child, child])
    rival = replace(parent, lock_time=1)
    ctx.package([parent, rival, _spend([_coin(parent), _coin(rival)], [10_000])])
    both = _spend([_coin(parent)], [10_000])
    ctx.package([parent, replace(both, vin=[*both.vin, *parent.vin])])


def a_package_over_the_weight(ctx: Ctx) -> None:
    """Over `MAX_PACKAGE_WEIGHT` with each over its own limit by none."""
    parent = _pay(ctx.coin(), 1_000, outputs=1_200)
    child = _pay(_coin(parent), 1_000, outputs=1_200)
    assert parent.weight + child.weight > 404_000
    assert max(parent.weight, child.weight) <= 400_000
    ctx.package([parent, child])
    ctx.package([parent])


def a_child_that_ends_the_package(ctx: Ctx) -> None:
    """Try a parent alone when its child fails for more than a fee."""
    free = _pay(ctx.coin(), 0)
    ctx.package([free, _pay(_coin(free), 5_000, version=4)])
    parent = _pay(ctx.coin(), 0)
    ctx.package([parent, _spend([_coin(parent)], [parent.vout[0].value + 1])])


def a_parent_that_breaks_a_rule_alone(ctx: Ctx) -> None:
    """Send a parent that alone breaks TRUC's rule, which ends the package."""
    held = _pay(ctx.coin(), 1_000)
    ctx.send(held)
    breaking = _pay(_coin(held), 1_000, version=3)
    nothing = _pay(ctx.coin(), 0)
    joined = [_coin(nothing), _coin(breaking)]
    child = _spend(joined, [sum(coin.value for coin in joined) - 5_000])
    ctx.package([nothing, breaking, child])


def the_fee_rate_limit(ctx: Ctx) -> None:
    """Hold each transaction to `maxfeerate`, and a package's child too."""
    rich = _pay(ctx.coin(), 1_000_000)
    ctx.package([rich])
    ctx.package([rich], 0)
    exact = _pay(ctx.coin(), 960)
    ctx.package([exact], 0.0001)
    over = _pay(ctx.coin(), 961)
    ctx.package([over], 0.0001)
    # a limit that is not a whole number of satoshi for the size: each
    # transaction is compared as a fraction, not by a fee rounded up
    below = _pay(ctx.coin(), 960)
    ctx.package([below], 0.00009999)
    free = _pay(ctx.coin(), 0)
    pays = _pay(_coin(free), 5_000)
    ctx.package([free, pays], 0.0001)
    ctx.package([free, pays], 0.001)
    parent = _pay(ctx.coin(), 1_000_000)
    ctx.package([parent, _pay(_coin(parent), 1_000)])


def the_fee_rate_limit_before_the_dust(ctx: Ctx) -> None:
    """Refuse the child's feerate where it would also leave the dust."""
    coin = ctx.coin()
    parent = _spend([coin], [coin.value, 0])
    leaves = _pay(_coin(parent, 0), 500_000)
    ctx.package([parent, leaves], 0.0001)
    ctx.package([parent, leaves])


def a_transaction_the_chain_holds(ctx: Ctx) -> None:
    """Answer a mined transaction known, alone and as a parent."""
    tx = _pay(ctx.coin(), 1_000)
    ctx.send(tx)
    ctx.mine()
    ctx.package([tx])
    ctx.package([tx, _pay(_coin(tx), 1_000)])


def a_burn(ctx: Ctx) -> None:
    """Refuse a burn over `maxburnamount`, before anything else."""
    tx = _pay(ctx.coin(), 1_000)
    burning = replace(tx, vout=[*tx.vout, TxOut(100, bytes([0x6A, 0x01, 0x07]))])
    ctx.package([burning])
    ctx.package([burning], 0.1, 0.000001)
    ctx.package([burning], 0.1, 0.00000099)
    ctx.package([burning], 0.1, "0.000001")
    ctx.package([_pay(ctx.coin(), 1_000), burning])


def a_transaction_that_is_not_final(ctx: Ctx) -> None:
    """Refuse a lock time that has not come."""
    coin = ctx.coin()
    tx = _spend([coin], [coin.value - 1_000], lock_time=10**6)
    ctx.package([tx])


def a_script_that_fails(ctx: Ctx) -> None:
    """Refuse a witness that does not satisfy, alone and in a package."""
    coin = ctx.coin()
    bad = _spend([coin], [coin.value - 1_000], witness=Witness([b"\x52"]))
    ctx.package([bad])
    free = _pay(ctx.coin(), 0)
    child = _spend(
        [_coin(free)], [free.vout[0].value - 5_000], witness=Witness([b"\x52"])
    )
    ctx.package([free, child])
    coin = ctx.coin()
    parent = _spend([coin], [coin.value], witness=Witness([b"\x52"]))
    ctx.package([parent, _pay(_coin(parent), 5_000)])


def a_transaction_a_witness_apart(ctx: Ctx) -> None:
    """Answer another witness of a held parent, and take its child."""
    parent = _pay(ctx.coin(), 1_000)
    ctx.send(parent)
    other = replace(
        parent,
        vin=[replace(parent.vin[0], script_witness=Witness([b"", _OP_TRUE]))],
    )
    ctx.package([other, _pay(_coin(parent), 1_000)])


def the_modified_fee(ctx: Ctx) -> None:
    """Count a delta in the effective feerate, and in the floor."""
    free = _pay(ctx.coin(), 0)
    child = _pay(_coin(free), 0)
    ctx.package([free, child])
    ctx.agree("prioritisetransaction", [child.id.hex(), None, 5_000])
    ctx.package([free, child])
    ctx.agree("prioritisetransaction", [free.id.hex(), None, -2_000])
    other = _pay(ctx.coin(), 0)
    ctx.agree("prioritisetransaction", [other.id.hex(), None, 1_000])
    ctx.package([other])
    ctx.package([other])


def truc_packages(ctx: Ctx) -> None:
    """Take a zero-fee version 3 parent with its child, and refuse the rest."""
    free = _pay(ctx.coin(), 0, version=3)
    ctx.package([free, _pay(_coin(free), 5_000, version=3)])
    plain = _pay(ctx.coin(), 0)
    ctx.package([plain, _pay(_coin(plain), 5_000, version=3)])
    # `maxfeerate` is asked before the rules, whose refusal this also is
    ctx.package([plain, _pay(_coin(plain), 5_000, version=3)], 0.0001)
    third = _pay(ctx.coin(), 0, version=3)
    ctx.package([third, _pay(_coin(third), 5_000)])
    held = _pay(ctx.coin(), 1_000, version=3)
    held_child = _pay(_coin(held), 1_000, version=3)
    ctx.send(held, held_child)
    ctx.package([_pay(_coin(held_child), 1_000, version=3)])
    big = _pay(ctx.coin(), 0, version=3, outputs=200)
    ctx.package([big, _pay(_coin(big), 5_000, version=3)])
    # a held parent with a child: the sibling rule is the transaction's own
    root = _pay(ctx.coin(), 1_000, outputs=2, version=3)
    ctx.send(root, _pay(_coin(root, 0), 1_000, version=3))
    nothing = _pay(_coin(root, 1), 0, version=3)
    ctx.package([nothing, _pay(_coin(nothing), 5_000, version=3)])
    # the rules against a held parent are the transaction's own refusal
    nothing = _pay(ctx.coin(), 0, version=3)
    huge = _pay(_coin(nothing), 50_000, version=3, outputs=300)
    assert huge.vsize > 10_000
    ctx.package([nothing, huge])
    held = _pay(ctx.coin(), 1_000)
    ctx.send(held)
    other = _pay(ctx.coin(), 0, version=3)
    joined = [_coin(held), _coin(other)]
    ctx.package(
        [other, _spend(joined, [sum(c.value for c in joined) - 5_000], version=3)]
    )
    # the rules of a package are asked in Core's order: ancestors first
    v3, v2 = _pay(ctx.coin(), 0, version=3), _pay(ctx.coin(), 0)
    both = [_coin(v3), _coin(v2)]
    ctx.package([v3, v2, _spend(both, [sum(c.value for c in both) - 5_000], version=3)])
    held_v3 = _pay(ctx.coin(), 1_000, version=3)
    ctx.send(held_v3)
    plain = _pay(ctx.coin(), 0)
    both = [_coin(held_v3), _coin(plain)]
    ctx.package([plain, _spend(both, [sum(c.value for c in both) - 5_000], version=3)])
    # and the size of a child before the version of its parent
    parent = _pay(ctx.coin(), 0)
    ctx.package([parent, _pay(_coin(parent), 5_000, version=3, outputs=30)])


def ephemeral_dust(ctx: Ctx) -> None:
    """Take a free parent with dust if its child spends the dust."""
    coin = ctx.coin()
    parent = _spend([coin], [coin.value, 0])
    spends = _spend(
        [_coin(parent, 0), _coin(parent, 1)], [parent.vout[0].value - 5_000]
    )
    leaves = _pay(_coin(parent, 0), 5_000)
    ctx.package([parent, leaves])
    ctx.package([parent, spends])
    paying = _spend([ctx.coin()], [_SUBSIDY - 1_000, 0])
    ctx.package([paying])
    ctx.package([paying, _pay(_coin(paying), 5_000)])
    # the package's fee floor is asked before the dust
    coin = ctx.coin()
    nothing = _spend([coin], [coin.value, 0])
    ctx.package([nothing, _pay(_coin(nothing, 0), 0)])


def a_cluster_too_large(ctx: Ctx) -> None:
    """Refuse a package that joins a cluster of the limit."""
    root = _pay(ctx.coin(), 1_000, outputs=70)
    ctx.send(root)
    for vout in range(63):
        ctx.send(_pay(_coin(root, vout), 1_000))
    parent = _pay(ctx.coin(), 0)
    joined = [_coin(parent), _coin(root, 69)]
    child = _spend(joined, [sum(coin.value for coin in joined) - 5_000])
    ctx.package([parent, child])
    ctx.package([_pay(_coin(root, 63), 1_000)])
    # the package's fee floor is asked before the cluster limit
    nothing = _pay(ctx.coin(), 0)
    joined = [_coin(nothing), _coin(root, 68)]
    ctx.package([nothing, _spend(joined, [sum(coin.value for coin in joined)])])


def a_conflict_that_pays_too_little(ctx: Ctx) -> None:
    """Refuse a transaction that spends what a held one does, for less."""
    coin = ctx.coin()
    held = _pay(coin, 5_000)
    ctx.send(held)
    ctx.package([_pay(coin, 1_000, outputs=2)])
    # Core refuses the pair as a package, "package RBF failed", where this
    # node, which replaces nothing, says "transaction failed"; what each
    # transaction answers is alike (btclib-org/btclib-node#1334)
    rival = _pay(coin, 4_000, outputs=2)
    ours, theirs = ctx.ask(
        "submitpackage", [[_hex(rival), _hex(_pay(_coin(rival), 1_000))]]
    )
    assert theirs[1]["package_msg"].startswith("package RBF failed: ")
    if ours is not None:
        assert ours[1]["package_msg"] == "transaction failed"
        assert ours[1]["tx-results"] == theirs[1]["tx-results"]


def a_child_that_conflicts_and_pays_too_little(ctx: Ctx) -> None:
    """Answer the child its missing input, as it was alone (ISS 1792)."""
    coin = ctx.coin()
    ctx.send(_pay(coin, 5_000))
    parent = _pay(ctx.coin(), 0)
    joined = [_coin(parent), coin]
    child = _spend(joined, [sum(spent.value for spent in joined) - 5_000])
    ours, theirs = ctx.ask("submitpackage", [[_hex(parent), _hex(child)]])
    assert theirs[1]["package_msg"].startswith("package RBF failed: ")
    assert theirs[1]["tx-results"][child.hash.hex()]["error"] == (
        "bad-txns-inputs-missingorspent"
    )
    # the message is as above (btclib-org/btclib-node#1334)
    if ours is not None:
        assert ours[1]["package_msg"] == "transaction failed"
        assert ours[1]["tx-results"] == theirs[1]["tx-results"]


SCENARIOS: list[Callable[[Ctx], None]] = [
    refusals,
    a_single_transaction,
    a_parent_the_child_pays_for,
    two_parents_the_child_pays_for,
    a_parent_that_pays_for_itself,
    a_parent_in_the_mempool,
    a_parent_in_the_mempool_and_another_for_the_package,
    a_child_that_pays_nothing,
    a_parent_that_fails_for_more_than_its_fee,
    inputs_that_are_not_there,
    a_package_that_is_not_one,
    a_package_over_the_weight,
    a_child_that_ends_the_package,
    a_parent_that_breaks_a_rule_alone,
    the_fee_rate_limit,
    the_fee_rate_limit_before_the_dust,
    a_transaction_the_chain_holds,
    a_burn,
    a_transaction_that_is_not_final,
    a_script_that_fails,
    a_transaction_a_witness_apart,
    the_modified_fee,
    truc_packages,
    ephemeral_dust,
    a_cluster_too_large,
    a_conflict_that_pays_too_little,
    a_child_that_conflicts_and_pays_too_little,
]


def test_submitpackage_answers_as_bitcoind_does(
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
