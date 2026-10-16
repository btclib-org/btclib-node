# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A node speaking p2p to a real peer, over an actual socket.

Each module here starts one or more `Node`s and drives them through a
handshake, a download or a request/answer exchange the way another
implementation on the wire would, rather than calling their internals
directly the way `tests/unit/p2p/` does.

What several modules share is here. A `ScriptedPeer` dials a node's p2p
port, completes the handshake and answers what the node asks of it from a
chain held in memory: `getheaders` with the headers after the first
locator entry it holds, `getdata` with the blocks, `ping` with `pong`.
Past the handshake it sends nothing of its own, so what it says is what
the test scripted. A peer lies by being given `doctor_headers` or
`doctor_block`, which see each answer before it is sent, or by serving a
chain whose blocks it then withholds.

`Lie` names the lies the tests tell and `a_peer` builds the peer that
tells one. `a_node_among_peers` starts a node and its peers in an order
the test chooses, which matters: a node syncs headers from one peer at a
time while its best header is old (`DownloadManager.sync_headers`), so
only the peers asked before the honest one are ever put to the proof.

The peers speak v1 on a raw socket, which a node without `v1transport`
drops. A real `Node` serves a chain too, as `header_sync_test.py` uses
one, but it cannot be told to lie and costs a store, two event loops and
a thread each.
"""

import functools
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from datetime import timedelta
from enum import StrEnum
from io import BytesIO
from itertools import count
from typing import TYPE_CHECKING, Self

from btclib.block import Block, BlockHeader
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS, target_from_bits
from btclib.exceptions import IncompleteMessageError
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.p2p.data import BlockPayload
from btclib.p2p.handshake import Verack, Version
from btclib.p2p.inventory import GetData, GetHeaders, Headers, NotFound
from btclib.p2p.keepalive import Ping, Pong
from btclib.p2p.limits import MAX_HEADERS_RESULTS, PROTOCOL_VERSION
from btclib.p2p.message import Message
from btclib.p2p.negotiation import WtxidRelay

from btclib_node import download
from btclib_node.chains import RegTest
from tests import (
    brute_force_nonce,
    generate_random_chain,
    wait_until,
    wait_until_listening,
)
from tests.conftest import node_context

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

    import pytest
    from btclib.p2p.payload import Payload

    from btclib_node import Node

_RECV_BYTES = 1 << 16

# regtest does not retarget, so every header must carry the pow limit; one
# step under it is a target the proof of work can meet and the chain does
# not require (Core's `bad-diffbits`)
_WRONG_BITS = REGTEST_POW_LIMIT_BITS[:-1] + bytes([REGTEST_POW_LIMIT_BITS[-1] - 1])

# how long a block may stay in flight before its peer is dropped: Core's
# ten minutes (`BLOCK_DOWNLOAD_TIMEOUT_BASE` block intervals), cut to what
# a test can wait for
_BLOCK_DOWNLOAD_TIMEOUT_SECONDS = 3

DoctorHeaders = Callable[[int, list[BlockHeader]], list[BlockHeader]]
DoctorBlock = Callable[[int, Block], Block]


def shorten_block_download_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop a peer a block was asked of after a few seconds, not ten minutes."""
    base = _BLOCK_DOWNLOAD_TIMEOUT_SECONDS / download._POW_TARGET_SPACING
    monkeypatch.setattr(download, "_BLOCK_DOWNLOAD_TIMEOUT_BASE", base)


class ScriptedPeer:
    """Serve `chain`, the blocks after the genesis `genesis`, to one node.

    `doctor_headers` is handed the height of the block before the batch
    and the batch a `getheaders` is answered with, and `doctor_block` the
    height and the block a `getdata` is answered with; what they return
    is what is sent. A peer given `serves_blocks=False` answers every
    block it is asked for with `notfound`. `lied` is set once a doctored
    answer or a `notfound` was sent, `forged` holds the hashes of the
    headers sent that the chain does not have, and `dropped` is set once
    the connection is closed, by whichever end.
    """

    def __init__(
        self,
        node: Node,
        chain: Sequence[Block],
        *,
        genesis: bytes,
        batch: int = MAX_HEADERS_RESULTS,
        doctor_headers: DoctorHeaders | None = None,
        doctor_block: DoctorBlock | None = None,
        serves_blocks: bool = True,
    ) -> None:
        """Dial `node`'s p2p port; the handshake and serving start on entry."""
        self.chain = list(chain)
        self.batch = batch
        self.doctor_headers = doctor_headers
        self.doctor_block = doctor_block
        self.serves_blocks = serves_blocks
        self.lied = threading.Event()
        self.forged: set[bytes] = set()
        self.dropped = threading.Event()
        self._magic = node.chain.magic
        self._height = {genesis: 0}
        self._height.update({b.header.hash: i + 1 for i, b in enumerate(self.chain)})
        self._socket = socket.create_connection(
            ("127.0.0.1", node.p2p_port), timeout=30
        )
        self._socket.settimeout(None)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> Self:
        """Shake hands, then answer the node on a thread of its own."""
        self._send(self._version())
        # `wtxidrelay` ahead of `verack`, where BIP339 places it
        self._send(WtxidRelay())
        self._send(Verack())
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Drop the socket and wait for the thread to end."""
        with suppress(OSError):
            self._socket.shutdown(socket.SHUT_RDWR)
        self._socket.close()
        self._thread.join(timeout=30)

    def _version(self) -> Version:
        services = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS
        return Version(
            version=PROTOCOL_VERSION,
            services=services,
            timestamp=int(time.time()),
            addr_recv=NetworkAddress(services=services, port=0),
            addr_from=NetworkAddress(services=services, port=0),
            nonce=secrets.randbelow(2**64),
            user_agent=b"/scripted/",
            start_height=len(self.chain),
            relay=True,
        )

    def _send(self, payload: Payload) -> None:
        message = Message(
            self._magic, payload.command, payload.serialize(check_validity=False)
        )
        self._socket.sendall(message.serialize())

    def _serve(self) -> None:
        """Read messages until the connection closes, and answer them."""
        buffer = b""
        try:
            while True:
                stream = BytesIO(buffer)
                try:
                    message = Message.parse(stream, check_validity=False)
                except IncompleteMessageError:
                    data = self._socket.recv(_RECV_BYTES)
                    if not data:
                        return
                    buffer += data
                    continue
                buffer = buffer[stream.tell() :]
                self._answer(message)
        except OSError:
            return
        finally:
            self.dropped.set()

    def _answer(self, message: Message) -> None:
        match message.command:
            case "ping":
                self._send(Pong(Ping.parse(message.payload).nonce))
            case "getheaders":
                locator = GetHeaders.parse(message.payload).locator
                self._send(Headers(self._headers_after(locator), check_validity=False))
            case "getdata":
                self._send_blocks(GetData.parse(message.payload))
            case _:
                pass

    def _headers_after(self, locator: Sequence[bytes]) -> list[BlockHeader]:
        """Return the batch after the first locator entry held, doctored."""
        start = next((self._height[h] for h in locator if h in self._height), 0)
        headers = [block.header for block in self.chain[start : start + self.batch]]
        if self.doctor_headers is None:
            return headers
        doctored = self.doctor_headers(start, headers)
        if doctored != headers:
            self.lied.set()
            self.forged |= {h.hash for h in doctored} - {h.hash for h in headers}
        return doctored

    def _send_blocks(self, request: GetData) -> None:
        missing = []
        for item in request.items:
            height = self._height.get(item.hash)
            if not (height and self.serves_blocks):
                missing.append(item)
                continue
            block = self.chain[height - 1]
            if self.doctor_block is not None:
                doctored = self.doctor_block(height, block)
                if doctored is not block:
                    self.lied.set()
                block = doctored
            self._send(BlockPayload(block, include_witness=True, check_validity=False))
        if missing:
            self.lied.set()
            self._send(NotFound(missing))


class Lie(StrEnum):
    """What a lying peer does, each a way Core comes to drop a peer for it."""

    PROOF_OF_WORK = "a header whose hash misses its target"
    GAP = "headers that do not follow one another"
    TARGET = "a header whose target the chain does not require"
    MUTATED_BLOCK = "a block whose body its header does not commit to"
    UNSERVED_FORK = "a chain with more work than the honest one and no blocks"


def _misses_target(header: BlockHeader, nonce: int) -> bool:
    """Set `nonce`, and answer whether the hash is above regtest's target."""
    header.nonce = nonce
    return header.hash > target_from_bits(REGTEST_POW_LIMIT_BITS)


def _hard_header(start: int, headers: list[BlockHeader]) -> list[BlockHeader]:
    """End the batch with a header that misses the target it carries."""
    middle = len(headers) // 2
    parent = headers[middle - 1]
    forged = BlockHeader(
        parent.version,
        parent.hash,
        secrets.token_bytes(32),
        parent.time + timedelta(seconds=1),
        REGTEST_POW_LIMIT_BITS,
        0,
        check_validity=False,
    )
    # a regtest hash misses its target about half the time, so the search
    # is short; a filter, so that no branch of it depends on the first nonce
    forged.nonce = next(filter(lambda n: _misses_target(forged, n), count()))
    return [*headers[:middle], forged]


def _gap(start: int, headers: list[BlockHeader]) -> list[BlockHeader]:
    """Leave out the second header."""
    return [headers[0], *headers[2:]]


def _wrong_target(start: int, headers: list[BlockHeader]) -> list[BlockHeader]:
    """End the batch with a solved header whose target the chain refuses."""
    middle = len(headers) // 2
    parent = headers[middle - 1]
    forged = BlockHeader(
        parent.version,
        parent.hash,
        secrets.token_bytes(32),
        parent.time + timedelta(seconds=1),
        _WRONG_BITS,
        0,
        check_validity=False,
    )
    brute_force_nonce(forged)
    return [*headers[:middle], forged]


_HEADER_LIES: dict[Lie, DoctorHeaders] = {
    Lie.PROOF_OF_WORK: _hard_header,
    Lie.GAP: _gap,
    Lie.TARGET: _wrong_target,
}


def _mutate(block: Block) -> Block:
    """Repeat the first transaction, which the merkle root rules out."""
    return Block(
        block.header, [*block.transactions, block.transactions[0]], check_validity=False
    )


def a_peer(
    node: Node,
    lie: Lie | None,
    honest: Sequence[Block],
    fork: Sequence[Block],
    *,
    lies_from: int,
    batch: int,
) -> ScriptedPeer:
    """Build the peer that tells `lie`, or the honest one where it is `None`.

    A header lie starts with the first batch that begins at `lies_from`
    or later, and a block lie with the block after that height, so the
    peer is honest until then, as a peer failing late is. `fork` is
    served whole by the peer telling `Lie.UNSERVED_FORK`.
    """
    genesis = RegTest().genesis.hash
    if lie is None:
        return ScriptedPeer(node, honest, genesis=genesis, batch=batch)
    if lie is Lie.UNSERVED_FORK:
        return ScriptedPeer(
            node, fork, genesis=genesis, batch=batch, serves_blocks=False
        )
    if lie is Lie.MUTATED_BLOCK:

        def doctor_block(height: int, block: Block) -> Block:
            return _mutate(block) if height > lies_from else block

        return ScriptedPeer(
            node, honest, genesis=genesis, batch=batch, doctor_block=doctor_block
        )
    doctor = _HEADER_LIES[lie]

    def doctor_headers(start: int, headers: list[BlockHeader]) -> list[BlockHeader]:
        return doctor(start, headers) if start >= lies_from else headers

    return ScriptedPeer(
        node, honest, genesis=genesis, batch=batch, doctor_headers=doctor_headers
    )


def _arrived(node: Node, peer: ScriptedPeer, known: set[int]) -> bool:
    """Whether the node holds a connection past `known`, or dropped `peer`."""
    return peer.dropped.is_set() or bool(set(node.p2p_manager.connections) - known)


@contextmanager
def a_node_among_peers(
    tmp_path: Path,
    lies: Sequence[Lie | None],
    *,
    length: int,
    lies_from: int,
    batch: int = MAX_HEADERS_RESULTS,
) -> Iterator[tuple[Node, list[ScriptedPeer], bytes]]:
    """Start a node and one peer for each of `lies`, in that order.

    `None` is the honest peer, serving `length` blocks. Each peer is
    started once the one before it is held by the node or dropped by it,
    so the node meets them in the order given. Yields the node, the
    peers, and the honest tip. The peers are closed and the node stopped
    on exit.
    """
    genesis = RegTest().genesis.hash
    honest = generate_random_chain(length, genesis)
    fork = generate_random_chain(length + 1, genesis)
    with (
        node_context(tmp_path, allow_rpc=False, v1transport=True) as node,
        ExitStack() as stack,
    ):
        wait_until_listening(node.p2p_manager)
        peers = []
        for lie in lies:
            peer = a_peer(node, lie, honest, fork, lies_from=lies_from, batch=batch)
            known = set(node.p2p_manager.connections)
            stack.enter_context(peer)
            peers.append(peer)
            wait_until(functools.partial(_arrived, node, peer, known))
        yield node, peers, honest[-1].header.hash
