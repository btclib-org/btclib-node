# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A block read from the store, checked as Core's `ReadBlock` checks it.

`ReadBlock(block, pos, expected_hash)` refuses, in this order, a header
missing its proof of work, on signet a block whose solution does not
verify, and a header that hashes to another hash than the one expected
(`src/node/blockstorage.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag, and unchanged at bitcoin/bitcoin@152a55397e). Each refusal here is
a `BTClibValueError`.
"""

from io import BytesIO
from typing import TYPE_CHECKING

from btclib.block import Block, BlockHeader
from btclib.exceptions import BTClibValueError

from btclib_node.chains import SigNet
from btclib_node.signet import assert_valid_solution

if TYPE_CHECKING:
    from btclib_node import Node

__all__ = ["read_checked_block", "read_checked_raw"]


def read_checked_raw(node: Node, block_hash: bytes) -> bytes | None:
    """Return the octets `node.block_db` holds for `block_hash`, or `None`.

    `None` is a block the store does not hold. Off signet, only the
    eighty header octets are parsed.
    """
    raw = node.block_db.get_raw_block(block_hash)
    if raw is None:
        return None
    # a stream, so that the octets past the header are left unread
    header = BlockHeader.parse(BytesIO(raw), check_validity=False)
    header.assert_valid_pow(node.chain.pow_limit_bits)
    if isinstance(node.chain, SigNet):
        assert_valid_solution(Block.parse(raw, check_validity=False), node.chain)
    if header.hash != block_hash:
        err_msg = f"stored block hashes to {header.hash.hex()}, not {block_hash.hex()}"
        raise BTClibValueError(err_msg)
    return raw


def read_checked_block(node: Node, block_hash: bytes) -> Block | None:
    """Return the block `node.block_db` holds for `block_hash`, or `None`.

    Refused as `read_checked_raw` refuses it, before the body is parsed.
    """
    raw = read_checked_raw(node, block_hash)
    if raw is None:
        return None
    return Block.parse(raw, check_validity=False)
