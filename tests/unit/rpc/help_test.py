# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.rpc.help`: `HELP_TEXT`, `CATEGORY`, `answer_help`."""

import re
from typing import TYPE_CHECKING, cast

import pytest

from btclib_node.rpc.callbacks import callbacks, stop
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT, answer_help

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

# The bare listing a real regtest bitcoind v31.1.0's own `help` answers,
# cut down to exactly the commands this node serves -- read back the same
# way each entry of `HELP_TEXT` was, and reproduced here as the
# regression: `_BARE_LISTING`'s own construction (`rpc.help`'s module
# level) is what this compares against, not a second copy of Core's own
# text, so a change to `HELP_TEXT` or `CATEGORY` that breaks the grouping
# is caught here rather than only by eye.
_EXPECTED_BARE_LISTING = (
    "== Blockchain ==\n"
    "getbestblockhash\n"
    'getblock "blockhash" ( verbosity )\n'
    "getblockchaininfo\n"
    "getblockcount\n"
    "getblockhash height\n"
    'getblockheader "blockhash" ( verbose )\n'
    "getmempoolinfo\n"
    "getrawmempool ( verbose mempool_sequence )\n"
    'gettxoutsetinfo ( "hash_type" hash_or_height use_index )\n'
    "pruneblockchain height\n"
    "\n"
    "== Control ==\n"
    'help ( "command" )\n'
    "stop\n"
    "\n"
    "== Mining ==\n"
    'submitblock "hexdata" ( "dummy" )\n'
    "\n"
    "== Network ==\n"
    'addnode "node" "command" ( v2transport )\n'
    "clearbanned\n"
    'disconnectnode ( "address" nodeid )\n'
    "getconnectioncount\n"
    "getnetworkinfo\n"
    "getpeerinfo\n"
    "listbanned\n"
    "ping\n"
    'setban "subnet" "command" ( bantime absolute )\n'
    "\n"
    "== Rawtransactions ==\n"
    'getrawtransaction "txid" ( verbosity "blockhash" )\n'
    'sendrawtransaction "hexstring" ( maxfeerate maxburnamount )\n'
    'testmempoolaccept ["rawtx",...] ( maxfeerate )'
)


# `HELP_TEXT["disconnectnode"]` and `HELP_TEXT["addnode"]`, read back
# from a real regtest bitcoind v31.1.0's own `help disconnectnode` and
# `help addnode`, kept here as a literal independent of `HELP_TEXT`
# itself: every other test in this module and in `callbacks_test.py`
# compares a callback's own refusal against `HELP_TEXT[name]`, which
# proves the two agree and nothing about whether either is what Core
# actually answers. Only a copy that does not read `rpc.help` at all
# can catch its own content going stale or truncated.
_DISCONNECTNODE_HELP = (
    'disconnectnode ( "address" nodeid )\n'
    "\n"
    "Immediately disconnects from the specified peer node.\n"
    "\n"
    "Strictly one out of 'address' and 'nodeid' can be provided to identify"
    " the node.\n"
    "\n"
    "To disconnect by nodeid, either set 'address' to the empty string, or"
    " call using the named 'nodeid' argument only.\n"
    "\n"
    "Arguments:\n"
    "1. address    (string, optional, default=fallback to nodeid) The IP"
    " address/port of the node\n"
    "2. nodeid     (numeric, optional, default=fallback to address) The node"
    " ID (see getpeerinfo for node IDs)\n"
    "\n"
    "Result:\n"
    "null    (json null)\n"
    "\n"
    "Examples:\n"
    '> bitcoin-cli disconnectnode "192.168.0.6:8333"\n'
    '> bitcoin-cli disconnectnode "" 1\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0",'
    ' "id": "curltest", "method": "disconnectnode", "params":'
    " [\"192.168.0.6:8333\"]}' -H 'content-type: application/json'"
    " http://127.0.0.1:8332/\n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0",'
    ' "id": "curltest", "method": "disconnectnode", "params": ["", 1]}\''
    " -H 'content-type: application/json' http://127.0.0.1:8332/\n"
)

_ADDNODE_HELP = (
    'addnode "node" "command" ( v2transport )\n'
    "\n"
    "Attempts to add or remove a node from the addnode list.\n"
    "Or try a connection to a node once.\n"
    "Nodes added using addnode (or -connect) are protected from DoS"
    " disconnection and are not required to be\n"
    "full nodes/support SegWit as other outbound peers are (though such"
    " peers will not be synced from).\n"
    "Addnode connections are limited to 8 at a time and are counted"
    " separately from the -maxconnections limit.\n"
    "\n"
    "Arguments:\n"
    "1. node           (string, required) The IP address/hostname"
    " optionally followed by :port of the peer to connect to\n"
    "2. command        (string, required) 'add' to add a node to the"
    " list, 'remove' to remove a node from the list, 'onetry' to try a"
    " connection to the node once\n"
    "3. v2transport    (boolean, optional, default=set by -v2transport)"
    " Attempt to connect using BIP324 v2 transport protocol (ignored for"
    " 'remove' command)\n"
    "\n"
    "Result:\n"
    "null    (json null)\n"
    "\n"
    "Examples:\n"
    '> bitcoin-cli addnode "192.168.0.6:8333" "onetry" true\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0",'
    ' "id": "curltest", "method": "addnode", "params":'
    ' ["192.168.0.6:8333", "onetry" true]}\' -H'
    " 'content-type: application/json' http://127.0.0.1:8332/\n"
)


def test_disconnectnode_and_addnode_help_match_a_real_bitcoind() -> None:
    """`HELP_TEXT`'s own content, not merely its agreement with itself.

    Read back from a regtest bitcoind v31.1.0's own `help disconnectnode`
    and `help addnode`, byte for byte.
    """
    assert HELP_TEXT["disconnectnode"] == _DISCONNECTNODE_HELP
    assert HELP_TEXT["addnode"] == _ADDNODE_HELP


def test_every_method_has_help_text() -> None:
    """`HELP_TEXT` has an entry for each method `callbacks` dispatches.

    `rpc.main._execute` reads `HELP_TEXT[request.method]` unconditionally
    once a method is found, for the upper-bound refusal every method
    shares -- a method missing here would `KeyError` there rather than
    answer anything Core would.
    """
    assert HELP_TEXT.keys() == callbacks.keys()


def test_every_method_has_a_category() -> None:
    """`CATEGORY` has an entry for each method `callbacks` dispatches."""
    assert CATEGORY.keys() == callbacks.keys()


def test_every_help_text_starts_with_its_own_usage_line() -> None:
    """Each entry's first line is `arg_names`' own method, Core's usage line."""
    for name in callbacks:
        assert HELP_TEXT[name].split("\n", 1)[0].startswith(name)


def test_bare_help_groups_the_served_commands_like_a_real_bitcoind() -> None:
    """`answer_help([])` is `_EXPECTED_BARE_LISTING`, read back from Core."""
    assert answer_help([]) == _EXPECTED_BARE_LISTING


def test_bare_help_is_also_answered_for_an_empty_or_null_command() -> None:
    """An explicit empty string or `null` is the argument's own default."""
    assert answer_help([""]) == _EXPECTED_BARE_LISTING
    assert answer_help([None]) == _EXPECTED_BARE_LISTING


@pytest.mark.parametrize("name", sorted(callbacks))
def test_a_known_command_answers_its_own_untruncated_help(name: str) -> None:
    """`answer_help([name])` is `HELP_TEXT[name]`, with no trailing newline.

    `CRPCTable::help`'s own `strRet.substr(0, strRet.size()-1)` drops
    exactly the one trailing newline `help <command>` would otherwise
    carry (`src/rpc/server.cpp:115`, at bitcoin/bitcoin@9be056a8a7).
    """
    assert answer_help([name]) == HELP_TEXT[name].rstrip("\n")


def test_an_unknown_command_answers_cores_own_message() -> None:
    """Core's own literal `"help: unknown command: %s"`, not a refusal."""
    assert answer_help(["nosuchcommand"]) == "help: unknown command: nosuchcommand"


def test_a_non_string_command_is_a_type_error() -> None:
    """`command` is declared a string, type-checked as every other one is."""
    with pytest.raises(RpcError) as refused:
        answer_help([1])
    assert refused.value.message == (
        "Wrong type passed:\n"
        "{\n"
        '    "Position 1 (command)": "JSON value of type number is not of'
        ' expected type string"\n'
        "}"
    )


def test_stop_help_names_what_stop_actually_returns() -> None:
    """`HELP_TEXT["stop"]`'s own quoted result is `stop`'s own return value.

    Core builds both `stop`'s description and its `RPCResult` from one
    `CLIENT_NAME` (`_HELP_STOP`'s own comment in `rpc.help`); this reads
    the quoted content back out of `HELP_TEXT["stop"]` and ties it to
    `callbacks.stop`'s own literal, rather than repeating either as a
    second hardcoded copy, so the two cannot drift apart unnoticed.
    """
    match = re.search(r"with the content '([^']*)'", HELP_TEXT["stop"])
    assert match is not None
    node = cast("Node", None)
    conn = cast("RpcConnection", None)
    assert match.group(1) == stop(node, conn, [])
