# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Fail where a bumped bitcoind pin leaves a release-pinned citation behind.

`.github/workflows/integration-bitcoind.yml` pins the bitcoind release
`tests/integration/reorg_test.py` runs its reorg module against, and
`CLAUDE.md`'s *Following Bitcoin Core* is why a citation of Core's source
at that release carries the release's own tag name beside the sha: the
tag is what tells such a citation from a stale read, because ancestry
does not. Nothing ties the tag name written into the tree back to the
pin, so a bump is a one-line diff that leaves every citation reading a
bitcoind the suite no longer runs -- the argument is
btclib-org/btclib-node#856.

This reads the tree rather than Bitcoin Core: it has no Core checkout
and no network access here, so it cannot say whether `bitcoin/bitcoin@
<sha>` still describes anything real -- only a human re-reading Core at
the new tag can say that. What it can say without either is whether the
tree still agrees with itself: every citation in `reorg_test.py` names
the same sha, that sha's own annotation names the release
`integration-bitcoind.yml` pins, and the handful of other places that
copy the release's full version number for a measurement claim --
`tests/integration/conftest.py`, `src/btclib_node/rpc/errors.py`, and
the workflow's own timing comment beside the pin -- name that same
release. Disagreement anywhere in that set is what a bump leaves behind
where the citations are not re-read, and is what this reports.

    python3 .github/scripts/check_core_citation_pin.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github/workflows/integration-bitcoind.yml"
_REORG_TEST = _ROOT / "tests/integration/reorg_test.py"
# every other site that copies the release's full version number as part
# of a measurement claim rather than as a citation of Core's source --
# see the module docstring for why each is in scope
_VERSION_CLAIM_FILES = (
    _WORKFLOW,
    _ROOT / "tests/integration/conftest.py",
    _ROOT / "src/btclib_node/rpc/errors.py",
)

_PIN = re.compile(r'version:\s*"([0-9]+\.[0-9]+)"')
_CITED_SHA = re.compile(r"bitcoin/bitcoin@([0-9a-f]{7,40})")
_ANNOTATED = re.compile(
    r"bitcoin/bitcoin@[0-9a-f]{7,40} -- v([0-9]+\.[0-9]+), the release"
)
# "Core v31.1.0" or "bitcoind v31.1.0", allowing the odd punctuation mark
# in between -- a bare `v\d+\.\d+\.\d+` also matches a pinned action's
# own tag in a trailing comment (`actions/checkout@... # v7.0.1`), which
# is not a bitcoind version and has no reason to track this pin
_VERSION_CLAIM = re.compile(r"(?:Core|bitcoind)[^\n]{0,20}?v([0-9]+\.[0-9]+)\.[0-9]+")


def _pinned_version() -> str:
    """Return the bitcoind release integration-bitcoind.yml's own pin names."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    matches = _PIN.findall(text)
    if len(matches) != 1:
        msg = (
            f"{_WORKFLOW}: expected exactly one bitcoind version pin, found {matches!r}"
        )
        raise AssertionError(msg)
    return str(matches[0])


def _sha_problems(pinned_version: str) -> list[str]:
    """Return every way reorg_test.py disagrees with itself or the pin."""
    text = _REORG_TEST.read_text(encoding="utf-8")
    shas = set(_CITED_SHA.findall(text))
    if not shas:
        return [f"{_REORG_TEST}: no `bitcoin/bitcoin@<sha>` citation found"]
    problems = []
    if len(shas) > 1:
        problems.append(
            f"{_REORG_TEST}: citations disagree with each other: {sorted(shas)}"
        )
    annotated = _ANNOTATED.search(text)
    if annotated is None:
        problems.append(
            f"{_REORG_TEST}: no citation names the release its sha is pinned"
            " to, as CLAUDE.md's Following Bitcoin Core asks of a"
            " release-pinned citation"
        )
    elif annotated.group(1) != pinned_version:
        problems.append(
            f"{_REORG_TEST}: citations are annotated as release"
            f" v{annotated.group(1)}, integration-bitcoind.yml pins"
            f" {pinned_version}"
        )
    return problems


def _version_claim_problems(pinned_version: str) -> list[str]:
    """Every full-version bitcoind claim that no longer names the pin."""
    problems = []
    for path in _VERSION_CLAIM_FILES:
        text = path.read_text(encoding="utf-8")
        for match in _VERSION_CLAIM.finditer(text):
            if match.group(1) != pinned_version:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(
                    f"{path}:{line}: names bitcoind v{match.group(1)}.x,"
                    f" integration-bitcoind.yml pins {pinned_version}"
                )
    return problems


def main() -> int:
    """Report every citation or claim a bitcoind pin bump left behind."""
    pinned_version = _pinned_version()
    problems = [
        *_sha_problems(pinned_version),
        *_version_claim_problems(pinned_version),
    ]
    for problem in problems:
        print(f"DRIFT: {problem}")
    if not problems:
        print(
            "Every release-pinned Core citation and version claim agrees"
            f" with the {pinned_version} bitcoind pin."
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
