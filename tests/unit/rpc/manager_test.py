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
import base64
import json
import os
import socket
import threading
from concurrent.futures import Future
from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

import pytest

from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.log import Logger
from btclib_node.rpc import manager as manager_module
from btclib_node.rpc.jsonrpc import OK, HttpReply
from btclib_node.rpc.manager import RpcManager
from tests import (
    RPCAUTH,
    RPCAUTH_LINE,
    cookie_path,
    get_random_port,
    taken_loopbacks,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from btclib_node import Node

REQUEST = {"jsonrpc": "2.0", "id": "a", "method": "getbestblockhash"}


class AManagerFactory(Protocol):
    """The type `a_manager` yields: one `RpcManager`, closed at teardown."""

    def __call__(
        self,
        port: int | None,
        rpc_host: str | None = None,
        rpcbind: tuple[str, ...] = (),
    ) -> RpcManager:
        """Build an `RpcManager` bound to `port` and `rpc_host` once started."""
        ...


@pytest.fixture
def a_manager(tmp_path: Path) -> Iterator[AManagerFactory]:
    """Build managers, and close their event loops however the test ends.

    Each accepts `RPCAUTH`'s user and writes its cookie under
    `tmp_path`, in the chain directory `Node.__init__` would create.
    """
    made: list[RpcManager] = []

    def make(
        port: int | None,
        rpc_host: str | None = None,
        rpcbind: tuple[str, ...] = (),
    ) -> RpcManager:
        config = Config(
            chain="regtest",
            data_dir=tmp_path,
            rpc_host=rpc_host,
            rpcbind=rpcbind,
            rpcauth=[RPCAUTH],
        )
        config.data_dir.mkdir(exist_ok=True)
        manager = RpcManager(
            cast(
                "Node",
                SimpleNamespace(
                    logger=Logger(debug=True), chain=RegTest(), config=config
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
    """Frame `payload` as a JSON-RPC HTTP POST request, `RPCAUTH`'s user's."""
    body = json.dumps(payload).encode()
    head = b"POST / HTTP/1.1\r\nHost: x\r\n" + RPCAUTH_LINE
    return head + b"Content-Length: %d\r\n\r\n" % len(body) + body


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
        assert data == REQUEST
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
            manager.connections[conn_id].send(HttpReply(OK, answer))
            client.settimeout(20)
            head, _, body = client.recv(4096).partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK\r\n")
        assert json.loads(body) == answer
    finally:
        manager.stop()
        manager.join(timeout=10)


def bound_hosts(server_sockets: list[socket.socket]) -> list[str]:
    """Close `server_sockets`, answering the host each of them bound."""
    hosts = [server_socket.getsockname()[0] for server_socket in server_sockets]
    for server_socket in server_sockets:
        server_socket.close()
    return hosts


def resolvable(host: str) -> bool:
    """Whether the lookup `_bind` makes answers for `host` on this machine.

    `AI_ADDRCONFIG`, which libevent passes off Windows, can refuse `::1`
    on a host with no IPv6 address, so what `bitcoind` binds depends on
    the machine too.
    """
    # numeric alone, which changes nothing for a literal and keeps a
    # name from being looked up
    flags = manager_module._ADDRESS_FLAGS | socket.AI_NUMERICHOST
    try:
        socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM, flags=flags)
    except OSError:
        return False
    return True


_LOOPBACKS = [host for host in ("::1", "127.0.0.1") if resolvable(host)]


def test_resolvable_refuses_what_the_lookup_does_not_answer() -> None:
    """The control for `_LOOPBACKS`: a name is not an address it answers."""
    assert not resolvable("localhost")


def test_bind_uses_both_loopbacks_not_every_interface(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1269: `::1` and `127.0.0.1`, as `HTTPBindAddresses` binds (#27).

    The sockets themselves are asked what they bound, rather than only
    asking whether some interface can still reach them.
    """
    manager = a_manager(get_random_port())
    assert bound_hosts(manager._bind()) == _LOOPBACKS


@pytest.mark.parametrize("taken", ["::1", "127.0.0.1"])
def test_a_loopback_that_cannot_be_bound_is_warned_over_and_passed(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch, taken: str
) -> None:
    """ISS 1269: `bitcoind` v31.1.0 binds the other loopback and starts.

    Measured with the one address held by another process: it logs
    "Binding RPC on address <host> port <port> failed." and listens on
    the other.
    """
    family = socket.AF_INET6 if ":" in taken else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as holder:
        holder.bind((taken, 0))
        holder.listen()
        port = holder.getsockname()[1]
        manager = a_manager(port)
        warnings: list[tuple[object, ...]] = []
        monkeypatch.setattr(
            manager.logger, "warning", lambda *args: warnings.append(args)
        )
        hosts = bound_hosts(manager._bind())
    assert hosts == [host for host in _LOOPBACKS if host != taken]
    assert warnings == [("Binding RPC on address %s port %s failed.", taken, port)]


@pytest.mark.parametrize("host", _LOOPBACKS)
def test_a_request_is_answered_on_either_loopback(
    a_manager: AManagerFactory, host: str
) -> None:
    """ISS 1269: `server` accepts from every socket `_bind` answers."""
    port = get_random_port()
    manager = a_manager(port)
    manager.start()
    try:
        wait_until_listening(manager)
        with socket.create_connection((host, port), timeout=20) as client:
            client.sendall(as_http(REQUEST))
            wait_until(lambda: manager.messages)
        assert manager.messages.popleft()[0] == REQUEST
    finally:
        manager.stop()
        manager.join(timeout=10)


def test_a_listening_socket_carries_libevent_s_options(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1269: `SO_KEEPALIVE`, and `SO_REUSEADDR` off Windows alone."""
    manager = a_manager(get_random_port())
    server_sockets = manager._bind()
    try:
        for server_socket in server_sockets:
            options = socket.SOL_SOCKET
            assert server_socket.getsockopt(options, socket.SO_KEEPALIVE)
            reuse = server_socket.getsockopt(options, socket.SO_REUSEADDR)
            assert bool(reuse) is (os.name != "nt")
    finally:
        bound_hosts(server_sockets)


def test_bind_honors_a_different_rpc_host(a_manager: AManagerFactory) -> None:
    """_bind binds whatever rpc_host the node's own config carries, alone."""
    all_interfaces = "0.0.0.0"  # noqa: S104
    manager = a_manager(get_random_port(), rpc_host=all_interfaces)
    assert bound_hosts(manager._bind()) == [all_interfaces]


_IGNORED = (
    "Option -rpcbind was ignored because -rpcallowip was not specified, "
    "refusing to allow everyone to connect"
)
_EXPOSED = (
    "The RPC server is not safe to expose to untrusted networks such as the "
    "public internet"
)
# the any address, named here to be refused or warned over, not bound
_EVERY_INTERFACE = "0.0.0.0"  # noqa: S104


@pytest.mark.parametrize(
    ("rpc_host", "rpcbind", "warned"),
    [
        pytest.param("127.0.0.1", (), [], id="loopback"),
        pytest.param(None, (_EVERY_INTERFACE,), [_IGNORED], id="ignored"),
        pytest.param(_EVERY_INTERFACE, (), [_EXPOSED], id="every interface"),
        pytest.param("localhost", (), [], id="a name, not looked up"),
    ],
)
def test_bind_warns_as_cores_http_bind_addresses(
    a_manager: AManagerFactory,
    monkeypatch: pytest.MonkeyPatch,
    rpc_host: str | None,
    rpcbind: tuple[str, ...],
    warned: list[str],
) -> None:
    """ISS 1211: `HTTPBindAddresses`'s warnings, and its "Binding RPC" line.

    Measured on bitcoind v31.1.0: `-rpcbind=0.0.0.0` without
    `-rpcallowip` logs the first warning and binds loopback. The second
    is logged for an address that binds every interface, once bound.
    """
    manager = a_manager(get_random_port(), rpc_host=rpc_host, rpcbind=rpcbind)
    warnings: list[str] = []
    infos: list[tuple[object, ...]] = []
    monkeypatch.setattr(manager.logger, "warning", warnings.append)
    monkeypatch.setattr(manager.logger, "info", lambda *args: infos.append(args))
    bound_hosts(manager._bind())
    assert warnings == warned
    hosts = ("::1", "127.0.0.1") if rpc_host is None else (rpc_host,)
    assert infos == [
        ("Binding RPC on address %s port %s", host, manager.port) for host in hosts
    ]


def test_a_body_that_is_not_json_answers_parse_error_and_forgets_the_client(
    a_manager: AManagerFactory,
) -> None:
    """A non-JSON body over a real socket answers PARSE_ERROR, socket closed.

    `HTTPReq_JSONRPC`'s own, 500 in the legacy envelope (issue #63).
    """
    port = get_random_port()
    manager = a_manager(port)
    manager.start()
    try:
        wait_until_listening(manager)
        body = b"not json"
        head = b"POST / HTTP/1.1\r\nHost: x\r\n" + RPCAUTH_LINE
        head += b"Content-Length: %d\r\n\r\n" % len(body)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
            client.sendall(head + body)
            client.settimeout(20)
            response_head, _, response_body = client.recv(4096).partition(b"\r\n\r\n")
        assert response_head.startswith(b"HTTP/1.1 500 Internal Server Error\r\n")
        assert json.loads(response_body) == {
            "result": None,
            "error": {"code": -32700, "message": "Parse error"},
            "id": None,
        }
        wait_until(lambda: not manager.connections)
    finally:
        manager.stop()
        manager.join(timeout=10)


def test_stop_still_closes_a_connection_mid_parse_error_reply(
    a_manager: AManagerFactory,
) -> None:
    """`stop`, right as a parse error's own reply is scheduled, still closes it.

    Nothing paces `stop` against `run`'s own parse-error branch the way
    `manager.messages` paces a well-formed request through `handle_rpc`:
    that branch answers on the spot, scheduling `async_send` as its own
    task rather than awaiting it inline (issue #640 review round 2).
    `stop`'s own two sweeps -- `asyncio.all_tasks(self.loop)`, cancelled,
    and `self.connections`, closed directly -- are read exactly once,
    right after `run`'s own task has finished scheduling that reply and
    before either of them has taken a single further step; this is that
    exact instant, made deterministic by driving the loop by hand rather
    than racing it from a second thread the way the real listener would.

    `run_coroutine_threadsafe` -- what a fix that scheduled through the
    wrong seam looks like here -- only queues the `Task`'s own creation
    for a turn after this one, so it is neither in `all_tasks()` for
    `stop`'s cancel sweep nor, if popped by `run` the way a dispatched
    reply is, still in `self.connections` for its close sweep either:
    the coroutine is torn down unawaited, with nothing to answer this
    socket and nothing to close it, so the client sees neither a reply
    nor a clean end-of-file, only a hang until its own timeout.
    """
    manager = a_manager(get_random_port())
    loop = manager.loop
    ours, theirs = socket.socketpair()
    try:
        conn = manager.create_connection(loop, ours)
        body = b"not json"
        head = b"POST / HTTP/1.1\r\nHost: x\r\n" + RPCAUTH_LINE
        head += b"Content-Length: %d\r\n\r\n" % len(body)
        theirs.sendall(head + body)
        run_task = loop.create_task(conn.run())
        # One full batch of whatever is already ready, then stop --
        # `run`'s own task, scheduled at creation, is the only thing
        # ready the first time through, so this is that task's own
        # first and only step, landing it on `except ValueError` and
        # back out again without ever suspending a second time.
        while not run_task.done():
            loop.call_soon(loop.stop)
            loop.run_forever()
        manager.stop()
        theirs.settimeout(5)
        assert theirs.recv(4096) == b""
        # a closed socket's own fileno is -1; still >= 0 is still open
        assert ours.fileno() == -1
    finally:
        theirs.close()


def test_a_manager_that_cannot_bind_stops_being_alive(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#88: see tests/unit/p2p/manager.py's manager of the same name.

    `_bind` runs in `run` before `run_forever`, so a taken port's
    `OSError` ends `run` itself instead of sitting unread in the
    `concurrent.futures.Future` `run_coroutine_threadsafe` used to hand
    back, and `start_listener` answers that it is not listening. The
    bind comes before the cookie, as in Core's `AppInitServers`, so the
    failure leaves no cookie behind.
    """
    logged: list[tuple[object, ...]] = []
    # both loopbacks and not "" (every interface, p2p/manager.py's own
    # test of the same name): the manager under test binds
    # Config.rpc_host's default, and a wildcard bind here would not
    # contend for those specific addresses the way it did before #27
    with taken_loopbacks() as port:
        manager = a_manager(port)
        monkeypatch.setattr(manager.logger, "error", lambda *a: logged.append(a))
        assert not manager.start_listener()
        wait_until(lambda: not manager.is_alive())
    assert logged == [("Unable to bind any endpoint for RPC server",)]
    assert not manager.listening.is_set()
    assert not cookie_path(manager.node.config.data_dir).exists()


def test_stop_closes_the_listening_socket_even_when_the_accept_task_never_ran(
    a_manager: AManagerFactory,
) -> None:
    """`stop` closes the listening socket even where the accept task never ran.

    `stop()` can cancel `server`'s own task before `run_forever` has
    stepped it even once -- what a manager started and stopped in quick
    succession does. `Task.cancel()` reaching a coroutine with no frame
    yet raises `CancelledError` at its own definition point rather than
    inside the running body, so `server`'s `ExitStack` is never entered
    and its own `__exit__` never runs (issue #323).

    The task is cancelled directly, before the loop has run a single
    iteration, which reproduces that deterministically where the real
    race -- `stop()` racing `run_forever`'s own first iteration -- is
    not.
    """
    manager = a_manager(get_random_port())
    loop = manager.loop
    server_sockets = manager._bind()
    manager._server_sockets = server_sockets
    task = loop.create_task(manager.server(loop, server_sockets))
    task.cancel()
    with suppress(asyncio.CancelledError):
        loop.run_until_complete(task)
    # the `ExitStack` was never entered: nothing has closed these yet
    assert all(server_socket.fileno() != -1 for server_socket in server_sockets)

    manager.stop()
    # a closed socket's own fileno is -1; still >= 0 is still open
    assert all(server_socket.fileno() == -1 for server_socket in server_sockets)


def test_server_closes_every_listening_socket_it_was_handed(
    a_manager: AManagerFactory,
) -> None:
    """ISS 1269: cancelled on its own, `server` closes each of its sockets.

    `stop` closes them too, so this is the caller that cancels `server`
    alone, as `server`'s own `finally` has it.
    """
    manager = a_manager(get_random_port())
    loop = manager.loop
    server_sockets = manager._bind()
    task = loop.create_task(manager.server(loop, server_sockets))
    while manager._accept_queue is None:
        loop.run_until_complete(asyncio.sleep(0))
    task.cancel()
    with suppress(asyncio.CancelledError):
        loop.run_until_complete(task)
    # a closed socket's own fileno is -1
    assert all(server_socket.fileno() == -1 for server_socket in server_sockets)


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

    `listening_socket` is bound and put into `listen`, matching what
    `_bind` always hands `server` in production (#430): left merely
    constructed, as an earlier revision of this test had it,
    `_accept_loop`'s own `sock_accept` fails on it synchronously and
    every retry races this test's own cancellation against Windows'
    Proactor allocating and abandoning its own internal accept socket,
    which is a real leak but not the one this test is for -- accepting
    nothing at all, bound and listening, is the ordinary idle shutdown
    `_accept_loop` is cancelled out of on every platform this node runs
    on, ceasing there without raising -- `settimeout(0.0)` beside it for
    the same reason `_bind_one` carries its own: a blocking `accept()`
    reached synchronously, which is what a freshly constructed socket
    still is, freezes this single thread's event loop for the length of
    `pyproject.toml`'s own per-test ceiling rather than ever reaching
    `_accept_loop`'s `await`.
    """
    manager = a_manager(get_random_port())
    loop = manager.loop
    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening_socket.bind(("127.0.0.1", 0))
    listening_socket.listen()
    listening_socket.settimeout(0.0)
    accepted, theirs = socket.socketpair()

    task = loop.create_task(manager.server(loop, [listening_socket]))
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


def test_stop_does_not_raise_where_start_was_called_but_run_never_reached_run_forever(
    a_manager: AManagerFactory,
) -> None:
    """`stop` does not raise where start ran but run never reached run_forever.

    `self.ident is not None` -- issue #362's own guard on a grace step
    this method no longer has -- is true from the moment `start()`
    is called, well before `run()` reaches `run_forever()`. Where
    `run()` returns before that -- a bind failure being the ordinary way
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
    with taken_loopbacks() as port:
        manager = a_manager(port)
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


def test_accept_loop_logs_and_retries_on_a_refused_accept(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_accept_loop`'s `OSError` arm logs and retries the accept.

    `accept()` can fail outright -- `ECONNABORTED` being the ordinary
    way, a peer resetting the connection between the kernel reporting it
    readable and the accept reaching it -- and this is what keeps that
    from ending the task outright: the queue `server` awaits is left
    untouched, and a fresh `sock_accept` is tried again rather than this
    coroutine returning.

    `sock_accept` itself is monkeypatched to fail rather than handed a
    socket engineered to make the real one fail: a duck-typed stand-in
    with a `fileno` of -1, this test's own shape before #430's Windows
    run, reaches a selector loop's `sock.accept()` call harmlessly but
    reaches Windows' Proactor at the raw `AcceptEx` level, on the real
    fd it reads off that -1 rather than through any Python-level
    `.accept()` call -- which is what crashed the worker process outright
    rather than raising, on that platform only. What this test is for is
    `_accept_loop`'s own retry-and-log arm, not the kernel's accept
    machinery underneath `sock_accept`, so a fake replacing `sock_accept`
    itself is the narrower fake and is what stays clear of it.

    The failure below is synchronous, so `loop.sock_accept`'s own
    internal future is already done the instant this loop's `await`
    reaches it -- awaiting an already-done future never suspends a task,
    so nothing but `_accept_loop`'s own `await asyncio.sleep(0)` lets the
    loop below step it more than once; without that yield this test hangs
    to `pyproject.toml`'s own per-test ceiling rather than reaching three
    attempts.
    """
    manager = a_manager(get_random_port())
    loop = manager.loop
    accepted: asyncio.Queue[Any] = asyncio.Queue()
    logged: list[Any] = []
    monkeypatch.setattr(manager.logger, "exception", lambda *a, **k: logged.append(a))

    async def refuse(
        sock: socket.socket,
    ) -> tuple[socket.socket, tuple[str, int]]:
        raise OSError("accept refused")  # noqa: TRY003

    monkeypatch.setattr(loop, "sock_accept", refuse)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        task = loop.create_task(manager._accept_loop(server_socket, accepted))
        try:
            while len(logged) < 3:
                loop.run_until_complete(asyncio.sleep(0))
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                loop.run_until_complete(task)
    assert accepted.empty()


def test_report_server_failure_logs_a_real_exception(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_report_server_failure` logs whatever `server`'s own task raised.

    `run`'s own `.add_done_callback` is what reaches this, not a live
    listener -- a `future` neither cancelled nor holding a
    `CancelledError`, the two arms the tests beside this one cover.
    """
    manager = a_manager(get_random_port())
    logged: list[BaseException | None] = []
    monkeypatch.setattr(
        manager.logger,
        "error",
        lambda *a, exc_info=None, **k: logged.append(exc_info),
    )
    future: Future[None] = Future()
    boom = ValueError("boom")
    future.set_exception(boom)
    manager._report_server_failure(future)
    assert logged == [boom]


def test_report_server_failure_suppresses_a_cancelled_future(
    a_manager: AManagerFactory,
) -> None:
    """`_report_server_failure` does not raise where `future.cancelled()`.

    This is the ordinary shape `stop`'s own sweep leaves behind: cancelling
    the `asyncio.Task` underneath `future` puts `future` itself into the
    cancelled state through `run_coroutine_threadsafe`'s own chaining
    (`asyncio.futures._set_concurrent_future_state`), and
    `future.exception()` on a cancelled future raises rather than
    returning -- which is what the `with suppress` this method opens
    with answers, before the `isinstance` arm below it is ever reached.
    """
    manager = a_manager(get_random_port())
    future: Future[None] = Future()
    assert future.cancel()
    manager._report_server_failure(future)


def test_report_server_failure_does_not_log_a_returned_cancelled_error(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_report_server_failure`'s `isinstance` arm, `stop` never reaches.

    `future.cancelled()` being true is what the test above covers, and
    is the only way `stop`'s own sweep actually ends this task -- so
    this arm has no path of its own through this class's ordinary use,
    only through a `future` built to carry a `CancelledError` as its
    stored exception rather than through `.cancel()`: what a `server`
    coroutine that caught and re-raised one not thrown by `Task.cancel`
    would leave behind, `Task.cancelled()` still false. Moved here
    rather than removed, on the same reasoning the coverage floor
    argues for a safety branch generally.
    """
    manager = a_manager(get_random_port())
    logged: list[BaseException | None] = []
    monkeypatch.setattr(
        manager.logger,
        "error",
        lambda *a, exc_info=None, **k: logged.append(exc_info),
    )
    future: Future[None] = Future()
    future.set_exception(asyncio.CancelledError())
    manager._report_server_failure(future)
    assert logged == []


def test_the_cookie_is_there_once_listening_and_gone_once_stopped(
    a_manager: AManagerFactory,
) -> None:
    """`run` writes the cookie before `listening`, and `stop` deletes it.

    A request carrying the cookie's own credential is queued, which is
    what a client that waited on `listening` and then read the cookie
    does.
    """
    port = get_random_port()
    manager = a_manager(port)
    path = cookie_path(manager.node.config.data_dir)
    try:
        # returns once listening, with nothing left to wait for
        assert manager.start_listener()
        assert path.exists()
        cookie = path.read_bytes()
        body = json.dumps(REQUEST).encode()
        head = b"POST / HTTP/1.1\r\nHost: x\r\nAuthorization: Basic "
        head += base64.b64encode(cookie) + b"\r\n"
        head += b"Content-Length: %d\r\n\r\n" % len(body)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
            client.sendall(head + body)
            wait_until(lambda: manager.messages)
        assert manager.messages.popleft()[0] == REQUEST
    finally:
        manager.stop()
        manager.join(timeout=10)
    assert not path.exists()


def test_a_wrong_password_is_logged_with_the_address_it_came_from(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's own warning, naming the client's `ip:port`, and a 401."""
    logged: list[tuple[object, ...]] = []
    port = get_random_port()
    manager = a_manager(port)
    monkeypatch.setattr(manager.logger, "warning", lambda *args: logged.append(args))
    manager.start()
    try:
        wait_until_listening(manager)
        body = json.dumps(REQUEST).encode()
        head = b"POST / HTTP/1.1\r\nHost: x\r\nAuthorization: Basic "
        head += base64.b64encode(b"pytest:wrong") + b"\r\n"
        head += b"Connection: close\r\nContent-Length: %d\r\n\r\n" % len(body)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
            client.sendall(head + body)
            client.settimeout(20)
            reply = client.recv(4096)
            local = client.getsockname()
        assert reply.startswith(b"HTTP/1.1 401 Unauthorized\r\n")
    finally:
        manager.stop()
        manager.join(timeout=10)
    assert logged == [
        (
            "ThreadRPCServer incorrect password attempt from %s",
            f"{local[0]}:{local[1]}",
        )
    ]
    assert not manager.messages


def test_a_manager_that_cannot_write_its_cookie_does_not_listen(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `InitRPCAuthentication` failing stops the RPC server, here too.

    The socket already bound is closed by `run` itself, before `stop`
    is ever called, so the port is not held by a manager that failed.
    """
    logged: list[tuple[object, ...]] = []
    manager = a_manager(get_random_port())
    manager.node.config.data_dir.rmdir()
    monkeypatch.setattr(manager.logger, "warning", lambda *args: logged.append(args))
    assert not manager.start_listener()
    wait_until(lambda: not manager.is_alive())
    tmp = f"{cookie_path(manager.node.config.data_dir)}.tmp"
    assert [str(args[1]) for args in logged] == [
        f"Unable to open cookie authentication file {tmp} for writing"
    ]
    assert not manager.listening.is_set()
    # both loopbacks bound, then closed: a closed socket's own fileno is -1
    assert len(manager._server_sockets) == 2
    assert all(sock.fileno() == -1 for sock in manager._server_sockets)
    manager.stop()


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            "/",
            marks=pytest.mark.skipif(
                os.name == "nt" or os.geteuid() == 0,
                reason="`/` writable by the user",
            ),
        ),
        ".",
        "a/..",
    ],
)
def test_an_rpccookiefile_core_cannot_write_stops_the_listener(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Refused on `_listen`'s own path, as `bitcoind` v31.1.0 refuses to start.

    `/` cannot be opened as `/.tmp`, and the chain directory named as
    `.` is written to `<chain dir>/..tmp` and cannot be renamed over.
    Nothing reaches `threading.excepthook`, and the socket is closed.
    """
    raised: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", raised.append)
    logged: list[tuple[object, ...]] = []
    manager = a_manager(get_random_port())
    data_dir = manager.node.config.data_dir
    config = Config(chain="regtest", data_dir=data_dir.parent, rpccookiefile=value)
    manager.auth.cookie_file = config.rpc_cookie_file
    manager.auth.cookie_tmp = config.rpc_cookie_tmp
    monkeypatch.setattr(manager.logger, "warning", lambda *args: logged.append(args))
    assert not manager.start_listener()
    wait_until(lambda: not manager.is_alive())
    assert raised == []
    assert len(logged) == 1
    assert str(logged[0][1]).startswith("Unable to ")
    assert len(manager._server_sockets) == 2
    assert all(sock.fileno() == -1 for sock in manager._server_sockets)
    assert not data_dir.with_name(data_dir.name + ".tmp").exists()
    manager.stop()


def test_a_cookie_that_cannot_be_removed_is_logged_and_stop_goes_on(
    a_manager: AManagerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `DeleteAuthCookie` logs a failure and goes on, and so does `stop`.

    A directory where the cookie was is what `unlink` refuses on every
    platform.
    """
    logged: list[object] = []
    manager = a_manager(get_random_port())
    monkeypatch.setattr(manager.logger, "warning", lambda msg, **_: logged.append(msg))
    blocker = manager.node.config.data_dir / "blocker"
    blocker.mkdir()
    manager.auth.cookie_path = blocker
    manager.stop()
    assert logged == ["Unable to remove the RPC authentication cookie"]
    assert blocker.is_dir()
