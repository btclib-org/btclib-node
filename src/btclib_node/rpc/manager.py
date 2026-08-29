# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`RpcManager`, the thread listening for JSON-RPC connections.

Runs its own asyncio loop, accepting a `RpcConnection` per accepted
socket -- one request or several, `connection.RpcConnection`'s own
docstring has the keep-alive that decides which -- and queuing what
each one parses onto `messages` for `Node`'s own thread to read in
`rpc.main.handle_rpc`. `listening` is set once `run` has actually bound
the socket, which is what a caller waits on rather than `is_alive()`
alone -- that flag is true before anything is bound.
"""

import asyncio
import socket
import threading
from collections import deque
from concurrent.futures import CancelledError
from contextlib import suppress
from typing import TYPE_CHECKING, Any, override

from btclib_node.rpc.connection import REQUEST_TIMEOUT, RpcConnection

if TYPE_CHECKING:
    from concurrent.futures import Future

    from btclib_node import Node

__all__ = ["RpcManager"]


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
        # what a connection parses out of one request: the JSON-RPC
        # batch -- a list even where the client sent a lone object --
        # and the connection id handle_rpc answers on
        self.messages: deque[tuple[list[Any], int]] = deque()
        self.loop = asyncio.new_event_loop()
        self.port = port
        self.last_connection_id = -1
        # handed to every RpcConnection create_connection below builds;
        # an instance attribute rather than a call-site default so a
        # test can lower it on a live manager, before opening the
        # connection it means to time out, without waiting through
        # REQUEST_TIMEOUT's own real, Core-matching value.
        self.request_timeout = REQUEST_TIMEOUT

        # see P2pManager.listening: `is_alive()` is true before `run`
        # has bound anything, and a client that posts on the strength of
        # it is refused
        self.listening = threading.Event()
        # What `run` binds and `stop` closes. `server`'s own
        # `with server_socket:` ordinarily closes this once `stop`'s
        # cancellation reaches that task -- except where `stop` arrives
        # before `run_forever` has stepped that task even once: a
        # coroutine `cancel()` reaches with no frame yet raises
        # `CancelledError` at its own definition point rather than
        # inside the running body, so `with server_socket:` is never
        # entered at all (btclib-org/btclib-node#323). Read only by
        # `run` and `stop`, both on this manager's own object and never
        # concurrently -- `run` sets it once, from this thread, before
        # `stop` could possibly be reached by another.
        self._server_socket: socket.socket | None = None
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
            request_timeout=self.request_timeout,
        )
        self.connections[self.last_connection_id] = new_connection
        return new_connection

    def _bind(self) -> socket.socket:
        """Bind and listen, synchronously, before anything is scheduled.

        See `P2pManager._bind`: the same shape of bug (#88) and the same
        fix, applied to the RPC listener instead of the P2P one.
        """
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Config.rpc_host, "127.0.0.1" unless a caller asks
            # otherwise -- see its own docstring for why the RPC
            # control plane's default is not every interface, unlike
            # P2pManager's
            server_socket.bind((self.node.config.rpc_host, self.port))
            server_socket.listen()
            server_socket.settimeout(0.0)
        except OSError:
            server_socket.close()
            raise
        self.listening.set()
        return server_socket

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

    async def server(
        self, loop: asyncio.AbstractEventLoop, server_socket: socket.socket
    ) -> None:
        """Accept connections off `server_socket` until cancelled by `stop`.

        Awaits `_accept_queue` for what `_accept_loop`, a task of its
        own, fills, one `RpcConnection` and one `conn.run` task per
        socket -- rather than a bare `await loop.sock_accept(server_socket)`
        right here, which does not have the property the comment below
        argues for.
        """
        with server_socket:
            # The queue is what keeps a shutdown from discarding an
            # already-accepted socket reaching `server`'s own consumption
            # below: an item lands in its deque through `put_nowait`, a
            # plain call rather than an await, so nothing this
            # coroutine's own suspension on `accepted.get()` is cancelled
            # out of can ever lose an item already there -- the `finally`
            # closes whatever is left in it on whichever pass reaches
            # this task. That half of btclib-org/btclib-node#391 still
            # holds exactly as it did.
            #
            # What no longer holds is the other half, for `_accept_loop`
            # itself: `loop.add_reader`, which #391 chose over
            # `loop.sock_accept` because the reader callback it registers
            # is never itself a `Task.cancel` target, is not implemented
            # by Windows' own default Proactor loop
            # (btclib-org/btclib-node#430) -- so accepting there has to
            # go back through a task awaiting `loop.sock_accept`, and
            # that task is reachable by `stop`'s own blanket sweep over
            # `asyncio.all_tasks` exactly as #323's shielded task was:
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
            # gives every item that did reach the queue. A client that
            # dials in that exact instant of shutdown loses the
            # connection it just opened; nothing during ordinary
            # operation reaches this window at all, `_accept_loop` never
            # otherwise stopping. `P2pManager.server` carries the
            # identical fix, for the identical reason.
            accepted: asyncio.Queue[tuple[socket.socket, tuple[str, int]]] = (
                asyncio.Queue()
            )
            self._accept_queue = accepted
            accept_task = loop.create_task(self._accept_loop(server_socket, accepted))
            try:
                while True:
                    client, _ = await accepted.get()
                    self.last_connection_id += 1
                    conn = self.create_connection(self.loop, client)
                    task: Future[None] = asyncio.run_coroutine_threadsafe(
                        conn.run(), self.loop
                    )
                    conn.task = task
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
        self.logger.info("Starting RPC manager")
        loop = self.loop
        asyncio.set_event_loop(loop)
        try:
            server_socket = self._bind()
        except OSError:
            self.logger.exception("Could not bind the RPC listener")
            raise
        self._server_socket = server_socket
        asyncio.run_coroutine_threadsafe(
            self.server(loop, server_socket), loop
        ).add_done_callback(self._report_server_failure)
        loop.run_forever()

    def stop(self) -> None:
        """Stop this manager's loop, join its thread, and close every socket.

        Guarded on `is_alive` for the node that never started this
        thread at all; the long comments below argue why the handle
        this schedules is cancelled unconditionally afterward, why the
        pending-task sweep runs as its own pass rather than folded into
        one combined loop, and why closing `_server_socket` here does
        not race `server`'s own `with server_socket:`.
        """
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
        # - This thread was started and `run()` raised before ever
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
        # Cancelled here, as its own pass over `pending`, before any of
        # them is driven to completion below -- not folded into one
        # combined loop. `run_until_complete(task)`, for any one task,
        # drives the *whole* loop, not only that task: under a single
        # combined pass, `server`'s own accept loop -- not yet reached
        # by this loop's own cancellation -- keeps accepting while an
        # earlier task's cancellation is delivered, landing a task that
        # is not in this snapshot and is never itself cancelled, only
        # reported destroyed while still pending
        # (btclib-org/btclib-node#323). This closes log noise rather
        # than a leak: the sweep below already reaches such a
        # connection's own socket, since it runs after this loop and
        # `create_connection` puts every connection straight into
        # `self.connections` rather than a dict of its own -- unlike
        # `P2pManager`, whose own connections sweep runs *before* this
        # same loop and so cannot.
        pending = asyncio.all_tasks(self.loop)
        # No step of the loop first here, unlike an earlier version of
        # this method: that step existed only to let a task sitting on
        # an already-resolved future -- `server`'s own former `accept`
        # task -- return normally into `create_connection` before a
        # direct cancel discarded it, `Task.cancel` on a task whose own
        # awaited future is already done forcing `CancelledError` in on
        # its next step regardless of what the future already held
        # (btclib-org/btclib-node#323, for a cancel arriving through
        # `server`'s own shield; this loop's own former blanket sweep,
        # for one reaching that task directly). `server` no longer has
        # such a task to protect: what it accepts sits in a queue
        # instead, and `server`'s own consumption of that queue is
        # immune to that discard regardless of when the cancel below
        # reaches it (btclib-org/btclib-node#391). `_accept_loop`'s own
        # production side of the same queue is not -- `server`'s own
        # docstring has the reason and the bound on what it costs
        # (btclib-org/btclib-node#430). `stop_handle.cancel()`
        # above already closed the other reason an earlier version of
        # this step existed, a `RuntimeError` this loop could raise
        # running `pending`'s own already-scheduled tasks on a loop
        # whose `run_forever` never delivered this method's own
        # `loop.stop` (btclib-org/btclib-node#377,
        # btclib-org/btclib-node#380) -- so neither of the two reasons
        # this step used to answer still applies.
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                self.loop.run_until_complete(task)
        for conn in self.connections.values():
            conn.close()
        # Closed explicitly and unconditionally, after the loop above
        # rather than instead of it: `server`'s own `with server_socket:`
        # is what ordinarily closes this, once that task's own
        # cancellation is delivered and the exception propagates through
        # the `with`. Measured to be skipped entirely where `stop()`
        # arrives before `run_forever` has stepped that task even once
        # -- `_server_socket`'s own comment has the mechanism, and this
        # is what closes what that path does not reach; a `with` block
        # already run leaves nothing here for `close()` to do, since a
        # socket is closed only once, whichever call reaches it first.
        if self._server_socket is not None:
            self._server_socket.close()
        self.loop.close()
        # so that the flag says what its name says: a socket
        # closed here is not one anything should wait for
        self.listening.clear()
        self.logger.info("Stopping RPC manager")
