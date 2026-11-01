# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`RpcManager`, the thread listening for JSON-RPC connections.

Runs its own asyncio loop, with a `RpcConnection` per socket
`_accept_loop` accepts from an allowed source -- one request or
several, `connection.RpcConnection`'s own docstring has the keep-alive
that decides which -- and queuing what
each one parses onto `messages` for `Node`'s own thread to read in
`rpc.main.handle_rpc`. `listening` is what a caller waits on rather
than `is_alive()` alone, that flag being true before anything is bound.
`auth` is who may call it, and which methods. `run` binds, then writes
the cookie where one is written, then sets `listening`, so a client that
waits on `listening` finds the cookie there; `stop` deletes it. That is
the order of Core's `AppInitServers` (`src/init.cpp:748-761`, at
bitcoin/bitcoin@9be056a8a7): `InitHTTPServer` binds before
`StartHTTPRPC` writes the cookie, so a bind that fails leaves no cookie
behind. `start_listener` is how `Node` learns that
either step failed, which Core turns into an `InitError`.

At most `max_connections` clients are held at once, Core's
`-rpcmaxconnections` (`config.DEFAULT_MAX_HTTP_CONNECTIONS` has the
default and the version that sets it). At the limit the accept loops
stop accepting, so a new client waits in the kernel's queue, holding no
descriptor of this process, until a held one leaves, as
`HTTPServer::GenerateWaitSockets` leaves it (`src/httpserver.cpp`, at
bitcoin/bitcoin@649bc0a645). `_accept_loop` has the one client per
listening socket that can be accepted past it.
"""

import asyncio
import errno
import socket
import sys
import threading
import time
from collections import deque
from concurrent.futures import CancelledError
from contextlib import suppress
from ipaddress import IPv4Address, IPv6Address
from typing import TYPE_CHECKING, Any, override

from btclib_node.config import lookup_service, service_text, split_host_port
from btclib_node.constants import CLIENT_NAME
from btclib_node.exceptions import RpcCredentialRefusedError
from btclib_node.p2p.address import ip_and_port
from btclib_node.p2p.banman import SpecialAddress
from btclib_node.p2p.manager import network_error_string
from btclib_node.rpc.allow import allowed_subnets, client_allowed
from btclib_node.rpc.auth import RpcAuth
from btclib_node.rpc.connection import RpcConnection
from btclib_node.rpc.main import answer_warmup

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence
    from concurrent.futures import Future

    from btclib_node import Node
    from btclib_node.log import Logger
    from btclib_node.p2p.banman import Host, Subnet
    from btclib_node.rpc.jsonrpc import HttpReply

__all__ = ["RpcManager"]

# What `GetBindAddresses` binds without `-rpcbind` and `-rpcallowip`
# both given, in its order (`src/httpserver.cpp`,
# at bitcoin/bitcoin@f0de87f040, the v32.1 tag).
_LOOPBACK_HOSTS = ("::1", "127.0.0.1")

# The seconds `StopHTTPServer` waits, once its workers have stopped, for
# its clients to disconnect (`src/httpserver.cpp`, at
# bitcoin/bitcoin@f0de87f040, the v32.1 tag)
_DISCONNECT_WAIT = 30.0


# What `accept` fails with where no descriptor is left, for this process
# or for the system. A connection the kernel leaves queued makes the next
# accept fail at once too. Core logs each failure at debug and tries
# again on its loop's next pass (`HTTPServer::AcceptConnection`,
# `src/httpserver.cpp`, at bitcoin/bitcoin@649bc0a645), a pass that can
# come at once while the listening socket stays readable.
# `_ACCEPT_RETRY_DELAY` is the seconds `_accept_loop` waits instead, a
# departure so that it does not spin this manager's loop
# (btclib-org/btclib-node#1953).
_OUT_OF_DESCRIPTORS = frozenset((errno.EMFILE, errno.ENFILE))
_ACCEPT_RETRY_DELAY = 0.1


def _bind_and_listen(host: Host, port: int, logger: Logger) -> socket.socket:
    """Return a socket listening at `host` and `port`, non-blocking.

    Core's `HTTPServer::BindAndStartListening` (`src/httpserver.cpp`, at
    bitcoin/bitcoin@f0de87f040, the v32.1 tag): `SO_EXCLUSIVEADDRUSE` on
    Windows, so that no other process binds the port, `SO_REUSEADDR`
    elsewhere, and `IPV6_V6ONLY` on an IPv6 socket, a failure to set
    either of the last two logged and passed over. Raises `OSError` in
    Core's words where any other step fails, the socket closed.

    Core also sets `IPV6_PROTECTION_LEVEL` on Windows, and this does not:
    `P2pManager._bind_one` leaves it unset for Core's `BindListenPort`
    too (btclib-org/btclib-node#2163).
    """
    address = service_text(host, port)
    if isinstance(host, SpecialAddress):
        err_msg = f"Bind address family for {address} not supported"
        raise OSError(err_msg)
    family = socket.AF_INET if isinstance(host, IPv4Address) else socket.AF_INET6
    try:
        server_socket = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    except OSError as error:
        err_msg = (
            f"Cannot create {address} listen socket: {network_error_string(error)}"
        )
        raise OSError(err_msg) from error
    try:
        if sys.platform == "win32":  # pragma: no cover -- the windows-latest cell
            try:
                server_socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
                )
            except OSError as error:
                err_msg = (
                    f"Cannot set SO_EXCLUSIVEADDRUSE on {address} listen socket: "
                    f"{network_error_string(error)}"
                )
                raise OSError(err_msg) from error
        else:
            _set_or_log(
                server_socket, socket.SOL_SOCKET, "SO_REUSEADDR", address, logger
            )
        if family == socket.AF_INET6:
            _set_or_log(
                server_socket, socket.IPPROTO_IPV6, "IPV6_V6ONLY", address, logger
            )
        try:
            server_socket.bind(_sockaddr(host, port))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                err_msg = (
                    f"Unable to bind to {address} on this computer. "
                    f"{CLIENT_NAME} is probably already running."
                )
            else:
                err_msg = (
                    f"Unable to bind to {address} on this computer (bind returned "
                    f"error {network_error_string(error)})"
                )
            raise OSError(err_msg) from error
        try:
            server_socket.listen(socket.SOMAXCONN)
        except OSError as error:
            err_msg = f"Cannot listen on {address}: {network_error_string(error)}"
            raise OSError(err_msg) from error
        server_socket.settimeout(0.0)
    except OSError:
        server_socket.close()
        raise
    return server_socket


def _sockaddr(
    host: IPv4Address | IPv6Address, port: int
) -> tuple[str, int] | tuple[str, int, int, int]:
    """Return the address `bind` takes for `host`, an IPv6 one with its scope.

    CPython reads a scope written after `%` in a 2-tuple's host as 0, so
    `fe80::1%1` binds only as a 4-tuple naming scope 1.
    """
    if isinstance(host, IPv4Address):
        return str(host), port
    return str(host).partition("%")[0], port, 0, int(host.scope_id or 0)


def _set_or_log(
    server_socket: socket.socket, level: int, name: str, address: str, logger: Logger
) -> None:
    """Turn the option `name` on, or log at debug that it could not be."""
    try:
        server_socket.setsockopt(level, getattr(socket, name), 1)
    except OSError as error:
        logger.log_debug(
            "http",
            "Cannot set %s on %s listen socket: %s, continuing anyway",
            name,
            address,
            network_error_string(error),
        )


def _idle_timeout(seconds: int) -> float | None:
    """Return the idle timeout `-rpcservertimeout=<seconds>` sets, in seconds.

    `None` for none: Core arms it only for a value above 0
    (`HTTPRemoteClient::MaybeDisconnect`, `src/httpserver.cpp`, at
    bitcoin/bitcoin@f0de87f040, the v32.1 tag). There Core compares the
    idle time, a duration of its steady clock in nanoseconds, with the
    option's seconds, so it converts them to nanoseconds as a signed
    64-bit count, which wraps past `INT64_MAX`. This wraps the same way.
    A result below 0 closes a connection before it reads anything:
    bitcoind v32.1.0 keeps an idle connection open at `9223372036` and
    closes it at once at `9223372037` (btclib-org/btclib-node#2152).
    """
    if seconds <= 0:
        return None
    nanoseconds = (seconds * 10**9 + 2**63) % 2**64 - 2**63
    return nanoseconds / 10**9


class RpcManager(threading.Thread):
    """The thread listening for JSON-RPC connections.

    The module docstring above is where its own loop and the boundary
    with `Node`'s thread are argued; `connections` and `messages` are
    this class's own state for that, and `_accept_queue` is `server`'s
    own, kept here only so a test can reach it directly.
    """

    def __init__(self, node: Node, port: int | None) -> None:
        """Set up empty connection and message tables, and a fresh loop."""
        super().__init__()
        self.node = node
        self.logger = node.logger
        self.chain = node.chain
        self.connections: dict[int, RpcConnection] = {}
        # a decoded body and the id of the connection it arrived on
        self.messages: deque[tuple[object, int]] = deque()
        self.loop = asyncio.new_event_loop()
        self.port = port
        self.last_connection_id = -1
        # handed to every RpcConnection create_connection below builds;
        # an instance attribute, so that a test can lower it on a live
        # manager before it opens the connection it means to idle out
        self.idle_timeout = _idle_timeout(node.config.rpcservertimeout)
        # how long `_finish_replies` waits for replies still being
        # written, an instance attribute for the same reason
        self.disconnect_wait = _DISCONNECT_WAIT
        # `-rpcmaxconnections`: the most entries `connections` holds. An
        # instance attribute, as `idle_timeout` is, so a test can lower
        # it on a live manager.
        self.max_connections = node.config.rpcmaxconnections
        # Set by `forget_connection` as an entry leaves `connections`,
        # awaited by `_until_room` on this manager's loop alone
        self._room = asyncio.Event()
        # `Config`'s users and whitelists, and the cookie's once `run`
        # writes it: what `RpcConnection.run` checks every request against
        self.auth = RpcAuth.from_config(node.config)
        # loopback and `-rpcallowip`'s subnets, as `_listen` parses them
        self.allowed_subnets: tuple[Subnet, ...] = allowed_subnets(())
        # what `Node` shows ahead of Core's "Unable to start HTTP
        # server" where `_listen` refused a `-rpcallowip` value, as
        # `InitHTTPAllowList` shows its own message ahead of that one
        self.init_error: str | None = None

        # see P2pManager.listening: `is_alive()` is true before `run`
        # has bound anything, and a client that posts on the strength of
        # it is refused
        self.listening = threading.Event()
        # set beside `listening`, in `_listen`, and never cleared:
        # `stop` clears `listening` once this thread has already ended,
        # which on its own reads exactly like a bind that never came up
        # at all. This is what `wait_until_listening` reads to tell the
        # two apart (btclib-org/btclib-node#1361).
        self.ever_listened = threading.Event()
        # set by `run` once it has either set `listening` or given up on
        # it, which is what `start_listener` waits on
        self._start_attempted = threading.Event()
        # why `run` gave up, set by it before `_start_attempted`: what
        # `wait_until_listening` names where this thread ended without
        # listening (btclib-org/btclib-node#1231)
        self.bind_error: str | None = None
        # Set by `interrupt`, read by every `RpcConnection.run` on this
        # manager's own loop: Core's `ThreadPool`'s own `m_interrupt`,
        # set by `InterruptHTTPServer`'s call to
        # `g_threadpool_http.Interrupt()` ahead of `StopHTTPServer`'s own
        # `Stop()` (`src/httpserver.cpp`, `src/util/threadpool.h`,
        # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag). A `threading.Event`
        # rather than a plain attribute for the same reason `listening`
        # and `ever_listened` are: it is written on `Node`'s thread and
        # read on this manager's own. `stop` also sets it,
        # unconditionally, as `ThreadPool::Stop` sets `m_interrupt`
        # itself too rather than relying on `Interrupt` having already
        # been called (btclib-org/btclib-node#1515).
        self.interrupted = threading.Event()
        # Held to set `interrupted`, and by `RpcConnection.run` from
        # reading it to queuing onto `messages`: `ThreadPool`'s own
        # `m_mutex`, which guards `m_interrupt` and `m_work_queue`
        # together (same file, same tag), so a request is either on
        # `messages` before `interrupt` returns or refused
        # (btclib-org/btclib-node#1515).
        self.queue_lock = threading.Lock()
        # Core's `fRPCInWarmup` and `rpcWarmupStatus` (`src/rpc/server.cpp`,
        # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): while `in_warmup`
        # holds, every request is answered `RPC_IN_WARMUP` with
        # `warmup_status`, on this manager's thread, since `Node`'s is
        # busy opening the stores. Written under `queue_lock`, which
        # `RpcConnection.run` holds from reading `in_warmup` to queuing or
        # answering, so a request is either answered in warmup or queued
        # after it, never both. Not in warmup until `Node.run` says so.
        self.in_warmup = False
        self.warmup_status = "RPC server started"
        # What `run` binds and `stop` closes, last. `server` never
        # closes them, cancelled or not; nor may it run at all where
        # `stop` arrives before `run_forever` has stepped its task even
        # once: a coroutine `cancel()` reaches with no frame yet raises
        # `CancelledError` at its own definition point rather than
        # inside the running body (btclib-org/btclib-node#323). Read only
        # by `run` and `stop`, both on this manager's own object and never
        # concurrently -- `run` sets it once, from this thread, before
        # `stop` could possibly be reached by another.
        self._server_sockets: list[socket.socket] = []
        # the coroutine of `server`'s own task, which `run` schedules and
        # `_stop_accepting` cancels ahead of every other
        # (btclib-org/btclib-node#1545)
        self._server_coro: Coroutine[Any, Any, None] | None = None
        # `server`'s own accept queue, kept here rather than only local
        # to `server`'s own frame so the two `manager_test.py` tests
        # naming btclib-org/btclib-node#391 can land a connection into
        # the live queue directly -- the seam a bare `await
        # loop.sock_accept` gave their own predecessors before that fix,
        # and gives `_accept_loop` again below
        # (btclib-org/btclib-node#430): what changed is what fills the
        # queue, a task rather than a reader callback, kept behind this
        # same queue so `server`'s own consumption of it is unaffected.
        # Nothing in this class reads it outside `server` and
        # `_accept_loop`.
        self._accept_queue: (
            asyncio.Queue[tuple[socket.socket, tuple[str, int]]] | None
        ) = None
        # The coroutine of every reply `RpcConnection` has handed to this
        # loop and not yet written, whatever issued it, with the
        # `time.monotonic()` value its write is due to begin by: what
        # `stop` finishes rather than cancels (btclib-org/btclib-node#1539).
        # Locked, because two threads write it: `Node`'s, handing over
        # `handle_rpc`'s replies, and this manager's own, scheduling the
        # refusals `RpcConnection.run` answers itself and ending each
        # reply once written.
        #
        # The lock order is `queue_lock`, then `_replies_lock`:
        # `RpcConnection.run` schedules a reply while holding
        # `queue_lock`, and nothing takes `queue_lock` while holding
        # `_replies_lock`. `_reply_deadline_lock` below is never held
        # together with either.
        self.replies: dict[object, float] = {}
        self._replies_lock = threading.Lock()
        # Set by `reply_ended`, awaited only by `stop`'s own wait, on the
        # thread driving this loop by then
        self._reply_ended = asyncio.Event()
        # Locked, because two threads reach it: `Node`'s writes it, and
        # `Node.stop` reads it from whichever thread asks the node to
        # stop -- an operator's signal, through `install_signal_handlers`.
        self._latest_reply_deadline: float | None = None
        self._reply_deadline_lock = threading.Lock()

    def extend_reply_deadline(self, deadline: float) -> None:
        """Push `latest_reply_deadline` to `deadline`, never back.

        Called on `Node`'s thread alone: by
        `RpcConnection.send_and_close_after` for a `stop` RPC's delayed
        reply (btclib-org/btclib-node#1467), by `Node._drain_message_queues`
        and `Node._drain_rpc_queue` once per request they answer, and by
        `stop` for the replies it finishes. Each is progress exactly as
        the others are, and `Node.stop`'s wait loop reads
        `latest_reply_deadline` without caring which moved it.
        `deadline` is the caller's own `time.monotonic()` reading, taken
        on its own thread rather than this method's, so that a caller
        answering several requests in a row times each push at the
        moment that request actually finished.
        """
        with self._reply_deadline_lock:
            latest = self._latest_reply_deadline
            if latest is None or deadline > latest:
                self._latest_reply_deadline = deadline

    def track_reply(self, reply: Coroutine[Any, Any, None], due: float) -> None:
        """Record `reply`, whose write is due to begin by `due`.

        `due` is a `time.monotonic()` value. Called before `reply` is
        handed to this manager's loop, never from inside it: a task
        recording itself on its own first step is missed by a `stop`
        whose `loop.stop` this loop delivers in the same pass that
        creates that task, and is then cancelled with the rest.
        """
        with self._replies_lock:
            self.replies[reply] = due

    def reply_ended(self, reply: object) -> None:
        """Forget `reply`, written, given up on or failed; once or again.

        Called as the reply's write ends and again as its task does:
        on this manager's loop, or on `Node`'s thread where a task it
        handed over had already ended by the time it asked to be told.
        Only `stop`'s own wait awaits `_reply_ended`, so a set from
        `Node`'s thread while this loop runs on its own wakes nothing.
        `reply` is what the running task's `get_coro` answers, which
        `None` or any coroutine not recorded leaves `replies` as it was.
        """
        with self._replies_lock:
            self.replies.pop(reply, None)
        self._reply_ended.set()

    def latest_reply_deadline(self) -> float | None:
        """Answer the latest deadline `extend_reply_deadline` has recorded.

        `None` where it has recorded none. Never lowered once a reply is
        sent: `Node.stop` has why.
        """
        with self._reply_deadline_lock:
            return self._latest_reply_deadline

    def create_connection(
        self, loop: asyncio.AbstractEventLoop, client: socket.socket
    ) -> RpcConnection:
        """Wrap `client` in a `RpcConnection` and register it under a new id."""
        client.settimeout(0.0)
        new_connection = RpcConnection(
            loop,
            client,
            self,
            self.last_connection_id,
            idle_timeout=self.idle_timeout,
        )
        self.connections[self.last_connection_id] = new_connection
        return new_connection

    def forget_connection(self, connection_id: int) -> None:
        """Drop `connection_id` from `connections`, freeing its place.

        Called on this manager's loop, as its connection closes.
        """
        self.connections.pop(connection_id, None)
        self._room.set()

    async def _until_room(self, held: Callable[[], int]) -> None:
        """Return once `held()` is under `max_connections`."""
        while held() >= self.max_connections:
            self._room.clear()
            await self._room.wait()

    def _bind(self) -> list[socket.socket]:
        """Bind and listen on every endpoint, before anything is scheduled.

        Core's `InitHTTPServer` (`src/httpserver.cpp`, at
        bitcoin/bitcoin@f0de87f040, the v32.1 tag): each endpoint is
        logged before it is tried, and looked up as `Lookup(address,
        port, false)` does, with no DNS lookup, so a name binds nothing.
        One that cannot be looked up or bound is warned about and passed
        over, so a host without IPv6 still listens on `127.0.0.1`. Empty
        where none binds. See `P2pManager._bind` for why this is
        synchronous (#88). Unlike that one, this does not set
        `listening`: `_listen` does, once the cookie is written too.
        """
        bound: list[socket.socket] = []
        for host, port in self._endpoints():
            self.logger.info("Binding RPC on address %s port %s", host, port)
            service = lookup_service(host, port)
            if service is None:
                self.logger.warning(
                    "Could not bind RPC on address %s port %s: Address lookup failed.",
                    host,
                    port,
                )
                continue
            address, service_port = service
            if not isinstance(address, SpecialAddress) and address.is_unspecified:
                self.logger.warning(
                    "The RPC server is not safe to expose to untrusted networks "
                    "such as the public internet"
                )
            try:
                bound.append(_bind_and_listen(address, service_port, self.logger))
            except OSError as error:
                self.logger.warning(
                    "Binding RPC on address %s failed: %s",
                    service_text(address, service_port),
                    error,
                )
        return bound

    def _endpoints(self) -> list[tuple[str, int]]:
        """Return what `GetBindAddresses` binds, logging its warnings.

        Loopback, where `-rpcbind` and `-rpcallowip` are not both given;
        every `-rpcbind` endpoint otherwise, its port defaulting to
        `-rpcport`. Empty, the error logged, for a value naming an
        invalid port, which `cli` has already refused.
        """
        config = self.node.config
        port = self.port or 0
        if not config.rpcallowip or not config.rpcbind:
            if config.rpcallowip:
                self.logger.warning(
                    "Option -rpcallowip was specified without -rpcbind; this "
                    "doesn't usually make sense"
                )
            if config.rpcbind:
                self.logger.warning(
                    "Option -rpcbind was ignored because -rpcallowip was not "
                    "specified, refusing to allow everyone to connect"
                )
            # Config.rpc_host, loopback unless a Python caller names a
            # host -- see its own comment for why the RPC control plane's
            # default is not every interface, unlike P2pManager's
            if config.rpc_host is None:
                return [(host, port) for host in _LOOPBACK_HOSTS]
            return [(config.rpc_host, port)]
        endpoints: list[tuple[str, int]] = []
        for value in config.rpcbind:
            try:
                endpoint: tuple[str, int] | None = split_host_port(value, port)
            except ValueError:
                endpoint = None
            if endpoint is None:
                self.logger.error("Invalid port specified in -rpcbind: '%s'", value)
                return []
            endpoints.append(endpoint)
        return endpoints

    def _listen(self) -> list[socket.socket]:
        """Bind, write the cookie where one is written, then set `listening`.

        `-rpcallowip` is parsed first, as `InitHTTPServer` parses it
        before it binds. Raises `OSError` where any step fails: a value
        naming no subnet, kept in `init_error` for `Node`; Core's "Unable
        to bind any endpoint for RPC server", logged; and
        `InitRPCAuthentication`'s refusal of a cookie that cannot be
        written, logged as the warning `GenerateAuthCookie` logs, the
        sockets closed. Raises `RpcCredentialRefusedError` where
        `auth.start` refuses a value, having closed the sockets, `start`
        having logged it.
        """
        try:
            self.allowed_subnets = allowed_subnets(self.node.config.rpcallowip)
        except ValueError as err:
            self.init_error = str(err)
            raise OSError(self.init_error) from None
        # `InitHTTPAllowList`'s own line, each subnet followed by a space
        self.logger.log_debug(
            "http",
            "Allowing HTTP connections from: %s",
            "".join(f"{subnet} " for subnet in self.allowed_subnets),
        )
        self._server_sockets = self._bind()
        if not self._server_sockets:
            msg = "Unable to bind any endpoint for RPC server"
            self.logger.error(msg)
            raise OSError(msg)
        try:
            self.auth.start(self.logger)
        except OSError as err:
            for server_socket in self._server_sockets:
                server_socket.close()
            self.logger.warning("%s", err)
            raise
        except RpcCredentialRefusedError:
            for server_socket in self._server_sockets:
                server_socket.close()
            raise
        self.listening.set()
        self.ever_listened.set()
        return self._server_sockets

    def start_listener(self) -> bool:
        """Start this thread, and answer whether it came up listening.

        Blocks until `run` has bound and written the cookie, or failed
        to: what `Node.run` turns into Core's `InitError`, where `start`
        alone would leave the node running without its listener.
        """
        self.start()
        self._start_attempted.wait()
        return self.listening.is_set()

    async def _accept_loop(
        self,
        server_socket: socket.socket,
        accepted: asyncio.Queue[tuple[socket.socket, tuple[str, int]]],
    ) -> None:
        """`server`'s own producer: one kernel accept at a time, queued.

        A unit test can land a connection directly in `accepted` --
        `_accept_queue` is kept for exactly that, `server`'s own
        docstring says where -- and cover this loop's `OSError` arm by
        handing it a socket that only duck-types `.accept()`, without a
        live listener.

        It accepts only while the connections held and those queued in
        `accepted` are fewer than `max_connections`; the module docstring
        has why. Each listening socket has a loop of its own, and loops
        that pass that check together can each accept one, so `accepted`
        can go past the limit by one per listening socket at most.
        `server` leaves those there until there is room.

        Where no descriptor is left, each failure is logged at debug, as
        Core logs it, and the accept tried again `_ACCEPT_RETRY_DELAY`
        seconds later, until one succeeds.

        A socket from a source `client_allowed` refuses is closed as it is
        accepted, before anything is read off it, and logged at debug:
        Core's `HTTPServer::AcceptConnection` (`src/httpserver.cpp`, at
        bitcoin/bitcoin@f0de87f040, the v32.1 tag). Otherwise a refused
        source would hold one of `max_connections` places for as long as
        it liked (btclib-org/btclib-node#1963).

        `loop.sock_accept` retries a `BlockingIOError`/`InterruptedError`
        internally on both loop families this node runs on (a selector
        loop's own reader callback, a Windows Proactor loop's overlapped
        `AcceptEx`) and raises anything else -- `ECONNABORTED` being the
        ordinary way, a peer resetting the connection between the kernel
        reporting it readable and the accept reaching it -- which is what
        the `except OSError` below answers.

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
            await self._until_room(lambda: len(self.connections) + accepted.qsize())
            try:
                sock, sockaddr = await asyncio.get_running_loop().sock_accept(
                    server_socket
                )
            except OSError as error:
                if error.errno in _OUT_OF_DESCRIPTORS:
                    self.logger.log_debug(
                        "http",
                        "Cannot accept new connection: %s",
                        network_error_string(error),
                    )
                    await asyncio.sleep(_ACCEPT_RETRY_DELAY)
                    continue
                self.logger.exception("Accepting an inbound connection failed")
                await asyncio.sleep(0)
                continue
            if sockaddr:
                host, port = sockaddr[:2]
            else:
                # no address, as `accept` answers for a peer that reset
                # before it: Core's `SetSockAddr` fails, and the unset
                # `CService` it checks instead is `[::]:0`, which
                # `client_allowed` refuses
                self.logger.log_debug("http", "Unknown socket family")
                host, port = "::", 0
            if not client_allowed(str(host), self.allowed_subnets):
                self.logger.log_debug(
                    "http",
                    "Connection from %s rejected: Client network is not allowed "
                    "HTTP access",
                    ip_and_port(host, port),
                )
                sock.close()
                continue
            # Nagle's algorithm off, as `HTTPServer::NewSockAccepted` sets
            # it on every accepted socket (`src/httpserver.cpp`, at
            # bitcoin/bitcoin@f0de87f040, the v32.1 tag)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                self.logger.log_debug(
                    "http",
                    "connection from %s: unable to set TCP_NODELAY, continuing anyway",
                    ip_and_port(host, port),
                )
            sock.settimeout(0.0)
            accepted.put_nowait((sock, sockaddr))

    async def server(
        self,
        loop: asyncio.AbstractEventLoop,
        server_sockets: Sequence[socket.socket],
    ) -> None:
        """Accept connections off `server_sockets` until cancelled by `stop`.

        Awaits `_accept_queue` for what `_accept_loop` fills, a task of
        its own per listening socket, one `RpcConnection` and one
        `conn.run` task per socket `_accept_loop` queues -- rather than a bare
        `await loop.sock_accept(...)` right here, which does not have the
        property the comment below argues for.

        Cancelled, it stops accepting and leaves `server_sockets` open:
        `stop` closes them last, as Core's `StopHTTPServer` calls
        `StopAccepting` before its wait and `StopListening` after it
        (`src/httpserver.cpp`, at bitcoin/bitcoin@f0de87f040, the v32.1
        tag).
        """
        # The queue keeps a cancel from losing a socket already accepted:
        # an item lands in its deque through `put_nowait`, a plain call,
        # so cancelling this coroutine's wait on `accepted.get()` cannot
        # lose it, and the `finally` closes whatever is left
        # (btclib-org/btclib-node#391).
        #
        # `_accept_loop` awaits `loop.sock_accept`, because Windows'
        # default Proactor loop has no `loop.add_reader`
        # (btclib-org/btclib-node#430). A cancel that lands after the
        # kernel completes an accept and before `_accept_loop`'s next
        # step discards that socket before it reaches the queue:
        # `Task.cancel` cannot cancel a future that is already done.
        # Nothing else holds the socket, so its own `__del__` closes it,
        # a `ResourceWarning` and not a leaked descriptor. Only shutdown
        # cancels `_accept_loop`, so only a client connecting in that
        # instant loses its connection. `P2pManager.server` does the
        # same, for the same reason.
        accepted: asyncio.Queue[tuple[socket.socket, tuple[str, int]]] = asyncio.Queue()
        self._accept_queue = accepted
        accept_tasks = [
            loop.create_task(self._accept_loop(server_socket, accepted))
            for server_socket in server_sockets
        ]
        try:
            while True:
                await self._until_room(lambda: len(self.connections))
                client, _ = await accepted.get()
                self.last_connection_id += 1
                conn = self.create_connection(self.loop, client)
                # The `Future` this returns is not kept: nothing
                # reads it, as `RpcConnection.send` discards
                # `async_send`'s, and `RpcConnection.close`'s docstring
                # says why a connection holds no handle (issue #714).
                asyncio.run_coroutine_threadsafe(conn.run(), self.loop)
        finally:
            # `_stop_accepting` cancels `server` alone, so the accept
            # loops are cancelled and awaited here: none is left
            # accepting once it returns.
            for accept_task in accept_tasks:
                accept_task.cancel()
            for accept_task in accept_tasks:
                with suppress(asyncio.CancelledError):
                    await accept_task
            self._accept_queue = None
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
                self.logger.error("RPC listener's accept loop ended", exc_info=exc)

    @override
    def run(self) -> None:
        loop = self.loop
        try:
            self.logger.info("Starting RPC manager")
            asyncio.set_event_loop(loop)
            server_sockets = self._listen()
        except (OSError, RpcCredentialRefusedError) as error:
            # logged by `_listen`; `start_listener` reads the failure
            # off `listening`, so it is not raised into
            # `threading.excepthook` as well
            self.bind_error = str(error)
            return
        finally:
            self._start_attempted.set()
        self._server_coro = self.server(loop, server_sockets)
        asyncio.run_coroutine_threadsafe(self._server_coro, loop).add_done_callback(
            self._report_server_failure
        )
        loop.run_forever()

    def start_warmup(self) -> None:
        """Core's `SetRPCWarmupStarting`: answer requests `RPC_IN_WARMUP`."""
        with self.queue_lock:
            self.in_warmup = True

    def set_warmup_status(self, status: str) -> None:
        """Core's `SetRPCWarmupStatus`: the message a warmup answer carries."""
        with self.queue_lock:
            self.warmup_status = status

    def finish_warmup(self) -> None:
        """Core's `SetRPCWarmupFinished`: queue requests for `Node` again."""
        with self.queue_lock:
            self.in_warmup = False

    def answer_warmup(self, conn: RpcConnection, body: object) -> HttpReply:
        """Answer `body`, a decoded request, as a node in warmup does.

        Called by `RpcConnection.run` holding `queue_lock`, `in_warmup` set.
        """
        return answer_warmup(self.node, conn, body)

    def interrupt(self) -> None:
        """Stop accepting new RPC work, without waiting for this thread.

        Core's `InterruptHTTPServer` (`src/httpserver.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): swaps in the request
        callback that answers `503` and calls `g_threadpool_http.Interrupt()`,
        both ahead of `StopHTTPServer`'s own `Stop()`, so a request already
        queued keeps draining in the background while a new one is refused
        at once rather than either queued or left for `stop` to close
        unanswered. `Node._drain_rpc_queue` calls this before it drains,
        which is what bounds that drain to what was queued before this
        call (btclib-org/btclib-node#1515).
        """
        with self.queue_lock:
            self.interrupted.set()

    def _stop_accepting(self) -> None:
        """Cancel `server`, which stops this manager accepting.

        Core's `StopHTTPServer` calls `StopAccepting` once its workers
        have stopped, and accepts until then (`src/httpserver.cpp`, at
        bitcoin/bitcoin@f0de87f040, the v32.1 tag). `server`'s `finally`
        cancels and awaits each `_accept_loop`, and leaves the listening
        sockets open for `stop` to close last.
        """
        # Cancelled directly, whether or not `_finish_replies` has run the
        # loop first. What `server` has accepted waits in a queue, and a
        # cancel cannot lose an item already there: `server`'s `finally`
        # closes what is left (btclib-org/btclib-node#391). An accept the
        # kernel has completed but `_accept_loop` has not yet queued can
        # be lost, as `server`'s comment says (btclib-org/btclib-node#430).
        # `run_until_complete` is safe here because `stop` cancelled its
        # own `loop.stop` handle first (btclib-org/btclib-node#377,
        # btclib-org/btclib-node#380).
        listening = {
            task
            for task in asyncio.all_tasks(self.loop)
            if task.get_coro() is self._server_coro
        }
        for task in listening:
            task.cancel()
            with suppress(asyncio.CancelledError):
                self.loop.run_until_complete(task)

    def _finish_replies(self) -> None:
        """Drive this loop until every reply in `replies` has ended, bounded.

        Called by `stop`, on `Node`'s thread, once this manager's own
        thread has ended. The bound is `disconnect_wait` past the latest
        moment a reply is due to begin its write -- a `stop` RPC's
        hidden `wait` (btclib-org/btclib-node#1467), a 401's
        `FAILED_ATTEMPT_DELAY`, or now -- and is recorded through
        `extend_reply_deadline` before the wait, so `Node.stop` waits for
        it too.

        Core's `StopHTTPServer` first stops its workers, however long
        their replies take to begin. Its socket thread meanwhile still
        accepts connections and refuses every request 503. Then it stops
        accepting, disconnects every client it has heard from and
        finished sending to, and waits `_DISCONNECT_WAIT` for the rest.
        This drives the loop until the last reply is due, `server` and
        every read still running, then calls `_stop_accepting`, closes
        each connection `RpcConnection.disconnect_if_idle` finds idle,
        and waits `disconnect_wait`. Meanwhile a write still idles out
        where `idle_timeout` arms that (`RpcConnection`), as in Core, so
        a client that stops reading ends the wait sooner. A reply given
        up on is cancelled by `stop` after this returns, and its
        connection closed.

        A reply scheduled during the wait -- the refusal of a request
        read off a connection still open -- is waited for too, within the
        same bound.

        Core also waits, within its bound, for a client still waiting for
        its first request or partway through one, until it idles out.
        This waits for neither: such a client, which may never send
        another byte, would hold shutdown until it idles out, up to the
        whole `disconnect_wait` where `-rpcservertimeout` arms no timeout
        or a longer one.
        """
        with self._replies_lock:
            due = max(self.replies.values(), default=None)
        if due is None:
            self._stop_accepting()
            return
        finish_by = max(time.monotonic(), due) + self.disconnect_wait
        self.extend_reply_deadline(finish_by)
        self.loop.run_until_complete(self._replies_written(due))
        self._stop_accepting()
        for conn in self.connections.values():
            self.loop.call_soon(conn.disconnect_if_idle)
        self.loop.run_until_complete(self._replies_written(finish_by))

    async def _replies_written(self, finish_by: float) -> None:
        """Return once `replies` is empty, or at the monotonic `finish_by`."""
        delay = finish_by - time.monotonic()
        with suppress(TimeoutError):
            async with asyncio.timeout(delay):
                while True:
                    self._reply_ended.clear()
                    with self._replies_lock:
                        if not self.replies:
                            return
                    await self._reply_ended.wait()

    def stop(self) -> None:
        """Stop this manager's loop, join its thread, and close every socket.

        Once the thread has ended: finishes the replies in `replies`
        through `_finish_replies`, which stops `server` accepting partway
        through, then cancels whatever is left, closes every connection,
        and closes the listening sockets last
        (btclib-org/btclib-node#1539, btclib-org/btclib-node#1545).

        Guarded on `is_alive` for the node that never started this
        thread at all; the long comments below argue why the handle
        this schedules is cancelled unconditionally afterward, and why
        the pending-task sweep runs as its own pass rather than folded
        into one combined loop.

        Sets `interrupted` too, unconditionally: Core's `ThreadPool::Stop`
        sets `m_interrupt = true` itself rather than relying on a prior
        `Interrupt()` call, and a caller here that goes straight to `stop`
        -- every test that never calls `Node`, and `RpcManager.stop`'s own
        callers before btclib-org/btclib-node#1515 -- gets the identical
        guarantee: nothing reaches `messages` once this method has begun.
        """
        with self.queue_lock:
            self.interrupted.set()
        stop_handle = self.loop.call_soon_threadsafe(self.loop.stop)
        # `join` blocks this thread without spinning it, the way
        # `Node.stop` already waits on itself with `self.join`. Guarded
        # on `is_alive`, since `Node.run` calls this unconditionally --
        # a node with `rpc_port` unset never calls `start`, and `join`
        # on a thread that was never started raises. See
        # `P2pManager.stop` for the same fix, applied there first.
        if self.is_alive():
            self.join()
        # `stop_handle.cancel()` is what makes every `run_until_complete`
        # below safe, on any loop this method could possibly be handed --
        # not one more guard clause alongside `self.ident` and `pending`,
        # which is what #362 tried and #380 and #377 each found a gap
        # in. The `call_soon_threadsafe` above only *schedules*
        # `loop.stop`; it is delivered -- `self._stopping` set, so
        # `run_forever` returns after its current pass -- only once
        # something actually drives this loop's `run_forever` far enough
        # to reach it. Three things can happen by the time `join` above
        # returns:
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
        #   way -- so `self.ident is not None` even though `run_forever`,
        #   again, never ran: the handle is undelivered the same as the
        #   case above, which is exactly what defeated `self.ident is
        #   not None` as a guard (btclib-org/btclib-node#380).
        #
        # `Handle.cancel()` on a handle already delivered is specified as
        # a no-op -- there is nothing left to remove from a ready queue
        # already drained of it -- so calling it here unconditionally is
        # correct for the first case above and is what removes the
        # landmine outright for the other two, rather than merely
        # stepping past where it goes off once (#362) and leaving every
        # `run_until_complete` downstream of that first step still primed
        # to hit it (btclib-org/btclib-node#377): a task whose own
        # cancellation needs a second real step to unwind -- an `except
        # CancelledError` handler that awaits a fresh timer rather than
        # only an already-cancelled future -- is not owed anything by a
        # guarded step loop, only by there being no leftover stop left to
        # answer at all. `P2pManager.stop` carries the identical fix, for
        # the identical reason (btclib-org/btclib-node#377,
        # btclib-org/btclib-node#380).
        stop_handle.cancel()
        # `StopHTTPServer`'s wait for its workers and its clients, with
        # `server` cancelled between the two (`_stop_accepting`), as
        # `_finish_replies` says. An idle read never holds it: `replies`
        # holds no read.
        self._finish_replies()
        # Every task left -- a reply `_finish_replies` gave up on, and the
        # read of every connection -- cancelled as its own pass before
        # any is driven to completion, not folded into one combined
        # loop: `run_until_complete(task)` drives the *whole* loop, so
        # under a single combined pass a task not yet reached by the
        # cancellation keeps making progress while an earlier one's is
        # delivered, landing a task that is not in this snapshot and is
        # never itself cancelled, only reported destroyed while still
        # pending (btclib-org/btclib-node#323, where that task was
        # `server`'s own accept loop). The sweep below closes the socket
        # of any connection such a task was reading, `create_connection`
        # putting every connection straight into `self.connections`.
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                self.loop.run_until_complete(task)
        for conn in self.connections.values():
            conn.close()
        # Last, as `StopHTTPServer` calls `StopListening` last
        # (`src/httpserver.cpp`, at bitcoin/bitcoin@f0de87f040, the v32.1
        # tag): `server` leaves these open once cancelled, and
        # `_stop_accepting` may not have reached it at all where `stop`
        # arrives before `run_forever` has stepped it even once
        # (`_server_sockets`'s own comment).
        for server_socket in self._server_sockets:
            server_socket.close()
        self.loop.close()
        # so that the flag says what its name says: a socket
        # closed here is not one anything should wait for
        self.listening.clear()
        # Core's `DeleteAuthCookie`, which logs a failure and goes on
        try:
            self.auth.delete_cookie()
        except OSError:
            self.logger.warning(
                "Unable to remove the RPC authentication cookie", exc_info=True
            )
        self.logger.info("Stopping RPC manager")
