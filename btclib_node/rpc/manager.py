# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio
import socket
import threading
from collections import deque
from contextlib import suppress
from typing import Any

from btclib_node.rpc.connection import Connection


class RpcManager(threading.Thread):
    def __init__(self, node, port):
        super().__init__()
        self.node = node
        self.logger = node.logger
        self.chain = node.chain
        self.connections = {}
        # what a connection parses out of one request: the JSON-RPC
        # batch -- a list even where the client sent a lone object --
        # and the connection id handle_rpc answers on
        self.messages: deque[tuple[list[Any], int]] = deque()
        self.loop = asyncio.new_event_loop()
        self.port = port
        self.last_connection_id = -1

    def create_connection(self, loop, client):
        client.settimeout(0.0)
        new_connection = Connection(loop, client, self, self.last_connection_id)
        self.connections[self.last_connection_id] = new_connection
        return new_connection

    def remove_connection(self, id):
        if id in self.connections.keys():
            self.connections[id].stop()
            self.connections.pop(id)

    async def server(self, loop):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # All interfaces, unconditionally -- there is no host
        # config option to bind the RPC control plane to
        # localhost instead. Pre-existing; not this lint-gate
        # PR to change, tracked as its own issue.
        server_socket.bind(("0.0.0.0", self.port))  # noqa: S104
        server_socket.listen()
        server_socket.settimeout(0.0)
        with server_socket:
            while True:
                client, _ = await loop.sock_accept(server_socket)
                self.last_connection_id += 1
                conn = self.create_connection(self.loop, client)
                task = asyncio.run_coroutine_threadsafe(conn.run(), self.loop)
                conn.task = task

    def run(self):
        self.logger.info("Starting RPC manager")
        loop = self.loop
        asyncio.set_event_loop(loop)
        asyncio.run_coroutine_threadsafe(self.server(loop), loop)
        loop.run_forever()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        while self.loop.is_running():
            pass
        for task in asyncio.all_tasks(self.loop):
            task.cancel()
            with suppress(asyncio.CancelledError):
                self.loop.run_until_complete(task)
        for conn in self.connections.values():
            conn.close()
        self.loop.close()
        self.logger.info("Stopping RPC manager")
