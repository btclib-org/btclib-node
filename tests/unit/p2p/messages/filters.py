# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What the filter payloads do, which is nothing at all.

Each parses without reading a byte of its stream and serializes to an
empty payload, so a peer sent one of these would be sent a message it
cannot act on, and a peer that sent one would have it silently thrown
away. That is btclib-org/btclib-node#50, which proposes deleting the
module; what is written here names the behaviour rather than endorsing
it, and keeps the file in the coverage report -- excusing it from the
report instead would hide whatever is added to it next.

The command each of them travels under is pinned in this package's
__init__, which is what imports the module.
"""

from io import BytesIO

import pytest

from btclib_node.p2p.messages.filters import (
    Feefilter,
    Filteradd,
    Filterclear,
    Filterload,
    Merkleblock,
)

PAYLOADS = [Feefilter, Filteradd, Filterclear, Filterload, Merkleblock]


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: p.command)
def test_a_filter_payload_serializes_to_no_octets(payload):
    assert payload().serialize() == b""


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: p.command)
def test_a_filter_payload_parses_without_reading_its_stream(payload):
    stream = BytesIO(b"\xff" * 32)
    assert payload.parse(stream) == payload()
    # not one byte consumed: whatever the peer sent is dropped, and the
    # stream is left where the next payload would be read from
    assert stream.tell() == 0
