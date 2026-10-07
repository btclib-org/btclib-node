# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`-assumevalid`: when `main._validate_block` skips a block's scripts.

Core's `feature_assumevalid.py` (at bitcoin/bitcoin@9be056a8a7, the v31.1
tag) builds a chain whose block 102 spends a coinbase with an invalid
signature and buries it under 2100 blocks, regtest's two weeks being 2016
blocks of 600 seconds. Each test here offers a node that chain, or one
of its variants, and asks whether the bad block is connected or refused,
and which of Core's reasons the log gives for verifying a block.
"""

import functools
from typing import TYPE_CHECKING

import pytest
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import main
from btclib_node.chains import RegTest
from btclib_node.chainstate.block_index import calculate_work
from btclib_node.exceptions import BlockScriptVerifyError
from tests import (
    LogLines,
    anyone_can_spend,
    build_block,
    generate_coinbase,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib.block import Block

    from btclib_node import Node

# `_chain`'s default: the one block whose script is invalid, the first a
# coinbase of block 1 is old enough to be spent
BAD = 102

# blocks above the bad one in Core's test, more than the 2016 blocks of
# `pow_target_spacing` that make two weeks
BURIED = 2100


def _spend(funding: Tx, script_sig: bytes) -> Tx:
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(funding.id, 0),
                script_sig=script_sig,
                sequence=0xFFFFFFFF,
            )
        ],
        vout=[TxOut(value=funding.vout[0].value, script_pub_key=anyone_can_spend())],
    )


def _bad_spend(funding: Tx) -> Tx:
    """Spend `funding`'s first output with a script that fails."""
    return _spend(funding, script.serialize(["OP_RETURN"]))


@functools.cache
def _chain(length: int, bad: tuple[int, ...] = (BAD,), shift: int = 0) -> list[Block]:
    """Return blocks 1 to `length`, the `bad` ones spending a coinbase badly.

    Block `h` spends the coinbase of block `h - 101`, the oldest one
    mature at `h`. `shift` dates every block later, so that a chain built
    with another one splits from this one at block 1.
    """
    blocks: list[Block] = []
    previous = RegTest().genesis.hash
    for height in range(1, length + 1):
        transactions = [generate_coinbase(height=height)]
        if height in bad:
            transactions.append(_bad_spend(blocks[height - 102].transactions[0]))
        block = build_block(previous, transactions, height + shift)
        blocks.append(block)
        previous = block.header.hash
    return blocks


def _offer(
    node: Node,
    headers: list[Block],
    blocks: list[Block] | None = None,
) -> None:
    """Give `node` `headers` and the data of `blocks`, or all, then connect."""
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in headers])
    for block in headers if blocks is None else blocks:
        block_index.set_downloaded(block.header.hash)
        node.block_db.add_block(block)
    main.activate_best_chain(node)


def _assume(node: Node, block: Block) -> LogLines:
    """Make `node` assume `block` valid, and return the log it will write."""
    node.config.assume_valid = block.header.hash
    lines = LogLines()
    node.logger.addHandler(lines)
    return lines


def _verified(lines: LogLines, block: Block, height: int, reason: str) -> None:
    """Assert the log says verification is on at `block`, for `reason`."""
    assert (
        f"Enabling script verification at block #{height} "
        f"({block.header.hash.hex()}): {reason}." in lines.messages
    )


def _connected(node: Node, height: int) -> None:
    assert len(node.chainstate.block_index.active_chain) == height + 1
    assert node.last_rejected_block is None


def _refused_at(node: Node, block: Block, height: int) -> None:
    """Assert the node refused `block`, at `height`, for its script."""
    assert len(node.chainstate.block_index.active_chain) == height
    assert node.last_rejected_block is not None
    failed_hash, error = node.last_rejected_block
    assert failed_hash == block.header.hash
    assert isinstance(error, BlockScriptVerifyError), repr(error)
    assert "OP_RETURN" in str(error)


def test_a_bad_script_under_the_assumed_valid_block_is_accepted(
    regtest_node: Callable[..., Node],
) -> None:
    """Core's `node1`: scripts are skipped up to the assumed block."""
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    lines = _assume(node, chain[BAD - 1])

    _offer(node, chain)

    _connected(node, BAD + BURIED)
    assert [m for m in lines.messages if "script verification" in m] == [
        f"Disabling script verification at block #1 ({chain[0].header.hash.hex()}).",
        (
            f"Enabling script verification at block #{BAD + 1} "
            f"({chain[BAD].header.hash.hex()}): "
            "block height above assumevalid height."
        ),
    ]


def test_a_block_not_yet_indexed_is_verified(
    regtest_node: Callable[..., Node],
) -> None:
    """`try_connect_block`, Core's `fJustCheck`, is on no chain: it verifies."""
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    _assume(node, chain[BAD - 1])
    _offer(node, chain)
    candidate = build_block(
        chain[-1].header.hash,
        [
            generate_coinbase(height=len(chain) + 1),
            _bad_spend(chain[4].transactions[0]),
        ],
        len(chain) + 1,
    )

    assert (
        main.script_check_reason(node, candidate.header.hash, len(chain) + 1)
        == "block height above assumevalid height"
    )
    with pytest.raises(BlockScriptVerifyError, match="OP_RETURN"):
        main.try_connect_block(node, candidate, len(chain) + 1)


def test_a_bad_script_after_the_assumed_valid_block_is_refused(
    regtest_node: Callable[..., Node],
) -> None:
    """Every block after the assumed one is verified, however deeply buried."""
    node = regtest_node()
    chain = _chain(BAD + BURIED, bad=(BAD, BAD + 1))
    lines = _assume(node, chain[BAD - 1])

    _offer(node, chain)

    _verified(lines, chain[BAD], BAD + 1, "block height above assumevalid height")
    _refused_at(node, chain[BAD], BAD + 1)


def test_a_bad_script_is_refused_without_assumevalid(
    regtest_node: Callable[..., Node],
) -> None:
    """Core's `node0`: the bad block ends the chain at the block before it."""
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    lines = LogLines()
    node.logger.addHandler(lines)

    _offer(node, chain)

    _verified(lines, chain[0], 1, "assumevalid=0 (always verify)")
    _refused_at(node, chain[BAD - 1], BAD)


def test_a_bad_script_is_refused_where_the_assumed_block_is_not_buried(
    regtest_node: Callable[..., Node],
) -> None:
    """Core's `node2`: the best header is 98 blocks above the assumed one."""
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    lines = _assume(node, chain[BAD - 1])

    _offer(node, chain[:200])

    _verified(lines, chain[0], 1, "block too recent relative to best header")
    _refused_at(node, chain[BAD - 1], BAD)


@pytest.mark.parametrize(
    ("above", "skipped"), [(2016, False), (2017, True)], ids=["two_weeks", "past_it"]
)
def test_the_assumed_block_is_buried_by_more_than_two_weeks_of_work(
    regtest_node: Callable[..., Node], above: int, *, skipped: bool
) -> None:
    """2016 blocks of 600 seconds are two weeks, and `<=` still verifies."""
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    lines = _assume(node, chain[BAD - 1])

    _offer(node, chain[: BAD + above], chain[: BAD + 1])

    if skipped:
        _connected(node, BAD + 1)
    else:
        _verified(
            lines, chain[BAD - 1], BAD, "block too recent relative to best header"
        )
        _refused_at(node, chain[BAD - 1], BAD)


@pytest.mark.parametrize(("short_by", "skipped"), [(1, False), (0, True)])
def test_the_best_header_needs_the_minimum_chain_work(
    regtest_node: Callable[..., Node], short_by: int, *, skipped: bool
) -> None:
    """Core's `node5`, `-minimumchainwork`: work equal to it is enough."""
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    lines = _assume(node, chain[BAD - 1])
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    node.config.minimum_chain_work = (
        block_index.chainwork[chain[-1].header.hash] + short_by
    )

    _offer(node, chain, chain[: BAD + 1])

    if skipped:
        _connected(node, BAD + 1)
    else:
        _verified(lines, chain[0], 1, "best header chainwork below minimumchainwork")
        _refused_at(node, chain[BAD - 1], BAD)


def test_a_bad_script_is_refused_where_the_assumed_block_has_no_header(
    regtest_node: Callable[..., Node],
) -> None:
    """Core's `node5`, "assumevalid hash not in headers"."""
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    lines = _assume(node, chain[BAD - 1])
    node.config.assume_valid = bytes.fromhex("12345678" * 8)

    _offer(node, chain[: BAD + 1])

    _verified(lines, chain[0], 1, "assumevalid hash not in headers")
    _refused_at(node, chain[BAD - 1], BAD)


def test_a_bad_script_is_refused_off_the_best_header_chain(
    regtest_node: Callable[..., Node],
) -> None:
    """Core's `node3`: block 1 is not on the best, longer header chain."""
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    longer = _chain(BAD + 50, shift=10_000)
    lines = _assume(node, chain[BAD - 1])
    # first, so that `get_first_candidate`'s scan of 100 reaches them
    node.chainstate.block_index.add_headers(
        [block.header for block in chain[: BAD + 1]]
    )
    node.chainstate.block_index.add_headers([block.header for block in longer])

    _offer(node, chain[: BAD + 1])

    _verified(lines, chain[0], 1, "block not in best header chain")
    _refused_at(node, chain[BAD - 1], BAD)


def test_a_bad_script_is_refused_off_the_assumed_chain(
    regtest_node: Callable[..., Node],
) -> None:
    """Core's `node4`: a branch from block 1 is not under the assumed block."""
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    branch = _chain(BAD, shift=10_000)
    lines = _assume(node, chain[BAD - 1])
    # first, so that `get_first_candidate`'s scan of 100 reaches it
    node.chainstate.block_index.add_headers([block.header for block in branch])
    node.chainstate.block_index.add_headers([block.header for block in chain])

    _offer(node, branch)

    _verified(lines, branch[0], 1, "block not in assumevalid chain")
    _refused_at(node, branch[BAD - 1], BAD)


def test_the_burial_is_measured_in_the_best_headers_work(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core divides by `GetBlockProof` of the best header, not of the block.

    Regtest never changes `bits`, so only a best header of other work tells
    the two apart: doubled, 2017 blocks are one week, not two.
    """
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    lines = _assume(node, chain[BAD - 1])
    best = chain[BAD + 2017 - 1].header
    real = calculate_work
    monkeypatch.setattr(
        main,
        "calculate_work",
        lambda header: real(header) * (2 if header.hash == best.hash else 1),
    )

    _offer(node, chain[: BAD + 2017], chain[: BAD + 1])

    _verified(lines, chain[0], 1, "block too recent relative to best header")
    _refused_at(node, chain[BAD - 1], BAD)


def test_a_block_at_the_assumed_height_is_not_in_the_assumed_chain(
    regtest_node: Callable[..., Node],
) -> None:
    """Only a block above the assumed height is "above" it."""
    node = regtest_node()
    chain = _chain(BAD + BURIED)
    branch = _chain(BAD, shift=10_000)
    _assume(node, chain[BAD - 1])
    node.chainstate.block_index.add_headers([block.header for block in chain])
    node.chainstate.block_index.add_headers([block.header for block in branch])

    assert (
        main.script_check_reason(node, branch[BAD - 1].header.hash, BAD)
        == "block not in assumevalid chain"
    )
