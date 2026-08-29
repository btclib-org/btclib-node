# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A pruned node, over a real connection: what it serves and what it drops.

The unit tests (`block_db_test.py`, `p2p/callbacks_test.py`, `main_test.py`)
cover the mechanism -- `BlockDB.prune_up_to`'s own deletion, `main._prune_chain`'s
own call into it, and `_below_prune_threshold`'s own disconnect decision --
each against a double standing in for the rest of the node. What this
checks is the same three things over an actual socket, between two real
`Node`s: a pruned node's own `version` on the wire, an old `getdata`
answered by dropping the connection rather than by a `notfound`, and a
recent one still answered in full.
"""

from collections import deque
from typing import TYPE_CHECKING, override

import pytest
from btclib.p2p.address import ServiceFlags
from btclib.p2p.data import BlockPayload as BlockMsg
from btclib.p2p.inventory import GetData, Inventory, InventoryType

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import MIN_BLOCKS_TO_KEEP, NodeStatus, P2pConnStatus
from tests import (
    generate_random_chain,
    get_random_port,
    local_addr,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# Eight blocks past the retained depth: enough for `main._prune_chain`
# to have deleted at least one real block (heights 1..8) and kept the
# rest (9 on), without building a chain the size `test_download`'s own
# 3000 is, which this test has no need of.
_CHAIN_LENGTH = MIN_BLOCKS_TO_KEEP + 8

# What Connection.parse_messages puts on the queue: the command, the
# payload behind it, which connection it came in on, and its own wire
# size -- the same shape block_filters_test.py's own Message names.
_Message = tuple[str, bytes, int, int]


class _RecordingDeque(deque[_Message]):
    """A message queue that also keeps what went through it.

    The client is a running node, so its own loop pops and processes
    what arrives -- reading the live queue after sending a request is a
    race against that thread, which `block_filters_test.py`'s own
    identically-shaped class is where this pattern is argued in full.
    What is recorded here is never taken away.
    """

    def __init__(self) -> None:
        """Start empty: nothing has been seen before the first message."""
        super().__init__()
        self.seen: list[_Message] = []

    @override
    def append(self, item: _Message) -> None:
        self.seen.append(item)
        super().append(item)

    @override
    def appendleft(self, item: _Message) -> None:
        self.seen.append(item)
        super().appendleft(item)


@pytest.fixture
def pruned_server_and_client(tmp_path: Path) -> Iterator[tuple[Node, Node]]:
    """A pruned, fully-synced server and a plain client, connected.

    The server is built `pruned=True` and driven by its own running
    loop -- headers added and every block marked downloaded, the same
    shape `block_filters_test.py`'s own `peers` fixture uses -- so
    `update_chain` connects the whole chain, and `main._prune_chain`
    prunes behind it, through the server's real thread rather than by
    calling either function directly.
    """
    server = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "server",
            p2p_port=get_random_port(),
            allow_rpc=False,
            pruned=True,
        )
    )
    client = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "client",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    # The handshake alone (`callbacks.verack`) sends a real `getheaders`,
    # and once the answer indexes past `HeaderSynced`,
    # `DownloadManager.block_download` would start requesting real
    # blocks on its own -- eventually the pruned ones too, disconnecting
    # the client out from under these tests before they ever send their
    # own `GetData`. Silencing `step` keeps every `block` message these
    # tests see traceable to the one `GetData` each of them sends by
    # hand.
    client.download_manager.step = lambda: None
    server.start()
    client.start()
    wait_until_listening(server.p2p_manager)
    wait_until_listening(client.p2p_manager)

    chain = generate_random_chain(_CHAIN_LENGTH, RegTest().genesis.hash)
    block_index = server.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    server.status = NodeStatus.HeaderSynced
    for block in chain:
        server.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == _CHAIN_LENGTH + 1)
    wait_until(lambda: server.block_db.pruned_up_to >= 0)

    client.p2p_manager.messages = _RecordingDeque()
    client.p2p_manager.connect(local_addr(server.p2p_port))
    wait_until(lambda: len(client.p2p_manager.connections))
    connection = client.p2p_manager.connections[0]
    wait_until(lambda: connection.status == P2pConnStatus.Connected)

    try:
        yield server, client
    finally:
        server.stop()
        client.stop()


def test_a_pruned_server_advertises_node_network_limited_only(
    pruned_server_and_client: tuple[Node, Node],
) -> None:
    """A pruned node's own `version`, read off the client's connection.

    `NODE_NETWORK_LIMITED` set, `NODE_NETWORK` dropped -- what the
    client actually received over the wire, not what the server's own
    `send_version` was asked to build.
    """
    _server, client = pruned_server_and_client
    version = client.p2p_manager.connections[0].version_message
    assert version is not None
    assert version.services & ServiceFlags.NODE_NETWORK_LIMITED
    assert not version.services & ServiceFlags.NODE_NETWORK


def test_a_pruned_server_serves_a_block_it_still_holds(
    pruned_server_and_client: tuple[Node, Node],
) -> None:
    """The tip itself, well inside the retained depth, is served whole."""
    server, client = pruned_server_and_client
    tip_hash = server.chainstate.block_index.active_chain[-1]
    connection = client.p2p_manager.connections[0]
    connection.send(GetData([Inventory(InventoryType.MSG_BLOCK, tip_hash)]))

    payload = _wait_for_message(client, "block")
    # regtest's own easy target fails btclib's default (mainnet) proof-
    # of-work check -- callbacks.block's own comment argues the same
    # unchecked-then-assert shape against this chain's own limit
    block = BlockMsg.parse(payload, check_validity=False).block
    assert block.header.hash == tip_hash
    assert connection.status == P2pConnStatus.Connected


def test_a_pruned_server_drops_a_client_asking_for_a_pruned_block(
    pruned_server_and_client: tuple[Node, Node],
) -> None:
    """A `getdata` for a block behind the retained depth ends the connection.

    Matches Core's own `ProcessGetBlockData`: the peer is dropped
    rather than told `notfound`, since a pruned node is not to be
    relied on for what it has already told this same peer, through its
    own `NODE_NETWORK_LIMITED`-only `version`, that it might not have.
    """
    server, client = pruned_server_and_client
    old_hash = server.chainstate.block_index.active_chain[1]
    connection = client.p2p_manager.connections[0]
    connection.send(GetData([Inventory(InventoryType.MSG_BLOCK, old_hash)]))

    # Not `not client.p2p_manager.connections`: this same client redials
    # the server on its own once the connection drops -- neither side
    # named the other through `-connect`/`-addnode`, so
    # `P2pManager._maybe_dial_more_peers` draws a fresh outbound
    # connection from the address the handshake already gossiped it,
    # same as a real client would. What the disconnect actually did is
    # on *this* connection object, not on the dict's own membership.
    wait_until(lambda: connection.status == P2pConnStatus.Closed)


def _wait_for_message(node: Node, command: str, timeout: float = 20) -> bytes:
    """Wait for and return the payload of the first `command` message seen.

    Reads `_RecordingDeque.seen`, not the live queue: `Node.run`'s own
    loop drains the queue as fast as it can, so a read racing that
    thread can miss an entry that already arrived and already left.
    """
    seen = cast_recording(node).seen

    def found() -> bool:
        return any(item[0] == command for item in seen)

    wait_until(found, timeout=timeout)
    return next(item[1] for item in seen if item[0] == command)


def cast_recording(node: Node) -> _RecordingDeque:
    """Narrow `node.p2p_manager.messages` back to the `_RecordingDeque` it is.

    Declared `deque[Message]` on `P2pManager` itself; every caller here
    only ever hands it a `_RecordingDeque`, through this fixture's own
    swap above.
    """
    messages = node.p2p_manager.messages
    assert isinstance(messages, _RecordingDeque)
    return messages
