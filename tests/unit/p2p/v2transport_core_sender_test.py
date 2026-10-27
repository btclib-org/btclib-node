# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`V2Transport`, against decoys built by Core's own v2 sender.

The peer is Core's functional-test `EncryptedP2PState`
(`test/functional/test_framework/v2_p2p.py` at bitcoin/bitcoin@de96f3ae6d16),
with Core's own ChaCha20-Poly1305, HKDF and ElligatorSwift, vendored
under `tests/unit/_data/test_framework/`. In `v2transport_test.py` the
peer is `btclib.p2p.bip324.Cipher`, the transport's own cipher, so a
defect shared by its sender and receiver passes there; here it does not.

The vendored files are loaded by path, under the package name
`test_framework` their imports use. They also import from Core's
`key.py`, `messages.py` and `util.py`, which are not vendored: they are
large, and bring `descriptors.py` and `crypto/siphash.py` with them, for
the few names used. Those names get stand-ins here, from the same
commit: `TaggedHash` and `MAGIC_BYTES` as Core has them, and
`assert_equal` and `assert_not_equal` without the dict summary and the
message Core's can add.
"""

from __future__ import annotations

import hashlib
import importlib.util
import random
import sys
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from ecc.curves import secp256k1

from btclib_node.chains import RegTest
from btclib_node.exceptions import RejectedMessageError
from btclib_node.p2p.transport import NetMessage, SerializedMessage
from btclib_node.p2p.v2transport import MAX_GARBAGE_LEN, V2Transport

MAGIC = RegTest().magic
_CORE_DIR = Path(__file__).parents[1] / "_data" / "test_framework"
# each after the modules it imports
_VENDORED = (
    "crypto.secp256k1",
    "crypto.ellswift",
    "crypto.chacha20",
    "crypto.poly1305",
    "crypto.bip324_cipher",
    "crypto.hkdf",
    "v2_p2p",
)
# what a packet adds to its contents: the length, the header and the tag
_EXPANSION = 3 + 1 + 16
_TERMINATOR_LEN = 16


def _assert_equal(thing1: object, thing2: object, *args: object) -> None:
    """Core's `util.assert_equal`, without its summary of two dicts."""
    if thing1 != thing2 or any(thing1 != arg for arg in args):
        err_msg = "not({})".format(" == ".join(map(str, (thing1, thing2, *args))))
        raise AssertionError(err_msg)


def _assert_not_equal(thing1: object, thing2: object) -> None:
    """Core's `util.assert_not_equal`, without its message argument."""
    if thing1 == thing2:
        err_msg = f"Both values are {thing1}"
        raise AssertionError(err_msg)


def _tagged_hash(tag: str, data: bytes) -> bytes:
    """Core's `key.TaggedHash`, BIP340's tagged hash."""
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + data).digest()


def _stand_in(name: str, **attributes: object) -> ModuleType:
    """Return a module named `name` holding `attributes`."""
    module = ModuleType(name)
    vars(module).update(attributes)
    return module


def _load_core() -> dict[str, ModuleType]:
    """Return the vendored modules, by their name under `test_framework`.

    The names are in `sys.modules` only while the modules import each
    other.
    """
    stand_ins = {
        "test_framework": _stand_in("test_framework", __path__=[]),
        "test_framework.crypto": _stand_in("test_framework.crypto", __path__=[]),
        "test_framework.key": _stand_in("test_framework.key", TaggedHash=_tagged_hash),
        # Core's `MAGIC_BYTES`, the regtest entry being the one used
        "test_framework.messages": _stand_in(
            "test_framework.messages",
            MAGIC_BYTES={
                "mainnet": b"\xf9\xbe\xb4\xd9",
                "testnet4": b"\x1c\x16\x3f\x28",
                "regtest": b"\xfa\xbf\xb5\xda",
                "signet": b"\x0a\x03\xcf\x40",
            },
        ),
        "test_framework.util": _stand_in(
            "test_framework.util",
            assert_equal=_assert_equal,
            assert_not_equal=_assert_not_equal,
        ),
    }
    assert not set(stand_ins) & set(sys.modules)
    loaded: dict[str, ModuleType] = {}
    sys.modules.update(stand_ins)
    try:
        for name in _VENDORED:
            path = _CORE_DIR.joinpath(*name.split(".")).with_suffix(".py")
            spec = importlib.util.spec_from_file_location(
                f"test_framework.{name}", path
            )
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
    finally:
        for name in [*stand_ins, *(f"test_framework.{n}" for n in loaded)]:
            del sys.modules[name]
    return loaded


@pytest.fixture(scope="module")
def core_modules() -> dict[str, ModuleType]:
    """Return Core's vendored modules, loaded once for this module."""
    return _load_core()


@pytest.fixture
def v2_p2p(
    core_modules: dict[str, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> ModuleType:
    """Return Core's `v2_p2p`, its keys, garbage and decoys drawn from seed 0.

    Core draws them from the module `random`, which is replaced here.
    """
    rng = random.Random(0)
    for name in ("v2_p2p", "crypto.ellswift"):
        monkeypatch.setattr(core_modules[name], "random", rng)
    return core_modules["v2_p2p"]


def deliver(
    transport: V2Transport, data: bytes, rng: random.Random
) -> list[NetMessage | None]:
    """Hand `data` over in chunks of random size, and return the messages.

    A message the transport rejects is `None`.
    """
    out: list[NetMessage | None] = []
    view = memoryview(data)
    while view or transport.received_message_complete():
        if transport.received_message_complete():
            try:
                out.append(transport.get_received_message())
            except RejectedMessageError:
                out.append(None)
            continue
        size = 1 + rng.randrange(len(view))
        view = view[size - len(transport.received_bytes(view[:size])) :]
    return out


def drain(transport: V2Transport) -> bytes:
    """Return everything the transport has to send, and mark it sent."""
    data = bytes(transport.get_bytes_to_send(have_next_message=False).to_send)
    transport.mark_bytes_sent(len(data))
    return data


def complete_with_decoys(state: Any, stream: BytesIO, decoys: list[bytes]) -> bytes:
    """Core's `complete_handshake`, with the decoys given rather than drawn."""
    theirs = state.received_prefix + stream.read(64 - len(state.received_prefix))
    secret = state.v2_ecdh(
        state.privkey_ours, theirs, state.ellswift_ours, state.initiating
    )
    state.initialize_v2_transport(secret)
    wire: bytes = state.peer["send_garbage_terminator"]
    aad = state.sent_garbage
    for decoy in decoys:
        wire += state.v2_enc_packet(decoy, aad=aad, ignore=True)
        aad = b""
    return wire + bytes(state.v2_enc_packet(state.transport_version, aad=aad))


def handshake(
    v2_p2p: ModuleType,
    rng: random.Random,
    decoys: list[bytes] | None,
    *,
    initiating: bool,
) -> tuple[V2Transport, Any]:
    """Return the transport and Core's state, past the handshake both ways.

    Core sends `decoys` before its version packet; where they are `None`,
    Core's own `complete_handshake` draws them.
    """
    transport = V2Transport(
        MAGIC,
        None,
        initiating=initiating,
        prv_key=rng.randrange(1, secp256k1.n),
        garbage=rng.randbytes(rng.randrange(MAX_GARBAGE_LEN + 1)),
    )
    state = v2_p2p.EncryptedP2PState(initiating=not initiating, net="regtest")
    if initiating:
        stream = BytesIO(drain(transport))
        _, wire = state.respond_v2_handshake(stream)
        assert isinstance(wire, bytes)
    else:
        assert deliver(transport, state.initiate_v2_handshake(), rng) == []
        stream = BytesIO(drain(transport))
        wire = b""
    if decoys is None:
        _, rest = state.complete_handshake(stream)
        # the draw sent at least one decoy
        assert len(rest) > _TERMINATOR_LEN + _EXPANSION
        wire += rest
    else:
        wire += complete_with_decoys(state, stream, decoys)
    assert deliver(transport, wire, rng) == []
    _, ok = state.authenticate_handshake(stream.read() + drain(transport))
    assert ok
    assert state.tried_v2_handshake
    return transport, state


def packet(v2_p2p: ModuleType, state: Any, command: bytes, payload: bytes) -> bytes:
    """Return a message as Core's sender encrypts it, by its short id."""
    short_id = v2_p2p.MSGTYPE_TO_SHORTID[command]
    return bytes(state.v2_enc_packet(bytes([short_id]) + payload))


def decoy(state: Any, contents: bytes) -> bytes:
    """Return a decoy as Core's sender encrypts it."""
    return bytes(state.v2_enc_packet(contents, ignore=True))


def ping_pong(
    v2_p2p: ModuleType, transport: V2Transport, state: Any, rng: random.Random
) -> None:
    """Check a message goes each way after the decoys."""
    nonce = rng.randbytes(8)
    wire = packet(v2_p2p, state, b"ping", nonce)
    assert deliver(transport, wire, rng) == [
        NetMessage("ping", nonce, 1 + 8 + _EXPANSION)
    ]
    assert transport.set_message_to_send(SerializedMessage("pong", nonce))
    _, contents = state.v2_receive_packet(drain(transport))
    assert contents == bytes([v2_p2p.MSGTYPE_TO_SHORTID[b"pong"]]) + nonce


# the transport's role; Core takes the other
ROLES = pytest.mark.parametrize(
    "initiating", [True, False], ids=["initiator", "responder"]
)


@ROLES
def test_core_s_own_decoys_before_the_version_packet_are_ignored(
    v2_p2p: ModuleType, *, initiating: bool
) -> None:
    """Core's `complete_handshake` sends decoys, as seed 0 draws them."""
    rng = random.Random(0)
    transport, state = handshake(v2_p2p, rng, None, initiating=initiating)
    ping_pong(v2_p2p, transport, state, rng)


@ROLES
@pytest.mark.parametrize(
    "lengths", [[0], [1], [100] * 10, [1_000_000]], ids=["empty", "one", "ten", "1MB"]
)
def test_decoys_before_the_version_packet_are_ignored(
    v2_p2p: ModuleType, lengths: list[int], *, initiating: bool
) -> None:
    """Decoys of any length between the terminator and the version packet."""
    rng = random.Random(0)
    decoys = [rng.randbytes(length) for length in lengths]
    transport, state = handshake(v2_p2p, rng, decoys, initiating=initiating)
    ping_pong(v2_p2p, transport, state, rng)


@ROLES
@pytest.mark.parametrize("length", [0, 1_000_000], ids=["empty", "1MB"])
def test_a_decoy_between_messages_is_ignored(
    v2_p2p: ModuleType, length: int, *, initiating: bool
) -> None:
    """An empty decoy and a 1 MB one, once the handshake is over."""
    rng = random.Random(0)
    transport, state = handshake(v2_p2p, rng, [], initiating=initiating)
    ping_pong(v2_p2p, transport, state, rng)
    assert deliver(transport, decoy(state, rng.randbytes(length)), rng) == []
    ping_pong(v2_p2p, transport, state, rng)


@ROLES
def test_decoys_across_a_rekey_are_ignored(
    core_modules: dict[str, ModuleType], v2_p2p: ModuleType, *, initiating: bool
) -> None:
    """Both of Core's ciphers rekey after every 224 packets, decoys included."""
    rekey = core_modules["crypto.chacha20"].REKEY_INTERVAL
    rng = random.Random(0)
    before_version = 100
    transport, state = handshake(
        v2_p2p, rng, [b""] * before_version, initiating=initiating
    )
    # the version packet was the last sent: decoys up to 20 past the rekey
    sent = before_version + 1
    wire = b"".join(
        decoy(state, rng.randbytes(rng.randrange(100)))
        for _ in range(rekey + 20 - sent)
    )
    nonce = rng.randbytes(8)
    wire += packet(v2_p2p, state, b"ping", nonce)
    assert deliver(transport, wire, rng) == [
        NetMessage("ping", nonce, 1 + 8 + _EXPANSION)
    ]
    ping_pong(v2_p2p, transport, state, rng)


@ROLES
def test_an_empty_packet_without_the_ignore_bit_is_a_message(
    v2_p2p: ModuleType, *, initiating: bool
) -> None:
    """The ignore bit makes a decoy, not the length: this one is rejected."""
    rng = random.Random(0)
    transport, state = handshake(v2_p2p, rng, [], initiating=initiating)
    assert deliver(transport, bytes(state.v2_enc_packet(b"")), rng) == [None]
    ping_pong(v2_p2p, transport, state, rng)


def test_the_stand_ins_fail_as_core_s_do(core_modules: dict[str, ModuleType]) -> None:
    """Core's checks reach the stand-ins, which raise on a mismatch."""
    v2_p2p, secp256k1_ = core_modules["v2_p2p"], core_modules["crypto.secp256k1"]
    with pytest.raises(AssertionError, match=r"not\(b'x' == b''\)"):
        v2_p2p.assert_equal(b"x", b"")
    with pytest.raises(AssertionError, match="Both values are 0"):
        secp256k1_.assert_not_equal(0, 0)
