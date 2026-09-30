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
import logging
import math
import re
import secrets
import socket
import sys
import threading
import time
import warnings
from concurrent.futures import Future
from contextlib import ExitStack, closing, suppress
from dataclasses import replace
from functools import partial
from ipaddress import ip_address
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast, override
from unittest.mock import AsyncMock

import pytest
from btclib.p2p.address import ServiceFlags
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2
from btclib.p2p.keepalive import Ping
from btclib.p2p.limits import PROTOCOL_VERSION

from btclib_node.chains import Main, RegTest
from btclib_node.config import DEFAULT_MAX_PEER_CONNECTIONS
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.log import Logger
from btclib_node.p2p import manager as manager_module
from btclib_node.p2p.address import (
    SEEDS_SERVICE_FLAGS,
    PeerDB,
    endpoint_key,
    fixed_seed_addresses,
    host_key,
    peer_address,
)
from btclib_node.p2p.anchors import dump_anchors, read_anchors
from btclib_node.p2p.banman import DUMP_BANS_INTERVAL, BanMan, Subnet, lookup_subnet
from btclib_node.p2p.eviction import Network, net_group
from btclib_node.p2p.main import handle_p2p_handshake
from btclib_node.p2p.manager import P2pManager

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable, Iterator, Sequence
    from pathlib import Path

    from btclib.p2p.payload import Payload

    from btclib_node import Node
    from btclib_node.p2p.eviction import EvictionCandidate
from tests import (
    ListenerEndedError,
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
    ping_start: float = 0,
    connected_time: int | None = None,
    address: NetworkAddressV2 | None = None,
    relay_tx: bool = True,
    feefilter: int = 0,
    nonce: int | None = None,
    inbound: bool = False,
    automatic: bool = False,
    protocol: int = PROTOCOL_VERSION,
    block_relay: bool = False,
    feeler: bool = False,
    addr_fetch: bool = False,
    addr_name: str | None = None,
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
        ping_start=ping_start,
        connected_time=int(time.time()) if connected_time is None else connected_time,
        ping_sent=0,
        relay_tx=relay_tx,
        feefilter=feefilter,
        nonce=nonce,
        inbound=inbound,
        automatic=automatic,
        version_message=SimpleNamespace(version=protocol),
        block_relay=block_relay,
        feeler=feeler,
        addr_fetch=addr_fetch,
        addr_name=addr_name,
        sent=[],
        stopped=[],
    )
    conn.send = conn.sent.append
    conn.stop = lambda: conn.stopped.append(True)

    def send_ping() -> None:
        # a ping already answered by nothing: the manager reads the time
        # it was sent to decide the peer is gone
        conn.ping_sent = time.time() - 200
        conn.ping_start = time.time()
        conn.sent.append("ping")

    conn.send_ping = send_ping
    return conn


def a_full_node(ip: str, port: int) -> NetworkAddressV2:
    """Build a peer advertising the services the dial loop requires."""
    return peer_address(ip, port, services=SEEDS_SERVICE_FLAGS)


def a_peer_db_stub(**attributes: Any) -> Any:
    """Build a `PeerDB` double good enough for `manage_connections`'s own loop.

    `get_active_addresses` is on every one of them: the loop calls it
    once `_ACTIVE_PRUNE_INTERVAL` has passed regardless of what else a
    test's own scenario does, btclib-org/btclib-node#71, so a peer db
    missing it fails a test on an `AttributeError` the test is not
    about. `holds_network` is too, answering that every network is
    held, so that no fixed seed is added where a test is not about them.
    A `random_address` given is the draw `address_sampler` hands back,
    so each call of it is one draw of the pass, and a `random_new_address`
    the draw it hands back for `new_only`, a feeler's; the one not given
    refuses to be asked. `attempt` records every try in `tries`, by
    `endpoint_key`, which `last_try` reads: whether the table holds the
    endpoint is `PeerDB`'s own test. `size` defaults to `0`, an empty
    table, so `_dns_address_seed` asks every seed at once with no wait
    unless a test overrides it.
    """
    tries: dict[bytes, float] = {}
    defaults: dict[str, Any] = {
        "get_active_addresses": list,
        "holds_network": lambda network_id: True,
        "size": 0,
        "tries": tries,
        "attempt": lambda address: tries.__setitem__(
            endpoint_key(address), time.time()
        ),
        "last_try": lambda address: tries.get(endpoint_key(address), 0.0),
    }
    if "random_address" in attributes or "random_new_address" in attributes:
        draw = attributes.pop("random_address", refuses_to_be_asked)
        new_draw = attributes.pop("random_new_address", refuses_to_be_asked)
        defaults["address_sampler"] = lambda *, new_only=False, network=None: (
            new_draw if new_only else draw
        )
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
        connect: Sequence[str] = (),
        addnode_args: Sequence[str] = (),
        seednode: Sequence[str] = (),
        listen: bool = True,
        discover: bool | None = None,
        max_connections: int = DEFAULT_MAX_PEER_CONNECTIONS,
        forcednsseed: bool = False,
        fixed_seeds: bool = True,
    ) -> P2pManager:
        """Build a `P2pManager` seeded with `conns`, `peer_db` and `status`."""
        ...


@pytest.fixture
def a_manager(tmp_path: Path) -> Iterator[AManagerFactory]:
    """Build managers, and close their event loops however the test ends.

    Their data directory is one of the test's own, where `anchors.dat` is
    read and written.
    """
    made: list[P2pManager] = []
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    def make(
        conns: Sequence[Any] = (),
        *,
        peer_db: Any = None,
        status: NodeStatus = NodeStatus.BlockSynced,
        port: int | None = None,
        connect: Sequence[str] = (),
        addnode_args: Sequence[str] = (),
        seednode: Sequence[str] = (),
        listen: bool = True,
        discover: bool | None = None,
        max_connections: int = DEFAULT_MAX_PEER_CONNECTIONS,
        forcednsseed: bool = False,
        fixed_seeds: bool = True,
    ) -> P2pManager:
        # `18444` is regtest's own well-known port -- binding it for
        # real, as a plain default would, collides with a second suite
        # of this same tree on one machine, or a second worker of this
        # same run (btclib-org/btclib-node#678). `get_random_port()`
        # drawn fresh on every call, rather than once as a mutable
        # default would be, is what gives every manager built without
        # an explicit `port=` a port of its own.
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
            # `P2pManager.__init__` reads the `node.config` fields below
            # directly, not through `Config` itself: a plain namespace
            # is enough. `connect_given` mirrors what `Config.__init__`
            # itself derives from `connect` -- true whenever the
            # sequence is non-empty, `["0"]` included, which nothing
            # here constructs -- and `listen` defaults to `True` so every
            # existing caller here keeps binding and accepting.
            # `max_connections` defaults to `Config`'s own default for
            # the same reason. `connect`/`seednode` here stand in for
            # `Config`'s own parsed pairs, read only for their
            # truthiness at `run`'s own `-seednode is ignored` check;
            # `_connect_peers`/`_seednodes` themselves read
            # `connect_args`/`seednode_args`, the raw specs kept whole
            # end to end (btclib-org/btclib-node#1493).
            config=SimpleNamespace(
                connect=connect,
                connect_given=bool(connect),
                connect_args=tuple(connect),
                addnode_args=tuple(addnode_args),
                seednode=seednode,
                seednode_args=tuple(seednode),
                listen=listen,
                # `Config.__init__`'s own sentinel: `discover=None`
                # follows `listen`, an explicit value winning over it,
                # exactly as `Config.discover` itself resolves.
                discover=listen if discover is None else discover,
                max_connections=max_connections,
                dnsseed=not connect and max_connections > 0,
                forcednsseed=forcednsseed,
                fixed_seeds=fixed_seeds,
                pruned=False,
            ),
            # `Connection.own_version`'s own `start_height`
            # (btclib-org/btclib-node#722), 0 matching a fresh `Node`'s
            # own initial value (`__init__.py`).
            best_height=0,
            data_dir=data_dir,
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
                    query_dns_seed=asks_no_dns_server,
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
    """A connection dropped before `own_version` ran carries no nonce."""
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


def test_a_quiet_peer_at_bip31_or_below_is_pinged_and_dropped_on_quiet(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1180: no `pong` to wait on, so twice the idle bound is waited.

    ISS 1204: meanwhile it is sent a `ping`, with no nonce, where none
    has been queued to it for the idle bound, and a second pass queues
    no second one; the same quiet span a pinged peer gets drops it.
    """
    bound = manager_module._IDLE_TIMEOUT
    long_ago = time.time() - bound - 10
    quiet = a_conn(1, last_receive=long_ago, protocol=60000)
    pinged = a_conn(2, last_receive=long_ago, ping_start=time.time(), protocol=60000)
    quieter = a_conn(3, last_receive=time.time() - 2 * bound - 10, protocol=60000)
    manager = a_manager([quiet, pinged, quieter])
    asyncio.run(one_pass(manager))
    asyncio.run(one_pass(manager))
    assert quiet.sent == ["ping"]
    assert pinged.sent == quieter.sent == []
    assert list(manager.connections) == [1, 2]


def test_a_peer_that_answered_recently_is_left_alone(
    a_manager: AManagerFactory,
) -> None:
    """A peer heard from recently is neither pinged nor dropped."""
    conn = a_conn(1)
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert conn.sent == []
    assert list(manager.connections) == [1]


def test_an_addr_fetch_connection_is_dropped_once_its_timeout_passes(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1284: `_ADDR_FETCH_TIMEOUT` drops an addr-fetch peer either way.

    Recently heard from, unlike every other connection this timeout
    would otherwise leave alone: Core's own bound is from
    `m_connected`, not from the last message.
    """
    bound = manager_module._ADDR_FETCH_TIMEOUT
    conn = a_conn(1, connected_time=int(time.time()) - bound - 1, addr_fetch=True)
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert not manager.connections


def test_an_addr_fetch_connection_survives_short_of_its_timeout(
    a_manager: AManagerFactory,
) -> None:
    """The negative half of the test above: short of the bound, it stays."""
    bound = manager_module._ADDR_FETCH_TIMEOUT
    conn = a_conn(1, connected_time=int(time.time()) - bound + 30, addr_fetch=True)
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
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
        addr_fetch = False
        version_message = SimpleNamespace(version=PROTOCOL_VERSION)

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
    is dropped rather than pinged and given a second window.
    """
    conn = a_conn(
        1,
        status=P2pConnStatus.Open,
        last_receive=time.time() - 200,
        connected_time=int(time.time()) - 200,
    )
    manager = a_manager()
    manager.pending_connections[conn.id] = conn
    asyncio.run(one_pass(manager))
    assert not manager.pending_connections
    assert conn.sent == []


@pytest.mark.parametrize(("age", "dropped"), [(62, True), (58, False)])
def test_a_pending_connection_is_dropped_a_minute_after_connecting(
    a_manager: AManagerFactory, age: int, *, dropped: bool
) -> None:
    """ISS 1169: Core's `InactivityCheck`, past `DEFAULT_PEER_CONNECT_TIMEOUT`.

    Core drops a connection not yet `fSuccessfullyConnected` once sixty
    seconds have passed since it connected, whatever it has sent: the
    peer here sent something just now. Two seconds short is the control.
    """
    conn = a_conn(1, status=P2pConnStatus.Open, connected_time=int(time.time()) - age)
    manager = a_manager()
    manager.pending_connections[conn.id] = conn
    asyncio.run(one_pass(manager))
    assert (conn.id not in manager.pending_connections) is dropped
    assert conn.sent == []


def test_a_connected_peer_outlives_the_handshake_timeout(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1169: the timeout is the handshake's, not the connection's."""
    conn = a_conn(1, connected_time=int(time.time()) - 600)
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert conn.id in manager.connections


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
    onion = NetworkAddressV2(
        0, SEEDS_SERVICE_FLAGS, BIP155Network.TORV3, b"\x11" * 32, 8333
    )
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
    onion = NetworkAddressV2(
        0, SEEDS_SERVICE_FLAGS, BIP155Network.TORV3, b"\x11" * 32, 8333
    )
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

    #70/#71: `callbacks.version` records the peer at a live timestamp and
    with its handshake's own services, so the row `PeerDB.random_address`
    can draw back is never equal, field for field, to the Connection's
    own address: the manager compares by `host_key`, or a peer already
    connected to is dialled a second time.
    An onion address the same way the sibling tests above use one: `not
    in already_connected` regressing to raw equality would reach the
    real `dial`, which raises on a network this node cannot open a
    socket for, straight into the same quiet-log assertion those use.
    """
    onion = NetworkAddressV2(
        0, SEEDS_SERVICE_FLAGS, BIP155Network.TORV3, b"\x11" * 32, 8333
    )
    conn = a_conn(1, address=onion)
    gossiped = NetworkAddressV2(
        int(time.time()),
        SEEDS_SERVICE_FLAGS,
        BIP155Network.TORV3,
        b"\x11" * 32,
        8333,
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
    onion = NetworkAddressV2(
        0, SEEDS_SERVICE_FLAGS, BIP155Network.TORV3, b"\x11" * 32, 8333
    )
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
    address = a_full_node("1.2.3.4", 18444)
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
    onion = NetworkAddressV2(
        0, SEEDS_SERVICE_FLAGS, BIP155Network.TORV3, b"\x11" * 32, 8333
    )
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
        random_address=lambda: a_full_node("5.6.7.8", 18444),
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
            a_full_node("1.2.200.200", 18444),
            False,
            id="automatic-same-16",
        ),
        pytest.param(
            a_conn(1, automatic=True, address=peer_address("1.2.3.4", 8333)),
            a_full_node("1.3.3.4", 8333),
            True,
            id="automatic-other-16",
        ),
        pytest.param(
            a_conn(1, address=peer_address("1.2.3.4", 8333)),
            a_full_node("1.2.200.200", 18444),
            False,
            id="addnode-same-16",
        ),
        pytest.param(
            a_conn(1, status=P2pConnStatus.Open, automatic=True),
            a_full_node("1.2.200.200", 18444),
            False,
            id="pending-same-16",
        ),
        pytest.param(
            a_conn(1, inbound=True, address=peer_address("1.2.3.4", 8333)),
            a_full_node("1.2.200.200", 18444),
            True,
            id="inbound-same-16",
        ),
        pytest.param(
            a_conn(1, address=peer_address("2001:db9:1::1", 8333)),
            a_full_node("2001:db9:2::2", 8333),
            False,
            id="ipv6-same-32",
        ),
        pytest.param(
            a_conn(1, address=peer_address("::ffff:1.2.3.4", 8333)),
            a_full_node("1.2.200.200", 18444),
            False,
            id="mapped-ipv4-same-16",
        ),
        pytest.param(
            a_conn(1, automatic=True, address=_ONION),
            a_full_node("1.2.200.200", 18444),
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


def a_seeding_manager(
    a_manager: AManagerFactory,
    *,
    held: Sequence[BIP155Network] = (),
    elapsed: float = 0.0,
    use_dns_seed: bool = True,
    addnode_args: Sequence[str] = (),
    seednode: Sequence[str] = (),
    conns: Sequence[Any] = (),
) -> tuple[P2pManager, list[list[NetworkAddressV2]]]:
    """Build a mainnet manager whose peer db holds only the `held` networks.

    What `add_addresses` is handed is returned beside the manager, and
    the dial start is backdated by `elapsed` seconds.
    """
    added: list[list[NetworkAddressV2]] = []
    peer_db = a_peer_db_stub(
        is_empty=True,
        holds_network=lambda network_id: network_id in held,
        add_addresses=lambda addresses: added.append(list(addresses)),
    )
    manager = a_manager(
        conns, peer_db=peer_db, addnode_args=addnode_args, seednode=seednode
    )
    manager.node.chain = Main()
    manager.use_dns_seed = use_dns_seed
    manager._dial_start = time.time() - elapsed
    return manager, added


def test_no_fixed_seed_is_added_within_the_first_minute(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1099: DNS seeding and `-addnode` are given Core's sixty seconds."""
    manager, added = a_seeding_manager(a_manager, elapsed=59)
    asyncio.run(manager._maybe_dial_more_peers())
    assert added == []
    assert manager.add_fixed_seeds


@pytest.mark.parametrize(
    ("held", "networks"),
    [
        pytest.param((), {BIP155Network.IPV4, BIP155Network.IPV6}, id="none-held"),
        pytest.param((BIP155Network.IPV4,), {BIP155Network.IPV6}, id="ipv4-held"),
    ],
)
def test_the_fixed_seeds_of_every_empty_network_are_added_after_a_minute(
    a_manager: AManagerFactory,
    held: Sequence[BIP155Network],
    networks: set[BIP155Network],
) -> None:
    """ISS 1099: past sixty seconds, the seeds of each empty reachable network.

    Core adds the seeds of the reachable networks `addrman` holds nothing
    for, and only once: a second pass adds nothing more.
    """
    manager, added = a_seeding_manager(a_manager, held=held, elapsed=61)
    asyncio.run(manager._maybe_dial_more_peers())
    (seeds,) = added
    assert {address.network_id for address in seeds} == networks
    assert seeds == [
        address
        for address in fixed_seed_addresses(Main().fixed_seeds)
        if address.network_id in networks
    ]
    assert not manager.add_fixed_seeds
    manager._next_fixed_seeds_check = 0.0
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(added) == 1


def test_no_fixed_seed_is_added_where_every_reachable_network_is_held(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1099: a table holding IPv4 and IPv6 gets none, and keeps asking."""
    held = (BIP155Network.IPV4, BIP155Network.IPV6)
    manager, added = a_seeding_manager(a_manager, held=held, elapsed=61)
    asyncio.run(manager._maybe_dial_more_peers())
    assert added == []
    assert manager.add_fixed_seeds


def test_fixed_seeds_off_by_config_is_set_at_construction(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1192: `Config.fixed_seeds=False` (`-fixedseeds=0`) reaches this.

    Through construction, not poked onto the attribute afterward: the
    fixture's own `fixed_seeds=False` is what `P2pManager.__init__`
    reads `node.config.fixed_seeds` from.
    """
    manager = a_manager(fixed_seeds=False)
    assert manager.add_fixed_seeds is False


def test_fixed_seeds_off_by_config_adds_nothing(a_manager: AManagerFactory) -> None:
    """ISS 1192: `Config.fixed_seeds=False` (`-fixedseeds=0`) reaches this."""
    manager, added = a_seeding_manager(a_manager, use_dns_seed=False, elapsed=61)
    manager.add_fixed_seeds = False
    asyncio.run(manager._maybe_dial_more_peers())
    assert added == []


def test_a_seednode_makes_fixed_seeds_wait_the_same_as_an_addnode(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1192: Core's own `!dnsseed && !use_seednodes` guards the arm.

    A `-seednode` alone, DNS seeding off, is not "nothing else may fill
    the table": fixed seeds wait the full minute the same as with an
    `-addnode`.
    """
    manager, added = a_seeding_manager(
        a_manager, use_dns_seed=False, seednode=["1.2.3.4:8333"]
    )
    asyncio.run(manager._maybe_dial_more_peers())
    assert added == []


@pytest.mark.parametrize(
    ("addnode_args", "adds"),
    [
        pytest.param((), True, id="no-addnode"),
        pytest.param(["1.2.3.4:8333"], False, id="addnode"),
    ],
)
def test_the_fixed_seeds_are_added_at_once_without_dns_seeding(
    a_manager: AManagerFactory, addnode_args: Sequence[str], *, adds: bool
) -> None:
    """ISS 1099: with DNS seeding off and no `-addnode`, Core does not wait."""
    manager, added = a_seeding_manager(
        a_manager, use_dns_seed=False, addnode_args=addnode_args
    )
    asyncio.run(manager._maybe_dial_more_peers())
    assert bool(added) is adds


@pytest.mark.parametrize(
    ("full_relay", "block_relay", "adds"),
    [(8, 0, True), (8, 2, True), (8, 3, False), (11, 0, False)],
)
def test_fixed_seeds_wait_on_the_outbound_grant_not_the_targets(
    a_manager: AManagerFactory, full_relay: int, block_relay: int, *, adds: bool
) -> None:
    """ISS 1099, 1095: Core's `semOutbound` holds eleven, not the two targets.

    `ThreadOpenConnections` takes a grant before its fixed-seed step and
    counts peers of either kind after it, so eight full-relay and two
    block-relay-only peers, both targets met, still leave it to seed;
    eleven automatic peers fill the grant, and the step is not reached.
    """
    conns = automatic_conns(full_relay, block_relay)
    manager, added = a_seeding_manager(a_manager, elapsed=61, conns=conns)
    asyncio.run(manager._maybe_dial_more_peers())
    assert bool(added) is adds
    assert manager.add_fixed_seeds is not adds


def test_the_tables_are_walked_for_fixed_seeds_once_per_cores_pass(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1099: Core looks once per 500ms pass of its loop, not every 100ms.

    Every network held keeps the step asking, so the second pass, right
    after the first, is the one the cadence turns away.
    """
    asked: list[int] = []

    def holds_network(network_id: int) -> bool:
        asked.append(network_id)
        return True

    peer_db = a_peer_db_stub(is_empty=True, holds_network=holds_network)
    manager = a_manager(peer_db=peer_db)
    manager._dial_start = time.time() - 61
    asyncio.run(manager._maybe_dial_more_peers())
    asyncio.run(manager._maybe_dial_more_peers())
    assert asked == [BIP155Network.IPV4, BIP155Network.IPV6]
    manager._next_fixed_seeds_check = 0.0
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(asked) == 4


def test_a_fixed_seed_step_that_raises_is_logged(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1099: a store that raises while seeding does not end the loop."""
    peer_db = a_peer_db_stub(is_empty=True, holds_network=refuses_to_be_asked)
    manager = a_manager(peer_db=peer_db)
    logged: list[str] = []
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    assert asyncio.run(one_pass(manager)) is True
    assert logged


def test_a_held_answered_peer_does_not_stop_the_dialling(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1201: the gossiped table is still drawn from, in the same pass.

    The answered table holds only the peer already connected, so every
    draw from it is refused; Core's coin and its hundred draws still
    reach the gossiped address. A real `PeerDB`, and the coin forced to
    the answered table on the first draw, so the pass has to draw again.
    """
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)

    monkeypatch.setattr(manager_module, "dial", records)
    coins = iter([1, 0])
    monkeypatch.setattr(secrets, "randbelow", lambda n: next(coins))
    held = a_full_node("1.2.3.4", 8333)
    gossiped = a_full_node("5.6.7.8", 8333)
    peer_db = PeerDB(cast("Any", None), None)
    peer_db.add_addresses([held, gossiped])
    peer_db.add_active_address(held)
    manager = a_manager([a_conn(1, automatic=True, address=held)], peer_db=peer_db)
    asyncio.run(manager._maybe_dial_more_peers())
    assert [a.address for a in dialled] == [gossiped.address]


def draws_of(*addresses: NetworkAddressV2) -> tuple[list[None], Any]:
    """Return a record of the draws made, and a draw answering `addresses`."""
    drawn: list[None] = []
    queue = iter(addresses)

    def draw() -> NetworkAddressV2:
        drawn.append(None)
        return next(queue)

    return drawn, draw


def test_a_draw_in_a_held_group_draws_again(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1201: Core's loop `continue`s on an outbound peer's group.

    `1.2.9.9` shares the `/16` of the outbound peer at `1.2.3.4`, so the
    pass draws again and dials `5.6.7.8`.
    """
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)

    monkeypatch.setattr(manager_module, "dial", records)
    same_group = a_full_node("1.2.9.9", 8333)
    other = a_full_node("5.6.7.8", 8333)
    drawn, draw = draws_of(same_group, other)
    peer_db = a_peer_db_stub(is_empty=False, random_address=draw)
    held = a_conn(1, address=peer_address("1.2.3.4", 8333))
    manager = a_manager([held], peer_db=peer_db)
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(drawn) == 2
    assert dialled == [other]


@pytest.mark.parametrize(
    ("held_host", "dials"), [("1.2.3.4", False), ("5.6.7.8", True)]
)
def test_a_host_held_on_any_port_is_not_dialled_again(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    held_host: str,
    *,
    dials: bool,
) -> None:
    """ISS 1304: Core's `AlreadyConnectedToAddress` compares no port.

    An inbound peer on its ephemeral port holds its host: a draw of the
    same host on its listening port is not dialled, and one of another
    host is. Inbound, so no network group is in the way.
    """
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)

    monkeypatch.setattr(manager_module, "dial", records)
    # the services `_passed_over` asks of a draw
    drawn_address = a_full_node("1.2.3.4", 8333)
    _, draw = draws_of(drawn_address)
    peer_db = a_peer_db_stub(is_empty=False, random_address=draw)
    held = a_conn(1, address=peer_address(held_host, 55555), inbound=True)
    manager = a_manager([held], peer_db=peer_db)
    asyncio.run(manager._maybe_dial_more_peers())
    assert dialled == ([drawn_address] if dials else [])


@pytest.mark.parametrize("refusal", ["connected", "discouraged"])
def test_a_draw_refused_otherwise_ends_the_pass(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """ISS 1201: Core's loop breaks with it, and nothing is dialled.

    `OpenNetworkConnection` returns without dialling an address already
    connected or discouraged, so the pass ends there. The peer already
    connected is inbound, adding no group, so the group check does not
    reach it first.
    """
    # a double that is never awaited, so it records without a body
    dial = AsyncMock(return_value=None)
    monkeypatch.setattr(manager_module, "dial", dial)
    refused = a_full_node("1.2.3.4", 8333)
    drawn, draw = draws_of(refused, a_full_node("5.6.7.8", 8333))
    peer_db = a_peer_db_stub(is_empty=False, random_address=draw)
    if refusal == "connected":
        manager = a_manager([a_conn(1, address=refused, inbound=True)], peer_db=peer_db)
    else:
        manager = a_manager(peer_db=peer_db)
        manager.discourage(refused)
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(drawn) == 1
    assert dial.await_count == 0


@pytest.mark.parametrize("port", [8333, 18444])
def test_a_local_address_drawn_ends_the_pass(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch, port: int
) -> None:
    """ISS 1238: "if we selected an invalid or local address, restart".

    `IsLocal` compares the host alone, `mapLocalHost` being keyed by
    `CNetAddr`: this node's own address on another port ends the pass
    too. The draw has no services, so it would be passed over, not end
    the pass, were `_passed_over` asked first.
    """
    dial = AsyncMock(return_value=None)
    monkeypatch.setattr(manager_module, "dial", dial)
    drawn, draw = draws_of(peer_address("1.2.3.4", port), a_full_node("5.6.7.8", 8333))
    manager = a_manager(peer_db=a_peer_db_stub(is_empty=False, random_address=draw))
    manager.local_addresses = frozenset({host_key(peer_address("1.2.3.4", 8333))})
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(drawn) == 1
    assert dial.await_count == 0


def test_discover_keeps_each_routable_interface_address_by_host(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1238: Core's `Discover`, each address through `AddLocal`.

    A private address is not routable, and `AddLocal` refuses it.
    """
    interfaces = [ip_address("1.2.3.4"), ip_address("192.168.1.2")]
    monkeypatch.setattr(manager_module, "local_addresses", lambda: interfaces)
    manager = a_manager()
    manager._discover()
    assert manager.local_addresses == {host_key(peer_address("1.2.3.4", 0))}


@pytest.mark.parametrize("listen", [True, False])
def test_run_discovers_where_it_listens_and_nowhere_else(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch, *, listen: bool
) -> None:
    """ISS 1238: `Discover` at start-up, `-listen=0` soft-sets it off.

    No `discover=` is given, so `a_manager`'s own sentinel ties it to
    `listen`, `Config.discover`'s default (ISS 1330).
    """
    monkeypatch.setattr(
        manager_module, "local_addresses", lambda: [ip_address("1.2.3.4")]
    )
    port = get_random_port()
    manager = a_manager(port=port, listen=listen)
    try:
        assert manager.start_listener()
        wait_until(manager.loop.is_running)
        expected = {host_key(peer_address("1.2.3.4", port))} if listen else set()
        assert manager.local_addresses == expected
    finally:
        manager.stop()
        manager.join(timeout=10)


def test_run_discovers_under_listen_0_discover_1(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1330: an explicit `-discover=1` records addresses under `-listen=0`.

    Core calls `Discover()` off `bind_on_any`, never off `fListen`
    (`P2pManager._discover`'s own docstring), which is exactly what
    `-listen=0 -discover=1` could not do before this: `_bind` never
    runs, so `manager.listening` stays clear, but `local_addresses` is
    filled all the same.
    """
    monkeypatch.setattr(
        manager_module, "local_addresses", lambda: [ip_address("1.2.3.4")]
    )
    port = get_random_port()
    manager = a_manager(port=port, listen=False, discover=True)
    try:
        assert manager.start_listener()
        wait_until(manager.loop.is_running)
        assert not manager.listening.is_set()
        assert manager.local_addresses == {host_key(peer_address("1.2.3.4", port))}
    finally:
        manager.stop()
        manager.join(timeout=10)


def test_a_pass_draws_a_hundred_times_at_most(a_manager: AManagerFactory) -> None:
    """ISS 1201: `ThreadOpenConnections` gives up after its hundredth draw."""
    held = peer_address("1.2.3.4", 8333)
    drawn: list[None] = []

    def draw() -> NetworkAddressV2:
        drawn.append(None)
        return held

    peer_db = a_peer_db_stub(is_empty=False, random_address=draw)
    manager = a_manager([a_conn(1, address=held)], peer_db=peer_db)
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(drawn) == 100


def test_an_empty_draw_ends_the_pass(a_manager: AManagerFactory) -> None:
    """ISS 1201: nothing dialable is an answer, not a hundred of them."""
    drawn: list[None] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn.append(None))
    manager = a_manager(peer_db=peer_db)
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(drawn) == 1


def test_a_pass_dials_once_and_draws_no_more(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1201: one `OpenNetworkConnection` per iteration of Core's loop."""
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)

    monkeypatch.setattr(manager_module, "dial", records)
    peer_db = a_peer_db_stub(
        is_empty=False, random_address=lambda: a_full_node("5.6.7.8", 8333)
    )
    manager = a_manager(peer_db=peer_db)
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(dialled) == 1


def refuses_to_be_asked() -> NoReturn:
    """Stand in for `random_address`/`get_active_addresses`, unreachable."""
    raise RuntimeError("no")


async def asks_no_dns_server(seed: str) -> None:
    """Stand in for a `query_dns_seed` that never touches a real server."""
    return


def test_connect_turns_off_addrman_outgoing(a_manager: AManagerFactory) -> None:
    """`node.config.connect` non-empty: `use_addrman_outgoing` is `False`."""
    manager = a_manager(connect=["1.2.3.4:8333"])
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
    manager = a_manager(peer_db=peer_db, connect=["1.2.3.4:8333"])
    asyncio.run(manager._maybe_dial_more_peers())
    assert not manager.connections
    assert not manager.pending_connections


async def _record_dns_lookup(calls: list[int], seed: str) -> None:
    """Stand in for `query_dns_seed`, recording that it was awaited.

    One shared function rather than a `spy` nested in each of the two
    tests below: the "never called" half of that pair would otherwise
    pin a line the suite can never reach and the 100% floor can never
    forgive. Sharing this one lets the positive control below cover it
    while the skip test's own `calls` stays empty.
    """
    del seed
    calls.append(1)


def _let_runs_own_coroutines_start(manager: P2pManager) -> None:
    """Return once every coroutine `run` scheduled has taken its first step.

    `run` schedules them before `run_forever`, so one scheduled from here
    once the loop is running is queued behind them, and its own result
    arriving means theirs have started: a `query_dns_seed` stand-in
    `run` scheduled has recorded its call by then. Without this, `calls`
    read the moment the loop runs can be empty where the lookup was
    scheduled and has not started yet.
    """
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), manager.loop).result(timeout=10)


def test_run_skips_the_dns_lookup_under_connect(a_manager: AManagerFactory) -> None:
    """`-connect` also stops `run` from ever scheduling `_dns_address_seed`.

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
        query_dns_seed=partial(_record_dns_lookup, calls),
    )
    manager = a_manager(
        peer_db=peer_db,
        port=get_random_port(),
        connect=["1.2.3.4:8333"],
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


def test_run_logs_when_a_seednode_is_ignored_under_connect(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1192: Core's own log line, `-seednode` given alongside `-connect`."""
    logged: list[Any] = []
    manager = a_manager(
        connect=["1.2.3.4:8333"],
        seednode=["5.6.7.8:8333"],
        listen=False,
    )
    monkeypatch.setattr(manager.logger, "info", logged.append)
    manager.start()
    wait_until(manager.loop.is_running)
    _let_runs_own_coroutines_start(manager)
    assert "-seednode is ignored when -connect is used" in logged


def test_run_does_not_log_it_without_a_seednode(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control: `-connect` alone logs nothing about `-seednode`."""
    logged: list[Any] = []
    manager = a_manager(connect=["1.2.3.4:8333"], listen=False)
    monkeypatch.setattr(manager.logger, "info", logged.append)
    manager.start()
    wait_until(manager.loop.is_running)
    _let_runs_own_coroutines_start(manager)
    assert "-seednode is ignored when -connect is used" not in logged


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
        query_dns_seed=partial(_record_dns_lookup, calls),
    )
    manager = a_manager(peer_db=peer_db, port=get_random_port())
    manager.start()
    wait_until_listening(manager)
    wait_until(lambda: calls)


def test_full_outbound_count_excludes_pending_block_relay_feeler_and_addr_fetch(
    a_manager: AManagerFactory,
) -> None:
    """Core's own `GetFullOutboundConnCount`: handshaken, automatic, full relay.

    Six connections: two count, and one each of the four ways to be
    excluded -- pending (not `fSuccessfullyConnected`), block-relay,
    feeler, and addr-fetch. An inbound one is excluded through
    `automatic` alone, `create_connection` never setting that for one.
    """
    manager = a_manager(
        conns=[
            a_conn(1, automatic=True),
            a_conn(2, automatic=True),
            a_conn(3, automatic=True, block_relay=True),
            a_conn(4, automatic=True, feeler=True),
            a_conn(5, automatic=True, addr_fetch=True),
        ],
    )
    manager.pending_connections[6] = a_conn(6, automatic=True)
    assert manager._full_outbound_count() == 2


def test_wait_for_seednode_peers_is_a_noop_without_seednode(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1461: nothing given, `use_seednodes` false, no wait at all."""
    monkeypatch.setattr(asyncio, "sleep", _fails_to_sleep)
    manager = a_manager()
    asyncio.run(manager._wait_for_seednode_peers())


def test_wait_for_seednode_peers_ends_early_once_enough_peers_answer(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1461: `_SEED_OUTBOUND_CONNECTION_THRESHOLD` peers end the wait.

    Polled every `_SEEDNODE_POLL_INTERVAL`; the third poll reports
    enough peers, so this returns after three sleeps, well under
    `_SEEDNODE_TIMEOUT`.
    """
    waited: list[float] = []

    async def records_sleep(delay: float) -> None:
        waited.append(delay)

    monkeypatch.setattr(asyncio, "sleep", records_sleep)
    manager = a_manager(seednode=["1.2.3.4:8333"])
    counts = iter([0, 0, 2])
    monkeypatch.setattr(manager, "_full_outbound_count", lambda: next(counts))
    asyncio.run(manager._wait_for_seednode_peers())
    assert waited == [manager_module._SEEDNODE_POLL_INTERVAL] * 3


def test_wait_for_seednode_peers_times_out_after_thirty_seconds(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1461: `_SEEDNODE_TIMEOUT` ends the wait, no peer ever enough.

    `_full_outbound_count` stubbed at 0 throughout: the wait ends on
    its own elapsed time, `_SEEDNODE_TIMEOUT` divided by
    `_SEEDNODE_POLL_INTERVAL` polls plus the one that crosses it.
    """
    waited: list[float] = []

    async def records_sleep(delay: float) -> None:
        waited.append(delay)

    monkeypatch.setattr(asyncio, "sleep", records_sleep)
    manager = a_manager(seednode=["1.2.3.4:8333"])
    monkeypatch.setattr(manager, "_full_outbound_count", lambda: 0)
    asyncio.run(manager._wait_for_seednode_peers())
    expected_polls = (
        int(manager_module._SEEDNODE_TIMEOUT / manager_module._SEEDNODE_POLL_INTERVAL)
        + 1
    )
    assert waited == [manager_module._SEEDNODE_POLL_INTERVAL] * expected_polls


async def _fails_past_seednode_poll(delay: float) -> None:
    """Stand in for `asyncio.sleep`: raise past the seednode wait's own step.

    A correct `delay` (`_SEEDNODE_POLL_INTERVAL`) raises `TimeoutError`,
    aborting `_dns_address_seed` right where `_wait_for_seednode_peers`
    calls it -- proof the seednode wait ran, and ran first, is that
    exception reaching the caller rather than the DNS-seed logic past
    it ever starting. Any other `delay` is a sleep this wait would never
    ask for, so it fails loudly instead: the proof that branch is not
    vacuous is `test_fails_past_seednode_poll_fails_on_an_unexpected_delay`
    below.
    """
    if delay != manager_module._SEEDNODE_POLL_INTERVAL:
        pytest.fail(f"asyncio.sleep awaited with an unexpected delay {delay!r}")
    raise TimeoutError


def test_fails_past_seednode_poll_fails_on_an_unexpected_delay() -> None:
    """The positive control: a wrong delay raises through `pytest.fail`."""
    with pytest.raises(pytest.fail.Exception, match="unexpected delay"):
        asyncio.run(
            _fails_past_seednode_poll(manager_module._DNS_SEEDS_DELAY_FEW_PEERS)
        )


def test_dns_address_seed_waits_for_seednode_peers_first(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1461: `_dns_address_seed` calls the seednode wait before anything.

    `use_seednodes` true and `_full_outbound_count` always under the
    threshold: `asyncio.sleep` patched to `_fails_past_seednode_poll`,
    since only the seednode wait (above) is meant to run before this
    returns.
    """
    monkeypatch.setattr(asyncio, "sleep", _fails_past_seednode_poll)
    manager = a_manager(seednode=["1.2.3.4:8333"])
    monkeypatch.setattr(manager, "_full_outbound_count", lambda: 0)
    with pytest.raises(TimeoutError):
        asyncio.run(manager._dns_address_seed())


def test_dns_address_seed_queues_the_seed_query_dns_seed_returns(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1284: a seed `query_dns_seed` could not answer is addr-fetched.

    `_dns_address_seed` is what `run` schedules in place of
    `query_dns_seed` directly: it is this wrapper's own job to queue
    what that coroutine returns into `_addr_fetches`, on the chain's own
    port -- regtest's `18444` here, and not `8333`. `RegTest`'s own one
    seed, `dummySeed.invalid.`, is what `node.chain.addresses` gives it
    to ask (`chains.py`).
    """

    async def answers_nothing_for(seed: str) -> str:
        return seed

    peer_db = a_peer_db_stub(query_dns_seed=answers_nothing_for)
    manager = a_manager(peer_db=peer_db)
    asyncio.run(manager._dns_address_seed())
    assert list(manager._addr_fetches) == [("dummySeed.invalid.", 18444)]


def test_dns_address_seed_shuffles_the_chains_seeds(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1265: Core's own `std::shuffle(seeds.begin(), seeds.end(), rng)`.

    A chain double with two seeds, `secrets.SystemRandom.shuffle`
    patched to reverse rather than to leave its argument alone: the
    query order this records is the reversed one, not the chain's own,
    which a shuffle that ran on a copy rather than on this list itself
    would leave undetected.
    """
    asked: list[str] = []

    async def records(seed: str) -> None:
        asked.append(seed)

    def reverse(self: object, values: list[str]) -> None:
        del self
        values.reverse()

    monkeypatch.setattr(secrets.SystemRandom, "shuffle", reverse)
    peer_db = a_peer_db_stub(query_dns_seed=records)
    manager = a_manager(peer_db=peer_db)
    manager.node.chain.addresses = ["one.example", "two.example"]
    asyncio.run(manager._dns_address_seed())
    assert asked == ["two.example", "one.example"]


async def _fails_to_sleep(delay: float) -> None:
    """Stand in for `asyncio.sleep`, refusing to be awaited at all.

    Shared by the two "asks at once" tests below, each of which patches
    `asyncio.sleep` to this and expects it never to be reached: a `fails`
    nested in each of them would otherwise pin a line the suite can
    never reach and the 100% floor can never forgive, the same shape
    `_record_dns_lookup`'s own docstring already argues. The proof this
    is not vacuous is `test_fails_to_sleep_raises_if_awaited` below.
    """
    del delay
    pytest.fail("asyncio.sleep was awaited where nothing here should wait")


def test_fails_to_sleep_raises_if_awaited() -> None:
    """The positive control: `_fails_to_sleep` actually raises, awaited."""
    with pytest.raises(pytest.fail.Exception, match=r"asyncio\.sleep was awaited"):
        asyncio.run(_fails_to_sleep(0))


async def _fails_to_query_seed(seed: str) -> None:
    """Stand in for `query_dns_seed`, refusing to be awaited at all.

    Used below where `_dns_address_seed` returns before ever asking a
    seed: a `records` appending to a list nobody then inspects except
    via `== []` would otherwise pin a body the suite can never reach,
    the same shape `_fails_to_sleep` above already argues. The proof
    this is not vacuous is `test_fails_to_query_seed_raises_if_awaited`.
    """
    pytest.fail(f"query_dns_seed was awaited with {seed!r} where nothing should ask")


def test_fails_to_query_seed_raises_if_awaited() -> None:
    """The positive control: `_fails_to_query_seed` actually raises, awaited."""
    with pytest.raises(pytest.fail.Exception, match=r"query_dns_seed was awaited"):
        asyncio.run(_fails_to_query_seed("one.example"))


def test_dns_address_seed_asks_every_seed_at_once_under_an_empty_table(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1265: `peer_db.size` zero skips the wait, as Core's own does.

    `asyncio.sleep` patched to `_fails_to_sleep`: an empty table is
    Core's own "query all" case (`seeds_right_now = seeds.size()`), so
    nothing here waits at all, over four seeds and three batches' worth
    of boundaries.
    """
    asked: list[str] = []

    async def records(seed: str) -> None:
        asked.append(seed)

    monkeypatch.setattr(asyncio, "sleep", _fails_to_sleep)
    monkeypatch.setattr(secrets.SystemRandom, "shuffle", lambda self, values: None)
    peer_db = a_peer_db_stub(query_dns_seed=records, size=0)
    manager = a_manager(peer_db=peer_db)
    manager.node.chain.addresses = [
        "one.example",
        "two.example",
        "three.example",
        "four.example",
    ]
    asyncio.run(manager._dns_address_seed())
    assert asked == ["one.example", "two.example", "three.example", "four.example"]


def test_dns_address_seed_asks_every_seed_at_once_under_forcednsseed(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1265: `-forcednsseed` skips the wait even with a non-empty table."""
    asked: list[str] = []

    async def records(seed: str) -> None:
        asked.append(seed)

    monkeypatch.setattr(asyncio, "sleep", _fails_to_sleep)
    monkeypatch.setattr(secrets.SystemRandom, "shuffle", lambda self, values: None)
    peer_db = a_peer_db_stub(query_dns_seed=records, size=1)
    manager = a_manager(peer_db=peer_db, forcednsseed=True)
    manager.node.chain.addresses = ["one.example", "two.example"]
    asyncio.run(manager._dns_address_seed())
    assert asked == ["one.example", "two.example"]


def test_dns_address_seed_waits_between_batches_of_three(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1265: a non-empty table waits `_DNS_SEEDS_DELAY_FEW_PEERS` a batch.

    Five seeds, `_DNS_SEEDS_TO_QUERY_AT_ONCE` (3) of them asked before
    the first wait: the wait itself is recorded rather than really
    slept, and `_full_outbound_count` stubbed at 0 so neither wait ends
    early.
    """
    asked: list[str] = []
    waited: list[float] = []

    async def records(seed: str) -> None:
        asked.append(seed)

    async def records_sleep(delay: float) -> None:
        waited.append(delay)

    monkeypatch.setattr(asyncio, "sleep", records_sleep)
    monkeypatch.setattr(secrets.SystemRandom, "shuffle", lambda self, values: None)
    peer_db = a_peer_db_stub(query_dns_seed=records, size=1)
    manager = a_manager(peer_db=peer_db)
    monkeypatch.setattr(manager, "_full_outbound_count", lambda: 0)
    manager.node.chain.addresses = [
        "one.example",
        "two.example",
        "three.example",
        "four.example",
        "five.example",
    ]
    asyncio.run(manager._dns_address_seed())
    assert asked == [
        "one.example",
        "two.example",
        "three.example",
        "four.example",
        "five.example",
    ]
    assert waited == [manager_module._DNS_SEEDS_DELAY_FEW_PEERS] * 2


def test_dns_address_seed_uses_the_longer_wait_past_the_peer_threshold(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1265: `_DNS_SEEDS_DELAY_PEER_THRESHOLD` addresses up waits 5 minutes.

    Slept in `_DNS_SEEDS_DELAY_FEW_PEERS`-second steps, so the total
    across every step is what is checked, rather than one sleep of the
    whole length.
    """
    waited: list[float] = []

    async def answers_nothing(seed: str) -> str:
        return seed

    async def records_sleep(delay: float) -> None:
        waited.append(delay)

    monkeypatch.setattr(asyncio, "sleep", records_sleep)
    monkeypatch.setattr(secrets.SystemRandom, "shuffle", lambda self, values: None)
    peer_db = a_peer_db_stub(
        query_dns_seed=answers_nothing,
        size=manager_module._DNS_SEEDS_DELAY_PEER_THRESHOLD,
    )
    manager = a_manager(peer_db=peer_db)
    monkeypatch.setattr(manager, "_full_outbound_count", lambda: 0)
    manager.node.chain.addresses = ["one.example"]
    asyncio.run(manager._dns_address_seed())
    assert sum(waited) == manager_module._DNS_SEEDS_DELAY_MANY_PEERS


def test_dns_address_seed_ends_early_once_enough_outbound_peers_answer(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1265: `_SEED_OUTBOUND_CONNECTION_THRESHOLD` peers end a wait early.

    A single seed with the table past `_DNS_SEEDS_DELAY_PEER_THRESHOLD`,
    so its one wait is `_DNS_SEEDS_DELAY_MANY_PEERS` long, slept in
    several `_DNS_SEEDS_DELAY_FEW_PEERS`-second steps: the second of
    them reports enough peers, so this returns without ever asking the
    seed -- `query_dns_seed` patched to `_fails_to_query_seed`, which
    would fail loudly if it ever ran.
    """
    calls: list[float] = []

    async def records_sleep(delay: float) -> None:
        calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", records_sleep)
    monkeypatch.setattr(secrets.SystemRandom, "shuffle", lambda self, values: None)
    peer_db = a_peer_db_stub(
        query_dns_seed=_fails_to_query_seed,
        size=manager_module._DNS_SEEDS_DELAY_PEER_THRESHOLD,
    )
    manager = a_manager(peer_db=peer_db)
    counts = iter([0, 2])
    monkeypatch.setattr(manager, "_full_outbound_count", lambda: next(counts))
    manager.node.chain.addresses = ["one.example"]
    asyncio.run(manager._dns_address_seed())
    assert len(calls) == 2


class _PeerDbSizeDrops:
    """A `peer_db` double whose `size` answers the next of `sizes` each read.

    A real `PeerDB.size` is not cached: `_dns_address_seed` reads it
    fresh at every batch boundary, so a value that drops between two of
    those reads -- `_maybe_prune_active_addresses` aging out the last
    active row while this coroutine's own wait sleeps, on the same
    event loop -- answers `0` where the boundary before it answered
    more, without the table ever having started empty (ISS 1265).
    """

    def __init__(self, sizes: list[int], query_dns_seed: object) -> None:
        self._sizes = sizes
        self.query_dns_seed = query_dns_seed

    @property
    def size(self) -> int:
        return self._sizes.pop(0)


def test_dns_address_seed_skips_a_later_waits_size_check_dropping_to_zero(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1265: `peer_db.size` read fresh at each boundary's wait-or-not check.

    Four seeds, batches of three: `_dns_address_seed`'s own two upfront
    reads (`ask_all_at_once`, then the wait's length, computed once)
    account for the first two of `sizes`; the third is the first
    batch's boundary's own `size > 0` check, non-zero, waiting
    `_DNS_SEEDS_DELAY_FEW_PEERS` once; the fourth is the second batch's
    boundary's own `size > 0` check, finding `0` and skipping the wait
    -- still querying that batch's own seed -- rather than raising or
    waiting on a table it never started at.
    """
    asked: list[str] = []

    async def records(seed: str) -> None:
        asked.append(seed)

    waited: list[float] = []

    async def records_sleep(delay: float) -> None:
        waited.append(delay)

    monkeypatch.setattr(asyncio, "sleep", records_sleep)
    monkeypatch.setattr(secrets.SystemRandom, "shuffle", lambda self, values: None)
    peer_db = _PeerDbSizeDrops([1, 1, 1, 0], records)
    manager = a_manager(peer_db=peer_db)
    monkeypatch.setattr(manager, "_full_outbound_count", lambda: 0)
    manager.node.chain.addresses = [
        "one.example",
        "two.example",
        "three.example",
        "four.example",
    ]
    asyncio.run(manager._dns_address_seed())
    assert asked == ["one.example", "two.example", "three.example", "four.example"]
    assert waited == [manager_module._DNS_SEEDS_DELAY_FEW_PEERS]


class _PeerDbSizeSequence:
    """A `peer_db` double whose `size` walks `sizes`, clamped at the end.

    Unlike `_PeerDbSizeDrops` above, a read past the end of `sizes`
    repeats its last entry rather than raising: this stands in for a
    table that keeps growing past `_DNS_SEEDS_DELAY_PEER_THRESHOLD`
    once seeds start answering, and stays meaningful however many times
    a given revision of `_dns_address_seed` happens to read `size`.
    """

    def __init__(self, sizes: list[int], query_dns_seed: object) -> None:
        self._sizes = sizes
        self._read = -1
        self.query_dns_seed = query_dns_seed

    @property
    def size(self) -> int:
        self._read = min(self._read + 1, len(self._sizes) - 1)
        return self._sizes[self._read]


def test_dns_address_seed_wait_length_is_fixed_once_at_entry(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1265: crossing the peer threshold mid-run keeps the short wait.

    Six seeds, two batch boundaries: `peer_db.size` starts under
    `_DNS_SEEDS_DELAY_PEER_THRESHOLD` and is past it by the second
    boundary, as a real answered seed growing the table during the
    first wait would leave it. Core's own `seeds_wait_time` is a
    `const std::chrono::seconds` read once before its loop
    (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), so
    both waits recorded here are `_DNS_SEEDS_DELAY_FEW_PEERS` -- a
    recomputation at the second boundary would answer
    `_DNS_SEEDS_DELAY_MANY_PEERS` instead.
    """
    asked: list[str] = []

    async def records(seed: str) -> None:
        asked.append(seed)

    waited: list[float] = []

    async def records_sleep(delay: float) -> None:
        waited.append(delay)

    monkeypatch.setattr(asyncio, "sleep", records_sleep)
    monkeypatch.setattr(secrets.SystemRandom, "shuffle", lambda self, values: None)
    peer_db = _PeerDbSizeSequence(
        [1, 1, manager_module._DNS_SEEDS_DELAY_PEER_THRESHOLD],
        records,
    )
    manager = a_manager(peer_db=peer_db)
    monkeypatch.setattr(manager, "_full_outbound_count", lambda: 0)
    manager.node.chain.addresses = [
        "one.example",
        "two.example",
        "three.example",
        "four.example",
        "five.example",
        "six.example",
    ]
    asyncio.run(manager._dns_address_seed())
    assert asked == [
        "one.example",
        "two.example",
        "three.example",
        "four.example",
        "five.example",
        "six.example",
    ]
    assert waited == [manager_module._DNS_SEEDS_DELAY_FEW_PEERS] * 2


def test_maybe_add_seednode_is_a_noop_without_seednodes(
    a_manager: AManagerFactory,
) -> None:
    """Nothing given: nothing queued, whatever `peer_db` holds."""
    manager = a_manager(peer_db=a_peer_db_stub(is_empty=True))
    manager._maybe_add_seednode()
    assert not manager._addr_fetches


def test_maybe_add_seednode_queues_the_first_value_at_once_when_peer_db_is_empty(
    a_manager: AManagerFactory,
) -> None:
    """Core's own `add_addr_fetch` initial value: `peer_db` empty, no wait."""
    manager = a_manager(
        peer_db=a_peer_db_stub(is_empty=True),
        seednode=["1.2.3.4", "5.6.7.8"],
    )
    manager._arm_dial_loop()
    manager._maybe_add_seednode()
    assert list(manager._addr_fetches) == [("5.6.7.8", RegTest().port)]
    assert manager._seednodes == [("1.2.3.4", RegTest().port)]


def test_maybe_add_seednode_waits_when_peer_db_already_holds_something(
    a_manager: AManagerFactory,
) -> None:
    """`peer_db` non-empty when the dial loop starts: the timer gates it."""
    manager = a_manager(peer_db=a_peer_db_stub(is_empty=False), seednode=["1.2.3.4"])
    manager._arm_dial_loop()
    manager._maybe_add_seednode()
    assert not manager._addr_fetches
    assert manager._seednodes == [("1.2.3.4", RegTest().port)]


def test_maybe_add_seednode_waits_the_interval_between_two_values(
    a_manager: AManagerFactory,
) -> None:
    """Core's own `ADD_NEXT_SEEDNODE`: one value per ten seconds, not sooner."""
    manager = a_manager(
        peer_db=a_peer_db_stub(is_empty=True),
        seednode=["1.2.3.4", "5.6.7.8"],
    )
    manager._arm_dial_loop()
    manager._maybe_add_seednode()
    manager._maybe_add_seednode()
    assert list(manager._addr_fetches) == [("5.6.7.8", RegTest().port)]
    manager._next_seednode_at = 0.0
    manager._maybe_add_seednode()
    assert list(manager._addr_fetches) == [
        ("5.6.7.8", RegTest().port),
        ("1.2.3.4", RegTest().port),
    ]
    assert not manager._seednodes


def test_maybe_add_seednode_does_not_fire_early_when_the_loop_starts_late(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1192 review round 2: a slow start must not pre-expire the timer.

    The reviewer's own reproduction: build the manager, let real
    wall-clock time pass before the dial loop's own `_arm_dial_loop`
    ever runs -- as a slow `Node.__init__` would -- and only then call
    `_maybe_add_seednode` for the first time. With `peer_db` already
    holding something, the old code anchored `_next_seednode_at` to
    construction time in `__init__`, so fifteen seconds of delay alone
    was enough to let the ten-second `_ADD_SEEDNODE_INTERVAL` timer
    expire before the loop's first pass, queuing a value immediately
    where Core's own drip-feed would still wait.
    """
    manager = a_manager(
        peer_db=a_peer_db_stub(is_empty=False),
        seednode=["1.2.3.4", "5.6.7.8"],
    )
    time.sleep(15)
    manager._arm_dial_loop()
    manager._maybe_add_seednode()
    assert not manager._addr_fetches
    assert manager._seednodes == [
        ("1.2.3.4", RegTest().port),
        ("5.6.7.8", RegTest().port),
    ]


def test_maybe_add_seednode_stops_once_full_relay_meets_the_threshold(
    a_manager: AManagerFactory,
) -> None:
    """Core's own `SEED_OUTBOUND_CONNECTION_THRESHOLD`: two, and it stops."""
    conns = automatic_conns(2, 0)
    manager = a_manager(
        conns, peer_db=a_peer_db_stub(is_empty=True), seednode=["1.2.3.4"]
    )
    manager._seednode_addr_fetch_due = False
    manager._next_seednode_at = 0.0
    manager._maybe_add_seednode()
    assert not manager._addr_fetches
    assert manager._seednodes == [("1.2.3.4", RegTest().port)]


def test_process_addr_fetch_is_a_noop_on_an_empty_queue(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to pop, nothing dialled: `dial` itself would fail the test."""
    monkeypatch.setattr(manager_module, "dial", refuses_to_be_asked)
    manager = a_manager()
    asyncio.run(manager._process_addr_fetch())
    assert not manager.connections
    assert not manager.pending_connections


def test_process_addr_fetch_skips_a_queued_host_already_held_by_name(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AlreadyConnectedToHost(pszDest)`: refused before any resolve.

    Core's own check runs on the unresolved string alone
    (`OpenNetworkConnection`, `src/net.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), so a queued host already
    held by name never reaches the resolver at all -- unlike the two
    post-resolve checks below, which see every candidate an already-held
    endpoint could still hide behind.
    """

    class _FailsIfResolved:
        async def getaddrinfo(self, host: str, port: int, **kwargs: object) -> NoReturn:
            # unreached unless the pre-resolve name check above is skipped
            pytest.fail("resolved a name already held")  # pragma: no cover -- see above

    held = a_conn(1, addr_name="seed.example")
    monkeypatch.setattr(asyncio, "get_running_loop", _FailsIfResolved)
    monkeypatch.setattr(manager_module, "dial", refuses_to_be_asked)
    manager = a_manager([held])
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert not manager._addr_fetches
    assert list(manager.connections) == [1]
    assert not manager.pending_connections


def test_process_addr_fetch_resolves_a_queued_host_held_by_no_connection(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection held by a different name does not block the resolve.

    ISS 1493: the queued `dest`, `"seed.example"`, names no port; `18444`
    is only `default_port`, kept apart from `addr_name`, which
    `test_process_addr_fetch_keeps_a_port_when_the_dest_names_one`
    (below) is the positive of.
    """
    ours, theirs = socket.socketpair()

    async def connects(address: NetworkAddressV2) -> socket.socket:
        return ours

    held = a_conn(1, addr_name="other.example")
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["5.6.7.8"]))
    monkeypatch.setattr(manager_module, "dial", connects)
    made: list[dict[str, Any]] = []
    manager = a_manager([held])
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert not manager._addr_fetches
    assert made == [{"inbound": False, "addr_fetch": True, "addr_name": "seed.example"}]
    theirs.close()


def test_process_addr_fetch_keeps_a_port_when_the_dest_names_one(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1493: a `-seednode` spec naming its own port keeps it on `addr_name`.

    `_seednodes` (`__init__`) queues `(spec, node.chain.port)` from
    `config.seednode_args` -- the raw spec, `default_port` only a
    fallback -- so a spec naming `9999` reaches `addr_name` with it,
    `default_port` unused.
    """
    ours, theirs = socket.socketpair()

    async def connects(address: NetworkAddressV2) -> socket.socket:
        return ours

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["5.6.7.8"]))
    monkeypatch.setattr(manager_module, "dial", connects)
    made: list[dict[str, Any]] = []
    manager = a_manager()
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )
    manager._addr_fetches.append(("seed.example:9999", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert not manager._addr_fetches
    assert made == [
        {"inbound": False, "addr_fetch": True, "addr_name": "seed.example:9999"}
    ]
    theirs.close()


def test_process_addr_fetch_drops_the_entry_when_the_name_resolves_to_nothing(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `gaierror` leaves the entry consumed and dials nothing."""

    class FailingLoop:
        async def getaddrinfo(self, host: str, port: int, **kwargs: object) -> NoReturn:
            err = socket.gaierror("no such host")
            raise err

    monkeypatch.setattr(asyncio, "get_running_loop", FailingLoop)
    monkeypatch.setattr(manager_module, "dial", refuses_to_be_asked)
    manager = a_manager()
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert not manager._addr_fetches
    assert not manager.connections
    assert not manager.pending_connections


class _NamedLoop:
    """A `getaddrinfo` stand-in answering fixed IPs for any host asked."""

    def __init__(self, ips: list[str]) -> None:
        self.ips = ips

    async def getaddrinfo(
        self, host: str, port: int, **kwargs: object
    ) -> list[tuple[None, None, None, None, tuple[str, int]]]:
        return [(None, None, None, None, (ip, port)) for ip in self.ips]


def test_process_addr_fetch_refuses_an_invalid_resolved_address(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sole answer under Core's own internal-marker prefix dials nothing.

    ISS 1466: `is_internal` (`_legacy_ipv6` reading the answer as
    `CNetAddr` would) drops it before the candidate list is even built,
    matching `LookupIntern`'s own collection-time filter
    (`src/netbase.cpp:144-168`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag) -- the later `is_valid` pass, which would refuse it too, never
    gets the chance to.
    """
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: _NamedLoop(["fd6b:88c0:8724::1"])
    )
    monkeypatch.setattr(manager_module, "dial", refuses_to_be_asked)
    manager = a_manager()
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert not manager._addr_fetches
    assert not manager.connections
    assert not manager.pending_connections


class _NoShuffle:
    """Stand in for `secrets.SystemRandom`, leaving a list's order alone."""

    def shuffle(self, x: list[object]) -> None:
        return


def test_process_addr_fetch_never_dials_a_valid_candidate_ahead_of_an_invalid_one(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later answer's invalidity aborts the whole attempt, dial included.

    `ConnectNode` validates and checks every resolved answer, in the
    order `Lookup` (shuffled) gave it, before dialling any of them, and
    returns on the first either check refuses -- so a dialable answer
    ahead of a bad one in that same order is never reached, exactly as
    one behind it never would be (`src/net.cpp:404-424`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). The bad answer is a
    documentation-range one (RFC3849, `2001:db8::/32`) rather than an
    internal one: ISS 1466's `is_internal` filter drops an internal
    answer before this pass ever runs, so it could no longer reach this
    check at all, and this test wants an answer that still does.
    """
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)  # pragma: no cover -- aborted before any dial

    monkeypatch.setattr(secrets, "SystemRandom", _NoShuffle)
    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: _NamedLoop(["1.2.3.4", "2001:db8::1"]),
    )
    monkeypatch.setattr(manager_module, "dial", records)
    manager = a_manager()
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert not dialled
    assert not manager._addr_fetches
    assert not manager.connections
    assert not manager.pending_connections


def test_process_addr_fetch_never_dials_a_valid_candidate_ahead_of_a_held_one(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later answer already connected aborts the whole attempt too.

    Same abort-before-any-dial rule as the invalid case above, this
    time on `AlreadyConnectedToAddressPort` rather than `IsValid`.
    """
    held = a_conn(1, address=peer_address("5.6.7.8", 18444))
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)  # pragma: no cover -- aborted before any dial

    monkeypatch.setattr(secrets, "SystemRandom", _NoShuffle)
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: _NamedLoop(["1.2.3.4", "5.6.7.8"])
    )
    monkeypatch.setattr(manager_module, "dial", records)
    manager = a_manager([held])
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert not dialled
    assert not manager._addr_fetches
    assert list(manager.connections) == [1]
    assert not manager.pending_connections


def test_process_addr_fetch_skips_a_candidate_already_connected(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ConnectNode`'s `AlreadyConnectedToAddressPort`, read after resolving."""
    held = a_conn(1, address=peer_address("1.2.3.4", 18444))
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["1.2.3.4"]))
    monkeypatch.setattr(manager_module, "dial", refuses_to_be_asked)
    manager = a_manager([held])
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert not manager._addr_fetches
    assert list(manager.connections) == [1]
    assert not manager.pending_connections


def test_process_addr_fetch_dials_and_marks_the_connection_addr_fetch(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolved candidate not already held is dialled and connected."""
    ours, theirs = socket.socketpair()

    async def connects(address: NetworkAddressV2) -> socket.socket:
        return ours

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["5.6.7.8"]))
    monkeypatch.setattr(manager_module, "dial", connects)
    made: list[dict[str, Any]] = []
    manager = a_manager()
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert not manager._addr_fetches
    assert made == [{"inbound": False, "addr_fetch": True, "addr_name": "seed.example"}]
    theirs.close()


def test_process_addr_fetch_tries_the_next_candidate_when_the_first_never_connects(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first candidate's own dial answering `None` does not end the pass."""
    ours, theirs = socket.socketpair()
    tried: list[str] = []

    async def only_the_second_connects(
        address: NetworkAddressV2,
    ) -> socket.socket | None:
        tried.append(str(address.address))
        return None if len(tried) == 1 else ours

    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: _NamedLoop(["1.1.1.1", "2.2.2.2"])
    )
    monkeypatch.setattr(manager_module, "dial", only_the_second_connects)
    made: list[dict[str, Any]] = []
    manager = a_manager()
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert len(tried) == 2
    assert made == [{"inbound": False, "addr_fetch": True, "addr_name": "seed.example"}]
    theirs.close()


def test_process_addr_fetch_gives_up_when_no_candidate_connects(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every resolved candidate answering `None` ends the pass quietly.

    `ConnectNode` tries every shuffled answer and returns once the list
    is exhausted, connected or not; nothing here names a peer left over
    to try again, `ADDR_FETCH` having no retry of its own
    (btclib-org/btclib-node#1284).
    """

    async def never_connects(address: NetworkAddressV2) -> None:
        return None

    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: _NamedLoop(["1.1.1.1", "2.2.2.2"])
    )
    monkeypatch.setattr(manager_module, "dial", never_connects)
    manager = a_manager()
    monkeypatch.setattr(manager, "create_connection", refuses_to_be_asked)
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert not manager._addr_fetches
    assert not manager.connections
    assert not manager.pending_connections


def test_process_addr_fetch_logs_and_continues_on_a_dial_that_raises(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dial that raises is logged, like every other housekeeping step.

    `_open_addr_fetches` never awaits this coroutine's own future, the
    same reason `_maybe_prune_active_addresses` guards its own call.
    """
    logged: list[str] = []
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["1.2.3.4"]))
    monkeypatch.setattr(manager_module, "dial", refuses_to_be_asked)
    manager = a_manager()
    monkeypatch.setattr(manager.logger, "exception", logged.append)
    manager._addr_fetches.append(("seed.example", 18444))
    asyncio.run(manager._process_addr_fetch())
    assert logged


def test_manage_connections_does_not_touch_the_addr_fetch_queue(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1366: a pass of `manage_connections` never reaches `_addr_fetches`.

    `async_connect_host` hangs on an `Event` nothing ever sets, standing
    in for a slow `getaddrinfo` or a slow `dial`; `_process_addr_fetch`
    pops its entry before ever reaching that await, so a `manage_connections`
    pass that still called it, as it did before #1366, would drain the
    queue even while stuck. Nothing here ever runs `_open_addr_fetches`,
    the loop that does own the queue since #1366 -- `one_pass` alone,
    on `manage_connections`, is the whole scenario.
    """
    manager = a_manager()
    manager._addr_fetches.append(("seed.example", 18444))
    gate = asyncio.Event()

    async def hangs(host: str, port: int, *, addr_fetch: bool = False) -> None:
        await gate.wait()  # pragma: no cover -- unreached, which is the assertion

    monkeypatch.setattr(manager, "async_connect_host", hangs)
    assert asyncio.run(one_pass(manager)) is True
    assert list(manager._addr_fetches) == [("seed.example", 18444)]


def test_zero_max_connections_turns_off_the_dns_lookup(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1066: `max_connections=0` seeds nothing, as `-connect` does not."""
    assert a_manager(max_connections=0).use_dns_seed is False
    assert a_manager().use_dns_seed is True
    assert a_manager(connect=["1.2.3.4:8333"]).use_dns_seed is False


def test_run_skips_the_dns_lookup_at_zero_max_connections(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1066: `run` never schedules `_dns_address_seed` at zero.

    `listen=False` beside it, what `-maxconnections=0` alone resolves to
    (`cli.py`'s own `build_config`);
    `test_run_schedules_the_dns_lookup_without_connect` above is the
    positive control that the stand-in sees a call.
    """
    calls: list[int] = []
    peer_db = a_peer_db_stub(
        is_empty=True,
        random_address=refuses_to_be_asked,
        query_dns_seed=partial(_record_dns_lookup, calls),
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
    (`cli.py`'s own `build_config`): `_open_connect_peers` (below)
    dials every `config.connect` peer regardless of `listen`, so a
    manager with `listen=False` has to still be able to reach one --
    dialled here at a second, ordinary manager that is listening, since
    one with `listen=False` has nothing of its own to dial back into.
    """
    target_port = get_random_port()
    target = a_running_manager(a_manager, target_port)
    dialer = a_manager(connect=[f"127.0.0.1:{target_port}"], listen=False)
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
    dialer = a_manager(connect=[f"127.0.0.1:{target_port}"])
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


class _LoopStoppedError(Exception):
    """Raised by `run_a_manual_loop`'s sleep to end a loop that never ends."""


def run_a_manual_loop(
    loop: Callable[[], Coroutine[Any, Any, None]],
    manager: P2pManager,
    monkeypatch: pytest.MonkeyPatch,
    sleeps: int,
) -> tuple[list[tuple[str, int]], list[float]]:
    """Run one of the manual-peer loops until its `sleeps`-th sleep.

    Answer what it dialled, through `async_connect_host`, and how long
    each sleep was, in order.
    """
    dialled: list[tuple[str, int]] = []
    slept: list[float] = []

    async def record_dial(host: str, port: int) -> None:
        dialled.append((host, port))

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == sleeps:
            raise _LoopStoppedError

    monkeypatch.setattr(manager, "async_connect_host", record_dial)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(_LoopStoppedError):
        asyncio.run(loop())
    return dialled, slept


def test_the_connect_loop_dials_each_peer_every_pass(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1316: `ThreadOpenConnections`' `-connect` arm and its sleeps.

    After the n-th pass each address is followed by `min(n, 10)` sleeps
    of 500 ms, and the list by one more. Each spec names its own port,
    `8333`, distinct from regtest's own -- `_open_connect_peers` still
    dials with `node.chain.port` as `default_port` (ISS 1493: `dest`
    alone, not a re-derived pair, is what carries a spec's own port
    through to `addr_name`).
    """
    peers = ["1.2.3.4:8333", "peer.example:8333"]
    manager = a_manager(connect=peers)
    dialled, slept = run_a_manual_loop(
        manager._open_connect_peers, manager, monkeypatch, 3 * 12
    )
    assert dialled == [(spec, RegTest().port) for spec in peers] * 12
    steps = [0.5 * min(n, 10) for n in range(12)]
    assert slept == [x for step in steps for x in (step, step, 0.5)]


def test_add_added_peer_grows_the_list_once(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1350: `add_added_peer` appends, and refuses the same string twice."""
    manager = a_manager()
    assert manager.add_added_peer("1.2.3.4:9999") is True
    assert manager._added_peers == {"1.2.3.4:9999": None}
    assert manager.add_added_peer("1.2.3.4:9999") is False
    assert manager._added_peers == {"1.2.3.4:9999": None}


def test_add_added_peer_refuses_the_same_resolved_literal(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1350: two literal spellings of one endpoint are one entry.

    Core's own `AddNode` refuses a second literal address that resolves
    (`LookupNumeric`) to the one an existing entry already does; a name
    is compared as text alone, `_resolved_literal` answering `None` for
    one.
    """
    manager = a_manager()
    assert manager.add_added_peer("1.2.3.4") is True
    assert manager.add_added_peer(f"1.2.3.4:{RegTest().port}") is False
    assert manager.add_added_peer("1.2.3.4:9999") is True
    assert manager.add_added_peer("example.com") is True
    assert manager.add_added_peer("example.com:9999") is True
    assert set(manager._added_peers) == {
        "1.2.3.4",
        "1.2.3.4:9999",
        "example.com",
        "example.com:9999",
    }


def test_add_added_peer_accepts_a_value_with_an_out_of_range_port(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1350: `split_host_port`'s own refusal does not reach `AddNode`.

    Core's `LookupNumeric` never raises on an unparsable spec, only
    answers an invalid `CService`; `_resolved_literal` reads
    `split_host_port`'s `ValueError` the same way, so `add_added_peer`
    still adds the value, matching Core's own permissive `AddNode`.
    """
    manager = a_manager()
    assert manager.add_added_peer("1.2.3.4:99999") is True
    assert manager._added_peers == {"1.2.3.4:99999": None}


def test_the_added_loop_logs_an_unparsable_entry_and_dials_the_rest(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1350: a value `split_host_port` refuses is `tried`, not dialled.

    Reachable only through `add_added_peer`, `-addnode` itself being
    validated at startup. Core's own dial of such a value never
    connects either, so no `(host, port)` here is dialled for it -- but
    Core's own loop still marks `tried` and spends this pass's 500ms
    step on it before `OpenNetworkConnection` ever resolves its
    `pszDest` (`ThreadOpenAddedConnections`, `src/net.cpp`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), so it must not be
    filtered out of the pass entirely: doing so answers `tried` wrongly
    for an all-malformed list, this test's sibling below
    (btclib-org/btclib-node#1350).
    """
    logged, info = log_recorder()
    manager = a_manager(addnode_args=["1.2.3.4:99999", "5.6.7.8:8333"])
    monkeypatch.setattr(manager.logger, "info", info)
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 2
    )
    assert dialled == [("5.6.7.8:8333", RegTest().port)]
    assert slept == [0.5, 0.5]
    assert "Dial to 1.2.3.4:99999 did not come up" in logged


def test_the_added_loop_retries_an_all_malformed_list_at_the_tried_interval(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1350: an all-malformed list still waits `_ADDNODE_RETRY_TRIED`.

    Not `_ADDNODE_RETRY_IDLE`: Core's own loop marks `tried` on every
    `vInfo` entry a free grant reaches, whether or not it ever resolves,
    so a list of nothing but malformed values is `tried` every pass, the
    same as a list that dialled for real.
    """
    manager = a_manager(addnode_args=["1.2.3.4:99999"])
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 2
    )
    assert dialled == []
    assert slept == [0.5, 60]


def test_remove_added_peer_matches_the_exact_string_alone(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1350: `remove_added_peer` is Core's `RemoveAddedNode`, by text."""
    manager = a_manager(addnode_args=["1.2.3.4:9999"])
    assert manager.remove_added_peer("1.2.3.4") is False
    assert manager._added_peers == {"1.2.3.4:9999": None}
    assert manager.remove_added_peer("1.2.3.4:9999") is True
    assert manager._added_peers == {}
    assert manager.remove_added_peer("1.2.3.4:9999") is False


def test_add_added_peer_is_picked_up_by_the_dial_loop(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1350: a peer `add_added_peer` grows the list with gets dialled.

    Built with no `-addnode` at all, so the only way `_open_added_peers`
    ever sees this peer is through the mutation itself. ISS 1493: the
    dial keeps the raw spec, port included, as `dest`.
    """
    manager = a_manager()
    manager.add_added_peer("1.2.3.4:9999")
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 1
    )
    assert dialled == [("1.2.3.4:9999", RegTest().port)]
    assert slept == [0.5]


def test_remove_added_peer_stops_the_dial_loop_from_finding_it(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1350: a peer `remove_added_peer` drops is no longer dialled."""
    manager = a_manager(addnode_args=["1.2.3.4:9999"])
    manager.remove_added_peer("1.2.3.4:9999")
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 1
    )
    assert dialled == []
    assert slept == [2]


def test_the_added_loop_dials_the_peers_not_held(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1316: `ThreadOpenAddedConnections`, 500 ms apart, then 60 s.

    "Held" is read off `addr_name` for a name, as `async_connect_host`'s
    own `AlreadyConnectedToHost` check is, not off the address: a peer
    given by name is not necessarily connected on the endpoint its name
    last resolved to (btclib-org/btclib-node#1264). `peers[0]` is a
    literal IP, held by its own resolved `address` instead, Core's own
    `mapConnected` arm (btclib-org/btclib-node#1498) -- `addr_name` is
    set too, matching what a real dial through this same code would
    leave, but is not what the match is against here.
    """
    peers = ["1.2.3.4:8333", "5.6.7.8:8333", "peer.example:8333"]
    held = a_conn(1, address=peer_address("1.2.3.4", 8333), addr_name=peers[0])
    manager = a_manager([held], addnode_args=peers)
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 6
    )
    assert dialled == [(spec, RegTest().port) for spec in peers[1:]] * 2
    assert slept == [0.5, 0.5, 60] * 2


def test_the_added_loop_waits_two_seconds_with_nothing_to_dial(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1316: a round that tried nothing sleeps `2s`, not `60s`.

    `1.2.3.4:8333` is a literal IP, held by its own resolved `address`
    (btclib-org/btclib-node#1498) -- `addr_name` is set too, matching
    what a real dial through this same code would leave, but is not
    what the match is against here.
    """
    held = a_conn(1, address=peer_address("1.2.3.4", 8333), addr_name="1.2.3.4:8333")
    manager = a_manager([held], addnode_args=["1.2.3.4:8333"])
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 2
    )
    assert dialled == []
    assert slept == [2, 2]


def test_the_added_loop_with_no_addnode_still_loops_forever(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `-addnode` at all: the loop still sleeps 2s rather than returning.

    Core's own `ThreadOpenAddedConnections` never returns early on an
    empty `GetAddedNodeInfo`, since `AddNode` can grow the list at any
    later time; `add_added_peer` is this node's own equivalent
    (btclib-org/btclib-node#1350).
    """
    manager = a_manager()
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 2
    )
    assert dialled == []
    assert slept == [2, 2]


def test_the_added_loop_stops_where_no_addnode_grant_is_free(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1316: `MAX_ADDNODE_CONNECTIONS` added peers held take every grant.

    Every spec here is a literal IP, so each is held by its own held
    connection's resolved `address`, Core's own `mapConnected` arm
    (btclib-org/btclib-node#1498) -- `addr_name` is set too, matching
    what a real dial through this same code would leave, but is not
    what the match is against here.
    """
    peers = [f"10.0.0.{i}:8333" for i in range(1, 10)]
    conns = [
        a_conn(i, address=peer_address(f"10.0.0.{i + 1}", 8333), addr_name=spec)
        for i, spec in enumerate(peers[:8])
    ]
    manager = a_manager(conns, addnode_args=peers)
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 1
    )
    assert dialled == []
    assert slept == [2]


@pytest.mark.parametrize("option", ["connect", "addnode"])
def test_a_manual_dial_that_raises_is_logged_and_the_loop_goes_on(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    """ISS 1316: an exception out of one dial is logged, not the loop's end."""
    logged: list[str] = []
    peers = ["1.2.3.4:8333"]
    manager = (
        a_manager(connect=peers)
        if option == "connect"
        else a_manager(addnode_args=peers)
    )
    monkeypatch.setattr(manager.logger, "exception", logged.append)

    async def raises(host: str, port: int) -> NoReturn:
        raise RuntimeError(host)

    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            raise _LoopStoppedError

    monkeypatch.setattr(manager, "async_connect_host", raises)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    loop = (
        manager._open_connect_peers
        if option == "connect"
        else manager._open_added_peers
    )
    with pytest.raises(_LoopStoppedError):
        asyncio.run(loop())
    assert len(logged) >= 2


def test_with_no_connect_peers_the_connect_loop_returns_at_once(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `-connect`: `_open_connect_peers` returns rather than sleeping.

    `_open_added_peers` has no such early return -- Core's own
    `ThreadOpenAddedConnections` loops forever regardless, since
    `add_added_peer` can fill an initially empty list at any later time
    (btclib-org/btclib-node#1350);
    `test_the_added_loop_with_no_addnode_still_loops_forever` is that
    loop's own empty-list case, run through `run_a_manual_loop` rather
    than let run free.
    """
    manager = a_manager()
    monkeypatch.setattr(manager, "async_connect_host", refuses_to_be_asked)
    asyncio.run(manager._open_connect_peers())


def test_open_connect_peers_resolves_a_hostname(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1264: a `-connect` peer given by name is resolved here too.

    Building the manager at all is already most of the regression test:
    before that issue, `Config`, and then `_connect_peers`'s own
    `peer_address` call, each raised on a hostname before a dial was
    ever attempted.

    ISS 1493: the spec names a port, `8333`, distinct from regtest's own
    -- `addr_name` keeps it, `dest` verbatim rather than the bare host
    `test_open_connect_peers_keeps_a_portless_hostname_without_one`
    (below) proves for a spec naming none.
    """
    ours, theirs = socket.socketpair()

    async def connects(address: NetworkAddressV2) -> socket.socket:
        return ours

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["5.6.7.8"]))
    monkeypatch.setattr(manager_module, "dial", connects)
    made: list[dict[str, Any]] = []
    manager = a_manager(connect=["peer.example:8333"])
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )

    async def stop_after_one_sleep(seconds: float) -> NoReturn:
        raise _LoopStoppedError

    monkeypatch.setattr(asyncio, "sleep", stop_after_one_sleep)
    with pytest.raises(_LoopStoppedError):
        asyncio.run(manager._open_connect_peers())
    assert made == [
        {"inbound": False, "addr_fetch": False, "addr_name": "peer.example:8333"}
    ]
    theirs.close()


def test_open_connect_peers_keeps_a_portless_hostname_without_one(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1493: a `-connect` spec naming no port has none on `addr_name`.

    The negative half of `test_open_connect_peers_resolves_a_hostname`:
    `dest` is `addr_name` verbatim either way, so a spec that never
    named a port does not gain the chain's own default one.
    """
    ours, theirs = socket.socketpair()

    async def connects(address: NetworkAddressV2) -> socket.socket:
        return ours

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["5.6.7.8"]))
    monkeypatch.setattr(manager_module, "dial", connects)
    made: list[dict[str, Any]] = []
    manager = a_manager(connect=["peer.example"])
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )

    async def stop_after_one_sleep(seconds: float) -> NoReturn:
        raise _LoopStoppedError

    monkeypatch.setattr(asyncio, "sleep", stop_after_one_sleep)
    with pytest.raises(_LoopStoppedError):
        asyncio.run(manager._open_connect_peers())
    assert made == [
        {"inbound": False, "addr_fetch": False, "addr_name": "peer.example"}
    ]
    theirs.close()


def test_open_added_peers_resolves_a_hostname(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1301: an `-addnode` peer given by name names its connection too.

    `run_a_manual_loop` (above) proves `_open_added_peers`' own dial
    loop -- which `(host, port)` pairs it reaches and its retry timing
    -- by mocking `async_connect_host` itself, so none of its own tests
    exercise `async_connect_host`'s real body. This one does not mock
    it, the way `test_open_connect_peers_resolves_a_hostname` already
    does for `-connect`: `create_connection` is reached for real, so
    the `addr_name` it is given -- `node_str`, unresolved -- is proved
    to survive `_open_added_peers`' own `split_host_port` and
    `_open_manual` in between, not only `async_connect_host`'s own.

    This spec names no port; `test_open_added_peers_keeps_a_port_when_given`
    (below) is ISS 1493's own positive, a spec that names one.
    """
    ours, theirs = socket.socketpair()

    async def connects(address: NetworkAddressV2) -> socket.socket:
        return ours

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["5.6.7.8"]))
    monkeypatch.setattr(manager_module, "dial", connects)
    made: list[dict[str, Any]] = []
    manager = a_manager(addnode_args=["peer.example"])
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )

    async def stop_after_one_sleep(seconds: float) -> NoReturn:
        raise _LoopStoppedError

    monkeypatch.setattr(asyncio, "sleep", stop_after_one_sleep)
    with pytest.raises(_LoopStoppedError):
        asyncio.run(manager._open_added_peers())
    assert made == [
        {"inbound": False, "addr_fetch": False, "addr_name": "peer.example"}
    ]
    theirs.close()


def test_open_added_peers_keeps_a_port_when_given(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1493: an `-addnode` spec naming a port keeps it on `addr_name`.

    `addnode_args` already kept the raw spec before ISS 1493 (ISS 1224);
    what is new is `addr_name` reflecting it end to end, port included.
    """
    ours, theirs = socket.socketpair()

    async def connects(address: NetworkAddressV2) -> socket.socket:
        return ours

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["5.6.7.8"]))
    monkeypatch.setattr(manager_module, "dial", connects)
    made: list[dict[str, Any]] = []
    manager = a_manager(addnode_args=["peer.example:9999"])
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )

    async def stop_after_one_sleep(seconds: float) -> NoReturn:
        raise _LoopStoppedError

    monkeypatch.setattr(asyncio, "sleep", stop_after_one_sleep)
    with pytest.raises(_LoopStoppedError):
        asyncio.run(manager._open_added_peers())
    assert made == [
        {"inbound": False, "addr_fetch": False, "addr_name": "peer.example:9999"}
    ]
    theirs.close()


def test_added_held_counts_a_literal_ip_by_its_resolved_address(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1498: `_added_held`'s own count uses the literal/name split too.

    `held`'s `addr_name` is deliberately a different port than its own
    `address`, so a match through `addr_name` alone would miss it --
    `_added_held` still counts it, through `_held_resolved_addresses`.
    """
    port = RegTest().port
    held = a_conn(
        1, address=peer_address("1.2.3.4", port), addr_name=f"1.2.3.4:{port + 1}"
    )
    manager = a_manager([held], addnode_args=["1.2.3.4"])
    assert manager._added_held() == 1


def test_the_added_loop_skips_a_literal_ip_held_by_a_different_route(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1498: a literal-IP `-addnode` is held by its resolved address.

    ISS 1493's own regression: `held` was dialled by name (a `-connect`
    or `onetry` spec naming a port), so its own `addr_name` is
    `"1.2.3.4:<port>"`, never equal to the bare `-addnode=1.2.3.4`
    spec's own raw string -- the check that regressed. Core's own
    `mapConnected` (`GetAddedNodeInfo`, `src/net.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) keys on the resolved
    address instead, whatever route opened the connection, which is
    what this is held by here.
    """
    port = RegTest().port
    held = a_conn(1, address=peer_address("1.2.3.4", port), addr_name=f"1.2.3.4:{port}")
    manager = a_manager([held], addnode_args=["1.2.3.4"])
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 1
    )
    assert dialled == []
    assert slept == [2]


def test_the_added_loop_skips_a_literal_ip_with_its_own_port_held(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1498: a literal spec naming a port is held at that exact port.

    `held`'s own `addr_name` names a different host entirely, proving
    the match is against the resolved address and not a coincidence of
    `addr_name` text.
    """
    held = a_conn(
        1, address=peer_address("1.2.3.4", 9999), addr_name="unrelated.example"
    )
    manager = a_manager([held], addnode_args=["1.2.3.4:9999"])
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 1
    )
    assert dialled == []
    assert slept == [2]


def test_the_added_loop_dials_a_literal_ip_held_at_a_different_port(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1498: the same IP held at a different port is not a match.

    Core's own `mapConnected` keys on the whole resolved `CService`,
    address and port together, not the address alone.
    """
    held = a_conn(1, address=peer_address("1.2.3.4", 9999), addr_name="1.2.3.4:9999")
    manager = a_manager([held], addnode_args=["1.2.3.4:8888"])
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 1
    )
    assert dialled == [("1.2.3.4:8888", RegTest().port)]
    assert slept == [0.5]


def test_the_added_loop_dials_a_name_not_matched_by_resolved_address(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1498: a name spec is held by `addr_name` alone, never by address.

    `held`'s own resolved address coincides with where `peer.example`
    would dial, and its `addr_name` does not match the spec: Core's own
    `mapConnectedByName` arm never consults `mapConnected` for a name
    (`GetAddedNodeInfo`, `src/net.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag), so this is dialled rather than skipped.
    """
    held = a_conn(
        1, address=peer_address("1.2.3.4", RegTest().port), addr_name="other.example"
    )
    manager = a_manager([held], addnode_args=["peer.example"])
    dialled, slept = run_a_manual_loop(
        manager._open_added_peers, manager, monkeypatch, 1
    )
    assert dialled == [("peer.example", RegTest().port)]
    assert slept == [0.5]


def test_async_connect_host_skips_a_resolved_address_already_held(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AlreadyConnectedToAddressPort` still holds, ISS 1498 untouched.

    `async_connect_host`'s own inner resolved-address check keys on
    `endpoint_key`, `PeerDB`'s own address identity, regardless of
    `addr_name` -- unlike `_added_held`/`_open_added_peers`'s own
    literal/name split above, this one path is not changed by ISS 1498.
    """
    logged, info = log_recorder()

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["5.6.7.8"]))
    monkeypatch.setattr(manager_module, "dial", refuses_to_be_asked)
    held = a_conn(1, address=peer_address("5.6.7.8", 18444))
    manager = a_manager([held])
    monkeypatch.setattr(manager.logger, "info", info)
    asyncio.run(manager.async_connect_host("peer.example", 18444))
    assert logged == [
        "Not opening a connection to peer.example, already connected to 5.6.7.8:18444"
    ]


def test_async_connect_host_logs_when_no_candidate_comes_up(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1264: a manual dial that resolves but never connects is logged.

    `addr_fetch` defaults to `False`, so the log line runs -- unlike
    every `_process_addr_fetch` test, which always passes `addr_fetch=True`
    and so never reaches it (`ADDR_FETCH` giving up quietly, its own
    docstring). ISS 1493: `dest` names no port, so none is on the line
    either -- `default_port` is a fallback for the resolve alone, never
    printed on its own;
    `test_async_connect_host_logs_the_port_when_dest_names_one` (below)
    is the positive, a `dest` that names one.
    """
    logged, info = log_recorder()

    async def never_connects(address: NetworkAddressV2) -> None:
        return None

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["1.2.3.4"]))
    monkeypatch.setattr(manager_module, "dial", never_connects)
    manager = a_manager()
    monkeypatch.setattr(manager.logger, "info", info)
    asyncio.run(manager.async_connect_host("peer.example", 18444))
    assert logged == ["Dial to peer.example did not come up"]


def test_async_connect_host_logs_the_port_when_dest_names_one(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1493: a `dest` naming its own port keeps it on the give-up line."""
    logged, info = log_recorder()

    async def never_connects(address: NetworkAddressV2) -> None:
        return None

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NamedLoop(["1.2.3.4"]))
    monkeypatch.setattr(manager_module, "dial", never_connects)
    manager = a_manager()
    monkeypatch.setattr(manager.logger, "info", info)
    asyncio.run(manager.async_connect_host("peer.example:9999", 18444))
    assert logged == ["Dial to peer.example:9999 did not come up"]


def test_async_connect_host_caps_the_resolved_list_at_256_before_dialling(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1466: an answer past `Lookup`'s own 256th is never even seen.

    257 answers, the last one invalid (documentation-range, RFC3849 --
    not internal, so the cap alone is what has to drop it, `is_internal`
    having nothing to say about it), the first 256 valid: uncapped, the
    first pass above would walk as far as that 257th, invalid one and
    abort the whole attempt, dialling nothing, same as the
    invalid-candidate test above. `_MAX_RESOLVED_ADDRESSES` drops it
    before that pass ever runs, so the first pass sees only the 256
    valid answers, clears them, and the second pass dials the first
    (`src/net.cpp:413`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    ours, theirs = socket.socketpair()

    async def connects(address: NetworkAddressV2) -> socket.socket:
        return ours

    valid_answers = [f"10.0.0.{i}" for i in range(256)]
    monkeypatch.setattr(secrets, "SystemRandom", _NoShuffle)
    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: _NamedLoop([*valid_answers, "2001:db8::1"]),
    )
    monkeypatch.setattr(manager_module, "dial", connects)
    made: list[dict[str, Any]] = []
    manager = a_manager()
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )
    asyncio.run(manager.async_connect_host("seed.example", 18444))
    assert made == [
        {
            "inbound": False,
            "addr_fetch": False,
            "addr_name": "seed.example",
        }
    ]
    theirs.close()


def test_async_connect_host_drops_an_internal_answer_before_counting_to_256(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1466: an internal answer never takes one of the 256 slots.

    `LookupIntern` drops an `IsInternal` answer at collection time and
    never counts it toward `nMaxSolutions` (`src/netbase.cpp:144-168`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag). 257 raw answers: one
    internal, then 256 valid -- `dial` connects on the last of those 256
    alone, refusing every one ahead of it, so a dial only ever reaches
    it by trying every other valid candidate first and finding none of
    them connect, the real loop below and not a shortcut through it.

    Capping the raw list before filtering, rather than after, drops
    that last valid answer instead of the internal one -- it is the
    257th raw entry, one past a 256-wide cap taken before the internal
    one is removed from the count -- leaving 255 valid candidates, all
    of which `dial` refuses, so nothing connects. Filtering first
    leaves the internal one out of the count instead, and all 256 valid
    candidates, that last one included, get their turn.
    """
    ours, theirs = socket.socketpair()
    port = 18444
    valid_answers = [f"10.0.{i // 256}.{i % 256}" for i in range(256)]
    survivor = peer_address(valid_answers[-1], port)
    tried: list[NetworkAddressV2] = []

    async def connects(address: NetworkAddressV2) -> socket.socket | None:
        tried.append(address)
        if address == survivor:
            return ours
        return None

    monkeypatch.setattr(secrets, "SystemRandom", _NoShuffle)
    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: _NamedLoop(["fd6b:88c0:8724::1", *valid_answers]),
    )
    monkeypatch.setattr(manager_module, "dial", connects)
    made: list[dict[str, Any]] = []
    manager = a_manager()
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )
    asyncio.run(manager.async_connect_host("seed.example", port))
    assert tried == [peer_address(ip, port) for ip in valid_answers]
    assert made == [
        {
            "inbound": False,
            "addr_fetch": False,
            "addr_name": "seed.example",
        }
    ]
    theirs.close()


def test_connect_host_schedules_a_dial_on_this_manager_s_own_loop(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1264: `connect_host`, the `addnode` RPC's own `onetry`/`add` route.

    `run_coroutine_threadsafe(self.async_connect_host(host, port), ...)`,
    the one path a stubbed `p2p_manager` in `rpc/callbacks_test.py` never
    exercises for real.
    """
    target_port = get_random_port()
    target = a_running_manager(a_manager, target_port)
    dialer = a_manager()
    try:
        wait_until_listening(target)
        dialer.start()
        wait_until(dialer.loop.is_running)
        dialer.connect_host("127.0.0.1", target_port)
        wait_until(lambda: dialer.pending_connections)
        wait_until(lambda: target.pending_connections)
    finally:
        dialer.stop()
        dialer.join(timeout=10)
        target.stop()
        target.join(timeout=10)


def test_run_dials_a_connect_peer_without_an_explicit_dial(
    a_manager: AManagerFactory,
) -> None:
    """The `-connect` loop `run` starts reaches the peer -- issues #651, #1316.

    Nothing here ever calls `dialer.connect(...)`: `_open_connect_peers`
    is what has to reach `target` for this to pass.
    """
    target_port = get_random_port()
    target = a_running_manager(a_manager, target_port)
    dialer = a_manager(connect=[f"127.0.0.1:{target_port}"])
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


def test_run_dials_an_added_peer_without_an_explicit_dial(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1316: the `-addnode` loop `run` starts reaches the peer."""
    target_port = get_random_port()
    target = a_running_manager(a_manager, target_port)
    dialer = a_manager(addnode_args=[f"127.0.0.1:{target_port}"])
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


def test_run_dials_an_addr_fetch_peer_without_manage_connections(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1366: `run` starts `_open_addr_fetches`, not `manage_connections`.

    Queued before `start()`, since nothing else feeds `_addr_fetches`
    here -- no DNS seed, no `-seednode`.
    """
    target_port = get_random_port()
    target = a_running_manager(a_manager, target_port)
    dialer = a_manager()
    dialer._addr_fetches.append(("127.0.0.1", target_port))
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


def automatic_conns(full_relay: int, block_relay: int) -> list[Any]:
    """Build that many full-relay and block-relay-only automatic peers."""
    return [a_conn(i, automatic=True) for i in range(full_relay)] + [
        a_conn(full_relay + i, automatic=True, block_relay=True)
        for i in range(block_relay)
    ]


@pytest.mark.parametrize("status", list(NodeStatus))
@pytest.mark.parametrize(
    ("full_relay", "block_relay", "dials"), [(7, 2, True), (8, 1, True), (8, 2, False)]
)
def test_eight_and_two_automatic_peers_are_the_target_however_far_the_sync_is(
    a_manager: AManagerFactory,
    status: NodeStatus,
    full_relay: int,
    block_relay: int,
    *,
    dials: bool,
) -> None:
    """ISS 1073, 1095: Core's two targets, eight and two, from the first pass.

    `m_max_outbound_full_relay` and `m_max_outbound_block_relay`: one
    short of either leaves room whatever `node.status` says, headers
    unsynced included, and both met fill it. The draw being asked for,
    or not, is the assertion.
    """
    drawn: list[None] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn.append(None))
    conns = automatic_conns(full_relay, block_relay)
    manager = a_manager(conns, peer_db=peer_db, status=status)
    asyncio.run(manager._maybe_dial_more_peers())
    assert bool(drawn) is dials


@pytest.mark.parametrize(
    ("full_relay", "block_relay", "kind"),
    [(0, 0, False), (7, 0, False), (7, 2, False), (8, 0, True), (8, 1, True)],
)
def test_block_relay_only_peers_are_dialled_once_full_relay_ones_are_met(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    full_relay: int,
    block_relay: int,
    *,
    kind: bool,
) -> None:
    """ISS 1095: `ThreadOpenConnections`' order, full-relay first.

    The dial is block-relay-only once eight full-relay peers are held,
    pending or not, and full-relay until then, whatever is held of the
    other kind. What `create_connection` is handed is the assertion.
    """
    ours, theirs = socket.socketpair()

    async def answers(address: NetworkAddressV2) -> socket.socket:
        return ours

    made: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module, "dial", answers)
    peer_db = a_peer_db_stub(
        is_empty=False, random_address=lambda: a_full_node("5.6.7.8", 18444)
    )
    conns = automatic_conns(full_relay, block_relay)
    manager = a_manager(peer_db=peer_db)
    for conn in conns:
        # distinct groups, so that the draw is refused for nothing else
        conn.address = peer_address(f"10.{conn.id}.0.1", 18444)
        manager.pending_connections[conn.id] = conn
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )
    with ours, theirs:
        asyncio.run(manager._maybe_dial_more_peers())
    assert made == [
        {"inbound": False, "automatic": True, "block_relay": kind, "feeler": False}
    ]


@pytest.mark.parametrize(
    ("started", "due", "dials"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_an_extra_block_relay_only_peer_waits_for_the_start_and_the_timer(
    a_manager: AManagerFactory, *, started: bool, due: bool, dials: bool
) -> None:
    """ISS 1095: past both targets, one more once the timer comes due.

    Core's `m_start_extra_block_relay_peers` and `next_extra_block_relay`
    both have to allow it, and picking it draws the timer again, so a
    second pass straight after dials nothing.
    """
    drawn: list[None] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn.append(None))
    manager = a_manager(automatic_conns(8, 2), peer_db=peer_db)
    manager.start_extra_block_relay_peers = started
    manager._next_extra_block_relay = time.time() + (-1 if due else 60)
    asyncio.run(manager._maybe_dial_more_peers())
    assert bool(drawn) is dials
    drawn.clear()
    asyncio.run(manager._maybe_dial_more_peers())
    assert not drawn


def test_the_extra_block_relay_only_timer_is_drawn_as_the_manager_runs(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` draws Core's `next_extra_block_relay` off its own start."""
    monkeypatch.setattr(manager_module, "_exponential_delay", lambda mean: mean)
    manager = a_manager(listen=False, max_connections=0)
    assert manager._next_extra_block_relay == math.inf
    before = time.time()
    manager.start()
    wait_until(manager.loop.is_running)
    assert before + 300 <= manager._next_extra_block_relay <= time.time() + 300


@pytest.mark.parametrize(
    ("max_connections", "full_relay", "block_relay", "dials"),
    [(8, 7, 0, True), (8, 8, 0, False), (9, 8, 0, True), (9, 8, 1, False)],
)
def test_the_outbound_grants_are_core_s_semaphore(
    a_manager: AManagerFactory,
    max_connections: int,
    full_relay: int,
    block_relay: int,
    *,
    dials: bool,
) -> None:
    """ISS 1095: `semOutbound`, `min(automatic outbound, -maxconnections)`.

    At `-maxconnections=8` the eight full-relay peers take every grant
    and no block-relay-only peer is dialled; at nine one is, and then
    nothing more, not even the extra one.
    """
    drawn: list[None] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn.append(None))
    manager = a_manager(
        automatic_conns(full_relay, block_relay),
        peer_db=peer_db,
        max_connections=max_connections,
    )
    manager.start_extra_block_relay_peers = True
    manager._next_extra_block_relay = 0
    asyncio.run(manager._maybe_dial_more_peers())
    assert bool(drawn) is dials


def test_an_exponential_delay_has_the_mean_it_is_given() -> None:
    """Core's `rand_exp_duration`: positive draws, averaging the mean."""
    draws = [manager_module._exponential_delay(300) for _ in range(20_000)]
    assert min(draws) > 0
    assert 280 < sum(draws) / len(draws) < 320


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


def test_eight_and_two_automatic_peers_fill_the_target(
    a_manager: AManagerFactory,
) -> None:
    """The control for the test above: ten dialled automatically fill it."""
    drawn: list[None] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn.append(None))
    conns = automatic_conns(8, 2)
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
        random_address=lambda: a_full_node("5.6.7.8", 18444),
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


@pytest.mark.parametrize("block_relay", [True, False])
def test_create_connection_marks_a_block_relay_only_connection(
    a_manager: AManagerFactory, *, block_relay: bool
) -> None:
    """ISS 1095: the kind is on the connection before its task first runs.

    `own_version` reads it for the relay flag, so it is set before the
    task is scheduled.
    """
    manager = a_manager()
    ours, theirs = socket.socketpair()
    address = peer_address("1.2.3.4", 18444)

    async def create() -> None:
        manager.create_connection(
            ours, address, inbound=False, automatic=True, block_relay=block_relay
        )
        (conn,) = manager.pending_connections.values()
        assert conn.block_relay is block_relay
        assert conn.automatic
        assert conn.task is not None
        conn.task.cancel()
        await asyncio.sleep(0)

    with ours, theirs:
        manager.loop.run_until_complete(create())


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
            # and nothing is sent to an inbound peer ahead of its own
            # `version`, as Core sends none (ISS 1207)
            peer.settimeout(0.5)
            with pytest.raises(TimeoutError):
                peer.recv(4096)
    finally:
        manager.stop()
        manager.join(timeout=10)
    assert not manager.is_alive()


def test_a_manager_accepts_an_ipv6_peer_too(  # pragma: no cover -- the body needs IPv6
    a_manager: AManagerFactory,
) -> None:
    """A manager also binds IPv6, accepting a peer that dials it over `::1`."""
    port = get_random_port()
    manager = a_running_manager(a_manager, port)
    wait_until_listening(manager)
    try:
        peer = socket.create_connection(("::1", port), timeout=20)
    except OSError as refused:
        manager.stop()
        manager.join(timeout=10)
        pytest.skip(f"this host has no IPv6: {refused}")
    # held open across the stop rather than closed by a `with`, on
    # `test_stopping_a_running_manager_stops_the_connections_it_holds`'s
    # own reasoning: closing it here races the still-running
    # `Connection`'s own read against the `stop` below
    with closing(peer):
        wait_until(lambda: manager.pending_connections)
        (conn,) = manager.pending_connections.values()
        assert conn.address.network_id == BIP155Network.IPV6
        assert conn.address.port == peer.getsockname()[1]
        manager.stop()
        manager.join(timeout=10)


@pytest.mark.parametrize(
    (
        "max_connections",
        "max_inbound",
        "max_outbound_full_relay",
        "max_outbound_block_relay",
        "grant",
    ),
    [
        # Core's own default: eleven outbound slots reserved, the rest
        # inbound, eight of the eleven full-relay and two block-relay-only
        (DEFAULT_MAX_PEER_CONNECTIONS, 114, 8, 2, 11),
        # one past the reservation: a single inbound slot
        (12, 1, 8, 2, 11),
        # one block-relay-only slot left over by the full-relay eight
        (9, 0, 8, 1, 9),
        # under the reservation: no inbound slot, and the full-relay
        # target and the grant are the total itself
        (5, 0, 5, 0, 5),
        (0, 0, 0, 0, 0),
    ],
)
def test_max_connections_is_divided_the_way_core_divides_it(
    a_manager: AManagerFactory,
    *,
    max_connections: int,
    max_inbound: int,
    max_outbound_full_relay: int,
    max_outbound_block_relay: int,
    grant: int,
) -> None:
    """`CConnman::Init`'s division, and the size of `semOutbound`."""
    manager = a_manager(max_connections=max_connections)
    assert manager.max_inbound == max_inbound
    assert manager.max_outbound_full_relay == max_outbound_full_relay
    assert manager.max_outbound_block_relay == max_outbound_block_relay
    assert manager.max_automatic_outbound == grant


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
    never reaches the line that sets it. Its thread ends instead, which
    `wait_until_listening` reports at once (btclib-org/btclib-node#1361).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("", 0))
        taken.listen()
        manager = a_manager(port=taken.getsockname()[1])
        manager.start()
        try:
            with pytest.raises(ListenerEndedError, match="ended without listening"):
                wait_until_listening(manager, timeout=10)
            assert not manager.listening.is_set()
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


def test_the_listening_sockets_are_kept_before_listening_is_set(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1325: a thread `listening` wakes finds `_server_sockets` filled."""
    manager = a_manager(port=get_random_port())
    seen: list[list[socket.socket]] = []

    class Recording(threading.Event):
        @override
        def set(self) -> None:
            seen.append(list(manager._server_sockets))
            super().set()

    manager.listening = Recording()
    try:
        assert manager.start_listener()
        assert seen
        assert seen[0]
    finally:
        manager.stop()
        manager.join(timeout=10)


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


FULL_NODE = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS


def a_feeler_manager(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    new: NetworkAddressV2 | None,
    conns: Sequence[Any] = (),
) -> tuple[P2pManager, list[dict[str, Any]]]:
    """Build a manager whose next dial is a feeler to `new`, recording it.

    Both targets are met by `conns`, or by eight and two peers where none
    are given, and the feeler timer is due. The draw without `new_only`
    refuses to be asked: a feeler draws from the new table alone.
    """
    ours, theirs = socket.socketpair()
    ours.close()
    theirs.close()

    async def answers(address: NetworkAddressV2) -> socket.socket:
        return ours

    made: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module, "dial", answers)
    monkeypatch.setattr(manager_module, "_FEELER_SLEEP_WINDOW", 0)
    peer_db = a_peer_db_stub(is_empty=False, random_new_address=lambda: new)
    manager = a_manager(peer_db=peer_db)
    for conn in conns or automatic_conns(8, 2):
        conn.address = conn.address if conns else peer_address(f"10.{conn.id}.0.1", 1)
        manager.connections[conn.id] = conn
    manager._next_feeler = 0
    monkeypatch.setattr(
        manager, "create_connection", lambda *args, **kwargs: made.append(kwargs)
    )
    return manager, made


def test_a_feeler_is_dialled_once_both_targets_are_met(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1096: `ConnectionType::FEELER`, off the new table, on its timer.

    Picking it draws the timer again, so a second pass dials nothing.
    """
    new = peer_address("5.6.7.8", 18444, services=FULL_NODE)
    manager, made = a_feeler_manager(a_manager, monkeypatch, new)
    asyncio.run(manager._maybe_dial_more_peers())
    assert made == [
        {"inbound": False, "automatic": True, "block_relay": False, "feeler": True}
    ]
    assert manager._next_feeler > time.time()
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(made) == 1


@pytest.mark.parametrize(
    ("services", "dials"),
    [
        (FULL_NODE, True),
        (ServiceFlags.NODE_NETWORK_LIMITED, True),
        (ServiceFlags.NODE_WITNESS, False),
        (0, False),
    ],
)
def test_a_feeler_wants_only_an_address_db(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    services: int,
    *,
    dials: bool,
) -> None:
    """Core's `MayHaveUsefulAddressDB`, not `HasAllDesirableServiceFlags`."""
    new = peer_address("5.6.7.8", 18444, services=services)
    manager, made = a_feeler_manager(a_manager, monkeypatch, new)
    asyncio.run(manager._maybe_dial_more_peers())
    assert bool(made) is dials


def test_a_feeler_draws_again_past_an_address_with_no_address_db(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `continue` on `MayHaveUsefulAddressDB`, inside the 100 tries.

    The first draw fails it and the second is dialled, in the same pass;
    a pass whose every draw fails it stops at `_MAX_DRAWS_PER_PASS`.
    """
    useless = peer_address("5.6.7.8", 18444, services=ServiceFlags.NODE_WITNESS)
    useful = peer_address("9.9.9.9", 18444, services=FULL_NODE)
    draws = iter([useless, useful])
    manager, _ = a_feeler_manager(a_manager, monkeypatch, None)
    manager.peer_db = a_peer_db_stub(
        is_empty=False, random_new_address=lambda: next(draws)
    )
    dialled: list[NetworkAddressV2] = []

    async def dial(address: NetworkAddressV2) -> None:
        dialled.append(address)

    monkeypatch.setattr(manager_module, "dial", dial)
    asyncio.run(manager._maybe_dial_more_peers())
    assert dialled == [useful]
    counted: list[NetworkAddressV2] = []

    def count() -> NetworkAddressV2:
        counted.append(useless)
        return useless

    manager.peer_db = a_peer_db_stub(is_empty=False, random_new_address=count)
    manager._next_feeler = 0
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(counted) == manager_module._MAX_DRAWS_PER_PASS
    assert dialled == [useful]


def test_a_feeler_is_held_to_no_network_group(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `!fFeeler &&` ahead of its network-group refusal."""
    conns = automatic_conns(8, 2)
    for conn in conns:
        conn.address = peer_address(f"5.6.{conn.id}.1", 1)
    new = peer_address("5.6.7.8", 18444, services=FULL_NODE)
    manager, made = a_feeler_manager(a_manager, monkeypatch, new, conns)
    asyncio.run(manager._maybe_dial_more_peers())
    assert made


@pytest.mark.parametrize("discouraged", [False, True])
def test_a_feeler_waits_a_uniform_second_before_its_dial(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch, *, discouraged: bool
) -> None:
    """Core's `FEELER_SLEEP_WINDOW`: up to a second, drawn ahead of the dial.

    Ahead of `OpenNetworkConnection`'s refusals too, which Core asks once
    the wait is over: a discouraged address is refused after it.
    """
    window = manager_module._FEELER_SLEEP_WINDOW
    new = peer_address("5.6.7.8", 18444, services=FULL_NODE)
    manager, _ = a_feeler_manager(a_manager, monkeypatch, new)
    monkeypatch.setattr(manager_module, "_FEELER_SLEEP_WINDOW", window)
    events: list[object] = []

    class Draws:
        def uniform(self, low: float, high: float) -> float:
            events.append((low, high))
            return 0.0

        def expovariate(self, rate: float) -> float:
            return 1 / rate

    async def dial(address: NetworkAddressV2) -> None:
        events.append(address)

    def is_discouraged(address: NetworkAddressV2) -> bool:
        events.append("asked")
        return discouraged

    monkeypatch.setattr(secrets, "SystemRandom", Draws)
    monkeypatch.setattr(manager_module, "dial", dial)
    monkeypatch.setattr(manager, "is_discouraged", is_discouraged)
    asyncio.run(manager._maybe_dial_more_peers())
    assert events == [(0, 1.0), "asked", *([] if discouraged else [new])]


def test_a_feeler_to_nothing_new_dials_nothing(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty new table is Core's invalid address: the pass ends."""
    manager, made = a_feeler_manager(a_manager, monkeypatch, None)
    asyncio.run(manager._maybe_dial_more_peers())
    assert not made


def test_a_feeler_waits_behind_both_targets_and_the_extra_peer(
    a_manager: AManagerFactory,
) -> None:
    """`ThreadOpenConnections`' order: a feeler comes after the other three."""
    manager = a_manager()
    manager._next_feeler = 0
    manager.start_extra_block_relay_peers = True
    manager._next_extra_block_relay = 0
    outbound = manager_module._Outbound
    assert manager._next_outbound(7, 2) is outbound.FULL_RELAY
    assert manager._next_outbound(8, 1) is outbound.BLOCK_RELAY
    assert manager._next_outbound(8, 2) is outbound.BLOCK_RELAY
    assert manager._next_outbound(8, 2) is outbound.FEELER
    assert manager._next_outbound(8, 2) is None


def test_a_feeler_counts_against_the_grants_and_no_target(
    a_manager: AManagerFactory,
) -> None:
    """A feeler holds a `semOutbound` grant, and is no full-relay peer.

    Seven full-relay peers and a feeler leave the full-relay target
    unmet; eight, two and a feeler take every grant at the default.
    """
    drawn: list[None] = []
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn.append(None))
    feeler = a_conn(20, automatic=True, feeler=True)
    manager = a_manager([*automatic_conns(7, 2), feeler], peer_db=peer_db)
    asyncio.run(manager._maybe_dial_more_peers())
    assert drawn
    drawn.clear()
    manager = a_manager([*automatic_conns(8, 2), feeler], peer_db=peer_db)
    manager._next_feeler = 0
    manager.start_extra_block_relay_peers = True
    manager._next_extra_block_relay = 0
    asyncio.run(manager._maybe_dial_more_peers())
    assert not drawn


def test_a_feeler_leaves_its_network_group_to_other_peers(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `case ConnectionType::FEELER: break`: a feeler adds no group."""
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)

    monkeypatch.setattr(manager_module, "dial", records)
    drawn = a_full_node("5.6.7.8", 18444)
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn)
    feeler = a_conn(
        1, automatic=True, feeler=True, address=peer_address("5.6.1.1", 18444)
    )
    manager = a_manager([feeler], peer_db=peer_db)
    asyncio.run(manager._maybe_dial_more_peers())
    assert dialled == [drawn]


def test_an_addr_fetch_connection_leaves_its_network_group_to_other_peers(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`IsOutboundOrBlockRelayConn` answers `false` for `ADDR_FETCH` too.

    Mirrors the feeler test above: an addr-fetch connection in the same
    network group as the draw does not hold that group against it.
    """
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)

    monkeypatch.setattr(manager_module, "dial", records)
    drawn = a_full_node("5.6.7.8", 18444)
    peer_db = a_peer_db_stub(is_empty=False, random_address=lambda: drawn)
    fetching = a_conn(1, addr_fetch=True, address=peer_address("5.6.1.1", 18444))
    manager = a_manager([fetching], peer_db=peer_db)
    asyncio.run(manager._maybe_dial_more_peers())
    assert dialled == [drawn]


def test_the_feeler_timer_is_drawn_as_the_manager_runs(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` draws Core's `next_feeler` off its own start, `FEELER_INTERVAL`."""
    monkeypatch.setattr(manager_module, "_exponential_delay", lambda mean: mean)
    manager = a_manager(listen=False, max_connections=0)
    assert manager._next_feeler == math.inf
    before = time.time()
    manager.start()
    wait_until(manager.loop.is_running)
    assert before + 120 <= manager._next_feeler <= time.time() + 120


def test_create_connection_marks_a_feeler(a_manager: AManagerFactory) -> None:
    """ISS 1096: the kind is on the connection before its task first runs."""
    manager = a_manager()
    ours, theirs = socket.socketpair()
    address = peer_address("1.2.3.4", 18444)

    async def create() -> None:
        manager.create_connection(
            ours, address, inbound=False, automatic=True, feeler=True
        )
        (conn,) = manager.pending_connections.values()
        assert conn.feeler
        assert not conn.block_relay
        assert conn.task is not None
        conn.task.cancel()
        await asyncio.sleep(0)

    with ours, theirs:
        manager.loop.run_until_complete(create())


ANCHOR = peer_address("5.6.7.8", 18444, services=FULL_NODE)


def test_an_anchor_comes_first_while_the_block_relay_only_target_is_unmet(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1097: Core's `anchor` arm, ahead of the full-relay target."""
    manager = a_manager()
    outbound = manager_module._Outbound
    manager.anchors = [ANCHOR]
    assert manager._next_outbound(0, 1) is outbound.ANCHOR
    assert manager._next_outbound(0, 2) is outbound.FULL_RELAY
    manager.anchors = []
    assert manager._next_outbound(8, 1) is outbound.BLOCK_RELAY


@pytest.mark.parametrize(
    "refused",
    [
        peer_address("5.6.7.9", 18444, services=ServiceFlags.NODE_NETWORK),
        peer_address("7.7.1.1", 18444, services=FULL_NODE),
        NetworkAddressV2(0, FULL_NODE, BIP155Network.TORV3, bytes(32), 8333),
        peer_address("0.0.0.0", 18444, services=FULL_NODE),  # noqa: S104
        peer_address("255.255.255.255", 18444, services=FULL_NODE),
        peer_address("::", 18444, services=FULL_NODE),
        peer_address("2001:db8::1", 18444, services=FULL_NODE),
    ],
    ids=[
        "services",
        "network-group",
        "undialable",
        "unspecified",
        "broadcast",
        "unspecified-ipv6",
        "documentation",
    ],
)
def test_an_anchor_is_popped_off_the_back_past_those_refused(
    a_manager: AManagerFactory, refused: NetworkAddressV2
) -> None:
    """Core's anchor loop: the back first, each refusal dropped for good.

    Short of `HasAllDesirableServiceFlags`, in a network group an outbound
    peer holds, on a network this node cannot dial, or refused by
    `CNetAddr::IsValid`: the unspecified and broadcast addresses and
    RFC3849's documentation range.
    """
    manager = a_manager()
    manager.anchors = [ANCHOR, refused]
    assert manager._pop_anchor({net_group(peer_address("7.7.2.2", 1))}) == ANCHOR
    assert manager.anchors == []


@pytest.mark.parametrize("port", [8333, 18444])
def test_an_anchor_that_is_this_node_s_own_is_passed_over(
    a_manager: AManagerFactory, port: int
) -> None:
    """ISS 1238: Core's anchor loop refuses `IsLocal(addr)` too.

    `IsLocal` compares the host alone, so an anchor at this node's own
    host on another port is refused the same way as any other.
    """
    manager = a_manager()
    own = peer_address("9.9.9.9", port, services=FULL_NODE)
    manager.local_addresses = frozenset({host_key(peer_address("9.9.9.9", 1))})
    manager.anchors = [ANCHOR, own]
    assert manager._pop_anchor({net_group(peer_address("7.7.2.2", 1))}) == ANCHOR
    assert manager.anchors == []


def test_an_anchor_short_of_any_left_draws_from_the_table(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's loop goes on to `addrman` with the anchors spent.

    The dial is still block-relay-only, and the refused anchor is gone.
    """
    drawn = a_full_node("9.9.9.9", 18444)
    manager = a_manager(peer_db=a_peer_db_stub(random_address=lambda: drawn))
    refused = peer_address("5.6.7.9", 18444)
    manager.anchors = [refused]
    made: list[tuple[NetworkAddressV2, dict[str, Any]]] = []

    async def answers(address: NetworkAddressV2) -> bool:
        return True

    monkeypatch.setattr(manager_module, "dial", answers)
    monkeypatch.setattr(
        manager,
        "create_connection",
        lambda sock, address, **kwargs: made.append((address, kwargs)),
    )
    asyncio.run(manager._dial_one_draw(set(), set(), manager_module._Outbound.ANCHOR))
    assert made == [
        (
            drawn,
            {"inbound": False, "automatic": True, "block_relay": True, "feeler": False},
        )
    ]
    assert manager.anchors == []


def test_an_anchor_is_dialled_block_relay_only_with_the_table_empty(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core tries its anchors ahead of `addrman`, whatever that holds."""
    ours, theirs = socket.socketpair()
    ours.close()
    theirs.close()

    async def answers(address: NetworkAddressV2) -> socket.socket:
        return ours

    made: list[tuple[NetworkAddressV2, dict[str, Any]]] = []
    monkeypatch.setattr(manager_module, "dial", answers)
    manager = a_manager()
    manager.anchors = [ANCHOR]
    monkeypatch.setattr(
        manager,
        "create_connection",
        lambda sock, address, **kwargs: made.append((address, kwargs)),
    )
    asyncio.run(manager._maybe_dial_more_peers())
    assert made == [
        (
            ANCHOR,
            {"inbound": False, "automatic": True, "block_relay": True, "feeler": False},
        )
    ]


def test_an_anchor_dial_records_its_try(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `ConnectNode` calls `Attempt` for an anchor's dial too.

    Recorded whether or not the connect answered, as for any other dial.
    """

    async def refuses(address: NetworkAddressV2) -> None:
        return None

    monkeypatch.setattr(manager_module, "dial", refuses)
    manager = a_manager(peer_db=a_peer_db_stub())
    manager.anchors = [ANCHOR]
    asyncio.run(manager._dial_one_draw(set(), set(), manager_module._Outbound.ANCHOR))
    assert list(tries_of(manager)) == [endpoint_key(ANCHOR)]


def an_anchors_file(manager: P2pManager, anchors: list[NetworkAddressV2]) -> Path:
    """Write `anchors` where `manager` reads them, and return the path."""
    path = manager._anchors_path
    dump_anchors(path, RegTest().magic, anchors)
    return path


@pytest.mark.parametrize("connect", [(), ("1.2.3.4:18444",)])
def test_the_anchors_are_read_as_the_manager_runs_and_the_file_goes(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    connect: Sequence[str],
) -> None:
    """`CConnman::Start` reads two at most, and none under `-connect`.

    The read logs `ReadAnchors`' line, of the three the file holds.
    """
    anchors = [peer_address(f"5.6.{i}.1", 18444, services=FULL_NODE) for i in range(3)]
    manager = a_manager(listen=False, max_connections=0, connect=connect)
    logged: list[str] = []
    monkeypatch.setattr(
        manager,
        "logger",
        SimpleNamespace(
            info=lambda fmt, *args: logged.append(fmt % args),
            debug=lambda *a: None,
            exception=lambda *a: None,
        ),
    )
    path = an_anchors_file(manager, anchors)
    manager.start()
    wait_until(manager.loop.is_running)
    assert manager.anchors == ([] if connect else anchors[:2])
    assert path.exists() is bool(connect)
    loaded = 'Loaded 3 addresses from "anchors.dat"'
    assert (loaded in logged) is not bool(connect)


def test_the_block_relay_only_peers_are_the_anchors_written_at_stop(
    a_manager: AManagerFactory,
) -> None:
    """`StopNodes`: the first two opened, pending ones included, as dialled."""
    conns = [
        a_conn(3, block_relay=True, address=peer_address("5.6.3.1", 1)),
        a_conn(1, address=peer_address("5.6.1.1", 1)),
        a_conn(2, block_relay=True, address=peer_address("5.6.2.1", 1)),
        a_conn(4, block_relay=True, address=peer_address("5.6.4.1", 1)),
    ]
    manager = a_manager(conns[:2], listen=False, max_connections=0)
    for conn in conns[2:]:
        manager.pending_connections[conn.id] = conn
    manager.start()
    wait_until(manager.loop.is_running)
    manager.stop()
    path = manager._anchors_path
    assert read_anchors(path, RegTest().magic) == [
        conns[2].address,
        conns[0].address,
    ]
    # once, as `StopNodes` clears `fAddressesInitialized` as it dumps
    manager._dump_anchors()
    assert not path.exists()


@pytest.mark.parametrize("started", [True, False])
def test_no_anchor_is_written_under_connect_or_short_of_the_start(
    a_manager: AManagerFactory, *, started: bool
) -> None:
    """`fAddressesInitialized` and `m_use_addrman_outgoing` both guard it."""
    conn = a_conn(1, block_relay=True, address=peer_address("5.6.1.1", 1))
    connect = ("1.2.3.4:18444",) if started else ()
    manager = a_manager([conn], listen=False, max_connections=0, connect=connect)
    if started:
        manager.start()
        wait_until(manager.loop.is_running)
    manager.stop()
    assert not manager._anchors_path.exists()


def test_an_anchors_file_that_cannot_be_written_is_logged(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SerializeFileDB` logs its failure, and the stop goes on."""
    logged: list[object] = []
    manager = a_manager(listen=False, max_connections=0)
    monkeypatch.setattr(
        manager,
        "logger",
        SimpleNamespace(
            info=lambda *a: None,
            debug=lambda *a: None,
            exception=lambda *a: logged.append(a),
        ),
    )

    def fails(*args: object) -> NoReturn:
        raise OSError

    monkeypatch.setattr(manager_module, "dump_anchors", fails)
    manager.start()
    wait_until(manager.loop.is_running)
    manager.stop()
    assert logged == [("Failed to write %s", "anchors.dat")]


def test_a_stale_tip_opens_one_full_relay_peer_past_the_target(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1100: `GetTryNewOutboundPeer()`, past both targets, then the rest."""
    manager = a_manager()
    outbound = manager_module._Outbound
    manager.try_new_outbound_peer = True
    manager._next_feeler = 0
    manager.start_extra_block_relay_peers = True
    manager._next_extra_block_relay = 0
    assert manager._next_outbound(8, 1) is outbound.BLOCK_RELAY
    assert manager._next_outbound(8, 2) is outbound.FULL_RELAY
    assert manager._next_outbound(9, 2) is outbound.FULL_RELAY
    manager.try_new_outbound_peer = False
    assert manager._next_outbound(9, 2) is outbound.BLOCK_RELAY


def a_network_manager(
    a_manager: AManagerFactory,
    conns: Sequence[Any],
    on: dict[Network, NetworkAddressV2],
    max_connections: int = DEFAULT_MAX_PEER_CONNECTIONS,
) -> P2pManager:
    """Build a manager whose table holds `on`, one address per network.

    `address_sampler` draws `on`'s address for the network it is given,
    and refuses to be asked for none.
    """

    def address_sampler(
        *, new_only: bool = False, network: Network | None = None
    ) -> Callable[[], NetworkAddressV2 | None]:
        assert network is not None
        assert not new_only
        return lambda: on.get(network)

    peer_db = a_peer_db_stub(
        is_empty=False,
        holds_network=lambda network_id: any(
            address.network_id == network_id for address in on.values()
        ),
        address_sampler=address_sampler,
    )
    manager = a_manager(conns, peer_db=peer_db, max_connections=max_connections)
    manager._next_extra_network_peer = 0
    return manager


V6 = a_full_node("2a00::1", 18444)


@pytest.mark.parametrize(
    ("full_relay", "max_connections", "dials"),
    [(8, DEFAULT_MAX_PEER_CONNECTIONS, True), (9, DEFAULT_MAX_PEER_CONNECTIONS, False)],
)
def test_an_extra_peer_is_dialled_for_a_network_no_peer_is_on(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    full_relay: int,
    max_connections: int,
    *,
    dials: bool,
) -> None:
    """ISS 1100: Core's `MaybePickPreferredNetwork`, at exactly eight peers.

    The eight are on IPv4 and the table holds IPv6: the timer is drawn
    again where the network is picked, and the dial is a full-relay one
    to the IPv6 address.
    """
    conns = [
        a_conn(i, automatic=True, address=peer_address(f"5.{i}.0.1", 1))
        for i in range(full_relay)
    ]
    manager = a_network_manager(a_manager, conns, {Network.IPV6: V6}, max_connections)
    expected = manager_module._Outbound.NETWORK if dials else None
    assert manager._next_outbound(full_relay, 2) is expected
    assert (manager._next_extra_network_peer > time.time()) is dials
    if dials:
        assert network_dials(manager, monkeypatch, set()) == [(V6, FULL_RELAY)]


FULL_RELAY = {
    "inbound": False,
    "automatic": True,
    "block_relay": False,
    "feeler": False,
}


def network_dials(
    manager: P2pManager, monkeypatch: pytest.MonkeyPatch, groups: set[bytes]
) -> list[tuple[NetworkAddressV2, dict[str, Any]]]:
    """Run one `NETWORK` pass of `_dial_one_draw`, answering what it made."""
    made: list[tuple[NetworkAddressV2, dict[str, Any]]] = []

    async def answers(address: NetworkAddressV2) -> bool:
        return True

    monkeypatch.setattr(manager_module, "dial", answers)
    monkeypatch.setattr(
        manager,
        "create_connection",
        lambda sock, address, **kwargs: made.append((address, kwargs)),
    )
    asyncio.run(manager._dial_one_draw(set(), groups, manager_module._Outbound.NETWORK))
    return made


def test_an_extra_network_peer_draws_again_past_a_held_group(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1100: the `preferred_net` draw runs in Core's loop of 100 tries.

    A draw in a group an outbound peer holds is followed by another, on
    the same network.
    """
    held = a_full_node("2a01::1", 18444)
    draws = iter([held, V6])
    manager = a_network_manager(a_manager, [], {})
    monkeypatch.setattr(
        manager.peer_db,
        "address_sampler",
        lambda *, new_only, network: (
            lambda: next(draws) if network is Network.IPV6 else None
        ),
    )
    manager._preferred_network = Network.IPV6
    made = network_dials(manager, monkeypatch, {net_group(held)})
    assert made == [(V6, FULL_RELAY)]


def test_an_extra_network_peer_is_held_to_the_loop_s_other_skips(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1100: the `preferred_net` draw is passed over as any other is.

    Short of `HasAllDesirableServiceFlags`, one of the skips past the
    network-group one, it is followed by another draw on the same network.
    """
    short = peer_address("2a01::1", 18444, services=ServiceFlags.NODE_NETWORK)
    draws = iter([short, V6])
    manager = a_network_manager(a_manager, [], {})
    monkeypatch.setattr(
        manager.peer_db,
        "address_sampler",
        lambda *, new_only, network: (
            lambda: next(draws) if network is Network.IPV6 else None
        ),
    )
    manager._preferred_network = Network.IPV6
    assert network_dials(manager, monkeypatch, set()) == [(V6, FULL_RELAY)]


def test_no_extra_network_peer_below_core_s_eight_full_relay_peers(
    a_manager: AManagerFactory,
) -> None:
    """`m_max_outbound_full_relay == MAX_OUTBOUND_FULL_RELAY_CONNECTIONS`.

    At `-maxconnections=7` seven full-relay peers meet the target, and no
    network peer is added to them.
    """
    manager = a_network_manager(a_manager, [], {Network.IPV6: V6}, 7)
    assert manager.max_outbound_full_relay == 7
    assert manager._next_outbound(7, 0) is None


@pytest.mark.parametrize(
    "on", [{}, {Network.IPV4: peer_address("5.9.0.1", 1)}], ids=["empty", "held"]
)
def test_no_network_to_prefer_draws_no_timer(
    a_manager: AManagerFactory, on: dict[Network, NetworkAddressV2]
) -> None:
    """A network with a peer on it, or none in the table, is no network.

    The timer stays due, as Core draws it only in the arm it enters.
    """
    conns = [
        a_conn(i, automatic=True, address=peer_address(f"5.{i}.0.1", 1))
        for i in range(8)
    ]
    manager = a_network_manager(a_manager, conns, on)
    assert manager._next_outbound(8, 2) is None
    assert manager._next_extra_network_peer == 0


def test_the_network_peer_waits_for_its_timer(a_manager: AManagerFactory) -> None:
    """`now > next_extra_network_peer`, drawn as the manager runs."""
    manager = a_network_manager(a_manager, [], {Network.IPV6: V6})
    manager._next_extra_network_peer = math.inf
    assert manager._next_outbound(8, 2) is None


def test_the_network_peer_timer_is_drawn_as_the_manager_runs(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` draws Core's `next_extra_network_peer`, five minutes on average."""
    monkeypatch.setattr(manager_module, "_exponential_delay", lambda mean: mean)
    manager = a_manager(listen=False, max_connections=0)
    before = time.time()
    manager.start()
    wait_until(manager.loop.is_running)
    assert before + 300 <= manager._next_extra_network_peer <= time.time() + 300


def test_the_network_counts_are_core_s_manual_and_full_relay_peers(
    a_manager: AManagerFactory,
) -> None:
    """`m_network_conn_counts`: `IsManualOrFullOutboundConn`, by `GetNetwork`.

    A pending peer counts; an inbound, block-relay-only, feeler or
    addr-fetch peer does not -- `IsManualOrFullOutboundConn` answers
    `false` for `ADDR_FETCH` too. A 6to4 address is IPv6 here, where
    `net_class` says IPv4.
    """
    manager = a_manager(
        [
            a_conn(1, automatic=True, address=peer_address("5.1.0.1", 1)),
            a_conn(2, address=peer_address("2002:0102:0304::1", 1)),
            a_conn(3, inbound=True, address=peer_address("5.3.0.1", 1)),
            a_conn(4, automatic=True, block_relay=True),
            a_conn(5, automatic=True, feeler=True),
            a_conn(6, addr_fetch=True, address=peer_address("5.6.0.1", 1)),
        ]
    )
    manager.pending_connections[7] = a_conn(
        7, automatic=True, address=peer_address("5.6.0.1", 1)
    )
    assert manager.network_conn_counts() == {Network.IPV4: 2, Network.IPV6: 1}


# `ThreadOpenConnections`' `nTries > 100`
_MAX_DRAWS = 100


def a_dialling_manager(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    draws: Sequence[NetworkAddressV2],
    **kwargs: Any,
) -> tuple[P2pManager, list[None], list[NetworkAddressV2]]:
    """Build a manager drawing `draws` in turn, recording what it dials."""
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)

    monkeypatch.setattr(manager_module, "dial", records)
    drawn, draw = draws_of(*draws)
    peer_db = a_peer_db_stub(is_empty=False, random_address=draw)
    return a_manager(peer_db=peer_db, **kwargs), drawn, dialled


def tries_of(manager: P2pManager) -> dict[bytes, float]:
    """Return the tries `a_peer_db_stub`'s `attempt` recorded."""
    return cast("dict[bytes, float]", cast("Any", manager.peer_db).tries)


@pytest.mark.parametrize(
    ("passed_over", "addnode_args"),
    [
        (peer_address("1.2.3.4", 8333, services=ServiceFlags.NODE_NETWORK), ()),
        (a_full_node("1.2.3.4", 22), ()),
        (a_full_node("1.2.3.4", 8333), ("1.2.3.4",)),
    ],
    ids=["missing-services", "bad-port", "addnode"],
)
def test_a_draw_core_passes_over_is_followed_by_another(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    passed_over: NetworkAddressV2,
    addnode_args: tuple[str, ...],
) -> None:
    """ISS 1224: each of `ThreadOpenConnections`' `continue`s draws again."""
    other = a_full_node("5.6.7.8", 8333)
    manager, drawn, dialled = a_dialling_manager(
        a_manager, monkeypatch, [passed_over, other], addnode_args=addnode_args
    )
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(drawn) == 2
    assert dialled == [other]


def test_a_recently_tried_draw_is_followed_by_another(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1224: an address dialled under ten minutes ago is passed over."""
    tried = a_full_node("1.2.3.4", 8333)
    other = a_full_node("5.6.7.8", 8333)
    manager, drawn, dialled = a_dialling_manager(
        a_manager, monkeypatch, [tried, tried, other]
    )
    tries_of(manager)[endpoint_key(tried)] = time.time() - (10 * 60 - 5)
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(drawn) == 3
    assert dialled == [other]


def test_a_try_ten_minutes_old_is_not_recent(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1224: `current_time - addr_last_try < 10min` is what passes over."""
    tried = a_full_node("1.2.3.4", 8333)
    manager, _, dialled = a_dialling_manager(a_manager, monkeypatch, [tried])
    now = time.time()
    # one clock for the record and the pass, so ten minutes is exact
    monkeypatch.setattr(time, "time", lambda: now)
    tries_of(manager)[endpoint_key(tried)] = now - 10 * 60
    asyncio.run(manager._maybe_dial_more_peers())
    assert dialled == [tried]


@pytest.mark.parametrize(
    ("address", "recent", "draws"),
    [(a_full_node("1.2.3.4", 8333), True, 30), (a_full_node("1.2.3.4", 22), False, 50)],
    ids=["recently-tried", "bad-port"],
)
def test_a_skip_core_bounds_by_draws_gives_way_at_its_bound(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    address: NetworkAddressV2,
    *,
    recent: bool,
    draws: int,
) -> None:
    """ISS 1224: `nTries < 30` and `nTries < 50`, `nTries` counting this draw.

    The same address drawn every time is passed over until the draw the
    bound names, and dialled there.
    """
    manager, drawn, dialled = a_dialling_manager(
        a_manager, monkeypatch, [address] * _MAX_DRAWS
    )
    if recent:
        manager.peer_db.attempt(address)
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(drawn) == draws
    assert dialled == [address]


@pytest.mark.parametrize(
    "address",
    [
        peer_address("1.2.3.4", 8333, services=ServiceFlags.NODE_WITNESS),
        a_full_node("1.2.3.4", 8333),
    ],
    ids=["missing-services", "addnode"],
)
def test_a_skip_core_does_not_bound_holds_for_every_draw(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    address: NetworkAddressV2,
) -> None:
    """ISS 1224: the services and `-addnode` checks have no draw bound."""
    manager, drawn, dialled = a_dialling_manager(
        a_manager, monkeypatch, [address] * _MAX_DRAWS, addnode_args=("1.2.3.4",)
    )
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(drawn) == _MAX_DRAWS
    assert dialled == []


@pytest.mark.parametrize(
    ("addnode_args", "address", "named"),
    [
        (("1.2.3.4",), a_full_node("1.2.3.4", 18444), True),
        (("1.2.3.4:8333",), a_full_node("1.2.3.4", 8333), True),
        (("1.2.3.4:8333",), a_full_node("1.2.3.4", 18444), False),
        (("2001:db8::1",), a_full_node("2001:db8::1", 18444), True),
        (("[2001:db8::1]:8333",), a_full_node("2001:db8::1", 8333), True),
        (("[2001:db8::1]:8333",), a_full_node("2001:db8::1", 18444), False),
        # compared as text, as Core compares it
        (("2001:db8:0::1",), a_full_node("2001:db8::1", 8333), False),
        # 23 and 24 distinct values, "1.2.3.4" one of them: `_added_peers`
        # is a `dict`, keyed on the value as given, so a repeated identical
        # value would not reach the bound the way a repetition in Core's
        # own `m_added_node_params` vector does -- distinct values are
        # what actually drives `_added_node`'s own `len(...)` read
        # (btclib-org/btclib-node#1350).
        (
            ("1.2.3.4", *(f"10.0.0.{i}" for i in range(22))),
            a_full_node("1.2.3.4", 8333),
            True,
        ),
        (
            ("1.2.3.4", *(f"10.0.0.{i}" for i in range(23))),
            a_full_node("1.2.3.4", 8333),
            False,
        ),
    ],
)
def test_an_addnode_value_names_a_draw_as_added_nodes_contain_does(
    a_manager: AManagerFactory,
    addnode_args: tuple[str, ...],
    address: NetworkAddressV2,
    *,
    named: bool,
) -> None:
    """ISS 1224: Core's `AddedNodesContain`, and its bound of 24 values."""
    manager = a_manager(addnode_args=addnode_args)
    assert manager._added_node(address) is named


@pytest.mark.parametrize("dialler", ["automatic", "connect"])
def test_every_dial_records_its_try(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch, dialler: str
) -> None:
    """ISS 1277: Core's `ConnectNode` calls `Attempt` for every dial.

    The automatic dial's and `async_connect`'s alike, the latter being
    the `-connect`/`-addnode` redial's and the `addnode` RPC's, whether
    or not the dial comes up.
    """
    address = a_full_node("5.6.7.8", 8333)
    manager, _, dialled = a_dialling_manager(a_manager, monkeypatch, [address])
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    if dialler == "automatic":
        asyncio.run(manager._maybe_dial_more_peers())
    else:
        asyncio.run(manager.async_connect(address))
    assert dialled == [address]
    assert tries_of(manager) == {endpoint_key(address): now}


def a_subnet(text: str) -> Subnet:
    """Parse `text` as `setban` would, asserting it parses."""
    subnet = lookup_subnet(text)
    assert subnet is not None
    return subnet


def test_a_banned_host_is_refused_with_every_slot_free(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `CreateNodeFromAcceptedSocket` refuses a banned peer outright.

    Ahead of the discouragement and the eviction, which both need the
    inbound slots nearly full: here none is taken. A host the banned
    subnet does not hold takes a slot.
    """
    port = get_random_port()
    manager = a_manager(port=port)
    manager.ban_man.ban(a_subnet("1.2.3.0/24"))
    logged, record = log_recorder()
    monkeypatch.setattr(manager.logger, "debug", record)
    manager.start()
    wait_until_listening(manager)
    with ExitStack() as peers:
        _, refused = land_an_inbound_peer(manager, "1.2.3.4", 50000)
        peers.enter_context(closing(refused))
        assert refused.recv(4096) == b""
        assert manager.last_connection_id == -1
        assert "connection from 1.2.3.4:50000 dropped (banned)" in logged
        _, accepted = land_an_inbound_peer(manager, "1.2.4.4", 50000)
        peers.enter_context(closing(accepted))
        wait_until(lambda: manager.last_connection_id == 0)
        manager.stop()
        manager.join(timeout=10)


def test_a_banned_address_is_not_dialled(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `OpenNetworkConnection` never dials a banned address."""
    dialled: list[NetworkAddressV2] = []

    async def records(address: NetworkAddressV2) -> None:
        dialled.append(address)

    monkeypatch.setattr(manager_module, "dial", records)
    drawn = [a_full_node("1.2.3.4", 18444), a_full_node("1.2.4.4", 18444)]
    peer_db = a_peer_db_stub(is_empty=False, random_address=drawn.pop)
    manager = a_manager(peer_db=peer_db)
    manager.ban_man.ban(a_subnet("1.2.4.0/24"))
    asyncio.run(one_pass(manager))
    asyncio.run(one_pass(manager))
    assert dialled == [a_full_node("1.2.3.4", 18444)]


def test_disconnecting_a_subnet_drops_every_connection_it_holds(
    a_manager: AManagerFactory,
) -> None:
    """Core's `DisconnectNode(CSubNet)`: every kind of connection, no other.

    Manual and pending ones included, an IPv4 peer mapped into IPv6 as
    well, and answering whether any matched.
    """
    inbound = a_conn(0, address=peer_address("1.2.3.4", 50000), inbound=True)
    manual = a_conn(1, address=peer_address("::ffff:1.2.3.5", 18444))
    pending = a_conn(2, address=peer_address("1.2.3.6", 18444), automatic=True)
    other = a_conn(3, address=peer_address("1.2.4.4", 18444), inbound=True)
    onion = a_conn(
        4, address=NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)
    )
    manager = a_manager([inbound, manual, other, onion])
    manager.pending_connections[2] = pending
    assert manager.disconnect_subnet(a_subnet("1.2.3.0/24")) is True
    for conn in (inbound, manual, pending):
        assert conn.stopped == [True]
    assert not other.stopped
    assert not onion.stopped
    assert manager.disconnect_subnet(a_subnet("5.6.7.8")) is False


def test_the_ban_list_is_written_once_every_interval(
    a_manager: AManagerFactory, tmp_path: Path
) -> None:
    """Core's scheduler dumps the ban list every `DUMP_BANS_INTERVAL`.

    A ban ending in the meantime is swept and written out then, rather
    than only at the next change or at stop.
    """
    path = tmp_path / "banlist.json"
    ban_man = BanMan(path, logging.getLogger(__name__))
    ban_man.ban(a_subnet("1.2.3.4"), 1_000)
    manager = a_manager()
    manager.ban_man = ban_man
    with ban_man._lock:
        ban_man._banned = {
            subnet: replace(entry, ban_until=entry.ban_until - 2_000)
            for subnet, entry in ban_man._banned.items()
        }
    now = manager._last_ban_dump + DUMP_BANS_INTERVAL
    manager._maybe_dump_banlist(now - 1)
    assert "1.2.3.4/32" in path.read_text(encoding="utf-8")
    manager._maybe_dump_banlist(now)
    assert "1.2.3.4/32" not in path.read_text(encoding="utf-8")


def test_a_feeler_passes_over_an_address_tried_recently_and_records_its_own(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1096: Core's recent-try `continue` holds of a feeler's draw too.

    The address tried a second ago is drawn until the 30th try, where the
    `continue` stops applying, and dialled then; the dial's own attempt
    is what the table keeps for it afterwards.
    """
    new = peer_address("5.6.7.8", 18444, services=FULL_NODE)
    counted: list[None] = []

    def count() -> NetworkAddressV2:
        counted.append(None)
        return new

    manager, made = a_feeler_manager(a_manager, monkeypatch, None)
    manager.peer_db = a_peer_db_stub(is_empty=False, random_new_address=count)
    before = time.time()
    tries_of(manager)[endpoint_key(new)] = before - 1
    asyncio.run(manager._maybe_dial_more_peers())
    assert len(counted) == manager_module._RECENT_TRY_DRAWS
    assert made
    assert tries_of(manager)[endpoint_key(new)] >= before
