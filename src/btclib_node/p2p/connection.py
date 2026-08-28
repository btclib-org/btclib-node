# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Connection`, one peer-to-peer socket and the messages framed over it.

Reads `btclib.p2p.message.Message`s off the wire and hands each one to
`P2pManager`, writes what `Node`'s own thread queues back out, and
bounds what it will buffer in either direction -- `MAX_PROTOCOL_MESSAGE_LENGTH`
on what any one message may claim to be, `MAX_QUEUED_RECV_BYTES` on how
much of what this connection has already handed to `P2pManager.messages`
or `P2pManager.handshake_messages` may sit there unprocessed before this
connection's own `run` stops reading any further, and a send buffer
capped the way Core's own `-maxsendbuffer` caps one, per the comments
beside each below.
"""

import asyncio
import contextlib
import secrets
import threading
import time
from importlib.metadata import version
from io import BytesIO
from typing import TYPE_CHECKING, cast, override

from btclib.exceptions import BTClibException, IncompleteMessageError
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.p2p.handshake import Version
from btclib.p2p.keepalive import Ping
from btclib.p2p.limits import MAX_PROTOCOL_MESSAGE_LENGTH
from btclib.p2p.message import Message

from btclib_node.constants import P2pConnStatus, ProtocolVersion
from btclib_node.exceptions import WrongNetworkMagicError
from btclib_node.p2p.address import ip_and_port, network_address
from btclib_node.p2p.callbacks import (
    MAX_GETDATA_INFLIGHT_BYTES,
    handshake_callbacks,
)
from btclib_node.p2p.filter_size import ONE_BUSY_MODERN_BLOCK_FILTER_BYTES

if TYPE_CHECKING:
    import socket
    from concurrent.futures import Future

    from btclib.p2p.addrv2 import NetworkAddressV2
    from btclib.p2p.payload import Payload

    from btclib_node import Node
    from btclib_node.p2p.manager import P2pManager

__all__ = ["MAX_QUEUED_RECV_BYTES", "MAX_QUEUED_SEND_BYTES", "Connection"]


# Core's own cap, `-maxsendbuffer` (`src/net.h`'s
# `DEFAULT_MAXSENDBUFFER = 1 * 1000`, in the KB units
# `src/init.cpp`'s `nSendBufferMaxSize = 1000 *
# args.GetIntArg("-maxsendbuffer", DEFAULT_MAXSENDBUFFER)` turns into
# bytes) is not this node's own number: at that threshold Core sets
# `fPauseSend` (`net.cpp:4205`) and `ProcessMessages`/`ProcessGetData`
# (`net_processing.cpp:5438`, `:2774-2776`) stop generating further
# messages for that peer, but what is already in `vSendMsg` keeps
# draining past the cap rather than being cut off.
#
# `get_cfilters` and `callbacks.getdata` (`p2p/callbacks.py`) each have
# that same kind of pause point of their own now --
# `MAX_CFILTERS_INFLIGHT_BYTES` and `MAX_GETDATA_INFLIGHT_BYTES`,
# argued beside them -- so this bound no longer has to hold one whole
# legitimate answer of either kind the way it once did
# (btclib-org/btclib-node#442 for `get_cfilters`, #470 for `getdata`,
# this node having had no "next call" to pause and resume at the way
# `fPauseSend` does, until those changes): each answer is now produced
# at the rate this connection drains it, not scheduled in full the
# moment it is asked for.
#
# Neither pacing bound is a hard ceiling on its own, though:
# `advance_getdata` and `advance_cfilters` (`p2p/callbacks.py`) each
# check their own bound *before* popping and sending the next item, not
# after, so either can schedule one item past its own bound before the
# next check catches it and pauses -- one more block, up to
# `MAX_PROTOCOL_MESSAGE_LENGTH`, for `getdata`; one more filter,
# `ONE_BUSY_MODERN_BLOCK_FILTER_BYTES`, for `get_cfilters`.
#
# Those two overshoots do not add. Both loops pace on the one
# `queued_send_bytes` field and neither reads anything else, so filters
# a connection is already committed to leave a `getdata` answer that
# much less room rather than adding to what that answer may commit; and
# `MAX_CFILTERS_INFLIGHT_BYTES` being the lower of the two bounds,
# `advance_cfilters` stops on its first check throughout a `getdata`
# overshoot. A peer pipelining a `getcfilters` behind a `getdata` it has
# not finished draining therefore has its filters counted inside that
# answer's own peak, never on top of it. The peak either mechanism can
# reach is its own bound plus one whole message of the kind that bound
# paces, and the larger of the two -- `MAX_GETDATA_INFLIGHT_BYTES` and a
# block -- is the first two terms below.
# btclib-org/btclib-node#521
#
# The third term is room above that peak. Its floor is the wire envelope
# those two terms leave out, `MAX_PROTOCOL_MESSAGE_LENGTH` bounding a
# payload rather than a message; the rest of it is what a sender passing
# no pacing check at all still spends, since such a sender commits its
# whole message on top of whatever this field already holds. `headers`
# and `addr` are the two left in that shape: each answers one request
# with everything it has in a single message, bounded by
# `MAX_HEADERS_RESULTS` and `MAX_ADDR_TO_SEND` respectively -- a full
# `headers` message some 162,000 wire octets, an `addr` 30,027 --
# each a real `Message` built the way `_queue` below builds one rather
# than the bare payload -- and infrequent enough, once per peer's own
# header sync and once per `getaddr`, that the room below covers either
# without a pacing point of its own.
#
# `notfound` -- a `getdata` answer's own trailing message -- is not one
# of those any more: `advance_getdata` (`p2p/callbacks.py`) now paces a
# miss the same way it paces a block or a transaction it does hold,
# checked before every item rather than once the whole request is
# served, so a `notfound` batching a request that named mostly misses is
# inside `advance_getdata`'s own peak above rather than committed on top
# of it. A transaction announcement's `inv` (`_send_due_announcements`,
# `download.py`) is paced too now, against this same field and this same
# `MAX_GETDATA_INFLIGHT_BYTES` bound, checked before every `MAX_INV_SZ`
# chunk -- so a peer this node is mid-`getdata`-answer to, in the same
# pass `Node.run` reaches `_step_chain` in, is not additionally charged
# for its own announcements: whichever of the two ran first this turn
# already left `queued_send_bytes` at or past this bound, and the second
# sees that and backs off before committing anything, the same
# displacement the paragraph above already gives `advance_cfilters`
# against a `getdata` answer's own overshoot, both checking this one
# field rather than a state either keeps of the other. Reusing the bound
# rather than giving `inv` a smaller one of its own is what keeps this
# room sized from filters and headers/addr alone, unchanged by that
# pacing. btclib-org/btclib-node#529
#
# What sizes that room is therefore still the filter answer's own peak --
# three filters, `MAX_CFILTERS_INFLIGHT_BYTES` pacing at two and
# `advance_cfilters` overshooting by one -- kept here as room rather than
# added above as a state the field reaches. It is written in filters
# rather than as that constant because the constant rounds its own
# product down to a whole number of bytes, and this sum would carry the
# rounding. `headers` and `addr` both fit under it, together as well as
# apart, though not by the margin the filter comparison above suggests:
# a full `headers` message (~162,000 octets) is itself larger than one
# filter, and a full `headers` and a full `addr` (30,027) together still
# leave some 102,000 octets of this room spare -- more than one filter's
# own size (98,079), but well short of the three this room is sized on.
# `addr` is a thousand `TimestampedNetworkAddress`, thirty octets each
# and not the twenty-six of the bare `NetworkAddress` inside one, plus
# the count and the envelope: measured rather than added up, by
# serializing `MAX_ADDR_TO_SEND` of them through btclib at the commit
# `uv.lock` pins.
#
# That the overshoot is one item, rather than one for every turn a
# pacing loop takes, is what `_queue` below buys: it counts a message
# against `queued_send_bytes` on the thread that committed it, so the
# item that goes past a pacing bound is in that count before the same
# loop's next check reads it (btclib-org/btclib-node#512).
#
# That puts this bound above both of Core's own flat, content-blind
# per-connection figures: its send buffer default just above
# (1,000,000 bytes) by more than an order of magnitude, and its
# *receive* buffer default -- `recv_flood_size`
# (`net.h:677`, `DEFAULT_MAXRECEIVEBUFFER * 1000` = 5,000,000 bytes,
# `net.h:100`), Core's own judgement of how much of one peer's traffic
# is worth holding at all -- by roughly two and a half times. Neither
# is the number to match: both are sized without reference to any one
# message's content, where this bound is sized from two real message
# sizes, a block and a filter, because this node's own dispatch has no
# incremental pause-and-resume loop of Core's own shape to lean on for
# the rest of an answer the way the paragraph above already argues --
# so a bound this much larger than either of Core's flat figures is
# what that difference in shape costs, not an oversight next to them.
MAX_QUEUED_SEND_BYTES = int(
    MAX_GETDATA_INFLIGHT_BYTES
    + MAX_PROTOCOL_MESSAGE_LENGTH
    + 3 * ONE_BUSY_MODERN_BLOCK_FILTER_BYTES
)

# Core's own per-connection receive bound, `-maxreceivebuffer`
# (`net.h`'s `DEFAULT_MAXRECEIVEBUFFER = 5 * 1000`, the same KB-to-bytes
# units `recv_flood_size` turns into): once a connection's own
# `m_msg_process_queue_size` exceeds it, `MarkReceivedMsgsForProcessing`
# sets `fPauseRecv` (`net.cpp:4116-4130`) and `GenerateWaitSockets`
# (`net.cpp:2102`) stops selecting that socket for a read event at all --
# not a drop of anything already parsed, a pause of the next `recv()` --
# and `PollMessage` (`net.cpp:4133-4142`) clears it again as the
# already-queued messages are processed one at a time -- all three
# read at bitcoin/bitcoin@b91d983f66.
#
# This node has no per-connection processing stage of Core's own shape
# to poll one message at a time from -- `P2pManager.messages` is one
# queue shared by every connection, drained by `Node`'s own loop through
# `_drain_message_queues`'s log2-scaled batch (btclib-org/btclib-node#462)
# -- but the same pause is available at the one place that is this
# connection's own: `run`'s own read loop below, gated on `_recv_resume`
# until enough of what this connection queued is processed to fall back
# under this bound. Pausing rather than dropping the message or the
# connection is the deliberate choice: unlike `MAX_QUEUED_SEND_BYTES`
# above, which drops a connection already over budget because this node
# once had no pause-and-resume point of its own to offer it (the comment
# there, and btclib-org/btclib-node#442), a connection hitting this bound
# has sent nothing but valid protocol messages faster than this node
# currently drains them -- exactly Core's own flood-control case, not a
# protocol violation to punish.
#
# The tempting number to size this against instead is this node's own
# worst legitimate receive burst: `download.py`'s own
# `_request_new_block_work` never asks one peer for more than
# `MAX_BLOCKS_PER_GETDATA_BURST` blocks at once (`pending[:2]` never
# adds to it -- the two are an `if`/`elif` on the same
# `download_queue`, never both in one batch), each up to
# `MAX_PROTOCOL_MESSAGE_LENGTH`, and this node never
# itself sends `GetCFilters`/`GetCFHeaders`/`GetCFCheckpt`, so no
# cfilter headroom belongs on this side either -- 64,000,000 bytes,
# nothing more, would be the whole of it. That is exactly the shape
# `MAX_QUEUED_SEND_BYTES` above used to be before btclib-org/btclib-node#442:
# a bound sized to the full legitimate case never distinguishes flooding
# from ordinary traffic, because ordinary traffic always fits under it
# whatever multiple of that burst is picked.
#
# And Core's own answer shows the size of that burst was never the
# question `recv_flood_size` was answering. Core requests the same 16
# blocks per peer, `MAX_BLOCKS_IN_TRANSIT_PER_PEER`
# (`net_processing.cpp:133`), at bitcoin/bitcoin@b91d983f66 --
# 64,000,000 bytes at `MAX_PROTOCOL_MESSAGE_LENGTH` each, the same
# figure this node's own burst comes to -- and still caps
# `recv_flood_size` at 5,000,000: Core pauses reading in the middle of
# its own ordinary IBD
# batches, on purpose, every time one arrives faster than
# `ProcessMessages` empties it. That pause costs nothing a well-behaved
# peer notices: the bytes it already sent sit in the kernel's own
# receive buffer and the TCP window rather than being dropped,
# `GenerateWaitSockets` simply stops selecting that socket for one more
# read, and the blocks still arrive once the queue falls back under the
# bound -- backpressure doing its job, not a flood being punished.
#
# This node's own drain differs from Core's in shape, not only in
# number. `ProcessMessages` is called once per peer every round of
# `ThreadMessageHandler`'s own loop, at bitcoin/bitcoin@b91d983f66
# (`net.cpp:3216-3238`), so every peer is guaranteed one message drained
# per round regardless of what any other peer has queued, where
# `_drain_message_queues` instead pops a `log2`-scaled share of one
# queue shared by every connection (btclib-org/btclib-node#462), with
# no such per-connection guarantee. Turning that shape into a larger
# number here would mean assuming some number of simultaneously busy
# peers, which nothing in this tree fixes as a constant -- doing so
# would be the same unmeasured inflation as the burst-sized bound
# above, just reached from the drain side instead of the peer side.
# This bound matches Core's own figure exactly rather than guessing past
# it. What that difference in shape costs a connection paused here -- a
# wait that is a function of how many peers are busy rather than a
# constant, and that grows more slowly than their number -- is
# `Node._drain_message_queues`'s own docstring
# (`btclib_node/__init__.py`), the loop that owns the resume, and
# `tests/unit/init_test.py` measures it in passes of that loop. Any
# argument for inflating this bound past Core's starts there.
# btclib-org/btclib-node#490
MAX_QUEUED_RECV_BYTES = 5 * 1000 * 1000

# The wire header's own layout -- `btclib.p2p.message`'s module docstring
# argues it against Core's `CMessageHeader` (`src/protocol.h`): magic (4
# octets) and command (12) ahead of a little-endian `length` (4), then a
# checksum (4). `btclib.p2p.message` keeps the matching constants private,
# so `parse_messages` below repeats the two it needs to peek the header
# itself, rather than reach into another module's underscored names.
_HEADER_SIZE = 24
_LENGTH_OFFSET = 16
_LENGTH_SIZE = 4

# BIP14's `/Name:Version/`, the shape Core builds in FormatSubVersion
# (`src/clientversion.cpp:65-70`, at bitcoin/bitcoin@204256c73f) and sends
# as `/Satoshi:29.0.0/` -- the one thing this node says about itself to
# every peer it meets, and what a crawler reporting the composition of
# the network parses.
#
# The version is read from the installed distribution rather than
# written here. `RELEASING.md`'s *Which version string is which*
# already tracks four spellings of one version, and a fifth that only a
# peer ever sees is the one nothing in this tree would catch drifting:
# no gate reads the wire. So this follows the cycle honestly -- a
# checkout of `main` announces the month it is open on, and what pip
# installs announces its release day.
#
# The name is the project's own, lowercase, and not the distribution's
# `btclib-node`: it is the organization's name on the network, where
# btclib is the library this node is a node over.
#
# A tree that was never installed has no metadata to read, and this
# raises there rather than falling back on a placeholder: a user agent
# is a claim, and one that says `unknown` where the version belongs is
# worse than a node that says why it will not start.
# btclib-org/btclib-node#580
_USER_AGENT = f"/btclib:{version('btclib-node')}/".encode()


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

        # Set by `send_version`, below, to what it drew: `None` until
        # then, and afterwards this connection's own share of
        # `P2pManager.pending_outbound_nonces` (`manager.py`), which is
        # what `promote_connection` and `remove_connection` read it back
        # for once this connection leaves the handshake.
        self.nonce: int | None = None

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
        # (`net_processing.cpp`, at bitcoin/bitcoin@58a7869f86), both
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
        # `sock_sendall` in full. `_queue` below counts it, on whichever
        # thread committed the message, before anything is scheduled on
        # the loop -- so a caller that has just handed several messages
        # over reads its own hand-off back rather than whatever the loop
        # has got round to. That is what `advance_getdata` and
        # `advance_cfilters` (`p2p/callbacks.py`) pace against, and the
        # only number either could pace against and stay inside
        # `MAX_QUEUED_SEND_BYTES`: counted on the loop instead, it would
        # not hold the caller's own earlier sends, and a `getdata`
        # answer would run as far past its own pacing bound as the loop
        # is behind -- spending the room that bound leaves above it and
        # dropping a peer that has committed no protocol violation
        # (btclib-org/btclib-node#512).
        #
        # `_send_lock` is what makes each `+=` and `-=` one step.
        # `_queue` is reached from `Node`'s own thread (the
        # `p2p.callbacks` handlers) and from `P2pManager`'s
        # (`manage_connections`, through `send_ping`), and `_deliver`
        # decrements from the loop, so a `+=` or `-=` here is a real
        # read-modify-write race rather than one thread's own sequential
        # bookkeeping -- the reason `queued_recv_bytes` below carries a
        # lock too. `_write_lock` guards something else entirely: two
        # `_deliver` calls racing `sock_sendall` on the same socket
        # would interleave their writes on the wire.
        self.queued_send_bytes: int = 0
        self._send_lock: threading.Lock = threading.Lock()
        self._write_lock = asyncio.Lock()

        # The read-side mirror of `queued_send_bytes` above: every octet
        # of a message `parse_messages` has already handed to
        # `manager.messages` and `handle_p2p` (`p2p/main.py`) has not yet
        # popped and dispatched, and `MAX_QUEUED_RECV_BYTES` its bound
        # (argued beside that constant). Unlike `queued_send_bytes`,
        # this is written from two threads rather than one:
        # `parse_messages` runs on this connection's own loop, and what
        # decrements it runs on `Node`'s, off `_drain_message_queues`'s
        # log2-scaled batch -- so a `+=` or `-=` here is a real
        # read-modify-write race rather than one thread's own
        # sequential bookkeeping, and `_recv_lock` is what makes each
        # one step. Modelled on `_ping_lock` above, guarding a pair of
        # fields crossing the same two threads for the same reason.
        self.queued_recv_bytes: int = 0
        self._recv_lock: threading.Lock = threading.Lock()
        # Set: `run`'s own read loop below may call `sock_recv` again.
        # `parse_messages` clears it, synchronously and on this same
        # loop, the moment `queued_recv_bytes` crosses
        # `MAX_QUEUED_RECV_BYTES`. What sets it back is `handle_p2p`'s
        # own decrement, from `Node`'s thread, through
        # `loop.call_soon_threadsafe` -- `asyncio.Event.set()` is not
        # itself safe to call from a thread other than the one running
        # the loop the event belongs to, the same reason `send` below
        # reaches `_deliver` through `run_coroutine_threadsafe` rather
        # than awaiting it directly.
        self._recv_resume: asyncio.Event = asyncio.Event()
        self._recv_resume.set()

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
        # Not closed here, on whichever thread called `stop`: closing
        # `self.client` while a reader or a writer is still registered
        # for it races `BaseSelectorEventLoop`'s own bookkeeping.
        # `_close` below does the closing, and does it on the loop's
        # own thread instead -- the comment beside it argues why that
        # is where this has to happen. btclib-org/btclib-node#518
        self.loop.call_soon_threadsafe(self._close)

    def _close(self) -> None:
        """Unregister `self.client`'s reader and writer, then close it.

        Runs on `self.loop`'s own thread, scheduled there by `stop`
        above through `call_soon_threadsafe` regardless of which
        thread called `stop` -- the loop's own selector is not safe to
        touch from any other one, which is the reason this is a
        separate step and not inlined into `stop` itself.

        The ordering is the fix for btclib-org/btclib-node#518.
        `BaseSelectorEventLoop.sock_recv` and `sock_sendall`
        (`asyncio/selector_events.py`, read on this tree's own
        `3.14`) each register a reader or a writer for `self.client`'s
        fd and add `_sock_read_done`/`_sock_write_done` as a done
        callback on the future they await, and that callback calls
        `remove_reader`/`remove_writer` in turn. `_remove_reader` reads
        `self._selector.get_map()` for the fd and, finding a writer
        still registered alongside the reader being dropped, calls
        `self._selector.modify(fd, ...)` rather than `unregister` --
        `modify` unregisters and re-registers, and re-registering a
        closed fd raises `OSError: Bad file descriptor` from
        `KqueueSelector.register`'s own `control()` call.
        `KqueueSelector.unregister`, the path taken when no writer is
        left to preserve, swallows exactly that error; `modify` does
        not carry the same guard, which is why the traceback in the
        issue only ever appears with a writer sharing the descriptor.
        `_send`'s own `sock_sendall` is that writer -- `async_send`
        reaches it through `_deliver`, two frames up -- so a peer not
        draining its send queue at the moment this closes is exactly
        the case that used to raise.
        Closing the fd before either callback has had a chance to run
        is what raises: this method removes the reader and the writer
        itself, before closing, so that fd is unregistered by the time
        anything might otherwise have tried to re-register it. Once
        removed here, `_sock_read_done`/`_sock_write_done`'s own later
        call is a no-op -- `_remove_reader`/`_remove_writer` cancel the
        stored handle as part of removing it, and `_sock_read_done`
        checks `handle.cancelled()` before calling `remove_reader`
        again, so the second call never reaches the selector at all.

        A fd of -1 is `self.client` already closed -- `stop`'s own
        idempotency (argued there) can schedule this twice -- and nothing
        is registered against a socket already closed, so there is
        nothing to remove either.
        """
        fd = self.client.fileno()
        if fd != -1:
            self.loop.remove_reader(fd)
            self.loop.remove_writer(fd)
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
                # Cleared by `parse_messages` once `queued_recv_bytes`
                # crosses `MAX_QUEUED_RECV_BYTES`, so a connection whose
                # own messages are piling up unprocessed stops pulling
                # more off the wire here rather than growing that queue
                # further -- the receive-side mirror of `_queue`'s own
                # refusal to queue past `MAX_QUEUED_SEND_BYTES` below,
                # and of Core's own `fPauseRecv`
                # (`MAX_QUEUED_RECV_BYTES`'s own comment).
                # btclib-org/btclib-node#462
                await self._recv_resume.wait()
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

    def _queue(self, payload: Payload) -> bytes | None:
        """Frame `payload` and count it, or refuse and return `None`.

        The whole of what a send commits to before anything reaches the
        loop, so that `queued_send_bytes` is true of this connection the
        moment the caller's own call returns rather than whenever the
        loop next runs -- the reason argued beside that field.

        `None` twice over: for a payload this node cannot serialize,
        which is logged and costs that one message; and for one that
        would take this connection past `MAX_QUEUED_SEND_BYTES`, which
        stops the connection.
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
            return None

        with self._send_lock:
            over_bound = self.queued_send_bytes + len(data) > MAX_QUEUED_SEND_BYTES
            if not over_bound:
                self.queued_send_bytes += len(data)
        if over_bound:
            # Not queued at all, so this message never reaches
            # `queued_send_bytes`: a peer already over budget gets
            # dropped rather than pushed further past it. `stop`
            # cancels `self.task`, the recv loop, so nothing more is
            # read from this peer either, and it is called outside the
            # lock above: it closes a socket and cancels a future, and
            # neither wants a lock every send takes held across it.
            #
            # It runs on whichever thread called `send`, which for a
            # `p2p.callbacks` sender is `Node`'s rather than the loop's.
            # That is deliberately the shape `stop` already documents for
            # `handle_p2p`, `handle_p2p_handshake` and `callbacks.pong`,
            # not a new cross-thread close this bound introduces --
            # btclib-org/btclib-node#518 is where ordering every such
            # close onto the loop is decided, for all of them at once.
            self.node.logger.warning(
                "send buffer bound exceeded, dropping connection: %r", self
            )
            self.stop()
            return None
        return data

    async def _deliver(self, data: bytes) -> None:
        """Write what `_queue` counted, and take it off the books after."""
        try:
            async with self._write_lock:
                await self._send(data)
        finally:
            with self._send_lock:
                self.queued_send_bytes -= len(data)
        self.last_send = time.time()

    async def async_send(self, payload: Payload) -> None:
        """Frame `payload` and send it, dropping the connection past the bound.

        What `send_version` awaits: it runs on the loop already, and
        wants this node's own `version` on the wire before `run` reads
        anything back. Every other sender in this tree reaches `send`
        below instead.
        """
        data = self._queue(payload)
        if data is not None:
            await self._deliver(data)

    def send(self, msg: Payload) -> None:
        """Frame and count `msg` here, and schedule its write onto the loop.

        The synchronous entry point, safe to call from any thread:
        `run_coroutine_threadsafe` is what lets both `Node`'s own thread
        (through the `p2p.callbacks` handlers) and `P2pManager`'s own
        (through `send_ping`) reach the loop without ever awaiting
        directly. Only the write is scheduled: `_queue` runs here, on
        the caller's own thread, so that a caller sending several
        messages in a row -- `advance_getdata` (`p2p/callbacks.py`)
        serving one block per turn of its own loop and pacing on
        `queued_send_bytes` between two of them -- reads its own
        hand-off back rather than a count the loop has yet to make.

        Serializing here rather than on the loop is what that costs, and
        it is paid by the thread that asked for the message: for the
        largest of them, a block, that is the thread which has just
        parsed the same block out of `block_db` to build the payload at
        all.
        """
        data = self._queue(msg)
        if data is not None:
            asyncio.run_coroutine_threadsafe(self._deliver(data), self.loop)

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
        self.nonce = nonce
        # Only an outbound connection's own nonce is ever recorded:
        # `P2pManager.is_self_connect_nonce`'s own docstring is where
        # that choice is argued against Core's.
        if not self.inbound:
            self.manager.add_pending_outbound_nonce(nonce)

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
            user_agent=_USER_AGENT,
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
        Every item carries its own wire size alongside it, a fourth
        tuple element `handle_p2p` or `handle_p2p_handshake`
        (`p2p/main.py`) weighs back off `queued_recv_bytes` once it is
        processed (btclib-org/btclib-node#462); `handshake_messages` is
        still drained whole every pass of `Node`'s own loop rather than
        sharing `messages`'s own log2-scaled share, which bounds how
        long a backlog persists but not how large one can grow between
        two passes -- what the size on this queue's own items is for,
        argued beside `consumed` below. btclib-org/btclib-node#482

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
        # Bytes handed to either queue this call, added to
        # `queued_recv_bytes` once, below, rather than once per message:
        # the same shape Core's own `MarkReceivedMsgsForProcessing`
        # accumulates `nSizeAdded` in before it takes
        # `m_msg_process_queue_mutex` once (`MAX_QUEUED_RECV_BYTES`'s own
        # comment). A handshake command counts here the same as any
        # other: `handshake_messages` shares this connection's own recv
        # bound, so a peer resending one faster than `Node`'s own loop
        # drains it pauses this connection's reads exactly as flooding
        # `messages` already does. btclib-org/btclib-node#482
        consumed = 0
        try:
            while True:
                start = stream.tell()
                try:
                    message = Message.parse(stream)
                except IncompleteMessageError:
                    # the only refusal more octets can answer, and the
                    # stream is back at the start of the partial message
                    return
                if message.magic != self.node.chain.magic:
                    raise WrongNetworkMagicError(message.magic)
                self.last_receive = time.time()
                size = stream.tell() - start
                consumed += size
                if message.command in handshake_callbacks:
                    self.manager.handshake_messages.append(
                        (message.command, message.payload, self.id, size)
                    )
                    continue
                item = (message.command, message.payload, self.id, size)
                if message.command in ("ping", "pong"):
                    self.manager.messages.appendleft(item)
                else:
                    self.manager.messages.append(item)
        finally:
            # `queued_recv_bytes` first, ahead of `self.buffer` below: the
            # two are independent bookkeeping over the same call, and
            # this order is what leaves the buffer rewind as the last
            # statement of the function, the shape every other exit path
            # above already relies on -- nothing here depends on which
            # runs first. Split into its own method rather than inlined
            # here: `parse_messages` is already at this file's own
            # complexity ceiling (`ruff`'s `complex-structure`) without
            # it.
            self._weigh_against_recv_bound(consumed)
            # whatever the loop did not consume, partial message
            # included. The gate above already returned without
            # touching `stream` for a read that completes no message at
            # all, so this only copies the (typically short) remainder
            # once a message has actually been taken off the front.
            if stream.tell():
                self.buffer = bytearray(stream.read())

    def _weigh_against_recv_bound(self, consumed: int) -> None:
        """Add `consumed` to `queued_recv_bytes`, pausing past the bound.

        Split out of `parse_messages`, the sole caller, only to keep that
        method under this file's own complexity ceiling; `consumed` is
        `0` whenever nothing was parsed this call, in which case this
        does nothing.
        """
        if not consumed:
            return
        with self._recv_lock:
            self.queued_recv_bytes += consumed
            over_bound = self.queued_recv_bytes > MAX_QUEUED_RECV_BYTES
        if over_bound:
            self._recv_resume.clear()

    @override
    def __repr__(self) -> str:
        try:
            peer = self.client.getpeername()
            out = f"Connection to {ip_and_port(peer[0], peer[1])}"
        except OSError:
            out = "Broken connection"
        return out
