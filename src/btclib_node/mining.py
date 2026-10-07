# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Block assembly, proof of work, and the validity check that stores nothing.

Core's `BlockAssembler::CreateNewBlock` (`src/node/miner.cpp`), the
nonce search of `GenerateBlock` (`src/rpc/mining.cpp`) and
`TestBlockValidity` (`src/validation.cpp`), at bitcoin/bitcoin@9be056a8a7,
the v31.1 tag. `rpc.mining` is the RPC surface over them.

Everything here runs on `Node`'s own thread, `ARCHITECTURE.md`'s *The
loop*, which is what lets it read the mempool and the staged UTXO set
without a lock. `solve_block` searches `_NONCE_CHUNK` nonces at a time
and hands the thread back to the loop between chunks, where Core
searches on an RPC thread of its own.

Where this module differs from Core:

- Core 31's `addChunks` takes a cluster mempool's chunks in feerate
  order. This mempool has no clusters (btclib-org/btclib-node#1383,
  btclib-org/btclib-node#1499), so `_PackageSelector` takes ancestor
  packages by feerate, the order Core used before them. The block is
  valid either way; the fee it collects can differ.
- The weight limit and `_minimum_time` follow Core's `master` at
  bitcoin/bitcoin@aef8a04966, not v31.1. The weight limit compares a
  package's real weight (bitcoin/bitcoin#35580), and the sigop limit
  is checked on its own. `_minimum_time` holds the last block of a
  difficulty period to the time of its first, BIP54's rule, on every
  network (bitcoin/bitcoin#35949).
- `-blockmaxweight`, `-blockmintxfee`, `-blockreservedweight` and
  `-printpriority` are not options of this node: the block limits are
  Core's defaults, and a package under `DEFAULT_BLOCK_MIN_TX_FEE`, 1
  sat/kvB, is left out as Core's `addChunks` stops at it.
- A reject reason is Core's `GetRejectReason`, the word BIP22 answers
  and never the debug text after it: `TxRejectedError.reason` for what
  connecting refuses, and Core's word where `_check_block`,
  `_contextual_header_reason`, `_witness_reason` and `_CONNECT_REASONS`
  have one. The rest is the exception's own text, as `submitblock`
  answers.
"""

from __future__ import annotations

import heapq
import re
import time
from datetime import UTC, datetime
from fractions import Fraction
from typing import TYPE_CHECKING, NamedTuple

from btclib.block import (
    Block,
    BlockHeader,
    assert_not_timewarp,
    coinbase_witness_commitment,
    median_time_past,
    next_bits_required,
    witness_commitment_output,
)
from btclib.block.block import (
    bip34_commitment,
    merkle_root_and_mutated_from_transactions,
)
from btclib.block.limits import MAX_BLOCK_SIGOPS_COST, MAX_TIMEWARP
from btclib.block.mining import NONCE_SPACE, mine
from btclib.consensus import MAX_BLOCK_WEIGHT, subsidy
from btclib.exceptions import BTClibException, BTClibValueError
from btclib.script.engine import sig_op_cost
from btclib.script.witness import Witness
from btclib.tx import OutPoint, Tx, TxIn, TxOut
from btclib.tx.tx_context import is_final

from btclib_node.chainstate.block_index import block_time
from btclib_node.exceptions import (
    InvalidBlockInputError,
    PrevoutCountMismatchError,
    TxRejectedError,
)
from btclib_node.interpreter import STANDARD_FLAGS
from btclib_node.main import (
    contextual_check_block,
    new_pow_valid_block,
    parent_lookup,
    try_connect_block,
    update_chain,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from btclib.block.header_context import ParentOf
    from btclib.script.script_pub_key import ScriptPubKey

    from btclib_node import Node

__all__ = [
    "DEFAULT_MAX_TRIES",
    "BlockTemplate",
    "TemplateError",
    "accept_block",
    "block_with_transactions",
    "check_block_validity",
    "create_new_block",
    "solve_block",
]

# `DEFAULT_MAX_TRIES` (`src/rpc/mining.cpp`): the nonces `generatetoaddress`
# and `generateblock` try before giving up
DEFAULT_MAX_TRIES = 1_000_000

# `DEFAULT_BLOCK_RESERVED_WEIGHT` and
# `DEFAULT_COINBASE_OUTPUT_MAX_ADDITIONAL_SIGOPS` (`src/policy/policy.h`):
# what the coinbase and the header are allowed for before any transaction
# is chosen
_RESERVED_WEIGHT = 8000
_RESERVED_SIGOPS = 400

# `CTransaction::CURRENT_VERSION`, the version of the coinbase Core builds
_COINBASE_VERSION = 2

# `CTxIn::MAX_SEQUENCE_NONFINAL`, "make sure timelock is enforced"
_COINBASE_SEQUENCE = 0xFFFFFFFE

# the witness reserved value `GenerateCoinbaseCommitment` writes
_WITNESS_NONCE = bytes(32)

# `DEFAULT_BLOCK_MIN_TX_FEE`, 1 sat/kvB, as satoshi per virtual byte
_MIN_FEERATE = Fraction(1, 1000)

# nonces `solve_block` tries in one step. A pass of the loop runs one step
# of each search in progress, so it grows by one chunk per search, at most
# `RPC_THREADS` of them (`constants.RPC_THREADS`).
_NONCE_CHUNK = 1 << 14

# the connect-time checks that raise no `TxRejectedError`, and Core's reason
# for each
_CONNECT_REASONS = {
    "prevout not found": "bad-txns-inputs-missingorspent",
    "prevout already spent in this batch": "bad-txns-inputs-missingorspent",
}

_REASON = re.compile(r"[a-z0-9-]+")

# the least header version once each of BIP34, BIP66 and BIP65 binds
# (`ContextualCheckBlockHeader`), with the `ConsensusParams` field
# holding the height it binds from
_MIN_VERSIONS = ((2, "bip34_height"), (3, "bip66_height"), (4, "bip65_height"))


class TemplateError(Exception):
    """The block `create_new_block` built was refused, as Core's is."""

    def __init__(self, reason: str) -> None:
        """Give Core's message for the `reason` the block was refused with."""
        super().__init__(f"TestBlockValidity failed: {reason}")


class BlockTemplate(NamedTuple):
    """An unsolved block, with what `getblocktemplate` reports beside it.

    `fees` and `sigops` are per transaction after the coinbase, in
    block order; `sigops` is the cost counted for block limits.
    `min_time` is `GetMinimumTime`.
    """

    block: Block
    fees: list[int]
    sigops: list[int]
    min_time: int


class _Chosen(NamedTuple):
    tx: Tx
    fee: int
    sigops: int


def _minimum_time(
    tip_header: BlockHeader, height: int, tip_mtp: int, node: Node
) -> int:
    """Return `GetMinimumTime`: past the median time, and two bounds.

    A period's first block is held to BIP94's timewarp bound and its last
    to BIP54's Murch-Zawy bound, the time of the period's first block.
    Core applies both to templates on every network. Only BIP94's is
    consensus here, and only on testnet4.
    """
    interval = node.chain.consensus.difficulty_adjustment_interval
    min_time = tip_mtp + 1
    if height % interval == 0:
        min_time = max(min_time, block_time(tip_header) - MAX_TIMEWARP)
    if height % interval == interval - 1:
        block_index = node.chainstate.block_index
        first = block_index.header_dict[block_index.active_chain[height - interval + 1]]
        min_time = max(min_time, block_time(first.header))
    return min_time


def _prev_outputs(node: Node, tx: Tx) -> list[TxOut]:
    """Return the outputs `tx` spends, from the UTXO set or a mempool parent."""
    outputs: list[TxOut] = []
    for tx_in in tx.vin:
        prev_out = tx_in.prev_out
        coin = node.chainstate.utxo_index.get_coin(
            prev_out.serialize(check_validity=False)
        )
        if coin is not None:
            outputs.append(coin.tx_out)
            continue
        parent = node.mempool.get_tx(prev_out.tx_id)
        # a mempool entry's inputs were found when it was accepted, and
        # nothing since has removed what they spend without removing it
        assert parent is not None  # noqa: S101
        outputs.append(parent.vout[prev_out.vout])
    return outputs


class _PackageSelector:
    """Choose mempool transactions for a block at `height`, best packages first.

    Core's `addPackageTxs` before cluster mempool: the package of a
    transaction is itself and the ancestors not yet in the block, ranked
    by the feerate of the package. A package that does not fit the
    weight or sigop limit, or holds a transaction not final at `height`,
    is left out, and the packages of the descendants of what goes in are
    ranked again.
    """

    def __init__(self, node: Node, height: int, lock_time_cutoff: int) -> None:
        self.node = node
        self.height = height
        self.lock_time_cutoff = lock_time_cutoff
        self.pool = node.mempool
        self.ancestors: dict[bytes, frozenset[bytes]] = {}
        self.sigops: dict[bytes, int] = {}
        self.in_block: set[bytes] = set()
        self.failed: set[bytes] = set()
        # the rank a heap entry was pushed with is current only while it
        # equals the wtxid's `revision`
        self.revision: dict[bytes, int] = {}
        self.heap: list[tuple[Fraction, int, bytes, int]] = []
        self.chosen: list[_Chosen] = []
        self.weight = _RESERVED_WEIGHT
        self.cost = _RESERVED_SIGOPS

    def _ancestors_of(self, wtxid: bytes) -> frozenset[bytes]:
        if wtxid not in self.ancestors:
            found: set[bytes] = set()
            for tx_in in self.pool.transactions[wtxid].vin:
                parent = self.pool.txid_index.get(tx_in.prev_out.tx_id)
                if parent is not None:
                    found.add(parent)
                    found |= self._ancestors_of(parent)
            self.ancestors[wtxid] = frozenset(found)
        return self.ancestors[wtxid]

    def _sigops_of(self, wtxid: bytes) -> int:
        if wtxid not in self.sigops:
            tx = self.pool.transactions[wtxid]
            self.sigops[wtxid] = sig_op_cost(
                _prev_outputs(self.node, tx), tx, STANDARD_FLAGS
            )
        return self.sigops[wtxid]

    def _package(self, wtxid: bytes) -> list[bytes]:
        """Return `wtxid` and its ancestors not in the block, parents first."""
        ancestors = self._ancestors_of(wtxid) - self.in_block
        return [*sorted(ancestors, key=lambda w: len(self._ancestors_of(w))), wtxid]

    def _push(self, wtxid: bytes) -> None:
        package = self._package(wtxid)
        fee = sum(self.pool.fees[w] for w in package)
        size = sum(self.pool.vsizes[w] for w in package)
        self.revision[wtxid] = self.revision.get(wtxid, 0) + 1
        heapq.heappush(
            self.heap,
            (-Fraction(fee, size), len(self.heap), wtxid, self.revision[wtxid]),
        )

    def _fits(self, package: list[bytes]) -> bool:
        transactions = self.pool.transactions
        return (
            self.weight + sum(transactions[w].weight for w in package)
            < MAX_BLOCK_WEIGHT
            and self.cost + sum(self._sigops_of(w) for w in package)
            < MAX_BLOCK_SIGOPS_COST
            and all(
                is_final(transactions[w], self.height, self.lock_time_cutoff)
                for w in package
            )
        )

    def _include(self, package: list[bytes]) -> None:
        for wtxid in package:
            tx = self.pool.transactions[wtxid]
            self.chosen.append(
                _Chosen(tx, self.pool.fees[wtxid], self._sigops_of(wtxid))
            )
            self.weight += tx.weight
            self.cost += self._sigops_of(wtxid)
            self.in_block.add(wtxid)

    def _children(self, wtxid: bytes) -> set[bytes]:
        return self.pool.spent_by.get(self.pool.transactions[wtxid].id, set())

    def _rank_descendants(self, package: list[bytes]) -> None:
        pending = [child for wtxid in package for child in self._children(wtxid)]
        ranked: set[bytes] = set()
        while pending:
            child = pending.pop()
            if child not in self.in_block and child not in ranked:
                ranked.add(child)
                self._push(child)
                pending.extend(self._children(child))

    def select(self) -> list[_Chosen]:
        """Return the chosen transactions in block order."""
        for wtxid in list(self.pool.transactions):
            self._push(wtxid)
        while self.heap:
            negative_rate, _, wtxid, revision = heapq.heappop(self.heap)
            if (
                wtxid in self.in_block
                or wtxid in self.failed
                or revision != self.revision[wtxid]
            ):
                continue
            if -negative_rate < _MIN_FEERATE:
                # everything left pays less
                break
            package = self._package(wtxid)
            if not self._fits(package):
                self.failed.add(wtxid)
                continue
            self._include(package)
            self._rank_descendants(package)
        return self.chosen


def _assemble(
    node: Node, script_pub_key: ScriptPubKey, chosen: list[_Chosen]
) -> BlockTemplate:
    """Build the unsolved block over `chosen`, paying `script_pub_key`.

    The coinbase, its commitment and the header as Core's `CreateNewBlock`
    fills them (`src/node/miner.cpp`), the version as `ComputeBlockVersion`.
    """
    block_index = node.chainstate.block_index
    consensus = node.chain.consensus
    tip_hash = block_index.active_chain[-1]
    tip_header = block_index.header_dict[tip_hash].header
    tip_height = len(block_index.active_chain) - 1
    height = tip_height + 1
    parent_of = parent_lookup(node)
    tip_mtp = median_time_past(tip_header, tip_height, parent_of)
    min_time = _minimum_time(tip_header, height, tip_mtp, node)

    fees = sum(c.fee for c in chosen)
    # the extra push is `include_dummy_extranonce`, which keeps the
    # script_sig of a height up to 16 at the two bytes `bad-cb-length` asks
    script_sig = bip34_commitment(height) + b"\x00"
    transactions = [
        Tx(
            version=_COINBASE_VERSION,
            lock_time=height - 1,
            vin=[
                TxIn(OutPoint(), script_sig, _COINBASE_SEQUENCE, check_validity=False)
            ],
            vout=[
                TxOut(
                    subsidy(height, consensus.subsidy_halving_interval) + fees,
                    script_pub_key,
                )
            ],
            check_validity=False,
        ),
        *(c.tx for c in chosen),
    ]
    commitment = witness_commitment_output(transactions, _WITNESS_NONCE)
    coinbase = transactions[0]
    # `UpdateUncommittedBlockStructures`: the reserved value goes in once
    # segwit is active
    witness = (
        Witness([_WITNESS_NONCE], check_validity=False)
        if height >= consensus.segwit_height
        else Witness([], check_validity=False)
    )
    transactions[0] = Tx(
        version=coinbase.version,
        lock_time=coinbase.lock_time,
        vin=[
            TxIn(
                coinbase.vin[0].prev_out,
                coinbase.vin[0].script_sig,
                coinbase.vin[0].sequence,
                witness,
                check_validity=False,
            )
        ],
        vout=[*coinbase.vout, commitment],
        check_validity=False,
    )

    when = datetime.fromtimestamp(max(min_time, int(time.time())), UTC)
    merkle_root, _ = merkle_root_and_mutated_from_transactions(transactions)
    version = node.unknown_activations.status(
        block_index.active_chain, block_index.header_dict
    ).version
    header = BlockHeader(version, tip_hash, merkle_root, when, tip_header.bits, 0)
    header.bits = next_bits_required(
        header, tip_header, tip_height, parent_of, consensus
    )
    return BlockTemplate(
        Block(header, transactions, check_validity=False),
        [c.fee for c in chosen],
        [c.sigops for c in chosen],
        min_time,
    )


def create_new_block(node: Node, script_pub_key: ScriptPubKey) -> BlockTemplate:
    """Build a block on the tip holding what the mempool offers.

    Core's `CreateNewBlock` checks the block it builds, without its proof
    of work and merkle root, and throws where it is refused; this raises
    `TemplateError`.
    """
    block_index = node.chainstate.block_index
    height = len(block_index.active_chain)
    tip_header = block_index.header_dict[block_index.active_chain[-1]].header
    tip_mtp = median_time_past(tip_header, height - 1, parent_lookup(node))
    template = _assemble(
        node, script_pub_key, _PackageSelector(node, height, tip_mtp).select()
    )
    reason = check_block_validity(node, template.block, check_merkle_root=False)
    if reason is not None:
        raise TemplateError(reason)
    return template


def block_with_transactions(
    node: Node, script_pub_key: ScriptPubKey, transactions: list[Tx]
) -> Block:
    """Build a block on the tip holding exactly `transactions`, in order.

    `generateblock`'s block: a coinbase that claims the subsidy alone,
    whatever the transactions pay, as `createNewBlock` with
    `use_mempool=false` gives it, and the witness commitment regenerated
    over them as `RegenerateCommitments` does.
    """
    return _assemble(
        node, script_pub_key, [_Chosen(tx, 0, 0) for tx in transactions]
    ).block


def solve_block(
    node: Node, block: Block, max_tries: int
) -> Generator[bool, None, tuple[Block | None, int]]:
    """Search `block`'s nonces, and return the solved block with the tries left.

    Core's `GenerateBlock`: one try per nonce, from the header's own. The
    block is `None` where the tries ran out, or `Node.terminate_flag`
    was set, with the tries left; and where the nonces ran out first,
    `max_tries` is what is left and `block` is `None` too, so the caller
    builds another block. Yields `True` after each `_NONCE_CHUNK` that
    found nothing, for `rpc.main` to resume it on `Node`'s loop.
    """
    header = block.header
    remaining = max_tries
    while remaining > 0 and not node.terminate_flag.is_set():
        start = header.nonce
        solved = mine(header, min(remaining, _NONCE_CHUNK))
        if solved is not None:
            header.nonce = solved.nonce
            return block, remaining - (solved.nonce - start)
        # mine stops at the end of the field, with `start + tried` its length
        tried = min(remaining, _NONCE_CHUNK, NONCE_SPACE - start)
        remaining -= tried
        header.nonce = start + tried
        if header.nonce == NONCE_SPACE:
            header.nonce = 0
            return None, remaining
        yield True
    return None, remaining


def _merkle_reason(block: Block) -> str | None:
    """Return why `block` fails `CheckMerkleRoot`, CVE-2012-2459 included."""
    if not block.transactions:
        # Core's merkle root of no transactions is the null hash
        return "bad-txnmrklroot" if block.header.merkle_root != bytes(32) else None
    try:
        block.assert_valid_merkle_root()
    except BTClibValueError as error:
        return "bad-txns-duplicate" if "duplicate" in str(error) else "bad-txnmrklroot"
    return None


def _check_block(block: Block, *, check_merkle_root: bool) -> str | None:
    """Return why `block` fails Core's `CheckBlock`, proof of work apart."""
    if check_merkle_root and (reason := _merkle_reason(block)) is not None:
        return reason
    return _structure_reason(block)


def _structure_reason(block: Block) -> str | None:
    """Return why `block`'s size, coinbase, transactions or sigops fail."""
    transactions = block.transactions
    try:
        block.assert_valid_length()
    except BTClibValueError:
        return "bad-blk-length"
    if not transactions[0].is_coinbase:
        return "bad-cb-missing"
    if any(tx.is_coinbase for tx in transactions[1:]):
        return "bad-cb-multiple"
    for tx in transactions:
        try:
            tx.assert_valid()
        except BTClibValueError as error:
            return str(error)
    try:
        block.assert_valid_sig_op_count()
    except BTClibValueError:
        return "bad-blk-sigops"
    return None


def _contextual_header_reason(node: Node, header: BlockHeader) -> str | None:
    """Return why `header` fails `ContextualCheckBlockHeader` on the tip."""
    block_index = node.chainstate.block_index
    tip_height = len(block_index.active_chain) - 1
    tip_header = block_index.header_dict[block_index.active_chain[-1]].header
    consensus = node.chain.consensus
    parent_of: ParentOf = parent_lookup(node)
    required = next_bits_required(header, tip_header, tip_height, parent_of, consensus)
    if header.bits != required:
        return "bad-diffbits"
    if block_time(header) <= median_time_past(tip_header, tip_height, parent_of):
        return "time-too-old"
    try:
        assert_not_timewarp(header, tip_header, tip_height, consensus)
    except BTClibValueError:
        return "time-timewarp-attack"
    try:
        header.assert_valid_time(datetime.now(UTC))
    except BTClibValueError:
        return "time-too-new"
    return _version_reason(node, header)


def _version_reason(node: Node, header: BlockHeader) -> str | None:
    """Return `bad-version` for a version BIP34, 66 or 65 made obsolete."""
    height = len(node.chainstate.block_index.active_chain)
    for least, field in _MIN_VERSIONS:
        if header.version < least and height >= getattr(node.chain.consensus, field):
            return f"bad-version(0x{header.version & 0xFFFFFFFF:08x})"
    return None


def _witness_reason(node: Node, block: Block, height: int) -> str | None:
    """Return why `block` fails `CheckWitnessMalleation` or the weight limit."""
    transactions = block.transactions
    commitment = (
        block.witness_commitment
        if height >= node.chain.consensus.segwit_height
        else None
    )
    if commitment is None:
        if any(tx.is_segwit for tx in transactions):
            return "unexpected-witness"
    else:
        stack = transactions[0].vin[0].script_witness.stack
        if len(stack) != 1 or len(stack[0]) != len(_WITNESS_NONCE):
            return "bad-witness-nonce-size"
        if coinbase_witness_commitment(transactions, stack[0]) != commitment:
            return "bad-witness-merkle-match"
    if block.weight > MAX_BLOCK_WEIGHT:
        return "bad-blk-weight"
    return None


def _contextual_block_reason(node: Node, block: Block, height: int) -> str | None:
    """Return why `block` fails `ContextualCheckBlock` at `height`."""
    try:
        contextual_check_block(node, block, height)
    except BTClibValueError as error:
        return str(error)
    return _witness_reason(node, block, height)


def _connect_reason(node: Node, block: Block, height: int) -> str | None:
    """Return why `block` does not connect at `height`."""
    try:
        try_connect_block(node, block, height)
    except TxRejectedError as error:
        # Core's `GetRejectReason`, which BIP22 answers, not `ToString`
        return error.reason
    except (
        BTClibException,
        InvalidBlockInputError,
        PrevoutCountMismatchError,
    ) as error:
        message = str(error)
        # a reason in Core's words, followed by the detail Core logs apart
        word, _, _ = message.partition(": ")
        return _CONNECT_REASONS.get(
            message, word if _REASON.fullmatch(word) else message
        )
    return None


def check_block_validity(
    node: Node, block: Block, *, check_merkle_root: bool
) -> str | None:
    """Answer why `block` is not valid on top of the tip, or `None` where it is.

    Core's `TestBlockValidity` with `check_pow` false, as both its RPC
    callers ask it: the checks `ProcessNewBlock` makes before and while
    it connects a block, in Core's order, and nothing stored. The reason
    is Core's word where this module has one.

    A failure of this node's own storage is not a reason about `block`,
    and propagates.
    """
    active_chain = node.chainstate.block_index.active_chain
    if block.header.previous_block_hash != active_chain[-1]:
        return "inconclusive-not-best-prevblk"
    height = len(active_chain)
    return (
        _check_block(block, check_merkle_root=check_merkle_root)
        or _contextual_header_reason(node, block.header)
        or _contextual_block_reason(node, block, height)
        or _connect_reason(node, block, height)
    )


def accept_block(node: Node, block: Block) -> str | None:
    """Store a block this node built on the tip, and connect it.

    Core's `ProcessNewBlock` for the block a mining RPC solved
    (`src/rpc/mining.cpp`). Answers why the block was refused, or `None`.
    `rpc.callbacks.submit_block` is the same hand-over for a block from
    outside; this block, built on the tip and checked, needs none of its
    refusals but one. The search can end after the tip it was built on is
    invalidated, and then its header is refused `bad-prevblk`, as Core's
    `AcceptBlockHeader` refuses it. A tip that only moved on leaves the
    block stored on its branch, as in Core.

    A failure that is not a verdict on the block stops `Node.run`, as
    `rpc.callbacks._validate_extending_tip` argues.
    """
    block_index = node.chainstate.block_index
    block_hash = block.header.hash
    try:
        block_index.add_headers([block.header])
    except BTClibException as error:
        return str(error)
    node.block_db.add_block(block)
    block_index.set_downloaded(block_hash)
    new_pow_valid_block(node, block)
    try:
        update_chain(node)
    except Exception:
        node.terminate_flag.set()
        raise
    failed = node.last_rejected_block
    if failed is not None and failed[0] == block_hash:
        # Core's `GetRejectReason`, never its debug message
        if isinstance(failed[1], TxRejectedError):
            return failed[1].reason
        return str(failed[1])
    return None
