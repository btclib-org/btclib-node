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
        self.connections[self.last_connection_id] = conn
        task = asyncio.run_coroutine_threadsafe(conn.run(), self.loop)
        conn.task = task

    def remove_connection(self, id: int) -> None:
        if id in self.connections.keys():
            self.connections[id].stop()
            self.connections.pop(id)

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
            if self.node.status < NodeStatus.HeaderSynced:
                connection_num = 1
            else:
                connection_num = 10
            if len(self.connections) < connection_num and not self.peer_db.is_empty:
                already_connected = [conn.address for conn in self.connections.values()]
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

    def _bind(self) -> socket.socket:
        """Bind and listen, synchronously, before anything is scheduled.

        Not the coroutine below: a coroutine handed to
        `run_coroutine_threadsafe` runs on the loop's own thread, behind a
        `concurrent.futures.Future` nobody reads, so a bind failure inside
        one is an `OSError` that vanishes rather than one that reaches
        `run`'s caller (#88). Doing it here instead, before `run_forever`
        is ever called, means the same failure raises out of `run` --
        this thread's target -- so the thread ends rather than staying
        `is_alive()` over a listener that never came up.
        """
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # All interfaces, by design: a P2P listener accepts
            # inbound peers from anywhere. noqa: S104
            server_socket.bind(("0.0.0.0", self.port))  # noqa: S104
            server_socket.listen()
            server_socket.settimeout(0.0)
        except OSError:
            # the caller never gets this socket to close: raising it
            # out of a coroutine nobody awaited (#88) is what let a
            # failed bind's fd outlive the exception in the first place
            server_socket.close()
            raise
        self.listening.set()
        return server_socket

    async def server(
        self, loop: asyncio.AbstractEventLoop, server_socket: socket.socket
    ) -> None:
        with server_socket:
            while True:
                sock, sockaddr = await loop.sock_accept(server_socket)
                address = peer_address(*sockaddr)
                self.create_connection(sock, address, True)

    @override
    def run(self) -> None:
        self.logger.info("Starting P2P manager")
        loop = self.loop
        asyncio.set_event_loop(loop)
        try:
            server_socket = self._bind()
        except OSError:
            self.logger.exception("Could not bind the P2P listener")
            raise
        asyncio.run_coroutine_threadsafe(self.peer_db.get_addr_from_dns(), loop)
        asyncio.run_coroutine_threadsafe(self.server(loop, server_socket), loop)
        asyncio.run_coroutine_threadsafe(self.manage_connections(loop), loop)
        loop.run_forever()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        for conn in self.connections.copy().values():
            conn.stop()
        while self.loop.is_running():
            pass
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

    def broadcast_raw_transaction(self, tx: BtclibTx) -> None:
        # the peers that asked for transactions, and not all of them:
        # a peer whose version set BIP37's fRelay false said so about
        # transactions from anywhere, not only about the ones another
        # peer handed this node. `DownloadManager.tx_download` reads the
        # same flag for the same reason. What a broadcast to everyone
        # would have been right for is a message every peer is owed, and
        # a transaction is not one, which leaves `sendall` with nothing
        # to do.
        msg = Tx(tx, include_witness=True)
        for conn in self.connections.copy().values():
            if conn.relay_tx:
                conn.send(msg)

    def ping_all(self) -> None:
        for conn in self.connections.copy().values():
            conn.send_ping()

    def stop_all(self) -> None:
        for conn in self.connections.copy().values():
            conn.stop()
