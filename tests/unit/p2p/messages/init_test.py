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
import threading
from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from btclib.exceptions import BTClibValueError
from btclib.p2p.handshake import Verack
from btclib.p2p.keepalive import Ping
from btclib.p2p.message import Message
from btclib.p2p.negotiation import Mempool
from btclib.p2p.payload import Payload

from btclib_node.chains import RegTest
from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.callbacks import callbacks, handshake_callbacks
from btclib_node.p2p.connection import Connection

if TYPE_CHECKING:
    from btclib_node.p2p.manager import P2pManager

MAGIC = RegTest().magic

# what this package defines: everything else the node speaks is
# btclib.p2p's, and named there
_MESSAGE_MODULES = ("errors",)

# where the rest of what the node speaks is defined
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
)

# the spelling the specification gives, which is the authority: a
# command is what a peer dispatches on, so a name only this tree agrees
# with is a message nobody answers. Nothing but the value can say a name
# is right -- a misspelling serializes, parses and round-trips exactly as
# well as the real thing. BIP61 introduces the message as "reject", and
# Bitcoin Core's NetMsgType has no entry for it, so the BIP is where the
# spelling is read rather than Core's header.
_COMMANDS = {
    "Reject": "reject",
}


def payload_classes() -> dict[str, type[Payload]]:
    """Every `Payload` subclass this package defines, keyed by name."""
    found: dict[str, type[Payload]] = {}
    for name in _MESSAGE_MODULES:
        module = importlib.import_module(f"btclib_node.p2p.messages.{name}")
        found.update(
            {
                attr: obj
                for attr, obj in vars(module).items()
                if inspect.isclass(obj)
                and issubclass(obj, Payload)
                and obj is not Payload
                and getattr(obj, "command", None)
            }
        )
    return found


def test_every_payload_travels_under_its_specification_s_name() -> None:
    """Every payload of this package carries the command its own spec gives.

    `_COMMANDS` is read off BIP61, not off this tree's own choice, and
    the check is two-way: every payload class has an entry here, and
    every entry names a class that exists, so an addition on either
    side that forgets the other is caught rather than silently unpaired.
    """
    classes = payload_classes()
    # no payload without an expected name, and no expected name without
    # a payload: a class added here has to be spelled out above
    assert set(classes) == set(_COMMANDS)
    for name, cls in sorted(classes.items()):
        assert cls.command == _COMMANDS[name], name


def test_the_dispatch_tables_key_on_real_commands() -> None:
    """Every key `callbacks`/`handshake_callbacks` register is a real command.

    Those two tables are hand-written string literals rather than
    derived from a `Payload` class, so a misspelled key is a handler
    that is registered and never called -- exactly how "sendcmpt" went
    unreached on the way out. This checks each key against every
    command a payload of this package or of `btclib.p2p` actually
    carries.
    """
    # The outbound side reads the command off the class; the inbound
    # side is still two hand-written tables of string literals, so a
    # misspelling there is an entry no message ever matches -- silent,
    # exactly as "sendcmpt" was on the way out. Every key has to be a
    # command some payload actually travels under, this package's or
    # btclib.p2p's.
    known = {cls.command for cls in payload_classes().values()}
    # imported here, by full dotted name, and deliberately not bound at
    # module scope: this file is the test package's __init__, so pytest
    # importing a sibling module sets it as an attribute of this package,
    # and a sibling named after a btclib.p2p module would shadow a plain
    # `from btclib.p2p import <name>` with the test module.
    for dotted in _BTCLIB_P2P_MODULES:
        module = importlib.import_module(dotted)
        known |= {
            obj.command
            for obj in vars(module).values()
            if inspect.isclass(obj)
            and issubclass(obj, Payload)
            and obj is not Payload
            and getattr(obj, "command", None)
        }
    unknown = (set(callbacks) | set(handshake_callbacks)) - known
    assert not unknown


def test_the_command_reaches_the_wire() -> None:
    """A payload's `command` is what its wire framing actually carries.

    `Payload.command` is a `ClassVar` read by callers, so it only
    matters if it is also what ends up in the serialized header: this
    round-trips one of each payload through `Message` and checks the
    command it comes back with.
    """
    # the ClassVar is only worth having if it is what gets serialized
    for name, cls in sorted(payload_classes().items()):
        message = Message(MAGIC, cls.command, b"")
        assert Message.parse(message.serialize()).command == _COMMANDS[name], name


def make_connection() -> Connection:
    """Just enough state on a `Connection` for `parse_messages` to run.

    Built with `__new__` rather than `Connection()`: the real
    constructor opens a socket, and nothing below exercises anything
    past framing, buffering and dispatch.
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


def test_a_bad_checksum_raises_instead_of_spinning() -> None:
    """A tampered checksum raises rather than retrying the same bytes forever.

    A regression test: recovering from a bad checksum by searching the
    buffer for the magic spelled as ASCII never matched the binary magic
    actually there, and the `while` loop retried the same unparsable
    message without end. Nothing here reaches that recovery any more --
    the checksum failure raises immediately, and `Connection.run` is
    what drops a peer whose message does this.
    """
    # This used to be an infinite loop: the recovery searched the binary
    # buffer for the magic spelled as ASCII text, never matched, and the
    # while loop tried the same message again forever. Core drops such a
    # peer, and Connection.run turns the raise into exactly that.
    conn = make_connection()
    tampered = bytearray(framed(Ping(1)))
    tampered[20] ^= 0xFF  # a checksum byte
    conn.buffer = tampered
    with pytest.raises(BTClibValueError):
        conn.parse_messages()
    assert not conn.manager.messages


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
