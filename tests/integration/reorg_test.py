# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A real bitcoind announces a reorg, and this node walks its chain back.

`update_chain` and `_reconcile_mempool_for_reorg` (`btclib_node/main.py`)
are otherwise driven by tests that hand this node both branches
directly, on the thread that built them, which says nothing about a
reorg reaching this node from an implementation it did not write. That
is what is here: a chain downloaded from Core over p2p, then a heavier
branch Core switches to and announces, with this node's own tip read
against Core's after each.

The reorg is the subject, so it is asserted and not assumed -- the node
is held to Core's first tip before the competing branch is submitted at
all, the abandoned block is looked up in the block index afterwards, and
the transaction it confirmed is looked for in the mempool. A node that
took the second branch as one long first sync fails every one of those.

The blocks are built here and handed over with `submitblock`, the way
`backpressure_test.py` builds its own: `submitblock` takes a branch from
a lower height as readily as it takes an extension, where mining a
competing branch out of bitcoind's own wallet wants `invalidateblock` or
a second daemon. What the abandoned branch has to carry is a
transaction, and the only thing a chain built from nothing has to spend
is a coinbase -- so the fork sits past Core's own coinbase maturity,
which is what `_COMMON` is.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from btclib.block import Block, BlockHeader, merkle_root_and_mutated_from_transactions
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.chainstate.block_index import BlockStatus
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.p2p.address import peer_address
from tests import (
    brute_force_nonce,
    get_random_port,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

# How many blocks the two branches share, and it is Core's own coinbase
# maturity: `COINBASE_MATURITY`, `src/consensus/consensus.h`,
# at bitcoin/bitcoin@9be056a8a7 -- v31.1, the release
# `integration-bitcoind.yml` pins and this module therefore runs
# against. A constant there and not a chain parameter, so regtest does
# not relax it. The fork therefore sits one block past that depth, which
# is the shallowest place a transaction spending the chain's first
# coinbase can be confirmed.
#
# The height is nearly free, every block here being a coinbase and at
# most one other transaction: the whole of it -- both branches built,
# submitted, downloaded over p2p and reorged -- costs less than the
# megabyte chain `backpressure_test.py` beside it hands over, which is
# what puts this module in the gate's own workflow rather than in a
# sentinel's. `--durations` over `tests/integration` re-derives that.
_COMMON = 100

# What the funding coinbase pays and what the transaction spending it
# gives up as a fee. The subsidy is regtest's own at these heights, and
# a coinbase claiming exactly it is what lets the fee be paid out of a
# real amount rather than out of nothing.
_SUBSIDY = 50 * 10**8
_FEE = 10**8

# An output anything spends and the input that spends it: a bare data
# push leaves a non-zero stack top, which is all consensus asks of a
# scriptPubKey that is neither P2SH nor a witness program -- so no key
# is generated and nothing is signed. `tests/unit/main_test.py`'s own
# `spend` builds its transactions the same way.
_FUNDED = script.serialize([b"\x22" * 32])
_SPENDS_IT = script.serialize([b"\x11" * 32])

# What dates the headers, and it is not the regtest genesis
# `backpressure_test.py` beside this one counts from. Core relays no
# inventory at all while it holds itself to be in initial block download
# (`PeerManagerImpl::UpdatedBlockTip` returns early on it,
# `src/net_processing.cpp`), and it leaves that state only once its own
# tip is within `-maxtipage` of the clock -- a day, by
# `DEFAULT_MAX_TIP_AGE` in `src/kernel/chainstatemanager_opts.h`; both
# at bitcoin/bitcoin@9be056a8a7. `tests.GENESIS_TIME` is
# `RegTest().genesis.time`, which is not a day behind the clock but
# years, so Core would hold every branch dated from it to itself and
# announce none of it.
# `bitcoind_test.py` and `backpressure_test.py` do not notice, both
# being a sync this node asks Core for; this module is the one that
# waits to be told, so it dates its chain backwards from now and lands
# the tip on the clock.
_SPACING = timedelta(seconds=60)
_BUILT_AT = datetime.now(tz=UTC)


def block_time(height: int) -> datetime:
    """Return the timestamp for a block at `height`, counting back from now."""
    return _BUILT_AT - _SPACING * (_COMMON + 2 - height)


def height_push(height: int) -> bytes:
    """Return the height a coinbase's scriptSig has to open with, BIP34's way.

    Core checks the opening push against `CScript() << nHeight`, which is
    a bare `OP_1`..`OP_16` up to sixteen and a minimal one-octet push
    above it -- the two are different encodings of the same number and
    only one of them is accepted at a given height. One octet is a
    minimal push only below 128, which every height this module builds
    is; a chain long enough to pass that would need the two-octet form,
    so it raises rather than building a block Core would refuse for a
    reason nothing here would name.
    """
    if height <= 16:
        return bytes([0x50 + height])
    if height < 128:
        return bytes([0x01, height])
    err_msg = f"a height past 127 needs a wider push: {height}"
    raise ValueError(err_msg)


def a_coinbase(height: int, *, funding: bool = False) -> Tx:
    """Return the coinbase for a block at `height`, spendable where `funding`.

    The push after the height is what carries the scriptSig to the two
    octets a coinbase's own consensus rule asks of it: BIP34 encodes a
    height below seventeen as one `OP_1`..`OP_16` octet, which
    `CheckTransaction` refuses as `bad-cb-length`
    (`src/consensus/tx_check.cpp`, at bitcoin/bitcoin@9be056a8a7), and
    the chain here is built from the genesis up. Core's own template
    pads the same place for the same reason and calls the padding a
    dummy extraNonce (`include_dummy_extranonce`, `src/node/miner.cpp`,
    at the same commit); random octets rather than its fixed `OP_0`
    leave no two coinbases here alike whatever else they share.
    """
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(),
                script_sig=height_push(height)
                + script.serialize([secrets.token_bytes(8)]),
                sequence=0xFFFFFFFF,
            )
        ],
        # a coinbase may claim less than the subsidy, and every one here
        # but the funding block's claims nothing: what this test spends
        # is a single known output, not whatever the chain paid out
        vout=[
            TxOut(
                value=_SUBSIDY if funding else 0,
                script_pub_key=_FUNDED
                if funding
                else script.serialize([secrets.token_bytes(32)]),
            )
        ],
    )


def a_block(
    previous_block_hash: bytes,
    height: int,
    transactions: tuple[Tx, ...] = (),
    *,
    funding: bool = False,
) -> Block:
    """Return a solved regtest block Core will accept at `height`."""
    body = [a_coinbase(height, funding=funding), *transactions]
    header = BlockHeader(
        version=0x20000000,
        previous_block_hash=previous_block_hash,
        merkle_root=merkle_root_and_mutated_from_transactions(body)[0],
        # a minute apart, so every header beats the median of the eleven
        # before it and none is ahead of the clock. Two blocks built at
        # one height on two branches share a timestamp, which is what a
        # real fork looks like and what Core allows
        time=block_time(height),
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)
    # `Block.__init__` checks the header against mainnet's pow limit,
    # which no regtest block meets; `brute_force_nonce` has already
    # checked it against the limit that does apply, the same reason
    # `tests.build_block` gives
    return Block(header, body, check_validity=False)


def spending(funding_coinbase: Tx) -> Tx:
    """Return the transaction the abandoned branch confirms and the reorg frees.

    Its prevout is the chain's first coinbase, which sits below the fork
    and so survives the reorg: that is what lets it verify again and
    re-enter the mempool rather than being dropped for good.
    """
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(funding_coinbase.id, 0),
                script_sig=_SPENDS_IT,
                sequence=0xFFFFFFFF,
            )
        ],
        vout=[
            TxOut(
                value=_SUBSIDY - _FEE,
                script_pub_key=script.serialize([secrets.token_bytes(32)]),
            )
        ],
    )


def submit(bitcoind: Bitcoind, blocks: list[Block]) -> str:
    """Hand `blocks` to bitcoind in order, and return its tip afterwards.

    `submitblock` answers with a reason rather than raising, so an
    unread answer would leave the chain short and blame whatever
    asserted on the tip next. Two of its answers mean Core took the
    block. `None` is the one every block extending the tip gets. The
    other is `"inconclusive"`, which the first block of the competing
    branch here gets and which is not a refusal: `submitblock` returns
    it where its own `submitblock_StateCatcher` saw no verdict on the
    block, and Core reaches no verdict on a branch that does not become
    the tip (`src/rpc/mining.cpp`, at bitcoin/bitcoin@9be056a8a7). A
    block Core actually refuses answers with the reason instead, which
    is what this assertion is for.
    """
    for block in blocks:
        answer = bitcoind.rpc(
            "submitblock", [block.serialize(check_validity=False).hex()]
        )
        assert answer in (None, "inconclusive"), answer
    return str(bitcoind.rpc("getbestblockhash"))


def a_chain(length: int) -> list[Block]:
    """Return `length` blocks from the regtest genesis, the first funding."""
    chain: list[Block] = []
    for height in range(1, length + 1):
        previous = chain[-1].header.hash if chain else RegTest().genesis.hash
        chain.append(a_block(previous, height, funding=height == 1))
    return chain


def test_a_node_follows_a_reorg_a_real_bitcoind_announces(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """The node downloads one branch from Core, then follows Core off it.

    What says the reorg happened, rather than one longer first sync:
    the node is held to the abandoned branch's own tip before the
    competing branch is built at all, the abandoned block is still in
    the block index afterwards and off the active chain, and the
    transaction it confirmed is back in the mempool -- which is
    `_reconcile_mempool_for_reorg`'s own work and reachable no other
    way.
    """
    common = a_chain(_COMMON)
    confirmed = spending(common[0].transactions[0])
    abandoned = a_block(common[-1].header.hash, _COMMON + 1, (confirmed,))
    first_tip = submit(bitcoind, [*common, abandoned])

    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node.start()
    wait_until_listening(node.p2p_manager)
    node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))

    block_index = node.chainstate.block_index
    wait_until(lambda: len(block_index.active_chain) == _COMMON + 2)
    wait_until(lambda: node.status == NodeStatus.BlockSynced)
    assert block_index.active_chain[-1].hex() == first_tip
    # confirmed by the branch it is about to lose, so its being in the
    # mempool at the end is something the reorg has to do
    assert not node.mempool.contains_tx(confirmed)

    # the first of the two is the abandoned block's own sibling and
    # carries no more work than it, which is why Core answers it
    # `"inconclusive"` and stays where it is; the second is what makes
    # the branch heavier and what Core reorgs onto
    rival = a_block(common[-1].header.hash, _COMMON + 1)
    heavier = a_block(rival.header.hash, _COMMON + 2)
    second_tip = submit(bitcoind, [rival, heavier])
    assert second_tip != first_tip

    wait_until(lambda: block_index.active_chain[-1].hex() == second_tip)

    # read straight off the wait above rather than waited for again:
    # `_finalize_fork` drops every abandoned block from the active chain
    # and marks it `valid` before it appends the block the wait returns
    # on, and both of those reach the index in memory as they are made
    assert abandoned.header.hash not in block_index.active_chain
    assert block_index.get_block_info(abandoned.header.hash).status == BlockStatus.valid

    # waited for, where the two above are not: `update_chain` reconciles
    # the mempool only after `_finalize_fork` has returned, so the tip
    # the wait above returns on is committed while the transaction the
    # abandoned branch confirmed is still out of the mempool. Reading it
    # straight off is a race this thread usually wins and does not
    # always: delaying `_reconcile_mempool_for_reorg` fails the bare
    # read and leaves this one passing.
    wait_until(lambda: node.mempool.contains_tx(confirmed))

    node.stop()
    node.join()
