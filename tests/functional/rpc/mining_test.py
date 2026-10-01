# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The mining RPCs, live: mine over the socket, then read the chain back."""

from typing import TYPE_CHECKING

from btclib.script.script_pub_key import ScriptPubKey

from tests import anyone_can_spend, rpc_client, wait_until_listening

if TYPE_CHECKING:
    from btclib_node import Node


def test_mined_blocks_are_the_chain_and_the_template_builds_on_it(
    rpc_node: Node,
) -> None:
    """`generatetoaddress` and `generateblock` extend the chain they answer for.

    The template then builds on the new tip, and `submitblock` finds the
    mined block already held.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    address = ScriptPubKey(anyone_can_spend(), "regtest").address
    client = rpc_client(node)

    def call(method: str, params: list[object]) -> object:
        _, body = client.call_raw(method, params, jsonrpc="1.0", request_timeout=30)
        assert body["error"] is None
        return body["result"]

    hashes = call("generatetoaddress", [3, address])
    assert isinstance(hashes, list)
    assert call("getblockcount", []) == 3
    assert call("getbestblockhash", []) == hashes[-1]

    mined = call("generateblock", [address, []])
    assert isinstance(mined, dict)
    assert call("getbestblockhash", []) == mined["hash"]

    template = call("getblocktemplate", [{"rules": ["segwit"]}])
    assert isinstance(template, dict)
    assert template["previousblockhash"] == mined["hash"]
    assert template["height"] == 5

    block_hex = call("getblock", [mined["hash"], 0])
    assert call("submitblock", [block_hex]) == "duplicate"
