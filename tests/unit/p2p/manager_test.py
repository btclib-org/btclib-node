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
import errno
import re
import socket
import sys
import threading
import time
import warnings
from concurrent.futures import Future
from contextlib import ExitStack, closing, suppress
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast, override

import pytest
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2
from btclib.p2p.keepalive import Ping

from btclib_node.chains import RegTest
from btclib_node.config import DEFAULT_MAX_PEER_CONNECTIONS
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.log import Logger
from btclib_node.p2p import manager as manager_module
from btclib_node.p2p.address import PeerDB, peer_address
from btclib_node.p2p.main import handle_p2p_handshake
from btclib_node.p2p.manager import P2pManager

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence
    from pathlib import Path

    from btclib.p2p.payload import Payload

    from btclib_node import Node
    from btclib_node.p2p.eviction import EvictionCandidate
from tests import (
    WaitTimeoutError,
    generate_random_transaction,
    get_random_port,
    log_recorder,
    taken_port_bind_error,
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
    nonce: int | None = None,
    inbound: bool = False,
    automatic: bool = False,
) -> Any:
    """Build a `Connection` double: no socket, its own `sent`/`stopped` logs.

    `send_ping` on this double does not send a real ping: it records
    one and backdates `ping_sent` well past the idle bound, standing in
    for a ping already sent and never answered. `nonce` defaults to
    `None`, the same as a real `Connection` that has not sent a
    `version` yet -- `promote_connection` and `remove_connection` both
    read it back to clear `pending_outbound_nonces`.
    """
    conn = SimpleNamespace(
        id=conn_id,
        status=status,
        address=address or peer_address("1.2.3.4", 18444),
        last_receive=time.time() if last_receive is None else last_receive,
        ping_sent=0,
        relay_tx=relay_tx,
        feefilter=feefilter,
        nonce=nonce,
        inbound=inbound,
        automatic=automatic,
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
        port: int | None = None,
        connect: Sequence[tuple[str, int]] = (),
        addnode: Sequence[tuple[str, int]] = (),
        listen: bool = True,
        max_connections: int = DEFAULT_MAX_PEER_CONNECTIONS,
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
        port: int | None = None,
        connect: Sequence[tuple[str, int]] = (),
        addnode: Sequence[tuple[str, int]] = (),
        listen: bool = True,
        max_connections: int = DEFAULT_MAX_PEER_CONNECTIONS,
    ) -> P2pManager:
        # `18444` is regtest's own well-known port -- binding it for
        # real, as a plain default would, collides with a second suite
        # of this same tree on one machine, or a second worker of this
        # same run (btclib-org/btclib-node#678). `get_random_port()`
        # drawn fresh on every call, rather than once as a mutable
        # default would be, is what keeps every manager built without
        # an explicit `port=` off both.
        if port is None:
            port = get_random_port()
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
            # `P2pManager.__init__` reads `node.config.connect_given`,
            # `node.config.listen`, `node.config.connect`,
            # `node.config.addnode` and `node.config.max_connections`
            # directly, not through `Config` itself: a plain namespace
            # is enough. `connect_given` mirrors what `Config.__init__`
            # itself derives from `connect` -- true whenever the
            # sequence is non-empty, `["0"]` included, which nothing
            # here constructs -- and `listen` defaults to `True` so every
            # existing caller here keeps binding and accepting.
            # `max_connections` defaults to `Config`'s own default for
            # the same reason.
            config=SimpleNamespace(
                connect=connect,
                connect_given=bool(connect),
                addnode=addnode,
                listen=listen,
                max_connections=max_connections,
                pruned=False,
            ),
            # `Connection.send_version`'s own `start_height`
            # (btclib-org/btclib-node#722), 0 matching a fresh `Node`'s
            # own initial value (`__init__.py`).
            best_height=0,
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


def test_removing_a_pending_connection_discards_its_own_pending_nonce(
    a_manager: AManagerFactory,
) -> None:
    """`remove_connection` clears this connection's nonce out of the set.

    #448: `callbacks.version`'s self-connection check walks
    `pending_outbound_nonces` for exactly as long as the connection that
    drew a nonce is still live and unhandshaken -- a connection dropped
    for any other reason has to leave it too, or a later, unrelated
    connection drawing the same nonce (astronomically unlikely, but not
    what this is testing) would find a stale entry answering for it.
    """
    conn = a_conn(1, status=P2pConnStatus.Open, nonce=7)
    manager = a_manager()
    manager.pending_connections[conn.id] = conn
    manager.pending_outbound_nonces.add(7)
    manager.remove_connection(1)
    assert not manager.pending_outbound_nonces


def test_removing_a_connection_with_no_nonce_yet_does_not_raise(
    a_manager: AManagerFactory,
) -> None:
    """A connection dropped before `send_version` ran carries no nonce."""
    conn = a_conn(1, status=P2pConnStatus.Open, nonce=None)
    manager = a_manager()
    manager.pending_connections[conn.id] = conn
    manager.remove_connection(1)
    assert not manager.pending_outbound_nonces


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


def test_discourage_marks_the_host_whatever_the_port(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1078: `discourage` keys the host, not the endpoint, as Core does.

    An inbound peer connects from a port of its own choosing each time,
    so only a key without the port finds it again; the same IPv4 host
    mapped into IPv6 is the same host, and a different one is not.
    """
    manager = a_manager()
    manager.discourage(peer_address("1.2.3.4", 18444))
    assert manager.is_discouraged(peer_address("1.2.3.4", 50000))
    assert manager.is_discouraged(peer_address("::ffff:1.2.3.4", 50001))
    assert not manager.is_discouraged(peer_address("1.2.3.5", 18444))


@pytest.mark.parametrize("host", ["127.0.0.1", "0.1.2.3", "::1", "::ffff:127.0.0.1"])
def test_a_local_host_is_never_discouraged(
    a_manager: AManagerFactory, host: str
) -> None:
    """ISS 1078: Core's `MaybeDiscourageAndDisconnect` spares a local peer.

    Keyed without the port, one local peer discouraged would be every
    local peer discouraged, so only the connection that misbehaved is
    dropped.
    """
    misbehaving = a_conn(0, address=peer_address(host, 18444), inbound=True)
    other = a_conn(1, address=peer_address(host, 50000), inbound=True)
    manager = a_manager([misbehaving, other])
    assert manager.maybe_discourage_and_disconnect(misbehaving) is False
    assert not manager.is_discouraged(peer_address(host, 18444))
    assert misbehaving.stopped == [True]
    assert not other.stopped


def test_discouraging_a_host_drops_every_connection_held_with_it(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1094: Core's `DisconnectNode(CSubNet(addr))` drops the whole host.

    Whatever the port, the kind of connection and whether its handshake
    is done, and no other host's.
    """
    misbehaving = a_conn(0, address=peer_address("1.2.3.4", 50000), inbound=True)
    same_host_inbound = a_conn(1, address=peer_address("1.2.3.4", 50001), inbound=True)
    same_host_manual = a_conn(2, address=peer_address("::ffff:1.2.3.4", 18444))
    same_host_pending = a_conn(
        3,
        address=peer_address("1.2.3.4", 18445),
        status=P2pConnStatus.Open,
        automatic=True,
    )
    other_host = a_conn(4, address=peer_address("1.2.3.5", 50000), inbound=True)
    manager = a_manager([misbehaving, same_host_inbound, same_host_manual, other_host])
    manager.pending_connections[3] = same_host_pending
    assert manager.maybe_discourage_and_disconnect(misbehaving) is True
    assert manager.is_discouraged(peer_address("1.2.3.4", 18444))
    for conn in (misbehaving, same_host_inbound, same_host_manual, same_host_pending):
        assert conn.stopped == [True]
    assert not other_host.stopped


def test_an_automatic_peer_misbehaving_is_discouraged(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1090: a peer this node drew and dialled itself is punished."""
    misbehaving = a_conn(0, automatic=True)
    manager = a_manager([misbehaving])
    assert manager.maybe_discourage_and_disconnect(misbehaving) is True
    assert manager.is_discouraged(misbehaving.address)
    assert misbehaving.stopped == [True]


def test_a_manual_peer_is_never_discouraged(a_manager: AManagerFactory) -> None:
    """ISS 1139: Core's `MaybeDiscourageAndDisconnect` spares a manual peer.

    "We never disconnect or discourage manual peers for bad behavior":
    neither the connection nor anything else held with the host is
    dropped.
    """
    misbehaving = a_conn(0, inbound=False, automatic=False)
    other = a_conn(1, address=peer_address("1.2.3.4", 50000), inbound=True)
    manager = a_manager([misbehaving, other])
    assert manager.maybe_discourage_and_disconnect(misbehaving) is False
    assert not manager.is_discouraged(misbehaving.address)
    assert not misbehaving.stopped
    assert not other.stopped


def test_the_host_discouraged_longest_ago_is_forgotten_first(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1078: past its capacity the record forgets its oldest host.

    Discouraging a host again makes it the newest, so the one forgotten
    is the one discouraged longest ago rather than the one first seen.
    """
    monkeypatch.setattr(manager_module, "_DISCOURAGED_CAPACITY", 2)
    manager = a_manager()
    first, second, third = (peer_address(f"1.2.3.{i}", 18444) for i in (1, 2, 3))
    manager.discourage(first)
    manager.discourage(second)
    manager.discourage(first)
    manager.discourage(third)
    assert manager.is_discouraged(first)
    assert not manager.is_discouraged(second)
    assert manager.is_discouraged(third)


def test_add_pending_outbound_nonce_makes_it_visible_to_is_self_connect_nonce(
    a_manager: AManagerFactory,
) -> None:
    """A nonce recorded by one is found by the other, on the same manager."""
    manager = a_manager()
    manager.add_pending_outbound_nonce(7)
    assert manager.pending_outbound_nonces == {7}
    assert manager.is_self_connect_nonce(7)


def test_is_self_connect_nonce_is_false_for_one_never_added(
    a_manager: AManagerFactory,
) -> None:
    """A nonce nobody drew answers `False`, not a `KeyError`."""
    manager = a_manager()
    assert not manager.is_self_connect_nonce(7)


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


def test_promoting_a_connection_discards_its_own_pending_nonce(
    a_manager: AManagerFactory,
) -> None:
    """A connection past its own `verack` no longer needs checking against.

    #448: `pending_outbound_nonces` holds a nonce only for as long as the
    connection that drew it is still short of `verack` -- successfully
    connected is exactly the state it stops standing for.
    """
    conn = a_conn(1, status=P2pConnStatus.Open, nonce=7)
    manager = a_manager()
    manager.pending_connections[conn.id] = conn
    manager.pending_outbound_nonces.add(7)
    manager.promote_connection(1)
    assert not manager.pending_outbound_nonces


def test_a_peer_that_cannot_be_dialled_is_not_kept(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `dial` that comes back with nothing leaves no connection behind.

    Asserted on the message and not only on the branch running: the
    whole point of this line is that a one-shot `connect` used to lose
    its dial with nothing in `debug.log` naming it (issue #1020), so a
    test that exercises it without reading it would keep passing if it
    ever went silent again. `caplog` cannot see this logger -- `#587`,
    and `Node.logger` never reaching `logging.getLogger` -- so this
    reads it the way `create_connection`'s own log line is read above.
    """
    logged, info = log_recorder()

    async def never_connects(address: NetworkAddressV2) -> None:
        return None

    monkeypatch.setattr(manager_module, "dial", never_connects)
    manager = a_manager()
    monkeypatch.setattr(manager.logger, "info", info)
    asyncio.run(manager.async_connect(peer_address("1.2.3.4", 18444)))
    assert not manager.connections
    assert not manager.pending_connections
    assert logged == ["Dial to 1.2.3.4:18444 did not come up"]


def test_a_connection_that_has_closed_is_let_go_of(a_manager: AManagerFactory) -> None:
    """One pass of the housekeeping loop drops a connection already `Closed`."""
    conn = a_conn(1, status=P2pConnStatus.Closed)
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert not manager.connections


def test_a_closed_connection_past_the_idle_bound_is_not_pinged(
    a_manager: AManagerFactory,
) -> None:
    """#435: removal does not fall through into the idle check below it.

    A connection `Closed` and idle at once used to be removed by the
    first check and then, still the loop variable, found idle by the
    second -- with `last_receive` frozen and `ping_sent` never set, so
    `send_ping` ran on a connection already out of both tables.
    """
    conn = a_conn(1, status=P2pConnStatus.Closed, last_receive=time.time() - 200)
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert not manager.connections
    assert conn.sent == []


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
        automatic = False

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
    """Build a `get_active_addresses` stub recording every call it answers."""
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
    """A pending connection heard from recently is neither pinged nor cut."""
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

    One already pending fills the one-peer target `max_connections=1`
    leaves: reaching for a second would raise into the housekeeping
    loop's own handler, so a quiet log is the assertion that it did not.
    """
    conn = a_conn(1, status=P2pConnStatus.Open, automatic=True)
    peer_db = a_peer_db_stub(is_empty=False, random_address=refuses_to_be_asked)
    manager = a_manager(peer_db=peer_db, max_connections=1)
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
    manager.discourage(onion)
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

    values_calls: list[None] = []

    class HookedPending(dict[int, Any]):
        @override
        def values(self) -> Any:
            # called twice, first from the count and then from the
            # snapshot below: the second call is the one to race
            result = dict.values(self)
            values_calls.append(None)
            if len(values_calls) < 2:
                return result
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
    unlocked reads could undercount a node that already has enough
    peers, one reading `connections` before the write and the other reading
    `pending_connections` after the pop, and this pass would then dial past the
    target it was told to stop at.
    """
    onion = NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)
    conn = a_conn(1, status=P2pConnStatus.Open, automatic=True)
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: onion)
    manager = a_manager(peer_db=peer_db, max_connections=1)
    manager.pending_connections[1] = conn

    promote_done = threading.Event()

    def promote_and_signal() -> None:
        manager.promote_connection(1)
        promote_done.set()

    promote_thread = threading.Thread(target=promote_and_signal)

    class HookedPending(dict[int, Any]):
        @override
        def values(self) -> Any:
            # called exactly once, from the count below -- the
            # snapshot's own call further down is never reached once
            # the count answers on its own
            promote_thread.start()
            # Not a race, on either side: `promote_connection` takes
            # the same lock this count is read under, so on the fixed
            # tree it cannot finish inside this wait whatever the
            # timeout is -- a `threading.Lock` already held by this
            # method admits no second entrant, ever, not merely a slow
            # one. On the unfixed tree nothing here contends that lock
            # at all, and a pop and a dict write finish well inside it.
            promote_done.wait(timeout=1)
            return dict.values(self)

    manager.pending_connections = HookedPending(manager.pending_connections)

    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(manager._maybe_dial_more_peers())
    promote_thread.join(timeout=5)

    assert not promote_thread.is_alive()
    assert not logged
    assert list(manager.connections) == [1]
    assert not manager.pending_connections


def test_a_dial_that_comes_back_with_nothing_adds_no_connection(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dial from the housekeeping loop that fails adds no connection."""

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


_ONION = NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)


@pytest.mark.parametrize(
    ("held", "drawn", "dials"),
    [
        pytest.param(
            a_conn(1, automatic=True, address=peer_address("1.2.3.4", 8333)),
            peer_address("1.2.200.200", 18444),
            False,
            id="automatic-same-16",
        ),
        pytest.param(
            a_conn(1, automatic=True, address=peer_address("1.2.3.4", 8333)),
            peer_address("1.3.3.4", 8333),
            True,
            id="automatic-other-16",
        ),
        pytest.param(
            a_conn(1, address=peer_address("1.2.3.4", 8333)),
            peer_address("1.2.200.200", 18444),
            False,
            id="addnode-same-16",
        ),
        pytest.param(
            a_conn(1, status=P2pConnStatus.Open, automatic=True),
            peer_address("1.2.200.200", 18444),
            False,
            id="pending-same-16",
        ),
        pytest.param(
            a_conn(1, inbound=True, address=peer_address("1.2.3.4", 8333)),
            peer_address("1.2.200.200", 18444),
            True,
            id="inbound-same-16",
        ),
        pytest.param(
            a_conn(1, address=peer_address("2001:db9:1::1", 8333)),
            peer_address("2001:db9:2::2", 8333),
            False,
            id="ipv6-same-32",
        ),
        pytest.param(
            a_conn(1, address=peer_address("::ffff:1.2.3.4", 8333)),
            peer_address("1.2.200.200", 18444),
            False,
            id="mapped-ipv4-same-16",
        ),
        pytest.param(
            a_conn(1, automatic=True, address=_ONION),
            peer_address("1.2.200.200", 18444),
            True,
            id="onion-held",
        ),
    ],
)
def test_an_outbound_peer_s_network_group_is_not_dialled_again(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    held: Any,
    drawn: NetworkAddressV2,
    *,
    dials: bool,
) -> None:
    """ISS 1098: one outbound peer per network group, as Core keeps them.

    `CConnman::ThreadOpenConnections` skips a drawn address whose
    `GetGroup` a manual, full-relay or block-relay-only peer already
    holds, a pending one included, and counts no inbound peer and no
    Tor, I2P or CJDNS one. Whether the dial is reached is the assertion.
    """
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)

    monkeypatch.setattr(manager_module, "dial", records)
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn)
    manager = a_manager(peer_db=peer_db)
    if held.status == P2pConnStatus.Open:
        manager.pending_connections[held.id] = held
    else:
        manager.connections[held.id] = held
    asyncio.run(manager._maybe_dial_more_peers())
    assert dialled == ([drawn] if dials else [])


def refuses_to_be_asked() -> NoReturn:
    """Stand in for `random_address`/`get_active_addresses`, unreachable."""
    raise RuntimeError("no")


async def asks_no_dns_server() -> None:
    """Stand in for a `get_addr_from_dns` that never touches a real server."""
    return


def test_connect_turns_off_addrman_outgoing(a_manager: AManagerFactory) -> None:
    """`node.config.connect` non-empty: `use_addrman_outgoing` is `False`."""
    manager = a_manager(connect=[("1.2.3.4", 8333)])
    assert manager.use_addrman_outgoing is False


def test_no_connect_leaves_addrman_outgoing_on(a_manager: AManagerFactory) -> None:
    """`node.config.connect` empty, the ordinary case: the draw stays on."""
    manager = a_manager()
    assert manager.use_addrman_outgoing is True


def test_maybe_dial_more_peers_is_a_noop_under_connect(
    a_manager: AManagerFactory,
) -> None:
    """Under `-connect`, the peer_db draw is never reached at all.

    `refuses_to_be_asked` would raise into the housekeeping loop's own
    handler the moment `random_address` is called; nothing here catches
    that, so a clean return is the proof `_maybe_dial_more_peers`
    returned before reaching for it.
    """
    peer_db = a_peer_db_stub(is_empty=False, random_address=refuses_to_be_asked)
    manager = a_manager(peer_db=peer_db, connect=[("1.2.3.4", 8333)])
    asyncio.run(manager._maybe_dial_more_peers())
    assert not manager.connections
    assert not manager.pending_connections


async def _record_dns_lookup(calls: list[int]) -> None:
    """Stand in for `get_addr_from_dns`, recording that it was awaited.

    One shared function rather than a `spy` nested in each of the two
    tests below: the "never called" half of that pair would otherwise
    pin a line the suite can never reach and the 100% floor can never
    forgive. Sharing this one lets the positive control below cover it
    while the skip test's own `calls` stays empty.
    """
    calls.append(1)


def _let_runs_own_coroutines_start(manager: P2pManager) -> None:
    """Return once every coroutine `run` scheduled has taken its first step.

    `run` schedules them before `run_forever`, so one scheduled from here
    once the loop is running is queued behind them, and its own result
    arriving means theirs have started: a `get_addr_from_dns` stand-in
    `run` scheduled has recorded its call by then. Without this, `calls`
    read the moment the loop runs can be empty where the lookup was
    scheduled and has not started yet.
    """
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), manager.loop).result(timeout=10)


def test_run_skips_the_dns_lookup_under_connect(a_manager: AManagerFactory) -> None:
    """`-connect` also stops `run` from ever scheduling `get_addr_from_dns`.

    `listen=False` alongside `connect`, matching what `-connect` alone
    resolves to without an explicit `-listen=1` (`cli.py`'s own
    `build_config`) -- so this is also where "no listener under
    `-connect`" is pinned, in place of the `wait_until_listening` an
    earlier version of this test waited on, which held regardless of
    `-connect` and so proved nothing about it.
    """
    calls: list[int] = []
    peer_db = a_peer_db_stub(
        is_empty=True,
        random_address=refuses_to_be_asked,
        get_addr_from_dns=partial(_record_dns_lookup, calls),
    )
    manager = a_manager(
        peer_db=peer_db,
        port=get_random_port(),
        connect=[("1.2.3.4", 8333)],
        listen=False,
    )
    manager.start()
    # `loop.is_running()` rather than `wait_until_listening`: nothing
    # binds under `listen=False`, so `listening` never sets and a wait
    # on it would only time out.
    wait_until(manager.loop.is_running)
    _let_runs_own_coroutines_start(manager)
    assert not manager.listening.is_set()
    assert not calls


def test_run_schedules_the_dns_lookup_without_connect(
    a_manager: AManagerFactory,
) -> None:
    """The positive control for the test above: without `-connect`, it runs.

    Proves the stand-in can actually observe a call at all, rather than
    the skip above passing because nothing here would ever detect one.
    """
    calls: list[int] = []
    peer_db = a_peer_db_stub(
        is_empty=True,
        random_address=refuses_to_be_asked,
        get_addr_from_dns=partial(_record_dns_lookup, calls),
    )
    manager = a_manager(peer_db=peer_db, port=get_random_port())
    manager.start()
    wait_until_listening(manager)
    wait_until(lambda: calls)


def test_zero_max_connections_turns_off_the_dns_lookup(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1066: `max_connections=0` seeds nothing, as `-connect` does not."""
    assert a_manager(max_connections=0).use_dns_seed is False
    assert a_manager().use_dns_seed is True
    assert a_manager(connect=[("1.2.3.4", 8333)]).use_dns_seed is False


def test_run_skips_the_dns_lookup_at_zero_max_connections(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1066: `run` never schedules `get_addr_from_dns` at zero.

    `listen=False` beside it, what `-maxconnections=0` alone resolves to
    (`cli.py`'s own `build_config`);
    `test_run_schedules_the_dns_lookup_without_connect` above is the
    positive control that the stand-in sees a call.
    """
    calls: list[int] = []
    peer_db = a_peer_db_stub(
        is_empty=True,
        random_address=refuses_to_be_asked,
        get_addr_from_dns=partial(_record_dns_lookup, calls),
    )
    manager = a_manager(
        peer_db=peer_db, port=get_random_port(), listen=False, max_connections=0
    )
    manager.start()
    wait_until(manager.loop.is_running)
    _let_runs_own_coroutines_start(manager)
    assert not manager.listening.is_set()
    assert not calls


def test_listen_false_binds_nothing_but_still_dials(a_manager: AManagerFactory) -> None:
    """`listen=False`: no bound socket, and the explicit dial still works.

    `-connect` alone resolves to exactly this combination
    (`cli.py`'s own `build_config`): `Node.run`'s dial loop calls
    `P2pManager.connect` for every `config.connect`/`config.addnode`
    peer regardless of `listen`, so a manager with `listen=False` has to
    still be able to reach one -- dialled here at a second, ordinary
    manager that is listening, since one with `listen=False` has
    nothing of its own to dial back into.
    """
    target_port = get_random_port()
    target = a_running_manager(a_manager, target_port)
    dialer = a_manager(connect=[("127.0.0.1", target_port)], listen=False)
    try:
        wait_until_listening(target)
        # `-listen=0` is not a failure to listen: nothing to wait for
        assert dialer.start_listener()
        wait_until(dialer.loop.is_running)
        assert not dialer.listening.is_set()
        dialer.connect(peer_address("127.0.0.1", target_port))
        wait_until(lambda: dialer.pending_connections)
        wait_until(lambda: target.pending_connections)
    finally:
        dialer.stop()
        dialer.join(timeout=10)
        target.stop()
        target.join(timeout=10)


def test_connect_and_explicit_listen_binds_and_dials(
    a_manager: AManagerFactory,
) -> None:
    """`-connect` plus `-listen=1`: both a bound socket and a working dial.

    The explicit override case: `connect` set and `listen` left at its
    own default `True` rather than the `False` `-connect` alone would
    resolve to.
    """
    target_port = get_random_port()
    target = a_running_manager(a_manager, target_port)
    dialer = a_manager(connect=[("127.0.0.1", target_port)])
    try:
        wait_until_listening(target)
        # returns once bound, with nothing left to wait for
        assert dialer.start_listener()
        assert dialer.listening.is_set()
        dialer.connect(peer_address("127.0.0.1", target_port))
        wait_until(lambda: dialer.pending_connections)
        wait_until(lambda: target.pending_connections)
    finally:
        dialer.stop()
        dialer.join(timeout=10)
        target.stop()
        target.join(timeout=10)


def test_maybe_redial_specified_is_a_noop_with_nothing_specified(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `-connect`/`-addnode` given: nothing is ever redialled."""
    manager = a_manager()
    assert not manager._redial_peers
    monkeypatch.setattr(manager, "async_connect", refuses_to_be_asked)
    asyncio.run(manager._maybe_redial_specified())


def test_maybe_redial_specified_skips_a_peer_not_yet_due(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A specified peer whose own backoff has not elapsed is left alone."""
    manager = a_manager(connect=[("1.2.3.4", 8333)])
    (key,) = manager._redial_peers
    manager._redial_next[key] = time.time() + 100
    monkeypatch.setattr(manager, "async_connect", refuses_to_be_asked)
    asyncio.run(manager._maybe_redial_specified())


def test_maybe_redial_specified_dials_a_due_peer_and_doubles_the_backoff(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A due, unconnected peer is dialled, and its own backoff doubles.

    `_redial_next` defaults to `0.0` -- always due -- for a manager
    built by `a_manager` directly rather than through `run`, which is
    the only place that seeds it forward (`run`'s own comment on the
    race that seeding avoids).
    """
    dialled: list[Any] = []

    async def record_dial(address: Any) -> None:
        dialled.append(address)

    manager = a_manager(connect=[("1.2.3.4", 8333)])
    monkeypatch.setattr(manager, "async_connect", record_dial)
    (key,) = manager._redial_peers
    assert manager._redial_backoff[key] == manager_module._REDIAL_BASE_SECONDS
    asyncio.run(manager._maybe_redial_specified())
    assert len(dialled) == 1
    assert manager._redial_backoff[key] == manager_module._REDIAL_BASE_SECONDS * 2
    # due again, forcing a second attempt without a real wait
    manager._redial_next[key] = 0.0
    asyncio.run(manager._maybe_redial_specified())
    assert len(dialled) == 2
    assert manager._redial_backoff[key] == manager_module._REDIAL_BASE_SECONDS * 4


def test_maybe_redial_specified_caps_the_backoff(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backoff never grows past `_REDIAL_MAX_SECONDS`."""

    async def do_nothing(address: Any) -> None:
        del address

    manager = a_manager(connect=[("1.2.3.4", 8333)])
    monkeypatch.setattr(manager, "async_connect", do_nothing)
    (key,) = manager._redial_peers
    manager._redial_backoff[key] = manager_module._REDIAL_MAX_SECONDS
    asyncio.run(manager._maybe_redial_specified())
    assert manager._redial_backoff[key] == manager_module._REDIAL_MAX_SECONDS


def test_maybe_redial_specified_resets_the_backoff_once_connected(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A specified peer already connected is left alone, backoff reset."""
    address = peer_address("1.2.3.4", 8333)
    conn = a_conn(1, address=address)
    manager = a_manager([conn], connect=[("1.2.3.4", 8333)])
    (key,) = manager._redial_peers
    manager._redial_backoff[key] = manager_module._REDIAL_MAX_SECONDS
    monkeypatch.setattr(manager, "async_connect", refuses_to_be_asked)
    asyncio.run(manager._maybe_redial_specified())
    assert manager._redial_backoff[key] == manager_module._REDIAL_BASE_SECONDS


def test_maybe_redial_specified_logs_and_continues_on_a_dial_that_raises(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dial that raises is logged, like every other housekeeping step."""
    logged: list[str] = []
    manager = a_manager(connect=[("1.2.3.4", 8333)])
    monkeypatch.setattr(manager, "async_connect", refuses_to_be_asked)
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    asyncio.run(manager._maybe_redial_specified())
    assert logged


def test_redial_connects_a_specified_peer_without_an_explicit_dial(
    a_manager: AManagerFactory,
) -> None:
    """The standing loop alone reconnects a `-connect` peer -- issue #651.

    Nothing here ever calls `dialer.connect(...)`: `manage_connections`'
    own `_maybe_redial_specified` is what has to reach `target` for this
    to pass, the same route a `-connect` peer that dropped and needs
    picking back up would go through.
    """
    target_port = get_random_port()
    target = a_running_manager(a_manager, target_port)
    dialer = a_manager(connect=[("127.0.0.1", target_port)])
    try:
        wait_until_listening(target)
        dialer.start()
        wait_until(lambda: dialer.pending_connections)
        wait_until(lambda: target.pending_connections)
    finally:
        dialer.stop()
        dialer.join(timeout=10)
        target.stop()
        target.join(timeout=10)


def test_a_peer_db_that_raises_does_not_stop_the_housekeeping(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `random_address` that raises logs and lets housekeeping go on."""
    logged: list[str] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=refuses_to_be_asked)
    manager = a_manager(peer_db=peer_db)
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    # still running when the pass ended: catching the exception and
    # returning would leave the node with no housekeeping at all
    assert asyncio.run(one_pass(manager)) is True
    assert logged


@pytest.mark.parametrize("status", list(NodeStatus))
@pytest.mark.parametrize(("automatic", "dials"), [(7, True), (8, False)])
def test_eight_automatic_peers_are_the_target_however_far_the_sync_is(
    a_manager: AManagerFactory, status: NodeStatus, automatic: int, *, dials: bool
) -> None:
    """ISS 1073: `m_max_outbound_full_relay`, eight, from the first pass on.

    Seven dialled automatically leave room for an eighth whatever
    `node.status` says, headers unsynced included, and eight fill it:
    the draw being asked for, or not, is the assertion.
    """
    drawn: list[None] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn.append(None))
    conns = [a_conn(i, automatic=True) for i in range(automatic)]
    manager = a_manager(conns, peer_db=peer_db, status=status)
    asyncio.run(manager._maybe_dial_more_peers())
    assert bool(drawn) is dials


def test_no_automatic_outbound_slot_means_no_dial(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`max_connections=0`: no dial, however much room the target leaves.

    The peer db refuses to be asked, as in the test above, so a quiet
    log is the assertion; the same manager at the default limit is the
    control that the refusal is reached at all.
    """
    for max_connections, dials in ((0, False), (DEFAULT_MAX_PEER_CONNECTIONS, True)):
        peer_db = a_peer_db_stub(is_empty=False, random_address=refuses_to_be_asked)
        manager = a_manager(peer_db=peer_db, max_connections=max_connections)
        logged: list[str] = []
        monkeypatch.setattr(manager.logger, "exception", logged.append)
        asyncio.run(one_pass(manager))
        assert bool(logged) is dials


@pytest.mark.parametrize(
    "conns",
    [
        pytest.param(
            [a_conn(i, inbound=True) for i in range(8)],
            id="eight-inbound",
        ),
        pytest.param(
            [a_conn(i) for i in range(8)],
            id="eight-addnode",
        ),
        pytest.param(
            [a_conn(1, status=P2pConnStatus.Open, inbound=True)],
            id="one-inbound-pending",
        ),
    ],
)
def test_a_connection_not_dialled_automatically_leaves_the_target_open(
    a_manager: AManagerFactory, conns: Sequence[Any]
) -> None:
    """ISS 1065: inbound and `-connect`/`-addnode` peers do not fill the target.

    Eight of either are the target: counted, they would stop the draw,
    so the draw being asked for is the assertion. An `a_conn` that is
    neither inbound nor `automatic` is what `async_connect`, the
    `-connect`/`-addnode` route, builds.
    """
    drawn: list[None] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn.append(None))
    manager = a_manager(peer_db=peer_db)
    for conn in conns:
        manager.pending_connections[conn.id] = conn
    asyncio.run(manager._maybe_dial_more_peers())
    assert drawn


def test_eight_automatic_peers_fill_the_target(a_manager: AManagerFactory) -> None:
    """The control for the test above: eight dialled automatically fill it."""
    drawn: list[None] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn.append(None))
    conns = [a_conn(i, automatic=True) for i in range(8)]
    manager = a_manager(conns, peer_db=peer_db)
    asyncio.run(manager._maybe_dial_more_peers())
    assert not drawn


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
    """A successful dial lands a pending outbound connection with its socket."""
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
        assert conn.automatic
        assert conn.task is not None
        conn.task.cancel()
        await asyncio.sleep(0)

    with ours, theirs:
        # on the manager's own loop, which is the one it schedules the
        # connection's loop on. asyncio.run would build a second one and
        # leave the manager holding a loop the fixture then never closes
        manager.loop.run_until_complete(dial())


@pytest.mark.parametrize(("inbound", "verb"), [(True, "Accepted"), (False, "Dialled")])
def test_create_connection_logs_the_id_beside_the_address(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    inbound: bool,
    verb: str,
) -> None:
    """#611: the id a connection is given is logged beside its address.

    The earliest point every path into a connection shares, dialled or
    accepted, before any wire message is parsed -- proved directly here
    rather than only through `callbacks.verack`'s own line, which a
    handshake failure can leave unreached.
    """
    logged, info = log_recorder()
    manager = a_manager()
    monkeypatch.setattr(manager.logger, "info", info)
    ours, theirs = socket.socketpair()
    address = peer_address("1.2.3.4", 18444)

    async def create() -> None:
        manager.create_connection(ours, address, inbound=inbound)
        (conn,) = manager.pending_connections.values()
        assert conn.task is not None
        conn.task.cancel()
        await asyncio.sleep(0)

    with ours, theirs:
        manager.loop.run_until_complete(create())

    assert logged == [f"{verb} 1.2.3.4:18444, connection 0"]


def test_a_connections_id_still_resolves_to_its_address_when_the_handshake_fails_before_verack(
    a_manager: AManagerFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#611: a malformed `version` raises strictly before `verack` ever runs.

    `callbacks.verack`'s own id-address pairing (#526) never reaches a
    connection dropped this way. Reading `create_connection`'s own line
    back beside `handle_p2p_handshake`'s own exception line (also #526)
    is what still resolves the id either of them names to the address
    the first one names -- proved end to end, against the real
    `version` callback raising on the malformed bytes the issue itself
    reproduces with, rather than against a stand-in that raises on cue.
    """
    log_path = tmp_path / "debug.log"
    logger = Logger(log_path, debug=True)
    manager = a_manager()
    monkeypatch.setattr(manager, "logger", logger)
    monkeypatch.setattr(manager.node, "logger", logger)
    monkeypatch.setattr(manager.node, "p2p_manager", manager, raising=False)
    ours, theirs = socket.socketpair()
    address = peer_address("1.2.3.4", 18444)

    async def create() -> Any:
        manager.create_connection(ours, address, inbound=True)
        (conn,) = manager.pending_connections.values()
        assert conn.task is not None
        conn.task.cancel()
        await asyncio.sleep(0)
        return conn

    with ours, theirs:
        conn = manager.loop.run_until_complete(create())
        manager.handshake_messages.append(("version", b"garbage", conn.id, 7))
        handle_p2p_handshake(manager.node)
    logger.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    (created,) = [line for line in lines if "Accepted 1.2.3.4:18444" in line]
    (failed,) = [line for line in lines if "Handling version from connection" in line]
    assert f"connection {conn.id}" in created
    assert f"connection {conn.id} failed" in failed
    # the fact #526's own comment argues: the failing line never repeats
    # the address, so `created` above is what makes `conn.id` resolvable
    # at all
    assert "1.2.3.4" not in failed


def a_running_manager(a_manager: AManagerFactory, port: int) -> P2pManager:
    """Build and start a `P2pManager` without waiting for it to listen."""
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
    """A manager also binds IPv6, accepting a peer that dials it over `::1`."""
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


@pytest.mark.parametrize(
    ("max_connections", "max_inbound", "max_outbound_full_relay"),
    [
        # Core's own default: eleven outbound slots reserved, the rest
        # inbound, eight of the eleven full-relay
        (DEFAULT_MAX_PEER_CONNECTIONS, 114, 8),
        # one past the reservation: a single inbound slot
        (12, 1, 8),
        # under the reservation: no inbound slot, and the full-relay
        # target is the total itself
        (5, 0, 5),
        (0, 0, 0),
    ],
)
def test_max_connections_is_divided_the_way_core_divides_it(
    a_manager: AManagerFactory,
    max_connections: int,
    max_inbound: int,
    max_outbound_full_relay: int,
) -> None:
    """`CConnman::Init`'s `m_max_inbound` and `m_max_outbound_full_relay`."""
    manager = a_manager(max_connections=max_connections)
    assert manager.max_inbound == max_inbound
    assert manager.max_outbound_full_relay == max_outbound_full_relay


def test_an_inbound_peer_past_the_limit_is_refused_until_a_slot_frees(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1054: the (N+1)th inbound peer is closed; a closed one frees a slot.

    `max_connections=12` leaves one inbound slot, eleven being reserved
    for outbound. Raw sockets rather than peers that speak the protocol:
    the first stays pending, holding its slot the same as a peer past
    `verack` would. That one peer is protected from eviction (ISS 1064),
    its netgroup being among the four kept, so the second is refused,
    and closed before `create_connection` runs for it, which
    `last_connection_id` not moving says.
    """
    port = get_random_port()
    manager = a_manager(port=port, max_connections=12)
    logged: list[str] = []
    monkeypatch.setattr(
        manager.logger, "debug", lambda msg, *args: logged.append(msg % args)
    )
    manager.start()
    wait_until_listening(manager)
    with closing(socket.create_connection(("127.0.0.1", port), timeout=20)):
        wait_until(lambda: manager.pending_connections)
        assert manager.last_connection_id == 0
        with closing(
            socket.create_connection(("127.0.0.1", port), timeout=20)
        ) as second:
            # an orderly close, with nothing sent first: this node's own
            # `version` is what an accepted peer reads, and a peer that
            # sent nothing and was read nothing is closed with a FIN
            second.settimeout(20)
            assert second.recv(4096) == b""
        assert manager.last_connection_id == 0
        assert len(manager.pending_connections) == 1
        assert (
            "failed to find an eviction candidate - connection dropped (full)" in logged
        )
    # the first peer closed by the `with`: its `Connection` reads the close,
    # `manage_connections` lets go of it, and the slot is free again
    wait_until(lambda: not manager.pending_connections)
    with closing(socket.create_connection(("127.0.0.1", port), timeout=20)) as third:
        wait_until(lambda: manager.pending_connections)
        (conn,) = manager.pending_connections.values()
        assert conn.id == 1
        assert conn.address.port == third.getsockname()[1]
        manager.stop()
        manager.join(timeout=10)


def test_a_full_manager_evicts_an_inbound_peer_to_accept_a_new_one(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1064: past the inbound share, a held peer makes way for a new one.

    `max_connections=32` leaves twenty-one inbound slots. Twenty-one raw
    sockets from one address answer no ping and relay nothing, so Core's
    fixed protections keep twenty of them -- four by netgroup, eight by
    ping, four by transaction, four by block -- and the ratio keeps none
    of one: one is left to evict, and the twenty-second peer takes its
    slot.
    """
    port = get_random_port()
    manager = a_manager(port=port, max_connections=32)
    assert manager.max_inbound == 21
    logged, record = log_recorder()
    monkeypatch.setattr(manager.logger, "debug", record)
    manager.start()
    wait_until_listening(manager)
    with ExitStack() as peers:
        for _ in range(manager.max_inbound):
            peers.enter_context(
                closing(socket.create_connection(("127.0.0.1", port), timeout=20))
            )
        wait_until(lambda: len(manager.pending_connections) == manager.max_inbound)
        held = set(manager.pending_connections)
        peers.enter_context(
            closing(socket.create_connection(("127.0.0.1", port), timeout=20))
        )
        wait_until(lambda: manager.last_connection_id == manager.max_inbound)
        wait_until(lambda: len(manager.pending_connections) == manager.max_inbound)
        (evicted,) = held - set(manager.pending_connections)
        assert manager.max_inbound in manager.pending_connections
        assert any(
            line.startswith(
                f"selected inbound connection for eviction, disconnecting peer={evicted}"
                " peeraddr=127.0.0.1:"
            )
            for line in logged
        )
        manager.stop()
        manager.join(timeout=10)


def land_an_inbound_peer(
    manager: P2pManager, host: str, port: int
) -> tuple[socket.socket, socket.socket]:
    """Hand `server` an accepted socket that says it came from `host`.

    Through `P2pManager._accept_queues`, since a peer the suite can
    really connect from is a local one, which is never discouraged.
    Returns the pair, the second being the peer's own end.
    """
    server_socket = manager._server_sockets[0]
    wait_until(lambda: server_socket in manager._accept_queues)
    ours, theirs = socket.socketpair()
    manager.loop.call_soon_threadsafe(
        manager._accept_queues[server_socket].put_nowait, (ours, (host, port))
    )
    theirs.settimeout(20)
    return ours, theirs


def test_a_discouraged_host_is_refused_where_it_would_fill_the_last_slot(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1078: with one inbound slot, a discouraged host is refused outright.

    `max_connections=12` leaves one inbound slot, and Core refuses a
    discouraged peer once `nInbound + 1 >= m_max_inbound`: here with no
    peer held at all, from a port other than the one discouraged. A
    host that is not discouraged takes the same slot.
    """
    port = get_random_port()
    manager = a_manager(port=port, max_connections=12)
    manager.discourage(peer_address("1.2.3.4", 18444))
    logged, record = log_recorder()
    monkeypatch.setattr(manager.logger, "debug", record)
    manager.start()
    wait_until_listening(manager)
    with ExitStack() as peers:
        _, refused = land_an_inbound_peer(manager, "1.2.3.4", 50000)
        peers.enter_context(closing(refused))
        # closed before `create_connection`, so nothing was sent to it
        assert refused.recv(4096) == b""
        assert manager.last_connection_id == -1
        assert "connection from 1.2.3.4:50000 dropped (discouraged)" in logged
        _, accepted = land_an_inbound_peer(manager, "1.2.3.5", 50000)
        peers.enter_context(closing(accepted))
        wait_until(lambda: manager.last_connection_id == 0)
        assert not manager.pending_connections[0].prefer_evict
        manager.stop()
        manager.join(timeout=10)


def test_a_discouraged_host_with_slots_to_spare_is_accepted_to_evict_first(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1078: short of the last slot, a discouraged host is `prefer_evict`.

    `max_connections=13` leaves two inbound slots. The first discouraged
    peer is accepted, as Core's `prefer_evict`, which its eviction
    candidate carries; a second one is refused with one slot still free,
    since it would take the last.
    """
    port = get_random_port()
    manager = a_manager(port=port, max_connections=13)
    assert manager.max_inbound == 2
    manager.discourage(peer_address("1.2.3.4", 18444))
    manager.start()
    wait_until_listening(manager)
    with ExitStack() as peers:
        _, first = land_an_inbound_peer(manager, "1.2.3.4", 50000)
        peers.enter_context(closing(first))
        wait_until(lambda: manager.last_connection_id == 0)
        conn = manager.pending_connections[0]
        assert conn.prefer_evict
        assert manager_module._eviction_candidate(conn).prefer_evict
        _, second = land_an_inbound_peer(manager, "1.2.3.4", 50001)
        peers.enter_context(closing(second))
        assert second.recv(4096) == b""
        assert manager.last_connection_id == 0
        manager.stop()
        manager.join(timeout=10)


def test_a_discouraged_host_knocking_on_a_full_manager_evicts_nobody(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1078: the discouraged refusal comes before any eviction, as in Core.

    `max_connections=13` leaves two inbound slots, both held here. An
    eviction tried ahead of the refusal would drop an honest peer for a
    newcomer that is then refused anyway, so the selector is replaced by
    a recorder, and nothing may reach it: whichever peer it would name,
    asking at all is the order Core does not take.
    """
    port = get_random_port()
    manager = a_manager(port=port, max_connections=13)
    assert manager.max_inbound == 2
    manager.discourage(peer_address("1.2.3.4", 18444))
    selections: list[Iterable[EvictionCandidate]] = []
    monkeypatch.setattr(manager_module, "select_node_to_evict", selections.append)
    logged, record = log_recorder()
    monkeypatch.setattr(manager.logger, "debug", record)
    manager.start()
    wait_until_listening(manager)
    with ExitStack() as peers:
        _, held = land_an_inbound_peer(manager, "1.2.3.5", 50000)
        peers.enter_context(closing(held))
        wait_until(lambda: manager.last_connection_id == 0)
        _, held = land_an_inbound_peer(manager, "1.2.3.6", 50000)
        peers.enter_context(closing(held))
        wait_until(lambda: manager.last_connection_id == 1)
        _, refused = land_an_inbound_peer(manager, "1.2.3.4", 50000)
        peers.enter_context(closing(refused))
        assert refused.recv(4096) == b""
        assert manager.last_connection_id == 1
        assert sorted(manager.pending_connections) == [0, 1]
        assert not selections
        assert not any(line.startswith("selected inbound") for line in logged)
        assert "connection from 1.2.3.4:50000 dropped (discouraged)" in logged
        manager.stop()
        manager.join(timeout=10)


def test_an_eviction_candidate_reads_relay_off_the_version_message() -> None:
    """ISS 1064: the relay flag comes from `version`, not a later write.

    `callbacks.version` sets `version_message` before `relay_tx`, so a
    connection between the two writes still holds `relay_tx`'s default.
    """
    conn = a_conn(1, inbound=True, relay_tx=True)
    conn.__dict__.update(
        connected_time=0,
        min_ping_time=0.0,
        last_novel_block_time=0,
        last_novel_tx_time=0,
        has_all_wanted_services=False,
        keyed_net_group=0,
        prefer_evict=False,
        version_message=None,
    )
    assert not manager_module._eviction_candidate(conn).relay_txs
    conn.version_message = SimpleNamespace(is_relay_requested=False)
    assert not manager_module._eviction_candidate(conn).relay_txs
    conn.version_message = SimpleNamespace(is_relay_requested=True)
    assert manager_module._eviction_candidate(conn).relay_txs


def test_an_outbound_connection_takes_no_inbound_slot(
    a_manager: AManagerFactory,
) -> None:
    """Only `inbound` connections are counted against `max_inbound`."""
    manager = a_manager([a_conn(1)], max_connections=12)
    assert manager._inbound_count() == 0
    manager.pending_connections[2] = a_conn(2, inbound=True)
    assert manager._inbound_count() == 1


def test_an_inbound_peer_past_verack_still_holds_its_slot(
    a_manager: AManagerFactory,
) -> None:
    """A promoted inbound peer counts, not only a pending one.

    The peer that completes the handshake and stays is the one that
    holds a slot longest, so `connections` is counted as well as
    `pending_connections`.
    """
    manager = a_manager([a_conn(1, inbound=True)], max_connections=12)
    assert manager._inbound_count() == 1


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


def test_a_manager_that_cannot_bind_never_says_it_is_listening(
    a_manager: AManagerFactory,
) -> None:
    """`listening` is never set where the bind that would set it fails.

    Set after the bind and not before it, which is the whole of what a
    caller waiting on the event is told: a manager whose bind failed
    never reaches the line that sets it.
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


def test_a_manager_that_cannot_bind_stops_being_alive(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#88: a bind failure used to vanish into a future nobody read.

    `server` bound inside itself, scheduled through
    `run_coroutine_threadsafe`, whose returned `concurrent.futures.Future`
    nobody read -- a taken port's `OSError` sat there unread, and the
    manager thread ran on, `is_alive()` true over a listener that never
    came up. `_bind` runs in `run` itself, before `run_forever`, so the
    same `OSError` ends `run` -- this thread's own target -- and
    `start_listener` answers that it is not listening.

    Why is kept as `bind_error` and logged, in Core's words for the error
    the platform answers, which `taken_port_bind_error` says.
    """
    logged: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("", 0))
        taken.listen()
        port = taken.getsockname()[1]
        manager = a_manager(port=port)
        monkeypatch.setattr(manager.logger, "exception", logged.append)
        assert not manager.start_listener()
        wait_until(lambda: not manager.is_alive())
    assert manager.bind_error is not None
    assert taken_port_bind_error(port).fullmatch(manager.bind_error)
    assert logged == [manager.bind_error]
    assert not manager.listening.is_set()


class RefusingSocket:
    """A listening socket's stand-in whose `refused` call raises `error`."""

    def __init__(self, refused: str, error: OSError) -> None:
        """Refuse `refused` -- "bind" or "listen" -- with `error`."""
        self.refused = refused
        self.error = error
        self.closed = False

    def setsockopt(self, *args: Any) -> None:
        """Accept any option, as a real socket does these."""

    def bind(self, address: Any) -> None:
        """Raise `error` if `bind` is what is refused."""
        self._refuse_if("bind")

    def listen(self) -> None:
        """Raise `error` if `listen` is what is refused."""
        self._refuse_if("listen")

    def _refuse_if(self, call: str) -> None:
        if self.refused == call:
            raise self.error

    def close(self) -> None:
        """Record that `_bind_one` closed this socket."""
        self.closed = True


# CPython's own text for `WSAEACCES`, as the windows-latest runner printed it
_WINSOCK_TEXT = (
    "An attempt was made to access a socket in a way forbidden by its access"
    " permissions"
)


def a_winsock_error(number: int, winerror: int) -> OSError:
    """Build a Windows socket call's error: `errno` renumbered, `winerror` not.

    Settable on any platform, which is how the Windows number is reached here.
    """
    error = OSError(number, _WINSOCK_TEXT)
    error.winerror = winerror  # type: ignore[attr-defined]
    return error


@pytest.mark.parametrize(
    ("refused", "family", "host", "error", "message"),
    [
        (
            "socket",
            socket.AF_INET,
            "0.0.0.0",  # noqa: S104
            OSError(errno.EMFILE, "Too many open files"),
            (
                "Couldn't open socket for incoming connections (socket returned"
                f" error Too many open files ({errno.EMFILE}))"
            ),
        ),
        (
            "bind",
            socket.AF_INET6,
            "::",
            OSError(errno.EADDRINUSE, "Address already in use"),
            (
                "Unable to bind to [::]:8333 on this computer."
                " btclib-node is probably already running."
            ),
        ),
        (
            "bind",
            socket.AF_INET,
            "0.0.0.0",  # noqa: S104
            OSError(errno.EACCES, "Permission denied"),
            (
                "Unable to bind to 0.0.0.0:8333 on this computer (bind returned"
                f" error Permission denied ({errno.EACCES}))"
            ),
        ),
        (
            "bind",
            socket.AF_INET,
            "0.0.0.0",  # noqa: S104
            a_winsock_error(errno.EACCES, 10013),
            (
                "Unable to bind to 0.0.0.0:8333 on this computer (bind returned"
                f" error {_WINSOCK_TEXT} (10013))"
            ),
        ),
        (
            "listen",
            socket.AF_INET,
            "0.0.0.0",  # noqa: S104
            OSError(errno.EOPNOTSUPP, "Operation not supported"),
            (
                "Listening for incoming connections failed (listen returned"
                f" error Operation not supported ({errno.EOPNOTSUPP}))"
            ),
        ),
    ],
)
def test_a_failed_bind_says_which_call_failed_and_why_as_core_does(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    refused: str,
    family: socket.AddressFamily,
    host: str,
    error: OSError,
    message: str,
) -> None:
    """`_bind_one` raises the message Core's `CConnman::BindListenPort` sets.

    A taken port, any other `bind` error, a failed `listen` and a socket
    that could not be opened each have a message of their own there
    (`src/net.cpp:3307-3373`, at bitcoin/bitcoin@9be056a8a7), and an IPv6
    address is written in brackets. The socket is closed whenever it
    was opened.
    """
    manager = a_manager(port=8333)
    stand_in = RefusingSocket(refused, error)

    def a_socket(*args: Any) -> RefusingSocket:
        if refused == "socket":
            raise error
        return stand_in

    monkeypatch.setattr(socket, "socket", a_socket)
    with pytest.raises(OSError, match=re.escape(message)) as excinfo:
        manager._bind_one(family, host)
    assert str(excinfo.value) == message
    assert excinfo.value.__cause__ is error
    assert stand_in.closed is (refused != "socket")


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
        # the `-connect`/`-addnode` route: no end of it is automatic
        assert not any(conn.automatic for conn in manager.pending_connections.values())
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
    it -- the assertion below holds whether the loop ever steps this
    task or not.

    `entered` is waited on before `stop()` is called purely to pin
    coverage of the line below it: which side of that race actually
    happens is otherwise up to `run`'s own thread reaching
    `run_forever()` before or after this thread's own `stop()` schedules
    `loop.stop`, and #917 found the gate at a 100% floor going red
    whenever the scheduler happened to land on the side that skips it.
    Forcing the loop to have taken this task's first step before `stop`
    is ever called does not touch what the assertion below depends on:
    it was already true regardless of that step, by the paragraph above.
    """
    port = get_random_port()
    manager = a_manager(port=port)
    entered: list[bool] = []

    async def server_without_a_with(
        loop: asyncio.AbstractEventLoop, server_socket: socket.socket
    ) -> None:
        entered.append(True)
        await asyncio.sleep(60)

    monkeypatch.setattr(manager, "server", server_without_a_with)
    manager.start()
    try:
        wait_until_listening(manager)
        sockets = list(manager._server_sockets)
        assert sockets
        wait_until(lambda: entered)
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


def test_stop_does_not_raise_where_start_was_called_but_run_never_reached_run_forever(
    a_manager: AManagerFactory,
) -> None:
    """#380: `stop` does not raise where `run` never reached `run_forever`.

    `self.ident is not None` -- #368's own guard on a grace step this
    method no longer has -- is true from the moment `start()` is
    called, well before `run()` reaches `run_forever()`. Where `run()`
    returns before that -- a bind failure being the ordinary way -- the
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

    `listening_socket` is bound and put into `listen`, matching what `_bind`
    always hands `server` in production (#430): left merely constructed, as
    an earlier revision of this test had it, `_accept_loop`'s own
    `sock_accept` fails on it synchronously and every retry races this
    test's own cancellation against Windows' Proactor allocating and
    abandoning its own internal accept socket, which is a real leak but not
    the one this test is for -- accepting nothing at all, bound and
    listening, is the ordinary idle shutdown `_accept_loop` is cancelled out
    of on every platform this node runs on, ceasing there without raising --
    `settimeout(0.0)` beside it for the same reason `_bind_one` carries its
    own: a blocking `accept()` reached synchronously, which is what a freshly
    constructed socket still is, freezes this single thread's event loop for
    the length of `pyproject.toml`'s own per-test ceiling rather than ever
    reaching `_accept_loop`'s `await`.
    """
    manager = a_manager()
    loop = manager.loop
    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening_socket.bind(("127.0.0.1", 0))
    listening_socket.listen()
    listening_socket.settimeout(0.0)
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


def test_accept_loop_discards_the_kernel_accepted_socket_on_the_documented_race(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 904: the cancel-after-`sock_accept` race raises, and is ignored.

    `_accept_loop`'s own docstring, and the comment above the `accepted`
    queue in `server`, describe a window `Task.cancel` cannot close:
    where the cancellation lands after `loop.sock_accept`'s internal
    future is already resolved with a real accepted socket, `Task.__wakeup`
    calls that future's own `result()` -- discarding the return value
    right there, before `_accept_loop`'s coroutine ever regains control --
    and then throws the `CancelledError` in regardless. Nothing this
    tree's code can reach ever holds that socket, so it is closed by its
    own `__del__`, raising `ResourceWarning: unclosed <socket.socket ...>`
    unraisably (btclib-org/btclib-node#904).

    `loop._run_once()` is what makes the race deterministic rather than
    timing-dependent: `BaseEventLoop._run_once`'s own `_process_events`
    call adds a ready reader's callback to `self._ready` *before* that
    same call snapshots `ntodo = len(self._ready)`, so one call both
    resolves `sock_accept`'s future (the reader callback's `sock.accept()`
    succeeding now that `client` has connected) and leaves the task's own
    wakeup queued rather than run -- exactly the gap between "the kernel
    resolved one `sock_accept`" and "`_accept_loop`'s own next step" the
    comment names. `task.cancel()` called in that gap cannot cancel the
    already-done future either, so it sets `Task._must_cancel` instead,
    which is what turns the wakeup already queued into a thrown
    `CancelledError` rather than a delivered result.

    That ordering is the **selector** loop's own: `BaseProactorEventLoop
    ._process_events` (`asyncio/proactor_events.py`, read on this tree's
    own `3.14`) is a no-op, `pass`, because a proactor loop resolves an
    overlapped operation's future earlier, inside `IocpProactor.select`
    itself (`asyncio/windows_events.py`) -- which schedules the task's
    wakeup through the same future-completion path *before* `_run_once`
    reads `self._ready` into `ntodo`, so that wakeup is one of the
    `ntodo` handles `_run_once` runs, not one left over for the next
    call. One `loop._run_once()` there does not leave the gap this
    docstring describes: it steps the accepting task all the way back
    into its next `sock_accept`, so `task.cancel()` below cancels that
    instead of discarding an already-resolved result -- a fresh,
    unaccepted connection, not a repeat of the discard #904 answers.
    `asyncio.new_event_loop()` (`P2pManager.__init__`) returns exactly
    such a proactor loop under Windows' own default policy, which is
    where this test crashed a worker instead of asserting
    (btclib-org/btclib-node#917): the two steps `race_once` expects in
    one `_run_once()` call happen in zero there, `task.cancel()` landing
    on live overlapped state this hand-driven sequencing was never
    written to reach. What that native crash's own cause is has not
    been measured beyond that -- this skips the sequencing rather than
    tracking it down. `_accept_loop` itself is not implicated: its
    `sock_accept` retry on `BlockingIOError`/`InterruptedError` is
    already documented, in its own docstring, to cover both loop
    families.

    So the property below is asked of the loop actually in use, not of
    `sys.platform`: a platform is a proxy for which `_process_events`
    underlies `_run_once`, and asking the loop directly stays correct
    the day a policy or an interpreter changes it.
    """
    manager = a_manager()

    # No cell this suite's own coverage floor runs on ever takes this
    # branch: the gated job is `ubuntu-latest`, whose `asyncio.new_event_loop()`
    # is always a selector loop, so the skip below is exercised only on
    # the Windows job, which does not gate the floor.
    if not isinstance(  # pragma: no cover -- only a proactor loop takes this
        manager.loop, asyncio.selector_events.BaseSelectorEventLoop
    ):
        pytest.skip(
            "race_once() hand-drives a selector loop's own _run_once "
            "ordering (see this test's docstring); this loop is not one"
        )

    def race_once() -> None:
        loop = manager.loop
        listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listening_socket.bind(("127.0.0.1", 0))
        listening_socket.listen()
        listening_socket.settimeout(0.0)
        accepted: asyncio.Queue[
            tuple[socket.socket, tuple[str, int] | tuple[str, int, int, int]]
        ] = asyncio.Queue()
        task = loop.create_task(manager._accept_loop(listening_socket, accepted))
        # One step: `sock_accept` finds nothing pending yet, so it
        # registers a reader and suspends -- the state the race needs to
        # start from.
        loop.run_until_complete(asyncio.sleep(0))
        client = socket.create_connection(listening_socket.getsockname())
        try:
            # resolves the future; the wakeup is queued, not run. `_run_once`
            # is undocumented and unstubbed -- typeshed's `AbstractEventLoop`
            # and `BaseEventLoop` alike carry nothing named it.
            loop._run_once()  # type: ignore[attr-defined]
            task.cancel()  # lands in the gap: discards the result on the next step
            with suppress(asyncio.CancelledError):
                loop.run_until_complete(task)
        finally:
            client.close()
            listening_socket.close()
        assert accepted.empty()  # the accepted socket never reached the queue

    unraisable: list[BaseException | None] = []
    monkeypatch.setattr(
        sys, "unraisablehook", lambda ua: unraisable.append(ua.exc_value)
    )

    # Without an ignore entry naming this warning -- what this tree would
    # raise had #904 gone unanswered -- `warnings.warn` itself raises
    # inside the socket's own `__del__`, and that raise is what reaches
    # `sys.unraisablehook`.
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        race_once()
    assert len(unraisable) == 1
    assert isinstance(unraisable[0], ResourceWarning)
    assert str(unraisable[0]).startswith("unclosed <socket.socket")

    # Under this tree's own `pyproject.toml` `filterwarnings` -- active
    # here as it is for the whole suite -- the identical race raises
    # nothing at all: `warnings.warn` never turns the warning into an
    # exception, so `__del__` returns normally and `sys.unraisablehook`
    # is never called.
    unraisable.clear()
    race_once()
    assert unraisable == []


def test_accept_loop_logs_and_retries_on_a_refused_accept(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_accept_loop`'s `OSError` arm logs and retries the accept.

    `accept()` can fail outright -- `ECONNABORTED` being the ordinary
    way, a peer resetting the connection between the kernel reporting
    it readable and the accept reaching it -- and this is what keeps
    that from ending the task outright: the queue `server` awaits is
    left untouched, and a fresh `sock_accept` is tried again rather than
    this coroutine returning.

    `sock_accept` itself is monkeypatched to fail rather than handed a
    socket engineered to make the real one fail: a duck-typed stand-in
    with a `fileno` of -1, this test's own shape before #430's Windows
    run, reaches a selector loop's `sock.accept()` call harmlessly but
    reaches Windows' Proactor at the raw `AcceptEx` level, on the real
    fd it reads off that -1 rather than through any Python-level
    `.accept()` call -- which is what crashed the worker process outright
    rather than raising, on that platform only. What this test is for is
    `_accept_loop`'s own retry-and-log arm, not the kernel's accept
    machinery underneath `sock_accept`, so a fake replacing `sock_accept`
    itself is the narrower fake and is what stays clear of it.

    The failure below is synchronous, so `loop.sock_accept`'s own
    internal future is already done the instant this loop's `await`
    reaches it -- awaiting an already-done future never suspends a task,
    so nothing but `_accept_loop`'s own `await asyncio.sleep(0)` lets the
    loop below step it more than once; without that yield this test hangs
    to `pyproject.toml`'s own per-test ceiling rather than reaching three
    attempts.
    """
    manager = a_manager()
    loop = manager.loop
    accepted: asyncio.Queue[Any] = asyncio.Queue()
    logged: list[Any] = []
    monkeypatch.setattr(manager.logger, "exception", lambda *a, **k: logged.append(a))

    async def refuse(
        sock: socket.socket,
    ) -> tuple[socket.socket, tuple[str, int]]:
        raise OSError("accept refused")  # noqa: TRY003

    monkeypatch.setattr(loop, "sock_accept", refuse)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        task = loop.create_task(manager._accept_loop(server_socket, accepted))
        try:
            while len(logged) < 3:
                loop.run_until_complete(asyncio.sleep(0))
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                loop.run_until_complete(task)
    assert accepted.empty()


def test_report_server_failure_logs_a_real_exception(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_report_server_failure` logs whatever `server`'s own task raised.

    `run`'s own `.add_done_callback` is what reaches this, not a live
    listener -- a `future` neither cancelled nor holding a
    `CancelledError`, the two arms the tests beside this one cover.
    """
    manager = a_manager()
    logged: list[BaseException | None] = []
    monkeypatch.setattr(
        manager.logger,
        "error",
        lambda *a, exc_info=None, **k: logged.append(exc_info),
        raising=False,
    )
    future: Future[None] = Future()
    boom = ValueError("boom")
    future.set_exception(boom)
    manager._report_server_failure(future)
    assert logged == [boom]


def test_report_server_failure_suppresses_a_cancelled_future(
    a_manager: AManagerFactory,
) -> None:
    """`_report_server_failure` does not raise where `future.cancelled()`.

    This is the ordinary shape `stop`'s own sweep leaves behind: cancelling
    the `asyncio.Task` underneath `future` puts `future` itself into the
    cancelled state through `run_coroutine_threadsafe`'s own chaining
    (`asyncio.futures._set_concurrent_future_state`), and
    `future.exception()` on a cancelled future raises rather than
    returning -- which is what the `with suppress` this method opens
    with answers, before the `isinstance` arm below it is ever reached.
    """
    manager = a_manager()
    future: Future[None] = Future()
    assert future.cancel()
    manager._report_server_failure(future)


def test_report_server_failure_does_not_log_a_returned_cancelled_error(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_report_server_failure`'s `isinstance` arm, `stop` never reaches.

    `future.cancelled()` being true is what the test above covers, and
    is the only way `stop`'s own sweep actually ends this task -- so
    this arm has no path of its own through this class's ordinary use,
    only through a `future` built to carry a `CancelledError` as its
    stored exception rather than through `.cancel()`: what a `server`
    coroutine that caught and re-raised one not thrown by `Task.cancel`
    would leave behind, `Task.cancelled()` still false. Moved here
    rather than removed, on the same reasoning the coverage floor
    argues for a safety branch generally.
    """
    manager = a_manager()
    logged: list[BaseException | None] = []
    monkeypatch.setattr(
        manager.logger,
        "error",
        lambda *a, exc_info=None, **k: logged.append(exc_info),
        raising=False,
    )
    future: Future[None] = Future()
    future.set_exception(asyncio.CancelledError())
    manager._report_server_failure(future)
    assert logged == []
