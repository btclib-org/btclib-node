# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`update_chain`/`verify_mempool_acceptance`: connect, reorg, reject."""

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.block import Block, witness_commitment_output
from btclib.consensus import MAX_BLOCK_WEIGHT
from btclib.exceptions import BTClibValueError
from btclib.fee import FeeRate, dust_threshold, fee_from_vsize
from btclib.p2p.compact_blocks import CmpctBlock
from btclib.p2p.inventory import (
    GetBlocks,
    Headers,
    Inv,
    Inventory,
    InventoryType,
)
from btclib.p2p.limits import PROTOCOL_VERSION
from btclib.script import script
from btclib.script.engine.flags import ScriptFlag
from btclib.script.script_pub_key import ScriptPubKey
from btclib.script.witness import Witness
from btclib.tx.limits import COINBASE_MATURITY
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node, main
from btclib_node.block_db import BlockDB
from btclib_node.chains import RegTest, SigNet
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate import utxo_index as utxo_index_module
from btclib_node.chainstate.block_index import BlockIndex, BlockInfo, BlockStatus
from btclib_node.config import Config
from btclib_node.constants import (
    MAX_TIP_AGE,
    MIN_BLOCKS_TO_KEEP,
    MIN_PRUNE_TARGET_MIB,
    NodeStatus,
    P2pConnStatus,
)
from btclib_node.exceptions import (
    ChainstateInconsistencyError,
    MisbehavingError,
    MissingPrevoutError,
    NonStandardTxError,
    TxRejectedError,
)
from btclib_node.interpreter import check_scripts, get_flags
from btclib_node.log import Logger
from btclib_node.main import (
    check_fork_warning_conditions,
    prune_up_to_height,
    update_chain,
    verify_mempool_acceptance,
)
from btclib_node.mempool import format_money
from btclib_node.p2p.block_availability import BlockAvailability
from btclib_node.p2p.callbacks import getblocks
from btclib_node.p2p.compact_block import MostRecentBlock, compact_block
from tests import (
    anyone_can_spend,
    anyone_can_spend_redeem_script,
    anyone_can_spend_script_sig,
    build_block,
    generate_coinbase,
    generate_random_chain,
    generate_random_header_chain,
    generate_random_transaction,
    generate_segwit_block,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib_node.block_db import Coin
    from btclib_node.p2p.connection import Connection


# what a mempool candidate below pays where the test is not about its fee:
# over `Config.min_relay_feerate`'s 100 sat/kvB for any size built here
FEE = 1_000


@pytest.fixture
def node(regtest_node: Callable[[], Node]) -> Node:
    """Give one header-synced regtest node, built fresh for the test."""
    return regtest_node()


def connect(node: Node, chain: list[Block]) -> BlockIndex:
    """Offer a chain to the node, drive it to connect what it will, and flush.

    The flush is what most callers of this helper actually want: a
    result on disk, staged nowhere, to make assertions against --
    `Chainstate.flush`'s own bound (`UtxoIndex._FLUSH_BOUND`) is a
    throughput question for a real sync and not one this helper's own
    short chains are testing.
    """
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(node)
    node.chainstate.flush()
    return block_index


def rejected_because(node: Node, block: Block, phrase: str) -> None:
    """Assert `block` is `node`'s own last rejection, and that `phrase` is why.

    `Node.last_rejected_block` pairs the hash `update_chain`'s trial
    loop was on with the exception it raised; matching both is what
    tells a block refused for its own rule apart from one refused for a
    different rule ranked ahead of it in the same per-block gate --
    the gap btclib-org/btclib-node#587 is about, where any raise
    anywhere in `_validate_block`/`check_scripts` satisfied a bare
    `not in active_chain`. `phrase` is checked with `in` rather than
    `==`: the exact wording is btclib's or this tree's own to change,
    not an interface either promises to keep, and a substring naming
    the rule is what a future rewording is least likely to break.
    """
    assert node.last_rejected_block is not None
    failed_hash, exc = node.last_rejected_block
    assert failed_hash == block.header.hash
    assert phrase in str(exc), str(exc)


def test_chain(node: Node) -> None:
    """A chain of headers added in batches of at most 2000 all connect."""
    length = 2000 * 1  # 2000
    chain = generate_random_chain(length, RegTest().genesis.hash)
    headers = [block.header for block in chain]
    block_index = node.chainstate.block_index
    for start in range(0, length, 2000):
        block_index.add_headers(headers[start : start + 2000])
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(node)
    assert len(block_index.active_chain) == length + 1


def test_a_getblocks_activates_the_best_chain_first(node: Node) -> None:
    """A block downloaded and not yet connected is in the `inv` it asks for.

    `callbacks.block` announces a block from `new_pow_valid_block`, and
    `update_chain` connects it only after the loop's p2p share, so a
    `getblocks` processed between the two would otherwise be answered
    without it, as Core's `ActivateBestChain` call prevents.
    """
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block in chain:
        block_index.set_downloaded(block.header.hash)
        node.block_db.add_block(block)
    assert len(block_index.active_chain) == 1
    sent: list[Any] = []
    peer = SimpleNamespace(send=sent.append, stop=lambda: None, continuation_block=None)
    request = GetBlocks(PROTOCOL_VERSION, [RegTest().genesis.hash]).serialize()
    getblocks(node, request, cast("Connection", peer))
    (answer,) = sent
    assert [item.hash for item in answer.items] == [b.header.hash for b in chain]


def spend(prevout_tx: Tx, value: int, script_sig: bytes | None = None) -> Tx:
    """Return a transaction spending `prevout_tx`'s first output for `value`."""
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(prevout_tx.id, 0),
                script_sig=script_sig
                if script_sig is not None
                else anyone_can_spend_script_sig(),
                sequence=0xFFFFFFFF,
            )
        ],
        vout=[
            TxOut(value=value, script_pub_key=anyone_can_spend()),
        ],
    )


def test_assert_valid_block_asks_the_signet_solution_on_a_signet_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`assert_valid_block`'s own `isinstance(chain, SigNet)` gate, isolated.

    `signet.assert_valid_solution` itself is `tests/unit/signet_test.py`'s;
    what is main's own to answer for is whether `assert_valid_block` calls
    it at all, and only for a signet chain -- proven here by a stub that
    always raises, so a genesis block (which the real function would wave
    through on its own) still shows whether the call happened.
    """

    def always_raises(*_args: object, **_kwargs: object) -> None:
        err_msg = "stub"
        raise MisbehavingError(err_msg)

    monkeypatch.setattr(main, "assert_valid_solution", always_raises)

    with pytest.raises(MisbehavingError):
        main.assert_valid_block(SigNet().genesis_block, SigNet())
    main.assert_valid_block(RegTest().genesis_block, RegTest())  # no raise


def test_reject_block_that_prints_money(node: Node) -> None:
    """A block whose output exceeds its input's value fails to connect."""
    # Script validation never reads the amounts except through the
    # sig_hash, so nothing in the engine notices an output larger than
    # the input it spends. The chain is COINBASE_MATURITY long and the
    # spend is chain[0]'s own coinbase, not chain[-1]'s: a fresher one
    # would be refused for prematurity before ever reaching the rule
    # this test is about.
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    bad = build_block(
        chain[-1].header.hash,
        [
            generate_coinbase(height=len(chain) + 1),
            spend(funding, funding.vout[0].value + 1),
        ],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-txns-in-belowout")


def test_reject_block_with_a_failing_script(node: Node) -> None:
    """A block with an input that fails script validation fails to connect."""
    # An input that does not verify has to fail the block. It used to be
    # written to errors/ and swallowed, inside a worker pool, so nothing
    # reached update_chain and the block was connected anyway. The chain
    # is COINBASE_MATURITY long and the spend is chain[0]'s own coinbase
    # for the same reason as the sibling test above.
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    unspendable = spend(
        funding,
        funding.vout[0].value,
        script_sig=script.serialize(["OP_RETURN"]),
    )
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), unspendable],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "OP_RETURN")


def test_reject_block_whose_coinbase_pays_more_than_subsidy_plus_fees(
    node: Node,
) -> None:
    """A coinbase paying far more than subsidy plus fees fails to connect."""
    # btclib-org/btclib-node#568: nothing used to compare a coinbase
    # against what it is allowed to pay, so this connected.
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    printed = 21_000_000 * 10**8
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(printed, height=len(chain) + 1)],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-cb-amount")


# MAX_BLOCK_SIGOPS_COST, 80_000, is reached below exactly: each
# `_sigop_script` counts 3_800 sigops, 20 per bare OP_CHECKMULTISIG, so
# 4 p2sh inputs * 3_800 * 4 + 5 p2wsh inputs * 3_800 + 50 coinbase
# OP_CHECKSIGs * 4.
_P2SH_INPUTS, _P2WSH_INPUTS, _MULTISIGS, _COINBASE_CHECKSIGS = 4, 5, 190, 50


def _sigop_script(extra_checksigs: int = 0) -> bytes:
    """Return a script true on an empty stack, of `20 * _MULTISIGS` sigops.

    `extra_checksigs` more `OP_CHECKSIG`s add one sigop each, all of
    them in the branch `OP_0 OP_IF` never runs, so the scripts verify.
    """
    return script.serialize(
        [
            "OP_0",
            "OP_IF",
            *["OP_CHECKMULTISIG"] * _MULTISIGS,
            *["OP_CHECKSIG"] * extra_checksigs,
            "OP_ENDIF",
            "OP_1",
        ]
    )


def _sigop_blocks(node: Node) -> tuple[BlockIndex, Callable[..., Block]]:
    """Connect a funded chain; return a builder of blocks near the sigop limit.

    The builder's block spends `_P2SH_INPUTS` p2sh and `_P2WSH_INPUTS`
    p2wsh outputs, each through `_sigop_script`, and its coinbase carries
    `_COINBASE_CHECKSIGS` legacy sigops: exactly `MAX_BLOCK_SIGOPS_COST`
    with no `*_over` set, and one sigop more of that kind for each set.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    funding = chain[0].transactions[0]
    # one p2sh and one p2wsh output more, committing to a script with an
    # extra sigop, so that a block over the limit still verifies and
    # the sigop cost is the one rule it breaks
    p2sh = [_sigop_script()] * _P2SH_INPUTS + [_sigop_script(1)]
    p2wsh = [_sigop_script()] * _P2WSH_INPUTS + [_sigop_script(1)]
    value = funding.vout[0].value // (len(p2sh) + len(p2wsh))
    fund = spend(funding, 0)
    fund.vout = [
        *[TxOut(value, ScriptPubKey.p2sh(redeem)) for redeem in p2sh],
        *[TxOut(value, ScriptPubKey.p2wsh(witness)) for witness in p2wsh],
    ]
    fund_block = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), fund],
        len(chain),
    )
    block_index = connect(node, [*chain, fund_block])
    assert fund_block.header.hash in block_index.active_chain

    def build(
        *,
        p2sh_over: bool = False,
        witness_over: bool = False,
        coinbase_over: bool = False,
        prints_money_after: bool = False,
    ) -> Block:
        # the outputs the block spends: the last p2sh one in place of the
        # first where `p2sh_over`, and likewise for p2wsh
        p2sh_vouts = list(range(_P2SH_INPUTS))
        if p2sh_over:
            p2sh_vouts[0] = _P2SH_INPUTS
        p2wsh_vouts = [len(p2sh) + i for i in range(_P2WSH_INPUTS)]
        if witness_over:
            p2wsh_vouts[0] = len(p2sh) + _P2WSH_INPUTS
        tx = spend(fund, value)
        tx.vin = [
            *[
                TxIn(OutPoint(fund.id, i), script.serialize([p2sh[i]]))
                for i in p2sh_vouts
            ],
            *[
                TxIn(
                    OutPoint(fund.id, i),
                    b"",
                    script_witness=Witness([p2wsh[i - len(p2sh)]]),
                )
                for i in p2wsh_vouts
            ],
        ]
        coinbase = generate_coinbase(height=len(chain) + 2)
        checksigs = _COINBASE_CHECKSIGS + coinbase_over
        nonce = bytes(32)
        coinbase.vout = [
            *coinbase.vout,
            TxOut(0, script.serialize(["OP_CHECKSIG"] * checksigs)),
        ]
        txs = [tx]
        if prints_money_after:
            # a coinbase `COINBASE_MATURITY` deep by this block's height
            printed = chain[1].transactions[0]
            txs.append(spend(printed, printed.vout[0].value + 1))
        coinbase.vout.append(witness_commitment_output([coinbase, *txs], nonce))
        coinbase.vin[0].script_witness = Witness([nonce])
        return build_block(fund_block.header.hash, [coinbase, *txs], len(chain) + 1)

    return block_index, build


def test_a_block_at_the_sigop_cost_limit_connects(node: Node) -> None:
    """Exactly `MAX_BLOCK_SIGOPS_COST`, legacy, p2sh and witness, connects."""
    block_index, build = _sigop_blocks(node)
    good = build()
    connect(node, [good])
    assert good.header.hash in block_index.active_chain


@pytest.mark.parametrize(
    "over",
    [{"witness_over": True}, {"p2sh_over": True}, {"coinbase_over": True}],
    ids=["witness", "p2sh", "coinbase"],
)
def test_a_block_over_the_sigop_cost_limit_is_refused(
    node: Node, over: dict[str, bool]
) -> None:
    """One sigop more of any kind is `bad-blk-sigops`, and the block invalid.

    btclib-org/btclib-node#1585: only `CheckBlock`'s legacy count was
    bounded, so this connected. Each case adds to one term alone, so
    each proves that term is counted: a witness sigop costs one, a p2sh
    sigop and a coinbase's legacy one cost four.
    """
    block_index, build = _sigop_blocks(node)
    bad = build(**over)
    connect(node, [bad])
    assert bad.header.hash not in block_index.active_chain
    rejected_because(node, bad, "bad-blk-sigops")
    assert block_index.get_block_info(bad.header.hash).status == BlockStatus.invalid


def test_a_block_s_sigop_cost_is_refused_before_a_later_transaction_s_amounts(
    node: Node,
) -> None:
    """The first transaction to pass the limit refuses the block, not the last.

    Core's `ConnectBlock` adds each transaction's sigop cost to the
    block's total before it checks the next one's inputs, so a block
    whose first spend passes `MAX_BLOCK_SIGOPS_COST` and whose second
    prints money is `bad-blk-sigops` (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Without the first
    spend's excess the same block is refused for the second's amounts.
    """
    block_index, build = _sigop_blocks(node)
    control = build(prints_money_after=True)
    connect(node, [control])
    rejected_because(node, control, "bad-txns-in-belowout")
    bad = build(p2sh_over=True, prints_money_after=True)
    connect(node, [bad])
    assert bad.header.hash not in block_index.active_chain
    rejected_because(node, bad, "bad-blk-sigops")


def test_the_sigop_cost_is_counted_under_the_block_s_own_flags(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without P2SH and WITNESS, as at a block Core exempts, only legacy counts.

    Core's `GetBlockScriptFlags` turns both off for the one mainnet block
    that broke BIP16, and `GetTransactionSigOpCost` then counts neither
    term; regtest exempts no block, so the flags are patched in.
    """
    block_index, build = _sigop_blocks(node)
    monkeypatch.setattr(main, "get_flags", lambda *_: ScriptFlag(0))
    over = build(p2sh_over=True, witness_over=True)
    connect(node, [over])
    assert over.header.hash in block_index.active_chain


def test_a_block_is_refused_for_the_first_rule_in_transaction_order(
    node: Node,
) -> None:
    """A spend's own rules are asked in turn, ahead of the next spend's.

    ISS 1587: Core's `ConnectBlock` asks one transaction's maturity,
    amounts, accumulated fee, BIP68 lock and sigop cost before it reads
    the next, so a block with an immature spend first and an unmet
    relative lock second is `bad-txns-premature-spend-of-coinbase`, and
    with the two the other way round `bad-txns-nonfinal`
    (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)
    # one block short of mature, and the oldest, which a lock then binds
    young, old = chain[1].transactions[0], chain[0].transactions[0]
    immature = spend(young, young.vout[0].value)
    unmet = relative_locked_spend(
        old, old.vout[0].value, sequence=COINBASE_MATURITY + 50
    )

    for transactions, reason in (
        ([immature, unmet], "bad-txns-premature-spend-of-coinbase"),
        ([unmet, immature], "bad-txns-nonfinal"),
    ):
        bad = build_block(
            chain[-1].header.hash,
            [generate_coinbase(height=len(chain) + 1), *transactions],
            len(chain),
        )
        connect(node, [bad])
        assert len(block_index.active_chain) == connected
        rejected_because(node, bad, reason)


def test_a_coinbase_paying_too_much_is_refused_before_a_failing_script(
    node: Node,
) -> None:
    """`bad-cb-amount` is Core's answer where a script also fails.

    ISS 1587: `ConnectBlock` checks the coinbase value after the loop
    over the transactions and collects the script results last, so a
    block whose spend fails its script and whose coinbase pays one
    satoshi too much is `bad-cb-amount` (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    funding = chain[0].transactions[0]
    failing = spend(
        funding, funding.vout[0].value, script_sig=script.serialize(["OP_RETURN"])
    )
    subsidy_here = generate_coinbase(height=len(chain) + 1).vout[0].value
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(subsidy_here + 1, height=len(chain) + 1), failing],
        len(chain),
    )
    connect(node, [bad])
    assert bad.header.hash not in block_index.active_chain
    rejected_because(node, bad, "bad-cb-amount")


@pytest.mark.parametrize(
    ("limit_offset", "refused"), [(0, False), (-1, True)], ids=["at", "over"]
)
def test_the_fees_a_block_accumulates_are_bounded_by_max_money(
    node: Node,
    monkeypatch: pytest.MonkeyPatch,
    limit_offset: int,
    refused: bool,  # noqa: FBT001
) -> None:
    """ISS 1587: `MoneyRange` on the accumulated fee, its bound inclusive.

    No block can carry fees near 21 million bitcoin, so the bound is
    brought down to the fees this block's two spends pay together: at it
    the block connects, one satoshi under it, with each spend's own fee
    well inside, is `bad-txns-accumulated-fee-outofrange`
    (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    chain = generate_random_chain(COINBASE_MATURITY + 1, RegTest().genesis.hash)
    block_index = connect(node, chain)
    fee = 10
    # a coinbase `COINBASE_MATURITY` deep, and an output the last block made
    # to be spent
    coins_spent = [chain[1].transactions[0], chain[-1].transactions[1]]
    spends = [spend(tx, tx.vout[0].value - fee) for tx in coins_spent]
    subsidy_here = generate_coinbase(height=len(chain) + 1).vout[0].value
    block = build_block(
        chain[-1].header.hash,
        [generate_coinbase(subsidy_here + 2 * fee, height=len(chain) + 1), *spends],
        len(chain),
    )
    monkeypatch.setattr(main, "_MAX_MONEY", 2 * fee + limit_offset)
    connect(node, [block])
    assert (block.header.hash in block_index.active_chain) is not refused
    if refused:
        rejected_because(node, block, "bad-txns-accumulated-fee-outofrange")


def test_reject_block_whose_coinbase_does_not_commit_to_its_height(
    node: Node,
) -> None:
    """A coinbase committing to no height at all fails to connect (BIP34)."""
    # btclib-org/btclib-node#571: Block.assert_valid_contextual was never
    # called, so this connected -- regtest enforces BIP34 from height 1.
    chain = generate_random_chain(1, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    bad = build_block(chain[-1].header.hash, [generate_coinbase()], len(chain))
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-cb-height")


def test_reject_block_whose_coinbase_height_and_a_transaction_are_both_bad(
    node: Node,
) -> None:
    """Core checks finality before the coinbase height commitment.

    A wrong-height coinbase (BIP34) and a non-final transaction fail in
    the same block; `bad-txns-nonfinal` is the answer, Core's own order
    (`ContextualCheckBlock`, `src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). ISS 1335.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    nonfinal = locked_spend(
        funding, funding.vout[0].value, lock_time=2_000_000_000, sequence=0
    )
    bad = build_block(
        chain[-1].header.hash, [generate_coinbase(), nonfinal], len(chain)
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-txns-nonfinal")


def test_reject_block_spending_a_coinbase_one_short_of_maturity(node: Node) -> None:
    """A spend of a coinbase `COINBASE_MATURITY - 1` deep fails to connect.

    ISS 569: the UTXO record carried neither a coin's height nor whether
    it came from a coinbase, so nothing on this path could tell a fresh
    coinbase from one old enough to spend -- this connected exactly as
    the one `COINBASE_MATURITY` blocks old does in the test right after
    this one.
    """
    chain = generate_random_chain(COINBASE_MATURITY - 1, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    bad = build_block(
        chain[-1].header.hash,
        [
            generate_coinbase(height=len(chain) + 1),
            spend(funding, funding.vout[0].value),
        ],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-txns-premature-spend-of-coinbase")


def test_a_coinbase_spend_at_exactly_maturity_connects(node: Node) -> None:
    """A spend of a coinbase exactly `COINBASE_MATURITY` blocks old connects."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    good = build_block(
        chain[-1].header.hash,
        [
            generate_coinbase(height=len(chain) + 1),
            spend(funding, funding.vout[0].value),
        ],
        len(chain),
    )
    connect(node, [good])

    assert good.header.hash in block_index.active_chain
    assert len(block_index.active_chain) == connected + 1


def test_reject_a_mempool_spend_of_an_immature_coinbase(node: Node) -> None:
    """`verify_mempool_acceptance` refuses the same premature spend.

    Core enforces `COINBASE_MATURITY` at both call sites -- `ConnectBlock`
    and mempool acceptance's own `AcceptToMemoryPoolWorker`
    (`src/validation.cpp:897` and `:2544`, at bitcoin/bitcoin@204256c73f)
    -- so a mempool that only enforced it on the block-connection path
    would relay a spend no peer accepting it into a block ever will.
    """
    chain = generate_random_chain(COINBASE_MATURITY - 1, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    premature = generate_random_transaction(funding.id, value=funding.vout[0].value)
    with pytest.raises(TxRejectedError) as refused:
        verify_mempool_acceptance(node, premature)
    # `bitcoind` v31.1 on regtest: "bad-txns-premature-spend-of-coinbase,
    # tried to spend coinbase at depth 1" for a coinbase one block deep.
    # This one is at height 1 and the spend at the next block's height,
    # one short of maturity (btclib-org/btclib-node#1328).
    assert str(refused.value) == (
        "bad-txns-premature-spend-of-coinbase, "
        f"tried to spend coinbase at depth {COINBASE_MATURITY - 1}"
    )


def locked_spend(
    prevout_tx: Tx,
    value: int,
    lock_time: int,
    sequence: int,
    *,
    version: int = 1,
    script_sig: bytes | None = None,
) -> Tx:
    """Return a transaction spending `prevout_tx`'s first output."""
    return Tx(
        version=version,
        lock_time=lock_time,
        vin=[
            TxIn(
                prev_out=OutPoint(prevout_tx.id, 0),
                script_sig=script_sig
                if script_sig is not None
                else anyone_can_spend_script_sig(),
                sequence=sequence,
            )
        ],
        vout=[
            TxOut(value=value, script_pub_key=anyone_can_spend()),
        ],
    )


def relative_locked_spend(prevout_tx: Tx, value: int, sequence: int) -> Tx:
    """Return a version-2 transaction spending `prevout_tx`, sequence set.

    Version 2, not 1: BIP68 binds a relative lock only from that version
    on (`btclib.tx.tx_context.assert_sequence_locks`' own docstring says
    why).
    """
    return locked_spend(prevout_tx, value, lock_time=0, sequence=sequence, version=2)


def test_reject_block_whose_coinbase_duplicates_an_unspent_txid(node: Node) -> None:
    """Two blocks sharing a coinbase: the second is refused for BIP30.

    ISS 570 / CVE-2012-1909's shape: nothing checked whether a
    coinbase's own txid already named an unspent output, so the second
    block's own write silently overwrote the first's, and a reorg away
    from it would have deleted an output the first block's own branch
    still carries. `UtxoIndex.add_block`'s own BIP30 check runs before
    `block.assert_valid_contextual` (BIP34), so this is refused for
    BIP30 regardless of whether the reused coinbase would also fail
    BIP34's own `bad-cb-height` at this height.
    """
    genesis_hash = RegTest().genesis.hash
    duplicate = generate_coinbase(height=1)
    first = build_block(genesis_hash, [duplicate], 0)
    connect(node, [first])
    block_index = node.chainstate.block_index
    assert first.header.hash in block_index.active_chain

    bad = build_block(first.header.hash, [duplicate], 1)
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    rejected_because(node, bad, "bad-txns-BIP30")

    # the first block's own coinbase output survives the refused
    # duplicate -- the CVE's actual danger, and not covered by the
    # refusal alone
    out_point = OutPoint(duplicate.id, 0)
    key = b"utxo-" + out_point.serialize(check_validity=False)
    assert node.chainstate.utxo_index.db.get(key) is not None


def test_a_block_both_nonfinal_and_without_its_height_is_refused_nonfinal(
    node: Node,
) -> None:
    """Core's `ContextualCheckBlock` asks `bad-txns-nonfinal` before BIP34's.

    ISS 1315's review measured it on bitcoind: a block failing both is
    refused `bad-txns-nonfinal`.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    funding = chain[0].transactions[0]
    nonfinal = locked_spend(
        funding, funding.vout[0].value, lock_time=2_000_000_000, sequence=0
    )
    bad = build_block(
        chain[-1].header.hash, [generate_coinbase(), nonfinal], len(chain)
    )
    connect(node, [bad])
    rejected_because(node, bad, "bad-txns-nonfinal")


def test_reject_block_with_a_transaction_locked_to_the_future(node: Node) -> None:
    """A transaction locked to a 2033 timestamp fails to connect.

    ISS 572's own probe: `sequence=0` -- not `SEQUENCE_FINAL` -- so
    Core's own escape hatch does not rescue it.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    nonfinal = locked_spend(
        funding, funding.vout[0].value, lock_time=2_000_000_000, sequence=0
    )
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), nonfinal],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-txns-nonfinal")


def test_a_transaction_locked_to_an_already_reached_height_connects(
    node: Node,
) -> None:
    """A transaction whose height-based lock_time has passed connects."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    final = locked_spend(funding, funding.vout[0].value, lock_time=1, sequence=0)
    good = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), final],
        len(chain),
    )
    connect(node, [good])

    assert good.header.hash in block_index.active_chain
    assert len(block_index.active_chain) == connected + 1


def test_reject_block_whose_relative_lock_is_not_satisfied(node: Node) -> None:
    """A BIP68 relative lock a hundred blocks away fails to connect.

    `funding` is `COINBASE_MATURITY` blocks old by the time this
    connects -- exactly mature enough to spend, and not old enough for
    a relative lock fifty blocks past that.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=COINBASE_MATURITY + 50
    )
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), unmet],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-txns-nonfinal")


def test_relative_lock_is_not_enforced_when_bip113_is_not_active(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same unmet relative lock connects once BIP113 reads as inactive.

    `_validate_block` skips `assert_sequence_locks` entirely rather than
    calling it with a flag, `btclib.tx.tx_context`'s own module
    docstring says why -- so what this proves is the skip itself, not
    the rule `test_reject_block_whose_relative_lock_is_not_satisfied`
    above already covers. `main.get_flags` is patched to answer as if
    RegTest had not yet activated CHECKSEQUENCEVERIFY, which it does
    from height 1 on (`chains.RegTest`'s own docstring), so there is no
    real chain height this scenario reaches otherwise.
    """

    def flags_without_csv(*args: Any, **kwargs: Any) -> ScriptFlag:
        return get_flags(*args, **kwargs) & ~ScriptFlag.CHECKSEQUENCEVERIFY

    monkeypatch.setattr(main, "get_flags", flags_without_csv)

    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=COINBASE_MATURITY + 50
    )
    good = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), unmet],
        len(chain),
    )
    connect(node, [good])

    assert good.header.hash in block_index.active_chain
    assert len(block_index.active_chain) == connected + 1


def test_a_relative_lock_satisfied_by_elapsed_blocks_connects(node: Node) -> None:
    """A BIP68 relative lock already satisfied by elapsed blocks connects."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    met = relative_locked_spend(funding, funding.vout[0].value, sequence=50)
    good = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), met],
        len(chain),
    )
    connect(node, [good])

    assert good.header.hash in block_index.active_chain
    assert len(block_index.active_chain) == connected + 1


def test_reject_block_whose_time_based_relative_lock_is_not_satisfied(
    node: Node,
) -> None:
    """A BIP68 time-based relative lock far in the future fails to connect.

    Unlike the height-based pair above, this exercises `_validate_block`'s
    own `ancestor_median_time_past` closure -- `header_at_height` walking
    back through real headers rather than a stub -- since a height-based
    lock never reaches it.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    type_flag = 1 << 22
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=type_flag | 1000
    )
    bad = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), unmet],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    assert len(block_index.active_chain) == connected
    rejected_because(node, bad, "bad-txns-nonfinal")


def test_a_time_based_relative_lock_satisfied_by_elapsed_time_connects(
    node: Node,
) -> None:
    """A BIP68 time-based relative lock of zero units connects immediately."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)
    connected = len(block_index.active_chain)

    funding = chain[0].transactions[0]
    type_flag = 1 << 22
    met = relative_locked_spend(funding, funding.vout[0].value, sequence=type_flag | 0)
    good = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), met],
        len(chain),
    )
    connect(node, [good])

    assert good.header.hash in block_index.active_chain
    assert len(block_index.active_chain) == connected + 1


def test_reject_a_mempool_spend_that_is_not_final(node: Node) -> None:
    """`verify_mempool_acceptance` refuses the same non-final transaction.

    Core checks finality in the mempool too, against the tip rather
    than the connecting block -- a mempool that skipped this would
    relay a transaction it would then refuse to connect.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    nonfinal = locked_spend(
        funding, funding.vout[0].value, lock_time=2_000_000_000, sequence=0
    )
    with pytest.raises(TxRejectedError, match=r"^non-final$"):
        verify_mempool_acceptance(node, nonfinal)


def test_a_nonfinal_spend_of_nothing_is_refused_as_nonfinal(node: Node) -> None:
    """Finality is checked ahead of the inputs, as in Core's `PreChecks`.

    A non-final candidate whose input is nowhere is "non-final", not a
    missing input (btclib-org/btclib-node#1328).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    nowhere = generate_random_transaction()
    nonfinal = locked_spend(nowhere, 1_000, lock_time=2_000_000_000, sequence=0)
    with pytest.raises(TxRejectedError, match=r"^non-final$"):
        verify_mempool_acceptance(node, nonfinal)


def test_a_nonfinal_unverifiable_mempool_spend_is_refused_as_nonfinal(
    node: Node,
) -> None:
    """A candidate both non-final and script-invalid is refused as non-final.

    Asserting the verdict alone -- `BTClibValueError` -- passes under
    either order, a failing script raising it too. Matching on the
    finality message pins the order instead: refused any other way, the
    script check ran ahead of the two cheap lock checks (ISS 829).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    nonfinal_and_unverifiable = locked_spend(
        funding,
        funding.vout[0].value,
        lock_time=2_000_000_000,
        sequence=0,
        script_sig=script.serialize([b"\x11" * 32]),
    )
    with pytest.raises(TxRejectedError, match=r"^non-final$"):
        verify_mempool_acceptance(node, nonfinal_and_unverifiable)


def test_a_mempool_spend_locked_to_an_already_reached_height_is_accepted(
    node: Node,
) -> None:
    """`verify_mempool_acceptance` accepts a transaction already final."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    final = locked_spend(funding, funding.vout[0].value - FEE, lock_time=1, sequence=0)
    fee = verify_mempool_acceptance(node, final).fee
    assert fee >= 0


def test_reject_a_mempool_spend_whose_relative_lock_is_not_satisfied(
    node: Node,
) -> None:
    """`verify_mempool_acceptance` refuses the same unmet BIP68 lock."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=COINBASE_MATURITY + 50
    )
    with pytest.raises(TxRejectedError, match=r"^non-BIP68-final$"):
        verify_mempool_acceptance(node, unmet)


def test_a_mempool_spend_whose_relative_lock_is_satisfied_is_accepted(
    node: Node,
) -> None:
    """`verify_mempool_acceptance` accepts a satisfied BIP68 relative lock."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    met = relative_locked_spend(funding, funding.vout[0].value - FEE, sequence=50)
    fee = verify_mempool_acceptance(node, met).fee
    assert fee >= 0


def test_reject_a_mempool_spend_whose_time_based_relative_lock_is_not_satisfied(
    node: Node,
) -> None:
    """`verify_mempool_acceptance` refuses the same unmet time-based lock.

    Exercises `verify_mempool_acceptance`'s own `ancestor_median_time_past`
    closure, which the height-based pair above never reaches.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    type_flag = 1 << 22
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=type_flag | 1000
    )
    with pytest.raises(TxRejectedError, match=r"^non-BIP68-final$"):
        verify_mempool_acceptance(node, unmet)


def test_a_mempool_spend_whose_time_based_relative_lock_is_satisfied(
    node: Node,
) -> None:
    """`verify_mempool_acceptance` accepts a satisfied time-based lock."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    type_flag = 1 << 22
    met = relative_locked_spend(
        funding, funding.vout[0].value - FEE, sequence=type_flag | 0
    )
    fee = verify_mempool_acceptance(node, met).fee
    assert fee >= 0


def test_a_mempool_chained_spend_s_zero_relative_lock_is_satisfied(
    node: Node,
) -> None:
    """A relative lock of zero against an unconfirmed parent is trivially met.

    `verify_mempool_acceptance`'s own `prevout_coins` stands an
    unconfirmed parent's own height in for Core's `MEMPOOL_HEIGHT`
    convention -- assumed to confirm in the very next block, i.e. at
    `spend_height` itself.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    parent = generate_random_transaction(funding.id, value=funding.vout[0].value - FEE)
    verify_mempool_acceptance(node, parent)
    node.mempool.add_tx(parent)

    child = relative_locked_spend(parent, parent.vout[0].value - FEE, sequence=0)
    fee = verify_mempool_acceptance(node, child).fee
    assert fee >= 0


def test_a_mempool_chained_spend_s_relative_lock_cannot_yet_be_met(
    node: Node,
) -> None:
    """A nonzero relative lock against an unconfirmed parent is never met.

    The parent is assumed to confirm alongside this transaction at the
    earliest, so any lock asking for a block *after* that can never be
    satisfied while the parent is still unconfirmed.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    funding = chain[0].transactions[0]
    parent = generate_random_transaction(funding.id, value=funding.vout[0].value - FEE)
    verify_mempool_acceptance(node, parent)
    node.mempool.add_tx(parent)

    child = relative_locked_spend(parent, parent.vout[0].value - FEE, sequence=1)
    with pytest.raises(TxRejectedError, match=r"^non-BIP68-final$"):
        verify_mempool_acceptance(node, child)


def test_add_tx(node: Node) -> None:
    """`verify_mempool_acceptance` accepts a prevout from chain or mempool."""
    # COINBASE_MATURITY long, and tx1 spends chain[0]'s own coinbase: a
    # fresher one would be refused for prematurity, which is a different
    # test (test_reject_a_mempool_spend_of_an_immature_coinbase, below).
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    headers = [block.header for block in chain]
    block_index = node.chainstate.block_index
    block_index.add_headers(headers)
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(node)

    invalid_tx = generate_random_transaction()
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, invalid_tx)

    tx1 = generate_random_transaction(
        chain[0].transactions[0].id, value=chain[0].transactions[0].vout[0].value - FEE
    )
    tx2 = generate_random_transaction(tx1.id, value=tx1.vout[0].value - FEE)

    verify_mempool_acceptance(node, tx1)

    # We can't find the prevouts
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, tx2)

    # tx1 needs to be added to the mempool
    node.mempool.add_tx(tx1)
    verify_mempool_acceptance(node, tx2)


@pytest.mark.parametrize("vout", [1, 5])
def test_a_spend_of_an_output_a_mempool_parent_lacks_is_a_missing_prevout(
    node: Node, vout: int
) -> None:
    """An index past a mempool parent's outputs is a missing input.

    It raised `IndexError`, which the `tx` callback drops the peer for
    and the RPC answers `-32603` with; `bitcoind` v31.1 answers
    `missing-inputs` (btclib-org/btclib-node#1252). `1` is the first
    index the one-output parent lacks.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    funding = chain[0].transactions[0]
    parent = generate_random_transaction(funding.id, value=funding.vout[0].value - FEE)
    node.mempool.add_tx(parent, *verify_mempool_acceptance(node, parent))
    assert len(parent.vout) == 1

    child = generate_random_transaction(parent.id, value=1)
    child.vin[0] = replace(child.vin[0], prev_out=OutPoint(parent.id, vout))
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, child)


def test_a_mempool_candidate_is_read_against_relay_policy(node: Node) -> None:
    """`verify_mempool_acceptance` refuses a spend only standardness refuses.

    The two spends below differ in one push: the redeem script, pushed
    with OP_PUSHDATA1 (`4c 02 ...`) in the first, where the two-octet
    push MINIMALDATA asks for is the second's, and that rule is not a
    consensus one -- a block carrying the first spend connects.
    `interpreter.STANDARD_FLAGS` is what refuses it here, and the second
    one being accepted is what says the refusal is that push and not
    something else about the pair. `NonStandardTxError` is the
    class the refusal reaches a caller as, which is what keeps the peer
    that relayed the transaction (`p2p/callbacks_test.py`).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    coinbase = chain[0].transactions[0]

    non_minimal = generate_random_transaction(
        coinbase.id, value=coinbase.vout[0].value - FEE
    )
    non_minimal.vin[0].script_sig = (
        script.serialize([b"\x11" * 32])
        + b"\x4c\x02"
        + anyone_can_spend_redeem_script()
    )
    with pytest.raises(
        NonStandardTxError, match=r"\(Data push larger than necessary\)"
    ):
        verify_mempool_acceptance(node, non_minimal)

    minimal = generate_random_transaction(
        coinbase.id, value=coinbase.vout[0].value - FEE
    )
    assert verify_mempool_acceptance(node, minimal).fee == FEE


def a_funded_spend(node: Node, fee: int) -> Tx:
    """Connect a mature chain and return a spend of its first coinbase."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    funding = chain[0].transactions[0]
    return generate_random_transaction(funding.id, value=funding.vout[0].value - fee)


def test_a_fee_under_the_relay_floor_is_refused_in_core_s_words(node: Node) -> None:
    """A fee under `min_relay_feerate` for the vsize is refused, as by Core.

    `bitcoind` v31.1 on regtest answers a zero-fee spend "min relay fee
    not met, 0 < 11", 11 being its own 110-vbyte size at 100 sat/kvB
    rounded up; this answers the same shape for its own size. One
    satoshi under the floor is refused and the floor itself accepted
    (btclib-org/btclib-node#1245).
    """
    probe = a_funded_spend(node, 0)
    floor = fee_from_vsize(probe.vsize, node.config.min_relay_feerate)
    assert floor > 0
    funding_value = probe.vout[0].value
    for fee in (0, floor - 1):
        short = generate_random_transaction(
            probe.vin[0].prev_out.tx_id, value=funding_value - fee
        )
        with pytest.raises(TxRejectedError) as refused:
            verify_mempool_acceptance(node, short)
        assert refused.value.reason == "min relay fee not met"
        assert str(refused.value) == f"min relay fee not met, {fee} < {floor}"
    at_floor = generate_random_transaction(
        probe.vin[0].prev_out.tx_id, value=funding_value - floor
    )
    assert verify_mempool_acceptance(node, at_floor).fee == floor


def test_a_fee_under_the_mempool_s_rolling_minimum_is_refused_first(
    node: Node,
) -> None:
    """The rolling minimum is checked ahead of the relay floor, as by Core.

    `CheckFeeRate` asks the mempool's own minimum first, so a fee under
    both floors is refused for that one; a fee under it alone, and above
    the relay floor, is refused too, and the minimum itself accepted.
    """
    probe = a_funded_spend(node, 0)
    node.mempool._rolling_min_fee_rate = 5000.0
    node.mempool._block_since_last_rolling_fee_bump = False
    assert node.mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=5000)
    floor = fee_from_vsize(probe.vsize, FeeRate(sats_per_kvbyte=5000))
    relay_floor = fee_from_vsize(probe.vsize, node.config.min_relay_feerate)
    assert relay_floor < floor - 1
    funding_value = probe.vout[0].value
    for fee in (0, floor - 1):
        short = generate_random_transaction(
            probe.vin[0].prev_out.tx_id, value=funding_value - fee
        )
        with pytest.raises(TxRejectedError) as refused:
            verify_mempool_acceptance(node, short)
        assert str(refused.value) == f"mempool min fee not met, {fee} < {floor}"
    at_floor = generate_random_transaction(
        probe.vin[0].prev_out.tx_id, value=funding_value - floor
    )
    assert verify_mempool_acceptance(node, at_floor).fee == floor


def a_sigop_dense_spend(node: Node, *repeats: int) -> Callable[[int], Tx]:
    """Return a builder, by fee, of a spend of one P2WSH output per count.

    Each witness script repeats `OP_0 OP_0 OP_0 OP_CHECKMULTISIGVERIFY`,
    which checks no key and so passes, and which Core's accurate count
    prices at `MAX_PUBKEYS_PER_MULTISIG`, the op before it not being `OP_1`
    to `OP_16`: 20 sigops a repeat, up to the 201 op codes a script may
    execute. The parent paying the P2WSH outputs is held in the mempool.
    """
    ops = ["OP_0", "OP_0", "OP_0", "OP_CHECKMULTISIGVERIFY"]
    witness_scripts = [script.serialize([*ops * n, "OP_1"]) for n in repeats]
    parent = a_funded_spend(node, 1_000)
    value = parent.vout[0].value // len(repeats)
    parent.vout = [
        TxOut(value, ScriptPubKey.p2wsh(witness_script, network="regtest"))
        for witness_script in witness_scripts
    ]
    node.mempool.add_tx(parent, *verify_mempool_acceptance(node, parent))
    vin = [
        TxIn(OutPoint(parent.id, vout), b"", 0xFFFFFFFF, Witness([witness_script]))
        for vout, witness_script in enumerate(witness_scripts)
    ]
    total = value * len(repeats)
    return lambda fee: Tx(2, 0, vin, [TxOut(total - fee, anyone_can_spend())])


def test_a_sigop_dense_spend_is_priced_by_its_sigop_cost(node: Node) -> None:
    """The vsize is Core's `GetVirtualTransactionSize(weight, cost, 20)`.

    `bitcoind` v31.1 on regtest, a P2WSH spend of 100 repeats, weight
    735: `testmempoolaccept` answers vsize 10000 (2000 sigops at 20 bytes,
    over four), accepts a fee of 1000 and refuses 999 "min relay fee not
    met, 999 < 1000", where the weight alone would price the floor at 19
    (btclib-org/btclib-node#1357).
    """
    spend = a_sigop_dense_spend(node, 100)
    with pytest.raises(TxRejectedError) as raised:
        verify_mempool_acceptance(node, spend(999))
    assert str(raised.value) == "min relay fee not met, 999 < 1000"
    assert verify_mempool_acceptance(node, spend(1_000)) == (1_000, 10_000)


def test_a_sigop_dense_conflict_pays_relay_for_its_sigop_cost(node: Node) -> None:
    """Rule 4 prices the candidate at its sigop-adjusted vsize.

    `bitcoind` v31.1 on regtest, the 100-repeat spend held at 1000: a
    conflict paying 1500 is "insufficient fee, rejecting replacement
    <txid>, not enough additional fees to relay; 0.000005 < 0.00001",
    where the weight alone would ask 19 satoshi (btclib-org/btclib-node#1357).
    """
    spend = a_sigop_dense_spend(node, 100)
    held = spend(1_000)
    node.mempool.add_tx(held, *verify_mempool_acceptance(node, held))
    conflict = spend(1_500)
    with pytest.raises(TxRejectedError) as raised:
        verify_mempool_acceptance(node, conflict)
    assert str(raised.value) == (
        f"insufficient fee, rejecting replacement {conflict.id.hex()}, not "
        "enough additional fees to relay; 0.000005 < 0.00001"
    )


def test_a_spend_over_the_standard_sigop_cost_is_refused(node: Node) -> None:
    """Core's "bad-txns-too-many-sigops", reorg re-add or not.

    `bitcoind` v31.1 on regtest answers a P2WSH spend of 801 repeats,
    16020 sigops, "bad-txns-too-many-sigops, 16020": the ceiling,
    `MAX_STANDARD_TX_SIGOPS_COST`, is 16000 and is checked whatever
    `bypass_limits` says. Four inputs of 200 repeats cost 16000 exactly
    (btclib-org/btclib-node#1357).
    """
    over = a_sigop_dense_spend(node, 200, 200, 200, 200, 1)
    for bypass_limits in (False, True):
        with pytest.raises(TxRejectedError) as raised:
            verify_mempool_acceptance(node, over(400_000), bypass_limits=bypass_limits)
        assert str(raised.value) == "bad-txns-too-many-sigops, 16020"


def test_a_spend_at_the_standard_sigop_cost_is_accepted(node: Node) -> None:
    """16000 sigops is the ceiling itself, and Core's `>` lets it through."""
    at_ceiling = a_sigop_dense_spend(node, 200, 200, 200, 200)
    assert verify_mempool_acceptance(node, at_ceiling(400_000)).vsize == 80_000


def test_bypass_limits_skips_the_feerate_floor(node: Node) -> None:
    """`bypass_limits` accepts a fee-free candidate, Core's own reorg re-add."""
    spend = a_funded_spend(node, 0)
    assert verify_mempool_acceptance(node, spend, bypass_limits=True).fee == 0


def test_a_spend_of_more_than_its_inputs_is_refused_for_that_not_its_fee(
    node: Node,
) -> None:
    """Outputs over inputs is refused as such, ahead of the feerate floor.

    Core's `CheckTxInputs` runs before `CheckFeeRate`; the negative fee
    would otherwise read as one under the floor. `bitcoind` v31.1 on
    regtest answers a 50-BTC input paying out 51 "bad-txns-in-belowout,
    value in (50.00) < value out (51.00)" (btclib-org/btclib-node#1328).
    """
    spend = a_funded_spend(node, -1)
    value_out = sum(tx_out.value for tx_out in spend.vout)
    with pytest.raises(TxRejectedError) as refused:
        verify_mempool_acceptance(node, spend)
    assert str(refused.value) == (
        f"bad-txns-in-belowout, value in ({format_money(value_out - 1)}) "
        f"< value out ({format_money(value_out)})"
    )


def a_candidate_heavier_than_standard(node: Node) -> Tx:
    """Return a funded spend over `MAX_STANDARD_TX_WEIGHT`.

    Its `script_sig` is push-only and fails, so a script check that ran
    would refuse it for another reason. The outputs that weigh it down
    are standard and worth nothing.
    """
    spend = a_funded_spend(node, 1_000_000)
    spend.vin[0].script_sig = script.serialize([b"\x11" * 32])
    spend.vout += [TxOut(0, anyone_can_spend())] * 3_130
    assert spend.weight > 400_000
    return spend


def record_calls(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, object]
) -> list[str]:
    """Replace each named `main` function by one that records its call.

    The replacement answers `answers[name]`. The list it appends to is
    returned.
    """
    calls: list[str] = []

    def recorder(name: str) -> Callable[..., object]:
        def record(*args: object, **kwargs: object) -> object:
            calls.append(name)
            return answers[name]

        return record

    for name in answers:
        monkeypatch.setattr(main, name, recorder(name))
    return calls


def test_standardness_is_judged_before_any_script_runs(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transaction over `MAX_STANDARD_TX_WEIGHT` is refused "tx-size" first.

    `PreChecks` (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag) asks `IsStandardTx` before it reads the inputs and runs
    the scripts last, in `PolicyScriptChecks`. Each function here that
    evaluates a script records its call. With `require_standard` off the
    same candidate reaches them, which says the recorders are in its
    path (btclib-org/btclib-node#1382).
    """
    spend = a_candidate_heavier_than_standard(node)
    calls = record_calls(
        monkeypatch,
        {
            "check_transaction": None,
            "sig_op_cost": 0,
            "are_inputs_standard": True,
            "is_witness_standard": True,
        },
    )

    for bypass_limits in (False, True):
        with pytest.raises(TxRejectedError) as refused:
            verify_mempool_acceptance(node, spend, bypass_limits=bypass_limits)
        assert str(refused.value) == "tx-size"
        assert calls == []

    node.config.require_standard = False
    verify_mempool_acceptance(node, spend)
    assert calls == ["sig_op_cost", "check_transaction"]


def test_the_inputs_are_judged_standard_before_any_script_runs(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AreInputsStandard` refuses ahead of the sigop count and the scripts."""
    parent = a_funded_spend(node, FEE)
    parent.vout[0] = TxOut(parent.vout[0].value, script.serialize(["OP_NOP", "OP_1"]))
    node.mempool.add_tx(parent, FEE, parent.vsize)
    child = generate_random_transaction(parent.id, value=parent.vout[0].value - FEE)
    child.vin[0].script_sig = b""
    calls = record_calls(monkeypatch, {"check_transaction": None, "sig_op_cost": 0})

    with pytest.raises(TxRejectedError) as refused:
        verify_mempool_acceptance(node, child)
    assert str(refused.value) == "bad-txns-nonstandard-inputs"
    assert calls == []


def a_multisig_script(keys: int) -> bytes:
    """Return a bare `keys`-of-`keys` multisig script_pub_key."""
    pubkeys = [b"\x02" + bytes([k]) * 32 for k in range(1, keys + 1)]
    return script.serialize([f"OP_{keys}", *pubkeys, f"OP_{keys}", "OP_CHECKMULTISIG"])


def a_data_output(size: int) -> TxOut:
    """Return an output of a nulldata script_pub_key of `size` bytes."""
    data = bytes(size - 3)
    return TxOut(0, b"\x6a\x4c" + bytes([len(data)]) + data)


def with_script_sig(spend: Tx, script_sig: bytes) -> Tx:
    """Return `spend` with `script_sig` for its only input's."""
    return replace(spend, vin=[replace(spend.vin[0], script_sig=script_sig)])


def with_outputs(spend: Tx, *extra: TxOut) -> Tx:
    """Return `spend` paying `extra` as well."""
    return replace(spend, vout=[*spend.vout, *extra])


@pytest.mark.parametrize(
    ("reason", "change", "permit_bare_multisig", "scripts_pass"),
    [
        ("version", lambda tx: replace(tx, version=4), True, True),
        (
            "scriptsig-size",
            lambda tx: with_script_sig(tx, script.serialize([b"\x11" * 500] * 4)),
            True,
            False,
        ),
        (
            "scriptsig-not-pushonly",
            lambda tx: with_script_sig(tx, script.serialize(["OP_NOP"])),
            True,
            False,
        ),
        (
            "scriptpubkey",
            lambda tx: with_outputs(tx, TxOut(0, script.serialize(["OP_NOP"]))),
            True,
            True,
        ),
        (
            "scriptpubkey",
            lambda tx: with_outputs(tx, TxOut(0, a_multisig_script(4))),
            True,
            True,
        ),
        ("datacarrier", lambda tx: with_outputs(tx, a_data_output(84)), True, True),
        (
            "dust",
            lambda tx: with_outputs(tx, *[TxOut(0, anyone_can_spend())] * 2),
            True,
            True,
        ),
        (
            "bare-multisig",
            lambda tx: with_outputs(tx, TxOut(0, a_multisig_script(1))),
            False,
            True,
        ),
    ],
)
def test_a_nonstandard_candidate_is_refused_in_core_s_words(
    node: Node,
    reason: str,
    change: Callable[[Tx], Tx],
    *,
    permit_bare_multisig: bool,
    scripts_pass: bool,
) -> None:
    """Each `IsStandardTx` rule refuses with its reason, reorg re-add or not.

    With `require_standard` off the same candidate is accepted, which
    says the rule is what refused it. The two that change the
    `script_sig` fail their script instead (btclib-org/btclib-node#1382).
    """
    node.config.max_datacarrier_bytes = 83
    node.config.permit_bare_multisig = permit_bare_multisig
    spend = change(a_funded_spend(node, FEE))
    for bypass_limits in (False, True):
        with pytest.raises(TxRejectedError) as refused:
            verify_mempool_acceptance(node, spend, bypass_limits=bypass_limits)
        assert str(refused.value) == reason

    node.config.require_standard = False
    if scripts_pass:
        assert verify_mempool_acceptance(node, spend).fee == FEE
    else:
        with pytest.raises(TxRejectedError, match=r"^mempool-script-verify-flag"):
            verify_mempool_acceptance(node, spend)


def test_the_standardness_options_are_read(node: Node) -> None:
    """The data carrier size and the dust rate bound what is relayed.

    The size is shared by the data outputs taken together, as Core
    shares it; `-nodatacarrier` allows none; a dust rate of zero makes
    no output dust (btclib-org/btclib-node#1382).
    """
    funded = a_funded_spend(node, FEE)
    spend = with_outputs(funded, a_data_output(50), a_data_output(50))
    node.config.max_datacarrier_bytes = 100
    assert verify_mempool_acceptance(node, spend).fee == FEE
    node.config.max_datacarrier_bytes = 99
    with pytest.raises(TxRejectedError, match=r"^datacarrier$"):
        verify_mempool_acceptance(node, spend)
    node.config.max_datacarrier_bytes = None
    with pytest.raises(TxRejectedError, match=r"^datacarrier$"):
        verify_mempool_acceptance(node, spend)

    dust = with_outputs(funded, *[TxOut(0, anyone_can_spend())] * 2)
    with pytest.raises(TxRejectedError, match=r"^dust$"):
        verify_mempool_acceptance(node, dust)
    node.config.dust_relay_feerate = FeeRate(sats_per_kvbyte=0)
    assert verify_mempool_acceptance(node, dust).fee == FEE


def a_dusty_spend(node: Node, fee: int) -> Tx:
    """Return a standard spend paying `fee` with one dust output."""
    return with_outputs(a_funded_spend(node, fee), TxOut(0, anyone_can_spend()))


def test_a_dust_output_that_pays_a_fee_is_refused_in_core_s_words(node: Node) -> None:
    """`PreCheckEphemeralTx` refuses "dust", reorg re-add or not.

    A dust output is held only by a transaction paying nothing. One dust
    output passes `IsStandardTx`, which allows one, so it is this rule
    that refuses. Each way out is taken in turn: no fee, `-acceptnonstdtxn`
    and a dust rate under which the output is no longer dust
    (btclib-org/btclib-node#1594).
    """
    spend = a_dusty_spend(node, FEE)
    for bypass_limits in (False, True):
        refused_with(
            node,
            spend,
            "dust",
            "tx with dust output must be 0-fee",
            bypass_limits=bypass_limits,
        )
    # ahead of the fee floor, which 1 satoshi is under
    cheap = replace(
        spend,
        vout=[
            replace(spend.vout[0], value=spend.vout[0].value + FEE - 1),
            spend.vout[1],
        ],
    )
    refused_with(node, cheap, "dust", "tx with dust output must be 0-fee")
    free = replace(
        spend,
        vout=[replace(spend.vout[0], value=spend.vout[0].value + FEE), spend.vout[1]],
    )
    assert verify_mempool_acceptance(node, free, bypass_limits=True).fee == 0

    node.config.require_standard = False
    assert verify_mempool_acceptance(node, spend).fee == FEE
    node.config.require_standard = True
    node.config.dust_relay_feerate = FeeRate(sats_per_kvbyte=0)
    assert verify_mempool_acceptance(node, spend).fee == FEE


def test_an_output_at_the_dust_threshold_is_no_dust(node: Node) -> None:
    """An output worth the threshold is not dust, so a fee is allowed.

    `IsDust` is "under the threshold", and the same spend with 1 satoshi
    less is refused (btclib-org/btclib-node#1594).
    """
    spend = a_dusty_spend(node, FEE)
    threshold = dust_threshold(
        spend.vout[1].script_pub_key.script, node.config.dust_relay_feerate
    )
    funded = replace(spend, vout=[spend.vout[0], TxOut(threshold, anyone_can_spend())])
    assert verify_mempool_acceptance(node, funded).fee == FEE - threshold
    under = replace(
        funded,
        vout=[funded.vout[0], TxOut(threshold - 1, spend.vout[1].script_pub_key)],
    )
    refused_with(node, under, "dust", "tx with dust output must be 0-fee")


def test_a_coinbase_is_refused_before_anything_else(node: Node) -> None:
    """`PreChecks` refuses a loose coinbase "coinbase", ahead of the rest."""
    with pytest.raises(TxRejectedError) as refused:
        verify_mempool_acceptance(node, generate_coinbase())
    assert str(refused.value) == "coinbase"


def a_witness_spend(node: Node, item: int) -> Tx:
    """Return a spend of a held P2WSH output, its witness item `item` bytes."""
    witness_script = script.serialize(["OP_DROP", "OP_1"])
    parent = a_funded_spend(node, FEE)
    parent.vout[0] = TxOut(
        parent.vout[0].value, ScriptPubKey.p2wsh(witness_script, network="regtest")
    )
    node.mempool.add_tx(parent, FEE, parent.vsize)
    value = parent.vout[0].value - FEE
    witness = Witness([b"\x11" * item, witness_script])
    return Tx(
        2,
        0,
        [TxIn(OutPoint(parent.id, 0), b"", 0xFFFFFFFF, witness)],
        [TxOut(value, anyone_can_spend())],
    )


def test_a_witness_over_the_standard_limits_is_refused(node: Node) -> None:
    """`IsWitnessStandard` refuses a P2WSH item of 81 bytes and passes 80."""
    with pytest.raises(TxRejectedError) as refused:
        verify_mempool_acceptance(node, a_witness_spend(node, 81))
    assert str(refused.value) == "bad-witness-nonstandard"

    assert verify_mempool_acceptance(node, a_witness_spend(node, 80)).fee == FEE


def funded_spends(node: Node, count: int) -> list[Tx]:
    """Connect a mature chain and return a spend of each of `count` coinbases.

    The chain's last block spends the first coinbase, so the spends are of
    the second on.
    """
    chain = generate_random_chain(COINBASE_MATURITY + count, RegTest().genesis.hash)
    connect(node, chain)
    return [
        generate_random_transaction(
            block.transactions[0].id, value=block.transactions[0].vout[0].value - FEE
        )
        for block in chain[1 : count + 1]
    ]


def hold(node: Node, tx: Tx) -> Tx:
    """Accept `tx` as the mempool does, keep it, and return it."""
    accepted = verify_mempool_acceptance(node, tx)
    assert node.mempool.add_tx(tx, accepted.fee, accepted.vsize)
    return tx


def child_of(parent: Tx, vout: int = 0, *, version: int = 1) -> Tx:
    """Return a spend of output `vout` of `parent`, paying `FEE`."""
    child = generate_random_transaction(parent.id, value=parent.vout[vout].value - FEE)
    child.vin[0] = replace(child.vin[0], prev_out=OutPoint(parent.id, vout))
    return replace(child, version=version)


def with_two_outputs(spend: Tx) -> Tx:
    """Return `spend` paying its value as two equal outputs."""
    half = spend.vout[0].value // 2
    return replace(spend, vout=[TxOut(half, anyone_can_spend())] * 2)


def padded(spend: Tx, vsize: int) -> Tx:
    """Return `spend` grown to exactly `vsize` vbytes, its fee unchanged.

    Outputs of 1,000 satoshi, above dust, make the size and a push the
    `script_sig` drops makes up the last bytes.
    """
    count = (vsize - spend.vsize) // 32 - 4
    outputs = [
        TxOut(spend.vout[0].value - 1_000 * count, anyone_can_spend()),
        *[TxOut(1_000, anyone_can_spend())] * count,
    ]
    candidates = (
        replace(
            spend,
            vin=[
                replace(
                    spend.vin[0],
                    script_sig=script.serialize(
                        [b"\x11" * pad, anyone_can_spend_redeem_script()]
                    ),
                )
            ],
            vout=outputs,
        )
        for pad in range(80, 400)
    )
    return next(candidate for candidate in candidates if candidate.vsize == vsize)


def a_tx_of_nonwitness_size(parent: Tx, size: int) -> Tx:
    """Return a spend of `parent`'s first output of `size` non-witness bytes.

    Its empty `script_sig` spends an `OP_1` output, and its one output is
    a standard `OP_RETURN` carrying `size - 61` bytes.
    """
    data = bytes(size - 62)
    return Tx(
        2,
        2_000_000_000,
        [TxIn(OutPoint(parent.id, 0), b"", 0)],
        [TxOut(0, b"\x6a" + bytes([len(data)]) + data)],
    )


def an_op_1_parent(node: Node) -> Tx:
    """Hold a spend whose only output `OP_1` an empty `script_sig` spends."""
    parent = funded_spends(node, 1)[0]
    parent.vout[0] = TxOut(parent.vout[0].value, script.serialize(["OP_1"]))
    assert node.mempool.add_tx(parent, FEE, parent.vsize)
    return parent


def ids(tx: Tx) -> str:
    """Return `tx` as Core's TRUC reasons name it."""
    return f"tx {tx.id.hex()} (wtxid={tx.hash.hex()})"


def refused_with(
    node: Node, tx: Tx, reason: str, details: str = "", **kwargs: bool
) -> None:
    """Assert `tx` is refused with `reason` and `details`."""
    with pytest.raises(TxRejectedError) as refused:
        verify_mempool_acceptance(node, tx, **kwargs)
    assert refused.value.reason == reason
    assert refused.value.details == details


def test_a_transaction_under_65_nonwitness_bytes_is_refused_tx_size_small(
    node: Node,
) -> None:
    """`PreChecks` refuses 64 bytes and takes 65, `-acceptnonstdtxn` or not.

    The 64-byte one is also non-final, so the refusal comes ahead of the
    finality check. The reason is `bitcoind` v31.1's
    (btclib-org/btclib-node#1687).
    """
    parent = an_op_1_parent(node)
    small = a_tx_of_nonwitness_size(parent, 64)
    assert (small.weight - small.size) // 3 == 64
    for require_standard in (True, False):
        node.config.require_standard = require_standard
        for bypass_limits in (False, True):
            refused_with(node, small, "tx-size-small", bypass_limits=bypass_limits)

    big_enough = replace(a_tx_of_nonwitness_size(parent, 65), lock_time=0)
    assert (big_enough.weight - big_enough.size) // 3 == 65
    node.config.require_standard = False
    assert verify_mempool_acceptance(node, big_enough).fee == parent.vout[0].value


def test_a_version_3_transaction_over_10000_vbytes_is_refused(node: Node) -> None:
    """`SingleTRUCChecks` refuses 10,001 vbytes and takes 10,000.

    The same size at version 2 is accepted, which says the version is
    what refuses it; a disconnected block's transaction is not held to
    it (`bypass_limits`). The words are `bitcoind` v31.1's
    (btclib-org/btclib-node#1399).
    """
    spend = funded_spends(node, 1)[0]
    cheap = replace(padded(spend, 10_001), version=3)
    base = replace(
        spend,
        vout=[TxOut(spend.vout[0].value - 100_000, spend.vout[0].script_pub_key)],
    )
    at_limit = replace(padded(base, 10_000), version=3)
    over = replace(padded(base, 10_001), version=3)

    assert verify_mempool_acceptance(node, at_limit).vsize == 10_000
    assert verify_mempool_acceptance(node, replace(over, version=2)).vsize == 10_001
    assert verify_mempool_acceptance(node, over, bypass_limits=True).vsize == 10_001
    refused_with(
        node,
        over,
        "TRUC-violation",
        f"version=3 {ids(over)} is too big: 10001 > 10000 virtual bytes",
    )
    # the fee floor is asked first, as `PreChecks` asks it
    refused_with(node, cheap, "min relay fee not met", "1000 < 1001")


@pytest.mark.parametrize(
    ("parent_version", "child_version", "refusal"),
    [
        (3, 2, "non-version=3 {child} cannot spend from version=3 {parent}"),
        (2, 3, "version=3 {child} cannot spend from non-version=3 {parent}"),
        (3, 3, None),
        (2, 2, None),
    ],
)
def test_truc_and_other_transactions_do_not_spend_from_each_other(
    node: Node, parent_version: int, child_version: int, refusal: str | None
) -> None:
    """A held parent and its child are both version 3 or neither is."""
    parent = replace(funded_spends(node, 1)[0], version=parent_version)
    hold(node, parent)
    child = child_of(parent, version=child_version)
    if refusal is None:
        assert verify_mempool_acceptance(node, child).fee == FEE
        return
    details = refusal.format(child=ids(child), parent=ids(parent))
    refused_with(node, child, "TRUC-violation", details)


def test_a_version_3_child_over_1000_vbytes_is_refused(node: Node) -> None:
    """A version-3 transaction with a held parent is at most 1,000 vbytes."""
    parent = hold(node, replace(funded_spends(node, 1)[0], version=3))
    child = child_of(parent, version=3)
    child.vout[0] = TxOut(child.vout[0].value - 100_000, child.vout[0].script_pub_key)
    at_limit, over = padded(child, 1_000), padded(child, 1_001)

    assert verify_mempool_acceptance(node, at_limit).vsize == 1_000
    refused_with(
        node,
        over,
        "TRUC-violation",
        f"version=3 child {ids(over)} is too big: 1001 > 1000 virtual bytes",
    )


def test_a_version_3_transaction_has_one_parent_and_no_grandparent(
    node: Node,
) -> None:
    """A second held parent, or a held grandparent, is refused."""
    first, second = (replace(spend, version=3) for spend in funded_spends(node, 2))
    hold(node, first)
    hold(node, second)
    two_parents = child_of(first, version=3)
    two_parents.vin.append(replace(child_of(second, version=3).vin[0]))
    refused_with(
        node,
        two_parents,
        "TRUC-violation",
        f"{ids(two_parents)} would have too many ancestors",
    )

    child = hold(node, child_of(first, version=3))
    grandchild = child_of(child, version=3)
    refused_with(
        node,
        grandchild,
        "TRUC-violation",
        f"{ids(grandchild)} would have too many ancestors",
    )


def test_a_version_3_parent_has_one_child(node: Node) -> None:
    """A second child is refused, unless it conflicts with the first.

    A conflicting one is not counted twice, and reaches
    `check_replacement`, which refuses it in the words of its own. A
    disconnected block's transaction is not held to the rule.
    """
    parent = hold(node, with_two_outputs(replace(funded_spends(node, 1)[0], version=3)))
    hold(node, child_of(parent, 0, version=3))
    second = child_of(parent, 1, version=3)
    refused_with(
        node,
        second,
        "TRUC-violation",
        f"{ids(parent)} would exceed descendant count limit",
    )
    assert verify_mempool_acceptance(node, second, bypass_limits=True).fee == FEE

    conflicting = child_of(parent, 0, version=3)
    conflicting.vout[0] = TxOut(conflicting.vout[0].value - 2 * FEE, anyone_can_spend())
    refused_with(node, conflicting, "bip125-replacement-disallowed")


def a_cluster(node: Node, root: Tx, count: int) -> list[Tx]:
    """Hold a chain of `count` transactions spending `root`, and return it."""
    chain = [root]
    for _ in range(count - 1):
        chain.append(child_of(chain[-1]))
    for tx in chain:
        assert node.mempool.add_tx(tx, FEE, tx.vsize)
    return chain


def refused_reason(node: Node, tx: Tx) -> str:
    """Return the reason `verify_mempool_acceptance` refuses `tx` with."""
    with pytest.raises(TxRejectedError) as refused:
        verify_mempool_acceptance(node, tx)
    return refused.value.reason


def test_a_cluster_of_more_than_64_transactions_is_refused(node: Node) -> None:
    """A candidate that makes a 65th transaction in its cluster is refused.

    A disconnected block's transaction is held to it too
    (btclib-org/btclib-node#1383).
    """
    root, unrelated = funded_spends(node, 2)
    chain = a_cluster(node, root, 63)
    assert verify_mempool_acceptance(node, child_of(chain[-1])).fee == FEE
    last = hold(node, child_of(chain[-1]))
    for bypass_limits in (False, True):
        refused_with(
            node,
            child_of(last),
            "too-large-cluster",
            bypass_limits=bypass_limits,
        )
    # a conflict is judged first, as `ReplacementChecks` precedes the limits
    conflicting = child_of(chain[0])
    assert refused_reason(node, conflicting) == "insufficient fee"
    assert verify_mempool_acceptance(node, unrelated).fee == FEE


def test_a_cluster_is_walked_up_and_down_and_across(node: Node) -> None:
    """The cluster counts a candidate's parent's other children and parents.

    One parent with 63 children is a full cluster, so a child of the
    parent and a child of one of its children are both the 65th.
    With 62 children both make the 64th.
    """
    root = funded_spends(node, 1)[0]
    root.vout = [TxOut(root.vout[0].value // 64, anyone_can_spend())] * 64
    assert node.mempool.add_tx(root, FEE, root.vsize)
    children = [child_of(root, vout) for vout in range(62)]
    for child in children:
        assert node.mempool.add_tx(child, FEE, child.vsize)
    for candidate in (child_of(root, 62), child_of(children[0])):
        assert verify_mempool_acceptance(node, candidate).fee == FEE
    hold(node, child_of(root, 62))
    for candidate in (child_of(root, 63), child_of(children[0])):
        refused_with(node, candidate, "too-large-cluster")


@pytest.mark.parametrize(("right_count", "accepted"), [(31, True), (32, False)])
def test_the_clusters_of_two_parents_are_added(
    node: Node, right_count: int, *, accepted: bool
) -> None:
    """A candidate joining two clusters is counted over both."""
    first, second = funded_spends(node, 2)
    left = a_cluster(node, first, 32)
    right = a_cluster(node, second, right_count)
    both = child_of(left[-1])
    both.vin.append(child_of(right[-1]).vin[0])
    if accepted:
        assert verify_mempool_acceptance(node, both).vsize == both.vsize
    else:
        refused_with(node, both, "too-large-cluster")


@pytest.mark.parametrize(("extra", "accepted"), [(0, True), (1, False)])
def test_a_cluster_is_limited_in_vbytes_too(
    node: Node, extra: int, *, accepted: bool
) -> None:
    """A cluster of 101,000 vbytes is full, and one more vbyte is refused."""
    parent = funded_spends(node, 1)[0]
    candidate = child_of(parent)
    assert node.mempool.add_tx(parent, FEE, 101_000 - candidate.vsize + extra)
    if accepted:
        assert verify_mempool_acceptance(node, candidate).fee == FEE
    else:
        refused_with(node, candidate, "too-large-cluster")


def test_a_candidate_over_101000_vbytes_alone_is_refused(node: Node) -> None:
    """A lone transaction past the size limit is a cluster past it."""
    base = funded_spends(node, 1)[0]
    base.vout[0] = TxOut(base.vout[0].value - 100_000, base.vout[0].script_pub_key)
    node.config.require_standard = False
    assert verify_mempool_acceptance(node, padded(base, 101_000)).vsize == 101_000
    refused_with(node, padded(base, 101_001), "too-large-cluster")


def test_truc_cluster_and_size_refusals_precede_every_script(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None of the three refusals evaluates a script, which a clean one does.

    `PolicyScriptChecks` runs last in `AcceptSingleTransactionInternal`
    (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    parent = an_op_1_parent(node)
    small = a_tx_of_nonwitness_size(parent, 64)
    first, second = funded_spends(node, 2)
    base = replace(second, version=3)
    base.vout[0] = TxOut(base.vout[0].value - 100_000, base.vout[0].script_pub_key)
    huge = padded(base, 10_001)
    chain = a_cluster(node, first, 64)
    crowded = child_of(chain[-1])
    calls = record_calls(monkeypatch, {"check_transaction": None})

    refused_with(node, small, "tx-size-small")
    assert verify_mempool_acceptance(node, base).fee == FEE + 100_000
    assert calls == ["check_transaction"]
    calls.clear()
    refused_with(
        node,
        huge,
        "TRUC-violation",
        f"version=3 {ids(huge)} is too big: 10001 > 10000 virtual bytes",
    )
    refused_with(node, crowded, "too-large-cluster")
    assert calls == []


def test_a_second_spend_of_a_held_outpoint_is_refused_as_core_refuses_it(
    node: Node,
) -> None:
    """The issue's double spend: each candidate answered in Core's words.

    `bitcoind` v31.1 on regtest, a spend paying 10000 held: a fee-free
    conflict "min relay fee not met", a 5000-sat one "insufficient fee
    ... less fees than conflicting txs; 0.00005 < 0.0001". One paying for
    the held spend and its own relay, which Core accepts as a
    replacement, is refused here (btclib-org/btclib-node#1244).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    funding = chain[0].transactions[0]
    value = funding.vout[0].value
    held = generate_random_transaction(funding.id, value=value - 10_000)
    node.mempool.add_tx(held, *verify_mempool_acceptance(node, held))

    refusals = {
        0: "min relay fee not met",
        5_000: "insufficient fee",
        20_000: "bip125-replacement-disallowed",
    }
    for fee, reason in refusals.items():
        conflict = generate_random_transaction(funding.id, value=value - fee)
        with pytest.raises(TxRejectedError) as refused:
            verify_mempool_acceptance(node, conflict)
        assert refused.value.reason == reason
        if fee == 5_000:
            assert str(refused.value).endswith("; 0.00005 < 0.0001")
    assert node.mempool.size == 1
    assert node.mempool.contains_tx(held)


def test_a_confirmed_double_spend_evicts_the_held_spend_and_its_child(
    node: Node,
) -> None:
    """A block spending a held spend's coin evicts it and what spends it.

    `bitcoind` v31.1 on regtest: `generateblock` with a conflicting spend
    leaves the held one out of `getrawmempool`. Core's `removeForBlock`
    calls `removeConflicts` for every transaction of the block.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    funding = chain[0].transactions[0]
    value = funding.vout[0].value
    held = generate_random_transaction(funding.id, value=value - FEE)
    node.mempool.add_tx(held, *verify_mempool_acceptance(node, held))
    child = generate_random_transaction(held.id, value=held.vout[0].value - FEE)
    node.mempool.add_tx(child, *verify_mempool_acceptance(node, child))
    assert node.mempool.size == 2

    double_spend = generate_random_transaction(funding.id, value=value - 2 * FEE)
    block = build_block(
        chain[-1].header.hash,
        [generate_coinbase(height=len(chain) + 1), double_spend],
        len(chain),
    )
    connect(node, [block])
    assert node.chainstate.block_index.active_chain[-1] == block.header.hash
    assert node.mempool.size == 0
    assert node.mempool.outpoint_spender == {}


def test_a_stored_coin_that_wont_parse_looks_missing_to_the_mempool(
    node: Node,
) -> None:
    """A broken `utxo-` record answers absent, the same as no record at all.

    The record corrupted here is one only this node's own earlier
    `connect` (via `UtxoIndex.finalize`) ever wrote -- `tx` below never
    supplies these bytes, so what this proves is over storage this
    node owns, not over `tx`'s own content. `UtxoIndex.get_coin` now
    matches `CDBWrapper::Read`/`CCoinsViewDB::GetCoin`'s own "absent"
    for a checksum-clean record `Coin.parse` still cannot read, since
    RocksDB's own checksum (btclib-org/btclib-node#641) is what now
    catches an actually corrupted record before this call is ever
    reached: `get_coin` answers `None`, the mempool has never heard of
    `coinbase.id` either, so `verify_mempool_acceptance` raises the
    same `MissingPrevoutError` it would over a prevout this node never
    had at all (btclib-org/btclib-node#631, btclib-org/btclib-node#650).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)

    coinbase = chain[0].transactions[0]
    key = b"utxo-" + OutPoint(coinbase.id, 0).serialize(check_validity=False)
    original = node.chainstate.utxo_index.db.get(key)
    assert original is not None
    node.chainstate.db.put(key, original[:1])

    tx = generate_random_transaction(coinbase.id)
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, tx)


def test_a_candidate_whose_block_has_not_arrived_is_not_connected(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`update_chain` declines a candidate without asking `block_db` a thing."""
    # headers run ahead of blocks for the whole of a sync, so the
    # commonest state of a candidate is one whose block is still being
    # fetched. It is declined before block_db is asked for anything:
    # asking and rolling back reaches the same chain, but by way of an
    # exception, on every pass of a loop that runs until the block
    # arrives.
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    asked: list[bytes] = []
    monkeypatch.setattr(node.block_db, "get_block", asked.append)
    update_chain(node)
    assert not asked
    assert block_index.active_chain == [RegTest().genesis.hash]
    assert node.status == NodeStatus.HeaderSynced


def test_a_node_still_syncing_headers_connects_what_it_holds(node: Node) -> None:
    """Blocks connect before header sync ends, and the status stays put.

    A node with no peer never finishes header sync, and `submitblock`
    is how it gets blocks at all (btclib-org/btclib-node#1071).
    """
    node.status = NodeStatus.SyncingHeaders
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, chain)
    assert block_index.active_chain == [RegTest().genesis.hash, *hashes(chain)]
    assert node.status == NodeStatus.SyncingHeaders


def test_a_hole_behind_a_downloaded_tip_does_not_block_a_complete_branch(
    node: Node,
) -> None:
    """A complete branch connects while a separate, incomplete one is queued."""
    # get_first_candidate used to ask only whether a candidate's own tip
    # had arrived, so a branch missing a block *behind* its downloaded
    # tip still passed it -- and then update_chain found the hole and
    # gave up the whole pass, leaving that same candidate at the front
    # of the queue next time: btclib-org/btclib-node#121
    block_index = node.chainstate.block_index

    hole = generate_random_chain(2, RegTest().genesis.hash)
    block_index.add_headers([block.header for block in hole])
    for block in hole:
        node.block_db.add_block(block)
    block_index.set_downloaded(hole[-1].header.hash)  # the tip alone

    complete = generate_random_chain(1, RegTest().genesis.hash)
    block_index.add_headers([block.header for block in complete])
    for block in complete:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    update_chain(node)
    assert block_index.active_chain[1:] == hashes(complete)


def test_update_chain_refuses_a_block_marked_downloaded_but_missing(
    node: Node,
) -> None:
    """`update_chain` raises on a downloaded-but-missing block."""
    # the download manager and block_db agree by construction; this is
    # the state they would be in if they did not
    chain = generate_random_chain(1, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    # deliberately not added to node.block_db
    with pytest.raises(
        ChainstateInconsistencyError, match="just checked downloaded is missing"
    ):
        update_chain(node)


def hashes(chain: list[Block]) -> list[bytes]:
    """Return every block's own header hash, in the chain's own order."""
    return [block.header.hash for block in chain]


def settle(node: Node) -> None:
    """Drive `update_chain` until nothing outweighs the active chain further."""
    # get_first_candidate offers the shallowest block that already
    # outweighs active, not necessarily a longer fork's own tip, so one
    # call connects only as far as that block; this drives update_chain
    # until nothing outweighs active any more, the same thing connect()
    # does for a chain built from genesis
    block_index = node.chainstate.block_index
    while block_index.get_first_candidate() is not None:
        update_chain(node)


def test_a_heavier_fork_replaces_the_chain_the_node_was_on(node: Node) -> None:
    """A heavier fork replaces every block of the chain it outweighs."""
    # more than one block on the branch being left, because that is the
    # shallowest branch whose blocks have to be undone in an order: an
    # output block N created and block N+1 spent is gone from the utxo
    # set by the time N comes to be undone
    first = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, first)
    assert block_index.active_chain[1:] == hashes(first)

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)
    assert block_index.active_chain[1:] == hashes(second)
    for block_hash in hashes(first):
        assert block_hash not in block_index.active_chain


def test_a_reorg_refuses_a_missing_reverse_patch(node: Node) -> None:
    """A missing reverse patch raises `ChainstateInconsistencyError`."""
    # every block on the active chain has one, by construction; this is
    # the state block_db would be in if it did not
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.block_db.rev_patches.pop(first[-1].header.hash)

    second = generate_random_chain(3, RegTest().genesis.hash)
    with pytest.raises(ChainstateInconsistencyError, match="no reverse patch"):
        connect(node, second)


def test_a_reorg_refuses_a_missing_removed_block(node: Node) -> None:
    """A missing removed block raises `ChainstateInconsistencyError`."""
    # the reverse patch of the block being undone is enough to roll the
    # chainstate back; giving the transactions of that same block back
    # to the mempool needs the block itself, which is the gap this pins
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    node.block_db.blocks.pop(first[-1].header.hash)

    second = generate_random_chain(3, RegTest().genesis.hash)
    with pytest.raises(
        ChainstateInconsistencyError, match="block just removed is missing"
    ):
        connect(node, second)


def test_a_reorg_whose_own_undo_raises_propagates_and_names_no_new_block(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollback failing on the fork's own undo stops the node, blames nothing.

    `_resolve_trial_exception` (`main.py`) re-raises here rather than
    swallowing: `ChainstateInconsistencyError` is not one of
    `_CONTENT_FAILURE`'s three types, so this is this node's own
    storage proving itself inconsistent, not a new candidate's content,
    the same distinction `_CONTENT_FAILURE`'s own comment and Core's
    `ActivateBestChainStep` (`src/validation.cpp`, at
    bitcoin/bitcoin@b91d983f66) draw for a failed `DisconnectTip`. The
    rollback still runs first -- `active_chain_before` is unchanged --
    and `_resolve_trial_exception` never reaches `_record_rejection`
    here, so `Node.last_rejected_block` stays unset the same as
    `failed_hash` -- still `None` at this point in the trial, exactly
    as `update_header_index`'s own guard reads it, so none of the
    fork's own candidate blocks is invalidated either.
    """
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)
    active_chain_before = list(block_index.active_chain)

    heavier = generate_random_chain(3, RegTest().genesis.hash)
    block_index.add_headers([block.header for block in heavier])
    for block in heavier:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    def boom(rev_block: object) -> None:
        err_msg = "boom"
        raise ChainstateInconsistencyError(err_msg)

    monkeypatch.setattr(node.chainstate.utxo_index, "apply_rev_block", boom)

    with pytest.raises(ChainstateInconsistencyError, match="boom"):
        update_chain(node)

    assert block_index.active_chain == active_chain_before
    assert node.last_rejected_block is None
    for block in heavier:
        info = block_index.get_block_info(block.header.hash)
        assert info.status != BlockStatus.invalid


def test_an_io_fault_writing_the_reverse_patch_does_not_invalidate_the_block(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`block_db.add_rev_block` raising propagates, and blames no block.

    `add_rev_block` itself is a pure in-memory buffer -- `self.pending_
    rev_blocks[hash] = rev_block`, no I/O at all; the write `OSError`
    would actually come from happens later, inside `finalize`. What
    this test pins is the classification, not the origin: whatever
    `add_rev_block` raises, `OSError` here standing in for it, is not
    one of `_CONTENT_FAILURE`'s three types, so `update_chain`'s own
    except re-raises it rather than treating it as this candidate's own
    content -- the same distinction
    `test_a_reorg_whose_own_undo_raises_names_no_new_block` above pins
    for the trial's other loop.
    """
    candidate = generate_random_chain(1, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    active_chain_before = list(block_index.active_chain)
    block_index.add_headers([block.header for block in candidate])
    for block in candidate:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    def boom(rev_block: object) -> None:
        err_msg = "disk full"
        raise OSError(err_msg)

    monkeypatch.setattr(node.block_db, "add_rev_block", boom)

    with pytest.raises(OSError, match="disk full"):
        update_chain(node)

    assert block_index.active_chain == active_chain_before
    info = block_index.get_block_info(candidate[0].header.hash)
    assert info.status != BlockStatus.invalid
    assert node.last_rejected_block is None
    assert node.chainstate.utxo_index.updated_utxo_set == {}
    assert node.chainstate.utxo_index.removed_utxos == set()
    assert node.chainstate.filter_index.pending == {}
    assert node.block_db.pending_rev_blocks == {}


def test_an_io_fault_indexing_the_block_filter_does_not_invalidate_the_block(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`filter_index.add_connected_block` raising propagates the same way.

    The sibling of the test above, over the trial's other storage call:
    `add_connected_block` reads the parent's filter header off
    `KeyValueStore.get`, which is exactly the read
    btclib-org/btclib-node#620 is about.
    """
    candidate = generate_random_chain(1, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    active_chain_before = list(block_index.active_chain)
    block_index.add_headers([block.header for block in candidate])
    for block in candidate:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    def boom(block: object, rev_block: object) -> None:
        err_msg = "database is locked"
        raise OSError(err_msg)

    monkeypatch.setattr(node.chainstate.filter_index, "add_connected_block", boom)

    with pytest.raises(OSError, match="database is locked"):
        update_chain(node)

    assert block_index.active_chain == active_chain_before
    info = block_index.get_block_info(candidate[0].header.hash)
    assert info.status != BlockStatus.invalid
    assert node.last_rejected_block is None
    assert node.chainstate.utxo_index.updated_utxo_set == {}
    assert node.chainstate.utxo_index.removed_utxos == set()
    assert node.chainstate.filter_index.pending == {}
    assert node.block_db.pending_rev_blocks == {}


def test_a_stored_utxo_record_that_wont_parse_is_treated_as_absent(
    node: Node,
) -> None:
    """A truncated `utxo-` record now rejects the block, not the node.

    No monkeypatch: `node.chainstate.db.put` overwrites the same
    `utxo-` key `UtxoIndex.finalize` wrote, with a truncated copy of
    what was already there -- a checksum-clean record RocksDB's own
    read never flags, exactly the fault `_bip30_violation`'s own
    `db.get` truthiness check cannot tell from a healthy one. `bad`
    then spends that same, otherwise unspent, output: `Coin.parse`
    inside `add_block`'s prevout resolution raises `BTClibValueError`
    over bytes this node wrote for itself, not over anything `bad`
    supplied, and `add_block` now answers that the same way it answers
    a genuinely missing prevout -- `InvalidBlockInputError("prevout
    not found")` -- matching `CDBWrapper::Read`/`CCoinsViewDB::GetCoin`'s
    own "absent" rather than raising `ChainstateInconsistencyError`,
    now that RocksDB's own checksum (btclib-org/btclib-node#641) is
    what catches an actually corrupted record before this line is
    ever reached (btclib-org/btclib-node#620, btclib-org/btclib-node#650).
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, chain)

    funding = chain[0].transactions[0]
    key = b"utxo-" + OutPoint(funding.id, 0, check_validity=False).serialize(
        check_validity=False
    )
    original = node.chainstate.db.get(key)
    assert original is not None
    node.chainstate.db.put(key, original[:1])

    bad = build_block(
        chain[-1].header.hash,
        [
            generate_coinbase(height=len(chain) + 1),
            spend(funding, funding.vout[0].value),
        ],
        len(chain),
    )
    connect(node, [bad])

    assert bad.header.hash not in block_index.active_chain
    rejected_because(node, bad, "prevout not found")


def test_a_reorg_evicts_a_transaction_the_reorg_itself_invalidated(
    node: Node,
) -> None:
    """A reorg does not re-add a tx whose own coinbase it just abandoned."""
    # first is COINBASE_MATURITY + 1 long, so its own last block already
    # carries a second transaction -- generate_random_chain's own rule
    # -- spending first[0]'s coinbase, confirmed rather than merely
    # offered. second outweighs it and abandons the whole branch, first[0]
    # included, so _reconcile_mempool_for_reorg's own
    # oldest-abandoned-block-first walk reaches orphaned only after the
    # coinbase it spent is already undone: #85's MissingPrevoutError, not
    # a second implementation of it here, is what that walk's own except
    # catches and skips rather than re-adding.
    first = generate_random_chain(COINBASE_MATURITY + 1, RegTest().genesis.hash)
    connect(node, first)
    assert node.status == NodeStatus.BlockSynced

    orphaned = first[-1].transactions[1]
    assert orphaned.vin[0].prev_out.tx_id == first[0].transactions[0].id

    second = generate_random_chain(COINBASE_MATURITY + 2, RegTest().genesis.hash)
    connect(node, second)

    # #85: orphaned spent the abandoned branch's own coinbase, which no
    # longer exists on any chain once the reorg undoes it -- it is
    # rejected the same way any other entrant into the mempool would be,
    # and does not go back in
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, orphaned)
    assert not node.mempool.contains_tx(orphaned)


def test_still_final_and_mature_refuses_a_transaction_past_its_own_locktime(
    node: Node,
) -> None:
    """`is_final`'s own height-based check can refuse a held transaction.

    `_still_final_and_mature` re-runs finality against the tip as it
    stands now, not only at acceptance; called directly, with
    `lock_time` set to the tip's own height (not yet reached) and
    `sequence` short of Core's own escape hatch, it is this branch
    alone -- no reorg needed to move the tip backward under it.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    funding = chain[0].transactions[0]
    spend_height = len(node.chainstate.block_index.active_chain)
    nonfinal = locked_spend(
        funding, funding.vout[0].value, lock_time=spend_height, sequence=0
    )

    assert not main._still_final_and_mature(node, nonfinal)


def test_still_final_and_mature_keeps_a_transaction_whose_prevout_is_gone(
    node: Node,
) -> None:
    """An unresolvable prevout is kept here, not this function's own call.

    Neither the UTXO set nor the mempool holds what `ghost` spends.
    `_still_final_and_mature`'s own docstring argues why this is safe in
    production -- `_reconcile_mempool_for_reorg`'s own two
    `Mempool.remove_dependents` calls already take out whatever this
    shape would otherwise leave behind, before this function is ever
    reached for it; called directly, with neither of those having run,
    it is this branch alone.
    """
    ghost = generate_random_transaction()

    assert main._still_final_and_mature(node, ghost)


def test_still_final_and_mature_refuses_an_unmet_time_based_relative_lock(
    node: Node,
) -> None:
    """A BIP68 time-based relative lock unmet at re-check time refuses.

    Exercises the `ancestor_median_time_past` closure --
    `test_reject_block_whose_time_based_relative_lock_is_not_satisfied`
    above's own docstring is where the identical closure, in
    `_validate_block`, is argued a height-based lock never reaches.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    funding = chain[0].transactions[0]
    type_flag = 1 << 22
    unmet = relative_locked_spend(
        funding, funding.vout[0].value, sequence=type_flag | 1000
    )

    assert not main._still_final_and_mature(node, unmet)


def test_still_final_and_mature_accepts_an_ordinary_mature_spend(node: Node) -> None:
    """A final, mature, version-1 spend falls through every check kept."""
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    funding = chain[0].transactions[0]
    healthy = generate_random_transaction(funding.id, value=funding.vout[0].value)

    assert main._still_final_and_mature(node, healthy)


def test_evict_immature_or_nonfinal_skips_a_wtxid_a_cascade_already_took(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descendant a cascading eviction already removed is not rechecked.

    `parent` is never final -- the same shape
    `test_still_final_and_mature_refuses_a_transaction_past_its_own_
    locktime` above pins directly -- so evicting it takes `child` out
    too, through `Mempool.remove_with_descendants`. The loop's own
    snapshot still names `child`'s wtxid, and the guard above
    `_still_final_and_mature` skips it. The guard is a shortcut: a
    recheck of `child` would find its prevout gone, keep it, and leave the
    outcome the same. What it saves is that call, so the calls are
    counted: one, for `parent`.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    funding = chain[0].transactions[0]
    spend_height = len(node.chainstate.block_index.active_chain)
    parent = locked_spend(
        funding, funding.vout[0].value, lock_time=spend_height, sequence=0
    )
    assert node.mempool.add_tx(parent, 1000)
    child = generate_random_transaction(parent.id, value=parent.vout[0].value)
    assert node.mempool.add_tx(child, 1000)
    checked: list[Tx] = []
    real = main._still_final_and_mature

    def counting(node: Node, tx: Tx) -> bool:
        checked.append(tx)
        return real(node, tx)

    monkeypatch.setattr(main, "_still_final_and_mature", counting)

    main._evict_immature_or_nonfinal(node)

    assert checked == [parent]
    assert not node.mempool.contains_tx(parent)
    assert not node.mempool.contains_tx(child)


def test_a_connected_block_restarts_the_mempool_s_decay_clock(node: Node) -> None:
    """Connecting a block restarts the mempool's rolling-minimum decay clock."""
    # note_block_connected runs once per block update_chain connects to the
    # active chain, restarting Mempool.get_min_fee_rate's own decay clock --
    # Core's own removeForBlock (src/txmempool.cpp:405-427,
    # at bitcoin/bitcoin@58a7869f86) does this for every block regardless of
    # what it held. btclib-org/btclib-node#294
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    assert node.status == NodeStatus.BlockSynced

    node.mempool._rolling_min_fee_rate = 5000.0
    node.mempool._block_since_last_rolling_fee_bump = False
    node.mempool._last_rolling_fee_update = 0.0

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)

    assert node.mempool._block_since_last_rolling_fee_bump is True
    assert node.mempool._last_rolling_fee_update > 0.0


def test_each_connected_block_brings_the_stalling_timeout_down(node: Node) -> None:
    """`DownloadManager.block_connected` runs once per block connected.

    Core's `PeerManagerImpl::BlockConnected`. btclib-org/btclib-node#1179
    """
    node.download_manager.block_stalling_timeout = 64
    connect(node, generate_random_chain(2, RegTest().genesis.hash))
    # 64 * 0.85 is 54, and 54 * 0.85 is 45, in whole seconds
    assert node.download_manager.block_stalling_timeout == 45


def _extend(previous_hash: bytes, start_height: int, count: int) -> list[Block]:
    # generate_random_chain restarts its own height at 0 for any start,
    # which is a timestamp that has to beat the median of *these*
    # ancestors, not a fresh chain's -- explicit, increasing heights are
    # what test_a_refused_branch_invalidates_headers_that_were_never_
    # candidates uses for the same reason
    continuation: list[Block] = []
    for height in range(start_height, start_height + count):
        block = build_block(
            previous_hash, [generate_coinbase(height=height + 1)], height
        )
        continuation.append(block)
        previous_hash = block.header.hash
    return continuation


@pytest.mark.parametrize("status", [NodeStatus.SyncingHeaders, NodeStatus.BlockSynced])
def test_a_reorg_still_resurrects_a_transaction_its_prevout_survives(
    node: Node, status: NodeStatus
) -> None:
    """A confirmed tx whose prevout survives the reorg re-enters the mempool.

    Before header sync ends too: Core's `MaybeUpdateMempoolForReorg`
    reads no sync state (btclib-org/btclib-node#1144).
    """
    node.status = status
    # #85's fix checks every re-added transaction rather than trusting
    # it: this is the other side of that, a transaction that spent an
    # output the reorg does not touch and is still good on the chain
    # that replaces the one it was confirmed on. common is
    # COINBASE_MATURITY long so that resurrectable, spending its own
    # first block's coinbase once abandoned extends past the tip, is a
    # spend this rule accepts rather than one it refuses on its own.
    common = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, common)

    resurrectable = generate_random_transaction(common[0].transactions[0].id)
    abandoned = build_block(
        common[-1].header.hash,
        [generate_coinbase(height=len(common) + 1), resurrectable],
        len(common),
    )
    fork = [*common, abandoned]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(fork)

    heavier = [*common, *_extend(common[-1].header.hash, len(common), 2)]
    block_index.add_headers([block.header for block in heavier[1:]])
    for block in heavier[1:]:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    assert node.status == status
    settle(node)
    assert block_index.active_chain[1:] == hashes(heavier)

    assert node.mempool.contains_tx(resurrectable)


@pytest.mark.parametrize("fee", [0, FEE])
def test_a_reorg_re_adds_a_dust_spend_only_if_it_pays_no_fee(
    node: Node, fee: int
) -> None:
    """`bypass_limits` skips the fee floors but not `PreCheckEphemeralTx`.

    A confirmed spend with a dust output comes back after a reorg when it
    paid nothing and stays out when it paid a fee, as bitcoin-node-tests'
    `mempool_ephemeral_dust` asserts of Core (btclib-org/btclib-node#1594).
    """
    common = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, common)
    funding = common[0].transactions[0]
    dusty = with_outputs(
        generate_random_transaction(funding.id, value=funding.vout[0].value - fee),
        TxOut(0, anyone_can_spend()),
    )
    abandoned = build_block(
        common[-1].header.hash,
        [generate_coinbase(height=len(common) + 1), dusty],
        len(common),
    )
    fork = [*common, abandoned]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(fork)

    heavier = [*common, *_extend(common[-1].header.hash, len(common), 2)]
    block_index.add_headers([block.header for block in heavier[1:]])
    for block in heavier[1:]:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(heavier)

    assert node.mempool.contains_tx(dusty) == (fee == 0)


def test_a_reorg_re_adds_abandoned_transactions_parent_first(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reorg re-adds an abandoned parent before the child that spends it.

    Each at the vsize verification answered (btclib-org/btclib-node#1357),
    which here is marked one over the real one to tell the two apart.
    """
    real = main.verify_mempool_acceptance

    def marked(node: Node, tx: Tx, *, bypass_limits: bool = False) -> Any:
        fee, vsize = real(node, tx, bypass_limits=bypass_limits)
        return main.MempoolAcceptance(fee, vsize + 1)

    monkeypatch.setattr(main, "verify_mempool_acceptance", marked)
    # a chain of two transactions confirmed only on the branch being
    # abandoned: the second spends the first's own output, which exists
    # nowhere but the mempool once the reorg undoes both blocks, so it
    # has to find its parent already there. Processed tip-first --
    # to_remove's own order, kept for the utxo undo above it -- the
    # child is checked before the parent it depends on ever returns,
    # and verify_mempool_acceptance drops it as a missing prevout for
    # good; Core's own MaybeUpdateMempoolForReorg re-adds oldest first
    # for the same reason (src/validation.cpp). common is
    # COINBASE_MATURITY long for the same reason as the sibling test
    # above: parent spends its own first block's coinbase, and that has
    # to be old enough by the time older connects.
    common = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, common)

    parent = generate_random_transaction(common[0].transactions[0].id)
    older = build_block(
        common[-1].header.hash,
        [generate_coinbase(height=len(common) + 1), parent],
        len(common),
    )
    child = generate_random_transaction(parent.id)
    newer = build_block(
        older.header.hash,
        [generate_coinbase(height=len(common) + 2), child],
        len(common) + 1,
    )
    fork = [*common, older, newer]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(fork)

    heavier = [*common, *_extend(common[-1].header.hash, len(common), 3)]
    block_index.add_headers([block.header for block in heavier[1:]])
    for block in heavier[1:]:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(heavier)

    assert node.mempool.contains_tx(parent)
    assert node.mempool.contains_tx(child)
    assert node.mempool.vsizes[parent.hash] == parent.vsize + 1


def test_a_block_connected_before_header_sync_ends_leaves_the_mempool(
    node: Node,
) -> None:
    """A connected block's own transactions leave the mempool at any status.

    Core's `ConnectTip` runs `removeForBlock` whatever the sync state
    (btclib-org/btclib-node#1144).
    """
    node.status = NodeStatus.SyncingHeaders
    # generate_random_chain's own last block, past COINBASE_MATURITY,
    # carries a second transaction spending chain[0]'s coinbase
    chain = generate_random_chain(COINBASE_MATURITY + 1, RegTest().genesis.hash)
    connect(node, chain[:-1])
    mined = chain[-1].transactions[1]
    # the block pays its coinbase the whole input, so this spend is fee-free
    fee = verify_mempool_acceptance(node, mined, bypass_limits=True).fee
    node.mempool.add_tx(mined, fee)

    connect(node, chain[-1:])
    assert node.chainstate.block_index.active_chain[-1] == chain[-1].header.hash
    assert node.status == NodeStatus.SyncingHeaders
    assert not node.mempool.contains_tx(mined)


def a_peer(
    sent: list[Any],
    availability: BlockAvailability | None = None,
    *,
    prefers_headers: bool = True,
    high_bandwidth: bool = False,
) -> Connection:
    """Build a connection double that records what it is sent."""
    return cast(
        "Connection",
        SimpleNamespace(
            prefers_headers=prefers_headers,
            requested_hb_cmpctblocks=high_bandwidth,
            send=sent.append,
            block_availability=availability or BlockAvailability(),
        ),
    )


def test_a_newly_connected_block_is_announced_to_every_connected_peer(
    node: Node,
) -> None:
    """A connected block reaches every peer: its headers, or the tip's `inv`."""
    # an accepted block used to reach nobody, by either shape.
    # btclib-org/btclib-node#202
    first = generate_random_chain(1, RegTest().genesis.hash)
    connect(node, first)
    assert node.status == NodeStatus.BlockSynced

    header_sent: list[Any] = []
    inv_sent: list[Any] = []
    genesis = RegTest().genesis.hash
    node.p2p_manager.connections[1] = a_peer(
        header_sent, BlockAvailability(best_known=genesis)
    )
    node.p2p_manager.connections[2] = a_peer(
        inv_sent, BlockAvailability(best_known=genesis), prefers_headers=False
    )

    # a recent tip, so that connecting it ends initial block download
    second = generate_random_chain(
        2, RegTest().genesis.hash, tip_time=datetime.now(UTC)
    )
    connect(node, second)

    (sent,) = header_sent
    assert isinstance(sent, Headers)
    assert [header.hash for header in sent.headers] == hashes(second)
    peer = node.p2p_manager.connections[1]
    assert peer.block_availability.best_header_sent == second[-1].header.hash

    # Core's `SendMessages` sends a peer that did not ask for headers an
    # `inv` of the tip alone
    (sent,) = inv_sent
    assert isinstance(sent, Inv)
    assert sent.items == (Inventory(InventoryType.MSG_BLOCK, second[-1].header.hash),)


@pytest.mark.parametrize("field", ["best_known", "best_header_sent"])
def test_a_peer_is_sent_the_headers_from_the_first_one_it_lacks(
    node: Node, field: str
) -> None:
    """A header the peer has is not sent again, whichever way it has it.

    Core's `PeerHasHeader` reads the best block the peer announced and
    the best header it was sent (btclib-org/btclib-node#1160).
    """
    # a chain one block shorter, so that the one below replaces it in a
    # single fork, announced whole
    connect(node, generate_random_chain(2, RegTest().genesis.hash))
    chain = generate_random_chain(3, RegTest().genesis.hash, tip_time=datetime.now(UTC))
    node.chainstate.block_index.add_headers([block.header for block in chain])
    sent: list[Any] = []
    availability = BlockAvailability()
    setattr(availability, field, chain[0].header.hash)
    node.p2p_manager.connections[1] = a_peer(sent, availability)

    connect(node, chain)
    assert node.chainstate.block_index.active_chain[1:] == hashes(chain)

    (message,) = sent
    assert isinstance(message, Headers)
    assert [header.hash for header in message.headers] == hashes(chain[1:])


@pytest.mark.parametrize("prefers_headers", [True, False])
def test_the_peer_that_sent_the_block_hears_nothing_back(
    node: Node, *, prefers_headers: bool
) -> None:
    """A peer that has the new tip is sent neither its headers nor its `inv`.

    Core's `SendMessages` skips every header `PeerHasHeader` answers for,
    and sends no `inv` of a tip the peer has (btclib-org/btclib-node#1160).
    """
    chain = generate_random_chain(1, RegTest().genesis.hash, tip_time=datetime.now(UTC))
    sent: list[Any] = []
    node.p2p_manager.connections[1] = a_peer(
        sent,
        BlockAvailability(best_known=chain[0].header.hash),
        prefers_headers=prefers_headers,
    )
    node.chainstate.block_index.add_headers([chain[0].header])

    connect(node, chain)
    assert node.chainstate.block_index.active_chain[-1] == chain[0].header.hash
    assert not sent


def test_a_peer_that_has_no_header_to_connect_to_is_sent_the_tip_s_inv(
    node: Node,
) -> None:
    """Headers that would not connect to one the peer has become an `inv`.

    Core's `SendMessages` reverts to an `inv` of the tip where the first
    header the peer lacks has a parent it lacks too.
    """
    chain = generate_random_chain(2, RegTest().genesis.hash, tip_time=datetime.now(UTC))
    sent: list[Any] = []
    node.p2p_manager.connections[1] = a_peer(sent)

    connect(node, chain)

    (message,) = sent
    assert isinstance(message, Inv)
    assert message.items == (Inventory(InventoryType.MSG_BLOCK, chain[-1].header.hash),)
    peer = node.p2p_manager.connections[1]
    assert peer.block_availability.best_header_sent is None


@pytest.mark.parametrize("prefers_headers", [True, False])
def test_a_high_bandwidth_peer_is_sent_a_lone_new_block_as_a_cmpctblock(
    node: Node, *, prefers_headers: bool
) -> None:
    """One new block whose parent the peer has goes as a `cmpctblock`.

    Core's `SendMessages` sends a peer that asked for high bandwidth a
    single header as the block's `cmpctblock`, whether or not it asked
    for headers (btclib-org/btclib-node#1223). Every peer gets the same
    one here, as in Core where `NewPoWValidBlock` left the block in
    `m_most_recent_compact_block`; where it did not, Core builds one per
    peer under a fresh nonce (`src/net_processing.cpp:5899-5913`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    chain = generate_random_chain(2, RegTest().genesis.hash, tip_time=datetime.now(UTC))
    connect(node, chain[:1])
    sent: dict[int, list[Any]] = {1: [], 2: []}
    for conn_id, messages in sent.items():
        node.p2p_manager.connections[conn_id] = a_peer(
            messages,
            BlockAvailability(best_known=chain[0].header.hash),
            prefers_headers=prefers_headers,
            high_bandwidth=True,
        )

    connect(node, chain[1:])
    (first,) = sent[1]
    assert isinstance(first, CmpctBlock)
    # serialized: `tip_time` carries microseconds the stored header drops
    assert first.serialize() == compact_block(chain[1], first.nonce).serialize()
    assert sent[2] == [first]
    peer = node.p2p_manager.connections[1]
    assert peer.block_availability.best_header_sent == chain[1].header.hash


@pytest.mark.parametrize(
    ("prefers_headers", "known", "expected"),
    [(False, None, Inv), (True, None, Headers), (True, 0, CmpctBlock)],
    ids=["inv", "headers", "cmpctblock"],
)
def test_a_high_bandwidth_peer_announced_two_blocks_gets_a_cmpctblock_for_one(
    node: Node, *, prefers_headers: bool, known: int | None, expected: type
) -> None:
    """Two new blocks go as a `cmpctblock` only where one header is to send.

    Core's `SendMessages` reverts to an `inv` for more than one block to
    a peer that did not ask for headers, and otherwise sends a
    `cmpctblock` only where the peer lacks the tip alone. `known` is the
    fork block the peer has, genesis where `None`.
    """
    connect(node, generate_random_chain(1, RegTest().genesis.hash))
    fork = generate_random_chain(2, RegTest().genesis.hash, tip_time=datetime.now(UTC))
    node.chainstate.block_index.add_headers([block.header for block in fork])
    best_known = RegTest().genesis.hash if known is None else fork[known].header.hash
    sent: list[Any] = []
    node.p2p_manager.connections[1] = a_peer(
        sent,
        BlockAvailability(best_known=best_known),
        prefers_headers=prefers_headers,
        high_bandwidth=True,
    )

    connect(node, fork)
    assert node.chainstate.block_index.active_chain[1:] == hashes(fork)

    (message,) = sent
    assert isinstance(message, expected)


@pytest.mark.parametrize(
    ("length", "known", "announced"),
    [(8, None, 8), (9, None, 0), (9, 0, 8)],
)
def test_a_fork_is_announced_by_its_newest_eight_blocks_at_most(
    node: Node, length: int, known: int | None, announced: int
) -> None:
    """Only the newest eight blocks of a fork are ever sent as headers.

    Core's `MAX_BLOCKS_TO_ANNOUNCE`: `UpdatedBlockTip` queues no more of
    the newest, and `SendMessages` sends them as headers only where the
    first connects to a header the peer has, else the tip's `inv`.
    `known` is the fork block the peer has, genesis where `None`;
    `announced` is how many headers it is sent, 0 for the `inv`.
    """
    # a chain one block shorter, so that the recent-tipped one below
    # replaces it in a single fork of `length` blocks
    connect(node, generate_random_chain(length - 1, RegTest().genesis.hash))
    fork = generate_random_chain(
        length, RegTest().genesis.hash, tip_time=datetime.now(UTC)
    )
    node.chainstate.block_index.add_headers([block.header for block in fork])
    has = RegTest().genesis.hash if known is None else fork[known].header.hash
    sent: list[Any] = []
    node.p2p_manager.connections[1] = a_peer(sent, BlockAvailability(best_known=has))

    connect(node, fork)
    assert node.chainstate.block_index.active_chain[1:] == hashes(fork)

    (message,) = sent
    if announced:
        assert isinstance(message, Headers)
        assert [header.hash for header in message.headers] == hashes(fork[-announced:])
    else:
        assert isinstance(message, Inv)
        assert message.items == (
            Inventory(InventoryType.MSG_BLOCK, fork[-1].header.hash),
        )


def test_a_block_connected_with_an_empty_mempool_asks_it_nothing_per_tx(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty mempool is asked for no transaction, yet sees every block.

    `Mempool.remove_tx` hashes the transaction it is asked about, which
    every block connected in initial block download would pay for; the
    once-per-block `note_block_connected` still runs, as Core's
    `removeForBlock` does.
    """
    chain = generate_random_chain(COINBASE_MATURITY + 1, RegTest().genesis.hash)
    connect(node, chain[:-1])
    assert node.mempool.size == 0
    asked: list[Tx] = []
    monkeypatch.setattr(node.mempool, "remove_tx", asked.append)
    node.mempool._block_since_last_rolling_fee_bump = False

    connect(node, chain[-1:])
    assert len(chain[-1].transactions) > 1
    assert not asked
    assert node.mempool._block_since_last_rolling_fee_bump is True


def test_a_reorg_during_initial_block_download_announces_nothing(
    node: Node,
) -> None:
    """A reorg in initial block download sends no peer anything, synced or not.

    Core's `UpdatedBlockTip` returns on `fInitialDownload` alone
    (btclib-org/btclib-node#1148): `generate_random_chain` dates every
    block too far back for the tip to end it.
    """
    first = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, first)
    assert node.status == NodeStatus.BlockSynced

    sent: list[Any] = []
    node.p2p_manager.connections[1] = a_peer(
        sent, BlockAvailability(best_known=RegTest().genesis.hash)
    )

    second = generate_random_chain(3, RegTest().genesis.hash)
    connect(node, second)
    assert node.is_initial_block_download is True
    assert not sent


def test_the_block_ending_initial_block_download_is_announced_before_sync(
    node: Node,
) -> None:
    """The block ending initial block download is announced at any status.

    A node no peer has sent a header stays `SyncingHeaders`, and the
    block `submitblock` hands it is announced once it is recent
    (btclib-org/btclib-node#1148): Core's `ConnectTip` updates the IBD
    latch before `UpdatedBlockTip` reads it.
    """
    node.status = NodeStatus.SyncingHeaders
    sent: list[Any] = []
    node.p2p_manager.connections[1] = a_peer(
        sent, BlockAvailability(best_known=RegTest().genesis.hash)
    )

    chain = generate_random_chain(1, RegTest().genesis.hash, tip_time=datetime.now(UTC))
    connect(node, chain)
    assert node.status == NodeStatus.SyncingHeaders
    assert node.is_initial_block_download is False
    (headers,) = sent
    assert isinstance(headers, Headers)
    assert [header.hash for header in headers.headers] == hashes(chain)


def test_blocknotify_does_not_fire_during_initial_block_download(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1519: Core's own `NotifyBlockTip_connect` gate.

    `if (sync_state != POST_INIT) return;` (`src/init.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `generate_random_chain`
    dates every block too far back for the tip to end initial block
    download, the same premise `test_a_reorg_during_initial_block_
    download_announces_nothing` above rests on.

    A separate test from the one below, on purpose: mypy's own
    narrowing of `node.is_initial_block_download` from an `is True`
    check does not see `connect` (an ordinary function call) as able to
    change it, and reports the final assertion of a combined version of
    this test as unreachable once a later `is False` check follows it
    in the same function -- confirmed against a minimal reproduction
    outside this tree, the same mypy limitation
    `chainstate/block_index_test.py`'s own
    `test_invalidate_sets_best_invalid_to_the_first_invalidated_block`
    docstring names.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        main, "run_detached", lambda logger, command: calls.append(command)
    )
    node.config.block_notify = "touch %s"

    stale = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, stale)
    assert node.is_initial_block_download is True
    assert calls == []


def test_blocknotify_fires_once_with_the_new_tips_hash_once_ibd_ends(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1519: `%s` is the new tip's hash, hex, once per commit.

    `recent` connects three blocks in one `_after_tip_change` call, so
    one call with the branch's own tip is also what tells this apart
    from firing once per block in it.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        main, "run_detached", lambda logger, command: calls.append(command)
    )
    node.config.block_notify = "touch %s"

    recent = generate_random_chain(
        3, RegTest().genesis.hash, tip_time=datetime.now(UTC)
    )
    connect(node, recent)
    assert node.is_initial_block_download is False
    assert calls == [f"touch {recent[-1].header.hash.hex()}"]


def test_unknown_activations_are_not_checked_during_initial_block_download(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1475: `Chainstate::UpdateTip` asks `if (!IsInitialBlockDownload())`.

    `generate_random_chain` dates every block too far back for the tip to
    end initial block download, as `test_blocknotify_does_not_fire_during_
    initial_block_download` above has it.
    """
    calls: list[tuple[Node, int]] = []
    monkeypatch.setattr(
        main, "check_unknown_activations", lambda *args: calls.append(args)
    )
    connect(node, generate_random_chain(2, RegTest().genesis.hash))
    assert node.is_initial_block_download is True
    assert calls == []


def test_unknown_activations_are_checked_once_per_commit_out_of_ibd(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1475: one call, with the number of blocks the commit connected."""
    calls: list[tuple[Node, int]] = []
    monkeypatch.setattr(
        main, "check_unknown_activations", lambda *args: calls.append(args)
    )
    recent = generate_random_chain(
        3, RegTest().genesis.hash, tip_time=datetime.now(UTC)
    )
    connect(node, recent)
    assert node.is_initial_block_download is False
    assert calls == [(node, 1)]


def test_a_reorganisation_checks_each_block_it_connects(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1475: the count is the blocks of the commit, here two."""
    calls: list[tuple[Node, int]] = []
    monkeypatch.setattr(
        main, "check_unknown_activations", lambda *args: calls.append(args)
    )
    genesis = RegTest().genesis.hash
    connect(node, generate_random_chain(1, genesis, tip_time=datetime.now(UTC)))
    connect(node, generate_random_chain(2, genesis, tip_time=datetime.now(UTC)))
    assert calls == [(node, 1), (node, 2)]


def test_check_fork_warning_conditions_raises_once_and_clears_on_catch_up(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1522: more than six blocks' worth of work past the tip, once.

    Core's own `CheckForkWarningConditions` (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). `alert_notify` fires only
    the first time the condition becomes true -- `Warnings::Set`'s own
    dedup -- and the warning clears, silently, once the active tip's own
    chainwork catches back up.
    """
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main,
        "alert_notify",
        lambda logger, command, message: calls.append((command, message)),
    )
    node.config.alert_notify = "echo %s"
    block_index = node.chainstate.block_index
    genesis = RegTest().genesis.hash

    invalid_chain = generate_random_header_chain(8, genesis)
    block_index.add_headers(invalid_chain)
    block_index.invalidate(invalid_chain[-1].hash)

    check_fork_warning_conditions(node)
    assert node.warnings.get_messages() == [
        (
            "Warning: Found invalid chain more than 6 blocks longer than our "
            "best chain. This could be due to database corruption or "
            "consensus incompatibility with peers."
        )
    ]
    assert len(calls) == 1

    # still true: no second call
    check_fork_warning_conditions(node)
    assert len(calls) == 1

    catch_up = generate_random_header_chain(9, genesis)
    block_index.add_headers(catch_up)
    for header in catch_up:
        block_index.add_to_active_chain(header.hash)
    check_fork_warning_conditions(node)
    assert node.warnings.get_messages() == []
    assert len(calls) == 1


def test_a_long_branch_on_a_failed_block_raises_the_fork_warning(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed connect weighs the downloaded branch built on the block.

    Core's own `ActivateBestChainStep` hands `InvalidChainFound` the top of
    the batch it was connecting, and `FindMostWorkChain` the candidate
    itself (`src/validation.cpp:3287` and `:3190-3191`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): here `branch[-1]`, twelve
    blocks' worth of work against a two-block tip.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        main, "alert_notify", lambda logger, command, message: calls.append(message)
    )
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)
    bad = build_block(
        RegTest().genesis.hash, [generate_coinbase(50 * 10**8 + 1, height=1)], 0
    )
    branch = [bad, *_extend(bad.header.hash, 1, 11)]
    block_index.add_headers([block.header for block in branch])
    for block in branch:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    update_chain(node)

    assert block_index.active_chain[1:] == hashes(active)
    assert block_index.best_invalid == branch[-1].header.hash
    assert len(node.warnings.get_messages()) == 1
    assert len(calls) == 1


def test_a_refused_branch_invalidates_only_the_block_that_failed(
    node: Node,
) -> None:
    """A failing tip is marked invalid; blocks under it stay `valid_header`."""
    # the branch is tried as a unit: its tip is what get_first_candidate
    # offers, so the blocks under it connect in the same pass the tip is
    # refused in, and the utxo set and the filter index are rolled back.
    # Neither rollback reaches the block index; what does is
    # update_header_index, on the one block whose own contextual check
    # raised -- the ones under it never failed anything and stay
    # valid_header, ready to connect if a different tip is built on them.
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)

    # a coinbase paying more than its own subsidy, not a spend of
    # below's own tip: that coinbase is not yet COINBASE_MATURITY deep,
    # and a spend of it would be refused for prematurity before ever
    # reaching the block-index machinery this test is about
    below = generate_random_chain(2, RegTest().genesis.hash)
    bad_tip = build_block(
        below[-1].header.hash,
        [generate_coinbase(50 * 10**8 + 1, height=len(below) + 1)],
        len(below),
    )
    fork = [*below, bad_tip]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == bad_tip.header.hash

    update_chain(node)
    assert block_index.active_chain[1:] == hashes(active)
    for block in below:
        info = block_index.get_block_info(block.header.hash)
        assert info.status == BlockStatus.valid_header
    assert block_index.get_block_info(bad_tip.header.hash).status == BlockStatus.invalid
    # the doomed tip no longer weighs on what get_first_candidate offers
    assert block_index.get_first_candidate() is None

    node.chainstate.close()
    reopened = Chainstate(node.data_dir, RegTest(), node.logger)
    for block in below:
        info = reopened.block_index.get_block_info(block.header.hash)
        assert info.status == BlockStatus.valid_header
    assert (
        reopened.block_index.get_block_info(bad_tip.header.hash).status
        == BlockStatus.invalid
    )
    reopened.close()


def test_a_refused_branch_leaves_no_reverse_patches_in_the_block_store(
    node: Node,
) -> None:
    """A rolled-back trial leaves no reverse patch behind for any block."""
    # active outweighs below's own two blocks individually, so only
    # bad_tip -- the fork's tip -- is its own candidate and the
    # whole fork connects in one trial. below's two blocks validate and
    # each generate a reverse patch before bad_tip fails and the
    # trial is rolled back: btclib-org/btclib-node#200
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)

    # a coinbase paying more than its own subsidy, not a spend of
    # below's own tip: that coinbase is not yet COINBASE_MATURITY deep,
    # and a spend of it would be refused for prematurity before ever
    # reaching the block-index machinery this test is about
    below = generate_random_chain(2, RegTest().genesis.hash)
    bad_tip = build_block(
        below[-1].header.hash,
        [generate_coinbase(50 * 10**8 + 1, height=len(below) + 1)],
        len(below),
    )
    fork = [*below, bad_tip]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == bad_tip.header.hash

    update_chain(node)

    assert block_index.active_chain[1:] == hashes(active)
    for block in below:
        assert node.block_db.get_rev_block(block.header.hash) is None
        assert block.header.hash not in node.block_db.rev_patches
    assert node.block_db.pending_rev_blocks == {}


def test_a_refused_branch_invalidates_headers_that_were_never_candidates(
    node: Node,
) -> None:
    """Invalidation cascades to blocks that never outweighed active alone."""
    # neither the block that fails nor a sibling built on it has to have
    # individually outweighed the active chain to be real: only the
    # branch's own tip does, for update_chain to try connecting it at
    # all. Both are hidden from block_candidates and only reachable by
    # walking BlockIndex.children -- proves the cascade through the real
    # update_chain -> update_header_index -> invalidate call chain, not
    # just the isolated BlockIndex-level call: btclib-org/btclib-node#125
    active = generate_random_chain(6, RegTest().genesis.hash)
    block_index = connect(node, active)

    # a coinbase paying more than its own subsidy, not a spend of
    # below's own tip: that coinbase is not yet COINBASE_MATURITY deep,
    # and a spend of it would be refused for prematurity before ever
    # reaching the block-index machinery this test is about
    below = generate_random_chain(2, RegTest().genesis.hash)
    bad_tip = build_block(
        below[-1].header.hash,
        [generate_coinbase(50 * 10**8 + 1, height=len(below) + 1)],
        len(below),
    )
    # more, structurally fine, blocks on top of the doomed one -- their
    # combined chainwork is what makes the branch's tip outweigh active,
    # not bad_tip on its own. Built with an explicit, increasing
    # height rather than generate_random_chain's own (which restarts at
    # 0 for any start): a header's timestamp has to beat the median of
    # its ancestors, and build_block's is derived from the height alone
    continuation: list[Block] = []
    previous = bad_tip
    for height in range(len(below) + 1, len(below) + 5):
        previous = build_block(previous.header.hash, [generate_coinbase()], height)
        continuation.append(previous)
    fork = [*below, bad_tip, *continuation]
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    # a sibling of the continuation, off bad_tip, real and indexed
    # but never downloaded and never its own block_candidates entry
    sibling = generate_random_header_chain(1, bad_tip.header.hash, bad_tip.header.time)
    block_index.add_headers(sibling)
    # the branch's own tip is the one candidate entry: everything below
    # it, bad_tip included, never individually outweighed active
    # on its own
    hidden = {bad_tip.header.hash, sibling[0].hash}
    hidden.update(block.header.hash for block in continuation[:-1])
    assert not hidden & {h for h, _ in block_index.block_candidates}

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == continuation[-1].header.hash

    update_chain(node)
    assert block_index.active_chain[1:] == hashes(active)
    for block_hash in {*hidden, continuation[-1].header.hash}:
        assert block_index.get_block_info(block_hash).status == BlockStatus.invalid


def test_a_stop_mid_reorg_rolls_the_trial_back_without_invalidating_it(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shutdown mid-trial rolls back cleanly, marking nothing invalid."""
    # `terminate_flag` is read between the blocks of `to_add`, so a
    # shutdown requested during a reorg is noticed after the block being
    # validated when it arrived rather than after the whole fork:
    # btclib-org/btclib-node#139. Nothing update_chain buffers along the
    # way reaches disk until every block of the fork has validated, so
    # the state this pins is not "stopped partway, with some of the fork
    # applied" -- there is no such state to reach -- but "stopped with
    # none of it applied, and the block it stopped on left alone", which
    # is what tells this apart from a block that failed its own check.
    active = generate_random_chain(2, RegTest().genesis.hash)
    block_index = connect(node, active)
    active_chain_before = list(block_index.active_chain)

    fork = generate_random_chain(4, RegTest().genesis.hash)
    block_index.add_headers([block.header for block in fork])
    for block in fork:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    # get_first_candidate offers the shallowest block that already
    # outweighs active, not necessarily the fork's own tip -- to_add is
    # whatever get_fork_details returns for that candidate, and this
    # pins the trial to stop inside it rather than assuming it is the
    # whole of `fork`
    candidate = block_index.get_first_candidate()
    assert candidate is not None
    to_add_hash, _ = block_index.get_fork_details(candidate.header.hash)
    assert len(to_add_hash) >= 3

    calls = 0

    def stop_after_the_second_block(
        transaction_data: list[tuple[list[Coin], Tx]],
        flags: ScriptFlag,
        node: Node,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            node.terminate_flag.set()
        return check_scripts(transaction_data, flags, node)

    monkeypatch.setattr(main, "check_scripts", stop_after_the_second_block)

    update_chain(node)

    # stopped between the second and the third block of the trial, not
    # partway through validating either and not at its end
    assert calls == 2
    assert block_index.active_chain == active_chain_before
    # a shutdown is not a defect in the block it landed on: none of the
    # fork's blocks is marked invalid, and the same candidate is still
    # offered whole
    for block in fork:
        info = block_index.get_block_info(block.header.hash)
        assert info.status != BlockStatus.invalid
    stopped_candidate = block_index.get_first_candidate()
    assert stopped_candidate is not None
    assert stopped_candidate.header.hash == candidate.header.hash
    # every buffer the trial writes into on its way to `finalize` is
    # back to empty, the same as after a block that failed its own check
    assert node.chainstate.utxo_index.updated_utxo_set == {}
    assert node.chainstate.utxo_index.removed_utxos == set()
    assert node.chainstate.filter_index.pending == {}
    assert node.block_db.pending_rev_blocks == {}

    # nothing here is stuck: a run with nothing asking it to stop
    # connects the whole fork, the same number of passes connect() takes
    # to drive any other fork of this length
    node.terminate_flag.clear()
    for _ in range(len(fork)):
        update_chain(node)
    assert block_index.active_chain[1:] == hashes(fork)


def stored_status(chainstate: Chainstate, block_hash: bytes) -> BlockStatus:
    """Read a block's own status off the store, not off `header_dict`.

    `KeyValueStore.get` answers `bytes | None`, and a caller of this
    helper already knows the record is there -- the point of every one
    below is that it either is or is not yet, never that it might not
    parse.
    """
    data = chainstate.db.get(b"blkinfo-" + block_hash)
    assert data is not None
    return BlockInfo.deserialize(data, check_validity=False).status


def test_the_utxo_cache_stays_staged_until_the_bound_then_flushes_all_three(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below `UtxoIndex`'s own bound nothing reaches disk; at it, all three do.

    `_FLUSH_BOUND` is lowered to 2 rather than exercised at its real
    size (btclib-org/btclib-node#586): each of these two blocks is a
    bare coinbase, staging exactly one entry, so the first leaves the
    bound unmet and the second reaches it -- the same shape a mainnet
    block reaches it in, only smaller.
    """
    monkeypatch.setattr(utxo_index_module, "_FLUSH_BOUND", 2)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    filter_index = node.chainstate.filter_index
    block_index.add_headers([block.header for block in chain])
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    first_out = OutPoint(chain[0].transactions[0].id, 0).serialize(check_validity=False)
    second_out = OutPoint(chain[1].transactions[0].id, 0).serialize(
        check_validity=False
    )

    update_chain(node)
    # one entry staged, one short of the bound: nothing below is on disk,
    # even though the in-memory chain already reflects the connection
    assert block_index.active_chain[1:] == [chain[0].header.hash]
    assert node.chainstate.db.get(b"utxo-" + first_out) is None
    assert (
        stored_status(node.chainstate, chain[0].header.hash) == BlockStatus.valid_header
    )
    assert node.chainstate.db.get(b"cfilter-" + chain[0].header.hash) is None
    # still answers correctly, staged rather than written
    assert filter_index.get_filter(chain[0].header.hash) is not None

    update_chain(node)
    # the second block's own entry reaches the bound, and the flush that
    # trips writes both blocks' status, both filters and both coins --
    # never only the block that happened to cross it
    assert node.chainstate.db.get(b"utxo-" + first_out) is not None
    assert node.chainstate.db.get(b"utxo-" + second_out) is not None
    for block in chain:
        assert (
            stored_status(node.chainstate, block.header.hash)
            == BlockStatus.in_active_chain
        )
        assert node.chainstate.db.get(b"cfilter-" + block.header.hash) is not None


def test_a_store_closed_without_a_flush_redoes_only_what_was_never_flushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unclean stop costs the blocks since the last flush, and nothing else.

    `Chainstate.close` is what flushes on a clean stop -- skipped here,
    on purpose, to stand in for a kill or a crash that never reaches it.
    The reopened store is not corrupted by that: it simply holds
    whatever the last actual flush wrote, `db.py`'s own docstring
    argues why, and driving it through `update_chain` again reaches the
    same chain a clean run would have, recomputed rather than read back.
    """
    monkeypatch.setattr(utxo_index_module, "_FLUSH_BOUND", 2)
    config = Config(
        chain="regtest", data_dir=tmp_path, allow_p2p=False, allow_rpc=False, debug=True
    )
    first = Node(config)
    first.load()
    first.status = NodeStatus.HeaderSynced

    chain = generate_random_chain(3, RegTest().genesis.hash)
    block_index = first.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block in chain:
        first.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    for _ in range(len(chain)):
        update_chain(first)
    # all three connected, in memory -- one flush happened along the way
    # (the second block reached the bound), the third block's own entry
    # left staged rather than written
    assert block_index.active_chain[1:] == [block.header.hash for block in chain]

    # an unclean stop: the connection closes with nothing flushed for
    # it, unlike Chainstate.close, which always flushes first. Every
    # other handle this node opened is still closed explicitly, the same
    # teardown tests/conftest.py's own unstarted_node_context uses,
    # since only the flush is what this test means to skip.
    first._close_worker_pool()
    first.p2p_manager.peer_db.close()
    first.chainstate.db.close()
    first.block_db.close()
    first.p2p_manager.loop.close()
    first.rpc_manager.loop.close()
    first.logger.close()

    reopened = Node(config)
    reopened.load()
    reopened.status = NodeStatus.HeaderSynced
    # the store opens without error, and reflects only the one flush
    # that actually happened: fewer than all three blocks are durable
    assert len(reopened.chainstate.block_index.active_chain) < len(chain) + 1

    for _ in range(len(chain)):
        update_chain(reopened)
    assert reopened.chainstate.block_index.active_chain[1:] == [
        block.header.hash for block in chain
    ]
    for block in chain:
        assert (
            reopened.chainstate.filter_index.get_filter(block.header.hash) is not None
        )

    reopened._close_worker_pool()
    reopened.p2p_manager.peer_db.close()
    reopened.chainstate.close()
    reopened.block_db.close()
    reopened.p2p_manager.loop.close()
    reopened.rpc_manager.loop.close()
    reopened.logger.close()


def test_a_reorg_restored_prevout_can_be_legitimately_spent_again(node: Node) -> None:
    """A reorg's own restore does not strand a later, heavier fork as invalid.

    `apply_rev_block` restoring a prevout that was durable when the
    block being undone spent it left the outpoint marked in
    `removed_utxos` even after putting it back into `updated_utxo_set` --
    staging now survives across trial boundaries (this is ISS 586's own
    point), so nothing erases that stale flag at the next trial's own
    boundary the way the old, per-trial `finalize` used to. A later,
    legitimate spend of the same output then hit `add_block`'s own
    "prevout already spent in this batch" guard, and
    `update_header_index` invalidated the block carrying it -- and
    everything built on it -- for good, stranding the node on a fork it
    could never leave (btclib-org/btclib-node#586). common is
    COINBASE_MATURITY long so that common[0]'s own coinbase, `funding`,
    is mature and spendable the moment a fork extends it.

    Three forks off `common`'s own tip, each heavier than the last:
    `fork_a` spends `funding` and connects; `fork_b`, two blocks not
    touching `funding`, outweighs it and reorgs it away, restoring
    `funding` -- staged, unflushed; `fork_c`, three blocks whose first
    legitimately re-spends the now-restored `funding`, outweighs
    `fork_b` in turn and has to connect rather than being refused as a
    double spend and invalidated.
    """
    common = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    block_index = connect(node, common)
    funding = common[0].transactions[0]

    fork_a = [
        *common,
        build_block(
            common[-1].header.hash,
            [
                generate_coinbase(height=len(common) + 1),
                generate_random_transaction(funding.id),
            ],
            len(common),
        ),
    ]
    block_index.add_headers([block.header for block in fork_a])
    for block in fork_a:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(fork_a)

    fork_b = [*common, *_extend(common[-1].header.hash, len(common), 2)]
    block_index.add_headers([block.header for block in fork_b])
    for block in fork_b:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)
    assert block_index.active_chain[1:] == hashes(fork_b)

    fork_c_first = build_block(
        common[-1].header.hash,
        [
            generate_coinbase(height=len(common) + 1),
            generate_random_transaction(funding.id),
        ],
        len(common),
    )
    fork_c = [
        *common,
        fork_c_first,
        *_extend(fork_c_first.header.hash, len(common) + 1, 2),
    ]
    block_index.add_headers([block.header for block in fork_c])
    for block in fork_c:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    settle(node)

    assert block_index.active_chain[1:] == hashes(fork_c)
    assert (
        block_index.get_block_info(fork_c_first.header.hash).status
        != BlockStatus.invalid
    )


def an_ibd_node(
    *,
    chainwork: int,
    tip_time: datetime,
    minimum_chain_work: int = 0,
    max_tip_age: int = int(MAX_TIP_AGE.total_seconds()),
    is_initial_block_download: bool = True,
) -> Node:
    """Build a node carrying just what `main.update_ibd_status` reads."""
    tip_hash = b"\x11" * 32
    return cast(
        "Node",
        SimpleNamespace(
            config=SimpleNamespace(
                minimum_chain_work=minimum_chain_work, max_tip_age=max_tip_age
            ),
            chainstate=SimpleNamespace(
                block_index=SimpleNamespace(
                    active_chain=[tip_hash],
                    chainwork={tip_hash: chainwork},
                    header_dict={
                        tip_hash: SimpleNamespace(header=SimpleNamespace(time=tip_time))
                    },
                )
            ),
            is_initial_block_download=is_initial_block_download,
        ),
    )


def test_update_ibd_status_stays_true_below_the_configured_minimum_work() -> None:
    """Below `node.config.minimum_chain_work`, the tip's age is unread."""
    node = an_ibd_node(chainwork=5, minimum_chain_work=10, tip_time=datetime.now(UTC))
    main.update_ibd_status(node)
    assert node.is_initial_block_download is True


def test_update_ibd_status_stays_true_past_the_configured_max_tip_age() -> None:
    """Enough work, but a tip older than `config.max_tip_age`: still IBD."""
    node = an_ibd_node(
        chainwork=10,
        minimum_chain_work=10,
        tip_time=datetime.now(UTC) - MAX_TIP_AGE - timedelta(seconds=1),
    )
    main.update_ibd_status(node)
    assert node.is_initial_block_download is True


def test_update_ibd_status_reads_minimum_chain_work_off_config_not_the_chain() -> None:
    """A `-minimumchainwork` override is read, not the chain's own floor.

    `an_ibd_node`'s `minimum_chain_work` stands in for `-minimumchainwork`:
    `update_ibd_status` no longer reads `node.chain.consensus` at all
    (btclib-org/btclib-node#1500), so a `node.chain` this node has no
    `consensus` on would not be noticed by a test built the old way.
    """
    node = an_ibd_node(
        chainwork=100, minimum_chain_work=1000, tip_time=datetime.now(UTC)
    )
    assert not hasattr(node, "chain")
    main.update_ibd_status(node)
    assert node.is_initial_block_download is True

    caught_up = an_ibd_node(
        chainwork=1000, minimum_chain_work=1000, tip_time=datetime.now(UTC)
    )
    main.update_ibd_status(caught_up)
    assert caught_up.is_initial_block_download is False


def test_update_ibd_status_reads_max_tip_age_off_config_not_the_constant() -> None:
    """A `-maxtipage` shorter than `MAX_TIP_AGE` ends IBD on a tip it stales.

    btclib-org/btclib-node#1474: a tip `MAX_TIP_AGE` would still call
    recent is too old for a `-maxtipage` this much smaller.
    """
    tip_time = datetime.now(UTC) - timedelta(hours=1)
    node = an_ibd_node(
        chainwork=10, minimum_chain_work=10, tip_time=tip_time, max_tip_age=60
    )
    main.update_ibd_status(node)
    assert node.is_initial_block_download is True

    lenient = an_ibd_node(
        chainwork=10,
        minimum_chain_work=10,
        tip_time=tip_time,
        max_tip_age=int(timedelta(hours=2).total_seconds()),
    )
    main.update_ibd_status(lenient)
    assert lenient.is_initial_block_download is False


def test_update_ibd_status_latches_off_and_never_back_as_the_tip_ages() -> None:
    """Both conditions met flips the latch; a stale re-check cannot undo it."""
    node = an_ibd_node(chainwork=10, minimum_chain_work=10, tip_time=datetime.now(UTC))
    main.update_ibd_status(node)
    assert node.is_initial_block_download is False

    # the tip itself never moves in this test; only time passes, past
    # what MAX_TIP_AGE would tolerate on a fresh check -- and
    # update_ibd_status's own first line returns before it reads the
    # tip a second time once the flag is already False
    stale_header = node.chainstate.block_index.header_dict[b"\x11" * 32].header
    stale_header.time = datetime.now(UTC) - MAX_TIP_AGE - timedelta(days=1)
    main.update_ibd_status(node)
    assert node.is_initial_block_download is False


def test_an_unpruned_node_never_calls_prune_up_to(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_prune_chain` is a no-op unless `Config.pruned` is set."""
    node = regtest_node(pruned=False)
    calls: list[int] = []
    monkeypatch.setattr(
        node.block_db, "prune_up_to", lambda target, _hash: calls.append(target)
    )
    connect(
        node, generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    )
    assert calls == []


def test_a_pruned_node_with_no_mib_target_never_calls_prune_up_to(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Config.prune_target_mib` unset is Core's own manual pruning, `-prune=1`.

    Nothing is deleted on its own here; only `rpc.callbacks.prune_blockchain`
    does, matching `node::ApplyArgsManOptions`'s own
    `PRUNE_TARGET_MANUAL` (`node/blockmanager_args.cpp:28-29`, at
    bitcoin/bitcoin@ca7162cde5): `FindFilesToPrune` is never called for a
    manually-pruned chainstate, only `FindFilesToPruneManual` is, and
    only from the RPC.
    """
    node = regtest_node(pruned=True, prune_target_mib=None)
    calls: list[int] = []
    monkeypatch.setattr(
        node.block_db, "prune_up_to", lambda target, _hash: calls.append(target)
    )
    connect(
        node, generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    )
    assert calls == []


def _always_over_target(node: Node, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force `_prune_to_target`'s stop condition to never fire.

    `node.block_db.current_usage` answering a constant far above any
    `prune_target_mib` in this file keeps `_prune_to_target`'s own
    while-loop running for every height `MIN_BLOCKS_TO_KEEP` allows --
    the real chains these tests build are a few kilobytes, orders of
    magnitude under even the smallest MiB target, so this is what
    stands in for a node whose actual disk usage stays over target
    throughout, the case `test_a_pruned_node_stops_once_under_target`
    below is the other side of.
    """
    monkeypatch.setattr(node.block_db, "current_usage", lambda: 2**40)


def test_a_pruned_node_over_target_deletes_past_the_retained_depth_never_further(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pruned node over target keeps only the last `MIN_BLOCKS_TO_KEEP`.

    Core's own retained depth (`constants.MIN_BLOCKS_TO_KEEP`'s own
    citation): a fork replacing the tip's own last `MIN_BLOCKS_TO_KEEP`
    blocks still finds what it needs, which is exactly what an ordinary
    connect through `update_chain` -- one block at a time, this test's
    own shape -- never has reason to reach further back than.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    _always_over_target(node, monkeypatch)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)
    tip_height = len(block_index.active_chain) - 1

    assert node.block_db.pruned_up_to == tip_height - MIN_BLOCKS_TO_KEEP
    oldest_kept = tip_height - MIN_BLOCKS_TO_KEEP + 1
    assert node.block_db.get_block(block_index.active_chain[oldest_kept - 1]) is None
    assert node.block_db.get_block(block_index.active_chain[oldest_kept]) is not None
    assert node.block_db.get_block(block_index.active_chain[-1]) is not None


def test_a_pruned_node_under_target_deletes_nothing(
    regtest_node: Callable[..., Node],
) -> None:
    """A pruned node under its own MiB target the whole time deletes nothing.

    No monkeypatch here: these chains are a few kilobytes, and
    `MIN_PRUNE_TARGET_MIB` (550) is never crossed by them, so this is
    `_prune_to_target`'s own real `block_db.current_usage` read, not a
    forced one.
    """
    node = regtest_node(pruned=True, prune_target_mib=MIN_PRUNE_TARGET_MIB)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)

    assert node.block_db.pruned_up_to == -1
    assert node.block_db.get_block(block_index.active_chain[1]) is not None


def test_a_pruned_node_stops_once_under_target(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_prune_to_target` stops as soon as usage drops under the target.

    Core's own `FindFilesToPrune` breaks out of its own file loop "are
    we below our target?" (`node/blockstorage.cpp:373-375`, at
    bitcoin/bitcoin@ca7162cde5) rather than reaching every file the
    retained-depth bound would otherwise allow; matched here by a
    `current_usage` that answers over target for the first three calls
    and under it from the fourth call on, so pruning takes heights 0, 1
    and 2, genesis being the first, and no further, well short of the
    `MIN_BLOCKS_TO_KEEP` floor this chain's own length would otherwise
    allow.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    usages = iter([2**40, 2**40, 2**40, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    monkeypatch.setattr(node.block_db, "current_usage", lambda: next(usages))
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)

    assert node.block_db.pruned_up_to == 2
    assert node.block_db.get_block(block_index.active_chain[2]) is None
    assert node.block_db.get_block(block_index.active_chain[3]) is not None


def test_a_pruned_node_still_prunes_when_usage_exactly_equals_the_target(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Usage exactly at the target still prunes, matching Core's own `>=`.

    Core's own check is `nCurrentUsage + nBuffer >= target`
    (`node/blockstorage.cpp:373`, at bitcoin/bitcoin@ca7162cde5) --
    inclusive, not `>`, so a node sitting precisely on its own target
    still prunes one more height rather than stopping short of it.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    target_bytes = 1 * 1024 * 1024
    monkeypatch.setattr(node.block_db, "current_usage", lambda: target_bytes)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    connect(node, chain)

    assert node.block_db.pruned_up_to >= 0


def test_a_pruned_node_over_target_leaves_headers_and_the_active_chain_untouched(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning never drops a header, a status, or the active chain itself.

    `BlockIndex` keeps every header this node has ever seen regardless
    of `Config.pruned`, matching Core's own block index -- pruning
    unlinks the file on disk (`node/blockstorage.cpp`'s
    `UnlinkPrunedFiles`, at bitcoin/bitcoin@ca7162cde5), it does not
    drop the `CBlockIndex` entry itself. What that entry's own
    `BLOCK_HAVE_DATA`/`BLOCK_HAVE_UNDO` do on prune is the next test.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    _always_over_target(node, monkeypatch)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)

    assert len(block_index.active_chain) == len(chain) + 1
    for block_hash in block_index.active_chain:
        assert block_index.get_block_info(block_hash) is not None


def test_a_pruned_node_over_target_clears_downloaded_for_every_block_it_deletes(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning clears `BlockInfo.downloaded`, matching Core's own index entry.

    Core's own `BlockManager::PruneOneBlockFile`
    (`node/blockstorage.cpp:270-286`, at bitcoin/bitcoin@ca7162cde5)
    clears `BLOCK_HAVE_DATA`/`BLOCK_HAVE_UNDO` on the `CBlockIndex`
    entry it prunes -- "any block we prune would have to be downloaded
    again in order to consider its chain" -- matched here by
    `BlockInfo.downloaded`, which `prune_up_to_height` clears for the
    same range `block_db.prune_up_to` deletes, genesis (height 0)
    included.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    _always_over_target(node, monkeypatch)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)
    tip_height = len(block_index.active_chain) - 1
    oldest_kept = tip_height - MIN_BLOCKS_TO_KEEP + 1

    for height in range(oldest_kept):
        block_hash = block_index.active_chain[height]
        assert block_index.get_block_info(block_hash).downloaded is False
    for height in range(oldest_kept, tip_height + 1):
        block_hash = block_index.active_chain[height]
        assert block_index.get_block_info(block_hash).downloaded is True


def test_a_fork_longer_than_the_retained_depth_prunes_correctly_on_disk(
    regtest_node: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork connected in one `update_chain` call still persists a clear.

    `_finalize_fork`'s own `to_add` loop is not bounded by
    `MIN_BLOCKS_TO_KEEP` anywhere, and `connect` below marks every
    header downloaded before its first `update_chain` call, so the
    whole `MIN_BLOCKS_TO_KEEP + 5`-block chain connects as one trial,
    one `_finalize_fork_and_prune` call. `get_block_info` reads
    `header_dict`, which a write that never reaches the store updates
    too -- only what actually reaches the store can tell, which is why
    this closes and reopens `Chainstate` rather than reading
    `block_index` again.
    """
    node = regtest_node(pruned=True, prune_target_mib=1)
    _always_over_target(node, monkeypatch)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 5, node.chain.genesis.hash)
    block_index = connect(node, chain)
    tip_height = len(block_index.active_chain) - 1
    oldest_kept = tip_height - MIN_BLOCKS_TO_KEEP + 1
    pruned_hashes = block_index.active_chain[1:oldest_kept]
    kept_hashes = block_index.active_chain[oldest_kept:]
    assert pruned_hashes

    node.chainstate.close()
    reopened = Chainstate(node.data_dir, RegTest(), node.logger)
    for block_hash in pruned_hashes:
        assert reopened.block_index.get_block_info(block_hash).downloaded is False
    for block_hash in kept_hashes:
        assert reopened.block_index.get_block_info(block_hash).downloaded is True
    reopened.close()


class _CrashError(Exception):
    """Stands for the process stopping at the point it is raised."""


def _crash_after(calls: int, original: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap `original` so every call after the first `calls` raises."""
    seen = 0

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal seen
        seen += 1
        if seen > calls:
            raise _CrashError
        return original(*args, **kwargs)

    return wrapper


# RocksDB holds `LOCK` open and creates it afresh on every open, so an
# image without it is what a restart finds; Windows refuses to copy it.
_WITHOUT_LOCK = shutil.ignore_patterns("LOCK")


def _assert_restartable(image: Path, chain: list[bytes]) -> int:
    """Open a crashed data directory and check no block it needs is gone.

    Needed are the blocks above the chainstate's own tip, which a restart
    connects again from `BlockDB`, and every block the index still marks
    `downloaded`. Returns the height the chainstate restarts from.
    """
    logger = Logger(debug=False)
    chainstate = Chainstate(image, RegTest(), logger)
    block_db = BlockDB(image, logger)
    try:
        height = len(chainstate.block_index.active_chain) - 1
        for block_hash in chain[height:]:
            assert block_db.get_block(block_hash) is not None
        for block_hash, info in chainstate.block_index.header_dict.items():
            assert not info.downloaded or block_db.get_block(block_hash) is not None
    finally:
        chainstate.close()
        block_db.close()
    return height


_CRASH_POINTS = {
    "before the flush": lambda node, patch: patch(node.chainstate, "flush", 0),
    "before the first flag is cleared": lambda node, patch: patch(
        node.chainstate.block_index, "set_downloaded", 0
    ),
    "between two flags": lambda node, patch: patch(
        node.chainstate.block_index, "set_downloaded", 3
    ),
    "before the delete": lambda node, patch: patch(node.block_db, "prune_up_to", 0),
    "between two deletes": lambda node, patch: patch(node.block_db.db, "delete", 3),
}


def _a_pruned_chain(
    regtest_node: Callable[..., Node],
) -> tuple[Node, list[bytes], int]:
    """Connect a chain with nothing flushed: the bound is never reached."""
    node = regtest_node(pruned=True, prune_target_mib=None)
    chain = generate_random_chain(MIN_BLOCKS_TO_KEEP + 20, node.chain.genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        node.block_db.add_block(block)
    for _ in chain:
        update_chain(node)
    return (
        node,
        [block.header.hash for block in chain],
        len(block_index.active_chain) - 1,
    )


def test_pruning_flushes_the_chainstate_before_it_deletes(
    regtest_node: Callable[..., Node], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """ISS 1248: a crash right after a prune restarts from the tip that pruned.

    Nothing flushed the chainstate before, so its on-disk tip was behind
    the blocks the prune had deleted.
    """
    node, chain, tip_height = _a_pruned_chain(regtest_node)
    prune_up_to_height(node, 19)
    image = tmp_path_factory.mktemp("image") / "data"
    shutil.copytree(node.data_dir, image, ignore=_WITHOUT_LOCK)
    assert _assert_restartable(image, chain) == tip_height


@pytest.mark.parametrize("point", _CRASH_POINTS)
def test_a_crash_while_pruning_leaves_nothing_a_restart_needs_missing(
    point: str,
    regtest_node: Callable[..., Node],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1248: the data directory a stop at each step leaves restarts."""
    node, chain, _ = _a_pruned_chain(regtest_node)

    def patch(target: object, name: str, calls: int) -> None:
        monkeypatch.setattr(target, name, _crash_after(calls, getattr(target, name)))

    _CRASH_POINTS[point](node, patch)
    with pytest.raises(_CrashError):
        prune_up_to_height(node, 19)
    image = tmp_path_factory.mktemp("image") / "data"
    shutil.copytree(node.data_dir, image, ignore=_WITHOUT_LOCK)
    _assert_restartable(image, chain)


_ROOT = Path(__file__).parents[2]

# a child that connects a chain, then kills itself with SIGKILL at the
# third key `BlockDB` deletes: nothing closes, nothing is flushed but
# what `prune_up_to_height` itself flushed
_KILLED_WHILE_PRUNING = """
import os, signal, sys
from pathlib import Path
from btclib_node.constants import NodeStatus
from btclib_node.main import prune_up_to_height
from tests.conftest import unstarted_node_context
from tests.unit.main_test import _a_pruned_chain

with unstarted_node_context(Path(sys.argv[1]), pruned=True) as node:
    node.status = NodeStatus.HeaderSynced
    _, chain, _ = _a_pruned_chain(lambda **_: node)
    Path(sys.argv[2]).write_text(" ".join(h.hex() for h in chain))
    deleted = []
    delete = node.block_db.db.delete

    def die_at_the_third(key):
        deleted.append(key)
        if len(deleted) == 3:
            os.kill(os.getpid(), signal.SIGKILL)
        delete(key)

    node.block_db.db.delete = die_at_the_third
    prune_up_to_height(node, 19)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGKILL")
def test_a_process_killed_while_pruning_restarts(tmp_path: Path) -> None:
    """ISS 1248: the same check, after a real SIGKILL rather than a raise."""
    hashes = tmp_path / "hashes"
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            _KILLED_WHILE_PRUNING,
            str(tmp_path / "data"),
            str(hashes),
        ],
        cwd=_ROOT,
        env={**os.environ, "PYTHONPATH": str(_ROOT)},
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == -signal.SIGKILL, completed.stderr
    chain = [bytes.fromhex(h) for h in hashes.read_text().split()]
    assert _assert_restartable(tmp_path / "data" / "regtest", chain) == len(chain)


def test_a_block_connected_stamps_the_last_tip_update(node: Node) -> None:
    """ISS 1100: Core's `BlockConnected` stamps `m_last_tip_update`.

    What `DownloadManager` reads a stale tip off: zero until a block
    connects, the time it did after.
    """
    assert node.download_manager.last_tip_update == 0
    before = time.time()
    connect(node, generate_random_chain(1, RegTest().genesis.hash))
    assert before <= node.download_manager.last_tip_update <= time.time()


def a_block_over(transactions: list[Tx], committed: list[Tx] | None = None) -> Block:
    """Build a block of `transactions` whose header commits to `committed`.

    `committed` defaults to `transactions` themselves, the honest case.
    """
    header = build_block(RegTest().genesis.hash, committed or transactions, 0).header
    return Block(header, transactions, check_validity=False)


def a_64_byte_transaction() -> Tx:
    """Build a transaction serializing to 64 bytes without its witness."""
    tx = Tx(
        vin=[TxIn(OutPoint(b"\x01" * 32, 0), b"\x51" * 4, 0xFFFFFFFF)],
        vout=[TxOut(1, b"")],
        check_validity=False,
    )
    assert len(tx.serialize(include_witness=False, check_validity=False)) == 64
    return tx


@pytest.mark.parametrize("check_witness_root", [True, False])
def test_a_block_as_mined_is_not_mutated(check_witness_root: bool) -> None:  # noqa: FBT001
    """ISS 1242: a body its header commits to, no witness, is not mutated."""
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    assert not main.is_block_mutated(block, check_witness_root=check_witness_root)


def test_a_body_the_merkle_root_does_not_match_is_mutated() -> None:
    """ISS 1242: Core's `bad-txnmrklroot`."""
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    forged = a_block_over([generate_coinbase(value=1, height=1)], block.transactions)
    assert main.is_block_mutated(forged, check_witness_root=True)


def test_a_body_repeating_its_last_transaction_is_mutated() -> None:
    """ISS 1242: CVE-2012-2459, one merkle root over one more transaction."""
    txs = [
        generate_coinbase(height=1),
        *(generate_random_transaction() for _ in range(2)),
    ]
    repeated = a_block_over([*txs, txs[-1]], txs)
    assert repeated.header.merkle_root == a_block_over(txs).header.merkle_root
    assert main.is_block_mutated(repeated, check_witness_root=True)


@pytest.mark.parametrize(
    ("root", "mutated"),
    [(bytes(32), False), (b"\x01" * 32, True)],
    ids=["null-root", "other-root"],
)
def test_an_empty_body_is_mutated_unless_the_root_is_null(
    root: bytes,
    mutated: bool,  # noqa: FBT001
) -> None:
    """ISS 1242: Core's merkle root of no transactions is the null hash."""
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    header = replace(block.header, merkle_root=root)
    empty = Block(header, [], check_validity=False)
    assert main.is_block_mutated(empty, check_witness_root=True) is mutated


@pytest.mark.parametrize("sixty_four", [True, False])
def test_a_body_without_a_coinbase_is_mutated_by_a_64_byte_transaction(
    sixty_four: bool,  # noqa: FBT001
) -> None:
    """ISS 1242: a 64-byte transaction could be an inner merkle node."""
    tx = a_64_byte_transaction() if sixty_four else generate_random_transaction()
    no_coinbase = a_block_over([tx])
    assert main.is_block_mutated(no_coinbase, check_witness_root=True) is sixty_four


def test_a_witness_the_coinbase_commits_to_is_not_mutated_under_segwit() -> None:
    """ISS 1242: a commitment matching the witnesses, and a 32-byte nonce."""
    assert not main.is_block_mutated(generate_segwit_block(), check_witness_root=True)


def test_a_witness_before_segwit_is_mutated_whatever_it_commits_to() -> None:
    """ISS 1242: no commitment is read, so any witness is unexpected."""
    assert main.is_block_mutated(generate_segwit_block(), check_witness_root=False)


def test_a_witness_with_no_commitment_is_mutated() -> None:
    """ISS 1242: Core's `unexpected-witness`."""
    spend = generate_random_transaction()
    spend.vin[0].script_witness = Witness([b"\x01" * 3])
    block = a_block_over([generate_coinbase(height=1), spend])
    assert main.is_block_mutated(block, check_witness_root=True)


@pytest.mark.parametrize("stack", [[], [bytes(32), b""]], ids=["none", "two"])
def test_a_witness_nonce_not_of_one_element_is_mutated(stack: list[bytes]) -> None:
    """ISS 1242: Core's `bad-witness-nonce-size`, on the element count."""
    block = generate_segwit_block()
    block.transactions[0].vin[0].script_witness = Witness(stack)
    assert main.is_block_mutated(block, check_witness_root=True)


def test_a_witness_nonce_not_of_32_bytes_is_mutated_though_it_matches() -> None:
    """ISS 1242: Core's `bad-witness-nonce-size`, its commitment holding."""
    block = generate_segwit_block(nonce=bytes(31))
    assert main.is_block_mutated(block, check_witness_root=True)


def test_a_witness_the_commitment_does_not_match_is_mutated() -> None:
    """ISS 1242: Core's `bad-witness-merkle-match`, a witness swapped after."""
    block = generate_segwit_block()
    block.transactions[1].vin[0].script_witness = Witness([b"\x02" * 3])
    assert main.is_block_mutated(block, check_witness_root=True)


def a_block_of_weight(weight: int, *extra: Tx) -> Block:
    """Build a segwit block whose weight is exactly `weight`, near the bound.

    `generate_segwit_block`'s own, `extra` after the spend. The spend's
    witness takes up the difference: a witness byte weighs one, and the
    length prefix of an element this long is five bytes either side of
    the adjustment.
    """
    witness = bytes(weight)
    block = generate_segwit_block(*extra, witness=witness)
    block = generate_segwit_block(
        *extra, witness=bytes(len(witness) + weight - block.weight)
    )
    assert block.weight == weight
    return block


def test_a_committed_body_over_the_weight_is_marked_failed() -> None:
    """ISS 1242: Core's `bad-blk-weight`, a `ContextualCheckBlock` rule."""
    over = a_block_of_weight(MAX_BLOCK_WEIGHT + 1)
    assert main.is_block_failed(over, check_witness_root=True)


def test_a_committed_body_at_the_weight_is_not_marked_failed() -> None:
    """ISS 1242: the bound is inclusive."""
    at = a_block_of_weight(MAX_BLOCK_WEIGHT)
    assert not main.is_block_failed(at, check_witness_root=True)


def test_a_body_over_the_weight_on_a_witness_it_does_not_commit_to_is_not_failed() -> (
    None
):
    """ISS 1242: Core asks the commitment first, so the weight is no one's."""
    over = a_block_of_weight(MAX_BLOCK_WEIGHT + 1)
    stuffed = over.transactions[1].vin[0].script_witness.stack[0]
    over.transactions[1].vin[0].script_witness = Witness([b"\x01" * len(stuffed)])
    assert over.weight > MAX_BLOCK_WEIGHT
    assert not main.is_block_failed(over, check_witness_root=True)


def a_spend_paying(script_pub_key: bytes) -> Tx:
    """Build a valid transaction whose one output is `script_pub_key`."""
    tx = generate_random_transaction()
    tx.vout[0] = TxOut(tx.vout[0].value, script_pub_key)
    return tx


def a_spend_twice_of_one_outpoint() -> Tx:
    """Build a spend of one outpoint twice, `bad-txns-inputs-duplicate`."""
    tx = generate_random_transaction()
    tx.vin = [tx.vin[0], tx.vin[0]]
    return tx


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        # three outputs of 400,000 bytes: 1.2 MB stripped, each transaction
        # well under the bound on its own
        (
            lambda: [a_spend_paying(bytes(400_000)) for _ in range(3)],
            "invalid stripped size",
        ),
        (lambda: [generate_coinbase(height=1)], "more than one coinbase"),
        (lambda: [a_spend_twice_of_one_outpoint()], "spent twice"),
        # OP_CHECKSIG, one legacy sigop a byte
        (lambda: [a_spend_paying(b"\xac" * 20_001)], "invalid sigop cost"),
    ],
    ids=["bad-blk-length", "bad-cb-multiple", "tx", "bad-blk-sigops"],
)
def test_a_committed_body_over_the_weight_failing_check_block_is_not_failed(
    extra: Callable[[], list[Tx]], error: str
) -> None:
    """ISS 1333: Core's `ProcessNewBlock` never marks a `CheckBlock` failure.

    Each body is over the weight too, which alone would mark it.
    """
    over = a_block_of_weight(MAX_BLOCK_WEIGHT + 1_000_000, *extra())
    assert over.weight > MAX_BLOCK_WEIGHT
    with pytest.raises(BTClibValueError, match=error):
        over.assert_valid(RegTest().pow_limit_bits)
    assert not main.is_block_mutated(over, check_witness_root=True)
    assert not main.is_block_failed(over, check_witness_root=True)


def test_a_body_over_the_weight_with_no_coinbase_is_not_failed() -> None:
    """ISS 1333: Core's `bad-cb-missing`, the body not mutated.

    Core's `IsBlockMutated` reads no witness of a block without a
    coinbase, so a witness is what puts this one over the weight.
    """
    spend = generate_random_transaction()
    spend.vin[0].script_witness = Witness([bytes(MAX_BLOCK_WEIGHT)])
    block = a_block_over([spend])
    assert block.weight > MAX_BLOCK_WEIGHT
    assert not main.is_block_mutated(block, check_witness_root=True)
    assert not main.is_block_failed(block, check_witness_root=True)


def a_fast_peer(
    sent: list[Any],
    availability: BlockAvailability,
    *,
    high_bandwidth: bool = True,
    version: int = 70016,
    status: P2pConnStatus = P2pConnStatus.Connected,
) -> Connection:
    """Build a connection double for `new_pow_valid_block`, recording sends."""
    return cast(
        "Connection",
        SimpleNamespace(
            status=status,
            version_message=SimpleNamespace(version=version),
            prefers_headers=True,
            requested_hb_cmpctblocks=high_bandwidth,
            send=sent.append,
            block_availability=availability,
        ),
    )


@pytest.mark.parametrize(
    "case",
    [
        "announced",
        "at-no-ban-version",
        "at-segwit",
        "ibd",
        "not-on-tip",
        "low-bandwidth",
        "no-parent",
        "has-it",
        "old-version",
        "closed",
        "announced-before",
        "pre-segwit",
        "bad-cb-height",
    ],
)
def test_a_new_block_is_sent_to_a_high_bandwidth_peer_before_connecting(
    node: Node, case: str
) -> None:
    """ISS 1315: Core's `NewPoWValidBlock`, and each condition before it.

    A new block extending the tip, out of initial block download and
    past `ContextualCheckBlock`, goes as a `cmpctblock` to a fully
    connected peer from `INVALID_CB_NO_BAN_VERSION` up that asked for
    high bandwidth, has the parent and lacks the block. `NewPoWValidBlock`
    goes no lower than the highest block it already sent this way, nor
    below segwit's height, and records the height before the segwit check.
    """
    chain = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, chain[:1])
    block_index = node.chainstate.block_index
    node.is_initial_block_download = case == "ibd"
    block = chain[1]
    if case == "not-on-tip":
        block = generate_random_chain(1, RegTest().genesis.hash)[0]
    elif case == "bad-cb-height":
        block = build_block(chain[0].header.hash, [generate_coinbase(height=5)], 2)
    elif case == "announced-before":
        node.highest_fast_announce = 2
    elif case in ("pre-segwit", "at-segwit"):
        segwit_height = 3 if case == "pre-segwit" else 2
        consensus = replace(node.chain.consensus, segwit_height=segwit_height)
        node.chain = cast("Any", SimpleNamespace(consensus=consensus))
    block_index.add_headers([block.header])
    best_known = {"no-parent": None, "has-it": block.header.hash}.get(
        case, chain[0].header.hash
    )
    sent: list[Any] = []
    node.p2p_manager.connections[1] = a_fast_peer(
        sent,
        BlockAvailability(best_known=best_known),
        high_bandwidth=case != "low-bandwidth",
        version={"old-version": 70014, "at-no-ban-version": 70015}.get(case, 70016),
        status=P2pConnStatus.Closed if case == "closed" else P2pConnStatus.Connected,
    )

    main.new_pow_valid_block(node, block)

    if case in ("announced", "at-no-ban-version", "at-segwit"):
        (message,) = sent
        assert isinstance(message, CmpctBlock)
        assert message.serialize() == compact_block(block, message.nonce).serialize()
        peer = node.p2p_manager.connections[1]
        assert peer.block_availability.best_header_sent == block.header.hash
    else:
        assert not sent
    recorded = case not in ("ibd", "not-on-tip", "bad-cb-height")
    assert node.highest_fast_announce == (2 if recorded else 0)
    # kept past every gate, announced to a peer or not, as Core's
    # `NewPoWValidBlock` stores it ahead of its `ForEachNode`
    kept = recorded and case not in ("announced-before", "pre-segwit")
    recent = node.most_recent_block
    assert (recent is not None) == kept
    if recent is not None:
        assert recent.block == block
        assert recent.hash == block.header.hash


def test_a_block_sent_before_connecting_is_not_announced_again(node: Node) -> None:
    """ISS 1315: the peer counts as having it, as `PeerHasHeader` answers.

    A peer that did not ask for high bandwidth hears of the block once
    it is connected, as before.
    """
    chain = generate_random_chain(2, RegTest().genesis.hash, tip_time=datetime.now(UTC))
    connect(node, chain[:1])
    node.is_initial_block_download = False
    fast: list[Any] = []
    slow: list[Any] = []
    parent = chain[0].header.hash
    node.p2p_manager.connections[1] = a_fast_peer(
        fast, BlockAvailability(best_known=parent)
    )
    node.p2p_manager.connections[2] = a_fast_peer(
        slow, BlockAvailability(best_known=parent), high_bandwidth=False
    )
    node.chainstate.block_index.add_headers([chain[1].header])
    main.new_pow_valid_block(node, chain[1])
    assert len(fast) == 1
    assert not slow

    connect(node, chain[1:])
    assert node.chainstate.block_index.active_chain[-1] == chain[1].header.hash
    assert len(fast) == 1
    (message,) = slow
    assert isinstance(message, Headers)


def test_every_high_bandwidth_peer_is_sent_one_and_the_same_cmpctblock(
    node: Node,
) -> None:
    """ISS 1315: Core's `NewPoWValidBlock` builds one `pcmpctblock` for all."""
    chain = generate_random_chain(2, RegTest().genesis.hash)
    connect(node, chain[:1])
    node.is_initial_block_download = False
    node.chainstate.block_index.add_headers([chain[1].header])
    sent: dict[int, list[Any]] = {1: [], 2: []}
    for conn_id, messages in sent.items():
        node.p2p_manager.connections[conn_id] = a_fast_peer(
            messages, BlockAvailability(best_known=chain[0].header.hash)
        )
    main.new_pow_valid_block(node, chain[1])
    (first,) = sent[1]
    assert sent[2] == [first]


def test_the_cmpctblock_sent_before_connecting_is_the_one_sent_after(
    node: Node,
) -> None:
    """ISS 1336: `SendMessages` sends `m_most_recent_compact_block` as it is.

    A high-bandwidth peer that lacked the parent when the block arrived
    hears of it once it is connected, under the nonce the others got.
    """
    chain = generate_random_chain(2, RegTest().genesis.hash, tip_time=datetime.now(UTC))
    connect(node, chain[:1])
    node.is_initial_block_download = False
    parent = chain[0].header.hash
    early: list[Any] = []
    late: list[Any] = []
    node.p2p_manager.connections[1] = a_fast_peer(
        early, BlockAvailability(best_known=parent)
    )
    behind = BlockAvailability(best_known=None)
    node.p2p_manager.connections[2] = a_fast_peer(late, behind)
    node.chainstate.block_index.add_headers([chain[1].header])
    main.new_pow_valid_block(node, chain[1])
    (first,) = early
    assert not late

    behind.best_known = parent
    connect(node, chain[1:])
    assert late == [first]


def test_a_cmpctblock_of_another_block_than_the_most_recent_is_built_again(
    node: Node,
) -> None:
    """ISS 1336: Core builds one where `m_most_recent_block_hash` differs."""
    chain = generate_random_chain(2, RegTest().genesis.hash, tip_time=datetime.now(UTC))
    connect(node, chain[:1])
    node.is_initial_block_download = False
    stale = compact_block(chain[0], 99)
    node.most_recent_block = MostRecentBlock(chain[0], stale)
    sent: list[Any] = []
    node.p2p_manager.connections[1] = a_peer(
        sent, BlockAvailability(best_known=chain[0].header.hash), high_bandwidth=True
    )
    connect(node, chain[1:])
    (message,) = sent
    assert isinstance(message, CmpctBlock)
    assert message.header.hash == chain[1].header.hash


def test_a_block_whose_header_is_unknown_or_valid_is_not_cached_invalid(
    node: Node,
) -> None:
    """ISS 1344: `duplicate-invalid` asks for a header indexed and invalid."""
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    assert not main.is_cached_invalid(block_index, block)
    block_index.add_headers([block.header])
    assert not main.is_cached_invalid(block_index, block)


def test_a_body_passing_check_block_under_an_invalid_header_is_cached_invalid(
    node: Node,
) -> None:
    """ISS 1344: Core's `AcceptBlockHeader` answers it `duplicate-invalid`.

    Over the weight too: Core asks the weight after the header.
    """
    over = a_block_of_weight(MAX_BLOCK_WEIGHT + 1)
    block_index = node.chainstate.block_index
    block_index.add_headers([over.header])
    block_index.invalidate(over.header.hash)
    assert main.is_cached_invalid(block_index, over)


@pytest.mark.parametrize("failure", ["merkle-root", "bad-cb-multiple"])
def test_a_body_failing_check_block_under_an_invalid_header_is_not_cached_invalid(
    node: Node, failure: str
) -> None:
    """ISS 1344: Core's `CheckBlock` comes first, and answers for itself."""
    honest = generate_segwit_block(generate_coinbase(height=1))
    block = (
        a_block_over([generate_coinbase(value=1, height=1)], honest.transactions)
        if failure == "merkle-root"
        else honest
    )
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header])
    block_index.invalidate(block.header.hash)
    assert not main.is_cached_invalid(block_index, block)
