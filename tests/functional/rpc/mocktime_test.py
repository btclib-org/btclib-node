# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`setmocktime`, live: the clock a real node reads moves when the caller says.

A ban's age and expiry are read off the node's clock, so `listbanned`
answering from a clock the test sets and steps is a time-dependent
behaviour observed moving over the RPC socket.
"""

from typing import TYPE_CHECKING

from btclib_node import clock
from btclib_node.rpc.help import HELP_TEXT
from tests import rpc_client, wait_until_listening

if TYPE_CHECKING:
    from btclib_node import Node

START = 1_700_000_000


def test_a_ban_ages_and_expires_with_the_mocked_clock(rpc_node: Node) -> None:
    """The ban list reads the clock `setmocktime` sets, and steps with it."""
    wait_until_listening(rpc_node.rpc_manager)
    client = rpc_client(rpc_node)

    assert client.call("setmocktime", [START]) is None
    client.call("setban", ["192.0.2.1", "add", 60])

    (ban,) = client.call("listbanned")
    assert ban["ban_created"] == START
    assert ban["banned_until"] == START + 60
    assert ban["time_remaining"] == 60

    client.call("setmocktime", [START + 59])
    (ban,) = client.call("listbanned")
    assert ban["time_remaining"] == 1

    client.call("setmocktime", [START + 61])
    assert client.call("listbanned") == []


def test_zero_hands_the_node_back_to_the_system_clock(rpc_node: Node) -> None:
    """After `0`, a ban is created at the system time, not at the mocked one."""
    wait_until_listening(rpc_node.rpc_manager)
    client = rpc_client(rpc_node)

    client.call("setmocktime", [START])
    client.call("setmocktime", [0])
    client.call("setban", ["192.0.2.2", "add", 60])

    (ban,) = client.call("listbanned")
    assert ban["ban_created"] > START
    assert clock.mock_time() == 0


def test_a_refusal_answers_core_s_error_and_leaves_the_clock(rpc_node: Node) -> None:
    """Out of range is `-8` with Core's message; the mock time stays as set."""
    wait_until_listening(rpc_node.rpc_manager)
    client = rpc_client(rpc_node)
    client.call("setmocktime", [START])

    _, body = client.call_raw("setmocktime", [-1], jsonrpc="1.0", request_timeout=2)
    assert body["error"] == {
        "code": -8,
        "message": "Mocktime must be in the range [0, 9223372036], not -1.",
    }
    _, body = client.call_raw("setmocktime", [], jsonrpc="1.0", request_timeout=2)
    assert body["error"] == {"code": -1, "message": HELP_TEXT["setmocktime"]}
    assert clock.mock_time() == START
