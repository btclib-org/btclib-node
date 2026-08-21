# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.constants import ProtocolVersion
from btclib_node.p2p.messages import get_payload
from btclib_node.p2p.messages.handshake import Verack, Version
from tests.helpers import local_addr


def test_verack():
    msg = Verack()
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Verack.deserialize(get_payload(msg_bytes)[1])


def test_version():
    services = 1032 + 1
    msg = Version(
        version=ProtocolVersion,
        services=services,
        timestamp=1,
        addr_recv=local_addr(1),
        addr_from=local_addr(1, services=services),
        nonce=1,
        user_agent="/Btclib/",
        start_height=0,
        relay=True,
    )
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Version.deserialize(get_payload(msg_bytes)[1])


def test_version_without_agent():
    services = 1032 + 1
    msg = Version(
        version=ProtocolVersion,
        services=services,
        timestamp=1,
        addr_recv=local_addr(1),
        addr_from=local_addr(1, services=services),
        nonce=1,
        user_agent="",
        start_height=0,
        relay=True,
    )
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Version.deserialize(get_payload(msg_bytes)[1])
