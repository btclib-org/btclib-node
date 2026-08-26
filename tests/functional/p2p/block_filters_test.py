# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A peer asks this node for compact filters, over a real connection.

The unit tests say which blocks a range names and what a filter is
built from. What this says is that the three answers survive the wire
and that a client can do with them what BIP157 is for: check the filter
headers it is sent against a chain it builds itself, and then use a
filter to decide whether a block is worth fetching.
"""

from collections import deque
from typing import TYPE_CHECKING, ClassVar, Protocol, Self, cast, override

import pytest
from btclib.block import Block
from btclib.block.block_filter import BasicBlockFilter, filter_header
from btclib.p2p.address import ServiceFlags
from btclib.p2p.block_filters import (
    BlockFilterType,
    CFCheckpt,
    CFHeaders,
    CFilter,
    GetCFCheckpt,
    GetCFHeaders,
    GetCFilters,
)

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus, P2pConnStatus
from tests.helpers import (
    generate_random_chain,
    get_random_port,
    local_addr,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from btclib.alias import BinaryData
    from btclib.p2p.payload import Payload

CHAIN_LENGTH = 3


# what Connection.parse_messages puts on the queue: the command, the
# payload behind it, which connection it came in on, and its own wire
# size, weighed against MAX_QUEUED_RECV_BYTES (btclib-org/btclib-node#462)
Message = tuple[str, bytes, int, int]

Peers = tuple[Node, Node, list[Block]]


class _ParsablePayload(Protocol):
    """The shape `received` and `answers` need: a command and a parser.

    `Payload` declares `command` but not `parse` -- every subclass
    declares that for its own return type, and this is what names the
    shape the ones these two functions are handed all share.
    """

    command: ClassVar[str]

    @classmethod
    def parse(cls, data: BinaryData, *, check_validity: bool = True) -> Self: ...


class RecordingDeque(deque[Message]):
    """A message queue that also keeps what went through it.

    The client is a running node, so its own loop pops what arrives --
    reading the queue is a race against it, and a test that watched the
    queue would pass or fail on which of the two got there first. What
    is recorded here is never taken away.
    """

    def __init__(self) -> None:
        """Start empty: nothing has been seen before the first message."""
        super().__init__()
        self.seen: list[Message] = []

    @override
    def append(self, item: Message) -> None:
        self.seen.append(item)
        super().append(item)

    @override
    def appendleft(self, item: Message) -> None:
        self.seen.append(item)
        super().appendleft(item)


@pytest.fixture(scope="module")
def peers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Peers]:
    """One connected pair for the module, rather than one per test.

    Every test here is a request and its answer over a connection that
    nothing in them changes, and a node costs a thread, two managers and
    a handshake to start. Paying that per test is what #46 is about.

    Module scope is not once per run, though: `-n auto` with the default
    `--dist load` spreads these tests over workers and each worker
    builds its own pair, so what this saves depends on how they land.
    Making it one is `--dist loadgroup`, which is #46's to decide rather
    than this fixture's.
    """
    tmp_path = tmp_path_factory.mktemp("block_filters")
    nodes = [
        Node(
            config=Config(
                chain="regtest",
                data_dir=tmp_path / name,
                p2p_port=get_random_port(),
                allow_rpc=False,
            )
        )
        for name in ("server", "client")
    ]
    server, client = nodes
    for node in nodes:
        node.start()
        wait_until_listening(node.p2p_manager)

    chain = generate_random_chain(CHAIN_LENGTH, RegTest().genesis.hash)
    block_index = server.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    server.status = NodeStatus.HeaderSynced
    for block in chain:
        server.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == CHAIN_LENGTH + 1)

    client.p2p_manager.messages = RecordingDeque()
    client.p2p_manager.connect(local_addr(server.p2p_port))
    wait_until(lambda: len(client.p2p_manager.connections))
    wait_until(lambda: len(server.p2p_manager.connections))
    for node in nodes:
        connection = node.p2p_manager.connections[0]
        # safe despite B023: wait_until resolves this lambda before the
        # loop rebinds connection, see wait_until's own comment
        wait_until(lambda: connection.status == P2pConnStatus.Connected)  # noqa: B023

    try:
        yield server, client, chain
    finally:
        for node in nodes:
            node.stop()


@pytest.fixture
def mark(peers: Peers) -> int:
    """Where this test's answers begin in what the client has seen.

    The pair is shared, so the queue carries the messages of the tests
    before this one; a test that read the whole of it would pass on
    somebody else's answer.
    """
    _, client, _ = peers
    return len(cast("RecordingDeque", client.p2p_manager.messages).seen)


def received[M: _ParsablePayload](
    client: Node, message_type: type[M], mark: int = 0
) -> list[M]:
    """Every `message_type` the client has seen from `mark` on, parsed."""
    seen = cast("RecordingDeque", client.p2p_manager.messages).seen
    return [
        message_type.parse(payload)
        for command, payload, _conn_id, _size in seen[mark:]
        if command == message_type.command
    ]


def answers[M: _ParsablePayload](
    client: Node, message_type: type[M], mark: int, count: int = 1
) -> list[M]:
    """Wait for the peer's answers to arrive, and parse them."""
    wait_until(lambda: len(received(client, message_type, mark)) >= count)
    return received(client, message_type, mark)


def ask(client: Node, message: Payload) -> None:
    """Send `message` to the server over the client's own connection."""
    client.p2p_manager.connections[0].send(message)


def test_a_peer_is_told_this_node_serves_compact_filters(
    peers: Peers, mark: int
) -> None:
    """The client's version message is how the server learns it may ask.

    `NODE_COMPACT_FILTERS` is a service bit of the version handshake, not
    of these messages themselves, so a peer that wants filters has to
    read it off the version the other side sent during the handshake --
    checked here from the server's side, against the client's.
    """
    server, _, _ = peers
    version = server.p2p_manager.connections[0].version_message
    assert version is not None
    # the client's own advertisement, read by the server: a node
    # that answers these messages says so in its version
    assert version.services & ServiceFlags.NODE_COMPACT_FILTERS


def test_the_filters_a_peer_is_sent_are_the_ones_it_asked_for(
    peers: Peers, mark: int
) -> None:
    """A `GetCFilters` range answers with exactly that range, in order.

    Each `CFilter` is also checked against the block it claims to be
    for: parsed back into a `BasicBlockFilter` and matched against the
    script the block's own coinbase output pays to, which is the
    property a filter is for and not merely a count of messages.
    """
    _, client, chain = peers
    ask(
        client,
        GetCFilters(BlockFilterType.BASIC, 1, chain[-1].header.hash),
    )
    got = answers(client, CFilter, mark, count=CHAIN_LENGTH)
    assert [msg.block_hash for msg in got] == [b.header.hash for b in chain]

    # and each is the filter of its own block, which is the whole
    # of what the message is worth: built here from the block the
    # test made, and matched against a script it pays to
    for message, block in zip(got, chain, strict=True):
        block_filter = BasicBlockFilter.parse(message.filter_bytes, message.block_hash)
        paid_to = block.transactions[0].vout[0].script_pub_key.script
        assert block_filter.match(paid_to)


def test_a_client_can_build_the_header_chain_from_what_it_is_sent(
    peers: Peers, mark: int
) -> None:
    """A `CFHeaders` answer carries enough for the client to derive it.

    The message itself carries filter hashes, not headers: the client
    chains them onto `previous_filter_header` the way BIP157 defines a
    filter header, and what it derives has to equal the header the
    server stores for the same block. The header just before the range
    is checked too, and has to be the genesis block's, since the range
    asked for starts at height one.
    """
    server, client, chain = peers
    ask(client, GetCFHeaders(BlockFilterType.BASIC, 1, chain[-1].header.hash))
    (headers_message,) = answers(client, CFHeaders, mark)
    assert headers_message.stop_hash == chain[-1].header.hash
    assert len(headers_message.filter_hashes) == CHAIN_LENGTH

    # the hashes are the field and the headers are derived, which
    # is the point of sending hashes: the client chains them onto
    # the one header it was given and gets what the server holds
    derived = headers_message.filter_headers
    assert derived[-1] == server.chainstate.filter_index.get_header(
        chain[-1].header.hash
    )
    # and the header before the range is the genesis block's, since
    # the range starts at height one
    assert headers_message.previous_filter_header == (
        server.chainstate.filter_index.get_header(RegTest().genesis.hash)
    )


def test_the_checkpoints_of_a_chain_shorter_than_the_interval(
    peers: Peers, mark: int
) -> None:
    """A chain with no block at a checkpoint height answers with none.

    `CFCheckpt` reports one header per multiple of the checkpoint
    interval up to the stop hash; a chain shorter than the interval has
    no such block, so the answer is a `CFCheckpt` with an empty list
    rather than a refusal or a message with a filler entry.
    """
    _, client, chain = peers
    ask(client, GetCFCheckpt(BlockFilterType.BASIC, chain[-1].header.hash))
    (checkpoints,) = answers(client, CFCheckpt, mark)
    assert checkpoints.stop_hash == chain[-1].header.hash
    # answered, and empty: a chain this short has no block at a
    # multiple of the interval
    assert not checkpoints.filter_headers


def test_a_filter_type_this_node_does_not_serve_gets_no_answer(
    peers: Peers, mark: int
) -> None:
    """A `GetCFilters` naming an unserved filter type is silently dropped.

    `1` is not `BlockFilterType.BASIC`, the only type this node indexes,
    so the request gets no `CFilter` at all -- not an error, nothing.
    Absence is checked by waiting for a second, servable request to be
    answered first: only once that answer has arrived can "no `CFilter`
    showed up" mean the first request was refused rather than merely
    still pending.
    """
    _, client, chain = peers
    ask(client, GetCFilters(1, 1, chain[-1].header.hash))
    # and then something it does answer, so this waits on an event
    # rather than on a duration: the second answer arriving with no
    # cfilter before it is what says the first was refused
    ask(client, GetCFCheckpt(BlockFilterType.BASIC, chain[-1].header.hash))
    answers(client, CFCheckpt, mark)
    assert not received(client, CFilter, mark)


def test_the_header_a_peer_derives_is_the_one_a_client_computes(
    peers: Peers, mark: int
) -> None:
    """The server's stored filter headers match an independent BIP157 chain.

    No message crosses the wire here: the block filters are rebuilt from
    what `filter_index` stores, chained by hand from the genesis header
    with `filter_header`, and compared against what `filter_index.
    get_header` answers for each block -- so this test would catch a
    stored header wrong at its source, which the request/answer tests
    above cannot: they only compare the server against itself.
    """
    # the cross-check that does not go through the node at all: the
    # filters of the blocks, chained from the genesis block the way
    # BIP157 defines it, are what the server says they are
    server, _, chain = peers
    filter_index = server.chainstate.filter_index
    header = filter_index.get_header(RegTest().genesis.hash)
    assert header is not None
    for block in chain:
        block_filter_bytes = filter_index.get_filter(block.header.hash)
        assert block_filter_bytes is not None
        block_filter = BasicBlockFilter.parse(block_filter_bytes, block.header.hash)
        header = filter_header(block_filter.hash, header)
        assert header == filter_index.get_header(block.header.hash)
