# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Connection`, one peer-to-peer socket and the messages framed over it.

Feeds what it reads off the wire to its `Transport` (`p2p/transport.py`,
which frames the octets and bounds what any one message may claim to be)
and hands each message that comes out to `P2pManager`, writes what
`Node`'s own thread queues back out through the same transport, and
bounds what it will buffer in either direction -- `recv_flood_size` on
how much of what this connection has already handed to
`P2pManager.messages` or `P2pManager.handshake_messages` may sit there
unprocessed before this connection's own `run` stops reading any
further, and `send_buffer_max_size` on how much it may owe the peer
before `pause_send` holds its later messages, as Core's `fPauseSend`
does, per the comments beside each in `Connection.__init__`.
"""

import asyncio
import contextlib
import math
import secrets
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, cast, override

from btclib.exceptions import BTClibRuntimeError, BTClibValueError
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.p2p.addrv2 import network_address
from btclib.p2p.handshake import Version
from btclib.p2p.keepalive import Ping
from btclib.p2p.limits import PROTOCOL_VERSION

from btclib_node.constants import (
    DEFAULT_MAXRECEIVEBUFFER,
    DEFAULT_MAXSENDBUFFER,
    USER_AGENT,
    P2pConnStatus,
)
from btclib_node.exceptions import RejectedMessageError
from btclib_node.p2p.address import ip_and_port
from btclib_node.p2p.block_availability import BlockAvailability
from btclib_node.p2p.callbacks import handshake_callbacks
from btclib_node.p2p.chain_sync import ChainSyncTimeoutState
from btclib_node.p2p.eviction import Network, net_class
from btclib_node.p2p.messages import NoncelessPing
from btclib_node.p2p.permissions import NetPermissionFlags
from btclib_node.p2p.protocol_version import BIP0031_VERSION, common_version
from btclib_node.p2p.transport import (
    NetMessage,
    SerializedMessage,
    Transport,
    TransportProtocolType,
    V1Transport,
)
from btclib_node.p2p.v2transport import V1PeerRefusedError, V2Transport
from btclib_node.rolling_bloom import RollingBloomFilter

if TYPE_CHECKING:
    import socket
    from concurrent.futures import Future

    from btclib.p2p.addrv2 import NetworkAddressV2
    from btclib.p2p.payload import Payload

    from btclib_node import Node
    from btclib_node.config import Config
    from btclib_node.p2p.headers_sync import HeadersSyncState
    from btclib_node.p2p.manager import P2pManager

__all__ = [
    "AddrKnown",
    "Connection",
    "KnownTxInventory",
    "PeerStats",
    "local_services",
]


# `sizeof(CSerializedNetMsg)` on a 64-bit libstdc++ build: a
# `std::vector<unsigned char>` (24 bytes) and a `std::string` (32 bytes:
# a pointer, a length and the 16-byte buffer behind `src/memusage.h`'s
# "15 bytes in modern libstdc++"), at the same tag. libc++'s `std::string`
# is 24 bytes, which makes it 48.
_SERIALIZED_NET_MSG_BYTES = 56


def _malloc_usage(alloc: int) -> int:
    """Return Core's `memusage::MallocUsage` on a 64-bit build."""
    return ((alloc + 31) >> 4) << 4 if alloc else 0


# `sizeof(CNetMessage)` and `sizeof(DataStream)` on the same build: the
# message is a `DataStream` (a vector, 24 bytes, and a read position, 8),
# a time (8), two 4-byte sizes and a `std::string` (32), at the same tag.
# libc++'s `std::string` makes the first 72.
_NET_MESSAGE_BYTES = 80
_DATA_STREAM_BYTES = 32


# Core's `ALL_NET_MESSAGE_TYPES` (`src/protocol.h`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the commands
# `bytes_recv_per_msg` keys by name, every other one being counted under
# `NET_MESSAGE_TYPE_OTHER`, so that a peer inventing commands cannot grow
# the table.
_MESSAGE_TYPES = frozenset(
    (
        "version",
        "verack",
        "addr",
        "addrv2",
        "sendaddrv2",
        "inv",
        "getdata",
        "merkleblock",
        "getblocks",
        "getheaders",
        "tx",
        "headers",
        "block",
        "getaddr",
        "mempool",
        "ping",
        "pong",
        "notfound",
        "filterload",
        "filteradd",
        "filterclear",
        "sendheaders",
        "feefilter",
        "sendcmpct",
        "cmpctblock",
        "getblocktxn",
        "blocktxn",
        "getcfilters",
        "cfilter",
        "getcfheaders",
        "cfheaders",
        "getcfcheckpt",
        "cfcheckpt",
        "wtxidrelay",
        "sendtxrcncl",
    )
)
_MESSAGE_TYPE_OTHER = "*other*"

# BIP14's `/Name:Version/`, the shape Core builds in FormatSubVersion
# (`src/clientversion.cpp:65-70`, at bitcoin/bitcoin@204256c73f) and sends
# as `/Satoshi:29.0.0/` -- the one thing this node says about itself to
# every peer it meets, and what a crawler reporting the composition of
# the network parses. `constants.USER_AGENT` is where this string is
# computed and argued, once, for this module's wire bytes and
# `rpc.callbacks`'s own `getnetworkinfo` answer alike
# (btclib-org/btclib-node#1009).
_USER_AGENT = USER_AGENT.encode()


# `_send` hands `sock_sendall` at most this many octets at a time, and
# stamps `last_send` and counts the octets after each.
_SEND_CHUNK = 64 * 1024


# Chunks one message may take from a transport; `V1Transport` takes two,
# and `V2Transport` one beside the handshake octets still unsent.
_MAX_CHUNKS = 16


def _send_memusage(message: SerializedMessage) -> int:
    """Return Core's `CSerializedNetMsg::GetMemoryUsage` for `message`.

    The struct, no allocation for a command short enough for the string's
    own buffer, and `MallocUsage` of the payload (`src/net.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Core reads the vector's
    capacity, which its standard library's growth can leave above the
    payload's length, by less than the length again; this takes the length.
    """
    return _SERIALIZED_NET_MSG_BYTES + _malloc_usage(len(message.payload))


def _recv_memusage(message: NetMessage) -> int:
    """Return Core's `CNetMessage::GetMemoryUsage` for a received `message`.

    The struct, no allocation for a short command, and the stream's own
    `GetMemoryUsage`: its struct and `MallocUsage` of the payload
    (`src/net.cpp` and `src/streams.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag). Core reads the vector's capacity; this takes the
    length, as `_send_memusage` does.
    """
    return _NET_MESSAGE_BYTES + _DATA_STREAM_BYTES + _malloc_usage(len(message.payload))


def _make_transport(
    magic: bytes, *, inbound: bool, use_v2transport: bool, allow_v1: bool
) -> Transport:
    """Return Core's `MakeTransport`: v2 with a v1 fallback, or v1.

    Without `allow_v1` the v2 responder has no fallback and refuses a v1
    peer. Core has no such option; it is this node's, for refusing v1
    (btclib-org/btclib-node#1190).
    """
    if use_v2transport:
        return V2Transport(
            magic,
            V1Transport(magic) if allow_v1 else None,
            initiating=not inbound,
        )
    return V1Transport(magic)


def local_services(config: Config) -> ServiceFlags:
    """Return the services this node offers, Core's own `g_local_services`.

    `own_version` below sends this, and `rpc.callbacks.get_network_info`
    answers `getnetworkinfo`'s `localservices`/`localservicesnames` from
    it too -- one function rather than each computing its own, as
    ISS 1394 asks.

    `NODE_NETWORK_LIMITED` is set unconditionally and `NODE_NETWORK`
    only where this node is not pruned, matching Core's own
    `g_local_services` (`src/init.cpp`, at bitcoin/bitcoin@ca7162cde5):
    `NODE_NETWORK_LIMITED | NODE_WITNESS` from the start, gaining
    `NODE_NETWORK` only once `!chainman.m_blockman.IsPruneMode()`
    (`:2026-2028`). `Config.pruned` answers that check here, one fixed
    set of services for every connection rather than the per-chainstate
    assumeutxo case Core's own comment there also covers -- this tree
    has no counterpart to a background snapshot sync.

    `NODE_COMPACT_FILTERS` is BIP157's, and saying it promises an
    answer to `getcfilters`, `getcfheaders` and `getcfcheckpt` for every
    block of the chain. `Config.peerblockfilters` is Core's own
    `-peerblockfilters` (`src/init.cpp:992-998`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), off by default there
    (`DEFAULT_PEERBLOCKFILTERS`, `src/net_processing.h`) as it is here.
    The filter index is caught up before the node starts listening and
    kept up as blocks connect, so the promise holds whenever
    `peerblockfilters` is on.

    `NODE_P2P_V2` is set where `Config.v2transport` is, as Core's
    `-v2transport` sets it (`src/init.cpp:987-990`, same sha): it says
    that a connection to this node may use BIP324, which `Connection`
    then does (`use_v2transport`).
    """
    services = ServiceFlags.NODE_WITNESS | ServiceFlags.NODE_NETWORK_LIMITED
    if not config.pruned:
        services |= ServiceFlags.NODE_NETWORK
    if config.peerblockfilters:
        services |= ServiceFlags.NODE_COMPACT_FILTERS
    if config.v2transport:
        services |= ServiceFlags.NODE_P2P_V2
    return services


@dataclass(slots=True)
class PeerStats:
    """What `getpeerinfo` reads of a connection and nothing else does.

    `time_offset` is Core's `Peer::m_time_offset`, the peer's `version`
    timestamp less this node's clock when `callbacks.version` read it,
    in whole seconds. `last_inv_sequence` is `TxRelay::m_last_inv_sequence`,
    `Mempool.sequence` as of this connection's last trickle, which
    `DownloadManager` writes, and 1 before the first, where Core starts it.
    `addr_processed` and `addr_rate_limited` are `Peer::m_addr_processed`
    and `m_addr_rate_limited`, which `callbacks.addr` and
    `callbacks.addrv2` count on `Node`'s thread.

    The rest are Core's `nSendBytes`, `nRecvBytes` and their per-command
    tables: the octets written to and read off the socket, the tables by
    the message the octets are for, header included: the received one per
    whole message, the sent one per chunk the socket takes. Each is written
    on this connection's loop alone, by `_deliver` and `run`, and read
    from `Node`'s thread, which copies a table before iterating it.
    """

    time_offset: int = 0
    last_inv_sequence: int = 1
    addr_processed: int = 0
    addr_rate_limited: int = 0
    bytes_sent: int = 0
    bytes_recv: int = 0
    bytes_sent_per_msg: Counter[str] = field(default_factory=Counter)
    bytes_recv_per_msg: Counter[str] = field(default_factory=Counter)


class KnownTxInventory(RollingBloomFilter):
    """The latest transaction hashes a peer announced to this node or was sent.

    Core's `TxRelay::m_tx_inventory_known_filter`, a `CRollingBloomFilter{50000,
    0.000001}` (`src/net_processing.cpp:307`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag). Like Core's, it withholds a transaction from a peer that
    lacks it about once in a million queries.

    A txid for a peer without wtxid relay and a wtxid otherwise, as Core's
    filter holds them. Reached from `Node`'s thread alone: the `inv` and `tx`
    callbacks and `DownloadManager` write and read it, `P2pManager`'s thread
    never does, so it needs no lock.
    """

    __slots__ = ()

    def __init__(self) -> None:
        """Size the filter as Core sizes its own."""
        super().__init__(50_000, 0.000_001)


class AddrKnown(RollingBloomFilter):
    """The addresses a peer sent this node or was sent, by `service_key`.

    Core's `Peer::m_addr_known`, a `CRollingBloomFilter{5000, 0.001}`
    (`src/net_processing.cpp:5707`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag). Like Core's, it leaves out of a `getaddr` answer up to one
    in a thousand of the addresses the peer neither sent nor was sent.

    Reached from `Node`'s thread, by the `addr`, `addrv2` and `getaddr`
    callbacks, and from `P2pManager`'s, by the self-announcement. Each
    call holds a lock, as Core's `g_msgproc_mutex` guards its filter.
    """

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        """Size the filter as Core sizes its own."""
        super().__init__(5_000, 0.001)
        self._lock = threading.Lock()

    @override
    def add(self, key: bytes) -> None:
        with self._lock:
            super().add(key)

    @override
    def __contains__(self, key: bytes) -> bool:
        with self._lock:
            return super().__contains__(key)

    @override
    def reset(self, *, tweak: int | None = None) -> None:
        with self._lock:
            super().reset(tweak=tweak)


class Connection:
    """One peer-to-peer socket and everything owed to or by it.

    The module docstring above is where its own three jobs -- framing,
    writing what `Node`'s thread queues, and bounding what it buffers --
    are argued.
    """

    # When the message `handle_p2p` is dispatching was read off the
    # socket, set by it before each callback: Core's `ProcessMessage`
    # parameter `time_received`, which `callbacks.pong` measures a round
    # trip to. Written and read on `Node`'s thread alone. A class default
    # rather than an `__init__` assignment, `__init__` being at ruff's
    # `too-many-statements` ceiling.
    time_received: float = 0
    # Set by callbacks.getaddr the first time it answers this
    # connection: a peer that asks again gets nothing, rather than a
    # second answer from the cache. The cache already stops a fresh
    # draw per ask; what this flag alone still stops is a peer using
    # a loop of getaddr on the one connection to learn when this
    # node's cached sample itself changes. btclib-org/btclib-node#71
    # A class default for the same reason as `time_received`.
    answered_getaddr: bool = False
    # Core's `Peer::m_addr_token_bucket` (`net_processing.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): how many gossiped
    # addresses this peer may still have taken in, one to start with so
    # that it can announce itself. Written and read on `Node`'s thread
    # alone, by `callbacks.version`, `addr` and `addrv2`; a class default
    # for the same reason as `time_received`.
    addr_token_bucket: float = 1.0
    # Core's `Peer::m_sent_sendheaders`: set by
    # `DownloadManager._send_due_sendheaders` once it has asked this
    # peer for headers announcements, which is asked once. A class
    # default for the same reason as `time_received`.
    sent_sendheaders: bool = False
    # Set by callbacks.sendcmpct, the peer's own request to be announced
    # a new block as a `cmpctblock`, BIP152's high-bandwidth mode: Core's
    # `m_requested_hb_cmpctblocks`, false until asked. Read by `main`'s
    # block announcement and by `getpeerinfo`'s `bip152_hb_from`, all on
    # `Node`'s thread; a class default for the same reason as
    # `time_received`. btclib-org/btclib-node#1223
    requested_hb_cmpctblocks: bool = False
    # Core's `m_provides_cmpctblocks`, set by callbacks.sendcmpct for a
    # `sendcmpct` of version 2 whatever it announces, and Core's
    # `m_bip152_highbandwidth_to`, set by
    # `compact_block.maybe_set_peer_as_announcing_header_and_ids` when it
    # sends this peer `sendcmpct(1)` and cleared when it sends
    # `sendcmpct(0)`. Read on `Node`'s thread; class defaults for the
    # same reason as `time_received`.
    provides_cmpctblocks: bool = False
    bip152_highbandwidth_to: bool = False
    # Core's `IsBlockOnlyConn()`: an automatic outbound connection this
    # node opened as `BLOCK_RELAY`, which relays blocks alone -- no
    # transaction and no address traffic either way. Set by
    # `P2pManager.create_connection` before this connection's task is
    # scheduled, and never changed after; a class default for the same
    # reason as `time_received`.
    block_relay: bool = False
    # Core's `IsFeelerConn()`: an automatic outbound connection opened to
    # learn whether an address answers, dropped as soon as its `version`
    # has been read. Set and kept as `block_relay` is.
    feeler: bool = False
    # Core's `IsAddrFetchConn()`: opened by `P2pManager._process_addr_fetch`
    # to draw a `getaddr` answer out of a DNS seed's bare name and then
    # close, never counted towards an outbound target. Set and kept as
    # `block_relay` is (btclib-org/btclib-node#1284).
    addr_fetch: bool = False
    # Core's `CNode::m_addr_name`: the destination string this connection
    # was dialled by, where it was dialled by one rather than by a
    # resolved address -- `pszDest` in `ConnectNode` (`src/net.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag). `None` for a connection
    # accepted or dialled straight to an address:
    # `P2pManager.async_connect_host` is this tree's one setter, its own
    # `dest` argument becoming this field verbatim -- port included
    # where `dest` names one -- for every manual dial: `-connect`'s and
    # `-addnode`'s own standing loops, the `addnode` RPC's `onetry`, and
    # an addr-fetch dial alike (btclib-org/btclib-node#1264, #1432,
    # #1493). Core's own field falls back to the formatted socket
    # address for a connection dialled by address, which
    # `_socket_addresses` (`rpc/callbacks.py`) does the same way for
    # `getpeerinfo`'s own `addr`. Set by `P2pManager.create_connection`
    # before this connection's task is scheduled, and never changed
    # after; a class default for the same reason as `time_received`.
    addr_name: str | None = None
    # Core's `m_last_block_announcement`: when this peer last sent a
    # header new here and with more work than the active tip, which
    # `callbacks.headers` sets and `DownloadManager` evicts the extra
    # full-relay peer by. A class default for the reason `time_received`
    # gives.
    last_block_announcement: int = 0
    # Core's `Peer::m_addr_relay_enabled` (`net_processing.cpp`,
    # at bitcoin/bitcoin@9be056a8a7): whether this peer takes part in
    # address relay. Set where Core calls `SetupAddressRelay`: by
    # `callbacks.version` for a peer this node dialled, and by `addr`,
    # `addrv2` and `getaddr` for one that dialled in. Read by
    # `getpeerinfo`; a class default for the same reason as
    # `time_received`.
    addr_relay_enabled: bool = False
    # Core's `Peer::m_next_local_addr_send`: when `P2pManager` next tells
    # this peer the address it is reached at, 0 for the first time.
    # Written and read on `P2pManager`'s loop alone; a class default for
    # the same reason as `time_received`.
    next_local_addr_send: float = 0.0
    # Core's `CNode::m_inbound_onion` (`src/net.h`, same sha): whether
    # this peer reached an `=onion` listener. Set by
    # `P2pManager.create_connection` before the task is scheduled, and
    # never changed after; a class default for the same reason as
    # `time_received`.
    inbound_onion: bool = False

    @property
    def connected_through_network(self) -> Network:
        """Core's `CNode::ConnectedThroughNetwork`: the network of the peer."""
        return Network.ONION if self.inbound_onion else net_class(self.address)

    # Core's `CNodeState` block fields (`p2p/block_availability.py`),
    # here rather than in a `DownloadManager` table keyed by connection
    # id: `CNodeState` lives exactly as long as the peer, which is this
    # object's own life, and every reader -- `callbacks`, `main`'s block
    # announcement, `DownloadManager` and `getpeerinfo` -- holds the
    # connection, on `Node`'s thread. Built on first read rather than in
    # `__init__`, for the same ceiling as `time_received` above.
    @cached_property
    def block_availability(self) -> BlockAvailability:
        """What this peer is known to have of the block chain."""
        return BlockAvailability()

    # What `_deliver` has queued and the transport has not yet taken,
    # in the order queued. Read and written on this connection's own
    # loop alone. Messages wait here while a `V2Transport` has no
    # cipher. Built on first read, for the ceiling `block_availability`
    # gives.
    @cached_property
    def _outbox(self) -> deque[SerializedMessage]:
        return deque()

    # Whether `run` is to drain `_outbox` after each read, until
    # `get_info` leaves "detecting". Set by `run`.
    _handshaking: bool = False

    # The task awaiting `_send`'s own `sock_sendall`, for `_close` to
    # cancel: it removes the writer that would have completed that wait.
    # One at a time, `_drain_outbox` holding `_write_lock` across `_send`; a
    # class default for the reason `time_received` gives.
    # btclib-org/btclib-node#1164
    _writing: asyncio.Task[object] | None = None

    # Core's `m_ping_start`: when `send_ping` last queued a `ping`,
    # nonceless or not, stamped as it is pushed rather than as the socket
    # takes it, which is what `last_send` records. A class default for
    # the reason `time_received` gives. btclib-org/btclib-node#1204
    ping_start: float = 0

    # Core's `Peer::m_headers_sync` (`p2p/headers_sync.py`): this peer's
    # low-work headers sync, `None` where none runs. Written by
    # `callbacks.headers` and read by `getpeerinfo`, both on `Node`'s
    # thread; a class default for the reason `time_received` gives.
    headers_sync: HeadersSyncState | None = None

    # Core's `Peer::m_continuation_block`: the last block of a `getblocks`
    # answer cut at its limit, whose `getdata` is answered with an `inv`
    # of the tip. Written by `callbacks.getblocks` and read by
    # `callbacks.getdata`, both on `Node`'s thread; a class default for
    # the reason `time_received` gives.
    continuation_block: bytes | None = None

    # Core's `CNodeState::m_chain_sync` (`p2p/chain_sync.py`), here for
    # the same reasons as `block_availability` above.
    @cached_property
    def chain_sync(self) -> ChainSyncTimeoutState:
        """Whether this peer is behind this node's tip, and since when."""
        return ChainSyncTimeoutState()

    # PLR0915 counts one assignment per field a connection starts with,
    # which is what this body is.
    def __init__(  # noqa: PLR0913, PLR0915
        self,
        manager: P2pManager,
        client: socket.socket,
        address: NetworkAddressV2,
        connection_id: int,
        *,
        inbound: bool,
        use_v2transport: bool = False,
        allow_v1: bool = True,
        send_buffer_max_size: int = 1000 * DEFAULT_MAXSENDBUFFER,
        recv_flood_size: int = 1000 * DEFAULT_MAXRECEIVEBUFFER,
    ) -> None:
        """Set every field a fresh connection starts with, before `run`.

        `use_v2transport` is Core's own: a `V2Transport` that initiates
        where this node dialled and responds where it accepted, falling
        back to v1 on its own where a responder is spoken to in v1.
        Without `allow_v1` (`Config.v1transport`) that responder refuses
        the v1 peer instead, and `run` stops. The two bounds are
        `Config.send_buffer_max_size` and `Config.receive_flood_size`,
        Core's defaults where not given.
        """
        self.id = connection_id
        self.manager = manager
        self.node: Node = manager.node

        self.loop: asyncio.AbstractEventLoop = manager.loop
        self.client: socket.socket = client
        self.address: NetworkAddressV2 = address
        # What frames this connection's octets: `parse_messages` feeds the
        # receiving half on this connection's loop, and `_deliver` the
        # sending half under `_write_lock`, `p2p/transport.py` being where
        # each is argued.
        self.transport: Transport = _make_transport(
            self.node.chain.magic,
            inbound=inbound,
            use_v2transport=use_v2transport,
            allow_v1=allow_v1,
        )
        self.task: Future[None] | None = None

        self.status: P2pConnStatus = P2pConnStatus.Open
        self.inbound: bool = inbound
        # Whether `P2pManager._maybe_dial_more_peers` dialled this off its
        # own draw -- Core's `OUTBOUND_FULL_RELAY`, its `BLOCK_RELAY`
        # where `block_relay` is set too, or its `FEELER` where `feeler`
        # is, where an inbound peer and a `-connect`/`-addnode` one
        # (Core's `MANUAL`) are not. Every automatic connection holds an
        # outbound grant; the full-relay and block-relay-only targets
        # count the first two kinds, and not a feeler.
        # `P2pManager.create_connection` sets it.
        self.automatic: bool = False
        # Core's `CNode::m_permission_flags`: what `-whitelist` grants this
        # peer, decided where the connection is accepted or dialled
        # (`P2pManager.create_connection`) and kept.
        self.permissions: NetPermissionFlags = NetPermissionFlags.NONE
        # Core's `CNode::m_prefer_evict`: whether this peer was accepted
        # from a discouraged host, which `select_node_to_evict` reads.
        # `P2pManager.server` decides it on accept and
        # `P2pManager.create_connection` sets it; nothing changes it after.
        self.prefer_evict: bool = False
        # Core's `CNode::m_network_key` (`src/net.h`, at
        # bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the key
        # `callbacks.getaddr` caches its answer under -- the BIP155 id of
        # the peer's network (Tor's for an `inbound_onion` one) and the
        # host and port of the accepted socket's local address, which on
        # a wildcard listener is the one the peer reached, standing in
        # for Core's own SipHash-keyed `uint64_t` of the same three
        # (`CreateNodeFromAcceptedSocket`, same file and sha). `None`
        # for an outbound connection, which `getaddr` never answers.
        # `P2pManager.server` reads that address off the accepted socket;
        # `P2pManager.create_connection` sets this from it.
        self.addr_cache_key: tuple[int, str, int] | None = None

        # Set by `own_version`, below, to what it drew: `None` until
        # then, and afterwards this connection's own share of
        # `P2pManager.pending_outbound_nonces` (`manager.py`), which is
        # what `promote_connection` and `remove_connection` read it back
        # for once this connection leaves the handshake.
        self.nonce: int | None = None

        self.version_message: Version | None = None
        # Core's `Peer::m_wtxid_relay`: whether transactions are
        # announced to and asked of this peer by wtxid, else by txid.
        self.wtxidrelay_received: bool = False
        self.stats: PeerStats = PeerStats()

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

        # These and the eviction fields below are on the wall clock, not
        # `time.monotonic()`: Core's `m_last_recv`, `m_last_send`,
        # `m_ping_start`, `m_connected`, `m_last_block_time` and
        # `m_last_tx_time` are all read off `NodeClock`, its mockable
        # system clock (`src/util/time.h`, at bitcoin/bitcoin@9be056a8a7,
        # the v31.1 tag).
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
        # to send -- which the peer's answer, carrying the nonce
        # actually sent, then cannot match. This lock is what makes
        # each of the two writes one step against the other's; `stop`
        # does not take it, since `stop` never touches either field --
        # what makes two concurrent `stop` calls harmless is argued at
        # `stop` itself, and is a different property from this one.
        # btclib-org/btclib-node#357
        self._ping_lock: threading.Lock = threading.Lock()

        # What `P2pManager` reads to pick an inbound peer to evict: Core's
        # `CNode` fields `m_connected`, `m_min_ping_time`,
        # `m_last_block_time`, `m_last_tx_time`, `m_has_all_wanted_services`
        # and `nKeyedNetGroup` (`src/net.h`, at bitcoin/bitcoin@9be056a8a7,
        # the v31.1 tag), in whole seconds as Core keeps them, the ping
        # aside. `connected_time` is set here and the netgroup by
        # `P2pManager.create_connection`, both before this connection's
        # task is scheduled; `callbacks.version`, `pong`, `block` and `tx`
        # write the rest from `Node`'s thread, and `P2pManager.server`
        # reads them all from its own. One writer per field, storing one
        # immutable value, so the reader sees the old value or the new one
        # and nothing between, which is what Core's `std::atomic` fields
        # give its own reader. `callbacks.pong` updates `min_ping_time`
        # inside its `_ping_lock` block, where it reads the `ping_sent` the
        # round trip is measured from.
        self.connected_time: int = int(time.time())
        self.min_ping_time: float = math.inf
        self.last_novel_block_time: int = 0
        self.last_novel_tx_time: int = 0
        self.has_all_wanted_services: bool = False
        self.keyed_net_group: int = 0

        # Core's `vBlocksInFlight`, the rest of whose `CNodeState` is
        # `block_availability`
        self.download_queue: list[bytes] = []

        # When `addr_token_bucket`, above, was last topped up: Core's
        # `Peer::m_addr_token_timestamp`, which starts at the peer's
        # creation.
        self.addr_token_timestamp: float = time.time()

        # What `DownloadManager.tx_download` is waiting to tell this peer
        # about, and when it may next do so -- Core's `TxRelay` holds the
        # same two things per peer (`m_tx_inventory_to_send`,
        # `m_next_inv_send_time`) rather than announcing a transaction the
        # instant it is accepted. 0 is "never scheduled", which the first
        # check always treats as due. The queue is an insertion-ordered set
        # (keys only), as Core's is a `std::set`: a trickle erases what it
        # pops and touches nothing else. btclib-org/btclib-node#141
        self.tx_announce_queue: dict[bytes, None] = {}
        self.next_inv_send_time: float = 0.0

        # What this peer is known to have, so that a transaction is not
        # announced to it again: Core's `m_tx_inventory_known_filter`.
        self.known_tx_inventory: KnownTxInventory = KnownTxInventory()
        # What this peer is known to have among addresses, so that one is
        # not sent to it again: Core's `m_addr_known`.
        self.addr_known: AddrKnown = AddrKnown()

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

        # Core's `nSendBufferMaxSize`, `-maxsendbuffer` in bytes. Past it
        # a connection sets `pause_send`, as Core sets `fPauseSend`, and
        # nothing drops it for that.
        #
        # The pause is what bounds a peer that does not read, as in Core.
        # While it is set, `_hold_message` (`p2p/main.py`) reads nothing
        # more from the peer, and what it sends waits against
        # `recv_flood_size`. `advance_getdata` and `advance_cfilters`
        # (`p2p/callbacks.py`) check it before each item. So what the peer
        # asks for takes its queue past the bound by one answer at most,
        # or for those two by one item and the `notfound` of the misses
        # before it.
        #
        # What this node sends unprompted -- a `ping`, an announcement, a
        # `feefilter`, its address -- checks no pause, here or in Core.
        # `_keep_alive` (`p2p/manager.py`) drops a peer that owes a `pong`
        # for `_TIMEOUT_INTERVAL`, and sends the next `ping`, behind
        # whatever is queued, within `_PING_INTERVAL` of each `pong`. So a
        # peer holds no more than what is queued for it unprompted in
        # `_TIMEOUT_INTERVAL` plus `_PING_INTERVAL`.
        self.send_buffer_max_size: int = send_buffer_max_size
        # Core's `m_send_memusage`: every message queued and not yet
        # written whole, weighed by `_send_memusage`. `_queue` counts it on
        # whichever thread committed the message, before anything is
        # scheduled on the loop, so a caller that has just handed several
        # messages over reads its own hand-off back: `advance_getdata`
        # (`p2p/callbacks.py`) pauses after the item that passes the
        # bound, not as far past it as the loop is behind
        # (btclib-org/btclib-node#512).
        #
        # `_send_lock` makes each `+=` and `-=` one step: `_queue` runs on
        # `Node`'s thread and on `P2pManager`'s (`send_ping`), and
        # `_drain_outbox` subtracts on the loop. `_write_lock` guards
        # something else: two `_deliver` calls racing `sock_sendall` on
        # the same socket would interleave their writes on the wire.
        self.send_memusage: int = 0
        # Core's `fPauseSend`: set where `_queue` takes `send_memusage`
        # past `send_buffer_max_size`, and computed again where
        # `_drain_outbox` takes a message off, both under `_send_lock`, as
        # Core sets it in `PushMessage` and `SocketSendData`. Read without
        # the lock on `Node`'s thread: a drain the read misses holds the
        # peer one more pass of `Node`'s loop.
        self.pause_send: bool = False
        self._send_lock: threading.Lock = threading.Lock()
        self._write_lock = asyncio.Lock()

        # Core's `m_recv_flood_size`, `-maxreceivebuffer` in bytes. Past it
        # Core sets `fPauseRecv` (`MarkReceivedMsgsForProcessing`) and
        # selects the socket for no more reads until `PollMessage` takes
        # its queue back within it (`src/net.cpp`, at
        # bitcoin/bitcoin@9be056a8a7, the v31.1 tag); `run` below waits on
        # `_recv_resume` for the same. It pauses rather than drops: a peer
        # past it has sent valid messages faster than this node handles
        # them, Core's flood-control case.
        #
        # Core's message handler takes one message of each peer per pass.
        # `Node._drain_message_queues` pops a share of one queue all peers
        # share (btclib-org/btclib-node#462), so how long a paused peer
        # waits grows with how many peers are busy; its docstring has what
        # that costs (btclib-org/btclib-node#490).
        self.recv_flood_size: int = recv_flood_size
        # Core's `m_msg_process_queue_size`: each message `parse_messages`
        # has handed to `manager.messages` and `handle_p2p` (`p2p/main.py`)
        # has not yet popped and dispatched, weighed by `_recv_memusage`
        # as Core weighs it. It is written from two threads:
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
        # `recv_flood_size`. What sets it back is the decrement of
        # `handle_p2p` (or `resume_tx_checks`, for a held message), from
        # `Node`'s thread, through
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
            # Already stopped: `run`'s own `finally` calls `stop` again
            # after every path that already did. Idempotent rather than
            # counted on not to happen, since nothing elsewhere in this
            # class serializes who gets to call it: `stop` is
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

    def stop_when_sent(self) -> None:
        """Stop once every message already handed to `send` is written.

        Core sets `fDisconnect` on a feeler after `PushMessage` has
        already written what it sent, and a bare `stop` here would close
        the socket ahead of it. Each `send` schedules its write through
        `run_coroutine_threadsafe` in turn, and this call after them;
        callbacks run in that order, and each write queues on
        `_write_lock` ahead of this one, which the lock hands on in the
        order it was asked for.
        """
        asyncio.run_coroutine_threadsafe(self._stop_when_sent(), self.loop)

    async def _stop_when_sent(self) -> None:
        async with self._write_lock:
            pass
        self.stop()

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
        reaches it through `_deliver` and `_drain_outbox` -- so a peer not
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

        `remove_reader`/`remove_writer` themselves raise
        `NotImplementedError` outright on Windows' own Proactor loop,
        registration or none -- the same family of gap `P2pManager.server`
        and `RpcManager.server` each answer for `add_reader`
        (btclib-org/btclib-node#430), and here with nothing to work
        around it with: `sock_recv`/`sock_sendall` are what `run` and
        `_send` already call, on both loop families, and neither one
        registers anything a Proactor loop would need unregistered, so
        `contextlib.suppress` below is not papering over a skipped
        cleanup, it is the whole of what #518's own fix has to do on a
        loop that was never asked to register a reader or a writer for
        this fd in the first place.

        On a selector loop the writer removed here is what completes the
        future `sock_sendall` awaits, so a `_send` waiting on it would
        wait forever: its task stays pending, holding `_write_lock`
        against every `_deliver` queued behind it and keeping its bytes
        in `send_memusage`, until `P2pManager.stop`'s own sweep
        cancels it or the collector frees it pending. Cancelling it here
        ends it on the next step of this loop, and each `_deliver` behind
        it then reaches a closed socket, whose `OSError` `_drain_outbox`
        suppresses. On a proactor loop the close alone would end that
        write, `sock_sendall` there being one overlapped `WSASend` on the
        socket's own handle; the cancel ends it first, and comes before
        the close so that it does not target a handle already gone --
        the order CPython's own `_ProactorBasePipeTransport._force_close`
        keeps, cancelling its futures and closing the socket after. A
        write the
        socket had taken whose task has not yet stepped is cancelled all
        the same, so `_count_sent` misses its last chunk.
        btclib-org/btclib-node#1164
        """
        fd = self.client.fileno()
        if fd != -1:
            with contextlib.suppress(NotImplementedError):
                self.loop.remove_reader(fd)
                self.loop.remove_writer(fd)
        if self._writing is not None:
            self._writing.cancel()
        self.client.close()

    async def run(self) -> None:
        """Send `version` if outbound, then dispatch messages until `stop`.

        An inbound connection is sent this node's `version` by
        `callbacks.version`, once the peer's own has been accepted.

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
            self._handshaking = isinstance(self.transport, V2Transport)
            if not self.inbound:
                # a v2 initiator's handshake octets go out ahead of it, and
                # the `version` waits in `_outbox` for the cipher
                await self.async_send(self.own_version())
            while self.status < P2pConnStatus.Closed:
                # Cleared by `parse_messages` once `queued_recv_bytes`
                # crosses `recv_flood_size`, so a connection whose
                # own messages are piling up unprocessed stops pulling
                # more off the wire here rather than growing that queue
                # further -- Core's own `fPauseRecv`
                # (`recv_flood_size`'s own comment).
                # btclib-org/btclib-node#462
                await self._recv_resume.wait()
                # 64 KB, matching Core's own read buffer (`pchBuf`,
                # `src/net.cpp`) rather than the 1024 this had no
                # argument for: fewer syscalls, and fewer reads for the
                # transport to take one large message from.
                # btclib-org/btclib-node#438
                try:
                    data = await self.loop.sock_recv(self.client, 65536)
                except OSError:
                    # A peer that reset the connection rather than
                    # closing it gracefully -- `ConnectionResetError`,
                    # `ConnectionAbortedError` -- is `sock_recv` raising
                    # rather than the empty read the `if not data:` arm
                    # below answers for; Core's own `SocketHandler`
                    # (`src/net.cpp` at bitcoin/bitcoin@ca7162cde5)
                    # treats every `Recv` failure that is not
                    # `WOULDBLOCK`/`MSGSIZE`/`EINTR`/`EINPROGRESS` the
                    # same way it treats a `nBytes == 0` graceful close:
                    # `CloseSocketDisconnect`, not a crash -- so this is
                    # that same hangup, not this node's own bug, whether
                    # the peer that reset it is real or, as
                    # `test_a_peer_that_hangs_up_is_dropped` reproduces
                    # it, `socket.socketpair()`'s own Windows fallback
                    # answering an abrupt local close with a hard reset
                    # that a POSIX pair, backed by the kernel rather
                    # than a real TCP loopback, never sends
                    # (btclib-org/btclib-node#430).
                    return self.stop(cancel_task=False)
                if not data:
                    return self.stop(cancel_task=False)
                self.stats.bytes_recv += len(data)
                try:
                    self.parse_messages(data)
                    if self._handshaking:
                        await self._drain_outbox()
                        self._handshaking = (
                            self.transport.get_info().transport_type
                            is TransportProtocolType.DETECTING
                        )
                except V1PeerRefusedError:
                    # Core's wording for a v2 transport error
                    # (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the
                    # v31.1 tag), for what `-v1transport` refuses: no
                    # discouragement. SECURITY.md's *Where this node
                    # departs from Bitcoin Core* argues the refusal.
                    self.node.logger.log_debug(
                        "net",
                        "V2 transport error: V1 peer refused (see -v1transport), peer=%d",
                        self.id,
                    )
                    return self.stop(cancel_task=False)
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
                except Exception:  # noqa: BLE001
                    # the transport refusing a header -- another
                    # network's magic, an oversized length -- or this
                    # node's own bug. Core's `ReceiveMsgBytes` answers
                    # the first by dropping the connection and
                    # discourages nobody (`src/net.cpp`,
                    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
                    return self.stop(cancel_task=False)
        finally:
            self.stop(cancel_task=False)

    async def _send(self, data: bytes, sent: list[tuple[str, int]], /) -> None:
        """Write `data`, raising `OSError` where the socket cannot take it.

        `sent` says which message each octet of `data` is sent for, in
        order, as `_frame` returns it.

        Core stamps `m_last_send` on every `send()` that takes octets
        (`SocketSendData`, `src/net.cpp`, at bitcoin/bitcoin@9be056a8a7,
        the v31.1 tag), so a peer reading one long message slowly is not
        dropped for send inactivity while it reads. `sock_sendall`
        reports only that all of `data` is taken, and asyncio has no
        `sock_send`, so `last_send` is stamped after each `_SEND_CHUNK`
        instead. A peer that takes less than one chunk in
        `manager._TIMEOUT_INTERVAL` is dropped where Core would keep it.
        btclib-org/btclib-node#1784

        Core counts `nSendBytes` and `AccountForSentBytes` after the same
        `send()`, so the octets are counted at the same grain, per chunk:
        a write that fails or is cancelled is counted for the chunks the
        socket took, and the one in flight is not counted at all.
        btclib-org/btclib-node#1869
        """
        self._writing = asyncio.current_task()
        try:
            view = memoryview(data)
            pending = deque(sent)
            for start in range(0, len(view), _SEND_CHUNK):
                chunk = view[start : start + _SEND_CHUNK]
                await self.loop.sock_sendall(self.client, chunk)
                self.last_send = time.time()
                self._count_sent(pending, len(chunk))
        finally:
            self._writing = None

    def _count_sent(self, pending: deque[tuple[str, int]], taken: int) -> None:
        """Add `taken` octets to `bytes_sent`, by message, off `pending`.

        Core's `SocketSendData` counts what the socket took, by the
        command the octets were sent on behalf of, and keeps the v2
        handshake octets, which have none, out of the table. The received
        table has no such case: `run` counts it per message.
        """
        while taken:
            command, size = pending.popleft()
            count = min(size, taken)
            if count < size:
                pending.appendleft((command, size - count))
            taken -= count
            self.stats.bytes_sent += count
            if command:  # "don't report v2 handshake bytes for now"
                self.stats.bytes_sent_per_msg[command] += count

    def _collect_octets(
        self, chunks: list[memoryview], sent: list[tuple[str, int]]
    ) -> None:
        """Take every octet the transport holds, with what each is sent for.

        Core's `SocketSendData` loop, run to the end of what the
        transport holds: a handshake not yet sent, or a message just set.
        `mark_bytes_sent` is called before the write rather than after
        it, which loses nothing: the write takes every octet or the
        connection is dropped, and nothing resumes half a message.

        Raises `BTClibRuntimeError` for a transport that does not finish.
        """
        for _ in range(_MAX_CHUNKS):
            to_send, more, command = self.transport.get_bytes_to_send(
                have_next_message=False
            )
            if to_send:
                chunks.append(to_send)
                sent.append((command, len(to_send)))
                self.transport.mark_bytes_sent(len(to_send))
            if not more:
                return
        # this loop never yields, so a transport that does not advance
        # would freeze the whole loop
        err_msg = "the transport does not finish a message"
        raise BTClibRuntimeError(err_msg)

    def _frame(
        self, message: SerializedMessage | None
    ) -> tuple[bytes, list[tuple[str, int]], bool]:
        """Return one write, what each octet is sent for, and if `message` went.

        What the transport already holds, a handshake, comes first, so
        the octets are on the wire in the order the transport made them.
        `message`, the head of `_outbox` or `None`, is set after it and
        joined into the same write: a header and a payload sent apart can
        wait on each other under Nagle, which Core answers with
        `MSG_MORE`, and `sock_sendall` has no such flag.

        `message` is not taken where the transport cannot send yet, a
        `V2Transport` short of its cipher. One the transport cannot frame
        is logged and counted taken, which drops it.

        Raises `BTClibRuntimeError` for a transport that does not finish.
        Called under `_write_lock`. A `V2Transport`'s receiving half
        also adds handshake octets on this loop, between two calls; they
        go out first, since the transport takes no message while it holds
        octets.
        """
        chunks: list[memoryview] = []
        sent: list[tuple[str, int]] = []
        self._collect_octets(chunks, sent)
        taken = False
        if message is not None:
            try:
                taken = self.transport.set_message_to_send(message)
            except BTClibValueError as e:
                self.node.logger.warning("error in serializing message: %s", e)
                taken = True
            else:
                if taken:
                    self._collect_octets(chunks, sent)
        return b"".join(chunks), sent, taken

    def _queue(self, payload: Payload) -> SerializedMessage | None:
        """Serialize `payload` and count it, or return `None`.

        The whole of what a send commits to before anything reaches the
        loop, so that `send_memusage` is true of this connection the
        moment the caller's own call returns rather than whenever the
        loop next runs -- the reason argued beside that field.

        `None` for a payload this node cannot serialize, which is logged
        and costs that one message.
        """
        self.node.logger.log_debug("net", "Sending message: %s", payload.command)

        try:
            # The payload names its own command.
            #
            # Its octets are not re-checked on the way out: this node
            # built them from state it has already validated, and
            # btclib's block payload would ask CheckBlock of them
            # against mainnet's pow limit, which no regtest or signet
            # block meets. The envelope still is, by the transport in
            # `_frame`: that check is about the octets this node emits
            # being well formed -- a command of at most twelve printable
            # octets, a length under the protocol's. It says nothing about
            # whether the command is one any peer answers to; the test over
            # every payload's `command` is what says that.
            message = SerializedMessage(
                payload.command, payload.serialize(check_validity=False)
            )
        # deliberately blind (BLE001): this is called for every message
        # this node ever sends, callers throughout src/btclib_node/p2p and
        # src/btclib_node/download.py among them, so a bug serializing one
        # payload logs and drops that one send rather than propagating
        # into an arbitrary caller's own control flow
        except Exception as e:  # noqa: BLE001
            self.node.logger.warning("error in serializing message: %s", e)
            return None

        with self._send_lock:
            self.send_memusage += _send_memusage(message)
            if self.send_memusage > self.send_buffer_max_size:
                self.pause_send = True
        return message

    async def _deliver(self, message: SerializedMessage) -> None:
        """Queue what `_queue` counted, and write it unless the transport waits.

        A `V2Transport` takes no message before it has a cipher, and
        `message` then stays in `_outbox` until `run` finds one. Others
        queued meanwhile stay behind it, in the order they were queued.
        """
        self._outbox.append(message)
        await self._drain_outbox()

    async def _drain_outbox(self) -> None:
        """Write the transport's own octets, then each message it takes.

        The transport frames and, for BIP324, encrypts each message
        under `_write_lock`, so the order of the octets on the wire is
        the order the transport produced them in, and the cipher's
        counter agrees with it. A message is taken off the books once
        written. Stops at the first the transport does not take yet.
        """
        async with self._write_lock:
            while True:
                data, sent, taken = self._frame(
                    self._outbox[0] if self._outbox else None
                )
                if data:
                    with contextlib.suppress(OSError):  # probably connection dropped
                        await self._send(data, sent)
                if not taken:
                    return
                with self._send_lock:
                    self.send_memusage -= _send_memusage(self._outbox.popleft())
                    self.pause_send = self.send_memusage > self.send_buffer_max_size

    async def async_send(self, payload: Payload) -> None:
        """Frame `payload` and send it.

        What `run` awaits for an outbound connection's `version`: it
        runs on the loop already, and wants that on the wire before it
        reads anything back. Every other sender in this tree reaches `send`
        below instead.
        """
        message = self._queue(payload)
        if message is not None:
            await self._deliver(message)

    def send(self, msg: Payload) -> None:
        """Serialize and count `msg` here, and schedule its write onto the loop.

        The synchronous entry point, safe to call from any thread:
        `run_coroutine_threadsafe` is what lets both `Node`'s own thread
        (through the `p2p.callbacks` handlers) and `P2pManager`'s own
        (through `send_ping`) reach the loop without ever awaiting
        directly. Only the framing and the write are scheduled: `_queue`
        runs here, on the caller's own thread, so that a caller sending
        several messages in a row -- `advance_getdata` (`p2p/callbacks.py`)
        checking `pause_send` before each item it serves -- reads its own
        hand-off back rather than a count the loop has yet to make.

        Serializing the payload here rather than on the loop is what that
        costs, and it is paid by the thread that asked for the message:
        for the largest of them, a block, that is the thread which has just
        parsed the same block out of `block_db` to build the payload at
        all. The transport adds the header, and its checksum, on the loop.
        """
        message = self._queue(msg)
        if message is not None:
            asyncio.run_coroutine_threadsafe(self._deliver(message), self.loop)

    def own_version(self) -> Version:
        """Build this node's own `version` message, recording its nonce.

        Sent by `run` as soon as an outbound connection opens, and by
        `callbacks.version` to an inbound peer whose own `version` it
        has accepted, which is where Core's `ProcessMessage` calls
        `PushNodeVersion` for each (`src/net_processing.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
        """
        services = local_services(self.manager.node.config)
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
        return Version(
            version=PROTOCOL_VERSION,
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
            # `Node.best_height`'s own comment (`__init__.py`) is where
            # reading this here, off `Node`'s own thread's writes without
            # a lock, is argued. btclib-org/btclib-node#722
            start_height=self.manager.node.best_height,
            # Core's own `fRelay` is `!RejectIncomingTxs` -- false for a
            # block-relay-only peer, a feeler and under `-blocksonly`
            # (src/net_processing.cpp, at bitcoin/bitcoin@9be056a8a7, the
            # v31.1 tag) -- and never about `IsInitialBlockDownload()`.
            # Of those this node has the first two, so the flag never
            # has to be revised once the node catches up: what a peer
            # sends before then is dropped on arrival instead,
            # `p2p/callbacks.tx`. btclib-org/btclib-node#129
            relay=not self.block_relay and not self.feeler,
        )

    def send_ping(self) -> None:
        """Send a `ping` with a fresh nonzero nonce, recording it under lock.

        Called from `Node`'s own thread, twice over (`callbacks.verack`
        once a handshake completes, and `rpc.callbacks.ping` through
        `ping_all`), and from `P2pManager`'s own thread once
        (`manage_connections`); `_ping_lock` is what keeps its own two
        writes one step against `callbacks.pong`'s read and clear.

        At a common version of `BIP0031_VERSION` or below it is a
        `NoncelessPing`, as in Core's `MaybeSendPing`
        (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the
        v31.1 tag): no `pong` answers it, so nothing is recorded as
        outstanding. btclib-org/btclib-node#1204
        """
        self.ping_start = time.time()
        if common_version(self) <= BIP0031_VERSION:
            self.send(NoncelessPing())
            return
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

    def parse_messages(self, data: bytes) -> None:
        """Feed `data` to the transport, queueing each message it completes.

        A trailing partial message stays in the transport for the next
        read, and each complete one is routed to `handshake_messages` or
        `messages`.
        Every item carries its own weight, `_recv_memusage`, a fourth
        tuple element `handle_p2p` or `handle_p2p_handshake`
        (`p2p/main.py`), or `resume_tx_checks` for a message they held back,
        weighs back off `queued_recv_bytes` once it is processed
        (btclib-org/btclib-node#462); `handshake_messages` is
        still drained whole every pass of `Node`'s own loop rather than
        sharing `messages`'s own log2-scaled share, which bounds how
        long a backlog persists but not how large one can grow between
        two passes -- what the weight on this queue's own items is for,
        argued beside `consumed` below. btclib-org/btclib-node#482

        A fifth element is the time it was read off the socket, for
        `callbacks.pong` (`P2pManager.messages`'s own comment).
        A handshake command goes to `handshake_messages`, and so does
        anything else read while the connection is still `Open`, so that
        no message overtakes the `verack` that precedes it.
        btclib-org/btclib-node#1657

        Core's `ReceiveMsgBytes` (`src/net.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the transport refusing
        octets raises, and the connection is dropped by `run`. A message
        it rejects is counted and the connection goes on.
        """
        # The weight handed to either queue this call, added to
        # `queued_recv_bytes` once, below, rather than once per message:
        # the same shape Core's own `MarkReceivedMsgsForProcessing`
        # accumulates `nSizeAdded` in before it takes
        # `m_msg_process_queue_mutex` once (`recv_flood_size`'s own
        # comment). A handshake command counts here the same as any
        # other: `handshake_messages` shares this connection's own recv
        # bound, so a peer resending one faster than `Node`'s own loop
        # drains it pauses this connection's reads exactly as flooding
        # `messages` already does. btclib-org/btclib-node#482
        consumed = 0
        remaining = memoryview(data)
        # Stamped on the octets, not on a message they complete, as
        # `CNode::ReceiveMsgBytes` stamps `m_last_recv`: a block arriving
        # slowly keeps its peer from being dropped as silent.
        # btclib-org/btclib-node#1768
        received = self.last_receive = time.time()
        try:
            while remaining:
                remaining = self.transport.received_bytes(remaining)
                if not self.transport.received_message_complete():
                    continue
                try:
                    message = self.transport.get_received_message()
                except RejectedMessageError as e:
                    # counted where Core's `ReceiveMsgBytes` counts a
                    # rejected message, and the peer kept
                    self._count_received(_MESSAGE_TYPE_OTHER, e.size)
                    continue
                weight = _recv_memusage(message)
                consumed += weight
                self._count_received(message.command, message.size)
                # `handshake_messages` is drained whole ahead of
                # `messages`, so what a connection sends while still `Open`
                # is queued there behind its handshake commands, so none
                # overtakes its `verack`. Two messages after the `verack`
                # can swap, once per connection, in the window between
                # `status` being read here and set on `Node`'s thread:
                # not worth a lock.
                # btclib-org/btclib-node#1657
                #
                # Every other message to the back of `messages`, `ping` and
                # `pong` included: Core splices them onto the end of
                # `m_msg_process_queue`
                # (`CNode::MarkReceivedMsgsForProcessing`), so a peer's
                # `pong` follows the answer to what it sent before the
                # `ping`. btclib-org/btclib-node#1410
                queue = (
                    self.manager.handshake_messages
                    if message.command in handshake_callbacks
                    or self.status == P2pConnStatus.Open
                    else self.manager.messages
                )
                queue.append(
                    (
                        message.command,
                        message.payload,
                        self.id,
                        weight,
                        received,
                    )
                )
        finally:
            # Split into its own method rather than inlined here:
            # `parse_messages` is already at this file's own complexity
            # ceiling (`ruff`'s `complex-structure`) without it.
            self._weigh_against_recv_bound(consumed)

    def _count_received(self, command: str, size: int) -> None:
        """Add one whole message to `bytes_recv_per_msg`, as Core keys it.

        Split out of `parse_messages` for the complexity ceiling
        `_weigh_against_recv_bound` below is split out for.
        """
        key = command if command in _MESSAGE_TYPES else _MESSAGE_TYPE_OTHER
        self.stats.bytes_recv_per_msg[key] += size

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
            over_bound = self.queued_recv_bytes > self.recv_flood_size
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
