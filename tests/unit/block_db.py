# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What survives being written to disk, and what is read back from where.

The block store keeps its blocks and its rollback patches in flat files
and their locations in the key-value store, so the questions are: does
what went in
come back out, does it still come back once the store has been closed
and reopened, and does it come back after the file it was written to is
no longer the file being written to.
"""

from pathlib import Path

import pytest
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx_out import TxOut

from btclib_node.block_db import BlockDB, BlockLocation, FileMetadata, RevBlock
from btclib_node.chains import RegTest
from btclib_node.log import Logger
from tests.helpers import generate_random_chain

MAX_FILE_SIZE = 128 * 1000**2


def a_rev_block(tag: int = 1, hash: bytes | None = None) -> RevBlock:
    out_point = OutPoint(bytes([tag]) * 32, tag)
    tx_out = TxOut(value=tag * 10**8, script_pub_key=script.serialize([bytes([tag])]))
    return RevBlock(
        hash=hash if hash is not None else bytes([tag]) * 32,
        to_add=[(out_point, tx_out)],
        to_remove=[OutPoint(bytes([tag + 1]) * 32, 0)],
    )


def a_db(tmp_path: Path) -> BlockDB:
    return BlockDB(tmp_path, Logger(debug=True))


def test_init(tmp_path: Path) -> None:
    BlockDB(tmp_path, Logger(debug=True))


def test_blocks(tmp_path: Path) -> None:
    chain = generate_random_chain(2000, RegTest().genesis.hash)
    for x in range(10):
        block_db = BlockDB(tmp_path / f"{x}", Logger(debug=True))
        for block in chain:
            block_db.add_block(block)
            stored_block = block_db.get_block(block.header.hash)
            assert stored_block == block


def test_a_rev_patch_survives_the_wire() -> None:
    rev_block = a_rev_block()
    assert RevBlock.deserialize(rev_block.serialize()) == rev_block


def test_a_rev_patch_is_read_back_from_the_file_it_went_into(tmp_path: Path) -> None:
    block_db = a_db(tmp_path)
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    block_db.add_block(block)
    rev_block = a_rev_block(hash=block.header.hash)
    block_db.add_rev_block(rev_block)
    block_db.finalize()
    assert block_db.get_rev_block(rev_block.hash) == rev_block


def test_what_was_never_stored_is_not_found(tmp_path: Path) -> None:
    block_db = a_db(tmp_path)
    assert block_db.get_block(b"\x11" * 32) is None
    assert block_db.get_rev_block(b"\x11" * 32) is None


def test_storing_the_same_block_twice_writes_it_once(tmp_path: Path) -> None:
    block_db = a_db(tmp_path)
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    block_db.add_block(block)
    written = block_db.files["000001.blk"].size
    block_db.add_block(block)
    assert block_db.files["000001.blk"].size == written
    assert block_db.get_block(block.header.hash) == block


def test_storing_the_same_rev_patch_twice_writes_it_once(tmp_path: Path) -> None:
    block_db = a_db(tmp_path)
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    block_db.add_block(block)
    rev_block = a_rev_block(hash=block.header.hash)
    block_db.add_rev_block(rev_block)
    block_db.finalize()
    filename = block_db.rev_patches[rev_block.hash].filename
    written = block_db.files[filename].size
    # already in rev_patches: buffered a second time and dropped again
    # without a second write, same as add_block's own dedup
    block_db.add_rev_block(rev_block)
    assert block_db.pending_rev_blocks == {}
    block_db.finalize()
    assert block_db.files[filename].size == written


def test_a_file_that_has_filled_up_is_left_behind(tmp_path: Path) -> None:
    block_db = a_db(tmp_path)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_db.add_block(chain[0])
    assert block_db.blocks[chain[0].header.hash].filename == "000001.blk"

    # what the store does with a full file, without writing 128MB to
    # find out: the size it reads is the size it recorded
    block_db.files["000001.blk"].size = MAX_FILE_SIZE + 1
    block_db.add_block(chain[1])
    assert block_db.blocks[chain[1].header.hash].filename == "000002.blk"
    assert block_db.file_index == 2

    # and the block in the file no longer being written to is still read
    assert block_db.get_block(chain[0].header.hash) == chain[0]
    assert block_db.get_block(chain[1].header.hash) == chain[1]


def test_a_file_exactly_at_the_bound_is_not_yet_full(tmp_path: Path) -> None:
    # the bound is what the size is compared against, so which side of
    # it is exclusive is a real question. Only where the block lands is
    # asserted here: the size set below is a fiction the file on disk
    # does not share, so the offset recorded for that block is not one
    # it can be read back from
    block_db = a_db(tmp_path)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_db.add_block(chain[0])
    block_db.files["000001.blk"].size = MAX_FILE_SIZE
    block_db.add_block(chain[1])
    assert block_db.blocks[chain[1].header.hash].filename == "000001.blk"
    assert block_db.file_index == 1


def test_a_rev_patch_goes_in_the_file_named_for_its_own_block(
    tmp_path: Path,
) -> None:
    # the first block's own patch is written only after the block file
    # has rolled over to a second one: were the rev file still named for
    # whichever block file happens to be open (btclib-org/btclib-node#116),
    # this patch would land in 000002.rev rather than its own block's
    # 000001.rev
    block_db = a_db(tmp_path)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    block_db.add_block(chain[0])
    assert block_db.blocks[chain[0].header.hash].filename == "000001.blk"

    block_db.files["000001.blk"].size = MAX_FILE_SIZE + 1
    block_db.add_block(chain[1])
    assert block_db.blocks[chain[1].header.hash].filename == "000002.blk"

    first = a_rev_block(1, hash=chain[0].header.hash)
    block_db.add_rev_block(first)
    block_db.finalize()
    assert block_db.rev_patches[first.hash].filename == "000001.rev"

    second = a_rev_block(3, hash=chain[1].header.hash)
    block_db.add_rev_block(second)
    block_db.finalize()
    assert block_db.rev_patches[second.hash].filename == "000002.rev"

    assert block_db.get_rev_block(first.hash) == first
    assert block_db.get_rev_block(second.hash) == second


def test_two_rev_patches_share_the_file_named_for_the_block_file(
    tmp_path: Path,
) -> None:
    block_db = a_db(tmp_path)
    chain = generate_random_chain(2, RegTest().genesis.hash)
    for block in chain:
        block_db.add_block(block)
    first = a_rev_block(1, hash=chain[0].header.hash)
    second = a_rev_block(5, hash=chain[1].header.hash)
    block_db.add_rev_block(first)
    block_db.add_rev_block(second)
    block_db.finalize()
    locations = [block_db.rev_patches[patch.hash] for patch in (first, second)]
    assert locations[0].filename == locations[1].filename
    assert locations[0].index != locations[1].index
    assert block_db.get_rev_block(first.hash) == first
    assert block_db.get_rev_block(second.hash) == second


def test_a_pending_rev_patch_is_not_yet_on_disk(tmp_path: Path) -> None:
    block_db = a_db(tmp_path)
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    block_db.add_block(block)
    rev_block = a_rev_block(hash=block.header.hash)
    block_db.add_rev_block(rev_block)
    assert block_db.get_rev_block(rev_block.hash) is None
    assert block_db.rev_patches == {}


def test_rollback_discards_every_pending_rev_patch(tmp_path: Path) -> None:
    block_db = a_db(tmp_path)
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    block_db.add_block(block)
    rev_block = a_rev_block(hash=block.header.hash)
    block_db.add_rev_block(rev_block)
    block_db.rollback()
    assert block_db.pending_rev_blocks == {}
    assert block_db.get_rev_block(rev_block.hash) is None

    # rollback left nothing half-written: buffering and finalizing it
    # again afterwards works cleanly
    block_db.add_rev_block(rev_block)
    block_db.finalize()
    assert block_db.get_rev_block(rev_block.hash) == rev_block


def test_finalize_refuses_a_patch_for_a_block_never_stored(tmp_path: Path) -> None:
    block_db = a_db(tmp_path)
    rev_block = a_rev_block()
    block_db.add_rev_block(rev_block)
    with pytest.raises(Exception, match="not stored"):
        block_db.finalize()


def test_a_filename_past_ten_characters_survives_the_wire() -> None:
    # ten octets was a fixed width assumed for `{index:06d}.blk`; past
    # the millionth file the name is eleven characters
    # (btclib-org/btclib-node#78)
    location = BlockLocation("1000000.blk", 5, 10)
    assert BlockLocation.deserialize(location.serialize()) == location

    metadata = FileMetadata("1000000.rev", 5)
    assert FileMetadata.deserialize(metadata.serialize()) == metadata


def test_an_offset_past_var_ints_default_parse_cap_survives_the_wire() -> None:
    # BlockLocation.index/size measure a position inside a still-open
    # 128MB block file, well past var_int.parse's default max_size
    # (0x02000000, Bitcoin Core's ReadCompactSize guard against an
    # attacker-inflated item count) before the file ever rotates
    location = BlockLocation("000001.blk", 40_000_000, 100)
    assert BlockLocation.deserialize(location.serialize()) == location

    metadata = FileMetadata("000001.blk", MAX_FILE_SIZE)
    assert FileMetadata.deserialize(metadata.serialize()) == metadata


def test_the_file_rotation_counter_passes_the_old_two_octet_ceiling(
    tmp_path: Path,
) -> None:
    # a fixed two-octet field overflows encoding 65536
    # (btclib-org/btclib-node#78); var_int has no ceiling below its own
    # 8-byte encoding limit
    block_db = a_db(tmp_path)
    block_db.file_index = 65600
    block_db.files["065600.blk"] = FileMetadata("065600.blk", MAX_FILE_SIZE + 1)
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    block_db.add_block(block)
    assert block_db.file_index == 65601
    assert block_db.blocks[block.header.hash].filename == "065601.blk"
    assert block_db.get_block(block.header.hash) == block
    block_db.close()

    reopened = a_db(tmp_path)
    assert reopened.file_index == 65601
    reopened.close()


def test_a_block_file_is_matched_by_its_resolved_path_not_its_basename(
    tmp_path: Path,
) -> None:
    # a handle open on a same-named file under a different directory is
    # not the file this store means: matching by a basename or a suffix
    # (btclib-org/btclib-node#79) would accept it anyway
    block_db = a_db(tmp_path)
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    block_db.add_block(block)

    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy_file = (decoy_dir / "000001.blk").open("a+b")
    decoy_file.write(b"not this store's block")
    decoy_file.flush()

    assert block_db.open_block_file is not None
    block_db.open_block_file.close()
    block_db.open_block_file = decoy_file
    assert block_db.get_block(block.header.hash) == block
    decoy_file.close()


def test_closing_a_store_that_wrote_nothing(tmp_path: Path) -> None:
    # nothing was written, so there is no file to close: only the
    # database itself
    a_db(tmp_path).close()


def test_a_key_this_version_does_not_know_is_left_where_it_is(tmp_path: Path) -> None:
    block_db = a_db(tmp_path)
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    block_db.add_block(block)
    block_db.db.put(b"z", b"from some other version of this store")
    block_db.close()

    reopened = a_db(tmp_path)
    # stepped over rather than filed under one of the four the store
    # knows: the tables it rebuilt are the ones it wrote
    assert reopened.blocks == block_db.blocks
    assert reopened.rev_patches == {}
    assert reopened.files == block_db.files
    assert reopened.file_index == block_db.file_index
    assert reopened.get_block(block.header.hash) == block
    reopened.close()


def test_the_store_comes_back_from_disk(tmp_path: Path) -> None:
    block_db = a_db(tmp_path)
    (block,) = generate_random_chain(1, RegTest().genesis.hash)
    block_db.add_block(block)
    rev_block = a_rev_block(hash=block.header.hash)
    block_db.add_rev_block(rev_block)
    block_db.finalize()
    block_db.close()

    reopened = a_db(tmp_path)
    assert reopened.file_index == block_db.file_index
    # the sizes too, not just the names: they are what tells the store
    # where the next block goes and when the file is full
    assert reopened.files == block_db.files
    assert reopened.blocks == block_db.blocks
    assert reopened.rev_patches == block_db.rev_patches
    assert reopened.get_block(block.header.hash) == block
    assert reopened.get_rev_block(rev_block.hash) == rev_block
    reopened.close()
