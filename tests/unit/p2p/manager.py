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
import time
from contextlib import closing, suppress
from types import SimpleNamespace

import pytest
from btclib.p2p.keepalive import Ping

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p.address import NetworkAddress, NetworkID
from btclib_node.p2p.manager import P2pManager
from tests.helpers import (
    generate_random_transaction,
    get_random_port,
    wait_until,
    wait_until_listening,
)


def a_conn(
    conn_id,
    *,
    status=P2pConnStatus.Connected,
    last_receive=None,
    address=None,
    relay_tx=True,
):
    conn = SimpleNamespace(
        id=conn_id,
        status=status,
        address=address or NetworkAddress.from_ip_and_port("1.2.3.4", 18444),
        last_receive=time.time() if last_receive is None else last_receive,
        ping_sent=0,
        relay_tx=relay_tx,
        sent=[],
        stopped=[],
    )
    conn.send = conn.sent.append
    conn.stop = lambda: conn.stopped.append(True)

    def send_ping():
        # a ping already answered by nothing: the manager reads the time
        # it was sent to decide the peer is gone
        conn.ping_sent = time.time() - 200
        conn.sent.append("ping")

    conn.send_ping = send_ping
    return conn


@pytest.fixture
def a_manager():
    """Build managers, and close their event loops however the test ends."""
    made = []

    def make(conns=(), *, peer_db=None, status=NodeStatus.BlockSynced, port=18444):
        node = SimpleNamespace(
            status=status,
            chain=RegTest(),
            logger=SimpleNamespace(
                info=lambda *a: None,
                debug=lambda *a: None,
                exception=lambda *a: None,
            ),
        )
        # a peer db that refuses to be asked by default: a test that
        # should not reach for a peer proves it by the log staying quiet
        manager = P2pManager(
            node,
            port,
            peer_db
            or SimpleNamespace(
                is_empty=True,
                random_address=refuses_to_be_asked,
                get_addr_from_dns=asks_no_dns_server,
                add_active_address=lambda address: None,
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


async def one_pass(manager):
    """Run the housekeeping loop's body exactly once.

    `ensure_future` queues the task's first step ahead of the timer, so
    the body runs before the cancel however slow the machine is. Two
    passes is this twice, rather than a sleep long enough for the loop's
    own -- which is a wait on the scheduler, and #46's shape.
    """
    task = asyncio.ensure_future(manager.manage_connections(None))
    await asyncio.sleep(0.05)
    still_running = not task.done()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    return still_running


def test_removing_a_connection_that_is_not_there_changes_nothing(a_manager):
    conn = a_conn(1)
    manager = a_manager([conn])
    manager.remove_connection(99)
    assert list(manager.connections) == [1]
    assert not conn.stopped


def test_removing_a_connection_stops_it(a_manager):
    conn = a_conn(1)
    manager = a_manager([conn])
    manager.remove_connection(1)
    assert not manager.connections
    assert conn.stopped == [True]


def test_a_peer_that_cannot_be_dialled_is_not_kept(a_manager):
    async def never_connects():
        return None

    address = SimpleNamespace(connect=never_connects)
    manager = a_manager()
    asyncio.run(manager.async_connect(address))
    assert not manager.connections


def test_a_connection_that_has_closed_is_let_go_of(a_manager):
    conn = a_conn(1, status=P2pConnStatus.Closed)
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert not manager.connections


def test_a_peer_that_has_gone_quiet_is_pinged_and_then_dropped(a_manager):
    conn = a_conn(1, last_receive=time.time() - 200)
    manager = a_manager([conn])

    async def pinged_then_dropped():
        await one_pass(manager)
        assert conn.sent == ["ping"]
        assert list(manager.connections) == [1]
        await one_pass(manager)

    asyncio.run(pinged_then_dropped())
    assert not manager.connections


def test_a_peer_that_answered_recently_is_left_alone(a_manager):
    conn = a_conn(1)
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert conn.sent == []
    assert list(manager.connections) == [1]


def test_an_address_already_connected_to_is_not_dialled_again(a_manager):
    # an onion address, which this node cannot dial: reaching for it
    # would raise into the housekeeping loop's own handler, so a quiet
    # log is the assertion that the manager never reached
    onion = NetworkAddress(netid=NetworkID.torv3, addr=b"\x11" * 32, port=8333)
    conn = a_conn(1, address=onion)
    peer_db = SimpleNamespace(is_empty=False, random_address=lambda: onion)
    manager = a_manager([conn], peer_db=peer_db)
    logged: list[str] = []
    manager.logger.exception = logged.append
    asyncio.run(one_pass(manager))
    assert not logged
    assert list(manager.connections) == [1]


def test_a_dial_that_comes_back_with_nothing_adds_no_connection(a_manager):
    async def connects():
        return None

    peer_db = SimpleNamespace(
        is_empty=False,
        random_address=lambda: SimpleNamespace(connect=connects),
    )
    manager = a_manager(peer_db=peer_db)
    asyncio.run(one_pass(manager))
    assert not manager.connections


def refuses_to_be_asked():
    raise RuntimeError("no")


async def asks_no_dns_server():
    return None


def test_a_peer_db_that_raises_does_not_stop_the_housekeeping(a_manager):
    logged: list[str] = []
    peer_db = SimpleNamespace(is_empty=False, random_address=refuses_to_be_asked)
    manager = a_manager(peer_db=peer_db)
    manager.logger.exception = logged.append
    # still running when the pass ended: catching the exception and
    # returning would leave the node with no housekeeping at all
    assert asyncio.run(one_pass(manager)) is True
    assert logged


def test_only_one_peer_is_wanted_until_the_headers_are_synced(a_manager):
    # a peer db that refuses to be asked: reaching for a second peer
    # would raise into the housekeeping loop's own handler, so a quiet
    # log is the assertion that one peer was enough
    conn = a_conn(1)
    peer_db = SimpleNamespace(is_empty=False, random_address=refuses_to_be_asked)
    manager = a_manager([conn], peer_db=peer_db, status=NodeStatus.Starting)
    logged: list[str] = []
    manager.logger.exception = logged.append
    asyncio.run(one_pass(manager))
    assert not logged


def test_a_message_for_a_connection_that_is_gone_is_dropped(a_manager):
    conn = a_conn(1)
    manager = a_manager([conn])
    manager.send("message", 99)
    assert conn.sent == []
    manager.send("message", 1)
    assert conn.sent == ["message"]


def test_every_connection_is_pinged_and_every_connection_is_stopped(a_manager):
    first, second = a_conn(1), a_conn(2)
    manager = a_manager([first, second])
    manager.ping_all()
    assert first.sent == ["ping"] and second.sent == ["ping"]
    manager.stop_all()
    assert first.stopped == [True] and second.stopped == [True]


def test_a_transaction_of_our_own_goes_only_to_the_peers_that_want_them(a_manager):
    # the RPC's sendrawtransaction, which is the other way a transaction
    # leaves this node: a peer that declined BIP37's relay declined
    # transactions, not only the ones another peer handed us
    wants, declined = a_conn(1), a_conn(2, relay_tx=False)
    manager = a_manager([wants, declined])
    tx = generate_random_transaction()
    manager.broadcast_raw_transaction(tx)
    (sent,) = wants.sent
    assert sent.tx.id == tx.id
    assert declined.sent == []


def test_a_peer_that_was_pinged_recently_is_given_time_to_answer(a_manager):
    conn = a_conn(1, last_receive=time.time() - 200)
    conn.ping_sent = time.time()
    manager = a_manager([conn])
    asyncio.run(one_pass(manager))
    assert conn.sent == []
    assert list(manager.connections) == [1]


def test_an_empty_peer_db_is_not_asked_for_an_address(a_manager):
    # nothing to draw from: asking anyway is how a node with no peers
    # spends its housekeeping raising and logging
    manager = a_manager()
    logged: list[str] = []
    manager.logger.exception = logged.append
    asyncio.run(one_pass(manager))
    assert not logged


def test_a_peer_db_with_nothing_dialable_is_a_pass_that_does_nothing(a_manager):
    # `is_empty` is false and the draw still comes back with nothing:
    # a table of ipv6 and onion addresses. The pass has to do nothing
    # and come round again -- dialling the nothing it was handed would
    # raise into the loop's own handler once every tenth of a second
    peer_db = SimpleNamespace(is_empty=False, random_address=lambda: None)
    manager = a_manager(peer_db=peer_db)
    logged: list[str] = []
    manager.logger.exception = logged.append
    assert asyncio.run(one_pass(manager)) is True
    assert not logged
    assert not manager.connections


def test_a_peer_that_answers_the_dial_becomes_a_connection(a_manager):
    ours, theirs = socket.socketpair()

    async def connects():
        return ours

    peer_db = SimpleNamespace(
        is_empty=False,
        random_address=lambda: SimpleNamespace(connect=connects),
        add_active_address=lambda addr: None,
    )
    manager = a_manager(peer_db=peer_db)

    async def dial():
        await one_pass(manager)
        (conn,) = manager.connections.values()
        assert conn.client is ours
        assert not conn.inbound
        conn.task.cancel()
        await asyncio.sleep(0)

    with ours, theirs:
        # on the manager's own loop, which is the one it schedules the
        # connection's loop on. asyncio.run would build a second one and
        # leave the manager holding a loop the fixture then never closes
        manager.loop.run_until_complete(dial())


def test_a_transaction_is_relayed_with_its_witness(a_manager):
    # without the witness the peer receives a transaction whose txid is
    # the one it was told about and whose wtxid is not
    conn = a_conn(1)
    manager = a_manager([conn])
    tx = generate_random_transaction()
    manager.broadcast_raw_transaction(tx)
    (payload,) = conn.sent
    assert payload.tx == tx
    assert payload.include_witness


def a_running_manager(a_manager, port):
    manager = a_manager(port=port)
    manager.start()
    return manager


def test_a_manager_says_when_it_is_listening_and_not_before(a_manager):
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
            wait_until(lambda: manager.connections)
            (conn,) = manager.connections.values()
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


def test_a_manager_that_cannot_bind_never_says_it_is_listening(a_manager):
    # set after the bind and not before it, which is the whole of what a
    # caller waiting on the event is told. A port already taken raises
    # inside a coroutine nobody awaits, so a manager that announced
    # itself first would send that caller at a socket that is not there
    # -- and say nothing about why.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("", 0))
        taken.listen()
        manager = a_manager(port=taken.getsockname()[1])
        manager.start()
        try:
            with pytest.raises(Exception, match="within 0.5 seconds"):
                wait_until_listening(manager, timeout=0.5)
        finally:
            manager.stop()
            manager.join(timeout=10)


def test_a_manager_dials_the_address_it_is_given(a_manager):
    # `connect` is called from the node's thread and hands the dial to
    # the manager's own loop. Dialled at itself, so what comes back is
    # both ends of one connection: the one this node opened and the one
    # it accepted.
    port = get_random_port()
    manager = a_running_manager(a_manager, port)
    try:
        wait_until_listening(manager)
        manager.connect(NetworkAddress.from_ip_and_port("127.0.0.1", port))
        wait_until(lambda: len(manager.connections) == 2)
        inbound = [conn.inbound for conn in manager.connections.values()]
        assert sorted(inbound) == [False, True]
    finally:
        manager.stop()
        manager.join(timeout=10)


def test_a_message_sent_on_a_running_connection_reaches_the_peer(a_manager):
    # `Connection.send` is called from the node's thread and hands the
    # write to the manager's loop; nothing else in these tests crosses
    # that line, and a message that never leaves is a peer that goes
    # quiet for no reason
    port = get_random_port()
    manager = a_running_manager(a_manager, port)
    try:
        wait_until_listening(manager)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as peer:
            wait_until(lambda: manager.connections)
            (conn,) = manager.connections.values()
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


def test_a_manager_left_running_is_stopped_by_whoever_built_it(a_manager):
    # deliberately not stopped here. A manager thread outliving its test
    # is non-daemon, so a test that fails before reaching its own stop
    # would hold the run open instead of failing it -- the fixture is
    # where that is caught, and this is the test that proves it does.
    manager = a_running_manager(a_manager, get_random_port())
    wait_until_listening(manager)
    assert manager.is_alive()


def test_stopping_a_running_manager_stops_the_connections_it_holds(a_manager):
    port = get_random_port()
    manager = a_running_manager(a_manager, port)
    wait_until_listening(manager)
    # held open across the stop rather than closed by a `with`: a
    # connection this manager closed and one that ended because the far
    # side went away are indistinguishable afterwards. A wait that times
    # out before the stop below leaves the manager to the fixture, which
    # stops what it finds running.
    with closing(socket.create_connection(("127.0.0.1", port), timeout=20)):
        wait_until(lambda: manager.connections)
        (conn,) = manager.connections.values()
        manager.stop()
        manager.join(timeout=10)
    assert conn.status == P2pConnStatus.Closed
    assert manager.loop.is_closed()
    # and the flag goes back to meaning what it says: waiting for a
    # stopped manager to listen would otherwise return at once, on a
    # socket that is closed
    assert not manager.listening.is_set()
