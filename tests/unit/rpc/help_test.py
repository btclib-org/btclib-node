# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.rpc.help`: `HELP_TEXT`, `CATEGORY`, `answer_help`."""

import pytest

from btclib_node.rpc.callbacks import callbacks
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT, answer_help

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
    carry (`src/rpc/server.cpp:123`, at bitcoin/bitcoin@9be056a8a7).
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
