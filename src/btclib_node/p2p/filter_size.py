# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""One BIP158 basic filter's own size, estimated once rather than twice.

`connection.py`'s own `MAX_QUEUED_SEND_BYTES` and `callbacks.py`'s own
`MAX_CFILTERS_INFLIGHT_BYTES` (`get_cfilters`'s pacing bound) both need
a peer-facing filter's size to size a bound from. A module of its own
is what keeps that estimate in one place rather than in both, where the
two could silently drift apart -- `connection.py` already imports
`callbacks.py` for `handshake_callbacks`, so `callbacks.py` importing
back from `connection.py` would cycle, and neither module's own job is
to hold a number the other one needs too.
"""

# Bytes per filter element, measured rather than guessed: averaging
# `btclib.block.block_filter._golomb_encode` (`BASIC_FILTER_P=19`,
# `BASIC_FILTER_M=784931`) over synthetic element counts from 2,000 to
# `MAX_FILTER_ELEMENT_COUNT` gives about 2.632 bytes, stable across
# scales -- the cost is the Golomb-Rice parameter's, not the elements'.
BYTES_PER_FILTER_ELEMENT = 2.632

# A real block anchors the element count instead of guessing that too:
# height 481824 (btclib's own `tests/block/_data/block_481824.bin`,
# 988,519 on-wire bytes) parses to 1,866 transactions, 4,124 outputs and
# 5,192 non-coinbase inputs -- 9,316 elements before the OP_RETURN
# exclusion and the deduplication `BasicBlockFilter.from_block` applies,
# both of which only lower the true count -- for about 24.5 KB of
# filter. That block is from 2017; four times its element count stands
# in for a block nearer today's without reaching for the 111,111-element
# theoretical ceiling (`MAX_FILTER_ELEMENT_COUNT`) itself, which no
# chain this node could serve has ever produced 1,000 of in a row.
ELEMENTS_PER_BUSY_MODERN_BLOCK = 4 * 9316

# One such filter, in bytes -- about 98,079.
ONE_BUSY_MODERN_BLOCK_FILTER_BYTES = (
    ELEMENTS_PER_BUSY_MODERN_BLOCK * BYTES_PER_FILTER_ELEMENT
)
