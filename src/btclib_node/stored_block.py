# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A block read from the store, checked as Core's `ReadBlock` checks it.

`ReadBlock(block, pos, expected_hash)` refuses, in this order, a header
missing its proof of work, on signet a block whose solution does not
verify, and a header that hashes to another hash than the one expected
(`src/node/blockstorage.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag). Each refusal here is a `BTClibValueError`, except in
`read_held_block`.
"""

from io import BytesIO
from typing import TYPE_CHECKING

from btclib.block import Block, BlockHeader
from btclib.exceptions import BTClibValueError

from btclib_node.chains import SigNet
from btclib_node.exceptions import ChainstateInconsistencyError
from btclib_node.signet import assert_valid_solution

if TYPE_CHECKING:
    from btclib_node.block_db import BlockDB
    from btclib_node.chains import Chain

__all__ = ["read_checked_block", "read_checked_raw", "read_held_block"]


def _read(
    block_db: BlockDB, chain: Chain, block_hash: bytes
) -> tuple[bytes, Block | None] | None:
    """Return the checked octets, with the block where it was parsed.

    `None` is a block the store does not hold. Off signet, only the
    eighty header octets are parsed.
    """
    raw = block_db.get_raw_block(block_hash)
    if raw is None:
        return None
    # a stream, so that the octets past the header are left unread
    header = BlockHeader.parse(BytesIO(raw), check_validity=False)
    header.assert_valid_pow(chain.pow_limit_bits)
    block = None
    if isinstance(chain, SigNet):
        block = Block.parse(raw, check_validity=False)
        assert_valid_solution(block, chain)
    if header.hash != block_hash:
        err_msg = f"stored block hashes to {header.hash.hex()}, not {block_hash.hex()}"
        raise BTClibValueError(err_msg)
    return raw, block


def read_checked_raw(
    block_db: BlockDB, chain: Chain, block_hash: bytes
) -> bytes | None:
    """Return the octets `block_db` holds for `block_hash`, or `None`."""
    read = _read(block_db, chain, block_hash)
    return None if read is None else read[0]


def read_checked_block(
    block_db: BlockDB, chain: Chain, block_hash: bytes
) -> Block | None:
    """Return the block `block_db` holds for `block_hash`, or `None`.

    Off signet, refused before the body is parsed.
    """
    read = _read(block_db, chain, block_hash)
    if read is None:
        return None
    raw, block = read
    return Block.parse(raw, check_validity=False) if block is None else block


def read_held_block(block_db: BlockDB, chain: Chain, block_hash: bytes) -> Block | None:
    """Return `read_checked_block`, a refusal raised as the store's own fault.

    The node stores a block, genesis apart, only once it passes
    `main.assert_valid_block`, and under its own hash: what this refuses
    is a record the store changed.

    For a read Core expects to succeed, and ends the node where it does
    not: `getblocktxn` asserts (`src/net_processing.cpp`), `ConnectTip`
    and, after `DisconnectTip`, `ActivateBestChainStep` call `FatalError`
    (`src/validation.cpp`), and `BaseIndex::ProcessBlock` calls
    `FatalErrorf` (`src/index/base.cpp`), at the same sha. The refusal is
    a `ChainstateInconsistencyError`.
    """
    try:
        return read_checked_block(block_db, chain, block_hash)
    except BTClibValueError as e:
        err_msg = f"cannot read block {block_hash.hex()} from the store: {e}"
        raise ChainstateInconsistencyError(err_msg) from e
