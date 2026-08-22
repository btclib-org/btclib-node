# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.p2p.messages.errors import Reject, RejectCode

MAGIC = RegTest().magic


def test_reject():
    msg = Reject("tx", RejectCode(0x42), "", b"\x00" * 32)
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Reject.parse(Message.parse(msg_bytes).payload)
