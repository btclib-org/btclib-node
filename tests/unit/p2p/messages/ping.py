# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.p2p.messages.ping import Ping, Pong

MAGIC = RegTest().magic


def test_ping():
    msg = Ping(1)
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Ping.deserialize(Message.parse(msg_bytes).payload)


def test_random_ping():
    msg = Ping()
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Ping.deserialize(Message.parse(msg_bytes).payload)


def test_pong():
    msg = Pong(1)
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Pong.deserialize(Message.parse(msg_bytes).payload)
