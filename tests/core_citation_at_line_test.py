# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A Core citation keeps `at` on the sha's own physical line.

`CONTRIBUTING.md`'s *Following Bitcoin Core* asks for it, because ruff's
`ERA001` reads one comment line at a time: a line that opens with
`bitcoin/bitcoin@<sha>` and nothing before it reads as code, and is what
the sweep of btclib-org/btclib-node#1517 removed before it came back
(btclib-org/btclib-node#2209). A citation that wraps therefore breaks
after `at`, never before it.
"""

from __future__ import annotations

from pathlib import Path

TREE = Path(__file__).parents[1]


def test_no_comment_line_opens_with_a_core_sha() -> None:
    """`git grep -n -P '^[ ]*# bitcoin/bitcoin@' -- src` prints nothing."""
    offenders = [
        f"{path.relative_to(TREE)}:{number}"
        for path in sorted((TREE / "src").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.lstrip().startswith("# bitcoin/bitcoin@")
    ]
    assert not offenders, offenders
