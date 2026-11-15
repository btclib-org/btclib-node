# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What needs something this repository does not ship: a real `bitcoind`.

Every module here skips itself unless `BTCLIB_NODE_INTEGRATION` is set,
`conftest.py`'s own `bitcoind_path` fixture being where that is decided
and named. `tests/integration/` is in `testpaths` like every other suite
directory, so a bare run collects it and reports the skip by name, and
it is left out of the coverage ratchet, a body that skips itself being
an uncovered line rather than a defect. What actually runs these is
`integration-bitcoind.yml`, which fails if what it runs skips rather
than actually reaching a node. `tests/README.md` declares the split from
`unit/` and `functional/`, and why it exists.

`departed` gives this node's words for the bitcoind refusals whose shapes
it reads: a hash or hex string quoted, and a transaction that does not
decode.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# bitcoind's refusals that quote what the caller gave, each built as Core
# builds it: `ParseHashV` and `ParseHexV` (`src/rpc/util.cpp:127`, `:129`
# and `:141`), and `testmempoolaccept` and `submitpackage`
# (`src/rpc/mempool.cpp:390` and `:1461`), at bitcoin/bitcoin@f0de87f040,
# the v32.1 tag. The name is one of Core's, which holds no quote or
# parenthesis, and the caller's string is the rest of the message up to
# Core's last words, whatever it holds, an apostrophe or one of these
# messages included.
_NAME = r"(?P<name>[A-Za-z_][A-Za-z0-9_ ]*)"
_WRONG_LENGTH = re.compile(
    rf"{_NAME} must be of length 64 \(not (?P<length>\d+), for '.*'\)", re.DOTALL
)
_NOT_HEX = re.compile(rf"{_NAME} must be hexadecimal string \(not '.*'\)", re.DOTALL)
_DECODE_FAILED = re.compile(
    r"TX decode failed: (?P<raw>.*) Make sure the tx has at least one input\.",
    re.DOTALL,
)


def departed(message: str, transactions: Sequence[object] = ()) -> str:
    """Return this node's words where bitcoind refuses with `message`.

    `message` is read whole against each shape above, and the caller's
    string in it is never rewritten. A string where a hash or hex belongs
    is written `...`, and the item of `transactions` that does not decode
    is named by its index (SECURITY.md, "Where this node departs from
    Bitcoin Core"). Any other message is bitcoind's own.
    """
    if (wrong := _WRONG_LENGTH.fullmatch(message)) is not None:
        return (
            f"{wrong['name']} must be of length 64 (not {wrong['length']}, for '...')"
        )
    if (wrong := _NOT_HEX.fullmatch(message)) is not None:
        return f"{wrong['name']} must be hexadecimal string (not '...')"
    if (failed := _DECODE_FAILED.fullmatch(message)) is not None:
        index = list(transactions).index(failed["raw"])
        return (
            f"TX decode failed for tx {index}. Make sure the tx has at least one input."
        )
    return message
