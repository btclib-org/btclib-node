# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`V1Transport` and `frame_message_bytes`, what `p2p/transport.py` holds.

Core's own `V1Transport` is exercised through `net_tests.cpp` and its
fuzz target; what is checked here is the same two halves, the receiving
one a chunk at a time and the sending one as Core's `SocketSendData`
drives it.
"""

import pytest
from btclib.exceptions import BTClibValueError, IncompleteMessageError
from btclib.p2p.limits import MAX_PROTOCOL_MESSAGE_LENGTH
from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.exceptions import RejectedMessageError, WrongNetworkMagicError
from btclib_node.p2p.transport import (
    HEADER_SIZE,
    NetMessage,
    SerializedMessage,
    TransportInfo,
    TransportProtocolType,
    V1Transport,
    frame_message_bytes,
)

MAGIC = RegTest().magic


def wire(command: str, payload: bytes = b"", magic: bytes = MAGIC) -> bytes:
    """Return `payload` framed the way btclib frames it, as a peer would."""
    return Message(magic, command, payload).serialize()


def feed_all(transport: V1Transport, data: bytes) -> list[NetMessage]:
    """Return every message `data` completes, as `parse_messages` loops."""
    out: list[NetMessage] = []
    remaining = memoryview(data)
    while remaining:
        remaining = transport.received_bytes(remaining)
        if transport.received_message_complete():
            out.append(transport.get_received_message())
    return out


def drain(transport: V1Transport) -> tuple[bytes, list[tuple[bool, str]]]:
    """Send everything the transport offers, as Core's loop does.

    Returns the octets and, per chunk, `more` and the command.
    """
    out = b""
    chunks: list[tuple[bool, str]] = []
    more = True
    while more:
        to_send, more, command = transport.get_bytes_to_send(have_next_message=False)
        out += to_send
        chunks.append((more, command))
        transport.mark_bytes_sent(len(to_send))
    return out, chunks


def test_a_v1_transport_says_it_is_v1_and_never_reconnects() -> None:
    """Core's `GetInfo` and `ShouldReconnectV1` for a `V1Transport`."""
    transport = V1Transport(MAGIC)
    assert transport.get_info() == TransportInfo(TransportProtocolType.V1, None)
    assert transport.get_info().transport_type == "v1"
    assert not transport.should_reconnect_v1()


def test_a_message_is_complete_once_its_last_octet_is_taken() -> None:
    """Nothing is complete before the last octet, and one octet completes it."""
    transport = V1Transport(MAGIC)
    data = wire("ping", b"\x01" * 8)
    for i in range(len(data) - 1):
        assert not transport.received_bytes(memoryview(data[i : i + 1]))
        assert not transport.received_message_complete()
    transport.received_bytes(memoryview(data[-1:]))
    assert transport.received_message_complete()
    assert transport.get_received_message() == NetMessage(
        "ping", b"\x01" * 8, len(data)
    )
    assert not transport.received_message_complete()


def test_received_bytes_stops_at_the_end_of_the_header_and_of_the_message() -> None:
    """What is not taken is returned, so the caller sees each boundary."""
    transport = V1Transport(MAGIC)
    first = wire("ping", b"\x01" * 8)
    second = wire("verack")
    data = memoryview(first + second)
    rest = transport.received_bytes(data)
    assert bytes(rest) == data[HEADER_SIZE:]
    assert not transport.received_message_complete()
    rest = transport.received_bytes(rest)
    assert bytes(rest) == second
    assert transport.received_message_complete()
    transport.get_received_message()
    assert bytes(transport.received_bytes(rest)) == b""
    assert transport.received_message_complete()


def test_a_message_with_no_payload_is_complete_at_its_header() -> None:
    """Core's `CompleteInternal`: a zero length is reached by the header."""
    transport = V1Transport(MAGIC)
    (message,) = feed_all(transport, wire("verack"))
    assert message == NetMessage("verack", b"", HEADER_SIZE)


def test_several_messages_in_one_chunk_come_out_in_order() -> None:
    """A chunk holding several messages is taken by the caller's loop."""
    transport = V1Transport(MAGIC)
    messages = feed_all(
        transport,
        wire("ping", b"\x01" * 8) + wire("verack") + wire("pong", b"\x02" * 8),
    )
    assert [m.command for m in messages] == ["ping", "verack", "pong"]


def test_another_networks_magic_is_refused_at_the_header() -> None:
    """The refusal comes with the header, before any payload is asked for."""
    transport = V1Transport(MAGIC)
    other = bytes.fromhex("f9beb4d9")
    data = wire("ping", b"\x01" * 8, magic=other)
    with pytest.raises(WrongNetworkMagicError):
        transport.received_bytes(memoryview(data[:HEADER_SIZE]))


@pytest.mark.parametrize(
    ("length", "refused"),
    [(MAX_PROTOCOL_MESSAGE_LENGTH, False), (MAX_PROTOCOL_MESSAGE_LENGTH + 1, True)],
)
def test_a_length_past_the_bound_is_refused_at_the_header(
    *, length: int, refused: bool
) -> None:
    """Core's `readHeader` bounds the length, before any payload is asked."""
    header = wire("ping")[:16] + length.to_bytes(4, "little") + b"\0" * 4
    transport = V1Transport(MAGIC)
    if refused:
        with pytest.raises(BTClibValueError):
            transport.received_bytes(memoryview(header))
    else:
        transport.received_bytes(memoryview(header))
        assert not transport.received_message_complete()


def test_a_bad_checksum_is_rejected_and_the_next_message_is_taken() -> None:
    """Core's `reject_message`: the whole message is dropped, size reported."""
    transport = V1Transport(MAGIC)
    tampered = bytearray(wire("ping", b"\x01" * 8))
    tampered[20] ^= 0xFF  # a checksum octet
    data = memoryview(bytes(tampered) + wire("verack"))
    data = transport.received_bytes(data)
    data = transport.received_bytes(data)
    with pytest.raises(RejectedMessageError) as raised:
        transport.get_received_message()
    assert raised.value.size == len(tampered)
    assert [m.command for m in feed_all(transport, bytes(data))] == ["verack"]


def test_an_invalid_command_is_rejected_when_its_payload_is_in() -> None:
    """A command `IsMessageTypeValid` refuses is rejected once whole."""
    good = wire("ping", b"\x01" * 8)
    bad = good[:15] + b"x" + good[16:]  # an octet after the padding's NUL
    transport = V1Transport(MAGIC)
    data = transport.received_bytes(memoryview(bad[:-1]))
    data = transport.received_bytes(data)
    assert not transport.received_message_complete()
    transport.received_bytes(memoryview(bad[-1:]))
    with pytest.raises(RejectedMessageError):
        transport.get_received_message()


def test_a_message_not_yet_whole_cannot_be_taken() -> None:
    """`IncompleteMessageError` says how many octets are still wanted."""
    transport = V1Transport(MAGIC)
    data = wire("ping", b"\x01" * 8)
    with pytest.raises(IncompleteMessageError) as raised:
        transport.get_received_message()
    assert raised.value.missing == HEADER_SIZE
    transport.received_bytes(memoryview(data[:HEADER_SIZE]))
    with pytest.raises(IncompleteMessageError) as raised:
        transport.get_received_message()
    assert raised.value.missing == 8


@pytest.mark.parametrize(
    "payload",
    [b"", b"\x01" * 8, b"\x02" * 100_000],
    ids=["empty", "small", "large"],
)
def test_the_octets_sent_are_the_octets_btclib_serializes(payload: bytes) -> None:
    """A header then the payload, byte for byte `Message.serialize`."""
    transport = V1Transport(MAGIC)
    assert transport.set_message_to_send(SerializedMessage("ping", payload))
    sent, chunks = drain(transport)
    assert sent == wire("ping", payload)
    # more follows the header only where there is a payload
    assert chunks == (
        [(True, "ping"), (False, "ping")] if payload else [(False, "ping")]
    )


def test_more_follows_the_last_chunk_where_another_message_is_ready() -> None:
    """`have_next_message` changes `more` after the payload and nothing else."""
    transport = V1Transport(MAGIC)
    transport.set_message_to_send(SerializedMessage("ping", b"\x01" * 8))
    header, more, _ = transport.get_bytes_to_send(have_next_message=True)
    assert more
    transport.mark_bytes_sent(len(header))
    payload, more, _ = transport.get_bytes_to_send(have_next_message=True)
    assert more
    assert bytes(payload) == b"\x01" * 8
    payload, more, _ = transport.get_bytes_to_send(have_next_message=False)
    assert not more


def test_a_message_sent_in_pieces_is_the_same_octets() -> None:
    """`mark_bytes_sent` may report part of a chunk, and the rest is offered."""
    transport = V1Transport(MAGIC)
    transport.set_message_to_send(SerializedMessage("ping", b"\x01" * 8))
    expected = wire("ping", b"\x01" * 8)
    out = b""
    while to_send := transport.get_bytes_to_send(have_next_message=False).to_send:
        out += to_send[:5]
        assert len(out) <= len(expected)
        transport.mark_bytes_sent(len(to_send[:5]))
    assert out == expected


def test_a_message_is_not_taken_until_the_last_is_sent() -> None:
    """Core's `SetMessageToSend`: busy until the header and payload are out."""
    transport = V1Transport(MAGIC)
    first = SerializedMessage("ping", b"\x01" * 8)
    assert transport.set_message_to_send(first)
    assert not transport.set_message_to_send(SerializedMessage("pong", b""))
    to_send, _, _ = transport.get_bytes_to_send(have_next_message=False)
    transport.mark_bytes_sent(len(to_send))
    assert not transport.set_message_to_send(SerializedMessage("pong", b""))
    to_send, _, _ = transport.get_bytes_to_send(have_next_message=False)
    transport.mark_bytes_sent(len(to_send))
    assert transport.set_message_to_send(SerializedMessage("pong", b""))


def test_a_message_it_cannot_frame_is_refused_and_changes_nothing() -> None:
    """A command past twelve octets raises, and the next message is taken."""
    transport = V1Transport(MAGIC)
    with pytest.raises(BTClibValueError):
        transport.set_message_to_send(SerializedMessage("a" * 13, b""))
    assert transport.set_message_to_send(SerializedMessage("ping", b"\x01" * 8))
    assert drain(transport)[0] == wire("ping", b"\x01" * 8)


def test_the_payload_is_not_held_once_sent() -> None:
    """Core's `ClearShrink`: a block does not outlive its sending."""
    transport = V1Transport(MAGIC)
    transport.set_message_to_send(SerializedMessage("block", b"\x01" * 1000))
    drain(transport)
    to_send, _, _ = transport.get_bytes_to_send(have_next_message=False)
    assert not to_send


def test_frame_message_bytes_returns_the_first_message() -> None:
    """The fuzz entry point frames one message and leaves the rest unread."""
    data = wire("ping", b"\x01" * 8)
    assert frame_message_bytes(data + b"trailing") == Message(
        MAGIC, "ping", b"\x01" * 8
    )
    assert frame_message_bytes(data).serialize() == data


@pytest.mark.parametrize("cut", [0, 5, HEADER_SIZE, HEADER_SIZE + 7])
def test_frame_message_bytes_refuses_octets_ending_inside_a_message(cut: int) -> None:
    """Whatever the cut, short octets are `IncompleteMessageError`."""
    with pytest.raises(IncompleteMessageError):
        frame_message_bytes(wire("ping", b"\x01" * 8)[:cut])
