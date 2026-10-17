# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A stored block served as the octets it is stored in.

`BlockDB` keeps a block as its witness serialization. A `getdata` for
`MSG_WITNESS_BLOCK` is answered with those octets, and one for `MSG_BLOCK`
with the same octets less the witnesses, so neither needs the block parsed
into objects and written out again.

`assert_well_formed` and `strip_witnesses` walk the octets once and refuse
what `Block.parse` refuses with `check_validity=False`, with the same
`BTClibValueError`: a block that does not parse is not served. Core serves
a stored witness block unread (`ReadRawBlock` in `ProcessGetBlockData`,
`src/net_processing.cpp` at bitcoin/bitcoin@9be056a8a7); this node checks
it first.
"""

from dataclasses import dataclass
from typing import ClassVar, override

from btclib.block.limits import MAX_BLOCK_WEIGHT, MIN_SERIALIZABLE_TRANSACTION_WEIGHT
from btclib.consensus import MAX_WITNESS_STACK_ITEMS
from btclib.exceptions import BTClibValueError
from btclib.p2p.payload import Payload
from btclib.var_int import MAX_SIZE

__all__ = [
    "RawBlock",
    "assert_well_formed",
    "strip_witnesses",
]

_HEADER_SIZE = 80
_MAX_TRANSACTIONS = MAX_BLOCK_WEIGHT // MIN_SERIALIZABLE_TRANSACTION_WEIGHT
_OUTPOINT_SIZE = 36
_SEQUENCE_SIZE = 4
_VALUE_SIZE = 8
_UINT32_SIZE = 4
# BIP144's marker and flag, and the least flag Core refuses as unknown
_SEGWIT_MARKER = b"\x00\x01"
_MARKER_SIZE = 2
_UNKNOWN_FLAG = 2
# the first octet of a CompactSize above 252 gives the octets that follow
# it and the least value that prefix may encode, so that a value has one
# encoding only
_ONE_OCTET_LIMIT = 0xFD
_WIDE = {0xFD: (2, 0xFD), 0xFE: (4, 0x0001_0000), 0xFF: (8, 0x0001_0000_0000)}


@dataclass(frozen=True)
class RawBlock(Payload):
    """A `block` message carrying octets already serialized."""

    command: ClassVar[str] = "block"

    data: bytes

    @override
    def serialize(self, *, check_validity: bool = True) -> bytes:
        """Return the octets this was built with."""
        return self.data


def _wide(raw: bytes, i: int, limit: int) -> tuple[int, int]:
    """Return the count a wide prefix at `i` begins, and where it ends."""
    size, minimum = _WIDE[raw[i]]
    end = i + 1 + size
    if end > len(raw):
        err_msg = "not enough binary data for var_int"
        raise BTClibValueError(err_msg)
    value = int.from_bytes(raw[i + 1 : end], "little")
    if value < minimum:
        err_msg = f"non-canonical var_int: {value}"
        raise BTClibValueError(err_msg)
    if value > limit:
        err_msg = f"var_int too big: {value}"
        raise BTClibValueError(err_msg)
    return value, end


def _count(raw: bytes, i: int, limit: int) -> tuple[int, int]:
    """Return the CompactSize at `i`, at most `limit`, and where it ends."""
    first = raw[i]
    # every limit is above the largest single octet
    if first < _ONE_OCTET_LIMIT:
        return first, i + 1
    return _wide(raw, i, limit)


def _skip_inputs(raw: bytes, i: int, inputs: int) -> int:
    """Return where `inputs` inputs starting at `i` end."""
    for _ in range(inputs):
        length, i = _count(raw, i + _OUTPOINT_SIZE, MAX_SIZE)
        i += length + _SEQUENCE_SIZE
    return i


def _skip_outputs(raw: bytes, i: int, outputs: int) -> int:
    """Return where `outputs` outputs starting at `i` end."""
    for _ in range(outputs):
        length, i = _count(raw, i + _VALUE_SIZE, MAX_SIZE)
        i += length
    return i


def _skip_witnesses(raw: bytes, i: int, inputs: int) -> tuple[int, bool]:
    """Return where `inputs` witnesses at `i` end, and if one holds an item."""
    carried = False
    for _ in range(inputs):
        items, i = _count(raw, i, MAX_WITNESS_STACK_ITEMS)
        carried = carried or items > 0
        for _ in range(items):
            length, i = _count(raw, i, MAX_SIZE)
            i += length
    return i, carried


def _is_segwit(raw: bytes, i: int) -> bool:
    """Whether a BIP144 marker and flag stand at `i`; refuse an unknown flag."""
    marker = raw[i : i + _MARKER_SIZE]
    if len(marker) == _MARKER_SIZE and marker[0] == 0 and marker[1] >= _UNKNOWN_FLAG:
        err_msg = "unknown transaction optional data"
        raise BTClibValueError(err_msg)
    return marker == _SEGWIT_MARKER


def _scan(raw: bytes, *, strip: bool) -> bytes:
    """Walk the block `raw`, and return it without its witnesses if `strip`.

    Raises `BTClibValueError` where `Block.parse` does: a header that is
    not eighty octets, a CompactSize that is not canonical or is above the
    bound `Block.parse` and its transactions set, a marker over no witness
    or over an unknown flag, and octets missing or left over. Without
    `strip` the walk only checks, and returns `raw`.
    """
    size = len(raw)
    if size < _HEADER_SIZE:
        err_msg = f"invalid decoded length: {size} instead of {_HEADER_SIZE}"
        raise BTClibValueError(err_msg)
    parts: list[bytes] = []
    try:
        transactions, i = _count(raw, _HEADER_SIZE, _MAX_TRANSACTIONS)
        run = 0
        for _ in range(transactions):
            start = i
            i += _UINT32_SIZE
            segwit = _is_segwit(raw, i)
            if segwit:
                i += _MARKER_SIZE
            body = i
            inputs, i = _count(raw, i, MAX_SIZE)
            i = _skip_inputs(raw, i, inputs)
            outputs, i = _count(raw, i, MAX_SIZE)
            i = _skip_outputs(raw, i, outputs)
            if segwit:
                witnesses = i
                i, carried = _skip_witnesses(raw, i, inputs)
                if not carried:
                    err_msg = "superfluous witness record"
                    raise BTClibValueError(err_msg)
                if strip:
                    # the version and the body stay, the marker and the
                    # witnesses go, the lock time is the next run's
                    parts.append(raw[run : start + _UINT32_SIZE])
                    parts.append(raw[body:witnesses])
                    run = i
            i += _UINT32_SIZE
    except IndexError as e:
        err_msg = "not enough binary data for the block"
        raise BTClibValueError(err_msg) from e
    if i != size:
        err_msg = f"{i} octets in the block, not {size}"
        raise BTClibValueError(err_msg)
    if not strip:
        return raw
    parts.append(raw[run:])
    return b"".join(parts)


def assert_well_formed(raw: bytes) -> None:
    """Raise `BTClibValueError` unless `Block.parse` reads `raw`."""
    _scan(raw, strip=False)


def strip_witnesses(raw: bytes) -> bytes:
    """Return `raw` as `Block.serialize(include_witness=False)` writes it."""
    return _scan(raw, strip=True)
