# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The rules a chain imposes on the header after it.

Every chain here is built out of unmined headers, the proof of work
being the one question these rules do not ask: a target no chain hands
out is exactly what `BlockHeader.assert_valid_pow` accepts, so a header
that would take a mainnet miner an hour is one line here.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from btclib.block import BlockHeader
from btclib.block.proof_of_work import DIFFICULTY_ADJUSTMENT_INTERVAL
from btclib.exceptions import BTClibValueError

from btclib_node.chains import Chain, Main, RegTest, TestNet
from btclib_node.chainstate.contextual import (
    MEDIAN_TIME_SPAN,
    ParentOf,
    assert_valid_in_context,
    block_time,
    median_time_past,
    next_bits_required,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# the mainnet genesis' timestamp, so that every header below is one
# `BlockHeader.assert_valid` accepts
EPOCH = datetime.fromtimestamp(1231006505, UTC)

POW_LIMIT = Main().pow_limit_bits
# 2^216, three bytes into the compact form and a quarter and four times
# of it as well, so a retarget's answer is read off the exponent
HARD = b"\x1c\x01\x00\x00"


def at(seconds: int) -> int:
    """Return the block time of a header `seconds` after the epoch above."""
    return int(EPOCH.timestamp()) + seconds


def a_header(previous_block_hash: bytes, time: int, bits: bytes) -> BlockHeader:
    return BlockHeader(
        version=70015,
        previous_block_hash=previous_block_hash,
        merkle_root=secrets.token_bytes(32),
        time=EPOCH + timedelta(seconds=time),
        bits=bits,
        nonce=1,
        check_validity=False,
    )


def a_chain(
    times: Sequence[int], bits: Sequence[bytes]
) -> tuple[list[BlockHeader], ParentOf]:
    """Return headers at heights 0, 1, ... and the walk back over them."""
    headers: list[BlockHeader] = []
    previous_block_hash = b"\x00" * 32
    for time, header_bits in zip(times, bits, strict=True):
        header = a_header(previous_block_hash, time, header_bits)
        headers.append(header)
        previous_block_hash = header.hash
    by_hash = {header.hash: header for header in headers}
    return headers, lambda header: by_hash[header.previous_block_hash]


def test_a_header_is_weighed_by_the_second_it_serializes_as() -> None:
    # the four bytes hold a whole second, so a header carrying a
    # fraction of one is compared as it goes on the wire
    header = a_header(b"\x00" * 32, 0, POW_LIMIT)
    header.time += timedelta(microseconds=999999)
    assert block_time(header) == at(0)


def test_the_median_of_a_chain_shorter_than_the_window() -> None:
    # below height ten the window is the whole chain, and the middle of
    # an even number of times is the later of the two middle ones
    headers, parent_of = a_chain([0, 10, 20, 30], [POW_LIMIT] * 4)
    assert median_time_past(headers[0], 0, parent_of) == at(0)
    assert median_time_past(headers[1], 1, parent_of) == at(10)
    assert median_time_past(headers[3], 3, parent_of) == at(20)


def test_the_median_of_the_eleven_is_not_the_last_of_them() -> None:
    # a miner's timestamp is its own, so the times of a window are not
    # sorted and the median is what the rule asks for
    times = [0, 1000, 2000, 300, 400, 500, 600, 700, 800, 900, 100, 950]
    headers, parent_of = a_chain(times, [POW_LIMIT] * len(times))
    window = sorted(times[1:])
    assert median_time_past(headers[-1], len(times) - 1, parent_of) == at(
        window[MEDIAN_TIME_SPAN // 2]
    )


def test_a_chain_that_does_not_retarget_keeps_the_target_it_has() -> None:
    # regtest, where every height is answered with the parent's target
    # and neither the interval nor the min-difficulty rule is reached
    headers, parent_of = a_chain([0, 600], [HARD, HARD])
    for parent_height in (1, DIFFICULTY_ADJUSTMENT_INTERVAL - 1):
        assert (
            next_bits_required(
                RegTest(), headers[1], parent_height, at(1200), parent_of
            )
            == HARD
        )


def test_between_two_retargets_the_target_is_the_parent_s() -> None:
    headers, parent_of = a_chain([0, 600], [HARD, HARD])
    assert next_bits_required(Main(), headers[1], 1, at(1200), parent_of) == HARD


def test_a_slow_block_on_a_min_difficulty_chain_may_be_mined_at_the_limit() -> None:
    # testnet's rule: more than two target spacings after its parent and
    # the header may carry the network's easiest target
    headers, parent_of = a_chain([0, 600], [HARD, HARD])
    assert (
        next_bits_required(TestNet(), headers[1], 1, at(1801), parent_of) == POW_LIMIT
    )
    # and one second sooner than that is not enough
    assert next_bits_required(TestNet(), headers[1], 1, at(1800), parent_of) == HARD


def test_the_block_after_a_min_difficulty_one_goes_back_to_the_real_target() -> None:
    # otherwise one slow block would make the rest of the period easy
    times = [0, 600, 1200, 1800]
    headers, parent_of = a_chain(times, [HARD, HARD, POW_LIMIT, POW_LIMIT])
    assert next_bits_required(TestNet(), headers[-1], 3, at(2400), parent_of) == HARD


def test_the_walk_back_for_the_real_target_stops_at_a_retarget_height() -> None:
    # a block at a multiple of the interval carries the target the
    # retarget handed out, whatever that target is
    times = [0, 600]
    headers, parent_of = a_chain(times, [POW_LIMIT, POW_LIMIT])
    height = DIFFICULTY_ADJUSTMENT_INTERVAL
    assert (
        next_bits_required(TestNet(), headers[1], height, at(1200), parent_of)
        == POW_LIMIT
    )


def test_the_walk_back_for_the_real_target_stops_at_the_genesis() -> None:
    # a chain mined at the limit from its first block has no other
    # target to go back to
    headers, parent_of = a_chain([0, 600], [POW_LIMIT, POW_LIMIT])
    assert (
        next_bits_required(TestNet(), headers[1], 1, at(1200), parent_of) == POW_LIMIT
    )


def a_period(spacing: int, bits: bytes = HARD) -> tuple[list[BlockHeader], ParentOf]:
    """Return a difficulty period ending at height 2015, evenly spaced."""
    times = [spacing * height for height in range(DIFFICULTY_ADJUSTMENT_INTERVAL)]
    return a_chain(times, [bits] * DIFFICULTY_ADJUSTMENT_INTERVAL)


def test_a_period_that_took_the_two_weeks_it_aims_at_leaves_the_target_alone() -> None:
    # the window is measured from height 0 and not from height 1, which
    # is the off-by-one Core keeps: a period read from the block after
    # the first would answer 1c00ffdf here
    headers, parent_of = a_period(600)
    headers[-1].time = EPOCH + timedelta(seconds=14 * 24 * 60 * 60)
    last = DIFFICULTY_ADJUSTMENT_INTERVAL - 1
    assert next_bits_required(Main(), headers[-1], last, at(0), parent_of) == HARD


def test_a_period_mined_too_slowly_moves_the_target_by_four_and_no_more() -> None:
    # 3000 seconds a block is over eight weeks, and the timespan is
    # clamped to four times two weeks before it scales the target: 2^216
    # becomes 2^218
    headers, parent_of = a_period(3000)
    last = DIFFICULTY_ADJUSTMENT_INTERVAL - 1
    assert (
        next_bits_required(Main(), headers[-1], last, at(0), parent_of)
        == b"\x1c\x04\x00\x00"
    )


def test_a_period_mined_too_fast_moves_the_target_by_a_quarter_and_no_more() -> None:
    # and clamped the other way, at a quarter of two weeks: 2^216
    # becomes 2^214
    headers, parent_of = a_period(100)
    last = DIFFICULTY_ADJUSTMENT_INTERVAL - 1
    assert (
        next_bits_required(Main(), headers[-1], last, at(0), parent_of)
        == b"\x1b\x40\x00\x00"
    )


def test_a_retarget_stops_at_the_easiest_target_the_network_allows() -> None:
    # 2^222 quadrupled is over mainnet's limit, and a target above the
    # limit is work no network asked for
    headers, parent_of = a_period(3000, b"\x1d\x00\x40\x00")
    last = DIFFICULTY_ADJUSTMENT_INTERVAL - 1
    assert next_bits_required(Main(), headers[-1], last, at(0), parent_of) == POW_LIMIT


def a_parent(chain: Chain) -> tuple[BlockHeader, ParentOf]:
    headers, parent_of = a_chain([0, 600], [chain.pow_limit_bits] * 2)
    return headers[1], parent_of


def test_a_header_at_the_target_the_chain_requires_is_accepted() -> None:
    chain = RegTest()
    parent, parent_of = a_parent(chain)
    header = a_header(parent.hash, 1200, chain.pow_limit_bits)
    assert_valid_in_context(chain, header, parent, 1, parent_of, datetime.now(UTC))


def test_a_header_at_another_target_than_the_chain_requires_is_refused() -> None:
    chain = RegTest()
    parent, parent_of = a_parent(chain)
    header = a_header(parent.hash, 1200, HARD)
    with pytest.raises(BTClibValueError, match="target not the required one"):
        assert_valid_in_context(chain, header, parent, 1, parent_of, datetime.now(UTC))


def test_a_header_no_later_than_the_median_before_it_is_refused() -> None:
    chain = RegTest()
    parent, parent_of = a_parent(chain)
    # the median of the genesis and its child is the child's own time
    header = a_header(parent.hash, 600, chain.pow_limit_bits)
    with pytest.raises(BTClibValueError, match="not after the median past"):
        assert_valid_in_context(chain, header, parent, 1, parent_of, datetime.now(UTC))


def test_a_header_too_far_ahead_of_the_clock_is_refused() -> None:
    chain = RegTest()
    parent, parent_of = a_parent(chain)
    header = a_header(parent.hash, 1200, chain.pow_limit_bits)
    # the clock a node reads, put a day behind the header
    now = header.time - timedelta(days=1)
    with pytest.raises(BTClibValueError, match="too far in the future"):
        assert_valid_in_context(chain, header, parent, 1, parent_of, now)
