# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""An atheris harness fuzzing this node's own p2p framing loop.

`Connection.parse_messages` peeks a header's own `length` field before
building a stream at all, then loops `Message.parse` over whatever
whole messages the buffer already holds and checks each one's magic
against the chain this node runs -- hostile-input arithmetic of this
tree's own, ahead of `btclib`'s own wire codec, that
btclib-org/btclib-node#516 asks this directory to cover the way
`fuzz_reject.py` already covers `Reject.parse`.

`p2p.connection.frame_message_bytes` is that arithmetic pulled into a
function of octets alone -- `p2p.connection`'s own module docstring is
where the extraction is argued against
`p2p_transport_serialization.cpp`, Core's matching fuzz target for its
own `V1Transport` (at bitcoin/bitcoin@ca7162cde5): a transport object
built with no socket, fed raw bytes directly, is the shape a framing
loop's own fuzz target takes there, and is what `Connection.run` itself
cannot be reduced to without first owning a socket, a manager and a
node -- issue #516's own three-way split, and why this harness stops
at framing rather than reaching `Connection.run`.

`BTClibException` is the whole family `Connection.run`'s own catch
(`p2p/connection.py`) discourages a peer on: `IncompleteMessageError`
for a message `stream` does not yet hold whole, `btclib`'s own parse
refusals from `Message.parse` itself, and `WrongNetworkMagicError` for
a message whose magic names a chain other than the one this node runs.
Suppressing that family alone is what makes this harness report the
same way `fuzz_reject.py` does: what leaves `fuzz_target` below is then
either a crash or a refusal outside the family `Connection.run` itself
tolerates from a peer, and each is a finding.

`fuzz.yml` runs this file as an ordinary script under the interpreter
`.python-version` pins, the same way it runs `fuzz_reject.py` -- see
that file's own docstring for why not a container.
"""

from __future__ import annotations

import contextlib
import sys

import atheris
from btclib.exceptions import BTClibException

from btclib_node.p2p.connection import frame_message_bytes

# tests/fuzz_corpus_test.py reads this with ast.literal_eval rather than
# by importing this module: atheris above is installed only by the
# `fuzz` dependency group, which nothing but fuzz.yml asks for, so the
# suite must not execute this file
ENTRY_POINTS = ("btclib_node.p2p.connection:frame_message_bytes",)


def fuzz_target(data: bytes) -> None:
    """Frame `data` as one wire message, the way `parse_messages` does.

    Atheris reports a failure on any exception leaving this function,
    so what is suppressed here is what `frame_message_bytes` refusing
    an input looks like; the module docstring above is where that
    family is argued.
    """
    with contextlib.suppress(BTClibException):
        frame_message_bytes(data)


def main() -> None:
    """Wire `fuzz_target` to libFuzzer through atheris."""
    atheris.instrument_all()
    atheris.Setup(sys.argv, fuzz_target)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
