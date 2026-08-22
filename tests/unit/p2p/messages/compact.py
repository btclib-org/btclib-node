# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from datetime import UTC, datetime

from btclib.block import BlockHeader
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.p2p.message import Message
from btclib.script import script
from btclib.tx.tx import Tx as TxData
from btclib.tx.tx import TxIn, TxOut
from btclib.tx.tx_in import OutPoint

from btclib_node.chains import RegTest
from btclib_node.p2p.messages.compact import Cmpctblock, Sendcmpct
from tests.helpers import brute_force_nonce

MAGIC = RegTest().magic


def test_sendcmpct():
    msg = Sendcmpct(1, 1)
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Sendcmpct.deserialize(Message.parse(msg_bytes).payload)
    msg = Sendcmpct(0, 1)
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Sendcmpct.deserialize(Message.parse(msg_bytes).payload)


def test_cmpctblock():
    transactions = []
    for x in range(10):
        tx_in = TxIn(
            prev_out=OutPoint(),
            script_sig=script.serialize([f"{x}{x}"]),
            sequence=0xFFFFFFFF,
        )
        tx_out = TxOut(
            value=50 * 10**8,
            script_pub_key=script.serialize([f"{x}{x}"]),
        )
        tx = TxData(
            version=1,
            lock_time=0,
            vin=[tx_in],
            vout=[tx_out],
        )
        transactions.append(tx)
    header = BlockHeader(
        version=1,
        previous_block_hash="00" * 32,
        merkle_root="00" * 32,
        time=datetime.fromtimestamp(1231006506, UTC),
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)
    msg = Cmpctblock(
        header,
        1,
        [b"\x00" * 6 for x in range(10)],
        [(x, transactions[x]) for x in range(10)],
    )
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Cmpctblock.deserialize(Message.parse(msg_bytes).payload)
