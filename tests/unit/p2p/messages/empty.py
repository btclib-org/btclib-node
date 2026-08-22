# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.p2p.messages.empty import (
    Getaddr,
    Mempool,
    Sendheaders,
    Wtxidrelay,
)

MAGIC = RegTest().magic


def test_sendheaders():
    msg = Sendheaders()
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Sendheaders.parse(Message.parse(msg_bytes).payload)


def test_mempool():
    msg = Mempool()
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Mempool.parse(Message.parse(msg_bytes).payload)


def test_getaddr():
    msg = Getaddr()
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Getaddr.parse(Message.parse(msg_bytes).payload)


def test_wtxidrelay():
    msg = Wtxidrelay()
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Wtxidrelay.parse(Message.parse(msg_bytes).payload)
