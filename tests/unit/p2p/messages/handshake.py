# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.constants import ProtocolVersion
from btclib_node.p2p.messages.handshake import Version
from tests.helpers import local_addr

MAGIC = RegTest().magic


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
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Version.parse(Message.parse(msg_bytes).payload)


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
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Version.parse(Message.parse(msg_bytes).payload)
