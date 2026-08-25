# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The listening half of the RPC manager, without a node behind it.

What answers a request is `handle_rpc`, which the node's loop calls and
tests/unit/rpc/main_test.py covers. What is left is everything between
the port and that queue -- binding it, accepting a client, and letting
go of both -- and until now only a functional test reached any of it.
"""

import asyncio
import json
import socket
from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast

import pytest

from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.log import Logger
from btclib_node.rpc.manager import RpcManager
from tests.helpers import get_random_port, wait_until, wait_until_listening

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from btclib_node import Node

REQUEST = {"jsonrpc": "2.0", "id": "a", "method": "getbestblockhash"}


class AManagerFactory(Protocol):
    """The type `a_manager` yields: one `RpcManager`, closed at teardown."""

    def __call__(self, port: int | None, rpc_host: str = "127.0.0.1") -> RpcManager:
        """Build an `RpcManager` bound to `port` and `rpc_host` once started."""
        ...


@pytest.fixture
def a_manager() -> Iterator[AManagerFactory]:
    """Build managers, and close their event loops however the test ends."""
    made: list[RpcManager] = []

    def make(port: int | None, rpc_host: str = "127.0.0.1") -> RpcManager:
        manager = RpcManager(
            cast(
                "Node",
                SimpleNamespace(
                    logger=Logger(debug=True),
                    chain=RegTest(),
                    config=Config(chain="regtest", rpc_host=rpc_host),
                ),
            ),
            port,
        )
        made.append(manager)
        return manager

    yield make
    for manager in made:
        # a no-op on the loop a stopped manager has already closed
        manager.loop.close()


def as_http(payload: Mapping[str, object]) -> bytes:
    """Frame `payload` as a JSON-RPC HTTP POST request, headers included."""
    body = json.dumps(payload).encode()
    head = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body)
    return head + body


def test_a_manager_says_when_it_is_listening_and_queues_what_arrives(
    a_manager: AManagerFactory,
) -> None:
    """`listening` is set once bound, and a real client's request is queued.

    `is_alive()` holds before `run` has bound anything, so a client that
    posts on the strength of it is refused; `listening` is what a caller
    can wait on instead (issue #46).
    """
    port = get_random_port()
    manager = a_manager(port)
    # the bind and not the thread: see tests/unit/p2p/manager.py
    assert not manager.listening.is_set()
    manager.start()
    try:
        wait_until_listening(manager)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
            client.sendall(as_http(REQUEST))
            wait_until(lambda: manager.messages)
            data, conn_id = manager.messages.popleft()
        # handed on as it arrived, and addressed to the connection it
        # arrived on, which is how the answer gets back to this client
        assert data == [REQUEST]
        assert conn_id in manager.connections
    finally:
        manager.stop()
        manager.join(timeout=10)
    assert not manager.is_alive()
    assert manager.loop.is_closed()
    # and it stops saying so once the socket is gone
    assert not manager.listening.is_set()


def test_an_answer_is_written_back_to_the_client_that_asked(
    a_manager: AManagerFactory,
) -> None:
    """A connection's own send writes an answer back to the client that asked.

    `RpcConnection.send` is what the node's loop calls once it has an
    answer, from its own thread: the write itself belongs to the
    manager's loop, and this is the line that crosses over.
    """
    port = get_random_port()
    manager = a_manager(port)
    manager.start()
    try:
        wait_until_listening(manager)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
            client.sendall(as_http(REQUEST))
            wait_until(lambda: manager.messages)
            _, conn_id = manager.messages.popleft()
            answer = {"jsonrpc": "2.0", "result": "0" * 64, "id": "a"}
            manager.connections[conn_id].send([answer])
            client.settimeout(20)
            head, _, body = client.recv(4096).partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK\r\n")
        assert json.loads(body) == answer
    finally:
        manager.stop()
        manager.join(timeout=10)


def test_bind_uses_config_rpc_host_not_every_interface(
    a_manager: AManagerFactory,
) -> None:
    """_bind binds Config.rpc_host's default, not every interface (issue #27).

    The socket itself is asked what it actually bound, rather than only
    asking whether some interface can still reach it.
    """
    manager = a_manager(get_random_port())
    server_socket = manager._bind()
    try:
        assert server_socket.getsockname()[0] == "127.0.0.1"
    finally:
        server_socket.close()


def test_bind_honors_a_different_rpc_host(a_manager: AManagerFactory) -> None:
    """_bind binds whatever rpc_host the node's own config carries."""
    all_interfaces = "0.0.0.0"  # noqa: S104
    manager = a_manager(get_random_port(), rpc_host=all_interfaces)
    server_socket = manager._bind()
    try:
        assert server_socket.getsockname()[0] == all_interfaces
    finally:
        server_socket.close()


def test_a_body_that_is_not_json_answers_parse_error_and_forgets_the_client(
    a_manager: AManagerFactory,
) -> None:
    """A non-JSON body over a real socket answers PARSE_ERROR, socket closed.

    JSON-RPC 2.0 section 5.1's own `PARSE_ERROR`, where this used to
    close the socket with no answer at all -- the first thing anything
    scanning the unauthenticated, all-interfaces port would find
    (issue #63, issue #27).
    """
    port = get_random_port()
    manager = a_manager(port)
    manager.start()
    try:
        wait_until_listening(manager)
        body = b"not json"
        head = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
            client.sendall(head + body)
            client.settimeout(20)
            response_head, _, response_body = client.recv(4096).partition(b"\r\n\r\n")
        assert response_head.startswith(b"HTTP/1.1 200 OK\r\n")
        assert json.loads(response_body) == {
            "jsonrpc": "2.0",
            "error": {"code": -32700, "message": "Parse error"},
            "id": None,
        }
        wait_until(lambda: not manager.connections)
    finally:
        manager.stop()
        manager.join(timeout=10)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_manager_that_cannot_bind_stops_being_alive(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#88: see tests/unit/p2p/manager.py's manager of the same name.

    `_bind` runs in `run` before `run_forever`, so a taken port's
    `OSError` comes back out of `run` itself instead of sitting unread
    in the `concurrent.futures.Future` `run_coroutine_threadsafe` used
    to hand back -- and out of `run` on the manager's own thread, which
    nothing there catches, is what this test is asking it to do.
    """
    logged: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        # 127.0.0.1 and not "" (every interface, p2p/manager.py's own
        # test of the same name): the manager under test binds
        # Config.rpc_host's default, and a wildcard bind here would not
        # contend for that specific address the way it did before #27
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        manager = a_manager(taken.getsockname()[1])
        monkeypatch.setattr(manager.logger, "exception", logged.append)
        manager.start()
        wait_until(lambda: not manager.is_alive())
    assert logged
    assert not manager.listening.is_set()


def test_stop_closes_the_listening_socket_even_when_the_accept_task_never_ran(
    a_manager: AManagerFactory,
) -> None:
    """`stop` closes the listening socket even where the accept task never ran.

    `stop()` can cancel `server`'s own task before `run_forever` has
    stepped it even once -- what a manager started and stopped in quick
    succession does. `Task.cancel()` reaching a coroutine with no frame
    yet raises `CancelledError` at its own definition point rather than
    inside the running body, so `with server_socket:` is never entered
    and its own `__exit__` never runs (issue #323).

    The task is cancelled directly, before the loop has run a single
    iteration, which reproduces that deterministically where the real
    race -- `stop()` racing `run_forever`'s own first iteration -- is
    not.
    """
    manager = a_manager(get_random_port())
    loop = manager.loop
    server_socket = manager._bind()
    manager._server_socket = server_socket
    task = loop.create_task(manager.server(loop, server_socket))
    task.cancel()
    with suppress(asyncio.CancelledError):
        loop.run_until_complete(task)
    # the `with` was never entered: nothing has closed this yet
    assert server_socket.fileno() != -1

    manager.stop()
    # a closed socket's own fileno is -1; still >= 0 is still open
    assert server_socket.fileno() == -1


def test_server_does_not_lose_a_connection_queued_in_the_instant_of_its_own_cancellation(
    a_manager: AManagerFactory,
) -> None:
    """`server` does not lose a connection queued the instant it is cancelled.

    Mirrors `P2pManager.server`'s own test of the same shape (issue
    #386). A connection can already sit in `server`'s own accept queue
    when something cancels the task waiting on it -- `Queue.get`'s own
    internal wakeup future can be discarded by `Task.cancel` exactly as
    `loop.sock_accept`'s own future used to be (#323), forcing
    `CancelledError` in on the task's next step rather than letting it
    resume with the result. What that discards is only the wakeup: the
    item itself lives in the queue's own deque and not inside that
    future, so it is still there for `server`'s own `finally` to close
    once the cancellation it raises unwinds through it -- unlike an
    accepted socket held by nothing but a discarded future, which goes
    out with the frame that unwinds and nothing else ever holding it.

    The two `call_soon` callbacks below are that instant, made
    deterministic: they run in the order they were scheduled, so the
    item has certainly landed by the time the cancel reaches the task.
    """
    manager = a_manager(get_random_port())
    loop = manager.loop
    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    accepted, theirs = socket.socketpair()

    task = loop.create_task(manager.server(loop, listening_socket))
    try:
        while manager._accept_queue is None:
            loop.run_until_complete(asyncio.sleep(0))
        queue = manager._accept_queue
        loop.call_soon(queue.put_nowait, (accepted, ("203.0.113.1", 45000)))
        loop.call_soon(task.cancel)
        with suppress(asyncio.CancelledError):
            loop.run_until_complete(task)
    finally:
        theirs.close()
        listening_socket.close()
    # a closed socket's own fileno is -1; still >= 0 is still open
    assert accepted.fileno() == -1


def test_stop_requests_every_tasks_cancellation_before_awaiting_any_one_of_them(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stop` cancels every pending task before awaiting any one of them.

    Mirrors `P2pManager.stop`'s own test of the same name (issue #312,
    issue #323). `run_until_complete(task)`, for any one task, drives the
    *whole* loop, not only that task -- so under a single combined pass
    over `asyncio.all_tasks()`, a task whose own turn has not yet come up
    keeps making ordinary forward progress (`server`'s own accept loop)
    while an earlier task's cancellation is being delivered. A connection
    it lands that way is added to `self.connections` strictly after the
    `asyncio.all_tasks()` snapshot has already run, so cancellation never
    reaches its own `RpcConnection.run` task, which is reported destroyed
    while still pending.

    Two dummy tasks, standing in for whatever `pending` holds in a real
    run, each record whether `cancel()` had already been called on the
    *other* one by the time either is first driven via
    `run_until_complete`.
    """
    manager = a_manager(get_random_port())
    loop = manager.loop
    asyncio.set_event_loop(loop)

    async def a_task() -> None:
        await asyncio.Event().wait()

    async def b_task() -> None:
        await asyncio.Event().wait()

    task_a = loop.create_task(a_task())
    task_b = loop.create_task(b_task())
    loop.run_until_complete(asyncio.sleep(0))

    order: list[str] = []
    real_cancel_a, real_cancel_b = task_a.cancel, task_b.cancel

    def cancel_a(*args: Any, **kwargs: Any) -> bool:
        order.append("cancel_a")
        return real_cancel_a(*args, **kwargs)

    def cancel_b(*args: Any, **kwargs: Any) -> bool:
        order.append("cancel_b")
        return real_cancel_b(*args, **kwargs)

    order_at_first_await: list[list[str]] = []
    real_run_until_complete = loop.run_until_complete

    def recording_run_until_complete(fut: Any) -> Any:
        order_at_first_await.append(list(order))
        return real_run_until_complete(fut)

    monkeypatch.setattr(task_a, "cancel", cancel_a)
    monkeypatch.setattr(task_b, "cancel", cancel_b)
    monkeypatch.setattr(asyncio, "all_tasks", lambda loop=None: {task_a, task_b})
    monkeypatch.setattr(loop, "run_until_complete", recording_run_until_complete)

    manager.stop()

    assert order_at_first_await
    assert order_at_first_await[0] == ["cancel_a", "cancel_b"] or order_at_first_await[
        0
    ] == ["cancel_b", "cancel_a"]


def test_stop_closes_a_connection_queued_when_the_drain_begins(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stop` closes a connection queued right as its own drain begins.

    Mirrors `P2pManager.stop`'s own test of the same shape (issue #386,
    issue #391). `server`'s own task is what `stop`'s blanket sweep over
    `asyncio.all_tasks` reaches directly, `accept` no longer being a
    task of its own for it to reach instead -- not only through
    `server`'s own task cascading a cancel onto it, which the
    neighbouring test above turns on instead.

    `Task.cancel` on a task whose own awaited future is already done
    cannot cancel that future either: it forces `CancelledError` into
    the task's next step regardless -- but the item this test lands is
    in the queue's own deque, not inside the future `Queue.get` awaits
    to be woken, so the discard costs it nothing, unlike
    `loop.sock_accept`'s own future before this fix. `server`'s own
    `finally` is what closes whatever the discard still leaves behind,
    reaching it before the unconditional `self.connections` sweep at
    the end of `stop()` ever could -- that sweep is what
    `RpcManager.stop`'s own standing comment still relies on for a
    connection landed later still, during the cancel-and-drain below
    rather than before it, which is why this manager needs no
    `P2pManager`-style repeated pass.

    Landed into the live queue directly, via `manager._accept_queue`,
    scheduled through a monkeypatched `is_alive()` -- the window `stop`
    itself calls it in, between scheduling `loop.stop` and waiting for
    the thread. `call_soon_threadsafe` queues behind that scheduling
    rather than ahead of it, so the manager's own loop sees `loop.stop`
    first and stops before ever stepping `server`'s own wakeup: the
    item is queued and the task that owns it is not, which is the same
    gap a landed kernel accept leaves for real.
    """
    manager = a_manager(get_random_port())
    manager.start()
    wait_until_listening(manager)
    wait_until(lambda: manager._accept_queue is not None)
    assert manager._accept_queue is not None
    queue = manager._accept_queue

    ours, theirs = socket.socketpair()
    real_is_alive = manager.is_alive
    landed: list[bool] = []

    def is_alive_after_queueing_one() -> bool:
        landed.append(True)
        manager.loop.call_soon_threadsafe(
            queue.put_nowait, (ours, ("127.0.0.1", 45000))
        )
        return real_is_alive()

    monkeypatch.setattr(manager, "is_alive", is_alive_after_queueing_one)
    try:
        manager.stop()
    finally:
        theirs.close()
    # exactly once, asserted rather than guarded against: `stop()` asks
    # this one question, and `monkeypatch` has put the real one back
    # before the fixture asks its own
    assert landed == [True]
    # a closed socket's own fileno is -1; still >= 0 is still open
    assert ours.fileno() == -1


def test_stop_drains_a_task_whose_own_cancellation_needs_a_second_step(
    a_manager: AManagerFactory,
) -> None:
    """`stop` drains a task whose cancellation needs a second step (issue #377).

    The unconditional drain below (`for task in pending: ...
    run_until_complete(task)`) is not, on its own, guarded against a
    task whose cancellation-unwind needs more than the one batch of
    already-ready callbacks the loop's very first `_run_once` since
    `stop()` scheduled its own `loop.stop` -- an `except CancelledError`
    handler that awaits a fresh, real timer rather than only re-awaiting
    an already-cancelled future. `stop_handle.cancel()` above is what
    answers it instead: cancelling that scheduled `loop.stop` outright,
    rather than guarding how many steps are taken before it, is what
    keeps it from firing mid-unwind regardless of how many steps this
    task's own cancellation needs.

    Neither #323's own regression test (`asyncio.Event().wait()`, whose
    cancellation resolves inside that same first batch) nor #368's
    (mirrored above in this file) builds a task shaped this way: this
    one does, on a manager whose thread was never started, and used to
    raise the identical `RuntimeError('Event loop stopped before Future
    completed.')` out of this same drain loop.
    """
    manager = a_manager(get_random_port())
    loop = manager.loop

    async def slow_unwind() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise

    task = loop.create_task(slow_unwind())
    loop.run_until_complete(asyncio.sleep(0))
    assert not task.done()

    manager.stop()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_stop_does_not_raise_where_start_was_called_but_run_never_reached_run_forever(
    a_manager: AManagerFactory,
) -> None:
    """`stop` does not raise where start ran but run never reached run_forever.

    `self.ident is not None` -- issue #362's own guard on a grace step
    this method no longer has -- is true from the moment `start()`
    is called, well before `run()` reaches `run_forever()`. Where
    `run()` raises before that -- a bind failure being the ordinary way
    -- the `loop.stop` `stop()` schedules at its own top is never
    delivered, and `self.ident is not None` read `True` anyway: the
    grace step that guard used to gate ran against a loop with nothing
    having ever stepped it, raising the identical `RuntimeError('Event
    loop stopped before Future completed.')` #362 exists to eliminate,
    through the very guard meant to rule it out. `stop_handle.cancel()`
    is what removes that failure outright now, on this precondition as
    on every other this method can be handed, so nothing downstream of
    it needs a guard of its own to answer this scenario any more.

    A real bind failure, not a monkeypatched `_bind`, the same way
    `test_a_manager_that_cannot_bind_stops_being_alive` above gets one --
    with a real task created directly on `manager.loop` before `start()`,
    the same caller shape #368's own P2pManager test and the one above
    build.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        manager = a_manager(taken.getsockname()[1])
        loop = manager.loop

        async def a_task() -> None:
            await asyncio.Event().wait()

        task = loop.create_task(a_task())
        loop.run_until_complete(asyncio.sleep(0))
        assert not task.done()

        manager.start()
        wait_until(lambda: not manager.is_alive())
        assert manager.ident is not None

        manager.stop()


def test_accept_one_leaves_the_queue_alone_where_nothing_is_pending(
    a_manager: AManagerFactory,
) -> None:
    """_accept_one's BlockingIOError arm returns without touching the queue.

    A reader callback can fire on a listening socket with an empty
    backlog, and this is what lets it return without touching the queue
    at all rather than raising out of a callback nothing awaits.
    """
    manager = a_manager(get_random_port())
    accepted: asyncio.Queue[Any] = asyncio.Queue()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listening_socket:
        listening_socket.bind(("127.0.0.1", 0))
        listening_socket.listen()
        listening_socket.settimeout(0.0)
        manager._accept_one(listening_socket, accepted)
    assert accepted.empty()


def test_accept_one_logs_and_returns_on_a_refused_accept(
    a_manager: AManagerFactory,
) -> None:
    """_accept_one's OSError arm logs and returns, leaving the queue untouched.

    `accept()` can fail outright -- `ECONNABORTED` being the ordinary
    way, a peer resetting the connection between the kernel reporting it
    readable and this callback reaching it -- and this is what keeps
    that from raising out of a reader callback asyncio has no coroutine
    frame to deliver it to.
    """
    manager = a_manager(get_random_port())
    accepted: asyncio.Queue[Any] = asyncio.Queue()

    class RefusingSocket:
        def accept(self) -> NoReturn:
            raise OSError("accept refused")  # noqa: TRY003

    manager._accept_one(cast("socket.socket", RefusingSocket()), accepted)
    assert accepted.empty()
