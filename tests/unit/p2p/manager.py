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
from contextlib import suppress
from types import SimpleNamespace

import pytest

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p.address import NetworkAddress, NetworkID
from btclib_node.p2p.manager import P2pManager


def a_conn(conn_id, *, status=P2pConnStatus.Connected, last_receive=None, address=None):
    conn = SimpleNamespace(
        id=conn_id,
        status=status,
        address=address or NetworkAddress.from_ip_and_port("1.2.3.4", 18444),
        last_receive=time.time() if last_receive is None else last_receive,
        ping_sent=0,
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

    def make(conns=(), *, peer_db=None, status=NodeStatus.BlockSynced):
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
            18444,
            peer_db
            or SimpleNamespace(is_empty=True, random_address=refuses_to_be_asked),
        )
        for conn in conns:
            manager.connections[conn.id] = conn
        made.append(manager)
        return manager

    yield make
    for manager in made:
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
