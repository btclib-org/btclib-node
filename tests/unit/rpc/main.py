# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What handle_rpc answers, for every shape a client can send.

`Node.run` calls it without a guard of its own, so a request it cannot
answer used to end the node rather than the request -- which is what
these are mostly about.
"""

from collections import deque
from types import SimpleNamespace

from btclib_node.rpc.main import error_msg, get_connection, handle_rpc, is_valid_rpc

PING = {"jsonrpc": "2.0", "id": "a", "method": "ping"}


def make_node(batch, conn_id=0, *, callback=None):
    sent = []
    conn = SimpleNamespace(
        send=sent.append,
        send_and_wait=sent.append,
    )
    stopped = []
    node = SimpleNamespace(
        rpc_manager=SimpleNamespace(
            messages=deque([(batch, conn_id)]), connections={0: conn}
        ),
        logger=SimpleNamespace(debug=lambda *a: None, exception=lambda *a: None),
        stop=lambda: stopped.append(True),
        p2p_manager=SimpleNamespace(ping_all=callback or (lambda: None)),
    )
    return node, sent, stopped


def test_a_request_is_answered():
    node, sent, _ = make_node([PING])
    handle_rpc(node)
    assert sent == [[{"jsonrpc": "2.0", "result": None, "id": "a"}]]


def test_an_empty_batch_is_an_invalid_request():
    # JSON-RPC 2.0 says so, and reading the loop variable after a loop
    # that never ran used to end the node instead
    node, sent, stopped = make_node([])
    handle_rpc(node)
    assert sent == [[error_msg(-32600)]]
    assert not stopped


def test_a_batch_ending_in_something_that_is_not_an_object():
    node, sent, stopped = make_node([PING, "garbage"])
    handle_rpc(node)
    answers = sent[0]
    assert answers[1] == error_msg(-32600)
    assert not stopped


def test_an_unknown_method_is_answered_not_found():
    node, sent, _ = make_node([{"jsonrpc": "2.0", "id": "a", "method": "nosuch"}])
    handle_rpc(node)
    assert sent == [[error_msg(-32601)]]


def test_a_request_without_an_id_is_invalid():
    node, sent, _ = make_node([{"jsonrpc": "2.0", "method": "ping"}])
    handle_rpc(node)
    assert sent == [[error_msg(-32600)]]


def test_a_callback_that_raises_is_answered_internal_error():
    def boom():
        raise RuntimeError("no")

    node, sent, _ = make_node([PING], callback=boom)
    handle_rpc(node)
    assert sent == [[error_msg(-32603)]]


def test_params_are_passed_when_given():
    seen = []
    node, sent, _ = make_node(
        [{"jsonrpc": "2.0", "id": "a", "method": "ping", "params": [1, 2]}]
    )
    node.p2p_manager = SimpleNamespace(ping_all=lambda: seen.append("called"))
    handle_rpc(node)
    assert seen == ["called"]


def test_stop_is_asked_of_the_batch_not_of_its_last_request():
    stop = {"jsonrpc": "2.0", "id": "a", "method": "stop"}
    node, sent, stopped = make_node([stop, PING])
    handle_rpc(node)
    # the stop is not last, and it still stops the node
    assert stopped == [True]
    assert len(sent) == 1


def test_a_message_for_a_connection_that_is_gone_is_dropped():
    node, sent, _ = make_node([PING], conn_id=99)
    handle_rpc(node)
    assert not sent


def test_get_connection_answers_none_rather_than_raising():
    assert get_connection(SimpleNamespace(connections={}), 0) is None


def test_is_valid_rpc_wants_an_object_with_a_method_and_an_id():
    assert is_valid_rpc(PING)
    assert not is_valid_rpc("garbage")
    assert not is_valid_rpc({"id": "a"})
    assert not is_valid_rpc({"method": "ping"})


def test_an_unknown_error_code_becomes_an_internal_error():
    assert error_msg(-1)["error"]["code"] == -32603
