# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio

try:
    from http_parser.parser import HttpParser
except ImportError:
    from http_parser.pyparser import HttpParser
import json


class JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, bytes):
            return obj.hex()
        return super().default(obj)


class Connection:
    def __init__(self, loop, client, manager, id):
        super().__init__()
        self.loop = loop
        self.client = client
        self.manager = manager
        self.id = id
        self.rpc_id = ""
        self.messages = []
        self.buffer = b""
        self.task = None

    def close(self):
        if self.task:
            self.task.cancel()
        self.client.close()

    async def run(self):
        p = HttpParser()
        body = []
        try:
            while True:
                data = await self.loop.sock_recv(self.client, 1024)
                if not data:
                    break
                recved = len(data)
                nparsed = p.execute(data, recved)
                if nparsed != recved:
                    raise RuntimeError
                if p.is_partial_body():
                    body.append(p.recv_body())
                if p.is_message_complete():
                    break
            body = json.loads(body[0])
            if not isinstance(body, list):
                body = [body]
            self.manager.messages.append((body, self.id))
        except Exception:
            self.client.close()

    async def async_send(self, response):
        if len(response) == 1:
            response = response[0]
        output_str = json.dumps(response, separators=(",", ":"), cls=JSONEncoder)
        response = "HTTP/1.1 200 OK\n"
        response += "Content-Type: application/json\n"
        response += f"Content-Length: {len(output_str) + 1}\n"
        response += "\n"  # Important!
        response += output_str
        response += "\n"
        await self.loop.sock_sendall(self.client, response.encode())
        self.client.close()

    def send(self, response):
        asyncio.run_coroutine_threadsafe(self.async_send(response), self.loop)

    # Use with care
    def send_and_wait(self, response):
        future = asyncio.run_coroutine_threadsafe(self.async_send(response), self.loop)
        try:
            future.result(timeout=2)
        except TimeoutError:
            pass

    def __repr__(self):
        try:
            peer = self.client.getpeername()
            out = f"Connection to {peer[0]}:{peer[1]}"
        except OSError:
            out = "Broken connection"
        return out
