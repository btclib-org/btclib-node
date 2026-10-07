# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Orphans and one-parent-one-child packages, through `tx` and the loop."""

import threading
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast, override

import pytest
from btclib.p2p.data import TxPayload as TxMsg
from btclib.tx.limits import COINBASE_MATURITY
from btclib.tx.out_point import OutPoint
from btclib.tx.tx_in import TxIn

import btclib_node.main as node_main
import btclib_node.p2p.callbacks as cb
import btclib_node.p2p.main as p2p_main
from btclib_node.chains import RegTest
from btclib_node.constants import P2pConnStatus
from btclib_node.exceptions import TxRejectedError
from btclib_node.interpreter import check_package
from btclib_node.mempool import package_hash
from btclib_node.p2p.callbacks import process_orphan
from btclib_node.p2p.main import resume_tx_checks
from btclib_node.p2p.tx_checks import TxCheck
from tests import generate_random_chain
from tests.unit.main_test import connect, spend
from tests.unit.p2p.callbacks_test import a_peer

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib.tx.tx import Tx

    from btclib_node import Node


class Settled:
    """The verdict of a script check run on the spot, as a pool answers it."""

    def __init__(self, function: Any, args: tuple[Any, ...]) -> None:
        """Run `function` now."""
        self.value = function(*args)

    def ready(self) -> bool:
        """Answer that it is."""
        return True

    def get(self) -> Any:
        """Answer what the check answered."""
        return self.value


class InlinePool:
    """A pool that runs every check where it is asked."""

    def apply_async(self, function: Any, args: tuple[Any, ...]) -> Settled:
        """Run the check now."""
        return Settled(function, args)

    def terminate(self) -> None:
        """Nothing runs, so nothing to stop."""

    def join(self) -> None:
        """Nothing runs, so nothing to wait for."""


class UnfinishedPool(InlinePool):
    """A pool whose checks never finish."""

    @override
    def apply_async(self, function: Any, args: tuple[Any, ...]) -> Any:
        """Answer a check that is not ready."""
        return SimpleNamespace(ready=lambda: False)


class Chain:
    """A node past coinbase maturity, with a parent and its child to relay."""

    def __init__(self, node: Node, parent: Tx, child: Tx) -> None:
        """Hold the node and the pair."""
        self.node = node
        self.parent = parent
        self.child = child


def a_pair(
    regtest_node: Callable[..., Node],
    *,
    parent_fee: int = 1000,
    child_fee: int = 100_000,
    floor: float = 100_000.0,
    parent_script_sig: bytes | None = None,
    child_script_sig: bytes | None = None,
) -> Chain:
    """Build a node whose mempool minimum is `floor` sat/kvB, and a pair.

    The parent pays `parent_fee`, under that floor; the child spends it and
    pays `child_fee`, enough that the two together are over. A
    `script_sig` of `b""` makes the scripts of that one fail.
    """
    node = regtest_node()
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    node.is_initial_block_download = False
    cast("Any", node)._worker_pool = InlinePool()
    node.mempool._rolling_min_fee_rate = floor
    funding = chain[0].transactions[0]
    parent = spend(funding, funding.vout[0].value - parent_fee, parent_script_sig)
    child = spend(parent, parent.vout[0].value - child_fee, child_script_sig)
    return Chain(node, parent, child)


def a_connected_peer(node: Node, conn_id: int = 3) -> Any:
    """Connect a peer double to `node`, as `verack` leaves it."""
    peer = a_peer(id=conn_id, status=P2pConnStatus.Connected)
    node.p2p_manager.connections[conn_id] = peer
    return peer


def relay(node: Node, peer: Any, tx: Tx) -> None:
    """Hand `tx` to the `tx` callback as `peer`'s message, then run the loop."""
    cb.tx(node, TxMsg(tx, include_witness=True).serialize(), peer)
    settle(node)


def settle(node: Node) -> None:
    """Run the loop's tx step until nothing is queued or being checked."""
    while resume_tx_checks(node) or node.tx_checks.queued:
        pass


def test_a_child_ahead_of_its_parent_is_taken_in_with_it(
    regtest_node: Callable[..., Node],
) -> None:
    """An orphan is taken in with the parent that pays too little."""
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    assert node.download_manager.orphanage.have_tx(pair.child.hash)
    assert not node.mempool.size
    relay(node, peer, pair.parent)
    assert node.mempool.contains_tx(pair.parent)
    assert node.mempool.contains_tx(pair.child)
    assert not node.download_manager.orphanage.have_tx(pair.child.hash)


def test_a_parent_ahead_of_its_child_is_taken_in_when_it_comes_again(
    regtest_node: Callable[..., Node],
) -> None:
    """The parent refused for its fee is taken in when sent again."""
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.parent)
    assert not node.mempool.size
    relay(node, peer, pair.child)
    assert node.download_manager.orphanage.have_tx(pair.child.hash)
    relay(node, peer, pair.parent)
    assert node.mempool.contains_tx(pair.parent)
    assert node.mempool.contains_tx(pair.child)
    assert not node.download_manager.orphanage.have_tx(pair.child.hash)


def test_a_parent_again_with_no_child_starts_no_package(
    regtest_node: Callable[..., Node],
) -> None:
    """A refusal that a package can undo is not tried by itself again."""
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.parent)
    relay(node, peer, pair.parent)
    assert not node.mempool.size
    assert not node.tx_checks.queued


def test_an_orphan_sent_again_is_not_judged_again(
    regtest_node: Callable[..., Node],
) -> None:
    """The orphanage is asked first."""
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    relay(node, peer, pair.child)
    assert node.download_manager.orphanage.announcement_count == 1


def test_a_package_whose_child_pays_too_little_is_refused_and_not_tried_again(
    regtest_node: Callable[..., Node],
) -> None:
    """Both are recorded, the pair by its hash, and the orphan is gone."""
    pair = a_pair(regtest_node, child_fee=1000)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    relay(node, peer, pair.parent)
    assert not node.mempool.size
    mempool = node.mempool
    assert mempool.was_recently_rejected_reconsiderable(
        package_hash([pair.parent.hash, pair.child.hash])
    )
    assert mempool.was_recently_rejected_reconsiderable(pair.child.hash)
    assert not node.download_manager.orphanage.have_tx(pair.child.hash)


class FailingPackagePool(InlinePool):
    """A pool whose package check fails at `position`, as a bad script would."""

    def __init__(self, position: int) -> None:
        """Fail the transaction at `position`, parents first."""
        self.position = position

    @override
    def apply_async(self, function: Any, args: tuple[Any, ...]) -> Settled:
        """Answer a package check with a script refusal."""
        assert function is check_package
        refusal = TxRejectedError("mempool-script-verify-flag-failed (x)")
        return Settled(lambda _: (self.position, refusal), args)


def test_a_child_whose_scripts_fail_is_refused_and_its_parent_is_not(
    regtest_node: Callable[..., Node],
) -> None:
    """`TxCheck.failed` names the child, so the parent answers a fee floor."""
    pair = a_pair(regtest_node)
    cast("Any", pair.node)._worker_pool = FailingPackagePool(1)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    relay(node, peer, pair.parent)
    assert not node.mempool.size
    mempool = node.mempool
    assert mempool.was_recently_rejected(pair.child.hash)
    assert mempool.was_recently_rejected_reconsiderable(pair.parent.hash)
    assert not mempool.was_recently_rejected(pair.parent.hash)


def test_a_parent_whose_scripts_fail_is_refused_with_its_child_left_missing_inputs(
    regtest_node: Callable[..., Node],
) -> None:
    """`TxCheck.failed` names the parent."""
    pair = a_pair(regtest_node)
    cast("Any", pair.node)._worker_pool = FailingPackagePool(0)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    relay(node, peer, pair.parent)
    assert not node.mempool.size
    assert node.mempool.was_recently_rejected(pair.parent.hash)
    assert not node.mempool.was_recently_rejected(pair.child.hash)


def test_a_child_spending_what_its_parent_spends_is_refused_without_an_answer(
    regtest_node: Callable[..., Node],
) -> None:
    """A "conflict-in-package" answers for neither; the pair is recorded."""
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    rival = TxIn(pair.parent.vin[0].prev_out, b"", 0xFFFFFFFF)
    child = replace(pair.child, vin=[*pair.child.vin, rival])
    relay(node, peer, child)
    relay(node, peer, pair.parent)
    assert not node.mempool.size
    assert node.mempool.was_recently_rejected_reconsiderable(
        package_hash([pair.parent.hash, child.hash])
    )


def test_a_package_over_the_weight_limit_leaves_the_child_an_orphan(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's "package-too-large" answers for neither member.

    `IsWellFormedPackage` (`src/policy/packages.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the pair is recorded and
    the child is not refused, as it would be for a cluster too large.
    """
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    monkeypatch.setattr(
        node_main, "_MAX_PACKAGE_WEIGHT", pair.parent.weight + pair.child.weight - 1
    )
    relay(node, peer, pair.child)
    relay(node, peer, pair.parent)
    assert not node.mempool.size
    assert node.download_manager.orphanage.have_tx(pair.child.hash)
    assert not node.mempool.was_recently_rejected(pair.child.hash)
    assert node.mempool.was_recently_rejected_reconsiderable(
        package_hash([pair.parent.hash, pair.child.hash])
    )


def test_a_package_that_no_longer_pays_when_its_scripts_are_in_is_refused(
    regtest_node: Callable[..., Node],
) -> None:
    """The checks but the scripts run again on the state of that moment."""
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    cb.tx(node, TxMsg(pair.parent, include_witness=True).serialize(), peer)
    assert node.tx_checks.queued
    node.mempool._rolling_min_fee_rate = 10.0**9
    settle(node)
    assert not node.mempool.size
    assert node.mempool.was_recently_rejected_reconsiderable(
        package_hash([pair.parent.hash, pair.child.hash])
    )


def test_a_package_the_mempool_cannot_hold_is_refused_as_full(
    regtest_node: Callable[..., Node],
) -> None:
    """Not reconsiderable: a member the trim removed is "mempool full".

    `AcceptPackage` makes it `TX_MEMPOOL_POLICY`, not the
    `TX_RECONSIDERABLE` of a single transaction
    (`src/validation.cpp:1748`, at bitcoin/bitcoin@9be056a8a7).
    """
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    node.mempool.bytesize_limit = 0
    relay(node, peer, pair.parent)
    assert not node.mempool.size
    assert node.mempool.was_recently_rejected(pair.parent.hash)
    assert node.mempool.was_recently_rejected(pair.child.hash)


def test_a_transaction_the_full_mempool_evicts_is_reconsiderable(
    regtest_node: Callable[..., Node],
) -> None:
    """The single transaction's "mempool full" a package may still undo."""
    pair = a_pair(regtest_node, parent_fee=50_000)
    node, peer = pair.node, a_connected_peer(pair.node)
    node.mempool.bytesize_limit = 0
    relay(node, peer, pair.parent)
    assert not node.mempool.size
    assert node.mempool.was_recently_rejected_reconsiderable(pair.parent.hash)


def test_a_parent_that_pays_for_itself_is_accepted_with_its_orphan_after(
    regtest_node: Callable[..., Node],
) -> None:
    """The orphan is marked, then taken up by the loop, and kept."""
    pair = a_pair(regtest_node, parent_fee=50_000)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    assert node.download_manager.orphanage.have_tx(pair.child.hash)
    relay(node, peer, pair.parent)
    assert node.mempool.contains_tx(pair.parent)
    assert node.mempool.contains_tx(pair.child)
    assert not node.download_manager.orphanage.have_tx(pair.child.hash)


def test_an_orphan_that_pays_too_little_is_refused_when_taken_up(
    regtest_node: Callable[..., Node],
) -> None:
    """Its parent is held, and what refuses it now is its own fee."""
    pair = a_pair(regtest_node, parent_fee=50_000, child_fee=0)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    relay(node, peer, pair.parent)
    assert node.mempool.contains_tx(pair.parent)
    assert not node.mempool.contains_tx(pair.child)
    assert node.mempool.was_recently_rejected_reconsiderable(pair.child.hash)
    assert not node.download_manager.orphanage.have_tx(pair.child.hash)


def test_a_message_waits_until_the_peer_has_no_orphan_to_reconsider(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1739: each orphan to reconsider is taken up before the next message.

    The first orphan taken up is refused, which queues nothing, and the
    second is still to reconsider: the `ping` behind them is not read yet.
    """
    pair = a_pair(regtest_node, parent_fee=50_000, child_fee=0)
    node = pair.node
    peer = a_connected_peer(node)
    peer.queued_recv_bytes = 0
    peer._recv_lock = threading.Lock()
    peer._recv_resume = SimpleNamespace(set=lambda: None)
    peer.loop = SimpleNamespace(call_soon_threadsafe=lambda fn: fn())
    node.p2p_manager.messages = deque()
    node.p2p_manager.handshake_messages = deque()
    better = spend(pair.parent, pair.parent.vout[0].value - 100_000)
    seen: list[tuple[bool, list[int]]] = []

    def ping(node: Node, msg: bytes, conn: Any) -> None:
        orphanage = node.download_manager.orphanage
        seen.append((node.mempool.contains_tx(better), orphanage.peers_to_reconsider()))

    monkeypatch.setitem(cb.callbacks, "ping", ping)
    for kind, payload in (
        ("tx", TxMsg(pair.child, include_witness=True).serialize()),
        ("tx", TxMsg(better, include_witness=True).serialize()),
        ("tx", TxMsg(pair.parent, include_witness=True).serialize()),
        ("ping", b"12345678"),
    ):
        peer.queued_recv_bytes += len(payload)
        node.p2p_manager.messages.append((kind, payload, peer.id, len(payload), 0.0))
        p2p_main.handle_p2p(node)
    settle(node)
    assert seen == [(True, [])]


def test_a_message_is_held_while_the_peer_has_an_orphan_to_reconsider(
    regtest_node: Callable[..., Node],
) -> None:
    """With no check queued, the orphan alone holds the peer's next message."""
    pair = a_pair(regtest_node, parent_fee=50_000)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    node.download_manager.orphanage.add_children_to_work_set(pair.parent)
    assert not node.tx_checks.busy(peer.id)
    held = ("ping", b"", 0, 0.0)
    assert p2p_main._wait_for_tx_check(node, peer.id, held)
    assert list(node.tx_checks.waiting[peer.id]) == [held]


def test_an_orphan_still_missing_an_input_stays_an_orphan(
    regtest_node: Callable[..., Node],
) -> None:
    """The next orphan is tried and, with none left, nothing is progress."""
    pair = a_pair(regtest_node, parent_fee=50_000)
    node, peer = pair.node, a_connected_peer(pair.node)
    two = replace(
        pair.child,
        vin=[*pair.child.vin, TxIn(OutPoint(b"\x09" * 32, 0), b"", 0xFFFFFFFF)],
    )
    relay(node, peer, two)
    relay(node, peer, pair.parent)
    assert node.download_manager.orphanage.have_tx(two.hash)
    assert not node.mempool.contains_tx(two)
    assert not process_orphan(node, peer)


def test_no_orphan_is_taken_up_for_a_peer_that_left(
    regtest_node: Callable[..., Node],
) -> None:
    """`DownloadManager` erases its orphans on its next step."""
    pair = a_pair(regtest_node, parent_fee=50_000)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    orphanage = node.download_manager.orphanage
    orphanage.add_children_to_work_set(pair.parent)
    del node.p2p_manager.connections[peer.id]
    resume_tx_checks(node)
    assert orphanage.peers_to_reconsider() == [peer.id]


def test_no_orphan_is_taken_up_for_a_peer_with_a_check_queued(
    regtest_node: Callable[..., Node],
) -> None:
    """One candidate per peer at a time."""
    pair = a_pair(regtest_node, parent_fee=50_000)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    orphanage = node.download_manager.orphanage
    orphanage.add_children_to_work_set(pair.parent)
    cast("Any", node)._worker_pool = UnfinishedPool()
    node.tx_checks.queue(TxCheck(peer, pair.parent, []))
    resume_tx_checks(node)
    assert orphanage.peers_to_reconsider() == [peer.id]


def test_a_failure_taking_up_an_orphan_is_handled_as_a_message_is(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure is logged, the peer kept, and the loop goes on."""
    pair = a_pair(regtest_node, parent_fee=50_000)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)

    def raises(node: Node, conn: Any) -> bool:
        node.download_manager.orphanage.get_tx_to_reconsider(conn.id)
        message = "boom"
        raise RuntimeError(message)

    monkeypatch.setattr(p2p_main, "process_orphan", raises)
    relay(node, peer, pair.parent)
    assert peer.id in node.p2p_manager.connections
    assert not node.download_manager.orphanage.peers_to_reconsider()


def test_a_package_whose_parent_comes_to_pass_alone_is_queued_alone(
    regtest_node: Callable[..., Node],
) -> None:
    """The floor fell between the check of the package and its scripts."""
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    cb.tx(node, TxMsg(pair.parent, include_witness=True).serialize(), peer)
    (queued,) = node.tx_checks.queued.values()
    assert queued.parent is not None
    node.mempool._rolling_min_fee_rate = 0.0
    settle(node)
    assert node.mempool.contains_tx(pair.parent)
    assert node.mempool.contains_tx(pair.child)


def test_a_package_start_finds_its_parent_passing_alone(
    regtest_node: Callable[..., Node],
) -> None:
    """A parent that passes by itself is queued alone, its child to follow."""
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    node.mempool.mark_rejected_reconsiderable(pair.parent.hash)
    node.mempool._rolling_min_fee_rate = 0.0
    relay(node, peer, pair.parent)
    assert node.mempool.contains_tx(pair.parent)
    assert node.mempool.contains_tx(pair.child)


def test_a_script_check_that_breaks_is_raised_not_recorded(
    regtest_node: Callable[..., Node],
) -> None:
    """Only a refusal of the scripts is a verdict on the package."""
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    cb.tx(node, TxMsg(pair.parent, include_witness=True).serialize(), peer)
    check = node.tx_checks.unqueue(peer.id)
    with pytest.raises(RuntimeError, match="broken"):
        cb.settle_tx(node, check, RuntimeError("broken"))


def test_a_package_whose_parent_was_taken_in_meanwhile_marks_nothing_refused(
    regtest_node: Callable[..., Node],
) -> None:
    """Another peer's copy, or a call, took the parent in while the check ran.

    The start of a check drops one whose child is held, but not one whose
    parent is. The package would fail on its parent as "txn-already-in-mempool"
    and put a held transaction in the filter of refusals.
    """
    pair = a_pair(regtest_node)
    node, peer = pair.node, a_connected_peer(pair.node)
    relay(node, peer, pair.child)
    cb.tx(node, TxMsg(pair.parent, include_witness=True).serialize(), peer)
    assert node.tx_checks.queued
    parent = pair.parent
    assert node.mempool.add_tx(parent, 1000, parent.vsize)
    settle(node)
    assert node.mempool.contains_tx(parent)
    assert not node.mempool.was_recently_rejected(parent.hash)
    assert not node.mempool.was_recently_rejected(pair.child.hash)


def test_a_package_queued_for_two_peers_marks_nothing_refused_for_the_second(
    regtest_node: Callable[..., Node],
) -> None:
    """Each peer's copy of the parent starts the package, and one is taken in.

    Core validates a package at once, so a second copy of the parent is
    `AlreadyHaveTx`. Here the second package would fail on its parent as
    "txn-already-in-mempool", and mark a held transaction refused. The
    loop drops it for its held child before the check starts, and
    `_settle_package` drops one already in flight: either alone does it.
    """
    pair = a_pair(regtest_node)
    node = pair.node
    first, second = a_connected_peer(node, 3), a_connected_peer(node, 4)
    relay(node, first, pair.child)
    node.download_manager.orphanage.add_announcer(pair.child.hash, second.id)
    for peer in (first, second):
        cb.tx(node, TxMsg(pair.parent, include_witness=True).serialize(), peer)
    assert len(node.tx_checks.queued) == 2
    settle(node)
    assert node.mempool.contains_tx(pair.parent)
    assert node.mempool.contains_tx(pair.child)
    assert not node.mempool.was_recently_rejected(pair.parent.hash)
    assert not node.mempool.was_recently_rejected(pair.child.hash)
