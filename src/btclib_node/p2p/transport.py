# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""How a connection's octets become messages and back, as Core's `Transport`.

`Transport` is Core's class of that name (`src/net.h`) and `V1Transport`
is its `V1Transport` (`src/net.cpp`), both read at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag, with the names in snake case.
A transport does no I/O: `Connection` feeds it what the socket read and
writes what it hands back. `V1Transport` holds no lock;
`p2p.v2transport`'s docstring says why `V2Transport` holds two. A BIP324
transport implements the same methods.

**Threads.** The receiving half is touched on the connection's own loop
alone, by `Connection.parse_messages`. The sending half is touched on
that loop too, under `Connection._write_lock`, which is what makes the
order of the octets on the wire the order the transport produced them
in. `get_info` and `should_reconnect_v1` read what the other methods
write and may be called from another thread, `V1Transport`'s answers
being constants.

**Where this differs from Core's shape**, each for a Python reason:

- `received_bytes` takes a `memoryview` and returns what it left, where
  Core chops the consumed octets off a span in place. It raises where
  Core returns `false`, so the refusal carries its reason.
- `get_received_message` raises `RejectedMessageError` where Core sets
  `reject_message`, and takes no time: the caller reads the clock.
  Either way the transport can go on.
- `get_bytes_to_send` returns a `memoryview` of the octets the transport
  holds, as Core returns a span, so a block is not copied to be sent.
- There is no `GetSendMemoryUsage`: `Connection.queued_send_bytes` is
  what bounds the send buffer, and counts a message before it reaches
  the transport.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple, override

from btclib.exceptions import BTClibValueError, IncompleteMessageError
from btclib.p2p.limits import MAX_PROTOCOL_MESSAGE_LENGTH
from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.exceptions import RejectedMessageError, WrongNetworkMagicError

__all__ = [
    "HEADER_SIZE",
    "BytesToSend",
    "NetMessage",
    "SerializedMessage",
    "Transport",
    "TransportInfo",
    "TransportProtocolType",
    "V1Transport",
    "frame_message_bytes",
]

# `CMessageHeader`'s layout (`src/protocol.h`), argued in
# `btclib.p2p.message`'s module docstring: magic (4 octets), command
# (12), a little-endian length (4) and a checksum (4). That module keeps
# its own constants private, so what is needed here is repeated.
HEADER_SIZE = 24
_MAGIC_SIZE = 4
_COMMAND_SIZE = 12
_LENGTH_AT = _MAGIC_SIZE + _COMMAND_SIZE
_LENGTH_SIZE = 4


class TransportProtocolType(StrEnum):
    """Core's `TransportProtocolType`, valued as `getpeerinfo` spells it."""

    DETECTING = "detecting"
    V1 = "v1"
    V2 = "v2"


@dataclass(frozen=True, slots=True)
class TransportInfo:
    """Core's `Transport::Info`: the protocol, and the BIP324 session id."""

    transport_type: TransportProtocolType
    session_id: bytes | None = None


@dataclass(frozen=True, slots=True)
class NetMessage:
    """Core's `CNetMessage`: a message taken off the wire.

    `size` is Core's `m_raw_message_size`, every octet the message took
    on the wire.
    """

    command: str
    payload: bytes
    size: int


@dataclass(frozen=True, slots=True)
class SerializedMessage:
    """Core's `CSerializedNetMsg`: a message to send, framing not yet added."""

    command: str
    payload: bytes


class BytesToSend(NamedTuple):
    """What `Transport.get_bytes_to_send` returns.

    `more` is whether more octets follow these. `command` is the message
    they are sent on behalf of, `""` for octets that belong to none.
    """

    to_send: memoryview
    more: bool
    command: str


class Transport(ABC):
    """Core's `Transport`: bytes in, messages out, and the other way round."""

    @abstractmethod
    def get_info(self) -> TransportInfo:
        """Return the protocol this transport speaks."""

    @abstractmethod
    def received_message_complete(self) -> bool:
        """Return whether `get_received_message` can be called."""

    @abstractmethod
    def received_bytes(self, msg_bytes: memoryview) -> memoryview:
        """Take wire octets and return those it did not take.

        The caller loops until none are left, fetching each message as
        `received_message_complete` says it is whole, and does not call
        this while a whole one waits to be fetched.

        Raises where some octets are invalid, after which the transport
        is no longer to be used: the connection is dropped.
        """

    @abstractmethod
    def get_received_message(self) -> NetMessage:
        """Return the whole message `received_message_complete` announced.

        Raises `IncompleteMessageError` where there is none, and
        `RejectedMessageError`, whose `size` is the octets it took, for
        one that is invalid and is dropped without dropping its sender.
        """

    @abstractmethod
    def set_message_to_send(self, msg: SerializedMessage) -> bool:
        """Set the next message to send.

        Returns `False`, and takes nothing, where the previous one is not
        yet sent or the transport cannot send yet. Raises
        `BTClibValueError` for a message it cannot frame.
        """

    @abstractmethod
    def get_bytes_to_send(self, *, have_next_message: bool) -> BytesToSend:
        """Return the octets to send, if more follow, and for which message.

        Changes nothing, so it may be called again. `have_next_message`
        is the caller saying another message is ready to be set, and
        changes `more` alone.
        """

    @abstractmethod
    def mark_bytes_sent(self, bytes_sent: int) -> None:
        """Report how many octets `get_bytes_to_send` offered have been sent."""

    @abstractmethod
    def should_reconnect_v1(self) -> bool:
        """Return whether, once dropped, a reconnection with v1 is warranted."""


class V1Transport(Transport):
    """Core's `V1Transport`: the 24-octet header, then the payload."""

    def __init__(self, magic: bytes) -> None:
        """Start on a message boundary, on the network `magic` names."""
        self._magic = magic
        # The message being received, header then payload, and its
        # payload's length once the header is whole. Core keeps header
        # and payload apart; one buffer is the same octets.
        self._buffer = bytearray()
        self._length: int | None = None
        # The message being sent, as Core's own four fields.
        self._header_to_send = b""
        self._message_to_send = SerializedMessage("", b"")
        self._sending_header = False
        self._bytes_sent = 0

    @override
    def get_info(self) -> TransportInfo:
        return TransportInfo(TransportProtocolType.V1)

    def _wanted(self) -> int:
        """Return how many octets the message being received still lacks."""
        total = HEADER_SIZE if self._length is None else HEADER_SIZE + self._length
        return total - len(self._buffer)

    @override
    def received_message_complete(self) -> bool:
        return self._length is not None and not self._wanted()

    @override
    def received_bytes(self, msg_bytes: memoryview) -> memoryview:
        """Take the octets the current header or payload still wants.

        Core's `readHeader` and `readData`, which also stop at the end of
        the header, so the caller's loop sees the header refused before
        any payload is taken.
        """
        taken = min(self._wanted(), len(msg_bytes))
        self._buffer += msg_bytes[:taken]
        if self._length is None and len(self._buffer) == HEADER_SIZE:
            self._read_header()
        return msg_bytes[taken:]

    def _read_header(self) -> None:
        """Refuse a header Core's `readHeader` refuses, else take its length.

        Another network's magic, and a length past
        `MAX_PROTOCOL_MESSAGE_LENGTH`, which keeps a peer from having a
        buffer sized by what it wrote (the disclosure Core's comment there
        cites, bitcoincore.org/en/2024/07/03/disclose_receive_buffer_oom).
        """
        magic = bytes(self._buffer[:_MAGIC_SIZE])
        if magic != self._magic:
            raise WrongNetworkMagicError(magic)
        length = int.from_bytes(
            self._buffer[_LENGTH_AT : _LENGTH_AT + _LENGTH_SIZE], "little"
        )
        if length > MAX_PROTOCOL_MESSAGE_LENGTH:
            err_msg = f"invalid payload length: {length}"
            err_msg += f" instead of at most {MAX_PROTOCOL_MESSAGE_LENGTH} bytes"
            raise BTClibValueError(err_msg)
        self._length = length

    @override
    def get_received_message(self) -> NetMessage:
        """Return the message, and start on the next.

        A checksum that does not match the payload, or a command
        `IsMessageTypeValid` refuses, is Core's `reject_message`.
        """
        wanted = self._wanted()
        if self._length is None or wanted:
            err_msg = "no whole message yet"
            raise IncompleteMessageError(err_msg, wanted)
        raw = bytes(self._buffer)
        self._buffer.clear()
        self._length = None
        try:
            message = Message.parse(raw)
        except BTClibValueError as e:
            raise RejectedMessageError(len(raw)) from e
        return NetMessage(message.command, message.payload, len(raw))

    @override
    def set_message_to_send(self, msg: SerializedMessage) -> bool:
        if self._sending_header or self._bytes_sent < len(
            self._message_to_send.payload
        ):
            return False
        # built to be refused, not to be sent: its `serialize` would copy
        # the payload behind the header, which is sent as it is
        message = Message(self._magic, msg.command, msg.payload)
        self._header_to_send = (
            self._magic
            + msg.command.encode("ascii").ljust(_COMMAND_SIZE, b"\x00")
            + len(msg.payload).to_bytes(_LENGTH_SIZE, "little")
            + message.checksum
        )
        self._message_to_send = msg
        self._sending_header = True
        self._bytes_sent = 0
        return True

    @override
    def get_bytes_to_send(self, *, have_next_message: bool) -> BytesToSend:
        message = self._message_to_send
        if self._sending_header:
            # more follows the header if the payload is not empty
            return BytesToSend(
                memoryview(self._header_to_send)[self._bytes_sent :],
                have_next_message or bool(message.payload),
                message.command,
            )
        return BytesToSend(
            memoryview(message.payload)[self._bytes_sent :],
            have_next_message,
            message.command,
        )

    @override
    def mark_bytes_sent(self, bytes_sent: int) -> None:
        self._bytes_sent += bytes_sent
        if self._sending_header and self._bytes_sent == len(self._header_to_send):
            self._sending_header = False
            self._bytes_sent = 0
        elif not self._sending_header and self._bytes_sent == len(
            self._message_to_send.payload
        ):
            # Core's `ClearShrink`: a block is not held once sent
            self._message_to_send = SerializedMessage(
                self._message_to_send.command, b""
            )
            self._bytes_sent = 0

    @override
    def should_reconnect_v1(self) -> bool:
        return False


def frame_message_bytes(data: bytes) -> Message:
    """Return the one-argument, octet-only shape `fuzz/fuzz_framing.py` drives.

    The first message `data` holds, framed by a `V1Transport` built with
    no socket: the shape of Core's `p2p_transport_serialization.cpp` fuzz
    target (at bitcoin/bitcoin@ca7162cde5), which feeds raw octets to a
    `V1Transport` constructed with no wider node context.

    `RegTest`'s own magic, this tree's cheapest chain to construct, rather
    than a magic threaded through the entry point: the magic check is
    worth fuzzing on its own footing rather than fixed away.

    Trailing octets are left unread, as `Connection.parse_messages` leaves
    them for the next message, so a seed exercised by
    `tests/fuzz_corpus_test.py`'s round-trip check
    (`frame_message_bytes(seed).serialize() == seed`) must carry none.
    Raises `IncompleteMessageError` for octets that end inside a message.
    """
    magic = RegTest().magic
    transport = V1Transport(magic)
    remaining = memoryview(data)
    while remaining and not transport.received_message_complete():
        remaining = transport.received_bytes(remaining)
    message = transport.get_received_message()
    return Message(magic, message.command, message.payload)
