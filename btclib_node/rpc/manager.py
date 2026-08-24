# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio
import socket
import threading
from collections import deque
from concurrent.futures import Future
from contextlib import suppress
from typing import TYPE_CHECKING, Any, override

from btclib_node.rpc.connection import Connection

if TYPE_CHECKING:
    from btclib_node import Node


class RpcManager(threading.Thread):
    def __init__(self, node: Node, port: int | None) -> None:
        super().__init__()
        self.node = node
        self.logger = node.logger
        self.chain = node.chain
        self.connections: dict[int, Connection] = {}
        # what a connection parses out of one request: the JSON-RPC
        # batch -- a list even where the client sent a lone object --
        # and the connection id handle_rpc answers on
        self.messages: deque[tuple[list[Any], int]] = deque()
        self.loop = asyncio.new_event_loop()
        self.port = port
        self.last_connection_id = -1

        # see P2pManager.listening: `is_alive()` is true before `run`
        # has bound anything, and a client that posts on the strength of
        # it is refused
        self.listening = threading.Event()
        # What `run` binds and `stop` closes. `server`'s own
        # `with server_socket:` ordinarily closes this once `stop`'s
        # cancellation reaches that task -- except where `stop` arrives
        # before `run_forever` has stepped that task even once: a
        # coroutine `cancel()` reaches with no frame yet raises
        # `CancelledError` at its own definition point rather than
        # inside the running body, so `with server_socket:` is never
        # entered at all (btclib-org/btclib-node#323). Read only by
        # `run` and `stop`, both on this manager's own object and never
        # concurrently -- `run` sets it once, from this thread, before
        # `stop` could possibly be reached by another.
        self._server_socket: socket.socket | None = None

    def create_connection(
        self, loop: asyncio.AbstractEventLoop, client: socket.socket
    ) -> Connection:
        client.settimeout(0.0)
        new_connection = Connection(loop, client, self, self.last_connection_id)
        self.connections[self.last_connection_id] = new_connection
        return new_connection

    def _bind(self) -> socket.socket:
        """Bind and listen, synchronously, before anything is scheduled.

        See `P2pManager._bind`: the same shape of bug (#88) and the same
        fix, applied to the RPC listener instead of the P2P one.
        """
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Config.rpc_host, "127.0.0.1" unless a caller asks
            # otherwise -- see its own docstring for why the RPC
            # control plane's default is not every interface, unlike
            # P2pManager's
            server_socket.bind((self.node.config.rpc_host, self.port))
            server_socket.listen()
            server_socket.settimeout(0.0)
        except OSError:
            server_socket.close()
            raise
        self.listening.set()
        return server_socket

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
                # (btclib-org/btclib-node#323).
                accept = loop.create_task(loop.sock_accept(server_socket))
                try:
                    client, _ = await asyncio.shield(accept)
                except asyncio.CancelledError:
                    # This server has stopped listening, so a
                    # connection `accept` did land is closed here
                    # rather than given to `create_connection`. The two
                    # suppressed ends are the ones that leave nothing
                    # to close: the cancel reaching `accept` before the
                    # kernel did, and `accept()` itself refusing.
                    accept.cancel()
                    with suppress(asyncio.CancelledError, OSError):
                        (await accept)[0].close()
                    raise
                self.last_connection_id += 1
                conn = self.create_connection(self.loop, client)
                task: Future[None] = asyncio.run_coroutine_threadsafe(
                    conn.run(), self.loop
                )
                conn.task = task

    @override
    def run(self) -> None:
        self.logger.info("Starting RPC manager")
        loop = self.loop
        asyncio.set_event_loop(loop)
        try:
            server_socket = self._bind()
        except OSError:
            self.logger.exception("Could not bind the RPC listener")
            raise
        self._server_socket = server_socket
        asyncio.run_coroutine_threadsafe(self.server(loop, server_socket), loop)
        loop.run_forever()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        # `join` blocks this thread without spinning it, the way
        # `Node.stop` already waits on itself with `self.join`. Guarded
        # on `is_alive`, since `Node.run` calls this unconditionally --
        # a node with `rpc_port` unset never calls `start`, and `join`
        # on a thread that was never started raises. See
        # `P2pManager.stop` for the same fix, applied there first.
        if self.is_alive():
            self.join()
        # Cancelled here, as its own pass over `pending`, before any of
        # them is driven to completion below -- not folded into one
        # combined loop. `run_until_complete(task)`, for any one task,
        # drives the *whole* loop, not only that task: under a single
        # combined pass, `server`'s own accept loop -- not yet reached
        # by this loop's own cancellation -- keeps accepting while an
        # earlier task's cancellation is delivered, landing a task that
        # is not in this snapshot and is never itself cancelled, only
        # reported destroyed while still pending
        # (btclib-org/btclib-node#323). This closes log noise rather
        # than a leak: the sweep below already reaches such a
        # connection's own socket, since it runs after this loop and
        # `create_connection` puts every connection straight into
        # `self.connections` rather than a dict of its own -- unlike
        # `P2pManager`, whose own connections sweep runs *before* this
        # same loop and so cannot.
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                self.loop.run_until_complete(task)
        for conn in self.connections.values():
            conn.close()
        # Closed explicitly and unconditionally, after the loop above
        # rather than instead of it: `server`'s own `with server_socket:`
        # is what ordinarily closes this, once that task's own
        # cancellation is delivered and the exception propagates out of
        # `sock_accept` through the `with`. Measured to be skipped
        # entirely where `stop()` arrives before `run_forever` has
        # stepped that task even once -- `_server_socket`'s own comment
        # has the mechanism, and this is what closes what that path does
        # not reach; a `with` block already run leaves nothing here for
        # `close()` to do, since a socket is closed only once, whichever
        # call reaches it first.
        if self._server_socket is not None:
            self._server_socket.close()
        self.loop.close()
        # so that the flag says what its name says: a socket
        # closed here is not one anything should wait for
        self.listening.clear()
        self.logger.info("Stopping RPC manager")
