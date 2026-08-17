from datetime import datetime, timezone

from btclib.block import Block as BlockData
from btclib.block import BlockHeader
from btclib.hashes import hash256, merkle_root
from btclib.script import script
from btclib.tx.tx import Tx as TxData
from btclib.tx.tx import TxIn, TxOut
from btclib.tx.tx_in import OutPoint

from btclib_node.p2p.messages import get_payload
from btclib_node.p2p.messages.data import Block, Blocktxn, Headers, Inv, Tx
from tests.helpers import brute_force_nonce


def test_tx():
    tx_in = TxIn(
        prev_out=OutPoint(),
        script_sig=script.serialize(["00"]),
        sequence=0xFFFFFFFF,
    )
    tx_out = TxOut(
        value=50 * 10**8,
        script_pub_key=script.serialize(["00"]),
    )
    tx = TxData(
        version=1,
        lock_time=0,
        vin=[tx_in],
        vout=[tx_out],
    )
    msg = Tx(tx)
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Tx.deserialize(get_payload(msg_bytes)[1])


def test_block():
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
    header.merkle_root = merkle_root(
        [tx.serialize(False) for tx in transactions], hash256
    )[::-1]
    msg = Block(BlockData(header, transactions))
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Block.deserialize(get_payload(msg_bytes)[1])


def test_empty_headers():
    msg = Headers([])
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Headers.deserialize(get_payload(msg_bytes)[1])


def test_headers():
    headers = []
    for x in range(10):
        header = BlockHeader(
            version=70015,
            previous_block_hash=f"{x}{x}" * 32,
            merkle_root_="00" * 32,
            time=datetime.fromtimestamp(1231006506, timezone.utc),
            bits=b"\x20\xff\xff\xff",
            nonce=1,
            check_validity=False,
        )
        brute_force_nonce(header)
        headers.append(header)
    msg = Headers(headers)
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Headers.deserialize(get_payload(msg_bytes)[1])


def test_empty_blocktxn():
    msg = Blocktxn(b"\x00" * 32, [])
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Blocktxn.deserialize(get_payload(msg_bytes)[1])


def test_blocktxn():
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
    msg = Blocktxn(b"\x00" * 32, transactions)
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Blocktxn.deserialize(get_payload(msg_bytes)[1])


def test_empty_inv():
    msg = Inv([])
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Inv.deserialize(get_payload(msg_bytes)[1])


def test_filled_inv():
    msg = Inv([(1, b"\x00" * 32)])
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Inv.deserialize(get_payload(msg_bytes)[1])


def test_invalid_inv():
    msg = Inv([(1, b"\x00"), (1, b"\x00")])
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg != Inv.deserialize(get_payload(msg_bytes)[1])
