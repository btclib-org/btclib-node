# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.p2p.messages import get_payload
from btclib_node.p2p.messages.ping import Ping, Pong


def test_ping():
    msg = Ping(1)
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Ping.deserialize(get_payload(msg_bytes)[1])


def test_random_ping():
    msg = Ping()
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Ping.deserialize(get_payload(msg_bytes)[1])


def test_pong():
    msg = Pong(1)
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Pong.deserialize(get_payload(msg_bytes)[1])
