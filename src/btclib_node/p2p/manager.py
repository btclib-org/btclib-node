# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`P2pManager`, the thread listening for and dialing peer connections.

Runs its own asyncio loop -- `manage_connections` accepts inbound
sockets, dials outbound ones from `PeerDB`, and prunes an idle or
handshake-stuck `Connection` -- and hands finished messages back to
`Node`'s own thread through `messages` and `handshake_messages`. A
coroutine enters this loop only through `run_coroutine_threadsafe`;
`Node`'s own thread calls this class's plain methods, such as `verack`'s
own `promote_connection`, directly.
"""

import asyncio
import errno
import secrets
import socket
import threading
import time
from collections import deque
from concurrent.futures import CancelledError
from contextlib import suppress
from typing import TYPE_CHECKING, override

from btclib.p2p.addrv2 import BIP155Network, can_addrv1, network_address

from btclib_node.constants import CLIENT_NAME, P2pConnStatus
from btclib_node.p2p.address import (
    PeerDB,
    dial,
    endpoint_key,
    fixed_seed_addresses,
    host_key,
    ip_and_port,
    peer_address,
)
from btclib_node.p2p.connection import Connection
from btclib_node.p2p.eviction import (
    EvictionCandidate,
    is_local,
    keyed_net_group,
    net_class,
    net_group,
    select_node_to_evict,
)
from btclib_node.p2p.protocol_version import BIP0031_VERSION, common_version

if TYPE_CHECKING:
    from concurrent.futures import Future

    from btclib.p2p.addrv2 import NetworkAddressV2
    from btclib.p2p.payload import Payload
    from btclib.tx.tx import Tx as BtclibTx

    from btclib_node import Node

__all__ = ["P2pManager"]

# How often `manage_connections`' own loop prunes the active-address
# table on its own rather than only as a side effect of something asking
# for it. Not tied to the loop's own sleep below -- an O(n) walk of
# `active_addresses` every pass buys nothing a run every few minutes
# does not -- but to `get_active_addresses`'s own three-hour staleness
# window: far enough under it that a stale row does not linger long past
# it, however rarely this node is asked for its table.
# btclib-org/btclib-node#71
_ACTIVE_PRUNE_INTERVAL = 300

# How long a connection has from connecting to finishing its handshake:
# Core's `DEFAULT_PEER_CONNECT_TIMEOUT` (`src/net.h`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag), past which
# `CConnman::InactivityCheck` drops a connection not yet
# `fSuccessfullyConnected`, whatever it has sent. This node has no
# `-peertimeout` to set it.
_PEER_CONNECT_TIMEOUT = 60

# `manage_connections`'s own idle bound, not Core's `TIMEOUT_INTERVAL`
# (20 minutes, `net.h`, aed80c7395) -- a shorter one of this tree's own:
# a connection quiet this long is sent a `ping`, and one still quiet
# this long again after that is dropped. A pending connection is held
# to `_PEER_CONNECT_TIMEOUT` above instead. A peer at `BIP0031_VERSION`
# or below is sent no `ping` (`Connection.send_ping`) and is dropped once
# quiet twice this long.
_IDLE_TIMEOUT = 120

# `_maybe_redial_specified`'s own backoff for a `-connect`/`-addnode`
# peer that is not currently connected: doubled on every attempt made,
# reset to this floor the moment the peer is seen connected, capped at
# `_REDIAL_MAX_SECONDS`. Core keeps a whole thread apiece for this --
# `ThreadOpenConnections`'s own `-connect` arm, an uncapped
# `for (int64_t nLoop = 0;; nLoop++)` loop redialling every named peer
# with a per-peer sleep that grows to `10 * 500ms` and a flat `500ms`
# after each full pass (`src/net.cpp:2592-2625`, at
# bitcoin/bitcoin@ca7162cde5), and `ThreadOpenAddedConnections`, a
# `while (true)` loop over `GetAddedNodeInfo(include_connected=false)`
# -- the "already connected, skip it" filter `_maybe_redial_specified`
# below reproduces with its own `connected` set -- redialling every
# not-yet-connected added peer with a `500ms` sleep between each and a
# `60s` (something was tried) or `2s` (nothing was) sleep after the
# pass (`src/net.cpp:3052-3082`, same sha). This node has one loop
# already, `manage_connections`, running every 0.1s regardless of
# either flag; reusing it for both rather than adding two more standing
# coroutines is this tree's own Python-native shape of the same
# requirement, at the cost of one shared, capped, doubling backoff in
# place of replicating either of Core's own two cadences exactly.
_REDIAL_BASE_SECONDS = 1.0
_REDIAL_MAX_SECONDS = 60.0

# The outbound slots Core reserves out of `-maxconnections` before
# inbound peers get the rest: `MAX_OUTBOUND_FULL_RELAY_CONNECTIONS`,
# `MAX_BLOCK_RELAY_ONLY_CONNECTIONS` and `MAX_FEELER_CONNECTIONS`
# (`src/net.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag). This node
# dials no block-relay-only or feeler connection, and reserves their
# slots all the same, so that a peer finds as many inbound slots here as
# in a Core node given the same `-maxconnections`.
_MAX_OUTBOUND_FULL_RELAY_CONNECTIONS = 8
_MAX_BLOCK_RELAY_ONLY_CONNECTIONS = 2
_MAX_FEELER_CONNECTIONS = 1

# How long `_maybe_add_fixed_seeds` gives DNS seeding, `-seednode` and
# `-addnode` to fill the table before falling back on the chain's fixed
# seeds: Core's `start + std::chrono::minutes{1}` in
# `ThreadOpenConnections` (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7,
# the v31.1 tag).
_FIXED_SEEDS_DELAY = 60

# How often `_maybe_add_fixed_seeds` looks, at most: once per pass of
# `ThreadOpenConnections`, which sleeps 500ms (same sha). Core's
# `addrman.Size(net)` is a counter, and `PeerDB.holds_network` is a walk
# of both tables, so this loop's own 100ms would walk them five times as
# often for nothing.
_FIXED_SEEDS_CHECK_INTERVAL = 0.5

# Core's `SEED_OUTBOUND_CONNECTION_THRESHOLD` (`src/net.cpp`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the full-relay peers below
# which another `-seednode` is queued, and which DNS seeding waits for
# the `-seednode` peers to bring.
_SEED_OUTBOUND_CONNECTION_THRESHOLD = 2
# `ThreadOpenConnections`'s `ADD_NEXT_SEEDNODE` (same file and sha): how
# long one `-seednode` is given before the next one is queued.
_ADD_NEXT_SEEDNODE = 10
# `ThreadDNSAddressSeed`'s `SEEDNODE_TIMEOUT` (same file and sha): how
# long DNS seeding waits on the `-seednode` peers, looking every
# `_SEEDNODE_CHECK_INTERVAL`, its `sleep_for(500ms)`.
_SEEDNODE_TIMEOUT = 30
_SEEDNODE_CHECK_INTERVAL = 0.5
# How long a `-seednode` connection is held waiting for its `addr`:
# Core's `10 * AVG_ADDRESS_BROADCAST_INTERVAL` (`src/net_processing.cpp`,
# same sha), 30 seconds being the interval.
_ADDR_FETCH_TIMEOUT = 10 * 30

# The networks Core reaches by default, which are this node's two:
# `g_reachable_nets` loses Tor, I2P and CJDNS in `AppInitMain` where no
# proxy, SAM bridge or `-cjdnsreachable` is given (`src/init.cpp`, same
# sha), and `dial` opens a socket for IPv4 and IPv6 alone.
_REACHABLE_NETWORKS = (BIP155Network.IPV4, BIP155Network.IPV6)

# How many addresses `_maybe_dial_more_peers` draws in one pass before
# giving up until the next: `ThreadOpenConnections`'s `nTries > 100`
# (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
_MAX_DRAWS_PER_PASS = 100

# How many hosts `P2pManager.discourage` remembers. Core keeps them in
# `BanMan::m_discouraged`, a `CRollingBloomFilter{50000, 0.000001}`
# (`src/banman.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag). This
# tree has no rolling bloom filter, so `P2pManager._discouraged` is an
# insertion-ordered `dict` of at most this many hosts, the one
# discouraged longest ago forgotten first. Core's filter answers yes
# for a host it never held, up to one time in a million, and this never
# does. Core's forgets a host 50,000 to 75,000 insertions later, its
# generations of 25,000 holding two or three at a time, and this
# forgets it once 50,000 other hosts have been discouraged since.
_DISCOURAGED_CAPACITY = 50_000


def _network_error_string(error: OSError) -> str:
    """Core's `NetworkErrorString` of a failed socket call: text, then number.

    Core's `"%s (%d)"` carries `SysErrorString`'s errno off Windows and
    `Win32ErrorString`'s Winsock code on it (`src/util/sock.cpp:426-434`,
    `src/util/syserror.cpp:17-50`, at bitcoin/bitcoin@9be056a8a7). CPython
    keeps that code as `winerror` on what a socket call raises there
    (`set_error`, `Modules/socketmodule.c` at CPython v3.14.0) and renumbers
    a few of them in `errno`, `WSAEACCES` 10013 becoming 13 (`PC/errmap.h`),
    so `winerror` is the number read where it is set.

    The text is not Core's to the byte on Windows: CPython strips trailing
    whitespace and periods off `FormatMessageW`'s text before this sees it
    (`Python/errors.c`), where Core's `Win32ErrorString` prints the buffer
    `FormatMessageA` fills without stripping it.
    """
    return f"{error.strerror} ({getattr(error, 'winerror', None) or error.errno})"


class P2pManager(threading.Thread):
    """The thread listening for and dialling peer connections.

    The module docstring above is where its own loop, its two message
    queues and the boundary with `Node`'s thread are argued;
    `connections`/`pending_connections` and the lock that guards moving
    a connection between them are this class's own state for that.
    """

    def __init__(self, node: Node, port: int | None, peer_db: PeerDB) -> None:
        """Set up empty connection tables and queues, and a fresh event loop."""
        super().__init__()
        self.node = node
        self.logger = node.logger
        self.port = port
        self.peer_db = peer_db
        # Core's own `-connect`: dial only the peers it names, with every
        # automatically-drawn outbound connection off
        # (`connOptions.m_use_addrman_outgoing = false`, `src/init.cpp`,
        # at bitcoin/bitcoin@ca7162cde5); `Config.dnsseed` is where its
        # soft-set of DNS seeding is read.
        # `node.config.connect_given`, not `node.config.connect`'s own
        # truthiness: the two disagree under `-connect=0`, which is
        # still the `-connect` arm even though it dials nobody
        # (`Config`'s own field comment). Read once here rather than at
        # each call site below, so a `Config` a caller mutates after
        # building this manager cannot change which arm `run` and
        # `_maybe_dial_more_peers` take mid-flight.
        self.use_addrman_outgoing = not node.config.connect_given
        # Core's own `-listen`, read the same way and for the same
        # reason: whether `_bind` below runs at all, decided once here
        # rather than reread from a `Config` a caller could still
        # mutate underneath `run`.
        self.listen = node.config.listen
        # Core's own division of `-maxconnections`, `CConnman::Init`
        # (`src/net.h`, at bitcoin/bitcoin@9be056a8a7): the outbound
        # slots above come off the top, capped by the total itself, and
        # inbound peers get what is left. `max_outbound_full_relay` is
        # Core's `m_max_outbound_full_relay`, the target
        # `ThreadOpenConnections` dials full-relay peers up to, and
        # `_maybe_dial_more_peers` is the one dial held to it:
        # `async_connect`, the `-connect`/`-addnode` route, reads no
        # bound, as Core's manual connections take no `semOutbound`
        # grant. Read once, for the same reason as the two fields above.
        max_connections = node.config.max_connections
        full_relay = min(_MAX_OUTBOUND_FULL_RELAY_CONNECTIONS, max_connections)
        block_relay = min(
            _MAX_BLOCK_RELAY_ONLY_CONNECTIONS, max_connections - full_relay
        )
        automatic_outbound = full_relay + block_relay + _MAX_FEELER_CONNECTIONS
        self.max_inbound = max(0, max_connections - automatic_outbound)
        self.max_outbound_full_relay = full_relay
        # The size of Core's `semOutbound`,
        # `min(m_max_automatic_outbound, m_max_automatic_connections)`
        # (`src/net.cpp`, same sha): what `ThreadOpenConnections` holds
        # a grant of before its fixed-seed step, whichever kind of
        # automatic connection it goes on to open. Every automatic
        # outbound connection counts against it, `_automatic_outbound`
        # below being the one count of them.
        self.max_automatic_outbound = min(automatic_outbound, max_connections)
        # Core's own `-dnsseed`, `Config.dnsseed` having taken its
        # soft-set: whether `run` schedules the lookup.
        self.use_dns_seed = node.config.dnsseed
        # Core's `-fixedseeds`, cleared once the seeds are added, as
        # `ThreadOpenConnections` clears `add_fixed_seeds`.
        self.add_fixed_seeds = node.config.fixedseeds
        # Core's `vSeedNodes`, shuffled as `CConnman::Start` shuffles it
        # (`src/net.cpp`, same sha), and popped from the end by
        # `_maybe_queue_seed_node` into `_addr_fetches`, Core's
        # `m_addr_fetches`, which `_process_addr_fetch` dials from the
        # front. Only the table's own arm reads it, as only
        # `ThreadOpenConnections`'s does, while `_seednode_given` is
        # Core's `!gArgs.GetArgs("-seednode").empty()`, read whatever
        # the arm.
        self._seed_nodes = list(node.config.seednode)
        secrets.SystemRandom().shuffle(self._seed_nodes)
        self._seednode_given = bool(node.config.seednode)
        self._addr_fetches: deque[tuple[str, int]] = deque()
        # Core's `add_addr_fetch` and `seed_node_timer`, set when
        # `manage_connections` begins.
        self._add_addr_fetch = False
        self._seed_node_timer = 0.0
        # Core's `m_added_node_params` being non-empty, which only
        # `-addnode` fills here: the `addnode` RPC's `add` dials once and
        # keeps no list (`rpc.callbacks.add_node`), so it does not count
        # as it does in Core.
        self._addnode_given = bool(node.config.addnode)
        # Core's `start`, reset when `manage_connections` begins.
        self._dial_start = time.time()
        self._next_fixed_seeds_check = 0.0

        # `-connect` and `-addnode` together, by `endpoint_key`: what
        # `_maybe_redial_specified` below redials once `Node.run`'s own
        # one-shot dial (`__init__.py`, issue #573) drops one of them.
        # Built once, here, for the same "a caller cannot change it
        # mid-flight" reason as the two fields above -- and a plain
        # `dict` rather than a `set`, since a redial needs the address
        # back, not only the key it is compared by.
        self._redial_peers: dict[bytes, NetworkAddressV2] = {
            endpoint_key(address): address
            for address in (
                *(peer_address(host, port) for host, port in node.config.connect),
                *(peer_address(host, port) for host, port in node.config.addnode),
            )
        }
        # Backoff state for the dict above, seeded in `run` rather than
        # here -- `run`'s own comment on `_redial_next` is where the
        # race this seeding avoids is argued.
        self._redial_backoff: dict[bytes, float] = dict.fromkeys(
            self._redial_peers, _REDIAL_BASE_SECONDS
        )
        self._redial_next: dict[bytes, float] = dict.fromkeys(self._redial_peers, 0.0)

        self.connections: dict[int, Connection] = {}
        # A connection accepted or dialled but not yet past `verack`,
        # kept out of `connections` so that nothing iterating it -- the
        # two sends #114 gated, ping housekeeping, `stop_all` -- can
        # reach a peer the handshake has not cleared to speak the rest
        # of the protocol to: btclib-org/btclib-node#131.
        # `promote_connection` is the only way out of this dict, and
        # `callbacks.verack` is the only caller, right where
        # `P2pConnStatus.Connected` is set.
        self.pending_connections: dict[int, Connection] = {}
        # `promote_connection` pops from `pending_connections` and then
        # writes into `connections` -- two statements, not one -- and
        # `remove_connection` pops from `connections` and then, only if
        # that missed, from `pending_connections` -- two statements
        # again. `promote_connection` runs on `Node`'s own loop, off
        # `callbacks.verack`; `remove_connection` runs on this
        # manager's own loop, off `_prune_stale_connections`. Unlocked,
        # a `remove_connection` whose first pop misses because the
        # connection is still pending can run its second pop after
        # `promote_connection` has already moved it, missing it there
        # too -- the connection is live in `connections` with nothing
        # having stopped it (btclib-org/btclib-node#358). This lock is
        # what makes the two pops and the pop-then-write one step
        # apiece; it is also what `_maybe_dial_more_peers` takes to
        # read both dicts as of one instant rather than two
        # (btclib-org/btclib-node#355). Held only across the dict
        # operations themselves in every case above -- never across an
        # `await` or a call into `Connection` -- so nothing here blocks
        # `Node`'s thread for longer than an in-memory pop or a write
        # takes.
        #
        # `stop()`'s own closing sweep (below) reads this same pair
        # unlocked, on purpose: it runs only after `join()`, and its own
        # comment there is what argues nothing but this manager's thread
        # can still be reaching either dict by then, `promote_connection`
        # included -- not a second snapshot-style reader this lock left
        # out.
        self._connections_lock = threading.Lock()
        # (command, payload, connection id, wire size), and for
        # `messages` a receive time after that, below -- the size,
        # `Connection.parse_messages`'s own addition since #462, is what
        # `handle_p2p`/`handle_p2p_handshake` (`p2p/main.py`) weigh back
        # off `queued_recv_bytes`, `MAX_QUEUED_RECV_BYTES`'s own comment
        # (`p2p/connection.py`) arguing why. `handshake_messages` is
        # drained whole every pass of `Node`'s own loop rather than
        # sharing `messages`'s own log2-scaled share
        # (btclib-org/btclib-node#462), and now paces its own reads
        # against the same bound `messages` does: the earlier scoping
        # away from it answered how long a backlog persists, not how
        # large one pass's own backlog could grow before draining it.
        # btclib-org/btclib-node#482
        #
        # Appended only from this manager's own event-loop thread --
        # every `Connection.run` coroutine, whichever connection it
        # belongs to, is multiplexed onto this one thread's asyncio loop
        # -- and popped only from `Node`'s, through
        # `Node._drain_message_queues`. Unlocked on both ends:
        # `deque.append`, `.appendleft` and `.popleft` are each wrapped
        # in their own `Py_BEGIN_CRITICAL_SECTION`/
        # `Py_END_CRITICAL_SECTION` (`Modules/_collectionsmodule.c` and
        # its clinic-generated wrapper, at python/cpython@f54fd2ab6e),
        # which locks the deque's own per-object mutex under a
        # free-threaded build and compiles to nothing under the ordinary
        # GIL one (`Include/critical_section.h`: "no-ops in
        # non-free-threaded builds") -- so a call from each thread can
        # never interleave its own mutation of the same deque with the
        # other's. What that does not cover, two threads calling the
        # same method on one deque at once, never happens here: this is
        # the only appender and `Node`'s thread the only popper.
        # btclib-org/btclib-node#484
        #
        # The receive time is the wall clock time the message was read
        # off the socket: Core's `CNetMessage::m_time`, which
        # `ProcessMessage` takes as `time_received` and reads only in its
        # `pong` handling (`src/net_processing.cpp`, at
        # bitcoin/bitcoin@9be056a8a7), as only `callbacks.pong` reads it
        # here. `handshake_messages` carries no `pong`, and no time.
        self.messages: deque[tuple[str, bytes, int, int, float]] = deque()
        self.handshake_messages: deque[tuple[str, bytes, int, int]] = deque()
        # Every nonce `add_pending_outbound_nonce` (below) has recorded
        # for an outbound connection still short of its own `verack` --
        # `promote_connection` and `remove_connection` below each
        # discard their own connection's entry, so this shrinks exactly
        # as those connections complete or close, rather than sitting
        # in a fixed-size ring. `is_self_connect_nonce` (below) is the
        # only reader, and both it and every write here go through
        # `_connections_lock` above, the same as `pending_connections`
        # and `connections` -- so a `remove_connection` discarding one
        # connection's entry on this manager's own thread can never
        # race a lookup for a different one on `Node`'s.
        self.pending_outbound_nonces: set[int] = set()
        self.last_connection_id = -1
        # The key `keyed_net_group` hashes each peer's netgroup under,
        # Core's `nSeed0`/`nSeed1` for `RANDOMIZER_ID_NETGROUP`: drawn
        # once per process, so no peer can predict which netgroups the
        # eviction's first protection keeps.
        self._net_group_key = secrets.token_bytes(16)
        # The hosts `discourage` has recorded, by `host_key`, oldest
        # first, as values of nothing: `_DISCOURAGED_CAPACITY` is where
        # this is set against Core's `BanMan::m_discouraged`. Process
        # lifetime, not `peer_db`'s own tables, as Core's filter is not
        # written to disk, so a restart forgets them. Locked, as Core's
        # `m_banned_mutex` guards its filter: `discourage` runs on
        # `Node`'s thread and on this manager's, and forgetting the
        # oldest host is a read and a delete that another write must
        # not land between.
        self._discouraged: dict[bytes, None] = {}
        self._discouraged_lock = threading.Lock()
        # 0.0, not `time.time()`: the first pass of `manage_connections`
        # prunes on the spot rather than waiting a full
        # `_ACTIVE_PRUNE_INTERVAL` after this manager was constructed.
        self._last_active_prune = 0.0

        # Set once the listening socket is bound and can hold a peer's
        # connection in its backlog. `is_alive()` says only that this
        # thread was started, which is true before `run` below has
        # scheduled anything, so a peer that dials on the strength of it
        # is refused -- and `dial` answers a refusal with None, which
        # `async_connect` drops. Nothing retries.
        self.listening = threading.Event()
        # set by `run` once it has bound, given up on binding, or been
        # told not to bind by `-listen=0`, which is what
        # `start_listener` waits on
        self._start_attempted = threading.Event()
        # Why the IPv4 bind failed, set by `run` before `_start_attempted`:
        # what Core's `CConnman::Bind` shows the user ahead of "Failed to
        # listen on any port", and `Node.run` hands on the same way
        self.bind_error: str | None = None

        self.loop = asyncio.new_event_loop()
        # What `run` binds and `stop` closes -- kept here rather than
        # only inside the `with server_socket:` each of `server`'s own
        # tasks holds, since that alone depends on this manager's own
        # loop actually delivering that task's cancellation before
        # `stop` returns, which stop()'s own comment on the connections
        # sweep below is not the only place that can go wrong under
        # load (btclib-org/btclib-node#312). Read only by `run` and
        # `stop`, both on this manager's own object and never
        # concurrently -- `run` sets it once, from this thread, before
        # `stop` could possibly be reached by another.
        self._server_sockets: list[socket.socket] = []
        # `server`'s own accept queue, one per listening socket, kept
        # here rather than only local to `server`'s own frame so the two
        # `manager_test.py` tests naming btclib-org/btclib-node#386 can
        # land a connection into the live queue directly -- the seam a
        # bare `await loop.sock_accept` gave their own predecessors
        # before that fix, and gives `_accept_loop` again below
        # (btclib-org/btclib-node#430): what changed is what fills the
        # queue, a task rather than a reader callback, kept behind this
        # same queue so `server`'s own consumption of it is unaffected.
        # Nothing in this class reads it outside `server` and
        # `_accept_loop`.
        self._accept_queues: dict[
            socket.socket,
            asyncio.Queue[
                tuple[socket.socket, tuple[str, int] | tuple[str, int, int, int]]
            ],
        ] = {}

    # Every keyword is one field the connection holds before its task
    # starts, so none can be set after this returns.
    def create_connection(  # noqa: PLR0913
        self,
        client: socket.socket,
        address: NetworkAddressV2,
        *,
        inbound: bool,
        automatic: bool = False,
        addr_fetch: bool = False,
        prefer_evict: bool = False,
    ) -> None:
        """Build a `Connection` for `client`, hold it pending, and start it.

        Logs the id this connection is given beside the address it was
        accepted from or dialled to -- the one point every path into a
        connection shares, before any wire message is parsed, and so
        the only point at which a handshake exception raised before
        `callbacks.verack` reaches its own pairing (`p2p/callbacks.py`)
        still leaves this id resolvable to a peer. `verack`'s own line
        is not redundant with this one despite both naming an address:
        that one marks the handshake completing, this one marks the
        connection existing, and an operator reading `debug.log` wants
        both moments where a connection dies between them.
        btclib-org/btclib-node#611

        `network_address` never raises building that address here: an
        inbound `address` only ever comes from `peer_address` (`server`
        below), which only ever returns the two IP networks
        `network_address` accepts, and an outbound one only reaches
        this method once `dial` (`p2p/address.py`) has already returned
        a live socket for it, which `dial` itself never does for
        anything else (`UnsupportedAddressTypeError`) -- `address_sampler`
        (`p2p/address.py`) filtering `_maybe_dial_more_peers`'s own draw
        to the same two networks first is belt on top of that braces,
        not what does the guarding.

        `info`, matching `verack`'s own line: this runs once per
        connection actually made, dialled or accepted, never once per
        attempt -- `async_connect` and `_maybe_dial_more_peers` below
        only call this once `dial` has already returned a socket, so a
        dial that goes nowhere never reaches here to begin with.

        Unconditional on the address, like `verack`'s own line and for
        the same reason -- argued there rather than twice here: Core's
        analogous site, `CNode`'s own constructor (`src/net.cpp`, at
        bitcoin/bitcoin@05e49b342f), gates the address on `fLogIPs`.
        """
        client.settimeout(0.0)
        self.last_connection_id += 1
        endpoint = network_address(address)
        self.logger.info(
            "%s %s, connection %s",
            "Accepted" if inbound else "Dialled",
            ip_and_port(str(endpoint.ip), endpoint.port),
            self.last_connection_id,
        )
        conn = Connection(
            self, client, address, self.last_connection_id, inbound=inbound
        )
        conn.automatic = automatic
        conn.addr_fetch = addr_fetch
        conn.prefer_evict = prefer_evict
        conn.keyed_net_group = keyed_net_group(self._net_group_key, address)
        self.pending_connections[self.last_connection_id] = conn
        task = asyncio.run_coroutine_threadsafe(conn.run(), self.loop)
        conn.task = task

    def promote_connection(self, connection_id: int) -> None:
        """Move a connection out of the handshake and into the herd.

        The only caller is `callbacks.verack`, right after it sets
        `P2pConnStatus.Connected` -- the two are one step, kept as two
        calls only because the status belongs to the connection and the
        dict it lives in belongs to the manager. `_connections_lock`
        (`__init__`) is what makes the pop and the write one step too,
        against `remove_connection`'s own two pops below, on the other
        thread.

        Successfully connected is exactly the state
        `pending_outbound_nonces` (`__init__`) has to stop answering
        for, so this connection's own nonce leaves it here too, inside
        the same locked block -- `discard` rather than a guarded pop,
        since an inbound connection's nonce, never added there, is just
        as harmless to ask it to remove; the `is not None` guard is only
        for `discard`'s own typing, `set[int]` rather than
        `set[int | None]`.
        """
        with self._connections_lock:
            conn = self.pending_connections.pop(connection_id, None)
            if conn is not None:
                self.connections[connection_id] = conn
                if conn.nonce is not None:
                    self.pending_outbound_nonces.discard(conn.nonce)

    def remove_connection(self, connection_id: int) -> None:
        """Drop `connection_id` from either table and stop it, if it was held.

        `_connections_lock` (`__init__`) is what makes the two pops one
        step, against `promote_connection`'s own pop-then-write. The
        same connection leaving `pending_connections` this way is one
        `pending_outbound_nonces` (`__init__`) has to stop answering for
        too, so its own nonce is discarded inside the same locked block,
        the same reason `promote_connection` above does it there rather
        than after. `conn.stop()` stays outside the lock, as every other
        call into `Connection` from in here does.
        """
        with self._connections_lock:
            conn = self.connections.pop(
                connection_id, None
            ) or self.pending_connections.pop(connection_id, None)
            if conn is not None and conn.nonce is not None:
                self.pending_outbound_nonces.discard(conn.nonce)
        if conn is not None:
            conn.stop()

    def add_pending_outbound_nonce(self, nonce: int) -> None:
        """Record `nonce` as this outbound, still-unhandshaken connection's own.

        The only caller is `Connection.own_version`, for an outbound
        connection. `_connections_lock` (`__init__`) is what every
        access to `pending_outbound_nonces` goes through -- this write
        included -- so it can never land between `is_self_connect_nonce`
        below reading the set and returning.
        """
        with self._connections_lock:
            self.pending_outbound_nonces.add(nonce)

    def is_self_connect_nonce(self, nonce: int) -> bool:
        """Whether `nonce` is a live, unhandshaken outbound connection's own.

        The only caller is `callbacks.version`. Matches Core's own
        live, per-connection search -- `CConnman::CheckIncomingNonce`,
        `net.cpp:360-376` at bitcoin/bitcoin@b91d983f66 -- which walks
        every node still short of `fSuccessfullyConnected` and not
        `IsInboundConn()`, rather than a fixed-size ring:
        `pending_outbound_nonces` (`__init__`) reproduces that search by
        never holding an inbound connection's own nonce to begin with
        (`add_pending_outbound_nonce` above), not by filtering one out
        of a wider set at lookup time.

        That same walk also excludes a private-broadcast connection's
        own nonce, one candidate among the ones it visits -- the reason
        given there is a peer taking such a connection down must not be
        able to infer this node dropped it and learn its clearnet
        address from the disconnect. This tree has no private-broadcast
        connection, so nothing here excludes on that account, and
        nothing here depends on that exclusion existing either.

        Separately, `net_processing.cpp:3886` only calls that walk at
        all for a `version` arriving on an inbound connection -- a
        second restriction, on when the search runs rather than on what
        it searches, and not the one the paragraph above is about. Not
        reproduced here: the set already holds only outbound-origin
        nonces, so an ordinary peer's own draw is never found in it
        regardless of which side received the `version`, and asking
        unconditionally costs nothing extra.
        """
        with self._connections_lock:
            return nonce in self.pending_outbound_nonces

    def discourage(self, address: NetworkAddressV2) -> None:
        """Record the host `address` is on as discouraged, Core's `Discourage`.

        `maybe_discourage_and_disconnect` is the caller, and decides
        which hosts get here. A discouraged host is not dialled by
        `_maybe_dial_more_peers`, is refused by `server` once the
        inbound slots are nearly full and accepted otherwise as the
        first to evict, is left out of a `getaddr` answer, and is not
        stored from an `addr` or `addrv2`.

        Keyed by `host_key`, without the port, as Core's
        `BanMan::Discourage` is (`src/banman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Discouraging a host
        already held moves it to the newest, as a second insert into
        Core's filter does.
        """
        key = host_key(address)
        with self._discouraged_lock:
            self._discouraged.pop(key, None)
            self._discouraged[key] = None
            if len(self._discouraged) > _DISCOURAGED_CAPACITY:
                del self._discouraged[next(iter(self._discouraged))]

    def is_discouraged(self, address: NetworkAddressV2) -> bool:
        """Whether `address`'s host is discouraged, Core's `IsDiscouraged`."""
        key = host_key(address)
        with self._discouraged_lock:
            return key in self._discouraged

    def maybe_discourage_and_disconnect(self, conn: Connection) -> bool:
        """Drop `conn` for misbehaving, and discourage its host as Core would.

        Core's `MaybeDiscourageAndDisconnect` (`src/net_processing.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), answering whether
        the host was discouraged. A manual peer, one `-connect`,
        `-addnode` or the `addnode` RPC dialled, is neither discouraged
        nor dropped. A local peer is dropped alone, since discouraging
        it would discourage every peer on the same local address.
        Otherwise the host is discouraged and every connection held with
        it is dropped, whatever its port and its kind, as Core's
        `DisconnectNode(CSubNet(addr))` does (`src/net.cpp`, same sha).
        """
        address = conn.address
        if not conn.inbound and not conn.automatic:
            return False
        if can_addrv1(address) and is_local(address):
            conn.stop()
            return False
        self.discourage(address)
        key = host_key(address)
        with self._connections_lock:
            held = (*self.connections.values(), *self.pending_connections.values())
        conn.stop()
        for other in held:
            if other is not conn and host_key(other.address) == key:
                other.stop()
        return True

    async def async_connect(self, address: NetworkAddressV2) -> None:
        """Dial `address` and, if it comes up, register the connection.

        Logged rather than silent where `dial` (p2p/address.py) comes
        back with nothing: unlike `_maybe_dial_more_peers` below, whose
        next pass draws another address, and `_maybe_redial_specified`
        beside it, which comes back to the same named peer on its own
        backoff, `connect` is only ever called once per address --
        `Node.run`'s own one-shot startup dial, or a caller reaching for
        one specific peer -- so a dial lost here has nothing behind it
        to try again, and used to vanish with nothing in `debug.log`
        naming it (issue #1020).
        """
        client = await dial(address)
        if client:
            self.create_connection(client, address, inbound=False)
        else:
            endpoint = network_address(address)
            self.logger.info(
                "Dial to %s did not come up",
                ip_and_port(str(endpoint.ip), endpoint.port),
            )

    def connect(self, address: NetworkAddressV2) -> None:
        """Schedule `async_connect(address)` onto this manager's own loop."""
        asyncio.run_coroutine_threadsafe(self.async_connect(address), self.loop)

    def _prune_stale_connections(self, now: float) -> None:
        for conn in self.connections.copy().values():
            if conn.status == P2pConnStatus.Closed:
                self.remove_connection(conn.id)
                continue
            if now - conn.last_receive > _IDLE_TIMEOUT:
                # One read, not `conn.ping_sent` re-read in the `elif`
                # below: `callbacks.pong`, on the other thread, clears
                # it the moment a pong answers this connection's own
                # ping, and a second read landing right after that
                # clear turned `now - 0 > _IDLE_TIMEOUT` true for every
                # `now`, dropping a peer for having just answered.
                # btclib-org/btclib-node#357
                ping_sent = conn.ping_sent
                if common_version(conn) <= BIP0031_VERSION:
                    # no `ping` to wait on (`Connection.send_ping`), so
                    # the whole quiet span is waited out here instead
                    if now - conn.last_receive > 2 * _IDLE_TIMEOUT:
                        self.remove_connection(conn.id)
                elif not ping_sent:
                    conn.send_ping()
                elif now - ping_sent > _IDLE_TIMEOUT:
                    self.remove_connection(conn.id)
        for conn in self.pending_connections.copy().values():
            # Dropped `_PEER_CONNECT_TIMEOUT` after connecting, quiet or
            # not, as Core's `InactivityCheck` drops a connection short
            # of `fSuccessfullyConnected` (btclib-org/btclib-node#1169).
            # No ping in between: `ping` is as much a message the
            # handshake has to clear before it is sent as `inv` or `tx`
            # is (#131). The idle bound above is not asked here, being
            # longer: a connection quiet that long is past this one.
            if (
                conn.status == P2pConnStatus.Closed
                or conn.connected_time + _PEER_CONNECT_TIMEOUT < now
            ):
                self.remove_connection(conn.id)

    def _drop_expired_addr_fetches(self, now: float) -> None:
        """Drop a `-seednode` connection held past `_ADDR_FETCH_TIMEOUT`.

        Core's `SendMessages` drops an `ADDR_FETCH` peer past its
        handshake once that long has passed since it connected
        (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the
        v31.1 tag).
        """
        for conn in self.connections.copy().values():
            if conn.addr_fetch and now - conn.connected_time > _ADDR_FETCH_TIMEOUT:
                self.logger.debug(
                    "addrfetch connection timeout, connection %s", conn.id
                )
                self.remove_connection(conn.id)

    def _maybe_prune_active_addresses(self, now: float) -> None:
        if now - self._last_active_prune < _ACTIVE_PRUNE_INTERVAL:
            return
        # The only other callers of `get_active_addresses` are
        # `address_sampler`, which this loop stops reaching for
        # once it has enough connections, and `getaddr`, answered
        # once per connection and never again -- so a node with
        # enough peers that nobody asks a `getaddr` would
        # otherwise never prune a stale row. btclib-org/btclib-node#71
        self._last_active_prune = now
        try:
            # get_active_addresses deletes every aged-out row
            # from the store, real I/O and not a pure read, and
            # this coroutine's own future is never awaited
            # (`run`, below) -- the same failure mode
            # `_bind_one`'s own docstring names for a coroutine
            # scheduled that way. Unguarded, whatever `db.delete`
            # ever raised would end this loop's pinging, eviction
            # and dialling for the rest of this node's life
            # rather than only this one prune, the same reason
            # the dial below is already inside a `try` of its
            # own.
            self.peer_db.get_active_addresses()
        except Exception:
            self.logger.exception("Exception occurred")

    def _maybe_add_fixed_seeds(self) -> None:
        """Add the chain's fixed seeds for every reachable network held empty.

        `CConnman::ThreadOpenConnections`'s own step ahead of its draw
        (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag):
        once `_FIXED_SEEDS_DELAY` has passed, or at once where DNS
        seeding is off and neither `-seednode` nor `-addnode` was given,
        and once only.
        """
        now = time.time()
        if not self.add_fixed_seeds or now < self._next_fixed_seeds_check:
            return
        self._next_fixed_seeds_check = now + _FIXED_SEEDS_CHECK_INTERVAL
        empty = [
            network
            for network in _REACHABLE_NETWORKS
            if not self.peer_db.holds_network(network)
        ]
        if not empty:
            return
        if now > self._dial_start + _FIXED_SEEDS_DELAY:
            self.logger.info(
                "Adding fixed seeds as 60 seconds have passed and addrman is "
                "empty for at least one reachable network"
            )
        elif (
            not self.use_dns_seed
            and not self._seednode_given
            and not self._addnode_given
        ):
            self.logger.info(
                "Adding fixed seeds as -dnsseed=0 (or IPv4/IPv6 connections are "
                "disabled via -onlynet) and neither -addnode nor -seednode are "
                "provided"
            )
        else:
            return
        seeds = [
            address
            for address in fixed_seed_addresses(self.node.chain.fixed_seeds)
            if address.network_id in empty
        ]
        self.peer_db.add_addresses(seeds)
        self.add_fixed_seeds = False
        self.logger.info("Added %s fixed seeds from reachable networks.", len(seeds))

    def _automatic_outbound(self) -> int:
        """Count the connections holding what Core's `semOutbound` grants.

        This node's own automatic dials, pending ones included, and not
        inbound or manual peers. Locked for the reason
        `_maybe_dial_more_peers` gives.
        """
        with self._connections_lock:
            return sum(
                conn.automatic
                for conn in (
                    *self.connections.values(),
                    *self.pending_connections.values(),
                )
            )

    def _full_relay_outbound(self, *, handshaken: bool = False) -> int:
        """Count the automatic connections that are not `-seednode` ones.

        Core's `IsFullOutboundConn()` peers: `ThreadOpenConnections`
        counts them handshake finished or not, and
        `GetFullOutboundConnCount` only where `fSuccessfullyConnected`,
        which `handshaken` asks for. Locked for the reason
        `_maybe_dial_more_peers` gives.
        """
        with self._connections_lock:
            held = [*self.connections.values()]
            if not handshaken:
                held.extend(self.pending_connections.values())
        return sum(conn.automatic and not conn.addr_fetch for conn in held)

    def _maybe_queue_seed_node(self) -> None:
        """Queue the next `-seednode` where `_add_addr_fetch` asks for one.

        `ThreadOpenConnections`'s first step (`src/net.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), logged in its words.
        """
        if not self._add_addr_fetch:
            return
        self._add_addr_fetch = False
        host, port = seed = self._seed_nodes.pop()
        self._addr_fetches.append(seed)
        if self.peer_db.holds_nothing:
            self.logger.info(
                "Empty addrman, adding seednode (%s) to addrfetch",
                ip_and_port(host, port),
            )
        else:
            self.logger.info(
                "Couldn't connect to peers from addrman after %d seconds. "
                "Adding seednode (%s) to addrfetch",
                _ADD_NEXT_SEEDNODE,
                ip_and_port(host, port),
            )

    async def _process_addr_fetch(self) -> None:
        """Dial the `-seednode` queued first, where an outbound slot is free.

        Core's `ProcessAddrFetch` (`src/net.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the seed leaves the
        queue whether or not it is dialled, as it does there where no
        `semOutbound` grant is free, and `OpenNetworkConnection` refuses
        one this node already holds a connection with.
        """
        if not self._addr_fetches:
            return
        host, port = self._addr_fetches.popleft()
        if self._automatic_outbound() >= self.max_automatic_outbound:
            return
        address = peer_address(host, port)
        with self._connections_lock:
            connected = (
                *self.connections.values(),
                *self.pending_connections.values(),
            )
        if endpoint_key(address) in {endpoint_key(c.address) for c in connected}:
            return
        sock = await dial(address)
        if sock:
            self.create_connection(
                sock, address, inbound=False, automatic=True, addr_fetch=True
            )

    async def _maybe_dial_more_peers(self) -> None:
        # `-connect`'s own other half: `peer_db`'s table is never drawn
        # from at all, on top of `run` below not scheduling the DNS
        # lookup that would otherwise fill it unless `-dnsseed` is
        # given. `Node.run` dials `node.config.connect` directly through
        # `connect()`, which does not pass through here.
        if not self.use_addrman_outgoing:
            return
        # `ThreadOpenConnections`'s own order: a `-seednode` is queued
        # and dialled ahead of the grant below, and whether the next one
        # is due is decided past it. Guarded as the draw below is.
        try:
            self._maybe_queue_seed_node()
            await self._process_addr_fetch()
        except Exception:
            self.logger.exception("Exception occurred")
        # The target does not depend on how far this node has synced:
        # `ThreadOpenConnections` opens a full-relay connection whenever
        # `nOutboundFullRelay < m_max_outbound_full_relay`, from its first
        # pass on. Which peer headers are synced from, one at a time until
        # the best header is recent and one more per block announced by
        # `inv`, is `DownloadManager.sync_headers`'s and `callbacks.inv`'s
        # choice, as it is `net_processing`'s in Core, not a cap on how
        # many peers are dialled.
        #
        # Only this method's own dials count against the target, pending
        # ones included: `CConnman::ThreadOpenConnections` counts
        # `IsFullOutboundConn()` and `IsBlockOnlyConn()` peers in
        # `m_nodes`, handshake finished or not, and leaves inbound and
        # `MANUAL` ones out (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7,
        # the v31.1 tag). Inbound connections cost an attacker nothing, so
        # counting them would let enough of them stop this node choosing
        # any peer of its own. A dial still in flight is in neither table
        # and in neither count: `ThreadOpenConnections` finishes its own
        # `OpenNetworkConnection` before it counts again, as
        # `manage_connections` awaits this method before its next pass.
        #
        # Locked, and the snapshot below locks separately rather than
        # sharing this one: `promote_connection` moves a connection
        # between `connections` and `pending_connections` in two
        # statements, so two unlocked reads, one of each dict, could
        # each miss it, out of
        # `connections` because the read ran before the write, out of
        # `pending_connections` because it ran after the pop, and this
        # count would then undercount a node that already has enough
        # peers (btclib-org/btclib-node#367). A second acquisition
        # rather than one covering both this count and the snapshot
        # below is what keeps the early returns cheap: most passes,
        # once the node already holds enough peers, return ahead of the
        # snapshot, and
        # building `already_connected` -- which such a pass would only
        # throw away -- is not owed every 100 ms just because this
        # count is.
        live = self._automatic_outbound()
        if live >= self.max_automatic_outbound:
            return
        # Past the grant and ahead of the full-relay target, as Core
        # takes a `semOutbound` grant and then adds fixed seeds before
        # it counts full-relay peers, so a node holding all eight of
        # them still seeds. Guarded as the draw below is:
        # `add_addresses` writes to the store.
        try:
            self._maybe_add_fixed_seeds()
        except Exception:
            self.logger.exception("Exception occurred")
        # A `-seednode` connection holds a grant and is no full-relay
        # peer, so the target counts without it.
        full_relay = self._full_relay_outbound()
        now = time.time()
        if (
            self._seed_nodes
            and full_relay < _SEED_OUTBOUND_CONNECTION_THRESHOLD
            and now > self._seed_node_timer + _ADD_NEXT_SEEDNODE
        ):
            self._seed_node_timer = now
            self._add_addr_fetch = True
        if full_relay >= self.max_outbound_full_relay or self.peer_db.is_empty:
            return
        # By endpoint_key, not raw equality: a drawn address
        # carries whatever timestamp and services callbacks.verack
        # or a gossiping peer last recorded it with, which is
        # never the pair an existing Connection's own address was
        # constructed with, so comparing the dataclasses
        # themselves never matches the peer this node is already
        # holding a connection with and dials it a second time.
        #
        # Locked for the same reason the count above is
        # (btclib-org/btclib-node#355).
        with self._connections_lock:
            connected = (
                *self.connections.values(),
                *self.pending_connections.values(),
            )
        already_connected = {endpoint_key(conn.address) for conn in connected}
        # One outbound peer per network group, as
        # `CConnman::ThreadOpenConnections` keeps them: the groups of
        # its `MANUAL`, `OUTBOUND_FULL_RELAY` and `BLOCK_RELAY` peers,
        # which here are every connection neither inbound nor a
        # `-seednode` one, pending ones included. A peer off IPv4 and
        # IPv6 adds no group, as Core adds none for Tor, I2P or CJDNS
        # (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
        outbound_net_groups = {
            net_group(conn.address)
            for conn in connected
            if not conn.inbound and not conn.addr_fetch and can_addrv1(conn.address)
        }
        try:
            await self._dial_one_draw(already_connected, outbound_net_groups)
        except Exception:
            self.logger.exception("Exception occurred")

    async def _dial_one_draw(
        self, already_connected: set[bytes], outbound_net_groups: set[bytes]
    ) -> None:
        """Draw up to `_MAX_DRAWS_PER_PASS` times, and dial at most once.

        Split out of `_maybe_dial_more_peers` for ruff's complexity
        ceiling; that method's own `try` guards it.
        """
        # A draw in the group of an outbound peer is followed by
        # another, up to `_MAX_DRAWS_PER_PASS`, as Core's loop
        # `continue`s on it: one draw a pass would stall wherever
        # the table is mostly such peers (btclib-org/btclib-node#1201).
        draw = self.peer_db.address_sampler()
        for _ in range(_MAX_DRAWS_PER_PASS):
            address = draw()
            # `is_empty` answers whether the table holds anything,
            # not whether it holds anything this node can dial, so
            # `_maybe_dial_more_peers`'s guard lets a table of ipv6
            # and onion addresses through. The draw is what knows,
            # and it answers with nothing: this pass has nothing to
            # do, and `manage_connections`'s sleep is what keeps that
            # from being a spin.
            if address is None:
                break
            if can_addrv1(address) and net_group(address) in outbound_net_groups:
                continue
            # Any other draw ends the pass, as Core's loop breaks
            # with it and `OpenNetworkConnection` (`src/net.cpp`, at
            # bitcoin/bitcoin@9be056a8a7, the v31.1 tag) returns
            # without dialling a peer already connected or
            # discouraged, the latter being one this node dropped
            # for cause (btclib-org/btclib-node#283).
            held = endpoint_key(address) in already_connected
            if not held and not self.is_discouraged(address):
                sock = await dial(address)
                if sock:
                    self.create_connection(sock, address, inbound=False, automatic=True)
            break

    async def _maybe_redial_specified(self) -> None:
        """Redial a `-connect`/`-addnode` peer not connected, on backoff.

        `_redial_peers` above is empty unless `Config.connect`/`addnode`
        named something, so this returns at once for every node that
        did not ask for either -- the ordinary case. A peer already in
        `connections` or `pending_connections` has its backoff reset to
        the floor and is left alone; one that is not, and whose own
        `_redial_next` has passed, is redialled and its backoff doubled
        (capped), the same as a peer this pass could not reach at all --
        distinguishing "reached but the handshake never got anywhere"
        from "could not even be dialled" is not something Core's own
        two loops above do either.
        """
        if not self._redial_peers:
            return
        now = time.time()
        with self._connections_lock:
            connected = {
                endpoint_key(conn.address)
                for conn in (
                    *self.connections.values(),
                    *self.pending_connections.values(),
                )
            }
        for key, address in self._redial_peers.items():
            if key in connected:
                self._redial_backoff[key] = _REDIAL_BASE_SECONDS
                continue
            if now < self._redial_next[key]:
                continue
            self._redial_next[key] = now + self._redial_backoff[key]
            self._redial_backoff[key] = min(
                self._redial_backoff[key] * 2, _REDIAL_MAX_SECONDS
            )
            try:
                await self.async_connect(address)
            except Exception:
                self.logger.exception("Exception occurred")

    async def manage_connections(self) -> None:
        """Prune, prune some more, maybe dial, sleep -- forever, every 0.1s.

        `_prune_stale_connections` pings or drops an idle peer every
        pass; `_maybe_prune_active_addresses` runs far less often;
        `_maybe_dial_more_peers` dials one more only if this node still
        has room for it; `_maybe_redial_specified` is the standing
        redial issue #651 asked for, for `-connect`/`-addnode` alone.
        """
        self._dial_start = time.time()
        self._seed_node_timer = self._dial_start
        self._add_addr_fetch = bool(self._seed_nodes) and self.peer_db.holds_nothing
        if self.use_addrman_outgoing and not self.add_fixed_seeds:
            self.logger.info("Fixed seeds are disabled")
        while True:
            now = time.time()
            self._prune_stale_connections(now)
            self._drop_expired_addr_fetches(now)
            self._maybe_prune_active_addresses(now)
            await self._maybe_dial_more_peers()
            await self._maybe_redial_specified()
            await asyncio.sleep(0.1)

    def _bind_one(self, family: socket.AddressFamily, host: str) -> socket.socket:
        """Bind and listen on one family, synchronously.

        Not the coroutine below: a coroutine handed to
        `run_coroutine_threadsafe` runs on the loop's own thread, behind a
        `concurrent.futures.Future` nobody reads, so a bind failure inside
        one is an `OSError` that vanishes rather than one that reaches
        `run`'s caller (#88). Doing it here instead, before `run_forever`
        is ever called, means the same failure reaches `run` -- this
        thread's target -- which returns on it, so the thread ends
        rather than staying `is_alive()` over a listener that never came
        up, and `start_listener` answers that it is not listening.

        The `OSError` raised says which call failed, and why, in the words
        of Core's `CConnman::BindListenPort` (`src/net.cpp:3307-3373`, at
        bitcoin/bitcoin@9be056a8a7): the message `run` keeps as
        `bind_error`.
        """
        # Core's `CService::ToStringAddrPort`, brackets around an IPv6 host
        address = f"[{host}]" if family == socket.AF_INET6 else host
        address = f"{address}:{self.port}"
        try:
            server_socket = socket.socket(family, socket.SOCK_STREAM)
        except OSError as error:
            msg = (
                "Couldn't open socket for incoming connections (socket returned"
                f" error {_network_error_string(error)})"
            )
            raise OSError(msg) from error
        try:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                # Otherwise a dual-stack kernel hands this socket an
                # inbound v4 peer too, its address mapped into sixteen
                # octets the way #151 has this node refuse to keep
                # gossiped -- and `server` below has no unmapping of its
                # own to give such a connection the network id #151
                # would ask for. Core sets the same option on its own
                # "::" listener for the same reason (net.cpp, 58a7869f86).
                server_socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                server_socket.bind((host, self.port))
            except OSError as error:
                if error.errno == errno.EADDRINUSE:
                    msg = (
                        f"Unable to bind to {address} on this computer."
                        f" {CLIENT_NAME} is probably already running."
                    )
                else:
                    msg = (
                        f"Unable to bind to {address} on this computer (bind"
                        f" returned error {_network_error_string(error)})"
                    )
                raise OSError(msg) from error
            try:
                server_socket.listen()
            except OSError as error:
                msg = (
                    "Listening for incoming connections failed (listen returned"
                    f" error {_network_error_string(error)})"
                )
                raise OSError(msg) from error
            server_socket.settimeout(0.0)
        except OSError:
            # the caller never gets this socket to close: raising it
            # out of a coroutine nobody awaited (#88) is what let a
            # failed bind's fd outlive the exception in the first place
            server_socket.close()
            raise
        return server_socket

    def _bind(self) -> list[socket.socket]:
        """Bind every listener this node has, the IPv4 one required.

        The IPv6 one is not: a host with no IPv6 route or with it turned
        off at the kernel fails the bind above, and that is not this
        node's own defect to end `run` on, unlike a taken IPv4
        port. Core's `InitBinds` treats its own "::" the same way --
        "Don't consider errors to bind on IPv6 '::' fatal because the
        host OS may not have IPv6 support" (net.cpp, 58a7869f86) -- while
        a failure to bind "0.0.0.0" is `BF_REPORT_ERROR` there too.
        """
        # All interfaces, by design: a P2P listener accepts inbound
        # peers from anywhere.
        sockets = [self._bind_one(socket.AF_INET, "0.0.0.0")]  # noqa: S104
        try:
            sockets.append(self._bind_one(socket.AF_INET6, "::"))
        except OSError:
            self.logger.info("No IPv6 P2P listener on port %s", self.port)
        self.listening.set()
        return sockets

    def start_listener(self) -> bool:
        """Start this thread, and answer whether it came up as `-listen` asked.

        Blocks until `run` has bound its listener, failed to, or skipped
        it under `-listen=0`, and answers False only for the failure:
        what `Node.run` turns into Core's "Failed to listen on any port",
        where `start` alone would leave the node running with neither a
        listener nor the dialling `run` schedules only after the bind.
        """
        self.start()
        self._start_attempted.wait()
        return not self.listen or self.listening.is_set()

    async def _accept_loop(
        self,
        server_socket: socket.socket,
        accepted: asyncio.Queue[
            tuple[socket.socket, tuple[str, int] | tuple[str, int, int, int]]
        ],
    ) -> None:
        """`server`'s own producer: one kernel accept at a time, queued.

        A unit test can land a connection directly in `accepted` --
        `P2pManager._accept_queues` is kept for exactly that, `server`'s
        own docstring says where -- and cover this loop's `OSError` arm
        by handing it a socket that only duck-types `.accept()`, without
        a live listener.

        `loop.sock_accept` retries a `BlockingIOError`/`InterruptedError`
        internally on both loop families this node runs on (a selector
        loop's own reader callback, a Windows Proactor loop's overlapped
        `AcceptEx`) and raises anything else -- `ECONNABORTED` being the
        ordinary way, a peer resetting the connection between the kernel
        reporting it readable and the accept reaching it -- which is what
        the `except OSError` below answers exactly as `server`'s own
        former reader callback used to.

        `sock_accept`'s own internal future is already resolved with
        that exception by the time this coroutine's `await` reaches it
        where the failure is synchronous rather than a readiness wait --
        an unbound or otherwise permanently broken socket being the
        degenerate case -- so nothing suspends this task between one
        attempt and the next unless something makes it: the `sleep(0)`
        below is that yield, without which this loop would spin the
        thread's CPU core solid on such a socket and could never be
        cancelled, `Task.cancel` reaching a task only on its next step.
        """
        while True:
            try:
                sock, sockaddr = await asyncio.get_running_loop().sock_accept(
                    server_socket
                )
            except OSError:
                self.logger.exception("Accepting an inbound connection failed")
                await asyncio.sleep(0)
                continue
            sock.settimeout(0.0)
            accepted.put_nowait((sock, sockaddr))

    def _inbound_count(self) -> int:
        """How many of the inbound slots `max_inbound` allows are taken.

        Pending connections count: a peer short of `verack` holds its
        socket and its `Connection` as much as one past it, the same as
        Core counting every inbound `CNode` in `m_nodes` whether or not
        its handshake has finished. Locked for the reason
        `_maybe_dial_more_peers`'s own count is.
        """
        with self._connections_lock:
            return sum(
                conn.inbound
                for conn in (
                    *self.connections.values(),
                    *self.pending_connections.values(),
                )
            )

    def _attempt_to_evict_connection(self) -> bool:
        """Disconnect one inbound peer to make room, and say whether one went.

        Core's `CConnman::AttemptToEvictConnection` (`src/net.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a candidate for every
        connection not already closing, pending ones included, and
        `select_node_to_evict` keeps the outbound ones out. The peer
        chosen leaves through `remove_connection`, the same drop
        `_prune_stale_connections` uses.
        """
        with self._connections_lock:
            conns = {
                conn.id: conn
                for conn in (
                    *self.connections.values(),
                    *self.pending_connections.values(),
                )
                if conn.status < P2pConnStatus.Closed
            }
        evict_id = select_node_to_evict(_eviction_candidate(c) for c in conns.values())
        if evict_id is None:
            return False
        endpoint = network_address(conns[evict_id].address)
        self.logger.debug(
            "selected inbound connection for eviction, disconnecting peer=%d"
            " peeraddr=%s",
            evict_id,
            ip_and_port(str(endpoint.ip), endpoint.port),
        )
        self.remove_connection(evict_id)
        return True

    async def server(
        self, loop: asyncio.AbstractEventLoop, server_socket: socket.socket
    ) -> None:
        """Accept connections off `server_socket`, one `create_connection` each.

        Reads through `accepted`, an `asyncio.Queue` a task of its own,
        `_accept_loop`, fills -- rather than a bare
        `await loop.sock_accept(server_socket)` right here, which does
        not have the property the comment below argues for.
        """
        with server_socket:
            # The queue is what keeps a shutdown from discarding an
            # already-accepted socket reaching `server`'s own consumption
            # below: an item lands in its deque through `put_nowait`, a
            # plain call rather than an await, so nothing this
            # coroutine's own suspension on `accepted.get()` is cancelled
            # out of can ever lose an item already there -- the `finally`
            # closes whatever is left in it on whichever pass reaches
            # this task. That half of btclib-org/btclib-node#386 still
            # holds exactly as it did.
            #
            # What no longer holds is the other half, for `_accept_loop`
            # itself: `loop.add_reader`, which #386 chose over
            # `loop.sock_accept` because the reader callback it registers
            # is never itself a `Task.cancel` target, is not implemented
            # by Windows' own default Proactor loop
            # (btclib-org/btclib-node#430) -- so accepting there has to
            # go back through a task awaiting `loop.sock_accept`, and
            # that task is reachable by `stop`'s own blanket sweep over
            # `asyncio.all_tasks` exactly as #312's shielded task was:
            # `Task.cancel` cannot cancel a future that is already done,
            # so a cancel landing in the narrow window between the
            # kernel resolving one `sock_accept` and `_accept_loop`'s own
            # next step still throws `CancelledError` in regardless,
            # before that socket ever reaches `put_nowait`. CPython's own
            # reference counting is what bounds the cost of that window
            # rather than eliminating it: nothing else holds the
            # discarded socket once its frame unwinds, so it is closed by
            # its own `__del__` -- a `ResourceWarning`, not a leaked
            # descriptor -- in place of the graceful close `finally`
            # gives every item that did reach the queue. A peer that
            # dials in that exact instant of shutdown loses the
            # connection it just opened; nothing during ordinary
            # operation reaches this window at all, `_accept_loop` never
            # otherwise stopping.
            accepted: asyncio.Queue[
                tuple[socket.socket, tuple[str, int] | tuple[str, int, int, int]]
            ] = asyncio.Queue()
            self._accept_queues[server_socket] = accepted
            accept_task = loop.create_task(self._accept_loop(server_socket, accepted))
            try:
                while True:
                    sock, sockaddr = await accepted.get()
                    # two fields for an AF_INET peer, four for an
                    # AF_INET6 one -- the flow info and the scope id
                    # BIP155 has nowhere to carry either,
                    # `get_addr_from_dns`'s own sockaddr comment being
                    # where that is argued
                    address = peer_address(*sockaddr[:2])
                    # Core's `CreateNodeFromAcceptedSocket` (`src/net.cpp`,
                    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), on one
                    # count of the inbound peers: a discouraged host is
                    # refused once one more peer would fill the inbound
                    # share; past that share an inbound peer is evicted
                    # to make room, and only where every candidate is
                    # protected is the new peer refused. Either refusal
                    # comes before `create_connection` builds anything.
                    inbound = self._inbound_count()
                    discouraged = self.is_discouraged(address)
                    if discouraged and inbound + 1 >= self.max_inbound:
                        endpoint = network_address(address)
                        self.logger.debug(
                            "connection from %s dropped (discouraged)",
                            ip_and_port(str(endpoint.ip), endpoint.port),
                        )
                        sock.close()
                        continue
                    if (
                        inbound >= self.max_inbound
                        and not self._attempt_to_evict_connection()
                    ):
                        self.logger.debug(
                            "failed to find an eviction candidate"
                            " - connection dropped (full)"
                        )
                        sock.close()
                        continue
                    self.create_connection(
                        sock, address, inbound=True, prefer_evict=discouraged
                    )
            finally:
                # Already cancelled directly by `stop`'s own sweep
                # whenever that is how this task ends too -- both are in
                # the same `asyncio.all_tasks` snapshot -- so this is for
                # the caller that cancels `server` alone, such as a test
                # exercising it outside `stop`, where nothing else would
                # ever join this task.
                accept_task.cancel()
                with suppress(asyncio.CancelledError):
                    await accept_task
                del self._accept_queues[server_socket]
                while not accepted.empty():
                    accepted.get_nowait()[0].close()

    def _report_server_failure(self, future: Future[None]) -> None:
        """Log what `server`'s own scheduled task ends on, loudly.

        `run` below schedules `server` through `run_coroutine_threadsafe`
        and never awaits the `concurrent.futures.Future` it returns --
        deliberately, `server` running for this manager's whole lifetime
        rather than returning -- so an exception it raises before ever
        reaching its own accept loop would otherwise surface only
        through asyncio's own "Task exception was never retrieved"
        warning: timed to whenever the garbage collector reaches that
        future rather than to the failure itself, and written to the
        `asyncio` logger rather than this node's own.
        `btclib-org/btclib-node#88` fixed the identical shape for `_bind`
        by making the bind synchronous instead, which `server` cannot be,
        it being this manager's own listener for as long as it runs.

        `stop`'s own sweep ending this task on purpose is not logged, in
        two different ways depending on how the cancellation actually
        unwound: the ordinary one is `future` itself in the cancelled
        state, `Task.cancelled()` being true because `server` ended on
        the exact `CancelledError` `Task.cancel()` threw in, which
        `future.exception()` answers by raising rather than returning --
        the `with suppress` below is what that arm is. The `isinstance`
        arm below it is for a `CancelledError` `future.exception()`
        returns instead of raising: `server`'s own coroutine chain
        catching and re-raising a `CancelledError` that was not the one
        `Task.cancel()` threw would leave `Task.cancelled()` false while
        still ending on that same exception type, and this is what
        keeps that from being logged as a failure too.
        """
        with suppress(CancelledError):
            exc = future.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                self.logger.error("P2P listener's accept loop ended", exc_info=exc)

    async def _dns_address_seed(self) -> None:
        """Wait on the `-seednode` peers, then ask the DNS seeds if still owed.

        Core's `ThreadDNSAddressSeed` (`src/net.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), as far as its
        `-seednode` wait and the decision after it, logged in its words.
        Whether the seeds are then asked is `PeerDB.get_addr_from_dns`'s
        own `ask_dns_nodes`.
        """
        outbound = 0
        if self._seednode_given:
            start = time.time()
            self.logger.info(
                "-seednode enabled. Trying the provided seeds for %d seconds "
                "before defaulting to the dnsseeds.",
                _SEEDNODE_TIMEOUT,
            )
            while True:
                await asyncio.sleep(_SEEDNODE_CHECK_INTERVAL)
                if time.time() > start + _SEEDNODE_TIMEOUT:
                    self.logger.info(
                        "Couldn't connect to enough peers via seed nodes. "
                        "Handing fetch logic to the DNS seeds."
                    )
                    break
                outbound = self._full_relay_outbound(handshaken=True)
                if outbound >= _SEED_OUTBOUND_CONNECTION_THRESHOLD:
                    self.logger.info(
                        "P2P peers available. Finished fetching data from seed nodes."
                    )
                    break
        # Core's `seeds_right_now`, which only an empty table sets here,
        # this node having no `-forcednsseed`
        if (
            outbound >= _SEED_OUTBOUND_CONNECTION_THRESHOLD
            and not self.peer_db.holds_nothing
        ):
            self.logger.info("Skipping DNS seeds. Enough peers have been found")
            return
        await self.peer_db.get_addr_from_dns()

    @override
    def run(self) -> None:
        loop = self.loop
        # Core's own `-listen=0`: no bind, no accept, outbound dialling
        # untouched -- `_bind`'s own listener socket is the only thing
        # this skips, `manage_connections` and the dial loop below both
        # running on this same loop regardless of whether `_bind` below
        # ever ran.
        server_sockets: list[socket.socket] = []
        try:
            self.logger.info("Starting P2P manager")
            asyncio.set_event_loop(loop)
            if self.listen:
                server_sockets = self._bind()
        except OSError as error:
            # `start_listener` reads the failure off `listening`, so it
            # is not raised into `threading.excepthook` as well; nothing
            # is scheduled, as Core's `CConnman::Start` returns before
            # starting any of its threads
            self.bind_error = str(error)
            self.logger.exception(self.bind_error)
            return
        finally:
            self._start_attempted.set()
        self._server_sockets = server_sockets
        # `AppInitMain`'s and `CConnman::Start`'s own lines (`src/init.cpp`,
        # `src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
        if self.node.config.connect and self._seednode_given:
            self.logger.info("-seednode is ignored when -connect is used")
        if self.use_dns_seed:
            asyncio.run_coroutine_threadsafe(self._dns_address_seed(), loop)
        else:
            self.logger.info("DNS seeding disabled")
        for server_socket in server_sockets:
            asyncio.run_coroutine_threadsafe(
                self.server(loop, server_socket), loop
            ).add_done_callback(self._report_server_failure)
        # Seeded here, immediately before `manage_connections` is ever
        # scheduled, rather than at `__init__` time: `Node.run`'s own
        # one-shot dial for these same peers (`__init__.py`, issue
        # #573) races this manager's first `manage_connections` pass,
        # each reaching `async_connect` from a different thread, and a
        # peer `_redial_next` already called overdue by the time this
        # loop starts would sometimes win that race and dial a peer
        # `Node.run` is dialling in the same instant. A `__init__`-time
        # seed cannot answer that: an unknown, possibly long, gap sits
        # between building this manager and `start()` ever being
        # called on it.
        now = time.time()
        for key in self._redial_next:
            self._redial_next[key] = now + _REDIAL_BASE_SECONDS
        asyncio.run_coroutine_threadsafe(self.manage_connections(), loop)
        loop.run_forever()

    def stop(self) -> None:
        """Stop this manager's own loop, then every connection and task on it.

        The comment below is the whole of what makes `stop_handle`
        itself safe to cancel unconditionally, on any of the three ways
        `run` above can have left this loop by the time `join` returns.
        """
        stop_handle = self.loop.call_soon_threadsafe(self.loop.stop)
        # `join` blocks this thread without spinning it, the way
        # `Node.stop` already waits on itself with `self.join`. Guarded
        # on `is_alive`, since `Node.run` calls this unconditionally --
        # a node with `p2p_port` unset never calls `start`, and `join`
        # on a thread that was never started raises.
        if self.is_alive():
            self.join()
        # `stop_handle.cancel()` is what makes every `run_until_complete`
        # below safe, on any loop this method could possibly be handed --
        # not one more guard clause alongside `self.ident` and `pending`,
        # which is what #368 and #362 each tried and #380 and #377 each
        # found a gap in. The `call_soon_threadsafe` above only
        # *schedules* `loop.stop`; it is delivered -- `self._stopping`
        # set, so `run_forever` returns after its current pass -- only
        # once something actually drives this loop's `run_forever` far
        # enough to reach it. Three things can happen by the time `join`
        # above returns:
        #
        # - This manager's own thread was running `run_forever` (the
        #   ordinary case) and delivered it there, exiting on its own.
        #   `join` already waited for exactly that, so the handle has
        #   already fired and is spent.
        # - This thread was never started at all (`self.ident is None`)
        #   -- `is_alive()` above is `False`, `join` is skipped, and
        #   nothing has ever driven this loop, so the handle is still
        #   sitting in its ready queue, undelivered.
        # - This thread was started and `run()` returned before ever
        #   reaching `run_forever` -- a bind failure being the ordinary
        #   way (btclib-org/btclib-node#353) -- so `self.ident is not
        #   None` even though `run_forever`, again, never ran: the
        #   handle is undelivered the same as the case above, which is
        #   exactly what defeated `self.ident is not None` as a guard
        #   (btclib-org/btclib-node#380).
        #
        # `Handle.cancel()` on a handle already delivered is specified as
        # a no-op -- there is nothing left to remove from a ready queue
        # already drained of it -- so calling it here unconditionally is
        # correct for the first case above and is what removes the
        # landmine outright for the other two, rather than merely
        # stepping past where it goes off once (#368) and leaving every
        # `run_until_complete` downstream of that first step still primed
        # to hit it (btclib-org/btclib-node#377): a task whose own
        # cancellation needs a second real step to unwind -- an `except
        # CancelledError` handler that awaits a fresh timer rather than
        # only an already-cancelled future -- is not owed anything by a
        # single guarded step, only by there being no leftover stop left
        # to answer at all. `RpcManager.stop` carries the identical fix,
        # for the identical reason (btclib-org/btclib-node#377,
        # btclib-org/btclib-node#380).
        stop_handle.cancel()
        # Only after join(), not before: `run()` above has now returned,
        # so nothing but this thread can still be adding to
        # `self.connections`/`self.pending_connections` -- `create_connection`
        # and `remove_connection` are only ever reached from a coroutine
        # on this manager's own loop, and `promote_connection`, `Node`'s
        # thread's own exception, cannot race a `stop()` that same
        # thread is itself blocked inside. A sweep taken before join()
        # closed whatever it snapshotted correctly but could still miss
        # a connection `server()`'s own accept loop created in the
        # window between `loop.stop` merely being scheduled above and
        # actually being delivered -- accepted, given a task, and never
        # swept, since nothing before join() ever looked again. Such a
        # task reaches only the cancellation below, which cannot close
        # `Connection.client` for it: `Task.cancel()` called before a
        # task has run even once skips the coroutine entirely, `run()`'s
        # own `finally` included (btclib-org/btclib-node#312).
        #
        # And a pass of each is not enough, because `run_until_complete`
        # runs the loop: a task this pass has not cancelled yet goes on
        # working while an earlier one is being drained. `server()` is
        # the one that matters -- it takes what the kernel left in the
        # listen backlog during that same window and hands it to
        # `create_connection`, which registers a connection the sweep has
        # already passed and gives it a task no snapshot taken before the
        # drain holds. Nothing closes that socket and nothing ends that
        # task, so `loop.close()` below leaves it pending at
        # `Connection.run`'s own `sock_recv` for the collector to report.
        # Cancelling every task before the loop is allowed to run again
        # is what answers that. Repeating the whole thing until the loop
        # has no tasks left is the postcondition stated outright rather
        # than argued from who is still able to call `create_connection`,
        # and it terminates because the accept loop is cancelled on the
        # first pass (btclib-org/btclib-node#312).
        while True:
            for conn in (
                *self.connections.values(),
                *self.pending_connections.values(),
            ):
                conn.stop()
            pending = asyncio.all_tasks(self.loop)
            if not pending:
                break
            # No step of the loop first here, unlike an earlier version
            # of this method: that step existed only to let a task
            # sitting on an already-resolved future -- `server`'s own
            # former `accept` task -- return normally into
            # `create_connection` before a direct cancel discarded it,
            # `Task.cancel` on a task whose own awaited future is
            # already done forcing `CancelledError` in on its next step
            # regardless of what the future already held
            # (btclib-org/btclib-node#312, for a cancel arriving through
            # `server`'s own shield; btclib-org/btclib-node#353 and this
            # loop's own former blanket sweep, for one reaching that task
            # directly). `server` no longer has such a task to protect:
            # what it accepts sits in a queue instead, and `server`'s own
            # consumption of that queue is immune to that discard
            # regardless of when the cancel below reaches it
            # (btclib-org/btclib-node#386). `_accept_loop`'s own
            # production side of the same queue is not -- `server`'s own
            # docstring has the reason and the bound on what it costs
            # (btclib-org/btclib-node#430). `stop_handle.cancel()` above
            # already closed the other reason an earlier version of this
            # step existed, a `RuntimeError` this loop could raise
            # running `pending`'s own already-scheduled tasks on a loop
            # whose `run_forever` never delivered this method's own
            # `loop.stop` (btclib-org/btclib-node#377,
            # btclib-org/btclib-node#380) -- so neither of the two
            # reasons this step used to answer still applies.
            #
            # every one of them before the loop is allowed to run again,
            # rather than cancelling and draining one at a time, which is
            # what leaves the accept loop live for the whole drain
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    self.loop.run_until_complete(task)
        # Closed here and not by `server`'s own `with server_socket:`,
        # which is skipped outright where `stop` arrives before that task
        # has taken a first step: the cancellation is then thrown into a
        # coroutine that has no frame yet, exactly as it is for
        # `Connection.run` above, so the `with` is never entered and the
        # socket is left listening. A node stopped that soon after
        # `start` is where that happens. A `with` block that did run
        # leaves nothing here for `close()` to do, a socket being closed
        # only once whichever call reaches it first
        # (btclib-org/btclib-node#312).
        for server_socket in self._server_sockets:
            server_socket.close()
        self.loop.close()
        # so that the flag says what its name says: a socket
        # closed here is not one anything should wait for
        self.listening.clear()
        self.logger.info("Stopping P2P Manager")

    def send(self, msg: Payload, connection_id: int) -> None:
        """Send `msg` on `connection_id`, a no-op if that connection is gone."""
        # `.get()`, not `in` then `[...]`: `remove_connection` pops
        # from `connections` on this manager's own loop, off
        # `_prune_stale_connections`, every pass of `manage_connections`
        # -- a caller on `Node`'s own loop that passed the `in` and was
        # preempted before the subscript would otherwise see the
        # `KeyError` reach whatever called `send`. A connection missing
        # here means the peer is already gone by the time this runs, so
        # there is nothing to send it to and this is a no-op, the same
        # answer `download.py`'s own `_request_wanted_txs` gives a
        # `connections.get` that misses. btclib-org/btclib-node#359
        conn = self.connections.get(connection_id)
        if conn is not None:
            conn.send(msg)

    def broadcast_raw_transaction(self, tx: BtclibTx, fee: int) -> None:  # noqa: ARG002
        """Queue `tx` for the inv/getdata round trip, not a direct send.

        The comment below is where this, and `fee` going unread here,
        are argued.
        """
        # `DownloadManager.tx_download`'s own queue, with no peer to
        # exclude as already holding it, rather than a push of its own:
        # a direct, unsolicited `Tx` to every peer the instant this
        # method is called would have been the one thing that told
        # apart a transaction of this node's own from one it relayed --
        # the delay and the `inv`/`getdata` round trip are what a
        # relayed transaction gets, so a locally originated one goes
        # through them too. `getdata`'s own handler serves a `tx` it
        # finds in the mempool, so this call answers for what a peer
        # asks back only where the caller has already put it there --
        # `send_raw_transaction` (rpc/callbacks.py) does, before calling
        # this. btclib-org/btclib-node#141
        #
        # `fee` is accepted rather than read here: the same caller has
        # just recorded it in `node.mempool.add_tx(tx, fee)`, which is
        # where `tx_download`'s own BIP133 feefilter check
        # (`Mempool.meets_fee_rate`) reads it from, keyed by the same
        # wtxid this queues -- one record rather than a second copy of
        # it threaded through `received_txs` too. btclib-org/btclib-node#260
        self.node.download_manager.received_txs.append((None, tx.hash))

    def ping_all(self) -> None:
        """Send every connected peer a fresh `ping`, as `send_ping` allows.

        A peer at `BIP0031_VERSION` or below is sent none.
        btclib-org/btclib-node#1204
        """
        for conn in self.connections.copy().values():
            conn.send_ping()

    def stop_all(self) -> None:
        """Stop every connection this manager holds, per the comment below."""
        # every socket this manager holds, handshake finished or not:
        # a peer mid-`verack` is still a peer to close on shutdown
        for conn in (
            *self.connections.copy().values(),
            *self.pending_connections.copy().values(),
        ):
            conn.stop()


def _eviction_candidate(conn: Connection) -> EvictionCandidate:
    """Build the `NodeEvictionCandidate` Core builds for `conn`, where it can.

    Where a field of Core's has nothing here to be read from, it takes
    the value that protects nobody:

    - `fBloomFilter`: this node answers no BIP37 `filterload`.
    - `m_noban`: this node has no `-whitebind`/`-whitelist` permissions.
    - an onion peer's `m_network`: this node has no Tor listener, so
      `net_class` answers from the address alone.

    `m_relay_txs` is the peer's `version` relay flag once that message
    has arrived and false before it, as Core's `m_relays_txs` is. It is
    read off `version_message` itself rather than `Connection.relay_tx`,
    which `callbacks.version` writes later, so a selection landing
    between the two writes reads the flag and not `relay_tx`'s default.
    """
    version_message = conn.version_message
    return EvictionCandidate(
        id=conn.id,
        connected=conn.connected_time,
        min_ping_time=conn.min_ping_time,
        last_block_time=conn.last_novel_block_time,
        last_tx_time=conn.last_novel_tx_time,
        relevant_services=conn.has_all_wanted_services,
        relay_txs=version_message is not None and version_message.is_relay_requested,
        bloom_filter=False,
        keyed_net_group=conn.keyed_net_group,
        prefer_evict=conn.prefer_evict,
        is_local=is_local(conn.address),
        network=net_class(conn.address),
        noban=False,
        inbound=conn.inbound,
    )
