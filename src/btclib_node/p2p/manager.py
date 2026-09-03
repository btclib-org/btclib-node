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
from concurrent.futures import CancelledError
from contextlib import suppress
from typing import TYPE_CHECKING, override

from btclib.p2p.addrv2 import network_address

from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p.address import (
    PeerDB,
    dial,
    endpoint_key,
    ip_and_port,
    peer_address,
)
from btclib_node.p2p.connection import Connection

if TYPE_CHECKING:
    from concurrent.futures import Future

    from btclib.p2p.addrv2 import NetworkAddressV2
    from btclib.p2p.payload import Payload
    from btclib.tx.tx import Tx as BtclibTx

    from btclib_node import Node

__all__ = ["P2pManager"]

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

# `_maybe_redial_specified`'s own backoff for a `-connect`/`-addnode`
# peer that is not currently connected: doubled on every attempt made,
# reset to this floor the moment the peer is seen connected, capped at
# `_REDIAL_MAX_SECONDS`. Core keeps a whole thread apiece for this --
# `ThreadOpenConnections`'s own `-connect` arm, an uncapped
# `for (int64_t nLoop = 0;; nLoop++)` loop redialling every named peer
# with a per-peer sleep that grows to `10 * 500ms` and a flat `500ms`
# after each full pass (`src/net.cpp:2592-2625`, at
# bitcoin/bitcoin@ca7162cde5), and `ThreadOpenAddedConnections`, a
# `while (true)` loop over `GetAddedNodeInfo(include_connected=false)`
# -- the "already connected, skip it" filter `_maybe_redial_specified`
# below reproduces with its own `connected` set -- redialling every
# not-yet-connected added peer with a `500ms` sleep between each and a
# `60s` (something was tried) or `2s` (nothing was) sleep after the
# pass (`src/net.cpp:3052-3082`, same sha). This node has one loop
# already, `manage_connections`, running every 0.1s regardless of
# either flag; reusing it for both rather than adding two more standing
# coroutines is this tree's own Python-native shape of the same
# requirement, at the cost of one shared, capped, doubling backoff in
# place of replicating either of Core's own two cadences exactly.
_REDIAL_BASE_SECONDS = 1.0
_REDIAL_MAX_SECONDS = 60.0


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
        # Core's own `-connect`: dial only the peers it names, with DNS
        # seeding and every automatically-drawn outbound connection off
        # (`connOptions.m_use_addrman_outgoing = false`, `src/init.cpp`
        # `InitParameterInteraction`, at bitcoin/bitcoin@ca7162cde5).
        # `node.config.connect_given`, not `node.config.connect`'s own
        # truthiness: the two disagree under `-connect=0`, which is
        # still the `-connect` arm even though it dials nobody
        # (`Config`'s own field comment). Read once here rather than at
        # each call site below, so a `Config` a caller mutates after
        # building this manager cannot change which arm `run` and
        # `_maybe_dial_more_peers` take mid-flight.
        self.use_addrman_outgoing = not node.config.connect_given
        # Core's own `-listen`, read the same way and for the same
        # reason: whether `_bind` below runs at all, decided once here
        # rather than reread from a `Config` a caller could still
        # mutate underneath `run`.
        self.listen = node.config.listen

        # `-connect` and `-addnode` together, by `endpoint_key`: what
        # `_maybe_redial_specified` below redials once `Node.run`'s own
        # one-shot dial (`__init__.py`, issue #573) drops one of them.
        # Built once, here, for the same "a caller cannot change it
        # mid-flight" reason as the two fields above -- and a plain
        # `dict` rather than a `set`, since a redial needs the address
        # back, not only the key it is compared by.
        self._redial_peers: dict[bytes, NetworkAddressV2] = {
            endpoint_key(address): address
            for address in (
                *(peer_address(host, port) for host, port in node.config.connect),
                *(peer_address(host, port) for host, port in node.config.addnode),
            )
        }
        # Backoff state for the dict above, seeded in `run` rather than
        # here -- `run`'s own comment on `_redial_next` is where the
        # race this seeding avoids is argued.
        self._redial_backoff: dict[bytes, float] = dict.fromkeys(
            self._redial_peers, _REDIAL_BASE_SECONDS
        )
        self._redial_next: dict[bytes, float] = dict.fromkeys(self._redial_peers, 0.0)

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
        # (command, payload, connection id, wire size) -- the size,
        # `Connection.parse_messages`'s own addition since #462, is what
        # `handle_p2p`/`handle_p2p_handshake` (`p2p/main.py`) weigh back
        # off `queued_recv_bytes`, `MAX_QUEUED_RECV_BYTES`'s own comment
        # (`p2p/connection.py`) arguing why. `handshake_messages` is
        # drained whole every pass of `Node`'s own loop rather than
        # sharing `messages`'s own log2-scaled share
        # (btclib-org/btclib-node#462), and now paces its own reads
        # against the same bound `messages` does: the earlier scoping
        # away from it answered how long a backlog persists, not how
        # large one pass's own backlog could grow before draining it.
        # btclib-org/btclib-node#482
        #
        # Appended only from this manager's own event-loop thread --
        # every `Connection.run` coroutine, whichever connection it
        # belongs to, is multiplexed onto this one thread's asyncio loop
        # -- and popped only from `Node`'s, through
        # `Node._drain_message_queues`. Unlocked on both ends:
        # `deque.append`, `.appendleft` and `.popleft` are each wrapped
        # in their own `Py_BEGIN_CRITICAL_SECTION`/
        # `Py_END_CRITICAL_SECTION` (`Modules/_collectionsmodule.c` and
        # its clinic-generated wrapper, at python/cpython@f54fd2ab6e),
        # which locks the deque's own per-object mutex under a
        # free-threaded build and compiles to nothing under the ordinary
        # GIL one (`Include/critical_section.h`: "no-ops in
        # non-free-threaded builds") -- so a call from each thread can
        # never interleave its own mutation of the same deque with the
        # other's. What that does not cover, two threads calling the
        # same method on one deque at once, never happens here: this is
        # the only appender and `Node`'s thread the only popper.
        # btclib-org/btclib-node#484
        self.messages: deque[tuple[str, bytes, int, int]] = deque()
        self.handshake_messages: deque[tuple[str, bytes, int, int]] = deque()
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
        # land a connection into the live queue directly -- the seam a
        # bare `await loop.sock_accept` gave their own predecessors
        # before that fix, and gives `_accept_loop` again below
        # (btclib-org/btclib-node#430): what changed is what fills the
        # queue, a task rather than a reader callback, kept behind this
        # same queue so `server`'s own consumption of it is unaffected.
        # Nothing in this class reads it outside `server` and
        # `_accept_loop`.
        self._accept_queues: dict[
            socket.socket,
            asyncio.Queue[
                tuple[socket.socket, tuple[str, int] | tuple[str, int, int, int]]
            ],
        ] = {}

    def create_connection(
        self, client: socket.socket, address: NetworkAddressV2, *, inbound: bool
    ) -> None:
        """Build a `Connection` for `client`, hold it pending, and start it.

        Logs the id this connection is given beside the address it was
        accepted from or dialled to -- the one point every path into a
        connection shares, before any wire message is parsed, and so
        the only point at which a handshake exception raised before
        `callbacks.verack` reaches its own pairing (`p2p/callbacks.py`)
        still leaves this id resolvable to a peer. `verack`'s own line
        is not redundant with this one despite both naming an address:
        that one marks the handshake completing, this one marks the
        connection existing, and an operator reading `debug.log` wants
        both moments where a connection dies between them.
        btclib-org/btclib-node#611

        `network_address` never raises building that address here: an
        inbound `address` only ever comes from `peer_address` (`server`
        below), which only ever returns the two IP networks
        `network_address` accepts, and an outbound one only reaches
        this method once `dial` (`p2p/address.py`) has already returned
        a live socket for it, which `dial` itself never does for
        anything else (`UnsupportedAddressTypeError`) -- `random_address`
        (`p2p/address.py`) filtering `_maybe_dial_more_peers`'s own draw
        to the same two networks first is belt on top of that braces,
        not what does the guarding.

        `info`, matching `verack`'s own line: this runs once per
        connection actually made, dialled or accepted, never once per
        attempt -- `async_connect` and `_maybe_dial_more_peers` below
        only call this once `dial` has already returned a socket, so a
        dial that goes nowhere never reaches here to begin with.

        Unconditional on the address, like `verack`'s own line and for
        the same reason -- argued there rather than twice here: Core's
        analogous site, `CNode`'s own constructor (`src/net.cpp`, at
        bitcoin/bitcoin@05e49b342f), gates the address on `fLogIPs`.
        """
        client.settimeout(0.0)
        self.last_connection_id += 1
        endpoint = network_address(address)
        self.logger.info(
            "%s %s, connection %s",
            "Accepted" if inbound else "Dialled",
            ip_and_port(str(endpoint.ip), endpoint.port),
            self.last_connection_id,
        )
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
        # `-connect`'s own other half: `peer_db`'s table is never drawn
        # from at all, on top of `run` below never scheduling the DNS
        # lookup that would otherwise fill it. `Node.run` dials
        # `node.config.connect` directly through `connect()`, which does
        # not pass through here.
        if not self.use_addrman_outgoing:
            return
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

    async def _maybe_redial_specified(self) -> None:
        """Redial a `-connect`/`-addnode` peer not connected, on backoff.

        `_redial_peers` above is empty unless `Config.connect`/`addnode`
        named something, so this returns at once for every node that
        did not ask for either -- the ordinary case. A peer already in
        `connections` or `pending_connections` has its backoff reset to
        the floor and is left alone; one that is not, and whose own
        `_redial_next` has passed, is redialled and its backoff doubled
        (capped), the same as a peer this pass could not reach at all --
        distinguishing "reached but the handshake never got anywhere"
        from "could not even be dialled" is not something Core's own
        two loops above do either.
        """
        if not self._redial_peers:
            return
        now = time.time()
        with self._connections_lock:
            connected = {
                endpoint_key(conn.address)
                for conn in (
                    *self.connections.values(),
                    *self.pending_connections.values(),
                )
            }
        for key, address in self._redial_peers.items():
            if key in connected:
                self._redial_backoff[key] = _REDIAL_BASE_SECONDS
                continue
            if now < self._redial_next[key]:
                continue
            self._redial_next[key] = now + self._redial_backoff[key]
            self._redial_backoff[key] = min(
                self._redial_backoff[key] * 2, _REDIAL_MAX_SECONDS
            )
            try:
                await self.async_connect(address)
            except Exception:
                self.logger.exception("Exception occurred")

    async def manage_connections(self) -> None:
        """Prune, prune some more, maybe dial, sleep -- forever, every 0.1s.

        `_prune_stale_connections` pings or drops an idle peer every
        pass; `_maybe_prune_active_addresses` runs far less often;
        `_maybe_dial_more_peers` dials one more only if this node still
        has room for it; `_maybe_redial_specified` is the standing
        redial issue #651 asked for, for `-connect`/`-addnode` alone.
        """
        while True:
            now = time.time()
            self._prune_stale_connections(now)
            self._maybe_prune_active_addresses(now)
            await self._maybe_dial_more_peers()
            await self._maybe_redial_specified()
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

    async def _accept_loop(
        self,
        server_socket: socket.socket,
        accepted: asyncio.Queue[
            tuple[socket.socket, tuple[str, int] | tuple[str, int, int, int]]
        ],
    ) -> None:
        """`server`'s own producer: one kernel accept at a time, queued.

        A unit test can land a connection directly in `accepted` --
        `P2pManager._accept_queues` is kept for exactly that, `server`'s
        own docstring says where -- and cover this loop's `OSError` arm
        by handing it a socket that only duck-types `.accept()`, without
        a live listener.

        `loop.sock_accept` retries a `BlockingIOError`/`InterruptedError`
        internally on both loop families this node runs on (a selector
        loop's own reader callback, a Windows Proactor loop's overlapped
        `AcceptEx`) and raises anything else -- `ECONNABORTED` being the
        ordinary way, a peer resetting the connection between the kernel
        reporting it readable and the accept reaching it -- which is what
        the `except OSError` below answers exactly as `server`'s own
        former reader callback used to.

        `sock_accept`'s own internal future is already resolved with
        that exception by the time this coroutine's `await` reaches it
        where the failure is synchronous rather than a readiness wait --
        an unbound or otherwise permanently broken socket being the
        degenerate case -- so nothing suspends this task between one
        attempt and the next unless something makes it: the `sleep(0)`
        below is that yield, without which this loop would spin the
        thread's CPU core solid on such a socket and could never be
        cancelled, `Task.cancel` reaching a task only on its next step.
        """
        while True:
            try:
                sock, sockaddr = await asyncio.get_running_loop().sock_accept(
                    server_socket
                )
            except OSError:
                self.logger.exception("Accepting an inbound connection failed")
                await asyncio.sleep(0)
                continue
            sock.settimeout(0.0)
            accepted.put_nowait((sock, sockaddr))

    async def server(
        self, loop: asyncio.AbstractEventLoop, server_socket: socket.socket
    ) -> None:
        """Accept connections off `server_socket`, one `create_connection` each.

        Reads through `accepted`, an `asyncio.Queue` a task of its own,
        `_accept_loop`, fills -- rather than a bare
        `await loop.sock_accept(server_socket)` right here, which does
        not have the property the comment below argues for.
        """
        with server_socket:
            # The queue is what keeps a shutdown from discarding an
            # already-accepted socket reaching `server`'s own consumption
            # below: an item lands in its deque through `put_nowait`, a
            # plain call rather than an await, so nothing this
            # coroutine's own suspension on `accepted.get()` is cancelled
            # out of can ever lose an item already there -- the `finally`
            # closes whatever is left in it on whichever pass reaches
            # this task. That half of btclib-org/btclib-node#386 still
            # holds exactly as it did.
            #
            # What no longer holds is the other half, for `_accept_loop`
            # itself: `loop.add_reader`, which #386 chose over
            # `loop.sock_accept` because the reader callback it registers
            # is never itself a `Task.cancel` target, is not implemented
            # by Windows' own default Proactor loop
            # (btclib-org/btclib-node#430) -- so accepting there has to
            # go back through a task awaiting `loop.sock_accept`, and
            # that task is reachable by `stop`'s own blanket sweep over
            # `asyncio.all_tasks` exactly as #312's shielded task was:
            # `Task.cancel` cannot cancel a future that is already done,
            # so a cancel landing in the narrow window between the
            # kernel resolving one `sock_accept` and `_accept_loop`'s own
            # next step still throws `CancelledError` in regardless,
            # before that socket ever reaches `put_nowait`. CPython's own
            # reference counting is what bounds the cost of that window
            # rather than eliminating it: nothing else holds the
            # discarded socket once its frame unwinds, so it is closed by
            # its own `__del__` -- a `ResourceWarning`, not a leaked
            # descriptor -- in place of the graceful close `finally`
            # gives every item that did reach the queue. A peer that
            # dials in that exact instant of shutdown loses the
            # connection it just opened; nothing during ordinary
            # operation reaches this window at all, `_accept_loop` never
            # otherwise stopping.
            accepted: asyncio.Queue[
                tuple[socket.socket, tuple[str, int] | tuple[str, int, int, int]]
            ] = asyncio.Queue()
            self._accept_queues[server_socket] = accepted
            accept_task = loop.create_task(self._accept_loop(server_socket, accepted))
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
                # Already cancelled directly by `stop`'s own sweep
                # whenever that is how this task ends too -- both are in
                # the same `asyncio.all_tasks` snapshot -- so this is for
                # the caller that cancels `server` alone, such as a test
                # exercising it outside `stop`, where nothing else would
                # ever join this task.
                accept_task.cancel()
                with suppress(asyncio.CancelledError):
                    await accept_task
                del self._accept_queues[server_socket]
                while not accepted.empty():
                    accepted.get_nowait()[0].close()

    def _report_server_failure(self, future: Future[None]) -> None:
        """Log what `server`'s own scheduled task ends on, loudly.

        `run` below schedules `server` through `run_coroutine_threadsafe`
        and never awaits the `concurrent.futures.Future` it returns --
        deliberately, `server` running for this manager's whole lifetime
        rather than returning -- so an exception it raises before ever
        reaching its own accept loop would otherwise surface only
        through asyncio's own "Task exception was never retrieved"
        warning: timed to whenever the garbage collector reaches that
        future rather than to the failure itself, and written to the
        `asyncio` logger rather than this node's own.
        `btclib-org/btclib-node#88` fixed the identical shape for `_bind`
        by making the bind synchronous instead, which `server` cannot be,
        it being this manager's own listener for as long as it runs.

        `stop`'s own sweep ending this task on purpose is not logged, in
        two different ways depending on how the cancellation actually
        unwound: the ordinary one is `future` itself in the cancelled
        state, `Task.cancelled()` being true because `server` ended on
        the exact `CancelledError` `Task.cancel()` threw in, which
        `future.exception()` answers by raising rather than returning --
        the `with suppress` below is what that arm is. The `isinstance`
        arm below it is for a `CancelledError` `future.exception()`
        returns instead of raising: `server`'s own coroutine chain
        catching and re-raising a `CancelledError` that was not the one
        `Task.cancel()` threw would leave `Task.cancelled()` false while
        still ending on that same exception type, and this is what
        keeps that from being logged as a failure too.
        """
        with suppress(CancelledError):
            exc = future.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                self.logger.error("P2P listener's accept loop ended", exc_info=exc)

    @override
    def run(self) -> None:
        self.logger.info("Starting P2P manager")
        loop = self.loop
        asyncio.set_event_loop(loop)
        # Core's own `-listen=0`: no bind, no accept, outbound dialling
        # untouched -- `_bind`'s own listener socket is the only thing
        # this skips, `manage_connections` and the dial loop below both
        # running on this same loop regardless of whether `_bind` below
        # ever ran.
        server_sockets: list[socket.socket] = []
        if self.listen:
            try:
                server_sockets = self._bind()
            except OSError:
                self.logger.exception("Could not bind the P2P listener")
                raise
        self._server_sockets = server_sockets
        if self.use_addrman_outgoing:
            asyncio.run_coroutine_threadsafe(self.peer_db.get_addr_from_dns(), loop)
        for server_socket in server_sockets:
            asyncio.run_coroutine_threadsafe(
                self.server(loop, server_socket), loop
            ).add_done_callback(self._report_server_failure)
        # Seeded here, immediately before `manage_connections` is ever
        # scheduled, rather than at `__init__` time: `Node.run`'s own
        # one-shot dial for these same peers (`__init__.py`, issue
        # #573) races this manager's first `manage_connections` pass,
        # each reaching `async_connect` from a different thread, and a
        # peer `_redial_next` already called overdue by the time this
        # loop starts would sometimes win that race and dial a peer
        # `Node.run` is dialling in the same instant. A `__init__`-time
        # seed cannot answer that: an unknown, possibly long, gap sits
        # between building this manager and `start()` ever being
        # called on it.
        now = time.time()
        for key in self._redial_next:
            self._redial_next[key] = now + _REDIAL_BASE_SECONDS
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
            # what it accepts sits in a queue instead, and `server`'s own
            # consumption of that queue is immune to that discard
            # regardless of when the cancel below reaches it
            # (btclib-org/btclib-node#386). `_accept_loop`'s own
            # production side of the same queue is not -- `server`'s own
            # docstring has the reason and the bound on what it costs
            # (btclib-org/btclib-node#430). `stop_handle.cancel()` above
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
