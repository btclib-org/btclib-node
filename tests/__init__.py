# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What the test suite shares, and how a vendored vector is read.

Vectors live in `tests/**/_data/` and are vendored rather than fetched:
a test that downloads its own input is a test whose verdict depends on
somebody else's uptime, and one that regenerates its input is a test
that agrees with whatever this tree already does. `tests/_data/README.md`
says where each file came from, pinned to a commit and to a blob.
"""

import json
import re
from pathlib import Path
from typing import Any

_TESTS_DIR = Path(__file__).parent


def load(*relative_path: str, encoding: str = "ascii") -> Any:
    """Read a vendored JSON vector file, named relative to `tests/`.

    Naming a vector file by its path from the test suite root, rather
    than from the test module that reads it, is what lets two modules
    share one file without the `parent.parent` walk that breaks the
    moment a test module moves.
    """
    with _TESTS_DIR.joinpath(*relative_path).open(encoding=encoding) as file_:
        return json.load(file_)


# what makes an id unreadable in a report and unusable in a -k
# expression: anything that is not a letter or a digit. Bitcoin Core's
# own notes hold spaces, commas and parentheses
_NOT_IN_AN_ID = re.compile(r"[^0-9A-Za-z]+")


def vector_id(index: int, *description: object) -> str:
    """Name the vector at `index`: where it is, then what it is about.

    The position alone is what parametrize generates on its own, and it
    says where in the file to look but not what the case was testing;
    the description alone -- a height, one of Core's notes -- reads well
    but is neither unique nor always there. Both, so that the red line
    of a report identifies the vector and says what it is.
    """
    text = "-".join(str(d) for d in description if d)
    text = _NOT_IN_AN_ID.sub("-", text).strip("-")
    return f"{index}-{text[:60]}" if text else str(index)
