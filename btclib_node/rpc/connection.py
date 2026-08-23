# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio
import json
import socket
from collections.abc import Callable
from concurrent.futures import Future
from http.client import parse_headers
from io import BytesIO
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from btclib_node.rpc.manager import RpcManager

HEADER_TERMINATOR = b"\r\n\r\n"
# Bounds on the read below, which is fed by whoever connects: the RPC
# socket is bound to every interface (see rpc.manager.RpcManager.server),
# so an unterminated header section or an overstated Content-Length must
# not grow the buffer without limit. Both are generous next to a real
# JSON-RPC request -- headers run to a few hundred bytes, and the largest
# body this node is sent is a raw transaction.
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024


class JSONEncoder(json.JSONEncoder):
    def default(self, obj: object) -> Any:
        if isinstance(obj, bytes):
            return obj.hex()
        return super().default(obj)


class Connection:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: socket.socket,
        manager: RpcManager,
        id: int,
    ) -> None:
        super().__init__()
        self.loop = loop
        self.client = client
        self.manager = manager
        self.id = id
        self.rpc_id = ""
        self.messages: list[Any] = []
        self.buffer = b""
        self.task: Future[None] | None = None

    def close(self) -> None:
        if self.task:
            self.task.cancel()
        self.client.close()

    async def _recv_until(
        self, predicate: Callable[[], bool], max_bytes: int | None = None
    ) -> None:
        while not predicate():
            if max_bytes is not None and len(self.buffer) > max_bytes:
                raise ConnectionError
            data = await self.loop.sock_recv(self.client, 1024)
            if not data:
                raise ConnectionError
            self.buffer += data

    async def run(self) -> None:
        try:
            await self._recv_until(
                lambda: HEADER_TERMINATOR in self.buffer, MAX_HEADER_BYTES
            )
            head, _, self.buffer = self.buffer.partition(HEADER_TERMINATOR)
            # parse_headers wants the field lines alone, so drop the
            # request line, and the blank line partition() consumed.
            _, _, fields = head.partition(b"\r\n")
            headers = parse_headers(BytesIO(fields + HEADER_TERMINATOR))
            length = int(headers.get("Content-Length", 0))
            # int() admits a negative, which would make the predicate
            # below true before a single body byte arrived and then
            # slice the body from the wrong end.
            if not 0 <= length <= MAX_BODY_BYTES:
                raise ConnectionError
            await self._recv_until(lambda: len(self.buffer) >= length)

            body = json.loads(self.buffer[:length])
            if not isinstance(body, list):
                body = [body]
            self.manager.messages.append((body, self.id))
        except Exception:
            self.client.close()

    async def async_send(self, response: list[dict[str, Any]]) -> None:
        body: list[dict[str, Any]] | dict[str, Any] = response
        if len(response) == 1:
            body = response[0]
        output_str = json.dumps(body, separators=(",", ":"), cls=JSONEncoder)
        # CRLF, which is what run() above requires of a request and what
        # HTTP/1.1 specifies: this server should not emit framing it
        # would itself refuse to read.
        http_response = "HTTP/1.1 200 OK\r\n"
        http_response += "Content-Type: application/json\r\n"
        http_response += f"Content-Length: {len(output_str) + 1}\r\n"
        http_response += "\r\n"  # Important!
        http_response += output_str
        http_response += "\n"
        await self.loop.sock_sendall(self.client, http_response.encode())
        self.client.close()

    def send(self, response: list[dict[str, Any]]) -> None:
        asyncio.run_coroutine_threadsafe(self.async_send(response), self.loop)

    # Use with care
    def send_and_wait(self, response: list[dict[str, Any]]) -> None:
        future = asyncio.run_coroutine_threadsafe(self.async_send(response), self.loop)
        try:
            future.result(timeout=2)
        except TimeoutError:
            pass

    def __repr__(self) -> str:
        try:
            peer = self.client.getpeername()
            out = f"Connection to {peer[0]}:{peer[1]}"
        except OSError:
            out = "Broken connection"
        return out
