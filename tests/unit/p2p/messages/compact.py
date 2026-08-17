from datetime import datetime, timezone

from btclib.block import BlockHeader
from btclib.script import script
from btclib.tx.tx import Tx as TxData
from btclib.tx.tx import TxIn, TxOut
from btclib.tx.tx_in import OutPoint

from btclib_node.p2p.messages import get_payload
from btclib_node.p2p.messages.compact import Cmpctblock, Sendcmpct
from tests.helpers import brute_force_nonce


def test_sendcmpt():
    msg = Sendcmpct(1, 1)
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Sendcmpct.deserialize(get_payload(msg_bytes)[1])
    msg = Sendcmpct(0, 1)
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Sendcmpct.deserialize(get_payload(msg_bytes)[1])


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
        merkle_root_="00" * 32,
        time=datetime.fromtimestamp(1231006506, timezone.utc),
        bits=b"\x20\xff\xff\xff",
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
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Cmpctblock.deserialize(get_payload(msg_bytes)[1])
