# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio
import secrets
import time
from io import BytesIO

from btclib.exceptions import BTClibValueError, IncompleteMessageError
from btclib.p2p.message import Message

from btclib_node.constants import NodeStatus, P2pConnStatus, ProtocolVersion, Services
from btclib_node.p2p.address import NetworkAddress
from btclib_node.p2p.callbacks import handshake_callbacks
from btclib_node.p2p.messages.handshake import Version
from btclib_node.p2p.messages.ping import Ping


class Connection:
    def __init__(self, manager, client, address, id, inbound):
        super().__init__()

        self.id = id
        self.manager = manager
        self.node = manager.node

        self.loop = manager.loop
        self.client = client
        self.address = address
        self.buffer = b""
        self.task = None

        self.status = P2pConnStatus.Open
        self.inbound = inbound

        self.version_message = None
        self.wtxidrelay_received = False

        self.relay_tx = True
        self.prefer_addressv2 = False

        self.last_receive = time.time()
        self.last_send = time.time()
        self.ping_nonce = None
        self.ping_sent = 0
        self.latency = 0

        self.download_queue = []
        self.pending_eviction = False
        self.last_block_timestamp = time.time()

    def stop(self, cancel_task=True):
        self.manager.peer_db.add_active_address(self.address)
        self.status = P2pConnStatus.Closed
        if self.task and cancel_task:
            self.task.cancel()
        self.client.close()

    async def run(self, connect=True):
        await self.send_version()
        while self.status < P2pConnStatus.Closed:
            data = await self.loop.sock_recv(self.client, 1024)
            if not data:
                return self.stop(cancel_task=False)
            try:
                self.buffer += data
                self.parse_messages()
            except Exception:
                return self.stop(cancel_task=False)

    async def _send(self, data):
        try:
            await self.loop.sock_sendall(self.client, data)
        except OSError:  # probably connection dropped
            pass

    async def async_send(self, payload):
        self.node.logger.debug(f"Sending message: {payload.command}")

        try:
            # the payload names its own command, and this is the only
            # place the magic is applied
            data = payload.to_message(self.node.chain.magic).serialize()
        except Exception as e:
            self.node.logger.warning(f"error in serializing message: {e!s}")
            return
        await self._send(data)
        self.last_send = time.time()

    def send(self, msg):
        asyncio.run_coroutine_threadsafe(self.async_send(msg), self.loop)

    async def send_version(self):
        services = Services.network + Services.witness + Services.network_limited
        nonce = secrets.randbelow(0xFFFFFFFFFFFF)
        self.manager.nonces.append(nonce)
        self.manager.nonces = self.manager.nonces[:10]

        version = Version(
            version=ProtocolVersion,
            services=services,
            timestamp=int(time.time()),
            addr_recv=self.address,
            addr_from=NetworkAddress(services=services, port=self.manager.port),
            nonce=nonce,
            user_agent="/Btclib/",
            start_height=0,
            relay=self.node.status == NodeStatus.BlockSynced,
        )
        await self.async_send(version)

    def send_ping(self):
        ping_msg = Ping()
        self.ping_sent = time.time()
        self.ping_nonce = ping_msg.nonce
        self.send(ping_msg)

    def parse_messages(self):
        # A stream and not the bytes: Message.parse consumes one message
        # and leaves the position after it, so several whole messages in
        # one read are taken one at a time, and a partial one rewinds.
        stream = BytesIO(self.buffer)
        try:
            while True:
                try:
                    message = Message.parse(stream)
                except IncompleteMessageError:
                    # the only refusal more octets can answer, and the
                    # stream is back at the start of the partial message
                    return
                if message.magic != self.node.chain.magic:
                    raise BTClibValueError(
                        f"message for another network: {message.magic.hex()}"
                    )
                self.last_receive = time.time()
                item = (message.command, message.payload, self.id)
                if message.command in handshake_callbacks:
                    self.manager.handshake_messages.append(item)
                elif message.command in ("ping", "pong"):
                    self.manager.messages.appendleft(item)
                else:
                    self.manager.messages.append(item)
        finally:
            # whatever the loop did not consume, partial message
            # included. Guarded on the position because the common read
            # off a socket completes no message at all: rewriting the
            # buffer there would copy it once more per 1024 octets, and
            # a block arrives in thousands of them.
            if stream.tell():
                self.buffer = stream.read()

    def __repr__(self):
        try:
            peer = self.client.getpeername()
            out = f"Connection to {peer[0]}:{peer[1]}"
        except OSError:
            out = "Broken connection"
        return out
