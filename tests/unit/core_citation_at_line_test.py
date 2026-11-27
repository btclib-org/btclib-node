# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""No comment line under `src/` opens with a Core sha.

`CONTRIBUTING.md`'s *Following Bitcoin Core* says where a citation's `at`
goes, and why. This module fails on a line of a `.py` file under `src/`
that, past its indentation, opens with `# bitcoin/bitcoin@`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TREE = Path(__file__).parents[2]


def _opens_with_core_sha(line: str) -> bool:
    """Return whether `line`, past its indentation, opens with a Core sha."""
    return line.lstrip().startswith("# bitcoin/bitcoin@")


def _offenders(root: Path) -> list[str]:
    """Name each `.py` line under `root`'s `src/` that opens with a Core sha."""
    return [
        f"{path.relative_to(root).as_posix()}:{number}"
        for path in sorted((root / "src").rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _opens_with_core_sha(line)
    ]


@pytest.mark.parametrize(
    "line",
    [
        "        # bitcoin/bitcoin@f0de87f040, the v32.1 tag), so `inv_to_send`",
        "\t# bitcoin/bitcoin@f0de87f040, the v32.1 tag), so `inv_to_send`",
    ],
)
def test_a_wrapped_citation_is_found(line: str) -> None:
    """The line a wrap before `at` leaves, indented by spaces or a tab."""
    assert _opens_with_core_sha(line)


def test_at_on_the_sha_s_line_is_allowed() -> None:
    """The line the rule asks for."""
    line = "        # (at bitcoin/bitcoin@f0de87f040, the v32.1 tag), so `inv_to_send`"
    assert not _opens_with_core_sha(line)


def test_a_file_other_than_py_is_not_read(tmp_path: Path) -> None:
    """A binary file under `src/` stops no scan of the `.py` beside it."""
    src = tmp_path / "src"
    src.mkdir()
    (src / ".DS_Store").write_bytes(b"\x00\x87\xff")
    (src / "m.py").write_text(
        "x = 1\n    # bitcoin/bitcoin@f0de87f040, the v32.1 tag)\n", encoding="utf-8"
    )
    assert _offenders(tmp_path) == ["src/m.py:2"]


def test_no_comment_line_opens_with_a_core_sha() -> None:
    """Each line found is named as its path and line number."""
    offenders = _offenders(TREE)
    assert not offenders, offenders
