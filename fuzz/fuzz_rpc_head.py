# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""An atheris harness fuzzing this node's own RPC request-head framing.

`RpcConnection.run` reads the header section of an HTTP request off an
unauthenticated socket bound to every interface (`rpc/connection.py`'s
own module docstring, issue #27), splits the request line from the
header fields, decides a `Content-Length` and a keep-alive default from
them -- hostile-input arithmetic of this tree's own, ahead of the
`json.loads` that decodes a request's body, which is stdlib's own
business and not this node's. `rpc.connection.parse_request_head` is
that arithmetic pulled into a function of octets alone, scoped the way
Core's own `http_request.cpp` fuzz target is scoped
(at bitcoin/bitcoin@ca7162cde5): a raw `http_buffer` fed to
`HTTPRequest::LoadControlData`/`LoadHeaders`/`LoadBody`, control-line
and header framing only, never reaching UniValue or JSON-RPC content
decoding -- `parse_request_head`'s own docstring is where that scoping
is argued in full, and why this is a second, narrower harness rather
than one that also drives `json.loads`, already exhaustively fuzzed
upstream by the interpreter's own test suite and not this tree's code.

`BTClibException` is the whole family `parse_request_head` refuses an
input with -- `IncompleteRequestHeadError` where no header terminator
is present yet, `MalformedRequestHeadError` for a `Content-Length` this
node will not honour -- and it is also what `RpcConnection.run`'s own
catch is a strict superset of: that catch is a bare `except Exception`
(`run`'s own docstring is where that breadth is argued, against
`RpcManager.stop`'s own sweep), so nothing this harness can find here
escapes unhandled in production, only unmeasured by this suite before
this issue. Suppressing that one family is what makes a green run
narrow enough to be evidence, the same way `fuzz_reject.py`'s own does.

`fuzz.yml` runs this file as an ordinary script under the interpreter
`.python-version` pins, the same way it runs `fuzz_reject.py` -- see
that file's own docstring for why not a container.
"""

from __future__ import annotations

import contextlib
import sys

import atheris
from btclib.exceptions import BTClibException

from btclib_node.rpc.connection import parse_request_head

# tests/fuzz_corpus_test.py reads this with ast.literal_eval rather than
# by importing this module: atheris above is installed only by the
# `fuzz` dependency group, which nothing but fuzz.yml asks for, so the
# suite must not execute this file
ENTRY_POINTS = ("btclib_node.rpc.connection:parse_request_head",)


def fuzz_target(data: bytes) -> None:
    """Parse `data` as a request's own header section.

    Atheris reports a failure on any exception leaving this function,
    so what is suppressed here is what `parse_request_head` refusing an
    input looks like; the module docstring above is where that family
    is argued.
    """
    with contextlib.suppress(BTClibException):
        parse_request_head(data)


def main() -> None:
    """Wire `fuzz_target` to libFuzzer through atheris."""
    atheris.instrument_all()
    atheris.Setup(sys.argv, fuzz_target)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
