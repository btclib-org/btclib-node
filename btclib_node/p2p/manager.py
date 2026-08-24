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

from btclib.fee import FeeRate, fee_from_vsize
from btclib.p2p.addrv2 import NetworkAddressV2
from btclib.p2p.data import TxPayload as Tx
from btclib.p2p.payload import Payload
from btclib.tx.tx import Tx as BtclibTx

from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p.address import PeerDB, dial, peer_address
from btclib_node.p2p.connection import Connection

if TYPE_CHECKING:
    from btclib_node import Node


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

        # Set once the listening socket is bound and can hold a peer's
        # connection in its backlog. `is_alive()` says only that this
        # thread was started, which is true before `run` below has
        # scheduled anything, so a peer that dials on the strength of it
        # is refused -- and `dial` answers a refusal with None, which
        # `async_connect` drops. Nothing retries.
        self.listening = threading.Event()

        self.loop = asyncio.new_event_loop()

    def create_connection(
        self, client: socket.socket, address: NetworkAddressV2, inbound: bool
    ) -> None:
        client.settimeout(0.0)
        self.last_connection_id += 1
        conn = Connection(self, client, address, self.last_connection_id, inbound)
        self.pending_connections[self.last_connection_id] = conn
        task = asyncio.run_coroutine_threadsafe(conn.run(), self.loop)
        conn.task = task

    def promote_connection(self, id: int) -> None:
        """Move a connection out of the handshake and into the herd.

        The only caller is `callbacks.verack`, right after it sets
        `P2pConnStatus.Connected` -- the two are one step, kept as two
        calls only because the status belongs to the connection and the
        dict it lives in belongs to the manager.
        """
        conn = self.pending_connections.pop(id, None)
        if conn is not None:
            self.connections[id] = conn

    def remove_connection(self, id: int) -> None:
        conn = self.connections.pop(id, None) or self.pending_connections.pop(id, None)
        if conn is not None:
            conn.stop()

    async def async_connect(self, address: NetworkAddressV2) -> None:
        client = await dial(address)
        if client:
            self.create_connection(client, address, False)

    def connect(self, address: NetworkAddressV2) -> None:
        asyncio.run_coroutine_threadsafe(self.async_connect(address), self.loop)

    async def manage_connections(self, loop: asyncio.AbstractEventLoop) -> None:
        while True:
            now = time.time()
            for conn in self.connections.copy().values():
                if conn.status == P2pConnStatus.Closed:
                    self.remove_connection(conn.id)
                if now - conn.last_receive > 120:
                    if not conn.ping_sent:
                        conn.send_ping()
                    elif now - conn.ping_sent > 120:
                        self.remove_connection(conn.id)
            for conn in self.pending_connections.copy().values():
                # The same idle bound, but no ping in between: `ping` is
                # as much a message the handshake has to clear before it
                # is sent as `inv` or `tx` is, so a connection stuck
                # short of `verack` is dropped once it goes quiet rather
                # than kept a second 120s waiting on an answer to
                # something #131 forbids sending it.
                if conn.status == P2pConnStatus.Closed or now - conn.last_receive > 120:
                    self.remove_connection(conn.id)
            if self.node.status < NodeStatus.HeaderSynced:
                connection_num = 1
            else:
                connection_num = 10
            live = len(self.connections) + len(self.pending_connections)
            if live < connection_num and not self.peer_db.is_empty:
                already_connected = [
                    conn.address
                    for conn in (
                        *self.connections.values(),
                        *self.pending_connections.values(),
                    )
                ]
                try:
                    address = self.peer_db.random_address()
                    # `is_empty` answers whether the table holds
                    # anything, not whether it holds anything this node
                    # can dial, so the guard above lets a table of ipv6
                    # and onion addresses through. The draw is what
                    # knows, and it answers with nothing: this pass has
                    # nothing to do, and the sleep below is what keeps
                    # that from being a spin.
                    if address is not None and address not in already_connected:
                        sock = await dial(address)
                        if sock:
                            self.create_connection(sock, address, False)
                except Exception:
                    self.logger.exception("Exception occurred")
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
                sock, sockaddr = await loop.sock_accept(server_socket)
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
        asyncio.run_coroutine_threadsafe(self.peer_db.get_addr_from_dns(), loop)
        for server_socket in server_sockets:
            asyncio.run_coroutine_threadsafe(self.server(loop, server_socket), loop)
        asyncio.run_coroutine_threadsafe(self.manage_connections(loop), loop)
        loop.run_forever()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        for conn in (
            *self.connections.copy().values(),
            *self.pending_connections.copy().values(),
        ):
            conn.stop()
        # `join` blocks this thread without spinning it, the way
        # `Node.stop` already waits on itself with `self.join`. Guarded
        # on `is_alive`, since `Node.run` calls this unconditionally --
        # a node with `p2p_port` unset never calls `start`, and `join`
        # on a thread that was never started raises.
        if self.is_alive():
            self.join()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                self.loop.run_until_complete(task)
        self.loop.close()
        # so that the flag says what its name says: a socket
        # closed here is not one anything should wait for
        self.listening.clear()
        self.logger.info("Stopping P2P Manager")

    def send(self, msg: Payload, id: int) -> None:
        if id in self.connections:
            self.connections[id].send(msg)

    def broadcast_raw_transaction(self, tx: BtclibTx, fee: int) -> None:
        # the peers that asked for transactions, and not all of them:
        # a peer whose version set BIP37's fRelay false said so about
        # transactions from anywhere, not only about the ones another
        # peer handed this node. `DownloadManager.tx_download` reads the
        # same flag for the same reason. What a broadcast to everyone
        # would have been right for is a message every peer is owed, and
        # a transaction is not one, which leaves `sendall` with nothing
        # to do.
        #
        # `fee` is the caller's own -- `rpc.callbacks.send_raw_transaction`
        # has just computed it out of `main.verify_mempool_acceptance` --
        # rather than looked up here, since a transaction can reach this
        # method before or without ever sitting in `node.mempool`.
        # BIP133: a peer's own advertised floor (`conn.feefilter`) is
        # honoured the same way `DownloadManager.tx_download` does.
        # btclib-org/btclib-node#260
        msg = Tx(tx, include_witness=True)
        for conn in self.connections.copy().values():
            if not conn.relay_tx:
                continue
            if conn.feefilter and fee < fee_from_vsize(
                tx.vsize, FeeRate(sats_per_kvbyte=conn.feefilter)
            ):
                continue
            conn.send(msg)

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
