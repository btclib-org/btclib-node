# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What handle_rpc answers, for the shapes it is asked.

`Node.run` calls it without a guard of its own, so a request it cannot
answer has to end the request rather than the node. The answers are
`bitcoind` v31.1.0's, whose envelope and HTTP status follow the version
a request names (`rpc.jsonrpc`).
"""

from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast

from bitcoin_core_rpc import RPCErrorCode

import btclib_node.rpc.callbacks as rpc_callbacks
from btclib_node.exceptions import StoreCorruptionError
from btclib_node.log import Logger
from btclib_node.rpc.callbacks import callbacks
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.jsonrpc import NO_CONTENT, OK, HttpReply
from btclib_node.rpc.main import get_connection, handle_rpc
from tests import generate_random_transaction

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from btclib_node.rpc.manager import RpcManager

PING = {"jsonrpc": "2.0", "id": "a", "method": "ping"}
LEGACY_PING = {"id": "a", "method": "ping"}
BAD_REQUEST = "400 Bad Request"
NOT_FOUND = "404 Not Found"
SERVER_ERROR = "500 Internal Server Error"


def make_node(
    body: object, conn_id: int = 0, *, callback: Any = None, logger: Any = None
) -> tuple[Any, list[Any], list[Any], list[bool]]:
    """Build a node whose rpc_manager queues `body` for handle_rpc to pop.

    Returns the node, and the lists its double `RpcConnection`'s `send`,
    `send_and_wait` and `stop` each append to -- the answer `handle_rpc`
    produces. `logger` defaults to a stand-in recording nothing, but a
    real `Logger` is what a test proving a log line needs instead,
    `Node.logger` never reaching `caplog` for `log.py`'s own reason
    (CLAUDE.md, btclib-org/btclib-node#587) -- the same default
    `p2p/main_test.py`'s own `make_node` already carries.
    """
    sent: list[Any] = []
    waited: list[Any] = []
    conn = SimpleNamespace(send=sent.append, send_and_wait=waited.append)
    stopped: list[bool] = []
    node = SimpleNamespace(
        rpc_manager=SimpleNamespace(
            messages=deque([(body, conn_id)]), connections={0: conn}
        ),
        logger=logger
        if logger is not None
        else SimpleNamespace(debug=lambda *a: None, exception=lambda *a: None),
        stop=lambda: stopped.append(True),
        p2p_manager=SimpleNamespace(ping_all=callback or (lambda: None)),
    )
    return node, sent, waited, stopped


def error(code: RPCErrorCode, message: str) -> dict[str, Any]:
    """Return the error object Core's `JSONRPCError` builds."""
    return {"code": code.value, "message": message}


def test_a_request_is_answered() -> None:
    """A 2.0 request is answered 200 in the 2.0 envelope."""
    node, sent, _, _ = make_node(PING)
    handle_rpc(node)
    assert sent == [HttpReply(OK, {"jsonrpc": "2.0", "result": None, "id": "a"})]


def test_a_legacy_request_is_answered_in_the_legacy_envelope() -> None:
    """A request naming no version, or 1.0, gets `result` and `error` both."""
    for request in (LEGACY_PING, {**LEGACY_PING, "jsonrpc": "1.0"}):
        node, sent, _, _ = make_node(request)
        handle_rpc(node)
        assert sent == [HttpReply(OK, {"result": None, "error": None, "id": "a"})]


def test_a_legacy_request_without_an_id_is_answered_without_one() -> None:
    """`JSONRPCReplyObj` writes an `id` only where the request had one."""
    node, sent, _, _ = make_node({"method": "ping"})
    handle_rpc(node)
    assert sent == [HttpReply(OK, {"result": None, "error": None})]


def test_a_legacy_error_is_an_http_error_status() -> None:
    """`JSONErrorReply`: 400 for an invalid request, 404 for an unknown method.

    The three bodies ISS 1109 measured against `bitcoind` v31.1.0; the
    third, a request Core's parse refuses, is not run.
    """
    ran: list[Any] = []
    node, sent, _, _ = make_node(
        {"id": 1, "method": "ping", "params": 5}, callback=lambda: ran.append(1)
    )
    handle_rpc(node)
    message = "Params must be an array or object"
    body = {"result": None, "error": error(RPCErrorCode.INVALID_REQUEST, message)}
    assert sent == [HttpReply(BAD_REQUEST, {**body, "id": 1})]
    assert not ran

    node, sent, _, _ = make_node({"jsonrpc": "1.0", "id": 1, "method": "nosuch"})
    handle_rpc(node)
    not_found = error(RPCErrorCode.METHOD_NOT_FOUND, "Method not found")
    assert sent == [HttpReply(NOT_FOUND, {"result": None, "error": not_found, "id": 1})]


def test_a_2_0_error_is_http_200() -> None:
    """A 2.0 request's error is caught into its reply, as `JSONRPCExec` does."""
    node, sent, _, _ = make_node({"jsonrpc": "2.0", "id": "a", "method": "nosuch"})
    handle_rpc(node)
    not_found = error(RPCErrorCode.METHOD_NOT_FOUND, "Method not found")
    assert sent == [HttpReply(OK, {"jsonrpc": "2.0", "error": not_found, "id": "a"})]


def test_a_2_0_request_the_parse_refuses_gets_the_legacy_status() -> None:
    """Core answers a 2.0 request its parse refuses through `JSONErrorReply`.

    The envelope is 2.0, the version having been read before the refusal,
    and the status is the legacy one, as `bitcoind` v31.1.0 answers it.
    """
    node, sent, _, _ = make_node({"jsonrpc": "2.0", "id": 1, "method": ["a"]})
    handle_rpc(node)
    message = "Method must be a string"
    body = {"jsonrpc": "2.0", "error": error(RPCErrorCode.INVALID_REQUEST, message)}
    assert sent == [HttpReply(BAD_REQUEST, {**body, "id": 1})]


def test_a_request_whose_version_is_refused_is_answered_legacy() -> None:
    """A `jsonrpc` that is not `"1.0"` or `"2.0"` leaves the request legacy."""
    for version, message in (
        (2, "jsonrpc field must be a string"),
        ("3.0", "JSON-RPC version not supported"),
    ):
        node, sent, _, _ = make_node({"jsonrpc": version, "method": "ping"})
        handle_rpc(node)
        reply = {"result": None, "error": error(RPCErrorCode.INVALID_REQUEST, message)}
        assert sent == [HttpReply(BAD_REQUEST, reply)]


def test_a_request_without_a_method_is_refused() -> None:
    """`JSONRPCRequest::parse` refuses a request with no method."""
    node, sent, _, _ = make_node({"id": 1})
    handle_rpc(node)
    missing = error(RPCErrorCode.INVALID_REQUEST, "Missing method")
    assert sent == [HttpReply(BAD_REQUEST, {"result": None, "error": missing, "id": 1})]


def test_a_2_0_notification_runs_and_is_answered_no_content() -> None:
    """A 2.0 request with no `id` runs, and its answer is 204 with no body."""
    ran: list[Any] = []
    node, sent, _, _ = make_node(
        {"jsonrpc": "2.0", "method": "ping"}, callback=lambda: ran.append(1)
    )
    handle_rpc(node)
    assert sent == [HttpReply(NO_CONTENT, None)]
    assert ran == [1]


def test_a_body_that_is_not_an_object_or_an_array_is_a_parse_error() -> None:
    """Core's "Top-level object parse error", 500 in the legacy envelope."""
    node, sent, _, stopped = make_node(5)
    handle_rpc(node)
    top = error(RPCErrorCode.PARSE_ERROR, "Top-level object parse error")
    assert sent == [HttpReply(SERVER_ERROR, {"result": None, "error": top, "id": None})]
    assert not stopped


def test_an_empty_batch_answers_an_empty_array() -> None:
    """An empty batch is answered `[]`, as Core answers it (issue #669)."""
    node, sent, _, stopped = make_node([])
    handle_rpc(node)
    assert sent == [HttpReply(OK, [])]
    assert not stopped


def test_a_batch_is_answered_200_whatever_its_members_versions() -> None:
    """Every member of a batch is answered inside HTTP 200.

    A member that is not an object is refused before its own `id` is
    read, so it carries the previous member's `id` and version, as
    `bitcoind` v31.1.0 answers `[{"id":7,...},5,...]`.
    """
    node, sent, _, stopped = make_node(
        [LEGACY_PING, "garbage", {"id": "b", "method": "nosuch"}]
    )
    handle_rpc(node)
    invalid = error(RPCErrorCode.INVALID_REQUEST, "Invalid Request object")
    not_found = error(RPCErrorCode.METHOD_NOT_FOUND, "Method not found")
    assert sent == [
        HttpReply(
            OK,
            [
                {"result": None, "error": None, "id": "a"},
                {"result": None, "error": invalid, "id": "a"},
                {"result": None, "error": not_found, "id": "b"},
            ],
        )
    ]
    assert not stopped


def test_each_batch_member_is_answered_in_its_own_version() -> None:
    """A legacy member after a 2.0 one is answered legacy, as in `bitcoind`."""
    node, sent, _, _ = make_node([PING, LEGACY_PING])
    handle_rpc(node)
    assert sent == [
        HttpReply(
            OK,
            [
                {"jsonrpc": "2.0", "result": None, "id": "a"},
                {"result": None, "error": None, "id": "a"},
            ],
        )
    ]


def test_a_batch_leaves_out_its_notifications() -> None:
    """A notification in a batch runs and gets no member of the answer."""
    ran: list[Any] = []
    notification = {"jsonrpc": "2.0", "method": "ping"}
    node, sent, _, _ = make_node([notification, PING], callback=lambda: ran.append(1))
    handle_rpc(node)
    assert sent == [HttpReply(OK, [{"jsonrpc": "2.0", "result": None, "id": "a"}])]
    assert ran == [1, 1]


def test_a_batch_of_notifications_is_answered_no_content() -> None:
    """A non-empty batch with nothing to answer is 204, as in Core.

    The member refused after a notification carries that notification's
    missing `id` and version, so it is left out too.
    """
    notification = {"jsonrpc": "2.0", "method": "ping"}
    node, sent, _, _ = make_node([notification, "garbage"])
    handle_rpc(node)
    assert sent == [HttpReply(NO_CONTENT, None)]


def test_a_callback_that_raises_is_answered_internal_error() -> None:
    """handle_rpc answers a raising callback INTERNAL_ERROR, and logs it too."""

    def boom() -> NoReturn:
        raise RuntimeError("no")

    internal = error(RPCErrorCode.INTERNAL_ERROR, "Internal Error")
    for request, reply in (
        (PING, HttpReply(OK, {"jsonrpc": "2.0", "error": internal, "id": "a"})),
        (
            LEGACY_PING,
            HttpReply(SERVER_ERROR, {"result": None, "error": internal, "id": "a"}),
        ),
    ):
        node, sent, _, _ = make_node(request, callback=boom)
        logged: list[Any] = []
        node.logger.exception = logged.append
        handle_rpc(node)
        assert sent == [reply]
        # -32603 is the node reporting itself broken, so it is the one
        # answer that is also an event of the node's
        assert logged == ["Exception occurred"]


def test_testmempoolaccept_own_store_error_reaches_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`testmempoolaccept`'s own store fault reaches `handle_rpc`'s log line.

    `test_mempool_accept` (`rpc/callbacks.py`) has no catch of its own
    for `StoreCorruptionError` -- the fault this proves reaches the log
    is a real one, monkeypatched onto `verify_mempool_acceptance` where
    `UtxoIndex.get_coin` would otherwise raise it -- so it propagates
    here the same as any other raising callback, `handle_rpc`'s own
    generic catch above being what logs it and answers `INTERNAL_ERROR`
    rather than `test_mempool_accept` folding it into one entry's own
    `"reject-reason"` (btclib-org/btclib-node#668). A real `Logger`
    writing to a real file, read back after, is what proves the line
    exists: `caplog` sees nothing from this tree's own logger
    (CLAUDE.md, btclib-org/btclib-node#587).
    """

    def corrupted(node: Any, tx: Any) -> NoReturn:
        err_msg = "stored utxo- record failed to parse"
        raise StoreCorruptionError(err_msg)

    monkeypatch.setattr(rpc_callbacks, "verify_mempool_acceptance", corrupted)
    raw = generate_random_transaction().serialize(include_witness=True).hex()
    request = {
        "jsonrpc": "2.0",
        "id": "a",
        "method": "testmempoolaccept",
        "params": [[raw]],
    }
    log_path = tmp_path / "debug.log"
    logger = Logger(log_path, debug=True)
    node, sent, _, _ = make_node(request, logger=logger)
    handle_rpc(node)
    logger.close()

    internal = error(RPCErrorCode.INTERNAL_ERROR, "Internal Error")
    assert sent == [HttpReply(OK, {"jsonrpc": "2.0", "error": internal, "id": "a"})]
    lines = [
        line
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if "Exception occurred" in line
    ]
    assert len(lines) == 1


def test_a_callback_that_refuses_names_its_own_code_and_reason() -> None:
    """handle_rpc answers an RpcError with the code and message it raised.

    The mechanism issue #83 asks for: a callback says which of the two
    was wrong, the request or the node, and the answer carries what it
    said.
    """

    def refuse() -> NoReturn:
        raise RpcError(RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Block not found")

    node, sent, _, _ = make_node(PING, callback=refuse)
    logged: list[Any] = []
    node.logger.exception = logged.append
    handle_rpc(node)
    not_found = {"code": -5, "message": "Block not found"}
    assert sent == [HttpReply(OK, {"jsonrpc": "2.0", "error": not_found, "id": "a"})]
    assert not logged


def test_params_are_passed_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """handle_rpc passes a request's own params through to its callback."""
    seen: list[Any] = []
    node, _, _, _ = make_node(
        {"jsonrpc": "2.0", "id": "a", "method": "withparams", "params": [1, 2]}
    )
    monkeypatch.setitem(
        callbacks, "withparams", lambda node, conn, params: seen.append(params)
    )
    handle_rpc(node)
    assert seen == [[1, 2]]


def test_no_params_is_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """handle_rpc passes an empty list where params are absent or null."""
    seen: list[Any] = []
    monkeypatch.setitem(
        callbacks, "noparams", lambda node, conn, params: seen.append(params)
    )
    for request in (
        {"jsonrpc": "2.0", "id": "a", "method": "noparams"},
        {"jsonrpc": "2.0", "id": "a", "method": "noparams", "params": None},
    ):
        node, _, _, _ = make_node(request)
        handle_rpc(node)
    assert seen == [[], []]


def test_stop_is_asked_of_the_batch_not_of_its_last_request() -> None:
    """handle_rpc stops the node for a stop request anywhere in the batch.

    The stop is not last, and it still stops the node; the answer is
    flushed before the node goes down, which is what `send_and_wait` is
    for -- a plain `send` would race the shutdown.
    """
    stop = {"jsonrpc": "2.0", "id": "a", "method": "stop"}
    node, sent, waited, stopped = make_node([stop, PING])
    handle_rpc(node)
    assert stopped == [True]
    assert len(waited) == 1
    assert not sent


def test_a_lone_stop_waits_for_its_reply() -> None:
    """A lone `stop` is answered by `send_and_wait`, then the node stops."""
    node, sent, waited, stopped = make_node({"id": "a", "method": "stop"})
    handle_rpc(node)
    assert stopped == [True]
    assert len(waited) == 1
    assert not sent


def test_an_answered_connection_is_left_for_async_send_to_forget() -> None:
    """handle_rpc leaves a connection's own entry for `async_send` to remove.

    `handle_rpc` used to pop this entry itself, right after scheduling
    `conn.send`, on the theory that every reply eventually closes --
    every request growing `connections` without bound otherwise, since
    nothing else shrank it (issue #64). Once a reply could keep the
    connection open instead (issue #640), that pop could run after
    `RpcConnection.async_send` had already read the *next* request off
    the same, still-open connection and queued it, on `RpcManager`'s own
    thread -- removing the entry that next request's own answer needed
    rather than the one this call was meant to retire, and losing that
    next request's answer with nothing logged on either side (issue
    #688). `RpcConnection.async_send` is the sole owner of this dict's
    membership now: this test pins `handle_rpc`'s own side of that, that
    it touches `connections` not at all.
    """
    node, _, _, _ = make_node(PING)
    assert 0 in node.rpc_manager.connections
    handle_rpc(node)
    assert 0 in node.rpc_manager.connections


def test_a_stopped_connection_is_left_registered_too() -> None:
    """handle_rpc does not pop the connection's own entry for `stop` either."""
    stop = {"jsonrpc": "2.0", "id": "a", "method": "stop"}
    node, _, _, stopped = make_node(stop)
    handle_rpc(node)
    assert stopped == [True]
    assert 0 in node.rpc_manager.connections


def test_a_message_for_a_connection_that_is_gone_is_dropped() -> None:
    """handle_rpc drops a message whose connection id is not registered."""
    node, sent, _, _ = make_node(PING, conn_id=99)
    handle_rpc(node)
    assert not sent


def test_get_connection_answers_none_rather_than_raising() -> None:
    """get_connection answers None for a connection id not in the table."""
    manager = cast("RpcManager", SimpleNamespace(connections={}))
    assert get_connection(manager, 0) is None
