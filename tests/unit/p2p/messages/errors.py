# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib.p2p.inventory import Inventory, InventoryType
from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.p2p.messages.errors import Reject, RejectCode

MAGIC = RegTest().magic
TXID = bytes(range(32))  # not a palindrome: a symmetric hash tells nothing


def test_reject():
    msg = Reject("tx", RejectCode(0x42), "", TXID)
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Reject.parse(Message.parse(msg_bytes).payload)


def test_a_reject_puts_a_hash_on_the_wire_the_way_an_inventory_does():
    # a round trip holds whichever way round the two sides agree on;
    # this is which way round that is, and it is the one everything
    # else in the protocol uses
    reject = Reject("tx", RejectCode.invalid, "", TXID)
    inventory = Inventory(InventoryType.MSG_TX, TXID)
    assert reject.serialize()[-32:] == inventory.serialize()[-32:]
