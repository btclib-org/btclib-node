# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`mempool.dat` written by this node and read by bitcoind, and the reverse.

`savemempool` and `importmempool` are asked of both nodes too, and what
each expires of what it imported.

Each file is compared byte for byte with the other side's, once written
again with that side's obfuscation key: the key is random, and the rest
of the file follows from its content.
"""

import secrets
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bitcoin_core_rpc import RpcError
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
    from collections.abc import Callable

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


def started(data_dir: Path, *, persist_mempool: bool = True) -> Node:
    """Start a regtest node on `data_dir`."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=data_dir,
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            persist_mempool=persist_mempool,
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


def _answer(call: Callable[[str, Any], Any], method: str, params: Any) -> Any:
    """Return what `call` answers, or the error as its code and message."""
    try:
        return ("result", call(method, params))
    except RpcError as error:
        # past the url, which names each side's own port
        return ("error", error.code, error.args[0].split(": ", 1)[1])


def _agree(client: Any, bitcoind: Bitcoind, calls: list[tuple[str, Any]]) -> None:
    """Assert both nodes answer each of `calls` alike."""
    for method, params in calls:
        ours = _answer(client.call, method, params)
        assert ours == _answer(bitcoind.rpc, method, params), (method, params)


def test_savemempool_and_importmempool_answer_as_bitcoind_does(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """The refusals alike, before and after the block download.

    Then each node imports the file the other saved, with everything in
    it, and saves it back unchanged.
    """
    garbage = tmp_path / "garbage.dat"
    garbage.write_bytes(b"garbage")
    missing = str(tmp_path / "missing.dat")
    data_dir = tmp_path / "node"
    node = started(data_dir)
    try:
        client = rpc_client(node)
        wait_until(lambda: client.call("getmempoolinfo")["loaded"])
        _agree(
            client,
            bitcoind,
            [
                ("savemempool", [1]),
                ("importmempool", []),
                ("importmempool", [1]),
                ("importmempool", [missing, 5]),
                ("importmempool", [1, 5]),
                ("importmempool", [missing, {}, 3]),
                ("importmempool", [missing, {"use_current_time": 1}]),
                ("importmempool", {"use_current_time": False}),
                ("importmempool", {"filepath": 3}),
            ],
        )

        chain = a_chain(100)
        submit(bitcoind, chain)
        node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))
        block_index = node.chainstate.block_index
        wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)
        wait_until(lambda: not client.call("getblockchaininfo")["initialblockdownload"])
        bitcoind.rpc("setnetworkactive", [False])
        wait_until(lambda: client.call("getconnectioncount") == 0)
        _agree(
            client,
            bitcoind,
            [
                ("importmempool", [missing]),
                ("importmempool", [str(garbage)]),
                ("importmempool", [""]),
                ("importmempool", [missing, {"use_current_time": 1}]),
                ("importmempool", [missing, {"apply_fee_delta_priority": "x"}]),
                ("importmempool", [missing, {"use_current_time": None, "foo": 1}]),
                ("importmempool", [missing, None]),
                (
                    "importmempool",
                    {"filepath": missing, "apply_unbroadcast_set": True, "options": {}},
                ),
                ("importmempool", {"filepath": missing, "apply_unbroadcast_set": 3}),
            ],
        )

        root = a_spend((chain[0].transactions[0], 0), 2, 200_000)
        first = a_spend((root, 0), 1, 30_000)
        last = a_spend((root, 1), 1, 5_000)
        for tx in (root, first, last):
            client.call(
                "sendrawtransaction", [tx.serialize(include_witness=True).hex()]
            )
        client.call("prioritisetransaction", [first.id.hex(), None, 1_000])
        saved = client.call("savemempool")
        assert saved == {"filename": str(node.data_dir / "mempool.dat")}
        ours = Path(saved["filename"]).read_bytes()
    finally:
        node.stop()

    assert bitcoind.rpc("importmempool", [saved["filename"], _ALL]) == {}
    theirs_path = Path(cast("Any", bitcoind.rpc("savemempool"))["filename"])
    assert rewritten(theirs_path.read_bytes(), ours) == theirs_path.read_bytes()

    bitcoind.rpc("prioritisetransaction", [last.id.hex(), None, 3_000])
    bitcoind.rpc("prioritisetransaction", [secrets.token_hex(32), None, 11])
    bitcoind.rpc("savemempool")
    theirs = theirs_path.read_bytes()
    node = started(data_dir, persist_mempool=False)
    try:
        client = rpc_client(node)
        wait_until(lambda: client.call("getmempoolinfo")["loaded"])
        assert client.call("getrawmempool") == []
        assert client.call("importmempool", [str(theirs_path), _ALL]) == {}
        assert client.call("getprioritisedtransactions") == bitcoind.rpc(
            "getprioritisedtransactions"
        )
        ours = Path(client.call("savemempool")["filename"]).read_bytes()
    finally:
        node.stop()
    assert rewritten(theirs, ours) == theirs


def test_both_nodes_expire_the_same_transactions(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Core's `Expire` at the next acceptance, with the descendants.

    Both nodes import one file whose oldest entries are just under the
    default 336 hours old, and then accept the same new transaction once
    those entries are past it. What each keeps is the same.
    """
    chain = a_chain(100)
    submit(bitcoind, chain)
    root = a_spend((chain[0].transactions[0], 0), 4, 200_000)
    old = a_spend((root, 0), 1, 10_000)
    old_child = a_spend((old, 0), 1, 10_000)
    young = a_spend((root, 1), 1, 10_000)
    young_s_old_child = a_spend((young, 0), 1, 10_000)
    kept = a_spend((root, 2), 1, 10_000)
    newcomer = a_spend((root, 3), 1, 10_000)
    now = int(time.time())
    # what both imports have to finish within
    margin = 15
    then = now - 336 * 3600 + margin
    entries = [
        (root, now, 0),
        (old, then, 0),
        (old_child, now, 0),
        (young, now, 0),
        (young_s_old_child, then, 0),
        (kept, now, 0),
    ]
    path = tmp_path / "expiring.dat"
    path.write_bytes(serialize_mempool(entries, {}, [], None))

    node = started(tmp_path / "node", persist_mempool=False)
    try:
        node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))
        block_index = node.chainstate.block_index
        wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)
        client = rpc_client(node)
        wait_until(lambda: not client.call("getblockchaininfo")["initialblockdownload"])
        bitcoind.rpc("setnetworkactive", [False])
        wait_until(lambda: client.call("getconnectioncount") == 0)
        assert client.call("importmempool", [str(path), _ALL]) == {}
        assert bitcoind.rpc("importmempool", [str(path), _ALL]) == {}
        held = {tx.id.hex() for tx, _, _ in entries}
        assert set(client.call("getrawmempool")) == held
        assert set(cast("Any", bitcoind.rpc("getrawmempool"))) == held

        time.sleep(max(0, now + margin + 1 - time.time()))
        raw = newcomer.serialize(include_witness=True).hex()
        client.call("sendrawtransaction", [raw])
        bitcoind.rpc("sendrawtransaction", [raw])
        ours = set(client.call("getrawmempool"))
        assert ours == set(cast("Any", bitcoind.rpc("getrawmempool")))
        assert ours == {tx.id.hex() for tx in (root, young, kept, newcomer)}
    finally:
        node.stop()
