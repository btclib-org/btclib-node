# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The command each payload travels under, and what Connection does.

The framing itself is btclib.p2p.message.Message's and is tested there.
What is this node's is the name every payload serializes under, which
queue a command lands in, how much of the buffer survives a partial
message, and what becomes of a peer whose octets do not decode.
"""

import importlib
import inspect
from collections import deque
from types import SimpleNamespace

import pytest
from btclib.exceptions import BTClibValueError
from btclib.p2p.message import Message
from btclib.p2p.payload import Payload

from btclib_node.chains import RegTest
from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.connection import Connection
from btclib_node.p2p.messages.getdata import Mempool
from btclib_node.p2p.messages.handshake import Verack
from btclib_node.p2p.messages.ping import Ping

MAGIC = RegTest().magic

_MESSAGE_MODULES = (
    "address",
    "compact",
    "data",
    "errors",
    "filters",
    "getdata",
    "handshake",
    "ping",
)

# Bitcoin Core's NetMsgType, which is the authority: a command is what a
# peer dispatches on, so a name only this tree agrees with is a message
# nobody answers. Nothing but the value can say a name is right -- a
# misspelling serializes, parses and round-trips exactly as well as the
# real thing, which is how "sendcmpt" and "cmptblock" went to the whole
# network unnoticed.
_COMMANDS = {
    "Addr": "addr",
    "AddrV2": "addrv2",
    "Block": "block",
    "Blocktxn": "blocktxn",
    "Cmpctblock": "cmpctblock",
    "Feefilter": "feefilter",
    "Filteradd": "filteradd",
    "Filterclear": "filterclear",
    "Filterload": "filterload",
    "Getaddr": "getaddr",
    "Getblocks": "getblocks",
    "Getblocktxn": "getblocktxn",
    "Getdata": "getdata",
    "Getheaders": "getheaders",
    "Headers": "headers",
    "Inv": "inv",
    "Mempool": "mempool",
    "Merkleblock": "merkleblock",
    "Notfound": "notfound",
    "Ping": "ping",
    "Pong": "pong",
    "Reject": "reject",
    "Sendaddrv2": "sendaddrv2",
    "Sendcmpct": "sendcmpct",
    "Sendheaders": "sendheaders",
    "Tx": "tx",
    "Verack": "verack",
    "Version": "version",
    "Wtxidrelay": "wtxidrelay",
}


def payload_classes():
    found = {}
    for name in _MESSAGE_MODULES:
        module = importlib.import_module(f"btclib_node.p2p.messages.{name}")
        for attr, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, Payload)
                and obj is not Payload
                and getattr(obj, "command", None)
            ):
                found[attr] = obj
    return found


def test_every_payload_travels_under_core_s_name():
    classes = payload_classes()
    # no payload without an expected name, and no expected name without
    # a payload: a class added here has to be spelled out above
    assert set(classes) == set(_COMMANDS)
    for name, cls in sorted(classes.items()):
        assert cls.command == _COMMANDS[name], name


def test_the_command_reaches_the_wire():
    # the ClassVar is only worth having if it is what gets serialized
    for name, cls in sorted(payload_classes().items()):
        message = Message(MAGIC, cls.command, b"")
        assert Message.parse(message.serialize()).command == _COMMANDS[name], name


def make_connection():
    manager = SimpleNamespace(
        node=SimpleNamespace(chain=RegTest()),
        loop=None,
        messages=deque(),
        handshake_messages=deque(),
        peer_db=None,
    )
    conn = Connection.__new__(Connection)
    conn.id = 0
    conn.manager = manager
    conn.node = manager.node
    conn.buffer = b""
    conn.status = P2pConnStatus.Open
    conn.last_receive = 0
    return conn


def framed(payload, magic=MAGIC):
    return payload.to_message(magic).serialize()


def test_one_message_is_dispatched():
    conn = make_connection()
    conn.buffer = framed(Ping(7))
    conn.parse_messages()
    assert not conn.buffer
    assert [item[0] for item in conn.manager.messages] == ["ping"]
    assert Ping.deserialize(conn.manager.messages[0][1]).nonce == 7


def test_several_messages_in_one_read():
    conn = make_connection()
    conn.buffer = framed(Ping(1)) + framed(Mempool()) + framed(Ping(2))
    conn.parse_messages()
    assert not conn.buffer
    # ping jumps the queue, mempool does not
    assert [item[0] for item in conn.manager.messages] == ["ping", "ping", "mempool"]


def test_a_handshake_message_goes_to_its_own_queue():
    conn = make_connection()
    conn.buffer = framed(Verack())
    conn.parse_messages()
    assert [item[0] for item in conn.manager.handshake_messages] == ["verack"]
    assert not conn.manager.messages


def test_a_partial_message_is_held_whole():
    whole = framed(Ping(1))
    # inside the header, at its boundary, and inside the payload
    for cut in (1, 10, 23, 24, len(whole) - 1):
        conn = make_connection()
        conn.buffer = whole[:cut]
        conn.parse_messages()
        assert not conn.manager.messages
        assert conn.buffer == whole[:cut], f"cut at {cut}"
        # and it completes once the rest arrives
        conn.buffer += whole[cut:]
        conn.parse_messages()
        assert [item[0] for item in conn.manager.messages] == ["ping"]
        assert not conn.buffer


def test_a_whole_message_before_a_partial_one_is_still_taken():
    conn = make_connection()
    second = framed(Ping(2))
    conn.buffer = framed(Ping(1)) + second[:8]
    conn.parse_messages()
    assert [item[0] for item in conn.manager.messages] == ["ping"]
    assert conn.buffer == second[:8]


def test_a_bad_checksum_raises_instead_of_spinning():
    # This used to be an infinite loop: the recovery searched the binary
    # buffer for the magic spelled as ASCII text, never matched, and the
    # while loop tried the same message again forever. Core drops such a
    # peer, and Connection.run turns the raise into exactly that.
    conn = make_connection()
    tampered = bytearray(framed(Ping(1)))
    tampered[20] ^= 0xFF  # a checksum byte
    conn.buffer = bytes(tampered)
    with pytest.raises(BTClibValueError):
        conn.parse_messages()
    assert not conn.manager.messages


def test_a_message_for_another_network_is_refused():
    conn = make_connection()
    conn.buffer = framed(Ping(1), magic=bytes.fromhex("f9beb4d9"))  # mainnet
    with pytest.raises(BTClibValueError):
        conn.parse_messages()
    assert not conn.manager.messages


def test_an_oversized_payload_is_refused_before_it_is_allocated():
    conn = make_connection()
    header = Message(MAGIC, "ping", b"").serialize()[:24]
    # rewrite the length field with something no peer would honour
    forged = header[:16] + (0xFFFFFFF0).to_bytes(4, "little") + header[20:]
    conn.buffer = forged
    with pytest.raises(BTClibValueError):
        conn.parse_messages()
    assert not conn.manager.messages
