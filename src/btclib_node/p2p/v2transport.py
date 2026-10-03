# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""BIP324's v2 transport as a state machine that reads and writes no socket.

`V2Transport` is Core's `V2Transport` (`src/net.h` and `src/net.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag), with Core's receive and send
states, implementing `p2p.transport.Transport`, whose conventions it
follows. The cipher, the key schedule and the short message ids are
`btclib.p2p.bip324`'s.

A responder that receives the first 16 bytes of a v1 `version` message
falls back to the `v1_fallback` it was given, which then takes over every
call.

`V2Transport` holds a lock for each half, taken receive before send, as
Core's does: the receiving half's key processing writes the sending
half's buffer and state.
"""

from __future__ import annotations

import secrets
from enum import Enum, auto
from threading import Lock
from typing import override

from btclib.exceptions import BTClibValueError, IncompleteMessageError
from btclib.p2p.bip324 import (
    EXPANSION,
    GARBAGE_TERMINATOR_LEN,
    LENGTH_LEN,
    Cipher,
    contents_from_message,
    message_from_contents,
)
from btclib.p2p.limits import MAX_PROTOCOL_MESSAGE_LENGTH
from btclib_ecc.curves import secp256k1
from btclib_ecc.ecc import ellswift

from btclib_node.exceptions import RejectedMessageError
from btclib_node.p2p.transport import (
    BytesToSend,
    NetMessage,
    SerializedMessage,
    Transport,
    TransportInfo,
    TransportProtocolType,
)

__all__ = [
    "MAX_GARBAGE_LEN",
    "RecvState",
    "SendState",
    "V2Transport",
    "V2TransportError",
]

# Core's `V2Transport::MAX_GARBAGE_LEN`
MAX_GARBAGE_LEN = 4095
# the length of an ElligatorSwift public key
_KEY_LEN = 64
# Core's `V1_PREFIX_LEN`: the magic and `version` and its padding
_V1_PREFIX_LEN = 16
# `CMessageHeader::HEADER_SIZE`: sending this much to a v1 peer is enough
# for it to disconnect us
_V1_HEADER_LEN = 24
# the 12 bytes of a v1 `version` message's command, after the magic
_V1_VERSION_COMMAND = b"version\x00\x00\x00\x00\x00"
# Core's `MAX_CONTENTS_LEN` for a packet: the long type's 0x00 and its
# 12 bytes, and the largest payload
_MAX_CONTENTS_LEN = 1 + 12 + MAX_PROTOCOL_MESSAGE_LENGTH


class V2TransportError(BTClibValueError):
    """Bytes received are invalid: the transport cannot be used anymore."""


class RecvState(Enum):
    """What the receive buffer holds, as Core's `V2Transport::RecvState`."""

    KEY_MAYBE_V1 = auto()
    KEY = auto()
    GARB_GARBTERM = auto()
    VERSION = auto()
    APP = auto()
    APP_READY = auto()
    V1 = auto()


class SendState(Enum):
    """What may be sent, as Core's `V2Transport::SendState`."""

    MAYBE_V1 = auto()
    AWAITING_KEY = auto()
    READY = auto()
    V1 = auto()


class V2Transport(Transport):
    """One connection's v2 transport."""

    _cipher: Cipher  # set when the peer's key is in, as the state allows

    def __init__(
        self,
        magic: bytes,
        v1_fallback: Transport,
        *,
        initiating: bool,
        prv_key: int | None = None,
        garbage: bytes | None = None,
    ) -> None:
        """Start a transport on the network whose message start is `magic`.

        `v1_fallback` takes over when a responder is spoken to in v1.
        `prv_key` and `garbage` are random unless given, which only tests
        do: `garbage` is at most `MAX_GARBAGE_LEN` bytes.
        """
        if prv_key is None:
            prv_key = 1 + secrets.randbelow(secp256k1.n - 1)
        if garbage is None:
            garbage = secrets.token_bytes(secrets.randbelow(MAX_GARBAGE_LEN + 1))
        if len(garbage) > MAX_GARBAGE_LEN:
            err_msg = f"garbage too long: {len(garbage)} bytes"
            raise ValueError(err_msg)
        self._magic = magic
        self._initiating = initiating
        self._v1 = v1_fallback
        self._prv_key = prv_key
        self._ell_ours = ellswift.create_var(prv_key)

        self._recv_lock = Lock()
        self._recv_len = 0
        self._recv_buffer = bytearray()
        self._recv_aad = b""
        self._recv_decode_buffer = b""
        self._recv_state = RecvState.KEY if initiating else RecvState.KEY_MAYBE_V1

        # always taken after `_recv_lock`
        self._send_lock = Lock()
        self._send_buffer = b""
        self._send_pos = 0
        self._send_garbage = garbage
        self._send_type = ""
        self._send_state = SendState.AWAITING_KEY if initiating else SendState.MAYBE_V1
        self._sent_v1_header_worth = False
        if initiating:
            with self._send_lock:
                self._start_sending_handshake()

    # receive side

    @override
    def received_message_complete(self) -> bool:
        with self._recv_lock:
            if self._recv_state is RecvState.V1:
                return self._v1.received_message_complete()
            return self._recv_state is RecvState.APP_READY

    @override
    def received_bytes(self, msg_bytes: memoryview) -> memoryview:
        """Take wire octets and return those it did not take.

        It leaves some once a message is complete, and when it has just
        fallen back to v1. Invalid octets raise `V2TransportError`.
        """
        with self._recv_lock:
            if self._recv_state is RecvState.V1:
                return self._v1.received_bytes(msg_bytes)
            taken = 0
            while taken < len(msg_bytes):
                if self._recv_state is RecvState.APP_READY:
                    break
                size = min(len(msg_bytes) - taken, self._max_bytes_to_process())
                self._recv_buffer += msg_bytes[taken : taken + size]
                taken += size
                if self._recv_state is RecvState.KEY_MAYBE_V1:
                    if self._process_maybe_v1_bytes():
                        break
                elif self._recv_state is RecvState.KEY:
                    self._process_key_bytes()
                elif self._recv_state is RecvState.GARB_GARBTERM:
                    self._process_garbage_bytes()
                else:
                    self._process_packet_bytes()
            return msg_bytes[taken:]

    @override
    def get_received_message(self) -> NetMessage:
        """Return the message, and start on the next.

        Raises `IncompleteMessageError` where there is none, and
        `RejectedMessageError` for contents that name no message type.
        """
        with self._recv_lock:
            if self._recv_state is RecvState.V1:
                return self._v1.get_received_message()
            if self._recv_state is not RecvState.APP_READY:
                err_msg = "no whole message yet"
                raise IncompleteMessageError(err_msg, 0)
            contents = self._recv_decode_buffer
            size = len(contents) + EXPANSION
            self._recv_decode_buffer = b""
            self._recv_state = RecvState.APP
            try:
                command, payload = message_from_contents(contents)
            except BTClibValueError as e:
                raise RejectedMessageError(size) from e
            return NetMessage(command, payload, size)

    def _max_bytes_to_process(self) -> int:
        """Return how many received bytes can be processed in one go."""
        state = self._recv_state
        if state is RecvState.KEY_MAYBE_V1:
            # no more than the 16 that tell v1 from v2: they may be handed to
            # the v1 fallback, which takes no more than a 24-byte header
            # before it reads one
            return _V1_PREFIX_LEN - len(self._recv_buffer)
        if state is RecvState.KEY:
            # the key is followed by garbage that only the key exchange
            # locates the end of
            return _KEY_LEN - len(self._recv_buffer)
        if state is RecvState.GARB_GARBTERM:
            # the terminator may start anywhere
            return 1
        # VERSION or APP: the length first, so that no byte of the next
        # packet is processed; then the rest of this packet, the encrypted
        # length staying in the buffer
        if len(self._recv_buffer) < LENGTH_LEN:
            return LENGTH_LEN - len(self._recv_buffer)
        return EXPANSION + self._recv_len - len(self._recv_buffer)

    def _process_maybe_v1_bytes(self) -> bool:
        """Return whether the v1 fallback has taken over."""
        v1_prefix = self._magic + _V1_VERSION_COMMAND
        if not v1_prefix.startswith(self._recv_buffer):
            # not v1: the bytes stay, now taken for the beginning of a key
            self._recv_state = RecvState.KEY
            with self._send_lock:
                self._send_state = SendState.AWAITING_KEY
                self._start_sending_handshake()
        elif len(self._recv_buffer) == _V1_PREFIX_LEN:
            with self._send_lock:
                self._v1.received_bytes(memoryview(bytes(self._recv_buffer)))
                self._recv_state = RecvState.V1
                self._send_state = SendState.V1
                self._recv_buffer = bytearray()
                self._send_buffer = b""
            return True
        # else: not enough to tell yet
        return False

    def _process_key_bytes(self) -> None:
        buffer = self._recv_buffer
        # A responder sent a v1 `version` under another network's magic
        # (under ours it would be in the v1 state): its own key and
        # garbage will make it disconnect, but only this way is it named.
        offset = len(self._magic)
        if (
            not self._initiating
            and len(buffer) >= offset + len(_V1_VERSION_COMMAND)
            and buffer[offset : offset + len(_V1_VERSION_COMMAND)]
            == _V1_VERSION_COMMAND
        ):
            err_msg = f"V1 peer with wrong MessageStart {bytes(buffer[:offset]).hex()}"
            raise V2TransportError(err_msg)
        if len(buffer) < _KEY_LEN:
            return
        self._cipher = Cipher(
            self._prv_key,
            self._ell_ours,
            bytes(buffer),
            self._initiating,
            self._magic,
        )
        self._recv_state = RecvState.GARB_GARBTERM
        self._recv_buffer = bytearray()
        with self._send_lock:
            self._send_state = SendState.READY
            # after the key and the garbage which may still be unsent: the
            # terminator, and the version packet authenticating the garbage
            self._send_buffer += self._cipher.send_garbage_terminator
            self._send_buffer += self._cipher.encrypt(b"", self._send_garbage)
            self._send_garbage = b""

    def _process_garbage_bytes(self) -> None:
        buffer = self._recv_buffer
        if len(buffer) < GARBAGE_TERMINATOR_LEN:
            return
        if buffer[-GARBAGE_TERMINATOR_LEN:] == self._cipher.recv_garbage_terminator:
            # the garbage is authenticated by the first packet
            self._recv_aad = bytes(buffer[:-GARBAGE_TERMINATOR_LEN])
            self._recv_buffer = bytearray()
            self._recv_state = RecvState.VERSION
        elif len(buffer) == MAX_GARBAGE_LEN + GARBAGE_TERMINATOR_LEN:
            err_msg = "missing garbage terminator"
            raise V2TransportError(err_msg)

    def _process_packet_bytes(self) -> None:
        buffer = self._recv_buffer
        if len(buffer) == LENGTH_LEN:
            self._recv_len = self._cipher.decrypt_length(bytes(buffer))
            if self._recv_len > _MAX_CONTENTS_LEN:
                err_msg = f"packet too large ({self._recv_len} bytes)"
                raise V2TransportError(err_msg)
        elif len(buffer) > LENGTH_LEN and len(buffer) == self._recv_len + EXPANSION:
            try:
                contents, ignore = self._cipher.decrypt(
                    bytes(buffer[LENGTH_LEN:]), self._recv_aad
                )
            except BTClibValueError as e:
                err_msg = f"packet decryption failure ({self._recv_len} bytes)"
                raise V2TransportError(err_msg) from e
            # a packet decrypted with the AAD expected: it is not expected again
            self._recv_aad = b""
            if not ignore:
                if self._recv_state is RecvState.VERSION:
                    # its contents are reserved for extensions, and ignored
                    self._recv_state = RecvState.APP
                else:
                    self._recv_state = RecvState.APP_READY
                    self._recv_decode_buffer = contents
            self._recv_buffer = bytearray()
        # else: the length or the packet is not complete

    # send side

    @override
    def set_message_to_send(self, msg: SerializedMessage) -> bool:
        """Set the next message to send, or return False if none can be now.

        One message at a time is held, once the handshake allows the
        cipher, and only when the buffer is empty: queueing is the
        caller's.
        """
        with self._send_lock:
            if self._send_state is SendState.V1:
                return self._v1.set_message_to_send(msg)
            if not (self._send_state is SendState.READY and not self._send_buffer):
                return False
            self._send_buffer = self._cipher.encrypt(
                contents_from_message(msg.command, msg.payload)
            )
            self._send_type = msg.command
            return True

    @override
    def get_bytes_to_send(self, *, have_next_message: bool) -> BytesToSend:
        """Return the octets to send, if more follow, and for which message.

        `more` is true when `have_next_message` and the handshake allows
        the cipher: before that no message could be set.
        """
        with self._send_lock:
            if self._send_state is SendState.V1:
                return self._v1.get_bytes_to_send(have_next_message=have_next_message)
            return BytesToSend(
                memoryview(self._send_buffer)[self._send_pos :],
                have_next_message and self._send_state is SendState.READY,
                self._send_type,
            )

    @override
    def mark_bytes_sent(self, bytes_sent: int) -> None:
        with self._send_lock:
            if self._send_state is SendState.V1:
                self._v1.mark_bytes_sent(bytes_sent)
                return
            self._send_pos += bytes_sent
            if self._send_pos >= _V1_HEADER_LEN:
                self._sent_v1_header_worth = True
            if self._send_pos == len(self._send_buffer):
                self._send_pos = 0
                self._send_buffer = b""

    def _start_sending_handshake(self) -> None:
        """Put our public key and garbage in the send buffer."""
        self._send_buffer = self._ell_ours + self._send_garbage

    # miscellaneous

    @override
    def should_reconnect_v1(self) -> bool:
        """Return whether, the connection having ended, a v1 retry is warranted.

        It is when we initiated, sent enough for a v1 peer to disconnect
        us, and received nothing.
        """
        if not self._initiating:
            return False
        with self._recv_lock:
            if self._recv_state is not RecvState.KEY or self._recv_buffer:
                return False
            with self._send_lock:
                return self._sent_v1_header_worth

    @override
    def get_info(self) -> TransportInfo:
        """Return the protocol, and for v2 the session id.

        Neither is reported before the version packet is received and
        verified, which says the peer very likely holds the same keys.
        """
        with self._recv_lock:
            if self._recv_state is RecvState.V1:
                return self._v1.get_info()
            if self._recv_state in {RecvState.APP, RecvState.APP_READY}:
                return TransportInfo(TransportProtocolType.V2, self._cipher.session_id)
            return TransportInfo(TransportProtocolType.DETECTING)
