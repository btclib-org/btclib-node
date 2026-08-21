# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.p2p.messages import get_payload
from btclib_node.p2p.messages.errors import Notfound, Reject, RejectCode


def test_not_found():
    msg = Notfound([(1, b"\x00" * 32)])
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Notfound.deserialize(get_payload(msg_bytes)[1])


def test_reject():
    msg = Reject("tx", RejectCode(0x42), "", b"\x00" * 32)
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Reject.deserialize(get_payload(msg_bytes)[1])
