# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Every `pragma: no cover` gives its reason on the same line.

`pyproject.toml`'s `[tool.coverage.report]` states the rule and points
its check at section 8 of the organization's standard rather than
restating it, so that the two move together:
`git grep -nE 'pragma: no cover$' -- '*.py'` is what section 8 names,
and what this module runs, unchanged -- a site giving no reason, a
reason on the line above being out of its reach.

This sits at `tests/` root rather than under `tests/unit/`, on the same
ground as `interpreters_test.py`: the rule comes from `pyproject.toml`,
a file neither `src/btclib_node/` nor `tests/` owns, and the command
above reads every tracked `.py` file the same way regardless of which
of those two -- or of `.github/scripts/` or `fuzz/` -- it sits under,
so there is no module of `src/btclib_node/` this is a test *of*, and no
suite machinery it reads the way `unit/coverage_floor_test.py` reads
`conftest.py` or `unit/helpers_test.py` reads `tests/__init__.py`. The
rule is section 8's own rather than one of section 7's conventions
`unit/conventions_test.py`'s `_CONVENTIONS` transcribes, so it earns no
row in `tests/README.md`'s *Convention tests* table either.
"""

import shutil
import subprocess
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_GIT = shutil.which("git") or "git"


def test_every_pragma_no_cover_gives_its_reason_on_the_same_line() -> None:
    """Run the command `pyproject.toml`'s own comment names, unchanged.

    `git grep` exits 1 where nothing matches, which is what a clean tree
    answers, and 0 where a line ending in the bare pragma is found --
    told apart by the exit code rather than by empty output, which a
    `git` invoked wrongly would print just as well.
    """
    result = subprocess.run(  # noqa: S603
        [_GIT, "grep", "-nE", "pragma: no cover$", "--", "*.py"],
        cwd=_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 1, (
        "a pragma: no cover with no reason on its own line (git grep exit"
        f" {result.returncode}):\n{result.stdout}{result.stderr}"
    )
