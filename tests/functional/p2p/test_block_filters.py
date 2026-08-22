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

from btclib.block.block_filter import BasicBlockFilter, filter_header
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
from btclib_node.constants import NodeStatus, P2pConnStatus, Services
from tests.helpers import (
    generate_random_chain,
    get_random_port,
    local_addr,
    wait_until,
)

CHAIN_LENGTH = 3


class RecordingDeque(deque):
    """A message queue that also keeps what went through it.

    The client is a running node, so its own loop pops what arrives --
    reading the queue is a race against it, and a test that watched the
    queue would pass or fail on which of the two got there first. What
    is recorded here is never taken away.
    """

    def __init__(self):
        super().__init__()
        self.seen = []

    def append(self, item):
        self.seen.append(item)
        super().append(item)

    def appendleft(self, item):
        self.seen.append(item)
        super().appendleft(item)


def two_connected_nodes(tmp_path):
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
        wait_until(lambda node=node: node.p2p_manager.is_alive())

    chain = generate_random_chain(CHAIN_LENGTH, RegTest().genesis.hash)
    block_index = server.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    server.status = NodeStatus.HeaderSynced
    for block in chain:
        server.block_db.add_block(block)
        block_info = block_index.get_block_info(block.header.hash)
        block_info.downloaded = True
        block_index.insert_block_info(block_info)
    wait_until(lambda: len(block_index.active_chain) == CHAIN_LENGTH + 1)

    client.p2p_manager.messages = RecordingDeque()
    client.p2p_manager.connect(local_addr(server.p2p_port))
    wait_until(lambda: len(client.p2p_manager.connections))
    wait_until(lambda: len(server.p2p_manager.connections))
    for node in nodes:
        connection = node.p2p_manager.connections[0]
        wait_until(lambda c=connection: c.status == P2pConnStatus.Connected)
    return server, client, chain


def received(client, message_type):
    return [
        message_type.parse(payload)
        for command, payload, _ in client.p2p_manager.messages.seen
        if command == message_type.command
    ]


def answers(client, message_type, count=1):
    """Wait for the peer's answers to arrive, and parse them."""
    wait_until(lambda: len(received(client, message_type)) >= count)
    return received(client, message_type)


def ask(client, message):
    client.p2p_manager.connections[0].send(message)


def test_a_peer_is_told_this_node_serves_compact_filters(tmp_path):
    server, client, _ = two_connected_nodes(tmp_path)
    try:
        version = server.p2p_manager.connections[0].version_message
        # the client's own advertisement, read by the server: a node
        # that answers these messages says so in its version
        assert version.services & Services.compact_filters
    finally:
        server.stop()
        client.stop()


def test_the_filters_a_peer_is_sent_are_the_ones_it_asked_for(tmp_path):
    server, client, chain = two_connected_nodes(tmp_path)
    try:
        ask(
            client,
            GetCFilters(BlockFilterType.BASIC, 1, chain[-1].header.hash),
        )
        got = answers(client, CFilter, count=CHAIN_LENGTH)
        assert [msg.block_hash for msg in got] == [b.header.hash for b in chain]

        # and each is the filter of its own block, which is the whole
        # of what the message is worth: built here from the block the
        # test made, and matched against a script it pays to
        for message, block in zip(got, chain):
            block_filter = BasicBlockFilter.parse(
                message.filter_bytes, message.block_hash
            )
            paid_to = block.transactions[0].vout[0].script_pub_key.script
            assert block_filter.match(paid_to)
    finally:
        server.stop()
        client.stop()


def test_a_client_can_build_the_header_chain_from_what_it_is_sent(tmp_path):
    server, client, chain = two_connected_nodes(tmp_path)
    try:
        ask(client, GetCFHeaders(BlockFilterType.BASIC, 1, chain[-1].header.hash))
        (headers_message,) = answers(client, CFHeaders)
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
    finally:
        server.stop()
        client.stop()


def test_the_checkpoints_of_a_chain_shorter_than_the_interval(tmp_path):
    server, client, chain = two_connected_nodes(tmp_path)
    try:
        ask(client, GetCFCheckpt(BlockFilterType.BASIC, chain[-1].header.hash))
        (checkpoints,) = answers(client, CFCheckpt)
        assert checkpoints.stop_hash == chain[-1].header.hash
        # answered, and empty: a chain this short has no block at a
        # multiple of the interval
        assert not checkpoints.filter_headers
    finally:
        server.stop()
        client.stop()


def test_a_filter_type_this_node_does_not_serve_gets_no_answer(tmp_path):
    server, client, chain = two_connected_nodes(tmp_path)
    try:
        ask(client, GetCFilters(1, 1, chain[-1].header.hash))
        # and then something it does answer, so this waits on an event
        # rather than on a duration: the second answer arriving with no
        # cfilter before it is what says the first was refused
        ask(client, GetCFCheckpt(BlockFilterType.BASIC, chain[-1].header.hash))
        answers(client, CFCheckpt)
        assert not received(client, CFilter)
    finally:
        server.stop()
        client.stop()


def test_the_header_a_peer_derives_is_the_one_a_client_computes(tmp_path):
    # the cross-check that does not go through the node at all: the
    # filters of the blocks, chained from the genesis block the way
    # BIP157 defines it, are what the server says they are
    server, client, chain = two_connected_nodes(tmp_path)
    try:
        filter_index = server.chainstate.filter_index
        header = filter_index.get_header(RegTest().genesis.hash)
        for block in chain:
            block_filter = BasicBlockFilter.parse(
                filter_index.get_filter(block.header.hash), block.header.hash
            )
            header = filter_header(block_filter.hash, header)
            assert header == filter_index.get_header(block.header.hash)
    finally:
        server.stop()
        client.stop()
