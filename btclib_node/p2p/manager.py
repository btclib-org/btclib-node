# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

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
    def __init__(self, node: Node, port: int | None, peer_db: PeerDB) -> None:
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
        # (command, payload, connection id), which is what a connection
        # appends and what p2p.main pops apart; the handshake ones go
        # in a queue of their own, drained whole before the rest.
        self.messages: deque[tuple[str, bytes, int]] = deque()
        self.handshake_messages: deque[tuple[str, bytes, int]] = deque()
        self.nonces: list[int] = []
        self.last_connection_id = -1
        # Endpoints `discourage` has been told to stop redialling, by
        # `endpoint_key` -- process lifetime, not `peer_db`'s own tables,
        # so a wrongly discouraged endpoint is recovered by a restart
        # rather than by touching the datadir, matching Core's own
        # `CRollingBloomFilter` (`banman.h`, bitcoin/bitcoin@58a7869f86)
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

    def create_connection(
        self, client: socket.socket, address: NetworkAddressV2, inbound: bool
    ) -> None:
        client.settimeout(0.0)
        self.last_connection_id += 1
        conn = Connection(self, client, address, self.last_connection_id, inbound)
        self.pending_connections[self.last_connection_id] = conn
        task = asyncio.run_coroutine_threadsafe(conn.run(), self.loop)
        conn.task = task

    def promote_connection(self, connection_id: int) -> None:
        """Move a connection out of the handshake and into the herd.

        The only caller is `callbacks.verack`, right after it sets
        `P2pConnStatus.Connected` -- the two are one step, kept as two
        calls only because the status belongs to the connection and the
        dict it lives in belongs to the manager.
        """
        conn = self.pending_connections.pop(connection_id, None)
        if conn is not None:
            self.connections[connection_id] = conn

    def remove_connection(self, connection_id: int) -> None:
        conn = self.connections.pop(
            connection_id, None
        ) or self.pending_connections.pop(connection_id, None)
        if conn is not None:
            conn.stop()

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
        client = await dial(address)
        if client:
            self.create_connection(client, address, False)

    def connect(self, address: NetworkAddressV2) -> None:
        asyncio.run_coroutine_threadsafe(self.async_connect(address), self.loop)

    def _prune_stale_connections(self, now: float) -> None:
        for conn in self.connections.copy().values():
            if conn.status == P2pConnStatus.Closed:
                self.remove_connection(conn.id)
            if now - conn.last_receive > _IDLE_TIMEOUT:
                if not conn.ping_sent:
                    conn.send_ping()
                elif now - conn.ping_sent > _IDLE_TIMEOUT:
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
        already_connected = {
            endpoint_key(conn.address)
            for conn in (
                *self.connections.values(),
                *self.pending_connections.values(),
            )
        }
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
                    self.create_connection(sock, address, False)
        except Exception:
            self.logger.exception("Exception occurred")

    async def manage_connections(self, loop: asyncio.AbstractEventLoop) -> None:
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
            self.logger.info(f"No IPv6 P2P listener on port {self.port}")
        self.listening.set()
        return sockets

    async def server(
        self, loop: asyncio.AbstractEventLoop, server_socket: socket.socket
    ) -> None:
        with server_socket:
            while True:
                # A task of its own and shielded, rather than a bare
                # `await loop.sock_accept(server_socket)`: that call's
                # own future can already carry a connection when `stop`
                # cancels this task, the kernel having handed one over
                # on a pass of the loop that has already run.
                # `Task.cancel` cannot cancel a future that is already
                # done, so it throws `CancelledError` in on the next
                # step rather than resuming with that result, and an
                # unshielded await loses the accepted socket with the
                # frame that unwinds, nothing else ever having held it.
                # Shielding keeps the accept running as a task of its
                # own, which the `except` below still has to read from
                # (btclib-org/btclib-node#312).
                accept = loop.create_task(loop.sock_accept(server_socket))
                try:
                    sock, sockaddr = await asyncio.shield(accept)
                except asyncio.CancelledError:
                    # This node has stopped listening, so a connection
                    # `accept` did land is closed here rather than given
                    # to `create_connection`. The two suppressed ends are
                    # the ones that leave nothing to close: the cancel
                    # reaching `accept` before the kernel did, and
                    # `accept()` itself refusing.
                    accept.cancel()
                    with suppress(asyncio.CancelledError, OSError):
                        (await accept)[0].close()
                    raise
                # two fields for an AF_INET peer, four for an AF_INET6
                # one -- the flow info and the scope id BIP155 has
                # nowhere to carry either, `get_addr_from_dns`'s own
                # sockaddr comment being where that is argued
                address = peer_address(*sockaddr[:2])
                self.create_connection(sock, address, True)

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
        asyncio.run_coroutine_threadsafe(self.manage_connections(loop), loop)
        loop.run_forever()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        # `join` blocks this thread without spinning it, the way
        # `Node.stop` already waits on itself with `self.join`. Guarded
        # on `is_alive`, since `Node.run` calls this unconditionally --
        # a node with `p2p_port` unset never calls `start`, and `join`
        # on a thread that was never started raises.
        if self.is_alive():
            self.join()
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
        if connection_id in self.connections:
            self.connections[connection_id].send(msg)

    def broadcast_raw_transaction(self, tx: BtclibTx, fee: int) -> None:
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
        for conn in self.connections.copy().values():
            conn.send_ping()

    def stop_all(self) -> None:
        # every socket this manager holds, handshake finished or not:
        # a peer mid-`verack` is still a peer to close on shutdown
        for conn in (
            *self.connections.copy().values(),
            *self.pending_connections.copy().values(),
        ):
            conn.stop()
