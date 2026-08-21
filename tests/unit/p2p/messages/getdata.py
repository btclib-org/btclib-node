# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.p2p.messages.getdata import (
    Getblocks,
    Getblocktxn,
    Getdata,
    Getheaders,
    Mempool,
    Sendheaders,
)

MAGIC = RegTest().magic


def test_sendheaders():
    msg = Sendheaders()
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Sendheaders.deserialize(Message.parse(msg_bytes).payload)


def test_mempool():
    msg = Mempool()
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Mempool.deserialize(Message.parse(msg_bytes).payload)


def test_getdata():
    msg = Getdata([(1, b"\x00" * 32)])
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Getdata.deserialize(Message.parse(msg_bytes).payload)


def test_getblocks():
    msg = Getblocks(70015, [b"\x00" * 32, b"\x11" * 32], b"\x00" * 32)
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Getblocks.deserialize(Message.parse(msg_bytes).payload)


def test_getheaders():
    msg = Getheaders(70015, [b"\x00" * 32, b"\x11" * 32], b"\x00" * 32)
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Getheaders.deserialize(Message.parse(msg_bytes).payload)


def test_getblocktxn():
    msg = Getblocktxn(b"\x00" * 32, [2**x for x in range(10)])
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Getblocktxn.deserialize(Message.parse(msg_bytes).payload)
