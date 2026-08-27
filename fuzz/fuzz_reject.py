# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""An atheris harness fuzzing BIP61's `reject` payload parser.

`Reject.parse` is the deserializer of a peer's own octets this tree
owns. `p2p.callbacks.reject` reads one with no signature check and no
verification of any kind in front of it, so a stranger's own choice of
octets reaches this parser directly -- the ranking btclib-org/.github#342
argues, an entry point reached from the network ahead of verification
being worth more than one reached only after it.

Everything else a peer's octets reach here is either `btclib`'s codec,
fuzzed by that repository's own harnesses, or a method over a connection
that owns a socket, a manager and a node; btclib-org/btclib-node#516 is
where the second of those is argued and is why this directory holds one
harness.

A crash is a defect in `parse` and never in this harness: `data` is
unconstrained octets handed straight to it, with nothing decoding,
validating or otherwise standing between the fuzzer's own choice of
octets and the parser.

What is suppressed below is what `parse` refuses today rather than what
it ought to refuse. `BTClibException` is `btclib.var_int.parse`'s own
refusal of octets no var_int carries. `ValueError` is this tree's own
two refusals, `str.decode` on a message no utf-8 encodes and
`RejectCode` on a code BIP61 does not name, and it covers the
`UnicodeDecodeError` of the first as a subclass;
btclib-org/btclib-node#515 is where that classification is argued, and
narrowing this suppression to `BTClibException` is what closes it from
this side.

`fuzz.yml` runs this file as an ordinary script under the interpreter
`.python-version` pins, and its header is where that is argued against
the container ClusterFuzzLite would build instead.
"""

from __future__ import annotations

import contextlib
import sys

import atheris
from btclib.exceptions import BTClibException

from btclib_node.p2p.messages.errors import Reject

# tests/fuzz_corpus_test.py reads this with ast.literal_eval rather than
# by importing this module: atheris above is installed only by the
# `fuzz` dependency group, which nothing but fuzz.yml asks for, so the
# suite must not execute this file
ENTRY_POINTS = ("btclib_node.p2p.messages.errors:Reject.parse",)


def fuzz_target(data: bytes) -> None:
    """Parse `data` as a `reject` payload, the way `p2p.callbacks` does.

    Atheris reports a failure on any exception leaving this function,
    so what is suppressed here is what `parse` refusing an input looks
    like; the module docstring above is where the two families are
    argued.
    """
    with contextlib.suppress(BTClibException, ValueError):
        Reject.parse(data)


def main() -> None:
    """Wire `fuzz_target` to libFuzzer through atheris."""
    atheris.instrument_all()
    atheris.Setup(sys.argv, fuzz_target)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
