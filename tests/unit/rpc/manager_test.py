# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The listening half of the RPC manager, without a node behind it.

What answers a request is `handle_rpc`, which the node's loop calls and
tests/unit/rpc/main.py covers. What is left is everything between the
port and that queue -- binding it, accepting a client, and letting go
of both -- and until now only a functional test reached any of it.
"""

import asyncio
import json
import socket
from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

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
    def __call__(self, port: int | None, rpc_host: str = "127.0.0.1") -> RpcManager: ...


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
    body = json.dumps(payload).encode()
    head = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body)
    return head + body


def test_a_manager_says_when_it_is_listening_and_queues_what_arrives(
    a_manager: AManagerFactory,
) -> None:
    # #46: `is_alive()` holds before `run` has bound anything, so a
    # client that posts on the strength of it is refused. The event is
    # what a caller can wait on instead.
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
    # `Connection.send` is what the node's loop calls once it has an
    # answer, from its own thread: the write itself belongs to the
    # manager's loop, and this is the line that crosses over
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
    # #27: no `("0.0.0.0", self.port)` left unconditional -- `_bind`
    # reads `Config.rpc_host` off the node it was given, so the socket
    # itself is asked what it actually bound rather than only asking
    # whether some interface can still reach it
    manager = a_manager(get_random_port())
    server_socket = manager._bind()
    try:
        assert server_socket.getsockname()[0] == "127.0.0.1"
    finally:
        server_socket.close()


def test_bind_honors_a_different_rpc_host(a_manager: AManagerFactory) -> None:
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
    # #63: JSON-RPC 2.0 section 5.1's PARSE_ERROR, where this used to
    # close the socket with no answer at all -- the first thing
    # anything scanning the unauthenticated, all-interfaces port (#27)
    # would find
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
    """#323: `stop()` can cancel `server`'s own task before `run_forever`
    has stepped it even once -- what a manager started and stopped in
    quick succession does. `Task.cancel()` reaching a coroutine with no
    frame yet raises `CancelledError` at its own definition point rather
    than inside the running body, so `with server_socket:` is never
    entered and its own `__exit__` never runs.

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


def test_server_does_not_lose_a_connection_accepted_in_the_instant_of_its_own_cancellation(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#323: mirrors `P2pManager.server`'s own test of the same name
    (#312). `server`'s own `loop.sock_accept(server_socket)` can have its
    underlying future already carrying a result -- the OS having handed
    asyncio a connection -- at the very instant `stop`'s
    `asyncio.all_tasks()` sweep cancels this task. `Task.cancel()` cannot
    cancel an already-resolved future, so it throws `CancelledError` into
    the coroutine on its *next* step instead of resuming it with that
    result, and that result -- an already connected client socket -- is
    referenced by nothing else once this frame unwinds. `asyncio.shield`
    is what keeps `server`'s own accept -- a task in its own right, not
    only a future -- reachable from the `except` below rather than lost
    with this coroutine's frame.

    `loop.sock_accept` is replaced with a stand-in whose future is
    resolved, and this task cancelled, by two `call_soon` callbacks
    scheduled in that order -- deterministic where the real race is not,
    since a callback the first schedules is appended after the second
    and so cannot run before it.
    """
    manager = a_manager(get_random_port())
    loop = manager.loop
    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    accepted, theirs = socket.socketpair()
    # a type alias, not a variable: PEP 8's own naming convention for
    # one is CapWords, the same as a class, not the lowercase this rule
    # otherwise asks a function-scoped name for
    AcceptResult = tuple[socket.socket, tuple[str, int]]  # noqa: N806
    fut_holder: dict[str, asyncio.Future[AcceptResult]] = {}

    async def fake_sock_accept(sock: socket.socket) -> AcceptResult:
        fut: asyncio.Future[AcceptResult] = loop.create_future()
        fut_holder["fut"] = fut
        return await fut

    monkeypatch.setattr(loop, "sock_accept", fake_sock_accept)
    task = loop.create_task(manager.server(loop, listening_socket))
    try:
        while "fut" not in fut_holder:
            loop.run_until_complete(asyncio.sleep(0))

        loop.call_soon(fut_holder["fut"].set_result, (accepted, ("203.0.113.1", 45000)))
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
    """#323: mirrors `P2pManager.stop`'s own test of the same name
    (#312). `run_until_complete(task)`, for any one task, drives the
    *whole* loop, not only that task -- so under a single combined pass
    over `asyncio.all_tasks()`, a task whose own turn has not yet come up
    keeps making ordinary forward progress (`server`'s own accept loop)
    while an earlier task's cancellation is being delivered. A connection
    it lands that way is added to `self.connections` strictly after the
    `asyncio.all_tasks()` snapshot has already run, so cancellation never
    reaches its own `Connection.run` task, which is reported destroyed
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


def test_stop_closes_an_accept_already_landed_when_the_drain_begins(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#362: mirrors `P2pManager.stop`'s own test of the same name
    (#353). `server`'s own `accept` is a task of its own, and
    `asyncio.all_tasks()` below reaches it directly on every call --
    not only through `server`'s own task cascading a cancel onto it,
    which the neighbouring test above turns on instead. That test's
    own cancel never reaches `accept` directly, only `server`, so it
    does not cover this: `stop` cancels `accept` itself, whether or not
    `server` is ever cancelled at all.

    `Task.cancel` on a task whose own awaited future is already done
    cannot cancel that future either: it forces `CancelledError` into
    the task's next step regardless, discarding whatever the kernel
    already handed over with nothing left holding it.

    A grace step before the sweep -- `run_until_complete(asyncio.sleep(0))`
    -- is what lets a task sitting on an already-resolved future return
    normally instead, into `create_connection`, whose own socket the
    unconditional `self.connections` sweep at the end of `stop()` then
    closes -- the same sweep `RpcManager.stop`'s own standing comment
    already relies on for a connection landed later still, during the
    cancel-and-drain below rather than before it, which is why this
    manager needs no `P2pManager`-style repeated pass.

    `sock_accept` is replaced so its own future is one this test can
    resolve from outside it, scheduled through a monkeypatched
    `is_alive()` -- the window `stop` itself calls it in, between
    scheduling `loop.stop` and waiting for the thread.
    `call_soon_threadsafe` queues behind that scheduling rather than
    ahead of it, so the manager's own loop sees `loop.stop` first and
    stops before ever stepping `accept`'s own wakeup: the future is
    resolved and the task that owns it is not, which is the same gap a
    landed kernel accept leaves for real.
    """
    manager = a_manager(get_random_port())

    accepts: list[asyncio.Future[tuple[socket.socket, Any]]] = []

    async def sock_accept(sock: socket.socket) -> tuple[socket.socket, Any]:
        accepts.append(manager.loop.create_future())
        return await accepts[-1]

    monkeypatch.setattr(manager.loop, "sock_accept", sock_accept)
    manager.start()
    wait_until_listening(manager)
    wait_until(lambda: accepts)

    ours, theirs = socket.socketpair()
    real_is_alive = manager.is_alive
    landed: list[bool] = []

    def is_alive_after_landing_the_accept() -> bool:
        landed.append(True)
        manager.loop.call_soon_threadsafe(
            accepts[0].set_result, (ours, ("127.0.0.1", 45000))
        )
        return real_is_alive()

    monkeypatch.setattr(manager, "is_alive", is_alive_after_landing_the_accept)
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
