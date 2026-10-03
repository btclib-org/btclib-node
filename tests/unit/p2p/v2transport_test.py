# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`V2Transport`, against the scenarios of Core's `v2transport_test`.

`src/test/net_tests.cpp` at bitcoin/bitcoin@9be056a8a7, the v31.1 tag.
Each test drives the transport with a `Cipher` playing the other side, as
Core's `V2TransportTester` does, since a second `V2Transport` could not
send garbage, decoys or a damaged packet.
"""

import random

import pytest
from btclib.exceptions import IncompleteMessageError
from btclib.p2p.bip324 import (
    EXPANSION,
    GARBAGE_TERMINATOR_LEN,
    LENGTH_LEN,
    MESSAGE_IDS,
    Cipher,
)
from btclib.p2p.message import Message
from btclib_ecc.curves import secp256k1
from btclib_ecc.ecc import ellswift

from btclib_node.chains import Main, RegTest
from btclib_node.exceptions import RejectedMessageError
from btclib_node.p2p.transport import (
    NetMessage,
    SerializedMessage,
    TransportInfo,
    TransportProtocolType,
    V1Transport,
)
from btclib_node.p2p.v2transport import (
    MAX_GARBAGE_LEN,
    RecvState,
    SendState,
    V1PeerRefusedError,
    V2Transport,
    V2TransportError,
)

MAGIC = RegTest().magic
KEY_LEN = 64


def feed(transport: V2Transport, data: bytes) -> int:
    """Hand `data` to the transport, and return how many octets it took."""
    return len(data) - len(transport.received_bytes(memoryview(data)))


def to_send(transport: V2Transport, *, more: bool = False) -> bytes:
    """Return the octets the transport offers to send."""
    return bytes(transport.get_bytes_to_send(have_next_message=more).to_send)


class Peer:
    """The transport under test and the other side, a `Cipher`.

    The other side's bytes are queued with `send_*`, and `interact` runs
    them in, in chunks of random size, and collects the messages and the
    bytes the transport sends.
    """

    def __init__(self, rng: random.Random, *, initiator: bool) -> None:
        """Build both sides."""
        self.rng = rng
        self.fallback = V1Transport(MAGIC)
        self.transport = V2Transport(
            MAGIC,
            self.fallback,
            initiating=initiator,
            prv_key=rng.randrange(1, secp256k1.n),
            garbage=rng.randbytes(rng.randrange(MAX_GARBAGE_LEN + 1)),
        )
        self.initiator = initiator
        self.prv_key = rng.randrange(1, secp256k1.n)
        self.ell = ellswift.create_var(self.prv_key)
        self.cipher: Cipher
        self.sent_garbage = b""
        self.recv_garbage = b""
        self.to_send = bytearray()
        self.received = bytearray()
        self.msg_to_send: list[SerializedMessage] = []
        self.sent_aad = False

    def interact(self) -> list[NetMessage | None] | None:
        """Run the queues in, and return the messages out; None on an error."""
        out: list[NetMessage | None] = []
        while True:
            progress = False
            if self.to_send:
                chunk = bytes(self.to_send[: 1 + self.rng.randrange(len(self.to_send))])
                try:
                    taken = feed(self.transport, chunk)
                except V2TransportError:
                    return None
                if taken:
                    progress = True
                    del self.to_send[:taken]
            if self.transport.received_message_complete() and (
                not progress or self.rng.random() < 0.5
            ):
                try:
                    out.append(self.transport.get_received_message())
                except RejectedMessageError:
                    out.append(None)
                progress = True
            if (
                self.msg_to_send
                and (not progress or self.rng.random() < 0.5)
                and self.transport.set_message_to_send(self.msg_to_send[0])
            ):
                del self.msg_to_send[0]
                progress = True
            to_recv = self.transport.get_bytes_to_send(
                have_next_message=bool(self.msg_to_send)
            ).to_send
            if to_recv and (not progress or self.rng.random() < 0.5):
                count = 1 + self.rng.randrange(len(to_recv))
                self.received += to_recv[:count]
                progress = True
                self.transport.mark_bytes_sent(count)
            if not progress:
                return out

    def send(self, data: bytes) -> None:
        """Queue `data` for the transport."""
        self.to_send += data

    def send_v1_version(self, magic: bytes) -> None:
        """Queue a whole v1 `version` message under `magic`."""
        self.send(Message(magic, "version", b"").serialize())

    def send_key(self) -> None:
        """Queue our public key."""
        self.send(self.ell)

    def send_garbage(self, garbage: bytes | int | None = None) -> None:
        """Queue garbage: given, of a given length, or of a random one."""
        if garbage is None:
            garbage = self.rng.randrange(MAX_GARBAGE_LEN + 1)
        if isinstance(garbage, int):
            garbage = self.rng.randbytes(garbage)
        self.sent_garbage = garbage
        self.send(garbage)

    def receive_key(self) -> None:
        """Read the transport's key from what it sent, and set our cipher."""
        assert len(self.received) >= KEY_LEN
        theirs = bytes(self.received[:KEY_LEN])
        self.cipher = Cipher(self.prv_key, self.ell, theirs, not self.initiator, MAGIC)
        del self.received[:KEY_LEN]

    def send_packet(
        self, contents: bytes, aad: bytes = b"", *, ignore: bool = False
    ) -> None:
        """Queue an encrypted packet."""
        self.send(self.cipher.encrypt(contents, aad, ignore=ignore))

    def send_garbage_term(self) -> None:
        """Queue our garbage terminator."""
        self.send(self.cipher.send_garbage_terminator)

    def send_version(self, data: bytes = b"", *, ignore: bool = False) -> None:
        """Queue a version packet, with the garbage as AAD if the first."""
        # the garbage is the AAD of the first packet only
        aad = b"" if self.sent_aad else self.sent_garbage
        self.send_packet(data, aad, ignore=ignore)
        self.sent_aad = True

    def receive_packet(self, aad: bytes = b"") -> bytes:
        """Return the contents of the next packet the transport sent."""
        assert len(self.received) >= LENGTH_LEN
        size = self.cipher.decrypt_length(bytes(self.received[:LENGTH_LEN]))
        assert len(self.received) >= size + EXPANSION
        contents, ignore = self.cipher.decrypt(
            bytes(self.received[LENGTH_LEN : size + EXPANSION]), aad
        )
        assert not ignore
        del self.received[: size + EXPANSION]
        return contents

    def receive_garbage(self) -> None:
        """Find the garbage terminator in what was received, and strip both."""
        garbage_len = self.received.find(self.cipher.recv_garbage_terminator)
        assert 0 <= garbage_len <= MAX_GARBAGE_LEN
        self.recv_garbage = bytes(self.received[:garbage_len])
        del self.received[: garbage_len + GARBAGE_TERMINATOR_LEN]

    def receive_version(self) -> None:
        """Read the transport's version packet: empty, the garbage as AAD."""
        # the transport sends an empty version packet
        assert self.receive_packet(self.recv_garbage) == b""

    def receive_message(self, short_id: int | str, payload: bytes) -> None:
        """Read a packet, and check its type and payload."""
        contents = self.receive_packet()
        if isinstance(short_id, int):
            assert contents == bytes([short_id]) + payload
        else:
            assert contents == b"\x00" + short_id.encode().ljust(12, b"\x00") + payload

    def send_message(self, command: int | str, payload: bytes = b"") -> None:
        """Queue a packet holding a message, by short id or long type."""
        if isinstance(command, int):
            self.send_packet(bytes([command]) + payload)
        else:
            self.send_packet(b"\x00" + command.encode().ljust(12, b"\x00") + payload)

    def compare_session_ids(self) -> None:
        """Check the transport reports v2 and our session id."""
        info = self.transport.get_info()
        assert info.transport_type is TransportProtocolType.V2
        assert info.session_id == self.cipher.session_id

    def damage(self, position: int) -> None:
        """Flip a bit of the byte at `position` of what is queued."""
        self.to_send[position] ^= 1


def message(command: str, payload: bytes, size: int | None = None) -> NetMessage:
    """Return the message a valid packet of `payload` is read as."""
    if size is None:
        size = len(payload) + 1 + EXPANSION
    return NetMessage(command, payload, size)


@pytest.mark.parametrize("seed", range(3))
def test_an_initiator_handshakes_and_exchanges_messages(seed: int) -> None:
    """An initiator handshakes; messages, valid or not, come out in order."""
    rng = random.Random(seed)
    tester = Peer(rng, initiator=True)
    assert tester.interact() == []
    tester.send_key()
    tester.send_garbage()
    tester.receive_key()
    tester.send_garbage_term()
    tester.send_version()
    assert tester.interact() == []
    tester.receive_garbage()
    tester.receive_version()
    tester.compare_session_ids()
    data_1 = rng.randbytes(rng.randrange(100000))
    data_2 = rng.randbytes(rng.randrange(1000))
    tester.send_message(4, data_1)  # cmpctblock
    tester.send_message(0)  # a long type of no characters is no type
    tester.send_message("tx", data_2)  # tx has a short id: the long form is read too
    assert tester.interact() == [
        message("cmpctblock", data_1),
        None,
        message("tx", data_2, 1 + 12 + len(data_2) + EXPANSION),
    ]


@pytest.mark.parametrize("position", [0, 20])
def test_a_bit_error_ends_the_connection_and_no_message_is_delivered(
    position: int,
) -> None:
    """Position 0 is the length: it is a packet longer by one, awaited.

    The error comes with the bytes of the next packet.
    """
    rng = random.Random(0)
    tester = Peer(rng, initiator=True)
    tester.send_key()
    tester.send_garbage()
    assert tester.interact() == []
    tester.receive_key()
    tester.send_garbage_term()
    tester.send_version()
    assert tester.interact() == []
    # contents of an even length, which a bit error in the length makes
    # longer by one
    tester.send_message("bad", rng.randbytes(1001))
    tester.damage(position)
    out = tester.interact()
    while out is not None:
        assert out == []
        tester.send_message(12, rng.randbytes(1000))
        out = tester.interact()


@pytest.mark.parametrize("seed", range(3))
def test_a_responder_handshakes_and_refuses_a_packet_over_the_limit(
    seed: int,
) -> None:
    """A responder handshakes, and a packet over the largest message ends it."""
    rng = random.Random(seed)
    tester = Peer(rng, initiator=False)
    tester.send_key()
    tester.send_garbage()
    assert tester.interact() == []
    tester.receive_key()
    tester.send_garbage_term()
    tester.send_version()
    assert tester.interact() == []
    tester.receive_garbage()
    tester.receive_version()
    tester.compare_session_ids()
    data_1 = rng.randbytes(rng.randrange(100000))
    data_2 = rng.randbytes(rng.randrange(1000))
    tester.send_message(14, data_1)
    tester.send_message(19, data_2)
    assert tester.interact() == [message("inv", data_1), message("pong", data_2)]
    tester.send_message(11, rng.randbytes(4_005_000))
    assert tester.interact() is None


@pytest.mark.parametrize("seed", range(12))
def test_unusual_but_valid_scenarios(seed: int) -> None:
    """The garbage at its bounds, decoys, a version packet with contents."""
    rng = random.Random(seed)
    initiator = rng.random() < 0.5
    garbage_len = rng.choice([0, MAX_GARBAGE_LEN])
    ignored_versions = rng.randrange(10)
    version_data = rng.randbytes(rng.choice([0, rng.randrange(1000)]))
    # the key goes out at once, as a responder's must
    send_immediately = not initiator or rng.random() < 0.5
    decoys_1, decoys_2 = rng.randrange(300), rng.randrange(300)
    tester = Peer(rng, initiator=initiator)
    if send_immediately:
        tester.send_key()
        tester.send_garbage(garbage_len)
    assert tester.interact() == []
    if not send_immediately:
        tester.send_key()
        tester.send_garbage(garbage_len)
    tester.receive_key()
    tester.send_garbage_term()
    for _ in range(ignored_versions):
        tester.send_version(
            rng.randbytes(rng.choice([0, rng.randrange(1000)])), ignore=True
        )
    tester.send_version(version_data)
    assert tester.interact() == []
    tester.receive_garbage()
    tester.receive_version()
    tester.compare_session_ids()
    for _ in range(decoys_1):
        tester.send_packet(rng.randbytes(rng.randrange(1000)), ignore=True)
    data_1 = rng.randbytes(rng.randrange(4_000_000))
    tester.send_message(28, data_1)  # addrv2
    for _ in range(decoys_2):
        tester.send_packet(rng.randbytes(rng.randrange(1000)), ignore=True)
    data_2 = rng.randbytes(rng.randrange(1000))
    tester.send_message(13, data_2)  # headers
    # `blocktxn` and a stray byte after its NULs
    tester.send_message("blocktxn\x00\x00\x00a")
    tester.send_message("foobar")  # an unknown type is a message
    tester.msg_to_send.append(SerializedMessage("barfoo", b""))
    out = tester.interact()
    assert out == [
        message("addrv2", data_1),
        message("headers", data_2),
        None,
        message("foobar", b"", 1 + 12 + EXPANSION),
    ]
    tester.receive_message("barfoo", b"")


def test_garbage_one_byte_too_long_fails_for_an_initiator() -> None:
    """4096 bytes of garbage and no terminator end the connection."""
    tester = Peer(random.Random(0), initiator=True)
    assert tester.interact() == []
    tester.send_key()
    tester.send_garbage(MAX_GARBAGE_LEN + 1)
    tester.receive_key()
    tester.send_garbage_term()
    assert tester.interact() is None


def test_garbage_one_byte_too_long_fails_for_a_responder() -> None:
    """The same for a responder."""
    tester = Peer(random.Random(0), initiator=False)
    tester.send_key()
    tester.send_garbage(MAX_GARBAGE_LEN + 1)
    assert tester.interact() == []
    tester.receive_key()
    tester.send_garbage_term()
    assert tester.interact() is None


def test_the_missing_terminator_is_reported_at_4111_bytes() -> None:
    """The error comes at 4095 bytes of garbage and 16 of terminator."""
    rng = random.Random(0)
    tester = Peer(rng, initiator=True)
    tester.send_key()
    assert tester.interact() == []
    tester.receive_key()
    tester.send(rng.randbytes(MAX_GARBAGE_LEN + GARBAGE_TERMINATOR_LEN - 1))
    assert tester.interact() == []
    tester.send(b"\x00")
    with pytest.raises(V2TransportError, match="missing garbage terminator"):
        feed(tester.transport, bytes(tester.to_send))


@pytest.mark.parametrize("seed", range(3))
def test_fifteen_bytes_of_the_terminator_inside_the_garbage_do_not_end_it(
    seed: int,
) -> None:
    """Only a whole terminator ends the garbage; 4 MB payloads pass."""
    rng = random.Random(seed)
    tester = Peer(rng, initiator=True)
    assert tester.interact() == []
    tester.send_key()
    tester.receive_key()
    before = rng.randrange(MAX_GARBAGE_LEN - 16 + 1)
    after = rng.randrange(MAX_GARBAGE_LEN - 16 - before + 1)
    garbage = bytearray(rng.randbytes(before + 16 + after))
    garbage[before : before + 16] = tester.cipher.send_garbage_terminator
    garbage[before + 15] ^= 1 << rng.randrange(8)
    tester.send_garbage(bytes(garbage))
    tester.send_garbage_term()
    tester.send_version()
    assert tester.interact() == []
    tester.receive_garbage()
    tester.receive_version()
    tester.compare_session_ids()
    data_1 = rng.randbytes(4_000_000)  # the largest payload is received
    data_2 = rng.randbytes(4_000_000)  # and sent
    tester.send_message(rng.randrange(223) + 33)  # beyond the table
    tester.send_message(2, data_1)  # block
    tester.msg_to_send.append(SerializedMessage("blocktxn", data_2))
    assert tester.interact() == [None, message("block", data_1)]
    tester.receive_message(3, data_2)


def test_the_v1_prefix_of_the_network_hands_over_to_the_fallback() -> None:
    """A v1 `version` message of the network is the fallback's."""
    tester = Peer(random.Random(0), initiator=False)
    tester.send_v1_version(MAGIC)
    assert tester.interact() == [NetMessage("version", b"", 24)]
    assert tester.transport.get_info() == TransportInfo(TransportProtocolType.V1)
    assert tester.received == b""


def test_the_v1_prefix_is_detected_at_16_bytes_and_not_before() -> None:
    """No more than 16 bytes are taken until they are known to be v1 or not."""
    transport = V2Transport(MAGIC, V1Transport(MAGIC), initiating=False)
    prefix = MAGIC + b"version\x00\x00\x00\x00\x00"
    assert feed(transport, prefix[:15]) == 15
    assert transport.get_info() == TransportInfo(TransportProtocolType.DETECTING)
    assert to_send(transport) == b""
    # one more byte is all that is taken, though the 24 of a header are there
    assert feed(transport, prefix[15:] + b"rest") == 1
    assert transport.get_info() == TransportInfo(TransportProtocolType.V1)


def test_a_byte_off_the_v1_prefix_is_the_start_of_a_key() -> None:
    """A mismatch means v2: the bytes stay, and the key goes out."""
    transport = V2Transport(
        MAGIC, V1Transport(MAGIC), initiating=False, prv_key=1, garbage=b""
    )
    prefix = MAGIC + b"version\x00\x00\x00\x00\x00"
    assert feed(transport, prefix[:-1] + b"X") == 16
    assert transport.get_info() == TransportInfo(TransportProtocolType.DETECTING)
    # the responder starts sending its key
    assert len(to_send(transport)) == KEY_LEN


def test_the_v1_prefix_of_another_network_is_refused() -> None:
    """A v1 header under another network's magic ends the connection."""
    tester = Peer(random.Random(0), initiator=False)
    tester.send_v1_version(Main().magic)
    assert tester.interact() is None


def test_the_wrong_network_is_refused_with_its_magic_named() -> None:
    """The error names the magic received."""
    transport = V2Transport(MAGIC, V1Transport(MAGIC), initiating=False)
    with pytest.raises(V2TransportError, match=r"wrong MessageStart f9beb4d9"):
        feed(transport, Main().magic + b"version\x00\x00\x00\x00\x00" + bytes(8))


def test_an_initiator_does_not_take_a_v1_header_for_the_wrong_network() -> None:
    """Only a responder is spoken to first, so only it looks."""
    transport = V2Transport(MAGIC, V1Transport(MAGIC), initiating=True)
    assert feed(transport, Main().magic + b"version\x00\x00\x00\x00\x00") == 16


def test_the_key_and_garbage_go_first_and_a_message_waits_for_the_handshake() -> None:
    """An initiator sends its key and garbage first; no message is set yet."""
    garbage = b"garbage"
    transport = V2Transport(
        MAGIC, V1Transport(MAGIC), initiating=True, prv_key=5, garbage=garbage
    )
    assert not transport.set_message_to_send(SerializedMessage("ping", bytes(8)))
    offered = transport.get_bytes_to_send(have_next_message=True)
    data = bytes(offered.to_send)
    assert len(data) == KEY_LEN + len(garbage)
    assert data[KEY_LEN:] == garbage
    assert (offered.more, offered.command) == (False, "")
    transport.mark_bytes_sent(10)
    assert to_send(transport) == data[10:]
    assert transport.get_info() == TransportInfo(TransportProtocolType.DETECTING)


def test_one_message_is_held_until_it_is_sent() -> None:
    """A message is set when the buffer is empty; `more` needs a next one."""
    rng = random.Random(0)
    tester = Peer(rng, initiator=True)
    tester.send_key()
    tester.send_garbage(5)
    assert tester.interact() == []
    tester.receive_key()
    tester.send_garbage_term()
    tester.send_version()
    assert tester.interact() == []
    transport = tester.transport
    # everything of the handshake is out; what is left is a message's to send
    assert to_send(transport) == b""
    assert transport.get_bytes_to_send(have_next_message=True).more
    ping = SerializedMessage("ping", bytes(8))
    assert transport.set_message_to_send(ping)
    assert not transport.set_message_to_send(SerializedMessage("pong", bytes(8)))
    offered = transport.get_bytes_to_send(have_next_message=False)
    assert (len(offered.to_send), offered.more, offered.command) == (
        1 + 8 + EXPANSION,
        False,
        "ping",
    )
    size = len(offered.to_send)
    assert transport.get_bytes_to_send(have_next_message=True).more
    transport.mark_bytes_sent(0)
    assert len(to_send(transport)) == size
    transport.mark_bytes_sent(size - 1)
    assert len(to_send(transport)) == 1
    assert not transport.set_message_to_send(SerializedMessage("pong", bytes(8)))
    transport.mark_bytes_sent(1)
    assert to_send(transport) == b""
    assert transport.set_message_to_send(SerializedMessage("pong", bytes(8)))


def test_a_message_type_with_no_short_id_is_sent_in_the_long_form() -> None:
    """A type outside the table is sent long, one in it by its id."""
    tester = Peer(random.Random(0), initiator=False)
    tester.send_key()
    tester.send_garbage(0)
    assert tester.interact() == []
    tester.receive_key()
    tester.send_garbage_term()
    tester.send_version()
    tester.msg_to_send.append(SerializedMessage("barfoo", b"x"))
    tester.msg_to_send.append(SerializedMessage("ping", b"y"))
    assert tester.interact() == []
    tester.receive_garbage()
    tester.receive_version()
    tester.receive_message("barfoo", b"x")
    tester.receive_message(MESSAGE_IDS.index("ping"), b"y")


def test_the_v1_state_hands_every_call_to_the_fallback() -> None:
    """After the hand-over every call is the fallback's."""
    transport = V2Transport(MAGIC, V1Transport(MAGIC), initiating=False)
    wire = Message(MAGIC, "version", b"abc").serialize()
    # the 16 bytes that told are taken, then the fallback takes the rest of
    # the header, and then the payload
    rest = transport.received_bytes(memoryview(wire))
    assert len(rest) == len(wire) - 16
    rest = transport.received_bytes(rest)
    assert len(rest) == len(wire) - 24
    assert not transport.received_message_complete()
    assert not transport.received_bytes(rest)
    assert transport.received_message_complete()
    assert transport.get_received_message() == NetMessage("version", b"abc", 27)
    sent = Message(MAGIC, "pong", b"\x01").serialize()
    assert transport.set_message_to_send(SerializedMessage("pong", b"\x01"))
    assert not transport.set_message_to_send(SerializedMessage("pong", b""))
    offered = transport.get_bytes_to_send(have_next_message=False)
    assert (bytes(offered.to_send), offered.more, offered.command) == (
        sent[:24],
        True,
        "pong",
    )
    transport.mark_bytes_sent(24)
    assert to_send(transport) == sent[24:]
    transport.mark_bytes_sent(1)
    assert not transport.should_reconnect_v1()
    assert transport.get_info() == TransportInfo(TransportProtocolType.V1)


@pytest.mark.parametrize("initiating", [True, False])
def test_no_message_is_incomplete_and_changes_nothing(*, initiating: bool) -> None:
    """Asking for a message before one is whole changes nothing."""
    transport = V2Transport(MAGIC, V1Transport(MAGIC), initiating=initiating)
    with pytest.raises(IncompleteMessageError):
        transport.get_received_message()
    assert transport.get_info() == TransportInfo(TransportProtocolType.DETECTING)
    assert feed(transport, bytes(1)) == 1


def test_garbage_longer_than_the_limit_is_refused_at_construction() -> None:
    """A caller's garbage is bounded as the receiver's is."""
    with pytest.raises(ValueError, match="garbage too long: 4096 bytes"):
        V2Transport(
            MAGIC,
            V1Transport(MAGIC),
            initiating=True,
            garbage=bytes(MAX_GARBAGE_LEN + 1),
        )


def test_without_a_key_or_garbage_given_both_are_random() -> None:
    """Two transports built alike send different keys."""
    first = V2Transport(MAGIC, V1Transport(MAGIC), initiating=True)
    second = V2Transport(MAGIC, V1Transport(MAGIC), initiating=True)
    assert to_send(first)[:KEY_LEN] != to_send(second)[:KEY_LEN]


def test_a_packet_that_does_not_authenticate_is_an_error_naming_its_size() -> None:
    """A version packet without the garbage as AAD fails to decrypt."""
    tester = Peer(random.Random(0), initiator=True)
    tester.send_key()
    tester.send_garbage(0)
    assert tester.interact() == []
    tester.receive_key()
    tester.send_garbage_term()
    # a version packet that does not carry the garbage as AAD
    tester.send_packet(b"", b"not the garbage")
    with pytest.raises(V2TransportError, match=r"decryption failure \(0 bytes\)"):
        feed(tester.transport, bytes(tester.to_send))


def test_a_packet_is_refused_over_the_largest_message() -> None:
    """A long type and 4,000,000 bytes of payload are taken; one more is not."""
    tester = Peer(random.Random(0), initiator=True)
    tester.send_key()
    tester.send_garbage(0)
    assert tester.interact() == []
    tester.receive_key()
    tester.send_garbage_term()
    tester.send_version()
    assert tester.interact() == []
    # the limit is a long type and 4,000,000 bytes of payload
    tester.send_message("big", bytes(4_000_000))
    out = tester.interact()
    assert out is not None
    assert [m.command for m in out if m] == ["big"]
    tester.send(tester.cipher.encrypt(bytes(1 + 12 + 4_000_001))[:LENGTH_LEN])
    with pytest.raises(V2TransportError, match=r"packet too large \(4000014 bytes\)"):
        feed(tester.transport, bytes(tester.to_send))


@pytest.mark.parametrize(
    ("initiating", "sent", "received", "expected"),
    [
        # a responder never reconnects
        (False, 0, 0, False),
        (False, 100, 1, False),
        # an initiator that has sent too little for a v1 peer to leave
        (True, 23, 0, False),
        # sent enough, and heard nothing
        (True, 24, 0, True),
        (True, 100, 0, True),
        # sent enough, and heard something
        (True, 100, 1, False),
        (True, 100, 63, False),
        # sent enough, and heard the key
        (True, 100, 64, False),
    ],
)
def test_should_reconnect_v1_truth_table(
    *, initiating: bool, sent: int, received: int, expected: bool
) -> None:
    """Only an initiator that sent 24 bytes and received none reconnects."""
    garbage = bytes(MAX_GARBAGE_LEN)
    transport = V2Transport(
        MAGIC, V1Transport(MAGIC), initiating=initiating, prv_key=7, garbage=garbage
    )
    feed(transport, b"\x01" * received)
    transport.mark_bytes_sent(sent)
    assert transport.should_reconnect_v1() is expected


def states(transport: V2Transport) -> tuple[SendState, RecvState]:
    """Return the send and the receive state."""
    return transport._send_state, transport._recv_state


def test_the_states_after_a_handshake() -> None:
    """With the key in, the sender is ready and the receiver awaits the end."""
    tester = Peer(random.Random(0), initiator=True)
    assert states(tester.transport) == (SendState.AWAITING_KEY, RecvState.KEY)
    tester.send_key()
    tester.send_garbage(3)
    assert tester.interact() == []
    assert states(tester.transport) == (SendState.READY, RecvState.GARB_GARBTERM)


def test_a_responder_without_a_fallback_refuses_the_v1_prefix_at_16_bytes() -> None:
    """Nothing is raised before the 16th byte, and then `V1PeerRefusedError`."""
    transport = V2Transport(MAGIC, None, initiating=False)
    prefix = MAGIC + b"version\x00\x00\x00\x00\x00"
    assert feed(transport, prefix[:15]) == 15
    assert transport.get_info() == TransportInfo(TransportProtocolType.DETECTING)
    with pytest.raises(V1PeerRefusedError, match="V1 peer refused"):
        feed(transport, prefix[15:])


def test_a_refused_v1_peer_is_a_v2_transport_error() -> None:
    """A caller that catches the family catches the refusal too."""
    assert issubclass(V1PeerRefusedError, V2TransportError)


def test_a_responder_without_a_fallback_still_takes_a_v2_peer() -> None:
    """A byte off the v1 prefix is the start of a key, fallback or not."""
    transport = V2Transport(MAGIC, None, initiating=False, prv_key=1, garbage=b"")
    prefix = MAGIC + b"version\x00\x00\x00\x00\x00"
    assert feed(transport, prefix[:-1] + b"X") == 16
    assert len(to_send(transport)) == KEY_LEN
