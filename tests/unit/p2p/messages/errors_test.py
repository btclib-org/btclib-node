# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Reject`, BIP61's message: its round trip, its byte order, its refusals."""

import contextlib

import pytest
from btclib.exceptions import BTClibException
from btclib.p2p.inventory import Inventory, InventoryType
from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.exceptions import InvalidRejectPayloadError
from btclib_node.p2p.messages.errors import Reject, RejectCode

MAGIC = RegTest().magic
TXID = bytes(range(32))  # not a palindrome: a symmetric hash tells nothing
# BIP61's two shapes: the common payload a version reject ends after,
# and the hash a tx or block reject appends to it
NO_HASH = Reject("version", RejectCode.obsolete, "too old", b"").serialize()
WITH_HASH = Reject("tx", RejectCode.insufficientfee, "min fee", TXID).serialize()


def test_reject() -> None:
    """A `Reject` framed onto the wire and parsed back is the same object."""
    msg = Reject("tx", RejectCode(0x42), "", TXID)
    msg_bytes = msg.to_message(MAGIC).serialize()
    assert msg == Reject.parse(Message.parse(msg_bytes).payload)


def test_a_reject_puts_a_hash_on_the_wire_the_way_an_inventory_does() -> None:
    """`Reject.data`'s byte order on the wire matches `Inventory`'s hash.

    Both classes hold their hash displayed, as everywhere else in this
    tree, and both reverse it on serialization -- checked here by
    comparing the trailing 32 bytes each puts on the wire for the same
    transaction, rather than trusting that two independent reversals
    agree just because each is internally consistent.
    """
    # a round trip holds whichever way round the two sides agree on;
    # this is which way round that is, and it is the one everything
    # else in the protocol uses
    reject = Reject("tx", RejectCode.invalid, "", TXID)
    inventory = Inventory(InventoryType.MSG_TX, TXID)
    assert reject.serialize()[-32:] == inventory.serialize()[-32:]


def test_a_reject_carrying_no_hash_round_trips() -> None:
    """BIP61's version reject ends after `reason`, and is accepted there.

    The field is the one the specification calls optional, so an empty
    `data` is a payload a peer legitimately sends rather than one this
    parser tolerates.
    """
    assert Reject.parse(NO_HASH) == Reject(
        "version", RejectCode.obsolete, "too old", b""
    )


@pytest.mark.parametrize("length", range(len(NO_HASH)))
def test_a_payload_cut_short_is_refused(length: int) -> None:
    """No proper prefix of a reject is itself a reject.

    `BytesIO.read` answers a stream that has run out with what is left,
    so every field's own read is what has to refuse the payload that
    does not hold it -- checked over every cut rather than over the one
    that happens to be interesting.
    """
    with pytest.raises(BTClibException):
        Reject.parse(NO_HASH[:length])


@pytest.mark.parametrize("length", [1, 31, 33])
def test_a_hash_neither_absent_nor_whole_is_refused(length: int) -> None:
    """What follows `reason` is a 32-octet hash or nothing at all.

    A hash cut short would otherwise parse as the absent one BIP61's
    version reject carries, and octets past it as a payload that
    reserializes to less than the peer sent.
    """
    payload = NO_HASH + TXID[:length] if length < 32 else NO_HASH + TXID + b"\x00"
    with pytest.raises(InvalidRejectPayloadError, match="a hash is 32 octets"):
        Reject.parse(payload)


@pytest.mark.parametrize("field", ["message", "reason"])
def test_a_var_str_no_utf8_decodes_is_refused(field: str) -> None:
    """Both of a reject's strings refuse octets no utf-8 decodes.

    The two octets 0xff 0xfe are what `str.decode` raises
    `UnicodeDecodeError` on, a `ValueError` and so outside the family
    `handle_p2p` discourages a peer on until `parse` answers for it.
    """
    bad = b"\x02\xff\xfe"
    payload = bad + b"\x11\x00" if field == "message" else b"\x00\x11" + bad
    with pytest.raises(InvalidRejectPayloadError, match=f"{field} is not utf-8"):
        Reject.parse(payload)


def test_a_code_bip61_does_not_name_is_refused() -> None:
    """`RejectCode` holds the codes BIP61's own tables name, and no others."""
    with pytest.raises(InvalidRejectPayloadError, match="not a BIP61 reject code"):
        Reject.parse(b"\x00\x99\x00")


def test_no_mutation_of_a_payload_refuses_outside_the_suppressed_family() -> None:
    """What `fuzz/fuzz_reject.py` suppresses is what `parse` raises.

    The harness suppresses `BTClibException` alone, so any other
    exception leaving `parse` is what the sentinel reports; this is
    that property over the population a fuzzer's first mutations reach
    -- every prefix of a valid payload, and every single octet of one
    replaced by each of the values a byte takes.
    """
    candidates = [WITH_HASH[:length] for length in range(len(WITH_HASH) + 1)]
    candidates += [
        WITH_HASH[:offset] + bytes([value]) + WITH_HASH[offset + 1 :]
        for offset in range(len(WITH_HASH))
        for value in range(256)
    ]
    assert len(candidates) > len(WITH_HASH)
    for candidate in candidates:
        # an escape fails this test as the exception it is, which is
        # what the harness does with the same octets
        with contextlib.suppress(BTClibException):
            Reject.parse(candidate)
