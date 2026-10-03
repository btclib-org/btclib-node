# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Two real nodes choose a transport as Core's do, and speak it.

Each case dials from `node2` to `node1` and reads which transport both
ends ended up on. `P2pManager.connect` takes the services the address
advertises, so a case says whether the dialled address carries
`NODE_P2P_V2`.
"""

import re
from typing import TYPE_CHECKING

import pytest
from btclib.p2p.address import ServiceFlags
from btclib.p2p.keepalive import Ping, Pong

from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.callbacks import callbacks
from btclib_node.p2p.transport import TransportProtocolType
from tests import LogLines, local_addr, wait_until, wait_until_listening
from tests.conftest import node_context

if TYPE_CHECKING:
    from pathlib import Path

    from btclib_node import Node
    from btclib_node.p2p.connection import Connection

_V2 = int(ServiceFlags.NODE_P2P_V2)

# BIP324 rekeys every 224 packets, in each direction
_REKEY_INTERVAL = 224


def _connected(node: Node) -> Connection:
    """Wait for `node`'s one connection to pass its handshake, and return it."""
    wait_until(lambda: len(node.p2p_manager.connections) == 1)
    (conn,) = node.p2p_manager.connections.values()
    wait_until(lambda: conn.status == P2pConnStatus.Connected)
    return conn


@pytest.mark.parametrize(
    ("dialler_v2", "advertised", "expected"),
    [
        # both ends offer it, and the address says so
        (True, _V2, TransportProtocolType.V2),
        # the address does not advertise it, so the dialler asks for v1
        (True, 0, TransportProtocolType.V1),
        # the dialler does not offer it, though the address does
        (False, _V2, TransportProtocolType.V1),
        # neither the dialler nor the address offers it
        (False, 0, TransportProtocolType.V1),
    ],
)
def test_the_transport_follows_what_both_ends_offer(
    tmp_path: Path,
    dialler_v2: bool,  # noqa: FBT001
    advertised: int,
    expected: TransportProtocolType,
) -> None:
    """The dialler picks v2 only where both it and the address offer it.

    The listener always offers v2, and answers a v1 dialler in v1.
    """
    with (
        node_context(tmp_path / "node1", allow_rpc=False) as node1,
        node_context(
            tmp_path / "node2", allow_rpc=False, v2transport=dialler_v2
        ) as node2,
    ):
        wait_until_listening(node1.p2p_manager)
        node2.p2p_manager.connect(local_addr(node1.p2p_port, services=advertised))
        listening = _connected(node1)
        dialling = _connected(node2)
        wait_until(
            lambda: (
                TransportProtocolType.DETECTING
                not in {
                    listening.transport.get_info().transport_type,
                    dialling.transport.get_info().transport_type,
                }
            )
        )
        assert listening.transport.get_info().transport_type is expected
        assert dialling.transport.get_info().transport_type is expected
        if expected is TransportProtocolType.V2:
            session_id = dialling.transport.get_info().session_id
            assert session_id is not None
            assert listening.transport.get_info().session_id == session_id


@pytest.mark.parametrize("by_name", [False, True], ids=["by address", "by name"])
def test_a_v2_dialler_retries_with_v1_against_a_node_without_v2transport(
    tmp_path: Path,
    *,
    by_name: bool,
) -> None:
    """The listener drops the key as a bad header; the dialler retries in v1.

    Core's `DisconnectNodes` logs the retry, and the reconnection speaks
    v1 on both ends. A dial by name resolves the name again.
    """
    with (
        node_context(tmp_path / "node1", allow_rpc=False, v2transport=False) as node1,
        node_context(tmp_path / "node2", allow_rpc=False) as node2,
    ):
        wait_until_listening(node1.p2p_manager)
        lines = LogLines()
        node2.logger.addHandler(lines)
        if by_name:
            assert node2.p2p_manager.add_added_peer(
                f"127.0.0.1:{node1.p2p_port}", use_v2transport=True
            )
        else:
            node2.p2p_manager.connect(local_addr(node1.p2p_port, services=_V2))
        listening = _connected(node1)
        dialling = _connected(node2)
        wait_until(
            lambda: re.search(
                r"retrying with v1 transport protocol for peer=\d+",
                "\n".join(lines.messages),
            )
        )
        assert listening.transport.get_info().transport_type is (
            TransportProtocolType.V1
        )
        assert dialling.transport.get_info().transport_type is (
            TransportProtocolType.V1
        )
        assert sum("retrying with v1" in m for m in lines.messages) == 1
        assert node2.p2p_manager.last_connection_id == 1


def test_a_listener_without_v1transport_refuses_a_v1_peer(tmp_path: Path) -> None:
    """The v1 dialler is dropped and the refusal logged; a v2 one is taken.

    Neither is discouraged: `node1` keeps no ban or discouragement of the
    loopback host, as the v2 dialler that follows connects.
    """
    with (
        node_context(tmp_path / "node1", allow_rpc=False, v1transport=False) as node1,
        node_context(tmp_path / "node2", allow_rpc=False, v2transport=False) as node2,
        node_context(tmp_path / "node3", allow_rpc=False) as node3,
    ):
        wait_until_listening(node1.p2p_manager)
        lines = LogLines()
        node1.logger.addHandler(lines)
        node2.p2p_manager.connect(local_addr(node1.p2p_port))
        wait_until(
            lambda: any(
                "V2 transport error: V1 peer refused (see -v1transport)" in line
                for line in lines.messages
            )
        )
        wait_until(lambda: not node2.p2p_manager.connections)
        assert not node1.p2p_manager.connections
        node3.p2p_manager.connect(local_addr(node1.p2p_port, services=_V2))
        listening = _connected(node1)
        dialling = _connected(node3)
        wait_until(
            lambda: (
                listening.transport.get_info().transport_type
                is TransportProtocolType.V2
            )
        )
        assert dialling.transport.get_info().transport_type is TransportProtocolType.V2


def test_a_v2_dialler_without_v1transport_does_not_retry_with_v1(
    tmp_path: Path,
) -> None:
    """The refusal is logged, and the node makes no second connection."""
    with (
        node_context(tmp_path / "node1", allow_rpc=False, v2transport=False) as node1,
        node_context(tmp_path / "node2", allow_rpc=False, v1transport=False) as node2,
    ):
        wait_until_listening(node1.p2p_manager)
        lines = LogLines()
        node2.logger.addHandler(lines)
        node2.p2p_manager.connect(local_addr(node1.p2p_port, services=_V2))
        wait_until(
            lambda: re.search(
                r"not retrying with v1 transport protocol for peer=\d+"
                r" \(see -v1transport\)",
                "\n".join(lines.messages),
            )
        )
        wait_until(lambda: not node2.p2p_manager.connections)
        assert not node1.p2p_manager.connections
        assert not any(line.startswith("retrying with v1") for line in lines.messages)
        assert sum("Dialled" in line for line in lines.messages) == 1


def test_messages_cross_the_rekey_in_both_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More than a rekey interval of `ping`s and their `pong`s is read.

    `node2` sends the `ping`s one after another from this thread and
    `node1` answers each with a `pong`, so both directions pass the
    rekey, with whatever the two nodes send beside them in between.
    """
    count = _REKEY_INTERVAL + 76
    nonces = range(1_000_000, 1_000_000 + count)
    pings: set[int] = set()
    pongs: set[int] = set()
    ping_callback = callbacks["ping"]
    pong_callback = callbacks["pong"]

    def counting_ping(node: Node, msg: bytes, conn: Connection) -> None:
        ping_callback(node, msg, conn)
        pings.add(Ping.parse(msg).nonce)

    def counting_pong(node: Node, msg: bytes, conn: Connection) -> None:
        pong_callback(node, msg, conn)
        pongs.add(Pong.parse(msg).nonce)

    monkeypatch.setitem(callbacks, "ping", counting_ping)
    monkeypatch.setitem(callbacks, "pong", counting_pong)
    with (
        node_context(tmp_path / "node1", allow_rpc=False) as node1,
        node_context(tmp_path / "node2", allow_rpc=False) as node2,
    ):
        wait_until_listening(node1.p2p_manager)
        node2.p2p_manager.connect(local_addr(node1.p2p_port, services=_V2))
        _connected(node1)
        conn = _connected(node2)
        assert conn.transport.get_info().transport_type is TransportProtocolType.V2
        for nonce in nonces:
            conn.send(Ping(nonce))
        wait_until(lambda: set(nonces) <= pings, timeout=60)
        wait_until(lambda: set(nonces) <= pongs, timeout=60)
