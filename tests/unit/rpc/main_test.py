# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What handle_rpc answers, for the shapes it is asked.

`Node.run` calls it without a guard of its own, so a request it cannot
answer used to end the node rather than the request -- which is what
these are mostly about.
"""

from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast

import btclib_node.rpc.callbacks as rpc_callbacks
from btclib_node.exceptions import StoreCorruptionError
from btclib_node.log import Logger
from btclib_node.rpc.callbacks import callbacks
from btclib_node.rpc.errors import RpcError, RpcErrorCode, error_msg
from btclib_node.rpc.main import (
    get_connection,
    handle_rpc,
    is_valid_rpc,
)
from tests import generate_random_transaction

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from btclib_node.rpc.manager import RpcManager

PING = {"jsonrpc": "2.0", "id": "a", "method": "ping"}


def make_node(
    batch: list[Any], conn_id: int = 0, *, callback: Any = None, logger: Any = None
) -> tuple[Any, list[Any], list[Any], list[bool]]:
    """Build a node whose rpc_manager queues `batch` for handle_rpc to pop.

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
            messages=deque([(batch, conn_id)]), connections={0: conn}
        ),
        logger=logger
        if logger is not None
        else SimpleNamespace(debug=lambda *a: None, exception=lambda *a: None),
        stop=lambda: stopped.append(True),
        p2p_manager=SimpleNamespace(ping_all=callback or (lambda: None)),
    )
    return node, sent, waited, stopped


def test_a_request_is_answered() -> None:
    """handle_rpc dispatches a valid request and sends its own result back."""
    node, sent, _, _ = make_node([PING])
    handle_rpc(node)
    assert sent == [[{"jsonrpc": "2.0", "result": None, "id": "a"}]]


def test_an_empty_batch_is_an_invalid_request() -> None:
    """handle_rpc answers an empty batch as JSON-RPC 2.0's own invalid request.

    Reading the loop variable after a loop that never ran used to end
    the node instead.
    """
    node, sent, _, stopped = make_node([])
    handle_rpc(node)
    assert sent == [[error_msg(RpcErrorCode.INVALID_REQUEST, "Invalid request")]]
    assert not stopped


def test_a_batch_ending_in_something_that_is_not_an_object() -> None:
    """handle_rpc answers a non-object batch entry as an invalid request."""
    node, sent, _, stopped = make_node([PING, "garbage"])
    handle_rpc(node)
    answers = sent[0]
    assert answers[1] == error_msg(RpcErrorCode.INVALID_REQUEST, "Invalid request")
    assert not stopped


def test_an_unknown_method_is_answered_not_found() -> None:
    """handle_rpc answers a method not in the callback table as not found."""
    node, sent, _, _ = make_node([{"jsonrpc": "2.0", "id": "a", "method": "nosuch"}])
    handle_rpc(node)
    assert sent == [[error_msg(RpcErrorCode.METHOD_NOT_FOUND, "Method not found", "a")]]


def test_a_request_without_an_id_is_invalid() -> None:
    """handle_rpc refuses a request missing JSON-RPC 2.0's own required id."""
    node, sent, _, _ = make_node([{"jsonrpc": "2.0", "method": "ping"}])
    handle_rpc(node)
    assert sent == [[error_msg(RpcErrorCode.INVALID_REQUEST, "Invalid request")]]


def test_a_callback_that_raises_is_answered_internal_error() -> None:
    """handle_rpc answers a raising callback INTERNAL_ERROR, and logs it too."""

    def boom() -> NoReturn:
        raise RuntimeError("no")

    node, sent, _, _ = make_node([PING], callback=boom)
    logged: list[Any] = []
    node.logger.exception = logged.append
    handle_rpc(node)
    assert sent == [[error_msg(RpcErrorCode.INTERNAL_ERROR, "Internal Error", "a")]]
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
    batch = [
        {"jsonrpc": "2.0", "id": "a", "method": "testmempoolaccept", "params": [[raw]]}
    ]
    log_path = tmp_path / "debug.log"
    logger = Logger(log_path, debug=True)
    node, sent, _, _ = make_node(batch, logger=logger)
    handle_rpc(node)
    logger.close()

    assert sent == [[error_msg(RpcErrorCode.INTERNAL_ERROR, "Internal Error", "a")]]
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
        raise RpcError(RpcErrorCode.INVALID_ADDRESS_OR_KEY, "Block not found")

    node, sent, _, _ = make_node([PING], callback=refuse)
    logged: list[Any] = []
    node.logger.exception = logged.append
    handle_rpc(node)
    assert sent == [
        [
            {
                "jsonrpc": "2.0",
                "error": {"code": -5, "message": "Block not found"},
                "id": "a",
            }
        ]
    ]
    assert not logged


def test_params_are_passed_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """handle_rpc passes a request's own params through to its callback."""
    seen: list[Any] = []
    node, _, _, _ = make_node(
        [{"jsonrpc": "2.0", "id": "a", "method": "withparams", "params": [1, 2]}]
    )
    monkeypatch.setitem(
        callbacks, "withparams", lambda node, conn, params: seen.append(params)
    )
    handle_rpc(node)
    assert seen == [[1, 2]]


def test_no_params_is_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """handle_rpc passes an empty list where a request carries no params."""
    seen: list[Any] = []
    node, _, _, _ = make_node([{"jsonrpc": "2.0", "id": "a", "method": "noparams"}])
    monkeypatch.setitem(
        callbacks, "noparams", lambda node, conn, params: seen.append(params)
    )
    handle_rpc(node)
    assert seen == [[]]


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


def test_an_answered_connection_is_forgotten() -> None:
    """handle_rpc pops a connection's own entry once it has answered it.

    The entry used to stay in `connections` for the life of the node,
    every request growing a dict nothing ever shrank (issue #64).
    """
    node, _, _, _ = make_node([PING])
    assert 0 in node.rpc_manager.connections
    handle_rpc(node)
    assert 0 not in node.rpc_manager.connections


def test_a_stopped_connection_is_forgotten_too() -> None:
    """handle_rpc pops the connection's own entry for a stop request too."""
    stop = {"jsonrpc": "2.0", "id": "a", "method": "stop"}
    node, _, _, stopped = make_node([stop])
    handle_rpc(node)
    assert stopped == [True]
    assert 0 not in node.rpc_manager.connections


def test_a_message_for_a_connection_that_is_gone_is_dropped() -> None:
    """handle_rpc drops a message whose connection id is not registered."""
    node, sent, _, _ = make_node([PING], conn_id=99)
    handle_rpc(node)
    assert not sent


def test_get_connection_answers_none_rather_than_raising() -> None:
    """get_connection answers None for a connection id not in the table."""
    manager = cast("RpcManager", SimpleNamespace(connections={}))
    assert get_connection(manager, 0) is None


def test_is_valid_rpc_wants_an_object_with_a_method_and_an_id() -> None:
    """is_valid_rpc requires an object carrying both a method and an id."""
    assert is_valid_rpc(PING)
    assert not is_valid_rpc("garbage")
    assert not is_valid_rpc({"id": "a"})
    assert not is_valid_rpc({"method": "ping"})


def test_a_method_that_is_not_a_string_is_invalid_not_a_crash() -> None:
    """is_valid_rpc refuses a non-string method rather than raising (issue #63).

    A list is unhashable, so `request["method"] not in callbacks` raises
    `TypeError` if `is_valid_rpc` lets it through.
    """
    assert not is_valid_rpc({"jsonrpc": "2.0", "id": 1, "method": ["a"]})
    assert not is_valid_rpc({"jsonrpc": "2.0", "id": 1, "method": {"a": 1}})


def test_a_method_that_is_not_a_string_is_answered_invalid_request() -> None:
    """handle_rpc answers a non-string method invalid, not a crash."""
    node, sent, _, stopped = make_node([{"jsonrpc": "2.0", "id": 1, "method": ["a"]}])
    handle_rpc(node)
    assert sent == [[error_msg(RpcErrorCode.INVALID_REQUEST, "Invalid request")]]
    assert not stopped


def test_an_error_carries_the_id_of_the_request_it_answers() -> None:
    """handle_rpc's error answers carry the request's own id, or null.

    JSON-RPC 2.0 section 5: the id is the request's own, and null is for
    a request no id could be read out of -- which is what the
    specification's own invalid-request example carries.
    """
    node, sent, _, _ = make_node([{"jsonrpc": "2.0", "id": "a", "method": "nosuch"}])
    handle_rpc(node)
    assert sent[0][0]["id"] == "a"

    node, sent, _, _ = make_node(["garbage"])
    handle_rpc(node)
    assert sent[0][0]["id"] is None
