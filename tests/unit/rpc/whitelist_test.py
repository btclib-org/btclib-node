# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What `-rpcwhitelist` lets a user call, and what `RpcConnection` answers.

Every reply expected below is what a real `bitcoind` v31.1.0 answered
the same request with, run with `-rpcuser=alice -rpcpassword=pw
-rpcwhitelist=alice:getblockcount`, less the `Date` header libevent
adds and the `Content-Type` it adds to a 403, which `RpcConnection`
does not write.
"""

import asyncio
import base64
import json
import socket
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from btclib_node.rpc.auth import (
    FORBIDDEN,
    Refusal,
    RpcAuth,
    RpcAuthEntry,
    parse_whitelist,
)
from btclib_node.rpc.connection import RpcConnection
from btclib_node.rpc.jsonrpc import OK, HttpReply
from tests import RPCAUTH

if TYPE_CHECKING:
    from btclib_node.rpc.manager import RpcManager

ALLOWED = "getblockcount"


def whitelisted(*values: str, default: bool | None = None) -> RpcAuth:
    """Return an `RpcAuth` accepting `RPCAUTH`'s user under `values`."""
    return RpcAuth(
        (RpcAuthEntry.parse(RPCAUTH),),
        whitelist=parse_whitelist(values),
        whitelist_default=bool(values) if default is None else default,
    )


PYTEST_ONLY = ("pytest:" + ALLOWED,)


def test_parse_whitelist_splits_at_every_comma_and_space() -> None:
    """`SplitString(..., ", ")`: either separator, empty names kept."""
    assert parse_whitelist(["alice:a, b,,c"]) == {
        b"alice": frozenset({"a", "", "b", "c"})
    }


def test_parse_whitelist_takes_the_user_up_to_the_first_colon() -> None:
    """A method name after the first `:` may hold another."""
    assert parse_whitelist(["alice:a:b"]) == {b"alice": frozenset({"a:b"})}


def test_several_whitelists_for_one_user_intersect() -> None:
    """Core's `std::set_intersection`, for one user and not across users."""
    whitelist = parse_whitelist(["alice:a,b,c", "bob:x", "alice:b,c,d", "alice:c,e"])
    assert whitelist == {b"alice": frozenset({"c"}), b"bob": frozenset({"x"})}


def test_a_whitelist_with_no_colon_is_empty_unless_the_user_has_one() -> None:
    """No `:` names a user whose whitelist is empty, or leaves it as it was."""
    assert parse_whitelist(["alice"]) == {b"alice": frozenset()}
    assert parse_whitelist(["alice:a", "alice"]) == {b"alice": frozenset({"a"})}
    assert parse_whitelist(["alice:"]) == {b"alice": frozenset({""})}


def test_a_user_with_no_whitelist_calls_everything_by_default() -> None:
    """No `-rpcwhitelist` at all and no `-rpcwhitelistdefault`: no refusal."""
    auth = RpcAuth((RpcAuthEntry.parse(RPCAUTH),))
    assert auth.refusal(b"pytest", {"id": 1, "method": "stop"}) is None
    assert auth.refusal(b"pytest", [{"id": 1, "method": "stop"}]) is None


def test_a_whitelist_for_one_user_refuses_every_other_user_everything() -> None:
    """`-rpcwhitelistdefault` defaults to true once any whitelist is set."""
    auth = whitelisted("alice:" + ALLOWED)
    assert auth.refusal(b"pytest", {"id": 1, "method": ALLOWED}) == Refusal(
        FORBIDDEN, warning=("RPC User %s not allowed to call any methods", "pytest")
    )


def test_rpcwhitelistdefault_0_lets_every_other_user_call_everything() -> None:
    """`-rpcwhitelistdefault=0` beside a whitelist for somebody else."""
    auth = whitelisted("alice:" + ALLOWED, default=False)
    assert auth.refusal(b"pytest", {"id": 1, "method": "stop"}) is None


@pytest.mark.parametrize(
    "request_",
    [{"id": 1, "method": ALLOWED}, "{", 5, [], [{"id": 1, "method": ALLOWED}]],
    ids=["request", "string", "number", "empty batch", "batch"],
)
def test_rpcwhitelistdefault_1_refuses_everything_that_parsed(request_: Any) -> None:
    """With no whitelist at all, whatever the body parsed as is a 403."""
    auth = whitelisted(default=True)
    refusal = auth.refusal(b"pytest", request_)
    assert refusal is not None
    assert refusal.status == FORBIDDEN


@pytest.mark.parametrize(
    "request_",
    [
        {"id": 1, "method": "stop"},
        {"method": "stop"},
        {"jsonrpc": "2.0", "id": 1, "method": "stop"},
        {"jsonrpc": "1.0", "id": 1, "method": "stop"},
        {"jsonrpc": None, "id": 1, "method": "stop"},
        {"id": 1, "method": "stop", "params": None},
        {"id": 1, "method": "stop", "params": {}},
        [{"id": 1, "method": ALLOWED}, {"id": 2, "method": "stop"}],
        [{"id": 1, "method": ALLOWED}, {"id": 2, "method": "stop"}, 5],
    ],
    ids=[
        "request",
        "no id",
        "2.0",
        "1.0",
        "null jsonrpc",
        "null params",
        "object params",
        "batch",
        "batch with a later non-object",
    ],
)
def test_a_method_outside_the_whitelist_is_a_403(request_: Any) -> None:
    """Core's 403 and its warning naming the user and the method."""
    refusal = whitelisted(*PYTEST_ONLY).refusal(b"pytest", request_)
    assert refusal == Refusal(
        FORBIDDEN,
        warning=("RPC User %s not allowed to call method %s", "pytest", "stop"),
    )


@pytest.mark.parametrize(
    "request_",
    [
        {"id": 1, "method": ALLOWED},
        [{"id": 1, "method": ALLOWED}, {"id": 2, "method": ALLOWED}],
        [],
        5,
    ],
    ids=["request", "batch", "empty batch", "number"],
)
def test_what_the_whitelist_allows_is_not_refused(request_: Any) -> None:
    """Refused by nothing here: a number is refused where anybody's is."""
    assert whitelisted(*PYTEST_ONLY).refusal(b"pytest", request_) is None


def test_an_empty_method_name_is_what_an_empty_list_allows() -> None:
    """`-rpcwhitelist=pytest:` allows `""` and nothing else, as in Core."""
    auth = whitelisted("pytest:")
    assert auth.refusal(b"pytest", {"id": 1, "method": ""}) is None
    refusal = auth.refusal(b"pytest", {"id": 1, "method": ALLOWED})
    assert refusal is not None
    assert refusal.status == FORBIDDEN


# (request, status line, body) as `bitcoind` v31.1.0 answered them
CORE_ERRORS = [
    (
        [{"id": 1, "method": ALLOWED}, 5],
        "400 Bad Request",
        (
            '{"result":null,"error":{"code":-32600,"message":"Invalid Request object"},'
            '"id":null}'
        ),
    ),
    (
        [5, {"id": 2, "method": "stop"}],
        "400 Bad Request",
        (
            '{"result":null,"error":{"code":-32600,"message":"Invalid Request object"},'
            '"id":null}'
        ),
    ),
    (
        [{"id": 1, "method": 5}],
        "500 Internal Server Error",
        (
            '{"result":null,"error":{"code":-32700,"message":"JSON value of type number '
            'is not of expected type string"},"id":null}'
        ),
    ),
    (
        [{"id": 1, "method": {}}],
        "500 Internal Server Error",
        (
            '{"result":null,"error":{"code":-32700,"message":"JSON value of type object '
            'is not of expected type string"},"id":null}'
        ),
    ),
    (
        [{"id": 1}],
        "500 Internal Server Error",
        (
            '{"result":null,"error":{"code":-32700,"message":"JSON value of type null '
            'is not of expected type string"},"id":null}'
        ),
    ),
    (
        {"id": 1, "method": 5},
        "400 Bad Request",
        (
            '{"result":null,"error":{"code":-32600,"message":"Method must be a string"},'
            '"id":1}'
        ),
    ),
    (
        {"id": 1, "method": "stop", "params": 5},
        "400 Bad Request",
        (
            '{"result":null,"error":{"code":-32600,"message":"Params must be an array or '
            'object"},"id":1}'
        ),
    ),
    (
        {"id": 1, "method": ALLOWED, "params": 5},
        "400 Bad Request",
        (
            '{"result":null,"error":{"code":-32600,"message":"Params must be an array or '
            'object"},"id":1}'
        ),
    ),
    (
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        "400 Bad Request",
        (
            '{"jsonrpc":"2.0","error":{"code":-32600,"message":"Method must be a '
            'string"},"id":1}'
        ),
    ),
    (
        {"jsonrpc": "2.0", "id": 7, "method": "stop", "params": 5},
        "400 Bad Request",
        (
            '{"jsonrpc":"2.0","error":{"code":-32600,"message":"Params must be an array '
            'or object"},"id":7}'
        ),
    ),
    (
        {"jsonrpc": 3, "id": 1, "method": "stop"},
        "400 Bad Request",
        (
            '{"result":null,"error":{"code":-32600,"message":"jsonrpc field must be a '
            'string"},"id":1}'
        ),
    ),
    (
        {"jsonrpc": "3.0", "method": "stop"},
        "400 Bad Request",
        (
            '{"result":null,"error":{"code":-32600,"message":"JSON-RPC version not '
            'supported"}}'
        ),
    ),
    (
        {"id": 1},
        "400 Bad Request",
        '{"result":null,"error":{"code":-32600,"message":"Missing method"},"id":1}',
    ),
]
CORE_ERROR_IDS = [
    "batch, non-object after an allowed method",
    "batch, non-object before a refused method",
    "batch, method a number",
    "batch, method an object",
    "batch, no method",
    "method a number",
    "params a number, method refused",
    "params a number, method allowed",
    "2.0, method a number",
    "2.0, params a number",
    "jsonrpc a number",
    "version not supported, no id",
    "no method",
]


def exchange(auth: RpcAuth, requests: list[Any]) -> tuple[bytes, list[Any], list[Any]]:
    """Send `requests` over one kept-alive connection, the last closing it.

    Returns every reply's bytes, what was queued for `handle_rpc`, and
    every warning logged. A request queued is answered here the way
    `handle_rpc` would, with a reply of its own, so that the next one
    is read.
    """
    warnings: list[Any] = []

    async def main() -> tuple[bytes, list[Any]]:
        ours, theirs = socket.socketpair()
        # `settimeout(0.0)`, which the standard library documents as
        # `setblocking(False)`, a call FBT003 refuses a positional flag in
        ours.settimeout(0.0)
        theirs.settimeout(0.0)
        loop = asyncio.get_running_loop()
        messages: list[Any] = []
        manager = SimpleNamespace(
            auth=auth,
            logger=SimpleNamespace(warning=lambda *args: warnings.append(args)),
            messages=messages,
            connections={0: None},
        )
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)
        userpass = base64.b64encode(b"pytest:pytest")
        for index, payload in enumerate(requests):
            body = json.dumps(payload).encode()
            head = b"POST / HTTP/1.1\r\nHost: x\r\nAuthorization: Basic " + userpass
            if index == len(requests) - 1:
                head += b"\r\nConnection: close"
            head += b"\r\nContent-Length: %d\r\n\r\n" % len(body)
            await loop.sock_sendall(theirs, head + body)
        queued = len(messages)
        task = asyncio.ensure_future(conn.run())
        reply = b""
        async with asyncio.timeout(5):
            while True:
                if len(messages) > queued:
                    queued = len(messages)
                    conn.send(HttpReply(OK, {"result": None}))
                try:
                    data = await asyncio.wait_for(loop.sock_recv(theirs, 65536), 0.05)
                except TimeoutError:
                    continue
                if not data:
                    break
                reply += data
        await task
        theirs.close()
        return reply, messages

    reply, messages = asyncio.run(main())
    return reply, messages, warnings


@pytest.mark.parametrize(
    ("request_", "status", "body"), CORE_ERRORS, ids=CORE_ERROR_IDS
)
def test_what_core_refuses_ahead_of_the_whitelist_is_core_s_reply(
    request_: Any, status: str, body: str
) -> None:
    """`JSONErrorReply`'s status line and JSON body, and nothing queued."""
    reply, messages, warnings = exchange(whitelisted(*PYTEST_ONLY), [request_])
    expected = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
    expected += f"Connection: close\r\nContent-Length: {len(body) + 1}\r\n\r\n"
    assert reply == (expected + body + "\n").encode()
    assert not messages
    assert not warnings


def test_a_403_is_core_s_bare_status_and_is_logged() -> None:
    """`HTTP/1.1 403 Forbidden`, no body, Core's warning, nothing queued."""
    reply, messages, warnings = exchange(
        whitelisted(*PYTEST_ONLY), [{"id": 1, "method": "stop"}]
    )
    expected = b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n"
    assert reply == expected + b"Content-Length: 0\r\n\r\n"
    assert not messages
    assert warnings == [("RPC User %s not allowed to call method %s", "pytest", "stop")]


def test_a_403_keeps_the_connection_for_the_next_request() -> None:
    """As `bitcoind` does: the next request on the socket is read and queued."""
    allowed = {"id": 2, "method": ALLOWED}
    reply, messages, _ = exchange(
        whitelisted(*PYTEST_ONLY), [{"id": 1, "method": "stop"}, allowed]
    )
    assert reply.startswith(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
    assert messages == [(allowed, 0)]


def test_a_batch_the_whitelist_allows_is_queued_whole() -> None:
    """Every entry allowed: queued as it arrived, for `handle_rpc`."""
    batch = [{"id": 1, "method": ALLOWED}, {"id": 2, "method": ALLOWED}]
    _, messages, warnings = exchange(whitelisted(*PYTEST_ONLY), [batch])
    assert messages == [(batch, 0)]
    assert not warnings
