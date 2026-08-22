# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio
import secrets
import time
from io import BytesIO

from btclib.exceptions import BTClibValueError, IncompleteMessageError
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.p2p.handshake import Version
from btclib.p2p.keepalive import Ping
from btclib.p2p.message import Message

from btclib_node.constants import NodeStatus, P2pConnStatus, ProtocolVersion
from btclib_node.p2p.address import network_address
from btclib_node.p2p.callbacks import handshake_callbacks


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

        # BIP37's default until the peer's version says otherwise, which
        # is what callbacks.version writes here
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
            # The payload names its own command, and this is the only
            # place the magic is applied.
            #
            # Its octets are not re-checked on the way out: this node
            # built them from state it has already validated, and
            # btclib's block payload would ask CheckBlock of them
            # against mainnet's pow limit, which no regtest or signet
            # block meets. The envelope still is: that check is about
            # the octets this node emits being well formed -- a magic of
            # four octets, a command of at most twelve printable ones, a
            # length under the protocol's. It says nothing about whether
            # the command is one any peer answers to; the test over every
            # payload's `command` is what says that.
            message = Message(
                self.node.chain.magic,
                payload.command,
                payload.serialize(check_validity=False),
            )
            data = message.serialize()
        except Exception as e:
            self.node.logger.warning(f"error in serializing message: {e!s}")
            return
        await self._send(data)
        self.last_send = time.time()

    def send(self, msg):
        asyncio.run_coroutine_threadsafe(self.async_send(msg), self.loop)

    async def send_version(self):
        # compact_filters is BIP157's NODE_COMPACT_FILTERS, and saying
        # it promises an answer to getcfilters, getcfheaders and
        # getcfcheckpt for every block of the chain. The filter index is
        # caught up before the node starts listening and kept up as
        # blocks connect, so the promise holds whenever this is sent.
        services = (
            ServiceFlags.NODE_NETWORK
            | ServiceFlags.NODE_WITNESS
            | ServiceFlags.NODE_COMPACT_FILTERS
            | ServiceFlags.NODE_NETWORK_LIMITED
        )
        # over the whole 64-bit field, as Core draws it: this nonce is
        # how a node recognises a connection to itself, so a narrower
        # draw is a narrower guarantee of that
        nonce = secrets.randbelow(2**64)
        self.manager.nonces.append(nonce)
        self.manager.nonces = self.manager.nonces[:10]

        version = Version(
            version=ProtocolVersion,
            services=services,
            timestamp=int(time.time()),
            # a `version` message's address carries no timestamp, which
            # is what the narrowest of btclib's address types is
            addr_recv=network_address(self.address),
            # the default address, which btclib spells `::` where this
            # node used to write the v4-mapped `::ffff:0.0.0.0`. Core's
            # own `addrMe` is a default CService, which is the sixteen
            # zero octets btclib writes, and no peer reads the field:
            # Core has ignored it since it started learning its own
            # address elsewhere
            addr_from=NetworkAddress(services=services, port=self.manager.port),
            nonce=nonce,
            # octets and not text: Core reads the subversion into a
            # string it sanitizes only for the log, so btclib carries
            # what the peer sent rather than what decodes
            user_agent=b"/Btclib/",
            start_height=0,
            relay=self.node.status == NodeStatus.BlockSynced,
        )
        await self.async_send(version)

    def send_ping(self):
        # The nonce is the sender's to choose, and btclib's Ping defaults
        # it to zero rather than drawing one. Zero is also what
        # ping_nonce means "no ping outstanding", so it is drawn here and
        # never zero: a ping carrying the sentinel would make the pong
        # that answers it indistinguishable from no pong at all.
        ping_msg = Ping(1 + secrets.randbelow(2**64 - 1))
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
