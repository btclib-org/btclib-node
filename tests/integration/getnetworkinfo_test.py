# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`getnetworkinfo`, this node's answer against a real bitcoind's.

Both start on regtest with the same relay feerates, neither with a peer,
a transaction or a proxy, so the fields the test names hold the same
value on each side. The client reads a JSON fraction as a `Decimal`, the
same for either answer.
"""

from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from btclib.fee import FeeRate

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import CLIENT_VERSION, numeric_version
from tests import get_random_port, rpc_client, wait_until_listening
from tests.integration.conftest import _started_bitcoind

if TYPE_CHECKING:
    from pathlib import Path


def test_the_fields_are_bitcoind_s_with_bitcoind_s_values(
    bitcoind_path: str, tmp_path: Path
) -> None:
    """ISS 2128: every key bitcoind answers, in its order and types.

    `localrelay`, `timeoffset`, `tx_send_rate`, `inv_buckets`, `networks`,
    `relayfee` and `incrementalfee` hold bitcoind's own values: with no
    peer and no transaction, the buckets stay as they started (ISS 2309).
    `version` is this node's own, in the form bitcoind's takes.
    """
    with _started_bitcoind(
        bitcoind_path,
        tmp_path,
        "-minrelaytxfee=0.00002",
        "-incrementalrelayfee=0.00003",
    ) as bitcoind:
        theirs = cast("dict[str, Any]", bitcoind.rpc("getnetworkinfo"))
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            min_relay_feerate=FeeRate(sats_per_kvbyte=2000),
            incremental_relay_feerate=FeeRate(sats_per_kvbyte=3000),
        )
    )
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        _, body = rpc_client(node).call_raw(
            "getnetworkinfo", jsonrpc="1.0", request_timeout=2
        )
    finally:
        node.stop()
        node.join()
    ours = cast("dict[str, Any]", body["result"])

    assert list(ours) == list(theirs)
    assert {key: type(value) for key, value in ours.items()} == {
        key: type(theirs[key]) for key in ours
    }
    for key in (
        "localrelay",
        "timeoffset",
        "tx_send_rate",
        "inv_buckets",
        "networks",
        "relayfee",
        "incrementalfee",
    ):
        assert ours[key] == theirs[key], key
    assert ours["relayfee"] == Decimal("0.00002")
    assert ours["incrementalfee"] == Decimal("0.00003")
    assert ours["version"] == numeric_version(CLIENT_VERSION)
