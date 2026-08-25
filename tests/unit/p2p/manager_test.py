# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Which peers the manager keeps, drops and reaches for.

The functional tests connect two nodes that stay connected. What this
is about is the housekeeping around that: a peer that has gone quiet, a
peer that cannot be dialled, an address already connected to, and the
messages addressed to a connection that is no longer there.
"""

import asyncio
import socket
import threading
import time
from contextlib import closing, suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast, override

import pytest
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2
from btclib.p2p.keepalive import Ping

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p import manager as manager_module
from btclib_node.p2p.address import PeerDB, endpoint_key, peer_address
from btclib_node.p2p.manager import P2pManager

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from btclib.p2p.payload import Payload

    from btclib_node import Node
from tests.helpers import (
    WaitTimeoutError,
    generate_random_transaction,
    get_random_port,
    wait_until,
    wait_until_listening,
)


def a_conn(
    conn_id: int,
    *,
    status: P2pConnStatus = P2pConnStatus.Connected,
    last_receive: float | None = None,
    address: NetworkAddressV2 | None = None,
    relay_tx: bool = True,
    feefilter: int = 0,
) -> Any:
    """Build a `Connection` double: no socket, its own `sent`/`stopped` logs.

    `send_ping` on this double does not send a real ping: it records
    one and backdates `ping_sent` well past the idle bound, standing in
    for a ping already sent and never answered.
    """
    conn = SimpleNamespace(
        id=conn_id,
        status=status,
        address=address or peer_address("1.2.3.4", 18444),
        last_receive=time.time() if last_receive is None else last_receive,
        ping_sent=0,
        relay_tx=relay_tx,
        feefilter=feefilter,
        sent=[],
        stopped=[],
    )
    conn.send = conn.sent.append
    conn.stop = lambda: conn.stopped.append(True)

    def send_ping() -> None:
        # a ping already answered by nothing: the manager reads the time
        # it was sent to decide the peer is gone
        conn.ping_sent = time.time() - 200
        conn.sent.append("ping")

    conn.send_ping = send_ping
    return conn


def a_peer_db_stub(**attributes: Any) -> Any:
    """Build a `PeerDB` double good enough for `manage_connections`'s own loop.

    `get_active_addresses` is on every one of them: the loop calls it
    once `_ACTIVE_PRUNE_INTERVAL` has passed regardless of what else a
    test's own scenario does, btclib-org/btclib-node#71, so a peer db
    missing it fails a test on an `AttributeError` the test is not
    about.
    """
    defaults: dict[str, Any] = {"get_active_addresses": list}
    defaults.update(attributes)
    return SimpleNamespace(**defaults)


class AManagerFactory(Protocol):
    """The shape of the `a_manager` fixture below, for typing its callers."""

    def __call__(
        self,
        conns: Sequence[Any] = (),
        *,
        peer_db: Any = None,
        status: NodeStatus = NodeStatus.BlockSynced,
        port: int = 18444,
    ) -> P2pManager:
        """Build a `P2pManager` seeded with `conns`, `peer_db` and `status`."""
        ...


@pytest.fixture
def a_manager() -> Iterator[AManagerFactory]:
    """Build managers, and close their event loops however the test ends."""
    made: list[P2pManager] = []

    def make(
        conns: Sequence[Any] = (),
        *,
        peer_db: Any = None,
        status: NodeStatus = NodeStatus.BlockSynced,
        port: int = 18444,
    ) -> P2pManager:
        node = SimpleNamespace(
            status=status,
            chain=RegTest(),
            logger=SimpleNamespace(
                info=lambda *a: None,
                debug=lambda *a: None,
                exception=lambda *a: None,
            ),
            # `broadcast_raw_transaction` no longer sends anything of its
            # own: it hands the transaction to this, the same queue a
            # relayed transaction goes through. btclib-org/btclib-node#141
            download_manager=SimpleNamespace(received_txs=[]),
        )
        # a peer db that refuses to be asked by default: a test that
        # should not reach for a peer proves it by the log staying quiet
        manager = P2pManager(
            cast("Node", node),
            port,
            cast(
                "PeerDB",
                peer_db
                or a_peer_db_stub(
                    is_empty=True,
                    random_address=refuses_to_be_asked,
                    get_addr_from_dns=asks_no_dns_server,
                ),
            ),
        )
        for conn in conns:
            manager.connections[conn.id] = conn
        made.append(manager)
        return manager

    yield make
    for manager in made:
        # stopped first where a test left it running: a loop cannot be
        # closed out from under the thread inside it, and a manager
        # thread outliving its test is non-daemon -- so a test that
        # fails before its own stop would otherwise take the run with
        # it rather than the test
        if manager.is_alive():
            manager.stop()
            manager.join(timeout=10)
        else:
            manager.loop.close()


async def one_pass(manager: P2pManager) -> bool:
    """Run the housekeeping loop's body exactly once.

    `ensure_future` queues the task's first step ahead of the timer, so
    the body runs before the cancel however slow the machine is. Two
    passes is this twice, rather than a sleep long enough for the loop's
    own -- which is a wait on the scheduler, and #46's shape.
    """
    task = asyncio.ensure_future(manager.manage_connections())
    await asyncio.sleep(0.05)
    still_running = not task.done()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    return still_running


def test_removing_a_connection_that_is_not_there_changes_nothing(
    a_manager: AManagerFactory,
) -> None:
    """Removing an id nobody holds leaves the real connection untouched."""
    conn = a_conn(1)
    manager = a_manager([conn])
    manager.remove_connection(99)
    assert list(manager.connections) == [1]
    assert not conn.stopped


def test_removing_a_connection_stops_it(a_manager: AManagerFactory) -> None:
    """`remove_connection` both drops it from `connections` and stops it."""
    conn = a_conn(1)
    manager = a_manager([conn])
    manager.remove_connection(1)
    assert not manager.connections
    assert conn.stopped == [True]


def test_removing_a_connection_still_pending_stops_it_too(
    a_manager: AManagerFactory,
) -> None:
    """`remove_connection` also stops a `pending_connections` entry."""
    conn = a_conn(1, status=P2pConnStatus.Open)
    manager = a_manager()
    manager.pending_connections[conn.id] = conn
    manager.remove_connection(1)
    assert not manager.pending_connections
    assert conn.stopped == [True]


def test_a_promote_racing_remove_connection_waits_for_its_own_two_pops(
    a_manager: AManagerFactory,
) -> None:
    """#358: `promote_connection` waits on a `remove_connection` mid-pop.

    Reached from a real second thread while `remove_connection` is
    still between its own two pops, `promote_connection` waits on
    `_connections_lock` rather than slipping into the gap -- the
    interleaving the issue names (the first pop misses because the
    connection is still pending, `promote_connection` runs whole, the
    second pop misses because promotion already took it) is what this
    rules out.
    """
    conn = a_conn(1, status=P2pConnStatus.Open)
    manager = a_manager()
    manager.pending_connections[1] = conn

    # Set only from the background thread's own `__enter__`, right
    # before it blocks on the real lock -- the main thread is the one
    # already holding it, from inside `remove_connection`'s own first
    # pop below, so this firing is what proves the background thread
    # reached the lock rather than running unguarded ahead of it.
    other_thread_about_to_block = threading.Event()
    real_lock = manager._connections_lock

    class SignallingLock:
        def __enter__(self) -> None:
            if threading.current_thread() is not threading.main_thread():
                other_thread_about_to_block.set()
            real_lock.acquire()

        def __exit__(self, *exc_info: object) -> None:
            real_lock.release()

    manager._connections_lock = cast("Any", SignallingLock())

    promote_thread = threading.Thread(target=manager.promote_connection, args=(1,))

    class HookedConnections(dict[int, Any]):
        @override
        def pop(self, key: int, default: Any = None) -> Any:
            result = dict.pop(self, key, default)
            promote_thread.start()
            # A bound only against a hang: on the unfixed tree
            # `promote_connection` takes no lock at all, so this event
            # is never set and the wait always exhausts its timeout
            # rather than the interleaving ever being confirmed.
            other_thread_about_to_block.wait(timeout=5)
            return result

    manager.connections = HookedConnections(manager.connections)

    manager.remove_connection(1)
    promote_thread.join(timeout=5)

    assert not promote_thread.is_alive()
    assert conn.stopped == [True]
    assert not manager.connections
    assert not manager.pending_connections


def test_discourage_marks_the_endpoint_dialled_or_accepted(
    a_manager: AManagerFactory,
) -> None:
    """`discourage` keys the endpoint by `endpoint_key`, not the raw address."""
    manager = a_manager()
    address = peer_address("1.2.3.4", 18444)
    manager.discourage(address)
    assert endpoint_key(address) in manager.discouraged


def test_promoting_a_connection_moves_it_into_connections(
    a_manager: AManagerFactory,
) -> None:
    """`promote_connection` moves a pending connection into `connections`."""
    conn = a_conn(1, status=P2pConnStatus.Open)
    manager = a_manager()
    manager.pending_connections[conn.id] = conn
    manager.promote_connection(1)
    assert list(manager.connections) == [1]
    assert not manager.pending_connections


def test_promoting_a_connection_that_is_not_pending_changes_nothing(
    a_manager: AManagerFactory,
) -> None:
    """`promote_connection` on an id nobody is waiting on does nothing."""
    manager = a_manager()
    manager.promote_connection(99)
    assert not manager.connections
    assert not manager.pending_connections


def test_a_peer_that_cannot_be_dialled_is_not_kept(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `dial` that comes back with nothing leaves no connection behind."""

    async def never_connects(address: NetworkAddressV2) -> None:
        return None

    monkeypatch.setattr(manager_module, "dial", never_connects)
    manager = a_manager()
    asyncio.run(manager.async_connect(peer_address("1.2.3.4", 18444)))
    assert not manager.connections
    assert not manager.pending_connections


def test_a_connection_that_has_closed_is_let_go_of(a_manager: AManagerFactory) -> None:
    """One pass of the housekeeping loop drops a connection already `Closed`."""
    conn = a_conn(1, status=P2pConnStatus.Closed)
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert not manager.connections


def test_a_peer_that_has_gone_quiet_is_pinged_and_then_dropped(
    a_manager: AManagerFactory,
) -> None:
    """An idle peer is pinged first, and dropped only past a second idle pass.

    `a_conn`'s own `send_ping` backdates `ping_sent` on the spot, so
    the second pass finds the ping already unanswered rather than
    waiting for a real one to time out.
    """
    conn = a_conn(1, last_receive=time.time() - 200)
    manager = a_manager([conn])

    async def pinged_then_dropped() -> None:
        await one_pass(manager)
        assert conn.sent == ["ping"]
        assert list(manager.connections) == [1]
        await one_pass(manager)

    asyncio.run(pinged_then_dropped())
    assert not manager.connections


def test_a_peer_that_answered_recently_is_left_alone(
    a_manager: AManagerFactory,
) -> None:
    """A peer heard from recently is neither pinged nor dropped."""
    conn = a_conn(1)
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert conn.sent == []
    assert list(manager.connections) == [1]


def test_a_pong_landing_between_the_idle_check_and_its_reread_does_not_drop_the_peer(
    a_manager: AManagerFactory,
) -> None:
    """#357's first interleaving: a `pong` between two rereads of `ping_sent`.

    `_prune_stale_connections` used to read `conn.ping_sent` twice -- once for
    `if not conn.ping_sent` and again for the `elif` right after -- so a
    `callbacks.pong` on the other thread clearing it to 0 between the two reads
    made `now - 0 > _IDLE_TIMEOUT` true for a peer that had just answered its
    ping.

    Driven deterministically rather than by timing an actual thread: a
    `ping_sent` that answers a recent timestamp on its first read and 0 -- what
    a pong's own clear would leave behind -- on any read after that stands in
    for the interleaving without needing one.
    """
    reads = iter([time.time(), 0])

    class ConnDouble:
        id = 1
        status = P2pConnStatus.Connected
        address = peer_address("1.2.3.4", 18444)
        last_receive = time.time() - 200
        relay_tx = True
        feefilter = 0

        @property
        def ping_sent(self) -> float:
            return next(reads)

    conn = ConnDouble()
    manager = a_manager([cast("Any", conn)])
    asyncio.run(one_pass(manager))
    # neither branch below the single read fires: `send_ping` and
    # `stop` are not even defined on this double, so either firing
    # would end the test on an `AttributeError` rather than on this
    # assertion
    assert list(manager.connections) == [1]


def test_a_pending_connection_that_has_closed_is_let_go_of(
    a_manager: AManagerFactory,
) -> None:
    """One pass drops a `pending_connections` entry already `Closed`, too."""
    conn = a_conn(1, status=P2pConnStatus.Closed)
    manager = a_manager()
    manager.pending_connections[conn.id] = conn
    asyncio.run(one_pass(manager))
    assert not manager.pending_connections


def test_a_pending_connection_gone_quiet_is_dropped_without_a_ping(
    a_manager: AManagerFactory,
) -> None:
    """An idle connection still mid-handshake is dropped, never pinged.

    `ping` is as much a message the handshake has to clear before it
    is sent as `inv`/`tx` is, so a connection stuck short of `verack`
    is dropped once idle rather than pinged and given a second window.
    """
    conn = a_conn(1, status=P2pConnStatus.Open, last_receive=time.time() - 200)
    manager = a_manager()
    manager.pending_connections[conn.id] = conn
    asyncio.run(one_pass(manager))
    assert not manager.pending_connections
    assert conn.sent == []


def a_counting_prune() -> tuple[list[None], Any]:
    """Build a `get_active_addresses` stub that records every call it answers.
    """
    calls: list[None] = []

    def get_active_addresses() -> list[Any]:
        calls.append(None)
        return []

    return calls, get_active_addresses


def test_the_active_table_is_pruned_without_being_asked(
    a_manager: AManagerFactory,
) -> None:
    """The active table is pruned once per pass, whether or not it is asked.

    #71: `get_active_addresses`'s own prune only ever ran behind
    something that already called it -- `random_address`, which this
    loop stops reaching for once it has enough connections, and
    `getaddr`, answered once per connection and never again -- so a
    well-connected node nobody asks a `getaddr` would otherwise never
    prune a stale row.
    """
    calls, get_active_addresses = a_counting_prune()
    peer_db = a_peer_db_stub(is_empty=True, get_active_addresses=get_active_addresses)
    manager = a_manager(peer_db=peer_db)
    asyncio.run(one_pass(manager))
    asyncio.run(one_pass(manager))
    assert len(calls) == 1


def test_the_active_table_prune_repeats_once_the_interval_passes(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second prune runs once `_ACTIVE_PRUNE_INTERVAL` elapses, not sooner.

    The previous test checks that two passes inside the interval prune only
    once; this pushes the clock forward past the interval between two passes and
    checks the count goes from one to two.
    """
    calls, get_active_addresses = a_counting_prune()
    peer_db = a_peer_db_stub(is_empty=True, get_active_addresses=get_active_addresses)
    manager = a_manager(peer_db=peer_db)
    asyncio.run(one_pass(manager))
    future = time.time() + manager_module._ACTIVE_PRUNE_INTERVAL + 1
    monkeypatch.setattr(time, "time", lambda: future)
    asyncio.run(one_pass(manager))
    assert len(calls) == 2


def raises_pruning() -> NoReturn:
    """Stand in for a `get_active_addresses` whose own `db.delete` raised."""
    raise RuntimeError("no")


def test_a_peer_db_that_raises_pruning_does_not_stop_the_housekeeping(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `get_active_addresses` that raises while pruning logs, not crashes.

    Whatever `get_active_addresses`'s own `db.delete` ever raised, and
    `manage_connections`'s own future is never awaited, so letting one
    out unhandled would end the loop for the rest of this node's life
    rather than only this one pass -- btclib-org/btclib-node#71.
    """
    logged: list[str] = []
    peer_db = a_peer_db_stub(is_empty=True, get_active_addresses=raises_pruning)
    manager = a_manager(peer_db=peer_db)
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    assert asyncio.run(one_pass(manager)) is True
    assert logged


def test_a_pending_connection_still_within_the_window_is_left_alone(
    a_manager: AManagerFactory,
) -> None:
    """A pending connection heard from recently is neither pinged nor dropped.
    """
    conn = a_conn(1, status=P2pConnStatus.Open)
    manager = a_manager()
    manager.pending_connections[conn.id] = conn
    asyncio.run(one_pass(manager))
    assert list(manager.pending_connections) == [1]
    assert conn.sent == []


def test_a_pending_connection_also_counts_toward_the_connection_target(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection still pending counts toward the target, so no second dial.

    One already pending fills the one-peer target before headers are
    synced: reaching for a second would raise into the housekeeping
    loop's own handler, so a quiet log is the assertion that it did not.
    """
    conn = a_conn(1, status=P2pConnStatus.Open)
    peer_db = a_peer_db_stub(is_empty=False, random_address=refuses_to_be_asked)
    manager = a_manager(peer_db=peer_db, status=NodeStatus.Starting)
    manager.pending_connections[conn.id] = conn
    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(one_pass(manager))
    assert not logged


def test_an_address_already_connected_to_is_not_dialled_again(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer already in `connections` is skipped, not redrawn and redialled.

    An onion address, which this node cannot dial: reaching for it
    would raise into the housekeeping loop's own handler, so a quiet
    log is the assertion that the manager never reached it.
    """
    onion = NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)
    conn = a_conn(1, address=onion)
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: onion)
    manager = a_manager([conn], peer_db=peer_db)
    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(one_pass(manager))
    assert not logged
    assert list(manager.connections) == [1]


def test_a_discouraged_address_is_not_dialled_again(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discouraged endpoint is skipped without ever reaching a real dial.

    Issue #283: an onion address, the same way the already-connected
    sibling test above proves a skip -- reaching the real `dial` would
    raise on a network this node cannot open a socket for, straight
    into the same quiet-log assertion.
    """
    onion = NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: onion)
    manager = a_manager(peer_db=peer_db)
    manager.discouraged.add(endpoint_key(onion))
    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(one_pass(manager))
    assert not logged
    assert not manager.connections
    assert not manager.pending_connections


def test_a_connected_peer_drawn_with_a_different_timestamp_is_not_redialled(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer drawn back with a different timestamp is still not redialled.

    #70/#71: callbacks.verack records the peer at a live timestamp and
    # with its handshake's own services, so the row PeerDB.random_address
    # can draw back is never equal, field for field, to the Connection's
    # own address -- endpoint_key is what the manager has to compare on
    # instead, or a peer already connected to is dialled a second time.
    An onion address the same way the sibling tests above use one: `not
    in already_connected` regressing to raw equality would reach the
    real `dial`, which raises on a network this node cannot open a
    socket for, straight into the same quiet-log assertion those use.
    """
    onion = NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)
    conn = a_conn(1, address=onion)
    gossiped = NetworkAddressV2(
        int(time.time()), 1, BIP155Network.TORV3, b"\x11" * 32, 8333
    )
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: gossiped)
    manager = a_manager([conn], peer_db=peer_db)
    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(one_pass(manager))
    assert not logged
    assert list(manager.connections) == [1]


def test_a_pending_connection_s_address_is_not_dialled_again_either(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer still mid-handshake counts as already connected too, for dialling.

    The same skip checked above against `connections` is checked here
    against `pending_connections` instead.
    """
    onion = NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)
    conn = a_conn(1, status=P2pConnStatus.Open, address=onion)
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: onion)
    manager = a_manager(peer_db=peer_db)
    manager.pending_connections[conn.id] = conn
    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(one_pass(manager))
    assert not logged
    assert list(manager.pending_connections) == [1]


def test_a_promote_racing_the_snapshot_still_counts_as_already_connected(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#355: a `promote_connection` racing the connected count is still counted.

    `_maybe_dial_more_peers` reads `connections` and `pending_connections` under
    `_connections_lock`, so a `promote_connection` racing from a real second
    thread cannot land between the two reads and go uncounted by both -- which
    is what would let this pass redial a peer whose handshake just completed.
    """
    address = peer_address("1.2.3.4", 18444)
    conn = a_conn(1, status=P2pConnStatus.Open, address=address)
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: address)
    manager = a_manager(peer_db=peer_db)
    manager.pending_connections[1] = conn

    # Set only from the background thread's own `__enter__`, right
    # before it blocks on the real lock -- the main thread is the one
    # already holding it, from inside the snapshot below, so this
    # firing is what proves the background thread reached the lock
    # rather than running unguarded ahead of it.
    other_thread_about_to_block = threading.Event()
    real_lock = manager._connections_lock

    class SignallingLock:
        def __enter__(self) -> None:
            if threading.current_thread() is not threading.main_thread():
                other_thread_about_to_block.set()
            real_lock.acquire()

        def __exit__(self, *exc_info: object) -> None:
            real_lock.release()

    manager._connections_lock = cast("Any", SignallingLock())

    promote_thread = threading.Thread(target=manager.promote_connection, args=(1,))

    class HookedPending(dict[int, Any]):
        @override
        def values(self) -> Any:
            # called exactly once, from the snapshot below -- `len()`,
            # the only other reader of this dict in the method, does
            # not go through `values()`
            result = dict.values(self)
            promote_thread.start()
            # A bound only against a hang: on the unfixed tree
            # `promote_connection` takes no lock at all, so this
            # event is never set and the wait always exhausts its
            # timeout rather than the interleaving ever being
            # confirmed.
            other_thread_about_to_block.wait(timeout=5)
            return result

    manager.pending_connections = HookedPending(manager.pending_connections)

    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(manager._maybe_dial_more_peers())
    promote_thread.join(timeout=5)

    assert not promote_thread.is_alive()
    assert not logged
    assert list(manager.connections) == [1]
    assert not manager.pending_connections


def test_a_promote_racing_the_count_does_not_dial_past_the_target(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#367: a promote racing the peer count must not dial past the target.

    `_maybe_dial_more_peers` reads `live` under `_connections_lock` too, not
    only the snapshot below it -- a `promote_connection` racing between two
    unlocked `len()` calls could undercount a node that already has enough
    peers, one call reading `connections` before the write and the other reading
    `pending_connections` after the pop, and this pass would then dial past the
    target it was told to stop at.
    """
    onion = NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)
    conn = a_conn(1, status=P2pConnStatus.Open)
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: onion)
    manager = a_manager(peer_db=peer_db, status=NodeStatus.SyncingHeaders)
    manager.pending_connections[1] = conn

    promote_done = threading.Event()

    def promote_and_signal() -> None:
        manager.promote_connection(1)
        promote_done.set()

    promote_thread = threading.Thread(target=promote_and_signal)

    class HookedPending(dict[int, Any]):
        @override
        def __len__(self) -> int:
            # called exactly once, from the count below -- `.values()`,
            # the snapshot's own reader further down, is never reached
            # once the count answers on its own
            promote_thread.start()
            # Not a race, on either side: `promote_connection` takes
            # the same lock this count is read under, so on the fixed
            # tree it cannot finish inside this wait whatever the
            # timeout is -- a `threading.Lock` already held by this
            # method admits no second entrant, ever, not merely a slow
            # one. On the unfixed tree nothing here contends that lock
            # at all, and a pop and a dict write finish well inside it.
            promote_done.wait(timeout=1)
            return dict.__len__(self)

    manager.pending_connections = HookedPending(manager.pending_connections)

    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(manager._maybe_dial_more_peers())
    promote_thread.join(timeout=5)

    assert not promote_thread.is_alive()
    assert not logged
    assert list(manager.connections) == [1]
    # `dict.__len__`, bypassing the hook above: `list(...)` calls
    # `__len__` too, as a size hint, and both that and `not
    # manager.pending_connections` would start `promote_thread` a
    # second time, which it refuses
    assert dict.__len__(manager.pending_connections) == 0


def test_a_dial_that_comes_back_with_nothing_adds_no_connection(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dial reached from the housekeeping loop that fails adds no connection.
    """

    async def comes_back_with_nothing(address: NetworkAddressV2) -> None:
        return None

    monkeypatch.setattr(manager_module, "dial", comes_back_with_nothing)
    peer_db = a_peer_db_stub(
        is_empty=False,
        random_address=lambda: peer_address("5.6.7.8", 18444),
    )
    manager = a_manager(peer_db=peer_db)
    asyncio.run(one_pass(manager))
    assert not manager.connections
    assert not manager.pending_connections


def refuses_to_be_asked() -> NoReturn:
    """Stand in for `random_address`/`get_active_addresses`, unreachable."""
    raise RuntimeError("no")


async def asks_no_dns_server() -> None:
    """Stand in for a `get_addr_from_dns` that never touches a real server."""
    return


def test_a_peer_db_that_raises_does_not_stop_the_housekeeping(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `random_address` that raises logs and lets the housekeeping loop go on.
    """
    logged: list[str] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=refuses_to_be_asked)
    manager = a_manager(peer_db=peer_db)
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    # still running when the pass ended: catching the exception and
    # returning would leave the node with no housekeeping at all
    assert asyncio.run(one_pass(manager)) is True
    assert logged


def test_only_one_peer_is_wanted_until_the_headers_are_synced(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before headers are synced, one connected peer is enough; no second dial.

    A peer db that refuses to be asked: reaching for a second peer
    would raise into the housekeeping loop's own handler, so a quiet
    log is the assertion that one peer was enough.
    """
    conn = a_conn(1)
    peer_db = a_peer_db_stub(is_empty=False, random_address=refuses_to_be_asked)
    manager = a_manager([conn], peer_db=peer_db, status=NodeStatus.Starting)
    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(one_pass(manager))
    assert not logged


def test_a_connection_removed_between_the_check_and_the_send_is_not_a_keyerror(
    a_manager: AManagerFactory,
) -> None:
    """#359: `send` on a connection popped mid-lookup does not raise `KeyError`.

    `send` reads `.get()`, one dict lookup, rather than an `in` check
    followed by a subscript -- a connection popped between the two
    (`remove_connection`, on this manager's own loop, on every pass of
    `manage_connections`) reached the caller as a `KeyError` out of the
    subscript before this.
    """

    class PoppingOnContains(dict[int, Any]):
        @override
        def __contains__(self, key: object) -> bool:
            found = dict.__contains__(self, key)
            if found:
                # simulates `remove_connection` popping the instant
                # `in` answers True, before the subscript that used to
                # follow it
                dict.pop(self, cast("int", key), None)
            return found

    conn = a_conn(1)
    manager = a_manager()
    manager.connections = PoppingOnContains({1: conn})

    # Both of `__contains__`'s own branches, the same two answers the
    # old `in`-then-subscript shape got before this fix dropped the
    # `in` step: found once, popping as a side effect, then not found.
    assert 1 in manager.connections
    assert 1 not in manager.connections

    manager.connections[1] = conn
    manager.send(cast("Payload", "message"), 1)
    assert conn.sent == ["message"]


def test_a_message_for_a_connection_that_is_gone_is_dropped(
    a_manager: AManagerFactory,
) -> None:
    """`send` to an unknown id is dropped; a real one still gets the message."""
    conn = a_conn(1)
    manager = a_manager([conn])
    manager.send(cast("Payload", "message"), 99)
    assert conn.sent == []
    manager.send(cast("Payload", "message"), 1)
    assert conn.sent == ["message"]


def test_every_connection_is_pinged_and_every_connection_is_stopped(
    a_manager: AManagerFactory,
) -> None:
    """`ping_all` skips a pending peer, `stop_all` closes it anyway.

    `ping` is post-handshake like `inv`/`tx`, so a connection still
    mid-handshake is not one `ping_all` reaches (btclib-org/btclib-node#131);
    shutdown is different, and `stop_all` closes it regardless.
    """
    first, second = a_conn(1), a_conn(2)
    pending = a_conn(3, status=P2pConnStatus.Open)
    manager = a_manager([first, second])
    manager.pending_connections[pending.id] = pending
    manager.ping_all()
    assert first.sent == ["ping"]
    assert second.sent == ["ping"]
    # `ping` is as much a post-handshake message as `inv`/`tx` is, so a
    # connection still finishing its handshake is not one of "every
    # connection" ping_all reaches: btclib-org/btclib-node#131
    assert pending.sent == []
    manager.stop_all()
    assert first.stopped == [True]
    assert second.stopped == [True]
    # shutdown is different: a socket mid-handshake still gets closed
    assert pending.stopped == [True]


def test_a_transaction_of_our_own_is_handed_to_the_download_manager(
    a_manager: AManagerFactory,
) -> None:
    """`broadcast_raw_transaction` queues into `download_manager`, nothing else.

    The RPC's `sendrawtransaction`, which is the other way a transaction leaves
    this node: `DownloadManager.tx_download` is what turns this into an `inv` --
    on its own per-peer schedule, gated on `relay_tx` and unreachable from a
    connection still mid-handshake exactly as a relayed transaction's own entry
    in the same list is -- rather than this method pushing a `Tx` of its own the
    instant it is called, which is the distinguisher #141 is about.
    """
    manager = a_manager()
    tx = generate_random_transaction()
    manager.broadcast_raw_transaction(tx, 1000)
    assert manager.node.download_manager.received_txs == [(None, tx.hash)]


def test_a_peer_that_was_pinged_recently_is_given_time_to_answer(
    a_manager: AManagerFactory,
) -> None:
    """An idle peer already pinged recently is not pinged again or dropped."""
    conn = a_conn(1, last_receive=time.time() - 200)
    conn.ping_sent = time.time()
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert conn.sent == []
    assert list(manager.connections) == [1]


def test_an_empty_peer_db_is_not_asked_for_an_address(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`is_empty` skips the draw entirely, rather than drawing from nothing.

    Nothing to draw from: asking anyway is how a node with no peers
    spends its housekeeping raising and logging.
    """
    manager = a_manager()
    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(one_pass(manager))
    assert not logged


def test_a_peer_db_with_nothing_dialable_is_a_pass_that_does_nothing(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `random_address` of `None` with `is_empty` false is a quiet no-op pass.

    `is_empty` is false and the draw still comes back with nothing: a table of
    onion and CJDNS addresses answers this way. The pass has to do nothing and
    come round again -- dialling the nothing it was handed would raise into the
    loop's own handler once every tenth of a second.
    """
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: None)
    manager = a_manager(peer_db=peer_db)
    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    assert asyncio.run(one_pass(manager)) is True
    assert not logged
    assert not manager.connections


def test_a_peer_that_answers_the_dial_becomes_a_connection(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful dial lands a pending, outbound connection with its socket.
    """
    ours, theirs = socket.socketpair()

    async def answers(address: NetworkAddressV2) -> socket.socket:
        return ours

    monkeypatch.setattr(manager_module, "dial", answers)
    peer_db = a_peer_db_stub(
        is_empty=False,
        random_address=lambda: peer_address("5.6.7.8", 18444),
    )
    manager = a_manager(peer_db=peer_db)

    async def dial() -> None:
        await one_pass(manager)
        # dialled, not yet handshaken: `create_connection` is what
        # `one_pass` reaches, and it starts every connection pending
        (conn,) = manager.pending_connections.values()
        assert conn.client is ours
        assert not conn.inbound
        assert conn.task is not None
        conn.task.cancel()
        await asyncio.sleep(0)

    with ours, theirs:
        # on the manager's own loop, which is the one it schedules the
        # connection's loop on. asyncio.run would build a second one and
        # leave the manager holding a loop the fixture then never closes
        manager.loop.run_until_complete(dial())


def a_running_manager(a_manager: AManagerFactory, port: int) -> P2pManager:
    """Build and start a `P2pManager`, without waiting for it to be listening.
    """
    manager = a_manager(port=port)
    manager.start()
    return manager


def test_a_manager_says_when_it_is_listening_and_not_before(
    a_manager: AManagerFactory,
) -> None:
    """#46: `is_alive()` holds before `run` has bound anything.

    A peer dialled on the strength of it is refused, silently and once,
    and the test that dialled then waits out its whole timeout. What
    this pins is the answer to that: an event the manager sets when the
    socket is bound, after which an accept cannot be missed.
    """
    port = get_random_port()
    manager = a_manager(port=port)
    # what the event answers is the bind and not the thread: a manager
    # that has not been started is not listening, and neither is one
    # whose `run` has not yet reached the coroutine that binds
    assert not manager.listening.is_set()
    manager.start()
    try:
        wait_until_listening(manager)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as peer:
            # a raw socket, not a peer that speaks the protocol: nothing
            # answers this node's `version`, so the handshake never
            # reaches `verack` and the accepted connection stays pending
            wait_until(lambda: manager.pending_connections)
            (conn,) = manager.pending_connections.values()
            assert conn.inbound
            assert conn.address.port == peer.getsockname()[1]
            # the version this node opens with: the socket was accepted
            # into a connection and not merely into the backlog
            peer.settimeout(20)
            assert peer.recv(4096)
    finally:
        manager.stop()
        manager.join(timeout=10)
    assert not manager.is_alive()


def test_a_manager_accepts_an_ipv6_peer_too(a_manager: AManagerFactory) -> None:
    """A manager also binds IPv6, and accepts a peer that dials it over `::1`.
    """
    port = get_random_port()
    manager = a_running_manager(a_manager, port)
    wait_until_listening(manager)
    # held open across the stop rather than closed by a `with`, on
    # `test_stopping_a_running_manager_stops_the_connections_it_holds`'s
    # own reasoning: closing it here races the still-running
    # `Connection`'s own read against the `stop` below
    with closing(socket.create_connection(("::1", port), timeout=20)) as peer:
        wait_until(lambda: manager.pending_connections)
        (conn,) = manager.pending_connections.values()
        assert conn.address.network_id == BIP155Network.IPV6
        assert conn.address.port == peer.getsockname()[1]
        manager.stop()
        manager.join(timeout=10)


def test_a_failed_ipv6_bind_does_not_stop_the_ipv4_listener(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An IPv6 bind failure still leaves the IPv4 listener accepting peers.

    A host with no IPv6 route or support: not fatal, on the reasoning
    `_bind`'s docstring cites from Core's own `InitBinds`.
    """
    port = get_random_port()
    manager = a_manager(port=port)
    real_socket = socket.socket

    def refuses_ipv6(
        family: socket.AddressFamily, *args: Any, **kwargs: Any
    ) -> socket.socket:
        # `*args, **kwargs` and not the two positional arguments `_bind`
        # is given: a listening socket's own `accept()` builds the
        # accepted connection through this same module-level name, with
        # `fileno=` rather than a family and a kind, and a wrapper that
        # only took `_bind`'s shape would refuse every inbound peer too
        if family == socket.AF_INET6:
            # OSError, not a class of this tree's own (TRY003): `_bind`
            # itself catches `OSError`, so a double standing in for what
            # the real socket module raises has to raise that type, not
            # a lookalike `_bind` was never written to catch
            raise OSError("no ipv6 route")  # noqa: TRY003
        return real_socket(family, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", refuses_ipv6)
    manager.start()
    wait_until_listening(manager)
    # held open across the stop rather than closed by a `with`, on
    # `test_stopping_a_running_manager_stops_the_connections_it_holds`'s
    # own reasoning: a connection the far side closes first and one this
    # manager's `stop` closes are otherwise indistinguishable here
    with closing(socket.create_connection(("127.0.0.1", port), timeout=20)):
        wait_until(lambda: manager.pending_connections)
        manager.stop()
        manager.join(timeout=10)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_manager_that_cannot_bind_never_says_it_is_listening(
    a_manager: AManagerFactory,
) -> None:
    """`listening` is never set where the bind that would set it fails.

    Set after the bind and not before it, which is the whole of what a
    caller waiting on the event is told: a manager whose bind failed
    never reaches the line that sets it.

    `_bind` raises out of `run` on purpose (#88, below), so `run` ends
    in an exception on the manager's own thread that nothing there
    catches -- exactly what this test asks the bind to do, and pytest
    warns about any uncaught thread exception by default.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("", 0))
        taken.listen()
        manager = a_manager(port=taken.getsockname()[1])
        manager.start()
        try:
            with pytest.raises(WaitTimeoutError, match=r"within 0\.5 seconds"):
                wait_until_listening(manager, timeout=0.5)
        finally:
            manager.stop()
            manager.join(timeout=10)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_manager_that_cannot_bind_stops_being_alive(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#88: a bind failure used to vanish into a future nobody read.

    `server` bound inside itself, scheduled through
    `run_coroutine_threadsafe`, whose returned `concurrent.futures.Future`
    nobody read -- a taken port's `OSError` sat there unread, and the
    manager thread ran on, `is_alive()` true over a listener that never
    came up. `_bind` now runs in `run` itself, before `run_forever`, so
    the same `OSError` comes back out of `run` -- this thread's own
    target -- and the thread ends rather than lying about the socket.
    """
    logged: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("", 0))
        taken.listen()
        manager = a_manager(port=taken.getsockname()[1])
        monkeypatch.setattr(manager.logger, "exception", logged.append)
        manager.start()
        wait_until(lambda: not manager.is_alive())
    assert logged
    assert not manager.listening.is_set()


def test_a_manager_dials_the_address_it_is_given(a_manager: AManagerFactory) -> None:
    """`connect` reaches the manager's loop and dials the address it is given.

    `connect` is called from the node's thread and hands the dial to
    the manager's own loop. Dialled at itself, so what comes back is
    both ends of one connection: the one this node opened and the one
    it accepted.
    """
    port = get_random_port()
    manager = a_running_manager(a_manager, port)
    try:
        wait_until_listening(manager)
        manager.connect(peer_address("127.0.0.1", port))
        # both ends are real `Connection`s speaking the protocol to each
        # other, but nothing here drains `handshake_messages` to answer
        # either `version` with a `verack`, so both stay pending
        wait_until(lambda: len(manager.pending_connections) == 2)
        inbound = [conn.inbound for conn in manager.pending_connections.values()]
        assert sorted(inbound) == [False, True]
    finally:
        manager.stop()
        manager.join(timeout=10)


def test_a_message_sent_on_a_running_connection_reaches_the_peer(
    a_manager: AManagerFactory,
) -> None:
    """`Connection.send`, crossing from the node's thread, reaches the wire.

    `Connection.send` is called from the node's thread and hands the write to
    the manager's loop; nothing else in these tests crosses that line, and a
    message that never leaves is a peer that goes quiet for no reason.
    """
    port = get_random_port()
    manager = a_running_manager(a_manager, port)
    try:
        wait_until_listening(manager)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as peer:
            # `Connection.send` is not gated on the manager's own dicts,
            # so a raw peer's connection reaching only `pending_connections`
            # sends exactly as well as one promoted to `connections` would
            wait_until(lambda: manager.pending_connections)
            (conn,) = manager.pending_connections.values()
            conn.send(Ping(7))
            # bounded by the socket's own timeout, so a ping that never
            # arrives is a failure rather than a test that never ends
            peer.settimeout(20)
            received = b""
            while b"ping" not in received:
                chunk = peer.recv(4096)
                assert chunk
                received += chunk
    finally:
        manager.stop()
        manager.join(timeout=10)


def test_a_manager_left_running_is_stopped_by_whoever_built_it(
    a_manager: AManagerFactory,
) -> None:
    """A manager left running is exactly what the `a_manager` fixture cleans up.

    Deliberately not stopped here. A manager thread outliving its test
    is non-daemon, so a test that fails before reaching its own stop
    would hold the run open instead of failing it -- the fixture is
    where that is caught, and this is the test that proves it does.
    """
    manager = a_running_manager(a_manager, get_random_port())
    wait_until_listening(manager)
    assert manager.is_alive()


def test_stopping_a_running_manager_stops_the_connections_it_holds(
    a_manager: AManagerFactory,
) -> None:
    """`stop` closes the manager's connections and loop, clears `listening`."""
    port = get_random_port()
    manager = a_running_manager(a_manager, port)
    wait_until_listening(manager)
    # held open across the stop rather than closed by a `with`: a
    # connection this manager closed and one that ended because the far
    # side went away are indistinguishable afterwards. A wait that times
    # out before the stop below leaves the manager to the fixture, which
    # stops what it finds running.
    with closing(socket.create_connection(("127.0.0.1", port), timeout=20)):
        # a raw peer, never past the handshake, so `stop` has to reach
        # it through `pending_connections` rather than `connections`
        wait_until(lambda: manager.pending_connections)
        (conn,) = manager.pending_connections.values()
        manager.stop()
        manager.join(timeout=10)
    assert conn.status == P2pConnStatus.Closed
    assert manager.loop.is_closed()
    # and the flag goes back to meaning what it says: waiting for a
    # stopped manager to listen would otherwise return at once, on a
    # socket that is closed
    assert not manager.listening.is_set()


def test_stop_closes_a_connection_accepted_in_its_own_race_window(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#312: a connection accepted in `stop`'s own race window is still closed.

    A connection `server()`'s own accept loop creates between `stop()`
    scheduling `loop.stop` and that actually being delivered must still
    be closed, whether or not its own `run()` task ever gets a chance
    to execute before being cancelled.

    `create_connection` is called from `is_alive`, standing in for
    `server()`'s own accept loop landing one more connection in exactly
    that window -- `stop()` asks it between scheduling `loop.stop` and
    waiting for the thread, which is the window itself.

    Not from `join`, which is inside the same window but is only reached
    while the thread is still running: a manager whose loop has already
    stopped by the time `stop()` looks skips it, and the hook hung there
    never runs at all. That is a test which passes for the wrong reason
    on an idle machine and fails on a loaded one, and the run that caught
    it reported this test's own `create_connection` line as uncovered,
    which is what says the hook and not the manager was at fault.
    """
    port = get_random_port()
    manager = a_running_manager(a_manager, port)
    wait_until_listening(manager)

    ours, theirs = socket.socketpair()
    address = peer_address("127.0.0.1", 18444)
    real_is_alive = manager.is_alive
    landed: list[bool] = []

    def is_alive_after_landing_one_more() -> bool:
        landed.append(True)
        manager.create_connection(ours, address, inbound=True)
        return real_is_alive()

    monkeypatch.setattr(manager, "is_alive", is_alive_after_landing_one_more)
    try:
        manager.stop()
    finally:
        theirs.close()
    # exactly once, asserted rather than guarded against: `stop()` asks
    # this one question, and `monkeypatch` has put the real one back
    # before the fixture asks its own
    assert landed == [True]
    # a closed socket's own fileno is -1; still >= 0 is still open
    assert ours.fileno() == -1


def test_stop_closes_the_listening_socket_even_if_the_accept_task_does_not(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stop` closes the listening socket even where the accept task never ran.

    #312: `server`'s own `with server_socket:` is skipped outright where the
    cancellation reaches that task before its first step, the same
    fact the connection race above turns on -- a coroutine thrown into
    before it has a frame never enters its body. `stop` cancels every
    task it finds before letting the loop run again, so that is the
    ordinary case for a manager stopped before its loop stepped
    anything, and closing every one of `_server_sockets` is what
    answers it.

    `server` is replaced with a coroutine that never wraps its socket in
    a `with` at all: the same thing from the socket's point of view, and
    it does not have to win a race against the loop's first pass to be
    it.
    """
    port = get_random_port()
    manager = a_manager(port=port)

    async def server_without_a_with(
        loop: asyncio.AbstractEventLoop, server_socket: socket.socket
    ) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(manager, "server", server_without_a_with)
    manager.start()
    try:
        wait_until_listening(manager)
        sockets = list(manager._server_sockets)
        assert sockets
    finally:
        manager.stop()
        manager.join(timeout=10)
    assert all(s.fileno() == -1 for s in sockets)


def test_stop_closes_a_connection_that_arrives_while_it_is_draining(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#312: `stop` closes a connection `server`'s accept loop lands mid-drain.

    `run_until_complete` runs the loop, so a task `stop` has not cancelled yet
    goes on working through the drain. `server`'s accept loop is the one that
    matters: it takes what the kernel left in the listen backlog while
    `loop.stop` was in flight and hands it to `create_connection`, which
    registers a connection after the sweep has passed and gives it a task no
    snapshot taken before the drain holds. Nothing closes that socket and
    nothing ends that task, and the collector reports both against whichever
    test it reaches them in -- `Connection.run` pending at its own `sock_recv`,
    beside an unclosed socket.

    `create_connection` is called from the first `run_until_complete`, which is
    the only place `stop` runs the loop at all: deterministic where the real
    interleaving -- which of the loop's tasks the drain happens to reach first
    -- is not.
    """
    port = get_random_port()
    manager = a_running_manager(a_manager, port)
    wait_until_listening(manager)

    ours, theirs = socket.socketpair()
    address = peer_address("127.0.0.1", 18444)
    real_run_until_complete = manager.loop.run_until_complete
    arrived: list[bool] = []

    def draining_run_until_complete(future: Any) -> Any:
        if not arrived:
            arrived.append(True)
            manager.create_connection(ours, address, inbound=True)
        return real_run_until_complete(future)

    monkeypatch.setattr(manager.loop, "run_until_complete", draining_run_until_complete)
    try:
        manager.stop()
    finally:
        theirs.close()
    assert arrived
    # a closed socket's own fileno is -1; still >= 0 is still open
    assert ours.fileno() == -1
    # and nothing is left for the collector to find pending on a loop
    # that will never run again
    assert not asyncio.all_tasks(manager.loop)


def test_stop_closes_a_connection_queued_when_the_drain_begins(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#386: a connection queued as `stop`'s drain begins is still closed.

    `server`'s own task is what `stop`'s blanket sweep over `asyncio.all_tasks`
    reaches directly on every pass, `accept` no longer being a task of its own
    for it to reach instead -- not only through `server`'s task cascading a
    cancel onto it, which is what the neighbouring tests above and below this
    one turn on instead.

    `Task.cancel` on a task whose own awaited future is already done cannot
    cancel that future either: it forces `CancelledError` into the task's next
    step regardless -- but the item this test lands is in `accepted`'s own
    deque, not inside the future `Queue.get` awaits to be woken, so the discard
    costs it nothing, unlike `loop.sock_accept`'s own future before this fix.
    `server`'s own `finally` is what closes whatever the discard still leaves
    behind.

    Landed into the live queue directly, via `P2pManager._accept_queues`,
    scheduled from the same window
    `test_stop_closes_a_connection_accepted_in_its_own_race_window` below
    already uses -- between `stop` scheduling `loop.stop` and waiting for the
    thread. `call_soon_threadsafe` queues behind that scheduling rather than
    ahead of it, so the manager's own loop sees `loop.stop` first and stops
    before ever stepping `server`'s own wakeup: the item is queued and the task
    that owns it is not, which is the same gap a landed kernel accept leaves for
    real.
    """
    port = get_random_port()
    manager = a_manager(port=port)
    manager.start()
    wait_until_listening(manager)
    server_socket = manager._server_sockets[0]
    wait_until(lambda: server_socket in manager._accept_queues)

    ours, theirs = socket.socketpair()
    real_is_alive = manager.is_alive
    landed: list[bool] = []

    def is_alive_after_queueing_one() -> bool:
        landed.append(True)
        manager.loop.call_soon_threadsafe(
            manager._accept_queues[server_socket].put_nowait,
            (ours, ("127.0.0.1", 18444)),
        )
        return real_is_alive()

    monkeypatch.setattr(manager, "is_alive", is_alive_after_queueing_one)
    try:
        manager.stop()
    finally:
        theirs.close()
    # exactly once, asserted rather than guarded against: `stop()` asks
    # this one question, and `monkeypatch` has put the real one back
    # before the fixture asks its own
    assert landed == [True]
    # a closed socket's own fileno is -1; still >= 0 is still open
    assert ours.fileno() == -1


def test_stop_does_not_raise_on_a_manager_whose_thread_was_never_started(
    a_manager: AManagerFactory,
) -> None:
    """#368: `stop` does not raise on a manager whose thread was never started.

    A caller can create real tasks on `manager.loop` directly, without
    ever calling `start()`, and `asyncio.all_tasks(self.loop)` below
    reads non-empty regardless of whether `run_forever` was ever
    entered. `stop()`'s own first line, `call_soon_threadsafe(self.loop.stop)`,
    only schedules `loop.stop`; a loop that has never run has not
    delivered it, so draining those tasks through `run_until_complete`
    used to be this method's first ask of the loop since that scheduling,
    raising `RuntimeError('Event loop stopped before Future completed.')`
    -- the same failure a bind failure already produced through the
    opposite precondition, `pending` empty rather than non-empty (#353).
    `stop_handle.cancel()` is what removes that failure now, on this
    precondition as on every other `run_until_complete` in this method
    can be handed, cancelling the leftover scheduled call outright rather
    than a guard answering which precondition makes one more step safe.

    The loop is stepped once directly before `stop()` is ever called, so
    that the reproduction is not merely "a fresh loop" but one `stop()`
    itself is the first caller to ask anything of since scheduling its
    own `loop.stop` -- the actual precondition `stop_handle.cancel()`
    answers.
    """
    manager = a_manager()
    loop = manager.loop

    async def a_task() -> None:
        await asyncio.Event().wait()

    async def b_task() -> None:
        await asyncio.Event().wait()

    task_a = loop.create_task(a_task())
    task_b = loop.create_task(b_task())
    loop.run_until_complete(asyncio.sleep(0))
    assert not task_a.done()
    assert not task_b.done()

    manager.stop()


def test_stop_drains_a_task_whose_own_cancellation_needs_a_second_step(
    a_manager: AManagerFactory,
) -> None:
    """#377: `stop` drains a task whose own cancellation needs a second step.

    The unconditional drain below (`for task in pending: ...
    run_until_complete(task)`) is not, on its own, guarded against a task
    whose cancellation-unwind needs more than the one batch of
    already-ready callbacks the loop's very first `_run_once` since
    `stop()` scheduled its own `loop.stop` -- an `except CancelledError`
    handler that awaits a fresh, real timer rather than only re-awaiting
    an already-cancelled future. `stop_handle.cancel()` above is what
    answers it instead: cancelling that scheduled `loop.stop` outright,
    rather than guarding how many steps are taken before it, is what
    keeps it from firing mid-unwind regardless of how many steps this
    task's own cancellation needs.

    Neither #312's own regression test nor #368's (above in this file)
    builds a task shaped this way -- both use `asyncio.Event().wait()`,
    whose cancellation resolves inside that same first batch. This one
    does, on a manager whose thread was never started, and used to raise
    the identical `RuntimeError('Event loop stopped before Future
    completed.')` out of this same drain loop.
    """
    manager = a_manager()
    loop = manager.loop

    async def slow_unwind() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise

    task = loop.create_task(slow_unwind())
    loop.run_until_complete(asyncio.sleep(0))
    assert not task.done()

    manager.stop()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_stop_does_not_raise_where_start_was_called_but_run_never_reached_run_forever(
    a_manager: AManagerFactory,
) -> None:
    """#380: `stop` does not raise where `run` never reached `run_forever`.

    `self.ident is not None` -- #368's own guard on a grace step this
    method no longer has -- is true from the moment `start()` is
    called, well before `run()` reaches `run_forever()`. Where `run()`
    raises before that -- a bind failure being the ordinary way -- the
    `loop.stop` `stop()` schedules at its own top is never delivered, and
    `self.ident is not None` read `True` anyway: the grace step that
    guard used to gate ran against a loop with nothing having ever
    stepped it, raising the identical `RuntimeError('Event loop stopped
    before Future completed.')` #368 exists to eliminate, through the
    very guard meant to rule it out. `stop_handle.cancel()` is what
    removes that failure outright now, on this precondition as on every
    other this method can be handed, so nothing downstream of it needs a
    guard of its own to answer this scenario any more.

    A real bind failure, not a monkeypatched `_bind`, the same way
    `test_a_manager_that_cannot_bind_stops_being_alive` above gets one --
    with a real task created directly on `manager.loop` before `start()`,
    the same caller shape #368's own test above builds.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("", 0))
        taken.listen()
        manager = a_manager(port=taken.getsockname()[1])
        loop = manager.loop

        async def a_task() -> None:
            await asyncio.Event().wait()

        task = loop.create_task(a_task())
        loop.run_until_complete(asyncio.sleep(0))
        assert not task.done()

        manager.start()
        wait_until(lambda: not manager.is_alive())
        assert manager.ident is not None

        manager.stop()


def test_server_closes_a_connection_queued_in_the_instant_it_is_cancelled(
    a_manager: AManagerFactory,
) -> None:
    """#386: `server` closes a connection queued as its own task is cancelled.

    A connection can already sit in `server`'s own accept queue when something
    cancels the task waiting on it -- `Queue.get`'s own internal wakeup future
    can be discarded by `Task.cancel` exactly as `loop.sock_accept`'s own future
    used to be (#312), forcing `CancelledError` in on the task's next step
    rather than letting it resume with the result. What that discards is only
    the wakeup: the item itself lives in the queue's own deque and not inside
    that future, so it is still there for `server`'s own `finally` to close once
    the cancellation it raises unwinds through it -- unlike an accepted socket
    held by nothing but a discarded future, which goes out with the frame that
    unwinds and nothing else ever holding it.

    The two `call_soon` callbacks below are that instant, made deterministic:
    they run in the order they were scheduled, so the item has certainly landed
    by the time the cancel reaches the task.
    """
    manager = a_manager()
    loop = manager.loop
    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    accepted, theirs = socket.socketpair()

    task = loop.create_task(manager.server(loop, listening_socket))
    try:
        while listening_socket not in manager._accept_queues:
            loop.run_until_complete(asyncio.sleep(0))
        queue = manager._accept_queues[listening_socket]
        loop.call_soon(queue.put_nowait, (accepted, ("127.0.0.1", 18444)))
        loop.call_soon(task.cancel)
        with suppress(asyncio.CancelledError):
            loop.run_until_complete(task)
    finally:
        theirs.close()
        listening_socket.close()
    assert accepted.fileno() == -1


def test_accept_one_leaves_the_queue_alone_where_nothing_is_pending(
    a_manager: AManagerFactory,
) -> None:
    """`_accept_one`'s `BlockingIOError` arm leaves the queue untouched.

    A reader callback can fire on a listening socket with an empty
    backlog, and this is what lets it return without touching the
    queue at all rather than raising out of a callback nothing awaits.
    """
    manager = a_manager()
    accepted: asyncio.Queue[Any] = asyncio.Queue()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listening_socket:
        listening_socket.bind(("127.0.0.1", 0))
        listening_socket.listen()
        listening_socket.settimeout(0.0)
        manager._accept_one(listening_socket, accepted)
    assert accepted.empty()


def test_accept_one_logs_and_returns_on_a_refused_accept(
    a_manager: AManagerFactory,
) -> None:
    """`_accept_one`'s `OSError` arm logs and returns rather than raising.

    `accept()` can fail outright -- `ECONNABORTED` being the ordinary
    way, a peer resetting the connection between the kernel reporting
    it readable and this callback reaching it -- and this is what keeps
    that from raising out of a reader callback asyncio has no coroutine
    frame to deliver it to, the queue this manager's own `server`
    awaits left untouched.
    """
    manager = a_manager()
    accepted: asyncio.Queue[Any] = asyncio.Queue()

    class RefusingSocket:
        def accept(self) -> NoReturn:
            raise OSError("accept refused")  # noqa: TRY003

    manager._accept_one(cast("socket.socket", RefusingSocket()), accepted)
    assert accepted.empty()
