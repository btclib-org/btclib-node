# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The dispatch tables' own keys, and what Connection does with a message.

The framing itself is btclib.p2p.message.Message's and is tested there,
and every payload's own command is btclib.p2p's, this package holding
none of its own. What is this node's is `callbacks` and
`handshake_callbacks`, two hand-written tables of string literals,
checked here against every command a real payload carries; which queue
a command lands in; how much of the buffer survives a partial message;
and what becomes of a peer whose octets do not decode.
"""

import asyncio
import importlib
import inspect
import threading
import time
from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from btclib.exceptions import BTClibValueError
from btclib.p2p.handshake import Verack
from btclib.p2p.keepalive import Ping
from btclib.p2p.limits import PROTOCOL_VERSION
from btclib.p2p.message import Message
from btclib.p2p.negotiation import Mempool
from btclib.p2p.payload import Payload

from btclib_node.chains import RegTest
from btclib_node.constants import P2pConnStatus
from btclib_node.exceptions import WrongNetworkMagicError
from btclib_node.p2p.callbacks import callbacks, handshake_callbacks
from btclib_node.p2p.connection import Connection, PeerStats

if TYPE_CHECKING:
    from btclib.p2p.handshake import Version

    from btclib_node.p2p.manager import P2pManager

MAGIC = RegTest().magic

# where every payload the node speaks is defined: this package holds
# none of its own, `btclib_node.p2p.messages`'s own docstring is why
_BTCLIB_P2P_MODULES = (
    "btclib.p2p.address",
    "btclib.p2p.addrv2",
    "btclib.p2p.block_filters",
    "btclib.p2p.compact_blocks",
    "btclib.p2p.data",
    "btclib.p2p.handshake",
    "btclib.p2p.inventory",
    "btclib.p2p.keepalive",
    "btclib.p2p.negotiation",
    "btclib.p2p.reject",
)


def known_commands() -> set[str]:
    """Every command a `btclib.p2p` payload class carries.

    Imported here, by full dotted name, and deliberately not bound at
    module scope: this file is the test package's `__init__`, so pytest
    importing a sibling module sets it as an attribute of this package,
    and a sibling named after a `btclib.p2p` module would shadow a plain
    `from btclib.p2p import <name>` with the test module.
    """
    found: set[str] = set()
    for dotted in _BTCLIB_P2P_MODULES:
        module = importlib.import_module(dotted)
        found |= {
            obj.command
            for obj in vars(module).values()
            if inspect.isclass(obj)
            and issubclass(obj, Payload)
            and obj is not Payload
            and getattr(obj, "command", None)
        }
    return found


def test_the_dispatch_tables_key_on_real_commands() -> None:
    """Every key `callbacks`/`handshake_callbacks` register is a real command.

    Those two tables are hand-written string literals rather than
    derived from a `Payload` class, so a misspelled key is a handler
    that is registered and never called -- exactly how "sendcmpt" went
    unreached on the way out. This checks each key against every
    command a `btclib.p2p` payload actually carries, this package
    holding none of its own.
    """
    unknown = (set(callbacks) | set(handshake_callbacks)) - known_commands()
    assert not unknown


def make_connection() -> Connection:
    """Just enough state on a `Connection` for `parse_messages` to run.

    Built with `__new__` rather than `Connection()`: the real
    constructor opens a socket, and nothing below exercises anything
    past framing, buffering and dispatch. `_recv_lock` and
    `_recv_resume` are `parse_messages`'s own bookkeeping toward
    `MAX_QUEUED_RECV_BYTES` (btclib-org/btclib-node#462); this
    package's own tests never queue enough to cross it, so they are
    here only because `parse_messages` always touches them, not because
    a test below exercises the bound itself -- `connection_test.py`'s
    own tests do that. `stats` is where `parse_messages` counts each
    message it frames.
    """
    manager = SimpleNamespace(
        node=SimpleNamespace(chain=RegTest()),
        loop=None,
        messages=deque(),
        handshake_messages=deque(),
        peer_db=None,
    )
    conn = Connection.__new__(Connection)
    conn.id = 0
    conn.manager = cast("P2pManager", manager)
    conn.node = manager.node
    conn.buffer = bytearray()
    conn.status = P2pConnStatus.Open
    conn.last_receive = 0
    conn._ping_lock = threading.Lock()
    # a peer past BIP0031_VERSION, which `send_ping` pings with a nonce
    conn.version_message = cast("Version", SimpleNamespace(version=PROTOCOL_VERSION))
    conn.queued_recv_bytes = 0
    conn._recv_lock = threading.Lock()
    conn._recv_resume = asyncio.Event()
    conn._recv_resume.set()
    conn.stats = PeerStats()
    return conn


def framed(payload: Payload, magic: bytes = MAGIC) -> bytes:
    """`payload` serialized whole, header and all, as a peer would send it."""
    return payload.to_message(magic).serialize()


def test_one_message_is_dispatched() -> None:
    """A single, complete message is fully consumed off the buffer.

    Parsed back out of the queue it lands in, its payload comes back
    with the field it was built with, not just the right command.
    """
    conn = make_connection()
    conn.buffer = bytearray(framed(Ping(7)))
    conn.parse_messages()
    assert not conn.buffer
    assert [item[0] for item in conn.manager.messages] == ["ping"]
    assert Ping.parse(conn.manager.messages[0][1]).nonce == 7


def test_a_queued_message_carries_the_time_it_was_read() -> None:
    """The fifth element of the item is the receive time, `last_receive`'s own.

    What `callbacks.pong` measures a round trip to (`P2pManager.messages`).
    """
    conn = make_connection()
    conn.buffer = bytearray(framed(Ping(7)))
    before = time.time()
    conn.parse_messages()
    after = time.time()
    (item,) = conn.manager.messages
    assert before <= item[4] <= after
    assert item[4] == conn.last_receive


def test_several_messages_in_one_read() -> None:
    """One read carrying several messages queues every one of them.

    `ping`/`pong` are pushed to the front of `messages` rather than the
    back, so a `ping` arriving between two other messages still ends up
    ahead of the one that arrived before it.
    """
    conn = make_connection()
    conn.buffer = bytearray(framed(Ping(1)) + framed(Mempool()) + framed(Ping(2)))
    conn.parse_messages()
    assert not conn.buffer
    # ping jumps the queue, mempool does not
    assert [item[0] for item in conn.manager.messages] == ["ping", "ping", "mempool"]


def test_a_handshake_message_goes_to_its_own_queue() -> None:
    """A handshake command lands in `handshake_messages`, not `messages`.

    `verack` is one of `handshake_callbacks`'s own keys, and that
    membership is what routes it -- `messages`, the ordinary queue,
    stays empty.
    """
    conn = make_connection()
    conn.buffer = bytearray(framed(Verack()))
    conn.parse_messages()
    assert [item[0] for item in conn.manager.handshake_messages] == ["verack"]
    assert not conn.manager.messages


def test_a_partial_message_is_held_whole() -> None:
    """A message cut anywhere is held back whole, then completed on arrival.

    Tried at five cut points -- inside the 24-byte header (1, 10, 23),
    exactly at its boundary (24), and inside the payload (`len - 1`) --
    because `parse_messages` rewinds on `IncompleteMessageError`, and a
    rewind that lands wrong would only show up at one of those
    boundaries, not at an arbitrary cut.
    """
    whole = framed(Ping(1))
    # inside the header, at its boundary, and inside the payload
    for cut in (1, 10, 23, 24, len(whole) - 1):
        conn = make_connection()
        conn.buffer = bytearray(whole[:cut])
        conn.parse_messages()
        assert not conn.manager.messages
        assert conn.buffer == whole[:cut], f"cut at {cut}"
        # and it completes once the rest arrives
        conn.buffer += whole[cut:]
        conn.parse_messages()
        assert [item[0] for item in conn.manager.messages] == ["ping"]
        assert not conn.buffer


def test_a_message_fed_one_octet_at_a_time_reassembles_identically() -> None:
    """A message split across as many chunks as it has octets still parses.

    #438: `parse_messages` peeks the header's own `length` field before
    it ever builds a stream, so this drives the read loop the way a
    real socket read would, one octet per call rather than one cut --
    the gate has to survive being asked, and answering "not yet",
    dozens of times running rather than once.
    """
    whole = framed(Ping(424242))
    conn = make_connection()
    for i in range(len(whole)):
        conn.buffer += whole[i : i + 1]
        conn.parse_messages()
    assert not conn.buffer
    assert [item[0] for item in conn.manager.messages] == ["ping"]
    assert Ping.parse(conn.manager.messages[0][1]).nonce == 424242


def test_a_declared_length_short_of_arrived_never_parses_early() -> None:
    """Nothing parses before the last octet a message's own length asks for.

    Distinct from `test_a_partial_message_is_held_whole`'s cut points: a
    message is fed one payload octet at a time after its header, and at
    every single step short of the last, `buffer` must hold exactly
    what has arrived and nothing must be queued -- not only at one
    chosen cut, so a gate that gets the bound wrong by one for some
    lengths but not others cannot pass by luck of the cut chosen.
    """
    whole = framed(Ping(1))  # 24-byte header + 8-byte nonce payload
    conn = make_connection()
    conn.buffer += whole[:24]  # the header, none of the payload
    for i in range(24, len(whole)):
        conn.parse_messages()
        assert conn.buffer == whole[:i]
        assert not conn.manager.messages
        conn.buffer += whole[i : i + 1]
    conn.parse_messages()
    assert not conn.buffer
    assert [item[0] for item in conn.manager.messages] == ["ping"]


def test_a_whole_message_before_a_partial_one_is_still_taken() -> None:
    """The first of two messages in one read is queued despite the second.

    Stopping to rewind on the trailing partial message must not also
    undo the complete one already parsed ahead of it.
    """
    conn = make_connection()
    second = framed(Ping(2))
    conn.buffer = bytearray(framed(Ping(1)) + second[:8])
    conn.parse_messages()
    assert [item[0] for item in conn.manager.messages] == ["ping"]
    assert conn.buffer == second[:8]


def test_a_bad_checksum_drops_the_message_and_keeps_reading() -> None:
    """ISS 1130: a tampered checksum drops that message, and the next arrives.

    Core's `GetReceivedMessage` rejects the message and `ReceiveMsgBytes`
    counts it under `*other*` and goes on. The message after it is
    queued, so the buffer was moved past the rejected one rather than
    retried or abandoned.
    """
    conn = make_connection()
    tampered = bytearray(framed(Ping(1)))
    tampered[20] ^= 0xFF  # a checksum byte
    conn.buffer = tampered + framed(Ping(2))
    conn.parse_messages()
    assert [Ping.parse(item[1]).nonce for item in conn.manager.messages] == [2]
    assert conn.stats.bytes_recv_per_msg == {
        "*other*": len(tampered),
        "ping": len(framed(Ping(2))),
    }
    assert not conn.buffer


def a_message_with_an_invalid_command(payload: bytes) -> bytes:
    """Build a message whose command has an octet after its NUL padding."""
    good = Message(MAGIC, "ping", payload).serialize()
    return good[:15] + b"x" + good[16:]


def test_an_invalid_command_drops_the_message_and_keeps_reading() -> None:
    """ISS 1130: a command `IsMessageTypeValid` refuses drops that message only.

    btclib refuses the command before it reads the payload; the payload
    is skipped all the same, as Core's `GetReceivedMessage` rejects the
    whole message.
    """
    conn = make_connection()
    rejected = a_message_with_an_invalid_command(b"\x00" * 8)
    conn.buffer = bytearray(framed(Ping(1)) + rejected + framed(Ping(2)))
    conn.parse_messages()
    # a `ping` goes to the front of the queue, so the order is not asked
    assert sorted(Ping.parse(item[1]).nonce for item in conn.manager.messages) == [1, 2]
    assert conn.stats.bytes_recv_per_msg["*other*"] == len(rejected)
    assert not conn.buffer


def test_an_invalid_command_waits_for_its_payload() -> None:
    """A refused command whose payload is not all in waits for the rest.

    Core rejects a message once it is whole, so the octets still on their
    way are not read as the next message's header.
    """
    conn = make_connection()
    rejected = a_message_with_an_invalid_command(b"\x00" * 8)
    first = framed(Ping(1))
    conn.buffer = bytearray(first + rejected[:-3])
    conn.parse_messages()
    assert len(conn.manager.messages) == 1
    assert conn.buffer == rejected[:-3]
    conn.buffer += rejected[-3:]
    conn.parse_messages()
    assert conn.stats.bytes_recv_per_msg["*other*"] == len(rejected)
    assert not conn.buffer


def test_a_message_for_another_network_is_refused() -> None:
    """A message stamped with mainnet's magic is refused on regtest.

    `parse_messages` compares the message's own magic against
    `self.node.chain.magic`, so a peer on the wrong network is caught
    at that check rather than by a command it happens not to recognise.
    """
    conn = make_connection()
    conn.buffer = bytearray(framed(Ping(1), magic=bytes.fromhex("f9beb4d9")))  # mainnet
    with pytest.raises(BTClibValueError):
        conn.parse_messages()
    assert not conn.manager.messages


def test_another_network_s_magic_is_refused_off_the_header() -> None:
    """The magic is checked once the header is in, as Core's `readHeader` does.

    A whole `ping` first, then only the header of one for mainnet: the
    refusal does not wait for a payload, nor for a checksum.
    """
    conn = make_connection()
    mainnet = framed(Ping(2), magic=bytes.fromhex("f9beb4d9"))
    conn.buffer = bytearray(framed(Ping(1)) + mainnet[:24])
    with pytest.raises(WrongNetworkMagicError):
        conn.parse_messages()
    assert len(conn.manager.messages) == 1


def test_another_network_s_magic_first_in_the_buffer_is_refused_off_the_header() -> (
    None
):
    """A wrong magic heading the buffer is refused before its payload arrives.

    Only the header of a mainnet `ping` is in: its declared payload never
    comes, and `parse_messages` does not wait for it.
    """
    conn = make_connection()
    conn.buffer = bytearray(framed(Ping(2), magic=bytes.fromhex("f9beb4d9"))[:24])
    with pytest.raises(WrongNetworkMagicError):
        conn.parse_messages()
    assert not conn.manager.messages


def test_an_oversized_payload_is_refused_before_it_is_allocated() -> None:
    """A header claiming an implausible payload length is refused up front.

    The length field is forged past `MAX_PROTOCOL_MESSAGE_LENGTH`, with
    no payload behind it: `Message.parse` checks the field against that
    bound before it reads the payload, so nothing here ever allocates a
    buffer sized by whatever a peer chose to put in the header.
    """
    conn = make_connection()
    header = Message(MAGIC, "ping", b"").serialize()[:24]
    # rewrite the length field with something no peer would honour
    forged = header[:16] + (0xFFFFFFF0).to_bytes(4, "little") + header[20:]
    conn.buffer = bytearray(forged)
    with pytest.raises(BTClibValueError):
        conn.parse_messages()
    assert not conn.manager.messages


def test_a_drawn_ping_nonce_is_never_the_sentinel() -> None:
    """`send_ping`'s nonce is nonzero, varies, and spans the whole field.

    Zero is `ping_nonce`'s own sentinel for "no ping outstanding", so a
    ping carrying it would make its `pong` indistinguishable from none
    arriving at all -- checked here and nowhere else, though the
    functional ping test depends on it and would otherwise fail only
    intermittently. Fifty draws all landing under `2**48` has
    probability about `2**-800`, so requiring one draw above it is a
    check on the width of the draw, not a coincidence a real 64-bit
    draw could plausibly fail.
    """
    # btclib's Ping defaults its nonce to zero, and zero is what
    # ping_nonce means "no ping outstanding": a ping carrying it makes
    # the pong that answers it indistinguishable from no pong at all.
    # Nothing else in the suite says so -- the functional ping test
    # depends on it and would come back as an intermittent red.
    conn = make_connection()
    sent: list[Ping] = []
    conn.send = sent.append  # type: ignore[method-assign,assignment]
    for _ in range(50):
        conn.send_ping()
        # what the pong is matched against is the nonce that went out
        assert conn.ping_nonce == sent[-1].nonce
    for ping in sent:
        assert 0 < ping.nonce < 2**64
    # drawn, not a constant
    assert len({ping.nonce for ping in sent}) > 1
    # and drawn over the whole field, which is the other half of #11:
    # a 48-bit draw satisfies everything above. Fifty draws all landing
    # under 2**48 has probability about 2**-800.
    assert max(ping.nonce for ping in sent) > 2**48
