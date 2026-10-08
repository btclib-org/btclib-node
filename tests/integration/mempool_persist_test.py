# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`mempool.dat` written by this node and read by bitcoind, and the reverse.

Each file is compared byte for byte with the other side's, once written
again with that side's obfuscation key: the key is random, and the rest
of the file follows from its content.
"""

import secrets
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.mempool_persist import read_mempool_file, serialize_mempool
from btclib_node.p2p.address import peer_address
from tests import (
    anyone_can_spend,
    anyone_can_spend_script_sig,
    get_random_port,
    rpc_client,
    wait_until,
    wait_until_listening,
)
from tests.integration.reorg_test import a_chain, submit

if TYPE_CHECKING:
    from tests.integration.conftest import Bitcoind

# what bitcoind's `importmempool` takes over from the file: everything
_ALL = {
    "use_current_time": False,
    "apply_fee_delta_priority": True,
    "apply_unbroadcast_set": True,
}


def a_spend(spent: tuple[Tx, int], outputs: int, fee: int) -> Tx:
    """Return a spend of one output into `outputs` equal ones, paying `fee`."""
    tx, vout = spent
    value = (tx.vout[vout].value - fee) // outputs
    return Tx(
        version=2,
        lock_time=0,
        vin=[TxIn(OutPoint(tx.id, vout), anyone_can_spend_script_sig(), 0xFFFFFFFF)],
        vout=[TxOut(value, anyone_can_spend()) for _ in range(outputs)],
    )


def started(data_dir: Path) -> Node:
    """Start a regtest node on `data_dir`."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=data_dir,
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
        )
    )
    node.start()
    wait_until_listening(node.rpc_manager)
    return node


def rewritten(theirs: bytes, ours: bytes) -> bytes:
    """Return the file `ours` holds, written again with the key of `theirs`."""
    content = read_mempool_file(ours)
    key = read_mempool_file(theirs).key
    return serialize_mempool(content.entries, content.deltas, content.unbroadcast, key)


def test_each_node_reads_the_file_the_other_writes(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """The file this node writes at shutdown, and the one bitcoind saves.

    Core's `CompareMainOrder`: `parent` and `child` are one chunk, written
    before `first`, which pays less than the two together. bitcoind
    imports this node's file with everything in it and saves it back
    unchanged; then this node loads bitcoind's file, with deltas bitcoind
    added, and writes it back unchanged.
    """
    chain = a_chain(100)
    submit(bitcoind, chain)
    root = a_spend((chain[0].transactions[0], 0), 3, 200_000)
    first = a_spend((root, 0), 1, 30_000)
    parent = a_spend((root, 1), 1, 20_000)
    child = a_spend((parent, 0), 1, 60_000)
    last = a_spend((root, 2), 1, 5_000)
    data_dir = tmp_path / "node"

    node = started(data_dir)
    try:
        node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))
        block_index = node.chainstate.block_index
        wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)
        # so that nothing this node holds reaches bitcoind but the file
        bitcoind.rpc("setnetworkactive", [False])
        client = rpc_client(node)
        wait_until(lambda: client.call("getconnectioncount") == 0)
        for tx in (root, first, parent, child, last):
            client.call(
                "sendrawtransaction", [tx.serialize(include_witness=True).hex()]
            )
        client.call("prioritisetransaction", [first.id.hex(), None, 1_000])
        client.call("prioritisetransaction", [secrets.token_hex(32), None, -7])
    finally:
        node.stop()
    path = node.data_dir / "mempool.dat"
    ours = path.read_bytes()

    assert bitcoind.rpc("importmempool", [str(path), _ALL]) == {}
    saved = cast("Any", bitcoind.rpc("savemempool"))["filename"]
    theirs = Path(saved).read_bytes()
    assert rewritten(theirs, ours) == theirs
    assert len(read_mempool_file(theirs).entries) == 5

    bitcoind.rpc("prioritisetransaction", [last.id.hex(), None, 3_000])
    bitcoind.rpc("prioritisetransaction", [secrets.token_hex(32), None, 11])
    bitcoind.rpc("savemempool")
    shutil.copyfile(saved, path)
    theirs = path.read_bytes()

    node = started(data_dir)
    try:
        client = rpc_client(node)
        wait_until(lambda: client.call("getmempoolinfo")["loaded"])
        assert client.call("getprioritisedtransactions") == bitcoind.rpc(
            "getprioritisedtransactions"
        )
        for tx in (root, first, parent, child, last):
            entry = client.call("getmempoolentry", [tx.id.hex()])
            core = cast("Any", bitcoind.rpc("getmempoolentry", [tx.id.hex()]))
            assert entry["time"] == core["time"]
            assert entry["unbroadcast"] is core["unbroadcast"] is True
    finally:
        node.stop()
    assert rewritten(theirs, path.read_bytes()) == theirs
