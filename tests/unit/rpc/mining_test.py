# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What `generatetoaddress`, `generateblock` and `getblocktemplate` answer.

Called as `rpc.main` calls them, on a regtest node built and driven in
this thread. What `bitcoind` v31.1.0 answers for the same call is the
expected value wherever a test says so; the rest is what these handlers
add where Core has no rule to copy.
"""

import json
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.block import Block, BlockHeader, witness_commitment_output
from btclib.block.block import merkle_root_and_mutated_from_transactions
from btclib.script.script_pub_key import ScriptPubKey
from btclib.tx import OutPoint, Tx, TxIn, TxOut

import btclib_node.mining as core_mining
import btclib_node.rpc.mining as rpc_mining
from btclib_node import Node
from btclib_node.chains import Main, SigNet
from btclib_node.chainstate.block_index import BlockStatus
from btclib_node.mining import (
    BlockTemplate,
    accept_block,
    create_new_block,
    solve_block,
)
from btclib_node.rpc.callbacks import arg_names, callbacks
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT
from btclib_node.rpc.mining import (
    _long_poll_id,
    get_block_template,
    wait_for_block_height,
)
from btclib_node.signet import SIGNET_CHALLENGE
from btclib_node.versionbits import UnknownActivations
from tests import (
    answer,
    anyone_can_spend,
    anyone_can_spend_script_sig,
    finish,
    generate_random_transaction,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib_node.rpc.connection import RpcConnection

CONN = cast("RpcConnection", None)

SCRIPT = ScriptPubKey(anyone_can_spend(), "regtest")
ADDRESS = SCRIPT.address
SUBSIDY = 50 * 10**8

# the p2wpkh script of this key, as `bitcoind` v31.1.0 derives it
COMPRESSED_KEY = "03a34b99f22c790c4e36b2b3c2c35a36db06226e41c692fc82b8b56ac1c540c5bd"
P2WPKH = bytes.fromhex("00149a1c78a507689f6f54b847ad1cef1e614ee23f1e")
P2PKH_KEY = (
    "0479be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    "483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8"
)
# a mainnet extended key, and a test chain's
XPUB = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoCu1R"
    "upje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)
# BIP32 test vector 1's master key on a test chain: its `/0h` is a hardened
# step only an extended private key can take
TPRV = (
    "tprv8ZgxMBicQKsPeDgjzdC36fs6bMjGApWDNLR9erAXMs5skhMv36j9MV5ecvfavji5kh"
    "qjWaWSFhN3YcCUUdiKH6isR4Pwy3U5y5egddBr16m"
)
# the compressed-key WIF of private key 1 on mainnet
MAINNET_WIF = "KwDiBf89QgGbjEhKnhXJuH7LrciVrZi3qYjgd9M7rFU73sVHnoWn"
TPUB = (
    "tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1xkh56hjod1"
    "RhGjxbaTLV3X4FyWuejifB9j"
    "usQ46QzG87VKp"
)


@pytest.fixture
def node(regtest_node: Callable[[], Node]) -> Node:
    """Give one regtest node, built fresh for the test."""
    return regtest_node()


@pytest.fixture
def funded(node: Node) -> Node:
    """Give a node whose first coinbases are spendable."""
    generate_to_address(node, CONN, [101 + 5, ADDRESS])
    return node


def generate_to_address(
    node: Node, conn: RpcConnection, params: list[Any]
) -> list[bytes]:
    """`rpc.mining.generate_to_address`, run to its answer."""
    return finish(rpc_mining.generate_to_address(node, conn, params))


def generate_block(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """`rpc.mining.generate_block`, run to its answer."""
    return finish(rpc_mining.generate_block(node, conn, params))


def finish_template(node: Node, request: dict[str, Any]) -> object:
    """Answer `getblocktemplate` for `request`, run to its answer."""
    result = get_block_template(node, CONN, [request])
    return finish(result) if isinstance(result, Generator) else result


def refusal(call: Callable[[], object]) -> tuple[int, str]:
    """Run `call`, and return the code and message it is refused with."""
    with pytest.raises(RpcError) as caught:
        call()
    return int(caught.value.code), caught.value.message


def tip(node: Node) -> bytes:
    """Return the hash of the node's active tip."""
    return node.chainstate.block_index.active_chain[-1]


def height(node: Node) -> int:
    """Return the height of the node's active tip."""
    return len(node.chainstate.block_index.active_chain) - 1


def spend(txid: bytes, value: int, index: int = 0) -> Tx:
    """Return a transaction spending an anyone-can-spend output."""
    return Tx(
        version=2,
        lock_time=0,
        vin=[TxIn(OutPoint(txid, index), anyone_can_spend_script_sig(), 0xFFFFFFFF)],
        vout=[TxOut(value, anyone_can_spend())],
    )


def coinbase_txid(node: Node, at: int) -> bytes:
    """Return the id of the coinbase at height `at` on the active chain."""
    block = node.block_db.get_block(node.chainstate.block_index.active_chain[at])
    assert block is not None
    return block.transactions[0].id


def test_the_methods_are_served_as_core_names_them() -> None:
    """Each is in the table, with Core's argument names and category."""
    assert callbacks["generatetoaddress"] is rpc_mining.generate_to_address
    assert callbacks["generateblock"] is rpc_mining.generate_block
    assert callbacks["getblocktemplate"] is get_block_template
    assert arg_names["generatetoaddress"] == ("nblocks", "address", "maxtries")
    assert arg_names["generateblock"] == ("output", "transactions", "submit")
    assert arg_names["getblocktemplate"] == ("template_request",)
    assert CATEGORY["generatetoaddress"] == "hidden"
    assert CATEGORY["generateblock"] == "hidden"
    assert CATEGORY["getblocktemplate"] == "Mining"


def test_generatetoaddress_mines_the_blocks_it_answers(node: Node) -> None:
    """The hashes are the new blocks of the active chain, in order."""
    hashes = generate_to_address(node, CONN, [3, ADDRESS])

    active_chain = node.chainstate.block_index.active_chain
    assert active_chain[-3:] == hashes
    assert len(hashes) == 3
    block = node.block_db.get_block(hashes[0])
    assert block is not None
    assert block.transactions[0].vout[0].script_pub_key.script == anyone_can_spend()


def test_generatetoaddress_fills_each_block_from_the_mempool(funded: Node) -> None:
    """A transaction in the mempool is mined, and leaves the mempool."""
    tx = spend(coinbase_txid(funded, 1), SUBSIDY - 1_000)
    assert funded.mempool.add_tx(tx, 1_000)

    [mined] = generate_to_address(funded, CONN, [1, ADDRESS])

    block = funded.block_db.get_block(mined)
    assert block is not None
    assert block.transactions[1:] == [tx]
    assert funded.mempool.size == 0


@pytest.mark.parametrize(
    ("params", "code", "message"),
    [
        (
            [1, "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"],
            -5,
            "Error: Invalid address",
        ),
        (
            [1, "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"],
            -5,
            "Error: Invalid address",
        ),
        ([1, "garbage"], -5, "Error: Invalid address"),
        ([1, ""], -5, "Error: Invalid address"),
        ([1, ADDRESS[:-1] + "x"], -5, "Error: Invalid address"),
        ([1.5, ADDRESS], -1, "JSON integer out of range"),
        ([2**31, ADDRESS], -1, "JSON integer out of range"),
        ([1, ADDRESS, 1.5], -1, "JSON integer out of range"),
    ],
)
def test_generatetoaddress_refuses_what_core_refuses(
    node: Node, params: list[Any], code: int, message: str
) -> None:
    """The error codes and messages are `bitcoind` v31.1.0's, on regtest."""
    assert refusal(lambda: generate_to_address(node, CONN, params)) == (code, message)
    assert height(node) == 0


def test_generatetoaddress_reads_an_address_as_the_chain_spells_it(node: Node) -> None:
    """Base58 of a test chain, and bech32 in capitals, are this chain's."""
    bech32 = ScriptPubKey(P2WPKH, "regtest").address
    assert bech32.startswith("bcrt1q")

    assert len(generate_to_address(node, CONN, [1, ADDRESS])) == 1
    assert len(generate_to_address(node, CONN, [1, bech32])) == 1
    assert len(generate_to_address(node, CONN, [1, bech32.upper()])) == 1
    mixed = bech32[:6] + bech32[6].upper() + bech32[7:]
    assert refusal(lambda: generate_to_address(node, CONN, [1, mixed])) == (
        -5,
        "Error: Invalid address",
    )


def test_generatetoaddress_type_errors_are_core_s(node: Node) -> None:
    """Every argument of the wrong type is named, in one error."""
    code, message = refusal(lambda: generate_to_address(node, CONN, ["1", 5, "x"]))

    assert code == -3
    assert message == (
        "Wrong type passed:\n"
        "{\n"
        '    "Position 1 (nblocks)": "JSON value of type string is not of expected type number",\n'
        '    "Position 2 (address)": "JSON value of type number is not of expected type string",\n'
        '    "Position 3 (maxtries)": "JSON value of type string is not of expected type number"\n'
        "}"
    )


def test_generatetoaddress_without_its_arguments_is_the_help_text(node: Node) -> None:
    """A call short of an argument is answered with the method's help."""
    assert refusal(lambda: generate_to_address(node, CONN, [1])) == (
        -1,
        HELP_TEXT["generatetoaddress"],
    )


def test_generatetoaddress_with_no_blocks_or_no_tries_mines_nothing(node: Node) -> None:
    """A count under one, or no hashes to spend, answers an empty list."""
    assert generate_to_address(node, CONN, [0, ADDRESS]) == []
    assert generate_to_address(node, CONN, [-1, ADDRESS]) == []
    assert generate_to_address(node, CONN, [1, ADDRESS, 0]) == []
    assert height(node) == 0


def test_generatetoaddress_reads_a_negative_maxtries_as_unsigned(node: Node) -> None:
    """As Core's `uint64_t` does, so the search is not stopped by it."""
    assert len(generate_to_address(node, CONN, [1, ADDRESS, -1])) == 1


def test_generatetoaddress_stops_where_the_tries_are_spent(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hashes of the blocks found are answered, and no more are mined."""
    solved = iter([True, False])
    real = solve_block

    def solve(
        node_: Node, block: Block, tries: int
    ) -> Generator[bool, None, tuple[Block | None, int]]:
        if next(solved):
            return real(node_, block, tries)
        return answer((None, 0))

    monkeypatch.setattr(rpc_mining, "solve_block", solve)

    hashes = generate_to_address(node, CONN, [3, ADDRESS])

    assert len(hashes) == 1
    assert height(node) == 1


def test_generatetoaddress_builds_another_block_where_the_nonces_run_out(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block no nonce solves does not count, and costs the tries it took."""
    answers = iter([False, True])
    real = solve_block
    tries_given: list[int] = []

    def solve(
        node_: Node, block: Block, tries: int
    ) -> Generator[bool, None, tuple[Block | None, int]]:
        tries_given.append(tries)
        if next(answers):
            return real(node_, block, tries)
        return answer((None, tries - 7))

    monkeypatch.setattr(rpc_mining, "solve_block", solve)

    assert len(generate_to_address(node, CONN, [1, ADDRESS, 100])) == 1
    assert tries_given == [100, 93]


def test_generatetoaddress_hands_the_thread_back_after_each_block(
    node: Node,
) -> None:
    """One step mines one block at most, so the loop runs between two."""
    job = rpc_mining.generate_to_address(node, CONN, [2, ADDRESS])

    assert next(job) is True
    assert height(node) == 1
    assert finish(job) == node.chainstate.block_index.active_chain[1:]


def test_generatetoaddress_stops_when_the_node_does(node: Node) -> None:
    """A node asked to stop mines no more blocks."""
    node.terminate_flag.set()

    assert generate_to_address(node, CONN, [3, ADDRESS]) == []


def test_generatetoaddress_stops_when_the_node_does_mid_search(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search that was interrupted returns what was mined and no more."""

    def solve(
        node_: Node, block: Block, tries: int
    ) -> Generator[bool, None, tuple[Block | None, int]]:
        node_.terminate_flag.set()
        return answer((None, tries))

    monkeypatch.setattr(rpc_mining, "solve_block", solve)

    assert generate_to_address(node, CONN, [3, ADDRESS]) == []


def test_a_block_the_chain_refuses_is_an_internal_error(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `ProcessNewBlock, block not accepted`."""
    monkeypatch.setattr(rpc_mining, "accept_block", lambda _node, _block: "why")

    assert refusal(lambda: generate_to_address(node, CONN, [1, ADDRESS])) == (
        -32603,
        "ProcessNewBlock, block not accepted",
    )


def test_a_template_the_chain_refuses_is_a_misc_error(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `std::runtime_error` reaches the caller as `-1`."""
    monkeypatch.setattr(core_mining, "check_block_validity", lambda *_, **__: "why")

    assert refusal(lambda: generate_to_address(node, CONN, [1, ADDRESS])) == (
        -1,
        "TestBlockValidity failed: why",
    )


def test_generateblock_mines_the_given_transactions_in_order(funded: Node) -> None:
    """A raw transaction and a txid of the mempool, in the order given."""
    first = spend(coinbase_txid(funded, 1), SUBSIDY - 1_000)
    second = spend(coinbase_txid(funded, 2), SUBSIDY - 2_000)
    third = spend(coinbase_txid(funded, 3), SUBSIDY - 3_000)
    assert funded.mempool.add_tx(second, 2_000)
    assert funded.mempool.add_tx(third, 3_000)

    result = generate_block(
        funded,
        CONN,
        [
            ADDRESS,
            [
                first.serialize(include_witness=True).hex(),
                third.id.hex(),
                second.id.hex(),
            ],
        ],
    )

    block = funded.block_db.get_block(result["hash"])
    assert block is not None
    assert block.transactions[1:] == [first, third, second]
    assert block.transactions[0].vout[0].value == SUBSIDY
    assert tip(funded) == result["hash"]
    assert set(result) == {"hash"}
    # the mempool keeps what the block did not confirm of it
    assert funded.mempool.size == 0


def test_generateblock_without_submit_answers_the_block_and_stores_nothing(
    funded: Node,
) -> None:
    """The hex is the block, and the chain is where it was."""
    before = tip(funded)
    tx = spend(coinbase_txid(funded, 1), SUBSIDY - 1_000)

    result = generate_block(
        funded, CONN, [ADDRESS, [tx.serialize(include_witness=True).hex()], False]
    )

    assert tip(funded) == before
    assert set(result) == {"hash", "hex"}
    block = Block.parse(bytes.fromhex(result["hex"]), check_validity=False)
    assert block.header.hash == result["hash"]
    assert block.transactions[1:] == [tx]
    assert accept_block(funded, block) is None
    assert tip(funded) == result["hash"]


def test_generateblock_reads_a_descriptor(node: Node) -> None:
    """A descriptor of one script, and a combo descriptor's p2wpkh or p2pkh."""
    scripts = []
    for output in (
        f"wpkh({COMPRESSED_KEY})",
        f"combo({COMPRESSED_KEY})",
        f"combo({P2PKH_KEY})",
        f"addr({ADDRESS})",
        f"wpkh({TPUB}/0/1)",
    ):
        result = generate_block(node, CONN, [output, [], False])
        block = Block.parse(bytes.fromhex(result["hex"]), check_validity=False)
        scripts.append(block.transactions[0].vout[0].script_pub_key.script)

    assert scripts[0] == P2WPKH
    assert scripts[1] == P2WPKH
    assert scripts[2][:3] == bytes.fromhex("76a914")
    assert scripts[3] == anyone_can_spend()
    assert scripts[4][:2] == bytes.fromhex("0014")


def coinbase_script_of(node: Node, output: str) -> bytes:
    """Mine `generateblock` to `output`, and return the script it pays."""
    result = generate_block(node, CONN, [output, [], False])
    block = Block.parse(bytes.fromhex(result["hex"]), check_validity=False)
    return block.transactions[0].vout[0].script_pub_key.script


def test_generateblock_derives_a_hardened_step_from_an_extended_private_key(
    node: Node,
) -> None:
    """The script is the one `bitcoind` v31.1.0 derives for `pkh(tprv/0h)`."""
    assert coinbase_script_of(node, f"pkh({TPRV}/0h)") == bytes.fromhex(
        "76a9145c1bd648ed23aa5fd50ba52b2457c11e9e80a6a788ac"
    )


def test_generateblock_refuses_a_wif_of_another_chain(node: Node) -> None:
    """Core's `Parse` refuses it with -5."""
    assert refusal(lambda: generate_block(node, CONN, [f"pkh({MAINNET_WIF})", []])) == (
        -5,
        "Error: Invalid address or descriptor",
    )


def test_a_raw_descriptor_that_looks_like_a_key_still_mines(node: Node) -> None:
    """`raw()` holds hex that the extended-key pattern also matches."""
    hexa = "abcdef12" * 13

    assert coinbase_script_of(node, f"raw({hexa})") == bytes.fromhex(hexa)


def test_generateblock_refuses_a_multipath_or_hardened_public_descriptor(
    node: Node,
) -> None:
    """Core's rows for `wpkh(tpub/<0;1>/0)` and `pkh(tpub/0h)`."""
    assert refusal(
        lambda: generate_block(node, CONN, [f"wpkh({TPUB}/<0;1>/0)", []])
    ) == (-8, "Multipath descriptor not accepted")
    assert refusal(lambda: generate_block(node, CONN, [f"pkh({TPUB}/0h)", []])) == (
        -5,
        "Cannot derive script without private keys",
    )


@pytest.mark.parametrize(
    ("descriptor", "code", "message"),
    [
        (f"pkh({TPUB}/0h/*)", -8, "Ranged descriptor not accepted. Maybe pass"),
        (f"pkh({TPUB}/*h)", -8, "Ranged descriptor not accepted. Maybe pass"),
        (f"pkh({XPUB}/0h)", -5, "Error: Invalid address or descriptor"),
        (f"pkh({XPUB}/<0;1>)", -5, "Error: Invalid address or descriptor"),
        (f"wpkh({TPUB}/<0;1>/*)", -8, "Multipath descriptor not accepted"),
        (f"wpkh({TPUB}/<0;1>/0h)", -8, "Multipath descriptor not accepted"),
        (f"sh(wpkh({TPUB}/0h))", -5, "Cannot derive script without private keys"),
    ],
)
def test_generateblock_checks_a_descriptor_in_cores_order(
    node: Node, descriptor: str, code: int, message: str
) -> None:
    """A key of another chain, then multipath, then ranged, then derivation."""
    got_code, got_message = refusal(
        lambda: generate_block(node, CONN, [descriptor, []])
    )

    assert got_code == code
    assert got_message.startswith(message)


@pytest.mark.parametrize(
    ("params", "code", "message"),
    [
        (["garbage", []], -5, "Error: Invalid address or descriptor"),
        ([f"wpkh({XPUB}/0/*)", []], -5, "Error: Invalid address or descriptor"),
        (
            [f"wpkh({TPUB}/0/*)", []],
            -8,
            (
                "Ranged descriptor not accepted. "
                "Maybe pass through deriveaddresses first?"
            ),
        ),
        (
            [ADDRESS, ["aa"]],
            -22,
            (
                "Transaction decode failed for aa. "
                "Make sure the tx has at least one input."
            ),
        ),
        ([ADDRESS, ["0" * 63 + "1"]], -5, f"Transaction {'0' * 63}1 not in mempool."),
        (
            [ADDRESS, [1]],
            -3,
            "JSON value of type number is not of expected type string",
        ),
        (
            [ADDRESS, ["zz"]],
            -22,
            (
                "Transaction decode failed for zz. "
                "Make sure the tx has at least one input."
            ),
        ),
    ],
)
def test_generateblock_refuses_what_core_refuses(
    node: Node, params: list[Any], code: int, message: str
) -> None:
    """The codes and messages are `bitcoind` v31.1.0's."""
    assert refusal(lambda: generate_block(node, CONN, params)) == (code, message)
    assert height(node) == 0


def test_generateblock_refuses_a_block_that_does_not_connect(funded: Node) -> None:
    """A transaction spending nothing is refused with its reason."""
    before = tip(funded)
    bad = generate_random_transaction()

    code, message = refusal(
        lambda: generate_block(
            funded, CONN, [ADDRESS, [bad.serialize(include_witness=True).hex()]]
        )
    )

    assert code == -25
    assert message == "TestBlockValidity failed: bad-txns-inputs-missingorspent"
    assert tip(funded) == before


def test_generateblock_type_errors_are_core_s(node: Node) -> None:
    """The arguments are checked together, by position and name."""
    code, message = refusal(lambda: generate_block(node, CONN, [1, "x"]))

    assert code == -3
    assert message == (
        "Wrong type passed:\n"
        "{\n"
        '    "Position 1 (output)": "JSON value of type number is not of expected type string",\n'
        '    "Position 2 (transactions)": "JSON value of type string is not of expected type array"\n'
        "}"
    )
    assert refusal(lambda: generate_block(node, CONN, [ADDRESS])) == (
        -1,
        HELP_TEXT["generateblock"],
    )
    assert refusal(lambda: generate_block(node, CONN, [ADDRESS, [], 1])) == (
        -3,
        (
            "Wrong type passed:\n{\n"
            '    "Position 3 (submit)": '
            '"JSON value of type number is not of expected type bool"\n'
            "}"
        ),
    )


def test_generateblock_fails_where_no_nonce_solves_the_block(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `Failed to make block.`."""
    monkeypatch.setattr(rpc_mining, "solve_block", lambda *_: answer((None, 0)))

    assert refusal(lambda: generate_block(node, CONN, [ADDRESS, []])) == (
        -1,
        "Failed to make block.",
    )


def test_generateblock_a_block_the_chain_refuses_is_an_internal_error(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `ProcessNewBlock, block not accepted`."""
    monkeypatch.setattr(rpc_mining, "accept_block", lambda _node, _block: "why")

    assert refusal(lambda: generate_block(node, CONN, [ADDRESS, []])) == (
        -32603,
        "ProcessNewBlock, block not accepted",
    )


def proposal(node: Node, block: Block) -> object:
    """Ask `getblocktemplate` to check `block`, as a proposal."""
    data = block.serialize(check_validity=False).hex()
    return get_block_template(node, CONN, [{"mode": "proposal", "data": data}])


def test_a_valid_proposal_is_null_and_stores_nothing(node: Node) -> None:
    """The block is checked on top of the tip and not kept."""
    block = create_new_block(node, SCRIPT).block
    before = (tip(node), len(node.chainstate.block_index.header_dict))

    assert proposal(node, block) is None
    assert (tip(node), len(node.chainstate.block_index.header_dict)) == before
    assert node.block_db.get_block(block.header.hash) is None


def test_a_proposal_leaves_the_utxo_set_as_it_was(funded: Node) -> None:
    """A block that spends a coin does not spend it for the next one."""
    tx = spend(coinbase_txid(funded, 1), SUBSIDY - 1_000)
    assert funded.mempool.add_tx(tx, 1_000)
    block = create_new_block(funded, SCRIPT).block
    assert block.transactions[1:] == [tx]

    assert proposal(funded, block) is None
    assert proposal(funded, block) is None
    assert accept_block(funded, solved(funded, block)) is None


def solved(node: Node, block: Block) -> Block:
    """Return `block` solved."""
    result, _ = finish(solve_block(node, block, 10_000))
    assert result is not None
    return result


def test_a_proposal_for_a_block_the_node_has_is_a_duplicate(funded: Node) -> None:
    """Connected, marked invalid, or stored and neither."""
    [mined] = generate_to_address(funded, CONN, [1, ADDRESS])
    block = funded.block_db.get_block(mined)
    assert block is not None
    assert proposal(funded, block) == "duplicate"

    # a sibling of the tip is stored and never connected
    parent = funded.chainstate.block_index.active_chain[-2]
    sibling = create_new_block(funded, SCRIPT).block
    sibling.header.previous_block_hash = parent
    sibling.header.time = block.header.time
    sibling.transactions[0].vin[0].script_sig += b"\x01"
    sibling.header.merkle_root = merkle_root_and_mutated_from_transactions(
        sibling.transactions
    )[0]
    rebuilt = solved(funded, sibling)
    assert accept_block(funded, rebuilt) is None
    assert tip(funded) == mined
    assert proposal(funded, rebuilt) == "duplicate-inconclusive"

    # a block whose connection failed is marked invalid
    bad = solved(funded, block_with_bad_amount(funded))
    assert accept_block(funded, bad) is not None
    assert proposal(funded, bad) == "duplicate-invalid"

    # a block a reorg removed from the active chain is `valid` too
    header_dict = funded.chainstate.block_index.header_dict
    header_dict[mined] = replace(header_dict[mined], status=BlockStatus.valid)
    assert proposal(funded, block) == "duplicate"


def test_a_proposal_of_the_genesis_block_is_inconclusive(node: Node) -> None:
    """Core never connected it through `ConnectBlock`."""
    genesis = node.block_db.get_block(node.chain.genesis.hash)
    assert genesis is not None
    assert proposal(node, genesis) == "duplicate-inconclusive"


def block_with_bad_amount(node: Node) -> Block:
    """Return a block on the tip whose coinbase claims more than it may."""
    block = create_new_block(node, SCRIPT).block
    coinbase = block.transactions[0]
    coinbase.vout[0] = TxOut(coinbase.vout[0].value + 1, anyone_can_spend())
    coinbase.vout[1] = witness_commitment_output(block.transactions, bytes(32))
    block.header.merkle_root = merkle_root_and_mutated_from_transactions(
        block.transactions
    )[0]
    return block


def test_a_template_the_chain_refuses_is_a_misc_error_in_template_mode(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same `-1` as `generatetoaddress`."""
    monkeypatch.setattr(core_mining, "check_block_validity", lambda *_, **__: "why")

    assert refusal(lambda: get_block_template(node, CONN, [{"rules": ["segwit"]}])) == (
        -1,
        "TestBlockValidity failed: why",
    )


def proposal_refusal(node: Node, data: object) -> tuple[int, str]:
    """Return the refusal of a proposal whose `data` is given."""
    request = {"mode": "proposal", "data": data}
    return refusal(lambda: get_block_template(node, CONN, [request]))


def test_a_proposal_that_is_not_one_is_refused_as_core_refuses(node: Node) -> None:
    """The codes and messages are `bitcoind` v31.1.0's."""
    assert refusal(lambda: get_block_template(node, CONN, [{"mode": "proposal"}])) == (
        -3,
        "Missing data String key for proposal",
    )
    for data in (5, None):
        assert proposal_refusal(node, data) == (
            -3,
            "Missing data String key for proposal",
        )
    for text in ("00", "", "zz", "0" * 160):
        assert proposal_refusal(node, text) == (-22, "Block decode failed")


@pytest.mark.parametrize(
    ("request_", "code", "message"),
    [
        ({"mode": "x"}, -8, "Invalid mode"),
        ({"mode": 5}, -8, "Invalid mode"),
        (
            {},
            -8,
            'getblocktemplate must be called with the segwit rule set (call with {"rules": ["segwit"]})',
        ),
        (
            {"rules": ["x"]},
            -8,
            'getblocktemplate must be called with the segwit rule set (call with {"rules": ["segwit"]})',
        ),
        (
            {"rules": [5]},
            -3,
            "JSON value of type number is not of expected type string",
        ),
        (
            {"rules": ["segwit"], "longpollid": "x"},
            -8,
            "longpollid must be of length 64 (not 1, for 'x')",
        ),
        (
            {"rules": ["segwit"], "longpollid": "zz" * 32},
            -8,
            f"longpollid must be hexadecimal string (not '{'zz' * 32}')",
        ),
        # the first 64 bytes, not characters
        (
            {"rules": ["segwit"], "longpollid": "\u00e9" * 10},
            -8,
            f"longpollid must be of length 64 (not 20, for '{'\u00e9' * 10}')",
        ),
        (
            {"rules": ["segwit"], "longpollid": "\u00e9" * 40},
            -8,
            f"longpollid must be hexadecimal string (not '{'\u00e9' * 32}')",
        ),
        # read before the rules, and waited on before them
        ({"longpollid": "x"}, -8, "longpollid must be of length 64 (not 1, for 'x')"),
        (
            {"longpollid": "00" * 32},
            -8,
            'getblocktemplate must be called with the segwit rule set (call with {"rules": ["segwit"]})',
        ),
    ],
)
def test_getblocktemplate_refuses_what_core_refuses(
    node: Node, request_: dict[str, Any], code: int, message: str
) -> None:
    """The codes and messages are `bitcoind` v31.1.0's."""
    assert refusal(lambda: finish_template(node, request_)) == (code, message)


def test_getblocktemplate_needs_its_argument_as_an_object(node: Node) -> None:
    """No argument is the help text, and a non-object is a type error."""
    assert refusal(lambda: get_block_template(node, CONN, [])) == (
        -1,
        HELP_TEXT["getblocktemplate"],
    )
    code, message = refusal(lambda: get_block_template(node, CONN, [[]]))
    assert code == -3
    assert "Position 1 (template_request)" in message


def test_the_template_has_the_keys_of_core_s_in_its_order(funded: Node) -> None:
    """The shape of `bitcoind` v31.1.0's regtest answer, two transactions."""
    parent = spend(coinbase_txid(funded, 1), SUBSIDY - 1_000)
    child = spend(parent.id, SUBSIDY - 3_000)
    assert funded.mempool.add_tx(parent, 1_000)
    assert funded.mempool.add_tx(child, 2_000)

    template = get_block_template(funded, CONN, [{"rules": ["segwit"]}])

    assert isinstance(template, dict)
    assert list(template) == [
        "capabilities",
        "version",
        "rules",
        "vbavailable",
        "vbrequired",
        "previousblockhash",
        "transactions",
        "coinbaseaux",
        "coinbasevalue",
        "longpollid",
        "target",
        "mintime",
        "mutable",
        "noncerange",
        "sigoplimit",
        "sizelimit",
        "weightlimit",
        "curtime",
        "bits",
        "height",
        "default_witness_commitment",
    ]
    assert template["capabilities"] == ["proposal"]
    assert template["version"] == 0x20000000
    assert template["rules"] == ["csv", "!segwit", "taproot"]
    assert template["vbavailable"] == {}
    assert template["vbrequired"] == 0
    assert template["previousblockhash"] == tip(funded)
    assert template["coinbaseaux"] == {}
    assert template["coinbasevalue"] == SUBSIDY + 3_000
    assert template["target"] == "7fffff" + "00" * 29
    assert template["mutable"] == ["time", "transactions", "prevblock"]
    assert template["noncerange"] == "00000000ffffffff"
    assert (template["sigoplimit"], template["sizelimit"]) == (80_000, 4_000_000)
    assert template["weightlimit"] == 4_000_000
    assert template["bits"] == "207fffff"
    assert template["height"] == height(funded) + 1
    assert template["curtime"] >= template["mintime"]
    assert template["longpollid"] == tip(funded).hex() + str(
        funded.mempool.transactions_updated
    )
    assert [tx["txid"] for tx in template["transactions"]] == [
        parent.id.hex(),
        child.id.hex(),
    ]
    assert template["transactions"][1] == {
        "data": child.serialize(include_witness=True).hex(),
        "txid": child.id.hex(),
        "hash": child.hash.hex(),
        "depends": [1],
        "fee": 2_000,
        "sigops": 0,
        "weight": child.weight,
    }
    assert template["transactions"][0]["depends"] == []
    assert template["default_witness_commitment"].startswith("6a24aa21a9ed")
    json.dumps(template, default=lambda b: b.hex())


def stub(
    chain: Main | SigNet,
    *,
    peers: int,
    syncing: bool,
    tip_height: int = 0,
    pending: int = 0,
) -> Node:
    """Give a stand-in for another chain's node, with what a template reads."""
    return cast(
        "Node",
        SimpleNamespace(
            chain=chain,
            p2p_manager=SimpleNamespace(
                connections=dict.fromkeys(range(peers)),
                pending_connections=dict.fromkeys(range(peers, peers + pending)),
            ),
            is_initial_block_download=syncing,
            chainstate=SimpleNamespace(
                block_index=SimpleNamespace(
                    active_chain=[bytes(32)] * (tip_height + 1), header_dict={}
                )
            ),
            unknown_activations=UnknownActivations(chain),
            mempool=SimpleNamespace(transactions_updated=5),
            template_transactions_updated=0,
        ),
    )


def test_the_version_and_deployments_follow_the_period_boundaries(node: Node) -> None:
    """Regtest's `testdummy` is `STARTED`, `LOCKED_IN`, `ACTIVE`, a period each.

    The versions and `vbavailable` are those of `bitcoind` v31.1.0 from
    its regtest height 144, and the blocks mined carry the template's
    version.
    """
    started = {"testdummy": 28}
    signalling = 0x30000000

    def template() -> dict[str, Any]:
        answer = get_block_template(node, CONN, [{"rules": ["segwit"]}])
        assert isinstance(answer, dict)
        return answer

    def mined_version() -> int:
        block_index = node.chainstate.block_index
        return block_index.header_dict[block_index.active_chain[-1]].header.version

    generate_to_address(node, CONN, [142, ADDRESS])
    assert (template()["version"], template()["vbavailable"]) == (0x20000000, {})

    generate_to_address(node, CONN, [1, ADDRESS])
    assert mined_version() == 0x20000000
    assert (template()["version"], template()["vbavailable"]) == (
        signalling,
        started,
    )

    generate_to_address(node, CONN, [144, ADDRESS])
    assert mined_version() == signalling
    assert (template()["version"], template()["vbavailable"]) == (
        signalling,
        started,
    )
    assert template()["rules"] == ["csv", "!segwit", "taproot"]

    generate_to_address(node, CONN, [144, ADDRESS])
    assert (template()["version"], template()["vbavailable"]) == (0x20000000, {})
    assert template()["rules"] == ["csv", "!segwit", "taproot", "testdummy"]


def test_a_mainnet_template_needs_a_peer_and_a_node_out_of_initial_sync() -> None:
    """Core's -9 and -10, and nothing of the kind on a test chain."""
    request = [{"rules": ["segwit"]}]
    assert refusal(
        lambda: get_block_template(stub(Main(), peers=0, syncing=False), CONN, request)
    ) == (
        -9,
        "Btclib node is not connected!",
    )
    assert refusal(
        lambda: get_block_template(stub(Main(), peers=1, syncing=True), CONN, request)
    ) == (
        -10,
        "Btclib node is in initial sync and waiting for blocks...",
    )


def test_a_pending_connection_is_a_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    """`getconnectioncount` counts it, and so does the check."""
    node = stub(Main(), peers=0, syncing=False, tip_height=10, pending=1)
    monkeypatch.setattr(
        rpc_mining,
        "create_new_block",
        lambda n, _s: template_block(n, parent=bytes(32)),
    )

    assert isinstance(get_block_template(node, CONN, [{"rules": ["segwit"]}]), dict)


def test_a_signet_template_needs_the_signet_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's -8 for a call without the rule, and no check of peers."""
    node = stub(SigNet(), peers=0, syncing=True)

    assert refusal(lambda: get_block_template(node, CONN, [{"rules": ["segwit"]}])) == (
        -8,
        (
            "getblocktemplate must be called with the signet rule set "
            '(call with {"rules": ["segwit", "signet"]})'
        ),
    )


def template_block(node: Node, *, parent: bytes) -> BlockTemplate:
    """Return a template with one transaction, for a stand-in node to report."""
    del node
    tx = spend(bytes(range(32)), 1_000)
    coinbase = Tx(
        version=2,
        lock_time=0,
        vin=[TxIn(OutPoint(), b"\x01\x01", 0xFFFFFFFE)],
        vout=[TxOut(5_000_000_000, b"")],
        check_validity=False,
    )
    transactions = [coinbase, tx]
    coinbase.vout.append(witness_commitment_output(transactions, bytes(32)))
    header = BlockHeader(
        0x20000000,
        parent,
        merkle_root_and_mutated_from_transactions(transactions)[0],
        datetime.fromtimestamp(1_700_000_000, UTC),
        bytes.fromhex("1d00ffff"),
        0,
        check_validity=False,
    )
    return BlockTemplate(
        Block(header, transactions, check_validity=False), [1_000], [8], 1_699_999_999
    )


def test_a_mainnet_template_before_segwit_reports_the_limits_of_a_legacy_miner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `!segwit`, no weight limit, and sigops and size divided by four."""
    node = stub(Main(), peers=1, syncing=False, tip_height=10)
    monkeypatch.setattr(
        rpc_mining,
        "create_new_block",
        lambda n, _s: template_block(n, parent=bytes(32)),
    )

    template = get_block_template(node, CONN, [{"rules": ["segwit"]}])

    assert isinstance(template, dict)
    assert template["rules"] == ["csv"]
    assert (template["sigoplimit"], template["sizelimit"]) == (20_000, 1_000_000)
    assert "weightlimit" not in template
    assert template["transactions"][0]["sigops"] == 2
    assert template["target"] == "00000000ffff" + "00" * 26
    assert template["height"] == 11


def test_a_signet_template_names_the_signet_rule_and_its_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`!signet` is a rule, and the challenge is reported."""
    node = stub(SigNet(), peers=0, syncing=True, tip_height=10)
    monkeypatch.setattr(
        rpc_mining,
        "create_new_block",
        lambda n, _s: template_block(n, parent=bytes(32)),
    )

    template = get_block_template(node, CONN, [{"rules": ["segwit", "signet"]}])

    assert isinstance(template, dict)
    assert template["rules"] == ["csv", "!segwit", "!signet", "taproot"]
    assert template["signet_challenge"] == SIGNET_CHALLENGE.hex()
    assert (
        list(template).index("signet_challenge") == list(template).index("height") + 1
    )


TEMPLATE = {"rules": ["segwit"]}


class Clock:
    """A `monotonic` the test moves by hand."""

    def __init__(self) -> None:
        """Start at an arbitrary moment."""
        self.now = 1_000.0

    def __call__(self) -> float:
        """Answer the moment the test last set."""
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Give `rpc.mining` a clock that moves only when the test moves it."""
    clock = Clock()
    monkeypatch.setattr(rpc_mining, "monotonic", clock)
    return clock


def template(node: Node) -> dict[str, Any]:
    """Return a template, without a long poll."""
    answer_ = get_block_template(node, CONN, [TEMPLATE])
    assert isinstance(answer_, dict)
    return answer_


def long_poll(node: Node, long_poll_id: object) -> Generator[bool, None, Any]:
    """Return the job of a long poll on `long_poll_id`."""
    job = get_block_template(node, CONN, [{**TEMPLATE, "longpollid": long_poll_id}])
    assert isinstance(job, Generator)
    return job


def add_spend(node: Node) -> Tx:
    """Add to the mempool a transaction spending the first coinbase."""
    tx = spend(coinbase_txid(node, 1), SUBSIDY - 1_000)
    assert node.mempool.add_tx(tx, 1_000)
    return tx


def test_a_long_poll_answers_once_the_tip_changes(node: Node, clock: Clock) -> None:
    """Core's `waitTipChanged`, looked at on every step."""
    job = long_poll(node, template(node)["longpollid"])
    assert next(job) is False
    assert next(job) is False

    [mined] = generate_to_address(node, CONN, [1, ADDRESS])

    assert finish(job)["previousblockhash"] == mined


def test_a_long_poll_sees_the_mempool_only_after_a_minute(
    funded: Node, clock: Clock
) -> None:
    """Core's first `checktxtime` is one minute."""
    job = long_poll(funded, template(funded)["longpollid"])
    tx = add_spend(funded)
    clock.now += 59
    assert next(job) is False

    clock.now += 1

    assert [t["txid"] for t in finish(job)["transactions"]] == [tx.id.hex()]


def test_a_long_poll_looks_at_the_mempool_every_ten_seconds_after_that(
    funded: Node, clock: Clock
) -> None:
    """A change after the first look is seen ten seconds after it."""
    job = long_poll(funded, template(funded)["longpollid"])
    clock.now += 60
    assert next(job) is False
    add_spend(funded)
    clock.now += 9
    assert next(job) is False

    clock.now += 1

    assert len(finish(job)["transactions"]) == 1


def test_a_long_poll_on_another_tip_answers_at_once(node: Node, clock: Clock) -> None:
    """A hash that is not the tip, a block or not, has already changed."""
    job = long_poll(node, "00" * 32 + "0")

    with pytest.raises(StopIteration) as done:
        next(job)
    assert done.value.value["previousblockhash"] == tip(node)


def test_a_long_poll_id_that_is_not_a_string_waits_past_the_last_template(
    funded: Node, clock: Clock
) -> None:
    """Core reads the tip, and the counter of the last template it built."""
    template(funded)
    add_spend(funded)
    job = long_poll(funded, 5)
    assert next(job) is False

    clock.now += 60

    with pytest.raises(StopIteration) as done:
        next(job)
    assert len(done.value.value["transactions"]) == 1


def test_a_long_poll_answers_shutting_down_once_the_node_stops(
    node: Node, clock: Clock
) -> None:
    """Core's `RPC_CLIENT_NOT_CONNECTED`, once `IsRPCRunning` is false."""
    job = long_poll(node, template(node)["longpollid"])
    assert next(job) is False

    node.terminate_flag.set()

    assert refusal(lambda: finish(job)) == (-9, "Shutting down")


@pytest.mark.parametrize(
    ("suffix", "counter"),
    [
        ("", 0),
        ("7", 7),
        ("+7", 7),
        (" \t7x", 7),
        ("+-7", 0),
        ("x7", 0),
        ("-1", 2**32 - 1),
        ("4294967297", 1),
        ("9" * 20, 2**32 - 1),
        ("-" + "9" * 20, 0),
    ],
)
def test_a_long_poll_id_is_read_as_core_reads_it(
    node: Node, suffix: str, counter: int
) -> None:
    """`LocaleIndependentAtoi<int64_t>`, kept as an `unsigned int`."""
    assert _long_poll_id(node, tip(node).hex().upper() + suffix) == (
        tip(node),
        counter,
    )


def test_the_long_poll_counter_starts_at_zero_and_counts_each_block(
    node: Node,
) -> None:
    """As `bitcoind` v31.1.0's after a restart: 0, then one per block."""
    assert template(node)["longpollid"] == tip(node).hex() + "0"

    generate_to_address(node, CONN, [3, ADDRESS])

    assert template(node)["longpollid"] == tip(node).hex() + "3"


def test_the_long_poll_counter_counts_transactions_and_disconnections(
    funded: Node,
) -> None:
    """One per transaction added or removed, and one per tip change."""
    before = funded.mempool.transactions_updated
    add_spend(funded)
    [mined] = generate_to_address(funded, CONN, [1, ADDRESS])
    assert funded.mempool.transactions_updated == before + 3

    callbacks["invalidateblock"](funded, CONN, [mined.hex()])

    assert funded.mempool.size == 1
    assert funded.mempool.transactions_updated == before + 5


def test_waitforblockheight_is_served_as_core_names_it() -> None:
    """In the table, with Core's argument names and category."""
    assert callbacks["waitforblockheight"] is wait_for_block_height
    assert arg_names["waitforblockheight"] == ("height", "timeout")
    assert CATEGORY["waitforblockheight"] == "Blockchain"


def wrong_type(*positions: tuple[int, str, str]) -> str:
    """Return Core's `Wrong type passed` message for `positions`."""
    lines = ",\n".join(
        f'    "Position {position} ({name})": "JSON value of type {kind} '
        'is not of expected type number"'
        for position, name, kind in positions
    )
    return "Wrong type passed:\n{\n" + lines + "\n}"


@pytest.mark.parametrize(
    ("params", "code", "message"),
    [
        ([], -1, HELP_TEXT["waitforblockheight"]),
        (["x"], -3, wrong_type((1, "height", "string"))),
        ([None], -3, wrong_type((1, "height", "null"))),
        ([True], -3, wrong_type((1, "height", "bool"))),
        (
            ["x", "y"],
            -3,
            wrong_type((1, "height", "string"), (2, "timeout", "string")),
        ),
        ([1.5, "y"], -3, wrong_type((2, "timeout", "string"))),
        ([1.5], -1, "JSON integer out of range"),
        ([2**31], -1, "JSON integer out of range"),
        ([0, 1.5], -1, "JSON integer out of range"),
        ([0, 2**31], -1, "JSON integer out of range"),
        ([0, -1], -1, "Negative timeout"),
    ],
)
def test_waitforblockheight_refuses_what_core_refuses(
    node: Node, params: list[Any], code: int, message: str
) -> None:
    """The codes and messages are `bitcoind` v31.1.0's, on regtest."""
    assert refusal(lambda: wait_for_block_height(node, CONN, params)) == (
        code,
        message,
    )


@pytest.mark.parametrize("params", [[0], [-1], [0, 10], [0, None]])
def test_waitforblockheight_answers_at_once_at_or_past_its_height(
    node: Node, params: list[Any]
) -> None:
    """The tip, with no step that waits."""
    with pytest.raises(StopIteration) as done:
        next(wait_for_block_height(node, CONN, params))
    assert done.value.value == {"hash": tip(node), "height": 0}


def test_waitforblockheight_answers_once_the_tip_reaches_its_height(
    node: Node,
) -> None:
    """A block below the height is not enough."""
    job = wait_for_block_height(node, CONN, [2])
    assert next(job) is False
    generate_to_address(node, CONN, [1, ADDRESS])
    assert next(job) is False

    generate_to_address(node, CONN, [1, ADDRESS])

    assert finish(job) == {"hash": tip(node), "height": 2}


@pytest.mark.parametrize("timeout", [0, None])
def test_waitforblockheight_without_a_timeout_waits_on(
    node: Node, clock: Clock, timeout: int | None
) -> None:
    """0, its default, is no timeout."""
    job = wait_for_block_height(node, CONN, [1, timeout])
    clock.now += 10**6

    assert next(job) is False


def test_waitforblockheight_answers_the_tip_at_its_timeout(
    node: Node, clock: Clock
) -> None:
    """The timeout is in milliseconds."""
    job = wait_for_block_height(node, CONN, [1, 1_000])
    clock.now += 0.75
    assert next(job) is False

    clock.now += 0.25

    assert finish(job) == {"hash": tip(node), "height": 0}


def test_waitforblockheight_answers_the_tip_once_the_node_stops(node: Node) -> None:
    """As Core's answers it on shutdown."""
    job = wait_for_block_height(node, CONN, [1])
    assert next(job) is False

    node.terminate_flag.set()

    assert finish(job) == {"hash": tip(node), "height": 0}
