# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`P2pManager`, the thread listening for and dialing peer connections.

Runs its own asyncio loop -- `manage_connections` accepts inbound
sockets, dials outbound ones from `PeerDB`, and prunes an idle or
handshake-stuck `Connection` -- and hands finished messages back to
`Node`'s own thread through `messages` and `handshake_messages`. A
coroutine enters this loop only through `run_coroutine_threadsafe`;
`Node`'s own thread calls this class's plain methods, such as `verack`'s
own `promote_connection`, directly.
"""

import asyncio
import socket
import threading
import time
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, override

from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p.address import PeerDB, dial, endpoint_key, peer_address
from btclib_node.p2p.connection import Connection

if TYPE_CHECKING:
    from btclib.p2p.addrv2 import NetworkAddressV2
    from btclib.p2p.payload import Payload
    from btclib.tx.tx import Tx as BtclibTx

    from btclib_node import Node

# How often `manage_connections`' own loop prunes the active-address
# table on its own rather than only as a side effect of something asking
# for it. Not tied to the loop's own sleep below -- an O(n) walk of
# `active_addresses` every pass buys nothing a run every few minutes
# does not -- but to `get_active_addresses`'s own three-hour staleness
# window: far enough under it that a stale row does not linger long past
# it, however rarely this node is asked for its table.
# btclib-org/btclib-node#71
_ACTIVE_PRUNE_INTERVAL = 300

# `manage_connections`'s own idle bound, not Core's `TIMEOUT_INTERVAL`
# (20 minutes, `net.h`, aed80c7395) -- a shorter one of this tree's own:
# a connection quiet this long is sent a `ping`, and one still quiet
# this long again after that, or a pending connection stuck short of
# `verack` this long with no `ping` to wait on at all, is dropped.
_IDLE_TIMEOUT = 120


class P2pManager(threading.Thread):
    """The thread listening for and dialling peer connections.

    The module docstring above is where its own loop, its two message
    queues and the boundary with `Node`'s thread are argued;
    `connections`/`pending_connections` and the lock that guards moving
    a connection between them are this class's own state for that.
    """

    def __init__(self, node: Node, port: int | None, peer_db: PeerDB) -> None:
        """Set up empty connection tables and queues, and a fresh event loop."""
        super().__init__()
        self.node = node
        self.logger = node.logger
        self.port = port
        self.peer_db = peer_db

        self.connections: dict[int, Connection] = {}
        # A connection accepted or dialled but not yet past `verack`,
        # kept out of `connections` so that nothing iterating it -- the
        # two sends #114 gated, ping housekeeping, `stop_all` -- can
        # reach a peer the handshake has not cleared to speak the rest
        # of the protocol to: btclib-org/btclib-node#131.
        # `promote_connection` is the only way out of this dict, and
        # `callbacks.verack` is the only caller, right where
        # `P2pConnStatus.Connected` is set.
        self.pending_connections: dict[int, Connection] = {}
        # `promote_connection` pops from `pending_connections` and then
        # writes into `connections` -- two statements, not one -- and
        # `remove_connection` pops from `connections` and then, only if
        # that missed, from `pending_connections` -- two statements
        # again. `promote_connection` runs on `Node`'s own loop, off
        # `callbacks.verack`; `remove_connection` runs on this
        # manager's own loop, off `_prune_stale_connections`. Unlocked,
        # a `remove_connection` whose first pop misses because the
        # connection is still pending can run its second pop after
        # `promote_connection` has already moved it, missing it there
        # too -- the connection is live in `connections` with nothing
        # having stopped it (btclib-org/btclib-node#358). This lock is
        # what makes the two pops and the pop-then-write one step
        # apiece; it is also what `_maybe_dial_more_peers` takes to
        # read both dicts as of one instant rather than two
        # (btclib-org/btclib-node#355). Held only across the dict
        # operations themselves in every case above -- never across an
        # `await` or a call into `Connection` -- so nothing here blocks
        # `Node`'s thread for longer than an in-memory pop or a write
        # takes.
        #
        # `stop()`'s own closing sweep (below) reads this same pair
        # unlocked, on purpose: it runs only after `join()`, and its own
        # comment there is what argues nothing but this manager's thread
        # can still be reaching either dict by then, `promote_connection`
        # included -- not a second snapshot-style reader this lock left
        # out.
        self._connections_lock = threading.Lock()
        # (command, payload, connection id), which is what a connection
        # appends and what p2p.main pops apart; the handshake ones go
        # in a queue of their own, drained whole before the rest.
        # `messages`' own items carry a fourth element, the message's own
        # wire size -- `Connection.parse_messages` and `handle_p2p`
        # (`p2p/main.py`) are the two ends of what that paces,
        # `Connection.MAX_QUEUED_RECV_BYTES`'s own comment arguing why;
        # `handshake_messages` is not paced this way and carries no
        # fourth element. btclib-org/btclib-node#462
        self.messages: deque[tuple[str, bytes, int, int]] = deque()
        self.handshake_messages: deque[tuple[str, bytes, int]] = deque()
        # Every nonce `add_pending_outbound_nonce` (below) has recorded
        # for an outbound connection still short of its own `verack` --
        # `promote_connection` and `remove_connection` below each
        # discard their own connection's entry, so this shrinks exactly
        # as those connections complete or close, rather than sitting
        # in a fixed-size ring. `is_self_connect_nonce` (below) is the
        # only reader, and both it and every write here go through
        # `_connections_lock` above, the same as `pending_connections`
        # and `connections` -- so a `remove_connection` discarding one
        # connection's entry on this manager's own thread can never
        # race a lookup for a different one on `Node`'s.
        self.pending_outbound_nonces: set[int] = set()
        self.last_connection_id = -1
        # Endpoints `discourage` has been told to stop redialling, by
        # `endpoint_key` -- process lifetime, not `peer_db`'s own tables,
        # so a wrongly discouraged endpoint is recovered by a restart
        # rather than by touching the datadir, matching Core's own
        # `CRollingBloomFilter` (`banman.h`, at bitcoin/bitcoin@58a7869f86)
        # over its persisted ban list. Unlocked: `discourage` only ever
        # adds a key and `manage_connections` only ever asks `in`, never
        # walks it, so there is nothing here for the two to catch each
        # other mid-stride the way `PeerDB._addresses_lock`'s own
        # iteration can -- the same reasoning `PeerDB.is_empty` already
        # gives for reading its own set unlocked. btclib-org/btclib-node#283
        self.discouraged: set[bytes] = set()
        # 0.0, not `time.time()`: the first pass of `manage_connections`
        # prunes on the spot rather than waiting a full
        # `_ACTIVE_PRUNE_INTERVAL` after this manager was constructed.
        self._last_active_prune = 0.0

        # Set once the listening socket is bound and can hold a peer's
        # connection in its backlog. `is_alive()` says only that this
        # thread was started, which is true before `run` below has
        # scheduled anything, so a peer that dials on the strength of it
        # is refused -- and `dial` answers a refusal with None, which
        # `async_connect` drops. Nothing retries.
        self.listening = threading.Event()

        self.loop = asyncio.new_event_loop()
        # What `run` binds and `stop` closes -- kept here rather than
        # only inside the `with server_socket:` each of `server`'s own
        # tasks holds, since that alone depends on this manager's own
        # loop actually delivering that task's cancellation before
        # `stop` returns, which stop()'s own comment on the connections
        # sweep below is not the only place that can go wrong under
        # load (btclib-org/btclib-node#312). Read only by `run` and
        # `stop`, both on this manager's own object and never
        # concurrently -- `run` sets it once, from this thread, before
        # `stop` could possibly be reached by another.
        self._server_sockets: list[socket.socket] = []
        # `server`'s own accept queue, one per listening socket, kept
        # here rather than only local to `server`'s own frame so the two
        # `manager_test.py` tests naming btclib-org/btclib-node#386 can
        # land a connection into the live queue directly -- the seam
        # `loop.sock_accept`'s own future used to give their own
        # predecessors before this fix replaced it. Nothing in this
        # class reads it outside `server` itself.
        self._accept_queues: dict[
            socket.socket,
            asyncio.Queue[
                tuple[socket.socket, tuple[str, int] | tuple[str, int, int, int]]
            ],
        ] = {}

    def create_connection(
        self, client: socket.socket, address: NetworkAddressV2, *, inbound: bool
    ) -> None:
        """Build a `Connection` for `client`, hold it pending, and start it."""
        client.settimeout(0.0)
        self.last_connection_id += 1
        conn = Connection(
            self, client, address, self.last_connection_id, inbound=inbound
        )
        self.pending_connections[self.last_connection_id] = conn
        task = asyncio.run_coroutine_threadsafe(conn.run(), self.loop)
        conn.task = task

    def promote_connection(self, connection_id: int) -> None:
        """Move a connection out of the handshake and into the herd.

        The only caller is `callbacks.verack`, right after it sets
        `P2pConnStatus.Connected` -- the two are one step, kept as two
        calls only because the status belongs to the connection and the
        dict it lives in belongs to the manager. `_connections_lock`
        (`__init__`) is what makes the pop and the write one step too,
        against `remove_connection`'s own two pops below, on the other
        thread.

        Successfully connected is exactly the state
        `pending_outbound_nonces` (`__init__`) has to stop answering
        for, so this connection's own nonce leaves it here too, inside
        the same locked block -- `discard` rather than a guarded pop,
        since an inbound connection's nonce, never added there, is just
        as harmless to ask it to remove; the `is not None` guard is only
        for `discard`'s own typing, `set[int]` rather than
        `set[int | None]`.
        """
        with self._connections_lock:
            conn = self.pending_connections.pop(connection_id, None)
            if conn is not None:
                self.connections[connection_id] = conn
                if conn.nonce is not None:
                    self.pending_outbound_nonces.discard(conn.nonce)

    def remove_connection(self, connection_id: int) -> None:
        """Drop `connection_id` from either table and stop it, if it was held.

        `_connections_lock` (`__init__`) is what makes the two pops one
        step, against `promote_connection`'s own pop-then-write. The
        same connection leaving `pending_connections` this way is one
        `pending_outbound_nonces` (`__init__`) has to stop answering for
        too, so its own nonce is discarded inside the same locked block,
        the same reason `promote_connection` above does it there rather
        than after. `conn.stop()` stays outside the lock, as every other
        call into `Connection` from in here does.
        """
        with self._connections_lock:
            conn = self.connections.pop(
                connection_id, None
            ) or self.pending_connections.pop(connection_id, None)
            if conn is not None and conn.nonce is not None:
                self.pending_outbound_nonces.discard(conn.nonce)
        if conn is not None:
            conn.stop()

    def add_pending_outbound_nonce(self, nonce: int) -> None:
        """Record `nonce` as this outbound, still-unhandshaken connection's own.

        The only caller is `Connection.send_version`, for an outbound
        connection. `_connections_lock` (`__init__`) is what every
        access to `pending_outbound_nonces` goes through -- this write
        included -- so it can never land between `is_self_connect_nonce`
        below reading the set and returning.
        """
        with self._connections_lock:
            self.pending_outbound_nonces.add(nonce)

    def is_self_connect_nonce(self, nonce: int) -> bool:
        """Whether `nonce` is a live, unhandshaken outbound connection's own.

        The only caller is `callbacks.version`. Matches Core's own
        live, per-connection search -- `CConnman::CheckIncomingNonce`,
        `net.cpp:360-376` at bitcoin/bitcoin@b91d983f66 -- which walks
        every node still short of `fSuccessfullyConnected` and not
        `IsInboundConn()`, rather than a fixed-size ring:
        `pending_outbound_nonces` (`__init__`) reproduces that search by
        never holding an inbound connection's own nonce to begin with
        (`add_pending_outbound_nonce` above), not by filtering one out
        of a wider set at lookup time.

        That same walk also excludes a private-broadcast connection's
        own nonce, one candidate among the ones it visits -- the reason
        given there is a peer taking such a connection down must not be
        able to infer this node dropped it and learn its clearnet
        address from the disconnect. This tree has no private-broadcast
        connection, so nothing here excludes on that account, and
        nothing here depends on that exclusion existing either.

        Separately, `net_processing.cpp:3886` only calls that walk at
        all for a `version` arriving on an inbound connection -- a
        second restriction, on when the search runs rather than on what
        it searches, and not the one the paragraph above is about. Not
        reproduced here: the set already holds only outbound-origin
        nonces, so an ordinary peer's own draw is never found in it
        regardless of which side received the `version`, and asking
        unconditionally costs nothing extra.
        """
        with self._connections_lock:
            return nonce in self.pending_outbound_nonces

    def discourage(self, address: NetworkAddressV2) -> None:
        """Stop `manage_connections` from redialling this endpoint.

        The caller is one of the `conn.stop()` sites that stops a
        connection this node dialled or accepted for cause -- an
        incompatible peer or one that broke the protocol, never a
        connection this node closed on its own account. `address` is
        `conn.address`, keyed the same way `already_connected` below
        already compares live connections against a draw.
        """
        self.discouraged.add(endpoint_key(address))

    async def async_connect(self, address: NetworkAddressV2) -> None:
        """Dial `address` and, if it comes up, register the connection."""
        client = await dial(address)
        if client:
            self.create_connection(client, address, inbound=False)

    def connect(self, address: NetworkAddressV2) -> None:
        """Schedule `async_connect(address)` onto this manager's own loop."""
        asyncio.run_coroutine_threadsafe(self.async_connect(address), self.loop)

    def _prune_stale_connections(self, now: float) -> None:
        for conn in self.connections.copy().values():
            if conn.status == P2pConnStatus.Closed:
                self.remove_connection(conn.id)
                continue
            if now - conn.last_receive > _IDLE_TIMEOUT:
                # One read, not `conn.ping_sent` re-read in the `elif`
                # below: `callbacks.pong`, on the other thread, clears
                # it the moment a pong answers this connection's own
                # ping, and a second read landing right after that
                # clear turned `now - 0 > _IDLE_TIMEOUT` true for every
                # `now`, dropping a peer for having just answered.
                # btclib-org/btclib-node#357
                ping_sent = conn.ping_sent
                if not ping_sent:
                    conn.send_ping()
                elif now - ping_sent > _IDLE_TIMEOUT:
                    self.remove_connection(conn.id)
        for conn in self.pending_connections.copy().values():
            # The same idle bound, but no ping in between: `ping` is
            # as much a message the handshake has to clear before it
            # is sent as `inv` or `tx` is, so a connection stuck
            # short of `verack` is dropped once it goes quiet rather
            # than kept a second `_IDLE_TIMEOUT` waiting on an answer
            # to something #131 forbids sending it.
            if (
                conn.status == P2pConnStatus.Closed
                or now - conn.last_receive > _IDLE_TIMEOUT
            ):
                self.remove_connection(conn.id)

    def _maybe_prune_active_addresses(self, now: float) -> None:
        if now - self._last_active_prune < _ACTIVE_PRUNE_INTERVAL:
            return
        # The only other callers of `get_active_addresses` are
        # `random_address`, which this loop stops reaching for
        # once it has enough connections, and `getaddr`, answered
        # once per connection and never again -- so a node with
        # enough peers that nobody asks a `getaddr` would
        # otherwise never prune a stale row. btclib-org/btclib-node#71
        self._last_active_prune = now
        try:
            # get_active_addresses deletes every aged-out row
            # from the store, real I/O and not a pure read, and
            # this coroutine's own future is never awaited
            # (`run`, below) -- the same failure mode
            # `_bind_one`'s own docstring names for a coroutine
            # scheduled that way. Unguarded, whatever `db.delete`
            # ever raised would end this loop's pinging, eviction
            # and dialling for the rest of this node's life
            # rather than only this one prune, the same reason
            # the dial below is already inside a `try` of its
            # own.
            self.peer_db.get_active_addresses()
        except Exception:
            self.logger.exception("Exception occurred")

    async def _maybe_dial_more_peers(self) -> None:
        connection_num = 1 if self.node.status < NodeStatus.HeaderSynced else 10
        # Locked, and the snapshot below locks separately rather than
        # sharing this one: `promote_connection` moves a connection
        # between `connections` and `pending_connections` in two
        # statements, so two unlocked reads taken apart -- a `len()`
        # here, `.values()` there -- could each miss it, out of
        # `connections` because the read ran before the write, out of
        # `pending_connections` because it ran after the pop, and this
        # count would then undercount a node that already has enough
        # peers (btclib-org/btclib-node#367). A second acquisition
        # rather than one covering both this count and the snapshot
        # below is what keeps this early return cheap: most passes,
        # once the node already holds enough peers, return here, and
        # building `already_connected` -- which such a pass would only
        # throw away -- is not owed every 100 ms just because this
        # count is.
        with self._connections_lock:
            live = len(self.connections) + len(self.pending_connections)
        if live >= connection_num or self.peer_db.is_empty:
            return
        # By endpoint_key, not raw equality: a drawn address
        # carries whatever timestamp and services callbacks.verack
        # or a gossiping peer last recorded it with, which is
        # never the pair an existing Connection's own address was
        # constructed with, so comparing the dataclasses
        # themselves never matches the peer this node is already
        # holding a connection with and dials it a second time.
        #
        # Locked for the same reason the count above is
        # (btclib-org/btclib-node#355).
        with self._connections_lock:
            connected = (
                *self.connections.values(),
                *self.pending_connections.values(),
            )
        already_connected = {endpoint_key(conn.address) for conn in connected}
        try:
            address = self.peer_db.random_address()
            # `is_empty` answers whether the table holds
            # anything, not whether it holds anything this node
            # can dial, so the guard above lets a table of ipv6
            # and onion addresses through. The draw is what
            # knows, and it answers with nothing: this pass has
            # nothing to do, and the sleep below is what keeps
            # that from being a spin. `discouraged` is the same
            # kind of refusal as `already_connected`, against a
            # peer this node has already dialled or accepted and
            # dropped for cause rather than one it already holds.
            # btclib-org/btclib-node#283
            if (
                address is not None
                and endpoint_key(address) not in already_connected
                and endpoint_key(address) not in self.discouraged
            ):
                sock = await dial(address)
                if sock:
                    self.create_connection(sock, address, inbound=False)
        except Exception:
            self.logger.exception("Exception occurred")

    async def manage_connections(self) -> None:
        """Prune, prune some more, maybe dial, sleep -- forever, every 0.1s.

        `_prune_stale_connections` pings or drops an idle peer every
        pass; `_maybe_prune_active_addresses` runs far less often; and
        `_maybe_dial_more_peers` dials one more only if this node still
        has room for it.
        """
        while True:
            now = time.time()
            self._prune_stale_connections(now)
            self._maybe_prune_active_addresses(now)
            await self._maybe_dial_more_peers()
            await asyncio.sleep(0.1)

    def _bind_one(self, family: socket.AddressFamily, host: str) -> socket.socket:
        """Bind and listen on one family, synchronously.

        Not the coroutine below: a coroutine handed to
        `run_coroutine_threadsafe` runs on the loop's own thread, behind a
        `concurrent.futures.Future` nobody reads, so a bind failure inside
        one is an `OSError` that vanishes rather than one that reaches
        `run`'s caller (#88). Doing it here instead, before `run_forever`
        is ever called, means the same failure raises out of `run` --
        this thread's target -- so the thread ends rather than staying
        `is_alive()` over a listener that never came up.
        """
        server_socket = socket.socket(family, socket.SOCK_STREAM)
        try:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                # Otherwise a dual-stack kernel hands this socket an
                # inbound v4 peer too, its address mapped into sixteen
                # octets the way #151 has this node refuse to keep
                # gossiped -- and `server` below has no unmapping of its
                # own to give such a connection the network id #151
                # would ask for. Core sets the same option on its own
                # "::" listener for the same reason (net.cpp, 58a7869f86).
                server_socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            server_socket.bind((host, self.port))
            server_socket.listen()
            server_socket.settimeout(0.0)
        except OSError:
            # the caller never gets this socket to close: raising it
            # out of a coroutine nobody awaited (#88) is what let a
            # failed bind's fd outlive the exception in the first place
            server_socket.close()
            raise
        return server_socket

    def _bind(self) -> list[socket.socket]:
        """Bind every listener this node has, the IPv4 one required.

        The IPv6 one is not: a host with no IPv6 route or with it turned
        off at the kernel fails the bind above, and that is not this
        node's own defect to raise `run` out on, unlike a taken IPv4
        port. Core's `InitBinds` treats its own "::" the same way --
        "Don't consider errors to bind on IPv6 '::' fatal because the
        host OS may not have IPv6 support" (net.cpp, 58a7869f86) -- while
        a failure to bind "0.0.0.0" is `BF_REPORT_ERROR` there too.
        """
        # All interfaces, by design: a P2P listener accepts inbound
        # peers from anywhere.
        sockets = [self._bind_one(socket.AF_INET, "0.0.0.0")]  # noqa: S104
        try:
            sockets.append(self._bind_one(socket.AF_INET6, "::"))
        except OSError:
            self.logger.info("No IPv6 P2P listener on port %s", self.port)
        self.listening.set()
        return sockets

    def _accept_one(
        self,
        server_socket: socket.socket,
        accepted: asyncio.Queue[
            tuple[socket.socket, tuple[str, int] | tuple[str, int, int, int]]
        ],
    ) -> None:
        """`server`'s own reader callback, a method rather than a closure.

        A unit test can call it directly against a socket of its own --
        real, or one that only duck-types `.accept()` -- to cover the
        two exception arms without a live listener.
        """
        try:
            sock, sockaddr = server_socket.accept()
        except BlockingIOError, InterruptedError:
            return
        except OSError:
            self.logger.exception("Accepting an inbound connection failed")
            return
        sock.settimeout(0.0)
        accepted.put_nowait((sock, sockaddr))

    async def server(
        self, loop: asyncio.AbstractEventLoop, server_socket: socket.socket
    ) -> None:
        """Accept connections off `server_socket`, one `create_connection` each.

        Reads through `accepted`, an `asyncio.Queue` the plain reader
        callback `_accept_one` fills, rather than a bare
        `await loop.sock_accept` -- the comment below argues why that
        queue is what keeps a shutdown from discarding an already
        accepted socket.
        """
        with server_socket:
            # A plain reader callback stores what it accepts in a
            # queue: the socket sits in a plain deque the instant
            # `on_readable` runs, a callback rather than a task and so
            # never itself a `Task.cancel` target, and the `finally`
            # below closes whatever cancellation leaves in the queue --
            # `server`'s own task included -- on whichever pass reaches
            # it.
            #
            # A bare `await loop.sock_accept(server_socket)` does not
            # have that property: that call's own future can already
            # carry a connection when something cancels the task
            # suspended on it, and `Task.cancel` cannot cancel a future
            # that is already done -- it throws `CancelledError` in on
            # the next step regardless, discarding whatever the kernel
            # handed over with nothing left holding it. Wrapping that
            # await in a task of its own, shielded from an outer cancel,
            # is what btclib-org/btclib-node#312 fixed this discard with
            # for a cancel arriving through this coroutine's own await;
            # it could not fix a cancel reaching that inner task
            # directly, which is what `stop`'s own blanket sweep over
            # `asyncio.all_tasks` does on every pass, and no fixed
            # number of grace steps before that sweep closes the gap --
            # the kernel is free to resolve the future in the exact
            # window between a check and the cancel that follows it
            # (btclib-org/btclib-node#386, measured against a live
            # listener under load rather than only the deterministic
            # race the tests construct).
            accepted: asyncio.Queue[
                tuple[socket.socket, tuple[str, int] | tuple[str, int, int, int]]
            ] = asyncio.Queue()
            self._accept_queues[server_socket] = accepted

            def on_readable() -> None:
                self._accept_one(server_socket, accepted)

            loop.add_reader(server_socket.fileno(), on_readable)
            try:
                while True:
                    sock, sockaddr = await accepted.get()
                    # two fields for an AF_INET peer, four for an
                    # AF_INET6 one -- the flow info and the scope id
                    # BIP155 has nowhere to carry either,
                    # `get_addr_from_dns`'s own sockaddr comment being
                    # where that is argued
                    address = peer_address(*sockaddr[:2])
                    self.create_connection(sock, address, inbound=True)
            finally:
                loop.remove_reader(server_socket.fileno())
                del self._accept_queues[server_socket]
                while not accepted.empty():
                    accepted.get_nowait()[0].close()

    @override
    def run(self) -> None:
        self.logger.info("Starting P2P manager")
        loop = self.loop
        asyncio.set_event_loop(loop)
        try:
            server_sockets = self._bind()
        except OSError:
            self.logger.exception("Could not bind the P2P listener")
            raise
        self._server_sockets = server_sockets
        asyncio.run_coroutine_threadsafe(self.peer_db.get_addr_from_dns(), loop)
        for server_socket in server_sockets:
            asyncio.run_coroutine_threadsafe(self.server(loop, server_socket), loop)
        asyncio.run_coroutine_threadsafe(self.manage_connections(), loop)
        loop.run_forever()

    def stop(self) -> None:
        """Stop this manager's own loop, then every connection and task on it.

        The comment below is the whole of what makes `stop_handle`
        itself safe to cancel unconditionally, on any of the three ways
        `run` above can have left this loop by the time `join` returns.
        """
        stop_handle = self.loop.call_soon_threadsafe(self.loop.stop)
        # `join` blocks this thread without spinning it, the way
        # `Node.stop` already waits on itself with `self.join`. Guarded
        # on `is_alive`, since `Node.run` calls this unconditionally --
        # a node with `p2p_port` unset never calls `start`, and `join`
        # on a thread that was never started raises.
        if self.is_alive():
            self.join()
        # `stop_handle.cancel()` is what makes every `run_until_complete`
        # below safe, on any loop this method could possibly be handed --
        # not one more guard clause alongside `self.ident` and `pending`,
        # which is what #368 and #362 each tried and #380 and #377 each
        # found a gap in. The `call_soon_threadsafe` above only
        # *schedules* `loop.stop`; it is delivered -- `self._stopping`
        # set, so `run_forever` returns after its current pass -- only
        # once something actually drives this loop's `run_forever` far
        # enough to reach it. Three things can happen by the time `join`
        # above returns:
        #
        # - This manager's own thread was running `run_forever` (the
        #   ordinary case) and delivered it there, exiting on its own.
        #   `join` already waited for exactly that, so the handle has
        #   already fired and is spent.
        # - This thread was never started at all (`self.ident is None`)
        #   -- `is_alive()` above is `False`, `join` is skipped, and
        #   nothing has ever driven this loop, so the handle is still
        #   sitting in its ready queue, undelivered.
        # - This thread was started and `run()` raised before ever
        #   reaching `run_forever` -- a bind failure being the ordinary
        #   way (btclib-org/btclib-node#353) -- so `self.ident is not
        #   None` even though `run_forever`, again, never ran: the
        #   handle is undelivered the same as the case above, which is
        #   exactly what defeated `self.ident is not None` as a guard
        #   (btclib-org/btclib-node#380).
        #
        # `Handle.cancel()` on a handle already delivered is specified as
        # a no-op -- there is nothing left to remove from a ready queue
        # already drained of it -- so calling it here unconditionally is
        # correct for the first case above and is what removes the
        # landmine outright for the other two, rather than merely
        # stepping past where it goes off once (#368) and leaving every
        # `run_until_complete` downstream of that first step still primed
        # to hit it (btclib-org/btclib-node#377): a task whose own
        # cancellation needs a second real step to unwind -- an `except
        # CancelledError` handler that awaits a fresh timer rather than
        # only an already-cancelled future -- is not owed anything by a
        # single guarded step, only by there being no leftover stop left
        # to answer at all. `RpcManager.stop` carries the identical fix,
        # for the identical reason (btclib-org/btclib-node#377,
        # btclib-org/btclib-node#380).
        stop_handle.cancel()
        # Only after join(), not before: `run()` above has now returned,
        # so nothing but this thread can still be adding to
        # `self.connections`/`self.pending_connections` -- `create_connection`
        # and `remove_connection` are only ever reached from a coroutine
        # on this manager's own loop, and `promote_connection`, `Node`'s
        # thread's own exception, cannot race a `stop()` that same
        # thread is itself blocked inside. A sweep taken before join()
        # closed whatever it snapshotted correctly but could still miss
        # a connection `server()`'s own accept loop created in the
        # window between `loop.stop` merely being scheduled above and
        # actually being delivered -- accepted, given a task, and never
        # swept, since nothing before join() ever looked again. Such a
        # task reaches only the cancellation below, which cannot close
        # `Connection.client` for it: `Task.cancel()` called before a
        # task has run even once skips the coroutine entirely, `run()`'s
        # own `finally` included (btclib-org/btclib-node#312).
        #
        # And a pass of each is not enough, because `run_until_complete`
        # runs the loop: a task this pass has not cancelled yet goes on
        # working while an earlier one is being drained. `server()` is
        # the one that matters -- it takes what the kernel left in the
        # listen backlog during that same window and hands it to
        # `create_connection`, which registers a connection the sweep has
        # already passed and gives it a task no snapshot taken before the
        # drain holds. Nothing closes that socket and nothing ends that
        # task, so `loop.close()` below leaves it pending at
        # `Connection.run`'s own `sock_recv` for the collector to report.
        # Cancelling every task before the loop is allowed to run again
        # is what answers that. Repeating the whole thing until the loop
        # has no tasks left is the postcondition stated outright rather
        # than argued from who is still able to call `create_connection`,
        # and it terminates because the accept loop is cancelled on the
        # first pass (btclib-org/btclib-node#312).
        while True:
            for conn in (
                *self.connections.values(),
                *self.pending_connections.values(),
            ):
                conn.stop()
            pending = asyncio.all_tasks(self.loop)
            if not pending:
                break
            # No step of the loop first here, unlike an earlier version
            # of this method: that step existed only to let a task
            # sitting on an already-resolved future -- `server`'s own
            # former `accept` task -- return normally into
            # `create_connection` before a direct cancel discarded it,
            # `Task.cancel` on a task whose own awaited future is
            # already done forcing `CancelledError` in on its next step
            # regardless of what the future already held
            # (btclib-org/btclib-node#312, for a cancel arriving through
            # `server`'s own shield; btclib-org/btclib-node#353 and this
            # loop's own former blanket sweep, for one reaching that task
            # directly). `server` no longer has such a task to protect:
            # what it accepts sits in a queue instead, immune to that
            # discard regardless of when the cancel below reaches it
            # (btclib-org/btclib-node#386). `stop_handle.cancel()` above
            # already closed the other reason an earlier version of this
            # step existed, a `RuntimeError` this loop could raise
            # running `pending`'s own already-scheduled tasks on a loop
            # whose `run_forever` never delivered this method's own
            # `loop.stop` (btclib-org/btclib-node#377,
            # btclib-org/btclib-node#380) -- so neither of the two
            # reasons this step used to answer still applies.
            #
            # every one of them before the loop is allowed to run again,
            # rather than cancelling and draining one at a time, which is
            # what leaves the accept loop live for the whole drain
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    self.loop.run_until_complete(task)
        # Closed here and not by `server`'s own `with server_socket:`,
        # which is skipped outright where `stop` arrives before that task
        # has taken a first step: the cancellation is then thrown into a
        # coroutine that has no frame yet, exactly as it is for
        # `Connection.run` above, so the `with` is never entered and the
        # socket is left listening. A node stopped that soon after
        # `start` is where that happens. A `with` block that did run
        # leaves nothing here for `close()` to do, a socket being closed
        # only once whichever call reaches it first
        # (btclib-org/btclib-node#312).
        for server_socket in self._server_sockets:
            server_socket.close()
        self.loop.close()
        # so that the flag says what its name says: a socket
        # closed here is not one anything should wait for
        self.listening.clear()
        self.logger.info("Stopping P2P Manager")

    def send(self, msg: Payload, connection_id: int) -> None:
        """Send `msg` on `connection_id`, a no-op if that connection is gone."""
        # `.get()`, not `in` then `[...]`: `remove_connection` pops
        # from `connections` on this manager's own loop, off
        # `_prune_stale_connections`, every pass of `manage_connections`
        # -- a caller on `Node`'s own loop that passed the `in` and was
        # preempted before the subscript would otherwise see the
        # `KeyError` reach whatever called `send`. A connection missing
        # here means the peer is already gone by the time this runs, so
        # there is nothing to send it to and this is a no-op, the same
        # answer `download.py`'s own `_request_wanted_txs` gives a
        # `connections.get` that misses. btclib-org/btclib-node#359
        conn = self.connections.get(connection_id)
        if conn is not None:
            conn.send(msg)

    def broadcast_raw_transaction(self, tx: BtclibTx, fee: int) -> None:  # noqa: ARG002
        """Queue `tx` for the inv/getdata round trip, not a direct send.

        The comment below is where this, and `fee` going unread here,
        are argued.
        """
        # `DownloadManager.tx_download`'s own queue, with no peer to
        # exclude as already holding it, rather than a push of its own:
        # a direct, unsolicited `Tx` to every peer the instant this
        # method is called would have been the one thing that told
        # apart a transaction of this node's own from one it relayed --
        # the delay and the `inv`/`getdata` round trip are what a
        # relayed transaction gets, so a locally originated one goes
        # through them too. `getdata`'s own handler serves a `tx` it
        # finds in the mempool, so this call answers for what a peer
        # asks back only where the caller has already put it there --
        # `send_raw_transaction` (rpc/callbacks.py) does, before calling
        # this. btclib-org/btclib-node#141
        #
        # `fee` is accepted rather than read here: the same caller has
        # just recorded it in `node.mempool.add_tx(tx, fee)`, which is
        # where `tx_download`'s own BIP133 feefilter check
        # (`Mempool.meets_fee_rate`) reads it from, keyed by the same
        # wtxid this queues -- one record rather than a second copy of
        # it threaded through `received_txs` too. btclib-org/btclib-node#260
        self.node.download_manager.received_txs.append((None, tx.hash))

    def ping_all(self) -> None:
        """Send every connected peer a fresh `ping`."""
        for conn in self.connections.copy().values():
            conn.send_ping()

    def stop_all(self) -> None:
        """Stop every connection this manager holds, per the comment below."""
        # every socket this manager holds, handshake finished or not:
        # a peer mid-`verack` is still a peer to close on shutdown
        for conn in (
            *self.connections.copy().values(),
            *self.pending_connections.copy().values(),
        ):
            conn.stop()
