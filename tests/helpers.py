import random
import secrets
import socket
import time
from datetime import datetime, timezone

from btclib.block import Block, BlockHeader
from btclib.exceptions import BTClibValueError
from btclib.hashes import hash256, merkle_root
from btclib.script import script
from btclib.tx.tx import Tx, TxIn, TxOut
from btclib.tx.tx_in import OutPoint

from btclib_node.p2p.address import NetworkAddress


def generate_random_header_chain(length, start):
    # random.seed(42)
    chain = []
    for x in range(length):
        if chain:
            previous_block_hash = chain[-1].hash
        else:
            previous_block_hash = start
        header = BlockHeader(
            version=70015,
            previous_block_hash=previous_block_hash,
            merkle_root_=secrets.token_bytes(32),
            time=datetime.fromtimestamp(1231006505 + x + 1, timezone.utc),
            bits=b"\x20\xff\xff\xff",
            nonce=1,
            check_validity=False,
        )
        brute_force_nonce(header)
        chain.append(header)
    return chain


def generate_random_transaction(prevouthash=None):
    prevouthash = prevouthash if prevouthash else secrets.token_bytes(32)
    tx_in = TxIn(
        prev_out=OutPoint(prevouthash, 0),
        script_sig=script.serialize([secrets.token_bytes(32)]),
        sequence=0xFFFFFFFF,
    )
    tx_out = TxOut(
        value=50 * 10**8,
        script_pub_key=script.serialize([secrets.token_bytes(32)]),
    )
    tx = Tx(
        version=1,
        lock_time=0,
        vin=[tx_in],
        vout=[tx_out],
    )
    return tx


def generate_random_chain(length, start):
    # random.seed(42)
    chain = []
    for x in range(length):
        previous_block_hash = chain[-1].header.hash if chain else start
        coinbase_in = TxIn(
            prev_out=OutPoint(),
            script_sig=script.serialize([secrets.token_bytes(32)]),
            sequence=0xFFFFFFFF,
        )
        coinbase_out = TxOut(
            value=50 * 10**8,
            script_pub_key=script.serialize([secrets.token_bytes(32)]),
        )
        coinbase = Tx(
            version=1,
            lock_time=0,
            vin=[coinbase_in],
            vout=[coinbase_out],
        )
        transactions = [coinbase]
        if chain:
            tx = generate_random_transaction(chain[x - 1].transactions[0].id)
            transactions.append(tx)
        header = BlockHeader(
            version=70015,
            previous_block_hash=previous_block_hash,
            merkle_root_=merkle_root(
                [tx.serialize(True, False) for tx in transactions], hash256
            )[::-1],
            time=datetime.fromtimestamp(1231006505 + x + 1, timezone.utc),
            bits=b"\x20\xff\xff\xff",
            nonce=1,
            check_validity=False,
        )
        brute_force_nonce(header)
        block = Block(header, transactions)
        chain.append(block)
    return chain


def get_random_port():
    while True:
        port = random.randint(1024, 65535)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("", port))
            sock.close()
            return port
        except OSError:
            sock.close()


def wait_until(func, timeout=2):
    start = time.time()
    while time.time() - start < timeout:
        if func():
            return
        time.sleep(0.025)
    raise Exception


def brute_force_nonce(header):
    for _ in range(100):
        try:
            header.assert_valid_pow()
            break
        except BTClibValueError:
            header.nonce += 1
    header.assert_valid()


def local_addr(port: int, time: int = 0, services: int = 0):
    return NetworkAddress.from_ip_and_port("0.0.0.0", port, time, services)
