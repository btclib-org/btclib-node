# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A stored block served as its octets, as the parse path would serve it.

`strip_witnesses` must write what `Block.serialize(include_witness=False)`
writes of the parsed block, and `assert_well_formed` and `strip_witnesses`
must refuse what `Block.parse` refuses, with its exception. The vectors are
the mainnet blocks `chainstate/mainnet_blocks_test.py` reads.
"""

import bz2
import struct
from compression import zstd
from pathlib import Path

import pytest
from btclib.block import Block
from btclib.block.limits import MAX_BLOCK_WEIGHT, MIN_SERIALIZABLE_TRANSACTION_WEIGHT
from btclib.consensus import MAX_WITNESS_STACK_ITEMS
from btclib.exceptions import BTClibValueError
from btclib.script.witness import Witness
from btclib.tx import Tx, TxIn, TxOut
from btclib.tx.out_point import OutPoint
from hypothesis import given
from hypothesis import strategies as st

from btclib_node.chains import RegTest
from btclib_node.p2p.raw_block import (
    RawBlock,
    assert_well_formed,
    strip_witnesses,
)

_MAX_TRANSACTIONS = MAX_BLOCK_WEIGHT // MIN_SERIALIZABLE_TRANSACTION_WEIGHT
_DATA = Path(__file__).parents[1] / "chainstate" / "_data"
_MAGIC_AND_LENGTH = 8


def stored_blocks(name: str) -> list[bytes]:
    """Return the blocks of one btcd fixture as the octets it holds."""
    data = bz2.decompress((_DATA / "btcd" / f"{name}.dat.bz2").read_bytes())
    blocks = []
    offset = 0
    while offset < len(data):
        (length,) = struct.unpack_from("<I", data, offset + 4)
        offset += _MAGIC_AND_LENGTH
        blocks.append(data[offset : offset + length])
        offset += length
    return blocks


def vectors() -> list[bytes]:
    """Return every mainnet block the tree holds, as stored."""
    return [
        *stored_blocks("blk_0_to_4"),
        *stored_blocks("blk_3A"),
        *stored_blocks("277647"),
        zstd.decompress((_DATA / "floresta" / "raw.zst").read_bytes()),
    ]


def legacy(raw: bytes) -> bytes:
    """Return `raw` as the parse path writes it for a `MSG_BLOCK`."""
    return Block.parse(raw, check_validity=False).serialize(
        include_witness=False, check_validity=False
    )


def test_the_vectors_include_a_block_carrying_witnesses() -> None:
    """Without one the stripping below would prove nothing."""
    assert any(Block.parse(raw).is_segwit for raw in vectors())


@pytest.mark.parametrize("raw", vectors(), ids=lambda raw: raw[:80].hex()[-24:])
def test_a_mainnet_block_is_stripped_as_it_is_serialized(raw: bytes) -> None:
    """The stripped octets are the legacy serialization."""
    assert strip_witnesses(raw) == legacy(raw)
    assert_well_formed(raw)


def test_the_octets_are_served_as_they_are() -> None:
    """A `RawBlock` is the `block` message, whatever the check flag says."""
    payload = RawBlock(b"\x01\x02")
    assert payload.command == "block"
    assert payload.serialize() == b"\x01\x02"
    assert payload.serialize(check_validity=False) == b"\x01\x02"


_UINT32 = st.integers(min_value=0, max_value=2**32 - 1)
# a script or an item long enough for a two-octet CompactSize
_longest = 300
_scripts = st.one_of(st.binary(max_size=8), st.binary(min_size=253, max_size=_longest))


def a_tx_in(
    tx_id: bytes, vout: int, script: bytes, sequence: int, stack: list[bytes]
) -> TxIn:
    """Build an input, checked by nothing."""
    return TxIn(
        OutPoint(tx_id, vout, check_validity=False),
        script,
        sequence,
        Witness(stack, check_validity=False),
        check_validity=False,
    )


def a_tx_out(value: int, script: bytes) -> TxOut:
    """Build an output, checked by nothing."""
    return TxOut(value, script, check_validity=False)


def a_tx(version: int, lock_time: int, vin: list[TxIn], vout: list[TxOut]) -> Tx:
    """Build a transaction, checked by nothing."""
    return Tx(version, lock_time, vin, vout, check_validity=False)


_inputs = st.builds(
    a_tx_in,
    st.binary(min_size=32, max_size=32),
    _UINT32,
    _scripts,
    _UINT32,
    st.lists(_scripts, max_size=3),
)
_outputs = st.builds(a_tx_out, st.integers(min_value=0, max_value=2**63 - 1), _scripts)
_transactions = st.builds(
    a_tx,
    _UINT32,
    _UINT32,
    st.lists(_inputs, min_size=1, max_size=3),
    st.lists(_outputs, max_size=3),
)
_HEADER = RegTest().genesis_block.header


@st.composite
def blocks(draw: st.DrawFn) -> Block:
    """Draw a block of transactions, some with witnesses and some without."""
    transactions = draw(st.lists(_transactions, min_size=1, max_size=4))
    return Block(_HEADER, transactions, check_validity=False)


@given(blocks())
def test_any_block_is_stripped_as_it_is_serialized(block: Block) -> None:
    """The property over drawn blocks: counts and items of every width."""
    raw = block.serialize(include_witness=True, check_validity=False)
    assert strip_witnesses(raw) == block.serialize(
        include_witness=False, check_validity=False
    )
    assert_well_formed(raw)


def outcome(raw: bytes) -> bytes | None:
    """Return what the parse path writes for `MSG_BLOCK`, or `None`."""
    try:
        return legacy(raw)
    except BTClibValueError:
        return None


def mutated(raw: bytes, edit: tuple[int, int, bytes]) -> bytes:
    """Replace `edit[1]` octets at `edit[0]` (cut to fit) by `edit[2]`."""
    position = edit[0] % (len(raw) + 1)
    return raw[:position] + edit[2] + raw[position + edit[1] :]


@given(
    blocks(),
    st.tuples(st.integers(min_value=0), st.integers(0, 3), st.binary(max_size=3)),
)
def test_octets_are_refused_where_the_parse_path_refuses_them(
    block: Block, edit: tuple[int, int, bytes]
) -> None:
    """A block with an edit is stripped or refused as `Block.parse` has it."""
    raw = mutated(block.serialize(include_witness=True, check_validity=False), edit)
    expected = outcome(raw)
    if expected is None:
        with pytest.raises(BTClibValueError):
            strip_witnesses(raw)
        with pytest.raises(BTClibValueError):
            assert_well_formed(raw)
    else:
        assert strip_witnesses(raw) == expected
        assert_well_formed(raw)


@pytest.mark.parametrize("raw", vectors()[-1:], ids=["segwit"])
def test_every_truncation_of_a_block_is_refused(raw: bytes) -> None:
    """A block cut short, or with a byte after it, is refused."""
    for size in (*range(200), *range(len(raw) - 200, len(raw))):
        with pytest.raises(BTClibValueError):
            strip_witnesses(raw[:size])
    with pytest.raises(BTClibValueError):
        assert_well_formed(raw + b"\x00")


def a_marked_tx(witness: Witness, marker: bytes = b"\x00\x01") -> bytes:
    """Return a transaction with `marker` after its version."""
    tx_in = TxIn(OutPoint(bytes(32), 0), b"", 0, witness, check_validity=False)
    legacy_tx = Tx(1, 0, [tx_in], [TxOut(0, b"")], check_validity=False).serialize(
        include_witness=False, check_validity=False
    )
    body = legacy_tx[4:-4]
    stack = witness.serialize(check_validity=False)
    return legacy_tx[:4] + marker + body + stack + legacy_tx[-4:]


def a_block_of(*transactions: bytes) -> bytes:
    """Return the raw `transactions` under the regtest genesis header."""
    return (
        _HEADER.serialize(check_validity=False)
        + bytes([len(transactions)])
        + b"".join(transactions)
    )


def test_a_marker_over_no_witness_is_refused() -> None:
    """Core's "Superfluous witness record", refused by the parse path too."""
    raw = a_block_of(a_marked_tx(Witness([], check_validity=False)))
    assert outcome(raw) is None
    with pytest.raises(BTClibValueError, match="superfluous"):
        strip_witnesses(raw)


def test_a_marker_over_an_empty_item_is_a_witness() -> None:
    """One empty item is a stack, so the transaction is stripped."""
    raw = a_block_of(a_marked_tx(Witness([b""], check_validity=False)))
    assert strip_witnesses(raw) == legacy(raw)


def test_an_unknown_flag_is_refused() -> None:
    """A zero marker over a flag above one is "unknown optional data"."""
    raw = a_block_of(a_marked_tx(Witness([b"\x01"], check_validity=False), b"\x00\x02"))
    assert outcome(raw) is None
    with pytest.raises(BTClibValueError, match="optional data"):
        strip_witnesses(raw)


def test_a_non_canonical_count_is_refused() -> None:
    """A count written wider than it needs is two encodings of one block."""
    raw = a_block_of(a_marked_tx(Witness([b"\x01"], check_validity=False)))
    wide = raw[:80] + b"\xfd\x01\x00" + raw[81:]
    assert outcome(wide) is None
    with pytest.raises(BTClibValueError, match="non-canonical"):
        strip_witnesses(wide)


def test_a_count_above_what_a_block_can_hold_is_refused() -> None:
    """One transaction more than a block can hold is not walked."""
    count = _MAX_TRANSACTIONS + 1
    raw = _HEADER.serialize(check_validity=False) + b"\xfe"
    raw += count.to_bytes(4, "little")
    assert outcome(raw) is None
    with pytest.raises(BTClibValueError, match="too big"):
        strip_witnesses(raw)


def test_a_witness_stack_above_the_item_bound_is_refused() -> None:
    """One item more than a witness stack may hold is not walked."""
    tx = a_marked_tx(Witness([b"\x01"], check_validity=False))
    items = b"\xfe" + (MAX_WITNESS_STACK_ITEMS + 1).to_bytes(4, "little")
    raw = a_block_of(tx[:-7] + items + tx[-4:])
    assert outcome(raw) is None
    with pytest.raises(BTClibValueError, match="too big"):
        strip_witnesses(raw)


def test_a_header_cut_short_is_refused() -> None:
    """Fewer than eighty octets is no header."""
    with pytest.raises(BTClibValueError, match="decoded length"):
        strip_witnesses(bytes(79))
