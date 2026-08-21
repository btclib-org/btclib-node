# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio
import re
import secrets
import time

from btclib_node.constants import NodeStatus, P2pConnStatus, ProtocolVersion, Services
from btclib_node.p2p.address import NetworkAddress
from btclib_node.p2p.callbacks import handshake_callbacks
from btclib_node.p2p.messages import WrongChecksumError, get_payload, verify_headers
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
        data = bytes.fromhex(self.node.chain.magic) + data
        try:
            await self.loop.sock_sendall(self.client, data)
        except OSError:  # probably connection dropped
            pass

    async def async_send(self, msg):
        self.node.logger.debug(f"Sending message: {msg.__class__.__name__}")

        try:
            serialized_message = msg.serialize()
        except Exception as e:
            self.node.logger.warning(f"error in serializing message: {e!s}")
        await self._send(serialized_message)
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
        while True:
            if not self.buffer:
                return
            try:
                verify_headers(self.buffer)
                self.last_receive = time.time()
                message_length = int.from_bytes(self.buffer[16:20], "little")
                message = get_payload(self.buffer)
                self.buffer = self.buffer[24 + message_length :]
                if message[0] in handshake_callbacks:
                    self.manager.handshake_messages.append((*message, self.id))
                elif message[0] in ("ping", "pong"):
                    self.manager.messages.appendleft((*message, self.id))
                else:
                    self.manager.messages.append((*message, self.id))
            except WrongChecksumError:
                # https://stackoverflow.com/questions/30945784/how-to-remove-all-characters-before-a-specific-character-in-python
                self.buffer = re.sub(
                    f"^.*?{self.node.chain.magic}".encode(),
                    self.node.chain.magic.encode(),
                    self.buffer,
                )
            except ValueError:
                return

    def __repr__(self):
        try:
            peer = self.client.getpeername()
            out = f"Connection to {peer[0]}:{peer[1]}"
        except OSError:
            out = "Broken connection"
        return out
