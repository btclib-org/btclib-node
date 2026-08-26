# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Connection`, one peer-to-peer socket and the messages framed over it.

Reads `btclib.p2p.message.Message`s off the wire and hands each one to
`P2pManager`, writes what `Node`'s own thread queues back out, and
bounds what it will buffer in either direction -- `MAX_GETCFILTERS_SIZE`
on a request it parses, and a send buffer capped the way Core's own
`-maxsendbuffer` caps one, per the comment beside that cap below.
"""

import asyncio
import contextlib
import secrets
import threading
import time
from io import BytesIO
from typing import TYPE_CHECKING, cast, override

from btclib.exceptions import BTClibException, IncompleteMessageError
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.p2p.handshake import Version
from btclib.p2p.keepalive import Ping
from btclib.p2p.limits import MAX_GETCFILTERS_SIZE, MAX_PROTOCOL_MESSAGE_LENGTH
from btclib.p2p.message import Message

from btclib_node.constants import P2pConnStatus, ProtocolVersion
from btclib_node.exceptions import WrongNetworkMagicError
from btclib_node.p2p.address import ip_and_port, network_address
from btclib_node.p2p.callbacks import handshake_callbacks

if TYPE_CHECKING:
    import socket
    from concurrent.futures import Future

    from btclib.p2p.addrv2 import NetworkAddressV2
    from btclib.p2p.payload import Payload

    from btclib_node import Node
    from btclib_node.p2p.manager import P2pManager


# Core's own cap, `-maxsendbuffer` (`src/net.h`'s
# `DEFAULT_MAXSENDBUFFER = 1 * 1000`, in the KB units
# `src/init.cpp`'s `nSendBufferMaxSize = 1000 *
# args.GetIntArg("-maxsendbuffer", DEFAULT_MAXSENDBUFFER)` turns into
# bytes) is not this node's own number: at that threshold Core sets
# `fPauseSend` (`net.cpp:4205`) and `ProcessMessages`/`ProcessGetData`
# (`net_processing.cpp:5438`, `:2774-2776`) stop generating further
# messages for that peer, but what is already in `vSendMsg` keeps
# draining past the cap rather than being cut off -- Core's own queue
# for an in-progress `getcfilters` answer routinely exceeds 1,000,000
# bytes while paused, because BIP157's own per-request bound reaches
# tens of megabytes on its own (`MAX_GETCFILTERS_SIZE`, `_filter_range`,
# this module's own answer to #101). A `Connection` that refuses to
# queue a message past a bound copied from `-maxsendbuffer` would drop
# a peer mid-response for asking for nothing out of spec, since this
# node has no message-processing stage separate from the handler that
# calls `send` once and is done: there is no later "next call" to pause
# and resume at the way `fPauseSend` is, so what queues here has to
# accommodate an entire legitimate answer, not merely start pausing
# where Core does.
#
# Bytes per filter element, measured rather than guessed: averaging
# `btclib.block.block_filter._golomb_encode` (`BASIC_FILTER_P=19`,
# `BASIC_FILTER_M=784931`) over synthetic element counts from 2,000 to
# `MAX_FILTER_ELEMENT_COUNT` gives about 2.632 bytes, stable across
# scales -- the cost is the Golomb-Rice parameter's, not the elements'.
#
# A real block anchors the element count instead of guessing that too:
# height 481824 (btclib's own `tests/block/_data/block_481824.bin`,
# 988,519 on-wire bytes) parses to 1,866 transactions, 4,124 outputs and
# 5,192 non-coinbase inputs -- 9,316 elements before the OP_RETURN
# exclusion and the deduplication `BasicBlockFilter.from_block` applies,
# both of which only lower the true count -- for about 24.5 KB of
# filter. That block is from 2017; four times its element count stands
# in for a block nearer today's without reaching for the 111,111-element
# theoretical ceiling (`MAX_FILTER_ELEMENT_COUNT`) itself, which no
# chain this node could serve has ever produced 1,000 of in a row.
_BYTES_PER_FILTER_ELEMENT = 2.632
_ELEMENTS_PER_BUSY_MODERN_BLOCK = 4 * 9316

# `MAX_GETCFILTERS_SIZE` (BIP157's own per-request bound, enforced by
# `_filter_range`) times that estimate is one legitimate `getcfilters`
# answer at its largest -- about 98 MB, the same order as the "tens of
# megabytes" the issue itself measured. Twice that is room for one such
# answer to drain in full and for a second one -- pipelined behind it,
# per the issue's other complaint, or simply the next request a peer
# sends without waiting for the first to finish -- to be under way as
# well, before a connection stops being plausibly one peer served within
# the protocol's own bounds.
MAX_QUEUED_SEND_BYTES = int(
    2
    * MAX_GETCFILTERS_SIZE
    * _ELEMENTS_PER_BUSY_MODERN_BLOCK
    * _BYTES_PER_FILTER_ELEMENT
)

# The wire header's own layout -- `btclib.p2p.message`'s module docstring
# argues it against Core's `CMessageHeader` (`src/protocol.h`): magic (4
# octets) and command (12) ahead of a little-endian `length` (4), then a
# checksum (4). `btclib.p2p.message` keeps the matching constants private,
# so `parse_messages` below repeats the two it needs to peek the header
# itself, rather than reach into another module's underscored names.
_HEADER_SIZE = 24
_LENGTH_OFFSET = 16
_LENGTH_SIZE = 4


class Connection:
    """One peer-to-peer socket and everything owed to or by it.

    The module docstring above is where its own three jobs -- framing,
    writing what `Node`'s thread queues, and bounding what it buffers --
    are argued.
    """

    def __init__(
        self,
        manager: P2pManager,
        client: socket.socket,
        address: NetworkAddressV2,
        connection_id: int,
        *,
        inbound: bool,
    ) -> None:
        """Set every field a fresh connection starts with, before `run`."""
        super().__init__()

        self.id = connection_id
        self.manager = manager
        self.node: Node = manager.node

        self.loop = manager.loop
        self.client: socket.socket = client
        self.address: NetworkAddressV2 = address
        # A `bytearray`, not `bytes`: `run`'s own `+=` below is an
        # in-place, amortised extend on this type and a full copy of
        # everything held so far on the other -- btclib-org/btclib-node#438.
        self.buffer = bytearray()
        self.task: Future[None] | None = None

        self.status: P2pConnStatus = P2pConnStatus.Open
        self.inbound: bool = inbound

        self.version_message: Version | None = None
        self.wtxidrelay_received: bool = False

        # BIP37's default until the peer's version says otherwise, which
        # is what callbacks.version writes here
        self.relay_tx: bool = True
        # sat/kvB, BIP133's own default of "no filter, everything relays"
        # until callbacks.feefilter writes here -- 0 is Core's own answer
        # for a rate it holds as no rate at all (btclib.fee.fee_from_vsize's
        # own docstring). Read by DownloadManager.tx_download and
        # P2pManager.broadcast_raw_transaction, through
        # Mempool.meets_fee_rate. btclib-org/btclib-node#260
        self.feefilter: int = 0
        self.prefer_addressv2: bool = False
        # set by callbacks.sendheaders, the peer's own request to be
        # announced a new block as a header rather than an inventory,
        # BIP130; Core's default is the same false until asked
        # (net_processing.cpp's m_prefers_headers). btclib-org/btclib-node#202
        self.prefers_headers: bool = False

        self.last_receive: float = time.time()
        self.last_send: float = time.time()
        self.ping_nonce: int | None = None
        self.ping_sent: float = 0
        self.latency: float = 0
        # `send_ping` (below) writes this pair from `P2pManager`'s own
        # loop, off `_prune_stale_connections`; `callbacks.pong` reads
        # and clears it from `Node`'s, off `handle_p2p`. Each is two
        # statements, not one, and unlocked the two threads' statements
        # can interleave into a ping outstanding under `ping_nonce ==
        # 0` -- the sentinel `send_ping`'s own comment is careful never
        # to send -- which reads as a peer answering a nonce it was
        # never sent and gets it discouraged (#283) and dropped for a
        # protocol violation this node caused. This lock is what makes
        # each of the two writes one step against the other's; `stop`
        # does not take it, since `stop` never touches either field --
        # what makes two concurrent `stop` calls harmless is argued at
        # `stop` itself, and is a different property from this one.
        # btclib-org/btclib-node#357
        self._ping_lock: threading.Lock = threading.Lock()

        self.download_queue: list[bytes] = []
        self.pending_eviction: bool = False
        self.last_block_timestamp: float = time.time()

        # Set by callbacks.getaddr the first time it answers this
        # connection: a peer that asks again gets nothing, rather than a
        # second answer from the cache. The cache already stops a fresh
        # draw per ask; what this flag alone still stops is a peer using
        # a loop of getaddr on the one connection to learn when this
        # node's cached sample itself changes. btclib-org/btclib-node#71
        self.answered_getaddr: bool = False

        # What `DownloadManager.tx_download` is waiting to tell this peer
        # about, and when it may next do so -- Core's `TxRelay` holds the
        # same two things per peer (`m_tx_inventory_to_send`,
        # `m_next_inv_send_time`) rather than announcing a transaction the
        # instant it is accepted. 0 is "never scheduled", which the first
        # check always treats as due. btclib-org/btclib-node#141
        self.tx_announce_queue: list[bytes] = []
        self.next_inv_send_time: float = 0.0
        # wtxid -> when this peer was asked for it, so a `notfound` this
        # node receives has something to clear and a second `getdata` for
        # the same wtxid is not sent while the first is still outstanding.
        # btclib-org/btclib-node#144
        self.tx_requested: dict[bytes, float] = {}

        # What this node last told this peer its own minimum relay
        # feerate is, and when it may next say so again -- Core's own
        # `Peer::m_fee_filter_sent`/`m_next_send_feefilter`
        # (`net_processing.cpp`, bitcoin/bitcoin@58a7869f86), both
        # initialized the same way there: 0 is a rate nothing is
        # withheld under, so the first comparison in
        # `DownloadManager._send_due_feefilters` never mistakes "never
        # sent" for "sent zero on purpose", and 0.0 is "never
        # scheduled", the same convention `next_inv_send_time` above
        # already uses. btclib-org/btclib-node#275
        self.feefilter_sent: int = 0
        self.next_feefilter_send_time: float = 0.0

        # What this connection currently owes the peer: every octet a
        # message has been serialized into and not yet handed to
        # `sock_sendall` in full, counted from `async_send` and not from
        # `send`, so a message still on another thread's way to the loop
        # is not double-counted against the one this thread is about to
        # queue. The lock is what makes "queued" true of the number: two
        # `async_send` calls racing `sock_sendall` on the same socket
        # would otherwise interleave their writes on the wire.
        self.queued_send_bytes: int = 0
        self.send_lock = asyncio.Lock()

    def stop(self, *, cancel_task: bool = True) -> None:
        """Close the socket and cancel `task`, idempotent on a repeat call.

        Called from `Node`'s own thread and from `P2pManager`'s alike;
        the comment below is where that and its own safety are argued.
        """
        if self.status == P2pConnStatus.Closed:
            # Already stopped: a peer over the send-buffer bound can
            # have several queued messages each independently discover
            # that on the same turn of the loop, each with its own call
            # to `stop`, before the first has had a chance to change
            # anything a later one could check instead. Idempotent
            # rather than counted on not to happen, since nothing
            # elsewhere in this class serializes who gets to call it --
            # and not only within one turn of this loop: `stop` is
            # called from Node's own thread as well as this manager's
            # (p2p/main.py's handle_p2p and handle_p2p_handshake,
            # callbacks.pong and every other callback that drops a peer
            # for cause, all on Node's; `_prune_stale_connections`
            # through `remove_connection`, on this manager's), so two
            # threads can pass this very guard on one connection before
            # either has written anything below. What makes that
            # harmless is not the guard, it is that the three
            # statements below are themselves idempotent: `self.status`
            # only ever moves toward `Closed`, so a second write of the
            # same value loses nothing; `self.task` is a
            # `concurrent.futures.Future` -- what
            # `asyncio.run_coroutine_threadsafe` returns and what
            # `P2pManager.create_connection` stores here, not an
            # `asyncio.Task` -- and a second `cancel()` on one already
            # `CANCELLED` takes that method's own early `if self._state
            # in [CANCELLED, CANCELLED_AND_NOTIFIED]: return True`
            # branch, before `_invoke_callbacks()` runs again: no
            # second attempt to cancel anything, whatever the call
            # returns (measured on this tree's own interpreter: `True`
            # both times, not `False` -- the state is already
            # `CANCELLED`, never `FINISHED`, so the branch that would
            # answer `False` is never the one taken); and a second
            # `socket.close()` on an already-closed socket raises
            # nothing, measured against a plain `socket.socket` and a
            # `socketpair()` half alike. Two racing callers each
            # running the body once is no different from one caller
            # running it twice. btclib-org/btclib-node#360
            return
        self.status = P2pConnStatus.Closed
        if self.task and cancel_task:
            self.task.cancel()
        self.client.close()

    async def run(self) -> None:
        """Send `version`, then read and dispatch messages until `stop`.

        Always ends in `stop`, whether by a graceful `return`, a caught
        exception, or the `finally` below catching a cancellation from
        outside this loop -- the comment above this method argues why.
        """
        # self.client is this coroutine's own resource, the same
        # guarantee P2pManager.server's own `with server_socket:` gives
        # its listening socket -- so a finally here, not only the
        # explicit stop() calls below. Every return above already goes
        # through stop(), which closes it; what a bare `return` would
        # not cover is this task cancelled directly rather than through
        # stop() -- P2pManager.stop()'s own final sweep, over
        # asyncio.all_tasks(self.loop), reaches a connection that way
        # whenever it was accepted or dialled after that same stop()'s
        # dict-based sweep over self.connections/self.pending_connections
        # already ran and missed it (btclib-org/btclib-node#312).
        # stop() is idempotent on an already-closed connection, so this
        # costs nothing on every other path, which already called it.
        try:
            await self.send_version()
            while self.status < P2pConnStatus.Closed:
                # 64 KB, matching Core's own read buffer (`pchBuf`,
                # `src/net.cpp`) rather than the 1024 this had no
                # argument for: fewer syscalls, and -- quadratically,
                # through `parse_messages`'s own gate below -- far fewer
                # bytes copied per message. btclib-org/btclib-node#438
                data = await self.loop.sock_recv(self.client, 65536)
                if not data:
                    return self.stop(cancel_task=False)
                try:
                    self.buffer += data
                    self.parse_messages()
                # deliberately blind (BLE001), not for the event loop's
                # own sake: `run` reaches this coroutine through
                # `run_coroutine_threadsafe`, whose own Future nothing
                # here ever reads, so an unhandled exception neither
                # crashes `P2pManager`'s loop nor any other connection
                # on it -- asyncio isolates that much on its own. What
                # this catch buys instead is one explicit outcome for
                # every failure this loop can hit: a bug in this node's
                # own parsing reaches the same `stop()` below that a
                # peer's bad envelope does, in the one place that
                # decides it, rather than falling through to the outer
                # `finally` by coincidence with nothing having looked at
                # it
                except Exception as e:  # noqa: BLE001
                    # A `BTClibException` is `Message.parse` (or the
                    # network-magic check right after it) refusing this
                    # peer's own envelope -- a bad checksum, an oversized
                    # length, a message for another network. Anything else
                    # caught here is this node's own bug, not the peer's
                    # doing. btclib-org/btclib-node#283
                    if isinstance(e, BTClibException):
                        self.manager.discourage(self.address)
                    return self.stop(cancel_task=False)
        finally:
            self.stop(cancel_task=False)

    async def _send(self, data: bytes) -> None:
        with contextlib.suppress(OSError):  # probably connection dropped
            await self.loop.sock_sendall(self.client, data)

    async def async_send(self, payload: Payload) -> None:
        """Frame `payload` and send it, dropping the connection past the bound.

        Counts what it queues into `queued_send_bytes` before awaiting
        the write, and refuses to queue at all -- stopping the
        connection instead -- once that plus this message would exceed
        `MAX_QUEUED_SEND_BYTES`.
        """
        self.node.logger.debug("Sending message: %s", payload.command)

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
        # deliberately blind (BLE001): this is called for every message
        # this node ever sends, callers throughout src/btclib_node/p2p and
        # src/btclib_node/download.py among them, so a bug serializing one
        # payload logs and drops that one send rather than propagating
        # into an arbitrary caller's own control flow
        except Exception as e:  # noqa: BLE001
            self.node.logger.warning("error in serializing message: %s", e)
            return

        if self.queued_send_bytes + len(data) > MAX_QUEUED_SEND_BYTES:
            # Not queued at all, so this message never reaches
            # `queued_send_bytes`: a peer already over budget gets
            # dropped rather than pushed further past it. `stop`
            # cancels `self.task`, the recv loop, so nothing more is
            # read from this peer either.
            self.node.logger.warning(
                "send buffer bound exceeded, dropping connection: %r", self
            )
            self.stop()
            return

        self.queued_send_bytes += len(data)
        try:
            async with self.send_lock:
                await self._send(data)
        finally:
            self.queued_send_bytes -= len(data)
        self.last_send = time.time()

    def send(self, msg: Payload) -> None:
        """Schedule `async_send(msg)` onto this connection's own loop.

        The synchronous entry point, safe to call from any thread:
        `run_coroutine_threadsafe` is what lets both `Node`'s own thread
        (through the `p2p.callbacks` handlers) and `P2pManager`'s own
        (through `send_ping`) reach it without ever awaiting directly.
        """
        asyncio.run_coroutine_threadsafe(self.async_send(msg), self.loop)

    async def send_version(self) -> None:
        """Build and send this node's own `version` message."""
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
        self.manager.nonces = self.manager.nonces[-10:]

        # A connection exists only once P2pManager.start() has run, and
        # that only happens with a port to listen on (Node.run guards
        # it on self.p2p_port): the type is wider than the invariant,
        # so this is a cast rather than a check that would be dead code
        # on every path that reaches here.
        port = cast("int", self.manager.port)
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
            addr_from=NetworkAddress(services=services, port=port),
            nonce=nonce,
            # octets and not text: Core reads the subversion into a
            # string it sanitizes only for the log, so btclib carries
            # what the peer sent rather than what decodes
            user_agent=b"/Btclib/",
            start_height=0,
            # Core's own `fRelay` is about the connection -- a
            # block-relay-only peer, a feeler, `-blocksonly`
            # (`RejectIncomingTxs`, src/net_processing.cpp) -- and never
            # about `IsInitialBlockDownload()`. None of this node's
            # connections are any of those, so this is always True and
            # never has to be revised once the node catches up: what a
            # peer sends before then is dropped on arrival instead,
            # `p2p/callbacks.tx`. btclib-org/btclib-node#129
            relay=True,
        )
        await self.async_send(version)

    def send_ping(self) -> None:
        """Send a `ping` with a fresh nonzero nonce, recording it under lock.

        Called from `Node`'s own thread, twice over (`callbacks.verack`
        once a handshake completes, and `rpc.callbacks.ping` through
        `ping_all`), and from `P2pManager`'s own thread once
        (`manage_connections`); `_ping_lock` is what keeps its own two
        writes one step against `callbacks.pong`'s read and clear.
        """
        # The nonce is the sender's to choose, and btclib's Ping defaults
        # it to zero rather than drawing one. Zero is also what
        # ping_nonce means "no ping outstanding", so it is drawn here and
        # never zero: a ping carrying the sentinel would make the pong
        # that answers it indistinguishable from no pong at all.
        ping_msg = Ping(1 + secrets.randbelow(2**64 - 1))
        with self._ping_lock:
            self.ping_sent = time.time()
            self.ping_nonce = ping_msg.nonce
        self.send(ping_msg)

    def parse_messages(self) -> None:
        """Parse every whole message in `buffer`, queueing each on the manager.

        Leaves a trailing partial message in `buffer` for the next
        read, and routes each parsed one to `handshake_messages` or
        `messages` -- `ping`/`pong` pushed to the front of the latter.

        Peeks the header's own `length` field in `buffer` before
        building a stream or calling `Message.parse` at all: a chunk
        that does not yet complete even the first message in `buffer`
        returns here without copying anything. That is the common case
        on a connection carrying one large message over many reads --
        a block during initial block download chief among them -- and
        it is what keeps such a message copied a constant number of
        times overall rather than once per chunk. btclib-org/btclib-node#438
        """
        if len(self.buffer) < _HEADER_SIZE:
            return
        length = int.from_bytes(
            self.buffer[_LENGTH_OFFSET : _LENGTH_OFFSET + _LENGTH_SIZE],
            byteorder="little",
        )
        # `length` above the protocol's own bound falls through instead
        # of waiting for however many further octets it claims: nothing
        # this node could ever receive completes such a message, and
        # `Message.parse` below refuses it the moment it reads the
        # header -- the same refusal a peer telling the truth about a
        # too-large message would get once its payload actually arrived,
        # just not deferred until then.
        if length <= MAX_PROTOCOL_MESSAGE_LENGTH and len(self.buffer) < (
            _HEADER_SIZE + length
        ):
            return

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
                    raise WrongNetworkMagicError(message.magic)
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
            # included. The gate above already returned without
            # touching `stream` for a read that completes no message at
            # all, so this only copies the (typically short) remainder
            # once a message has actually been taken off the front.
            if stream.tell():
                self.buffer = bytearray(stream.read())

    @override
    def __repr__(self) -> str:
        try:
            peer = self.client.getpeername()
            out = f"Connection to {ip_and_port(peer[0], peer[1])}"
        except OSError:
            out = "Broken connection"
        return out
