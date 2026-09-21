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

#856's own fix enumerated one file and one citation form rather than the
pattern, and btclib-org/btclib-node#1014 is the same trap closing a
second time: `rpc/callbacks.py` grew an identical release-annotated
citation that neither the reorg-test check nor a hardcoded tuple of
version-claim files named, so a future bump would have left it stale
with this gate exiting 0 throughout. So the version-claim half of this
reads every file `git` tracks (`git ls-files`, in place of a directory
walk that would need its own exclude list for `.venv`, `.git` and build
output) rather than a named few: a release-annotated citation or a bare
version claim is checked wherever it is written, and a citation site
gained later needs no entry added here to be covered.

What stays scoped to `reorg_test.py` alone is its own convention of
citing one commit several times and annotating it only once -- every
bare citation in that module names the commit its own annotated citation
already established, which is a property of how that one module is
written and not a second instance of the file-enumeration bug: a bare
citation elsewhere in the tree (a `CHANGELOG.md` entry, one of
`rpc/callbacks.py`'s own citations of Core's unreleased `master`) names
a commit unrelated to the others and to the pin on purpose, so requiring
tree-wide agreement between arbitrary bare citations would be wrong.

This reads the tree rather than Bitcoin Core: it has no Core checkout
and no network access here, so it cannot say whether `bitcoin/bitcoin@
<sha>` still describes anything real -- only a human re-reading Core at
the new tag can say that. What it can say without either is whether the
tree still agrees with itself: every citation in `reorg_test.py` names
the same sha, that sha's own annotation names the release
`integration-bitcoind.yml` pins, and every release-annotated citation or
bare version claim anywhere else in the tree names that same release.
Disagreement anywhere in that set is what a bump leaves behind where the
citations are not re-read, and is what this reports.

Three kinds of file are excluded from the tree-wide scan, none of them a
citation site being enumerated back in. This script's own source and its
test (`tests/check_core_citation_pin_test.py`) are the first two: this
file's own comments describe the patterns below with symbolic version
numbers rather than the real pin, on purpose, so it never needs an
entry protecting it; the test's fixtures are engineered to match and
mismatch the patterns, and are written to files under `tmp_path` the
script is pointed at -- scanning the test module's own source as well
would check text meant to simulate a tree, not a claim about this one.

`CHANGELOG.md` and `RELEASE_NOTES.md` are the third, and for a
different reason: both are append-only narrations of history rather
than descriptions of current state, so a past entry saying a measurement
was taken "against a real `bitcoind` v31.1.0" stays true, and stays
written that way, long after a later pin bump makes the number a claim
this check would otherwise read as drift on a file nothing in this
change touched. `CHANGELOG.md:2857`'s own entry is exactly this shape
today, and reads as drift the moment the pin moves unless the file is
out of scope. The release-annotated citation form, by contrast, exists
specifically to describe what the tree is tested against *now*
(`CLAUDE.md`'s *Following Bitcoin Core*), and neither file currently
carries one -- excluding both here is a decision about what kind of
claim each file makes, not a measurement of what happens to be in them
today.

    python3 .github/scripts/check_core_citation_pin.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

# resolved once: a bare "git" in a subprocess list is a partial
# executable path relying on PATH's own search order rather than naming
# what actually runs (same reason check_vendored_pin.py resolves its
# own git and gh this way)
_GIT = shutil.which("git") or "git"

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github/workflows/integration-bitcoind.yml"
_REORG_TEST = _ROOT / "tests/integration/reorg_test.py"
# this script's own source and test, plus the two append-only prose
# files -- none of the three is a citation site; the module docstring
# above says why each is excluded rather than read
_EXCLUDED_FROM_SCAN = (
    Path(__file__).resolve(),
    _ROOT / "tests/check_core_citation_pin_test.py",
    _ROOT / "CHANGELOG.md",
    _ROOT / "RELEASE_NOTES.md",
)

_PIN = re.compile(r'version:\s*"([0-9]+\.[0-9]+)"')
_CITED_SHA = re.compile(r"bitcoin/bitcoin@([0-9a-f]{7,40})")
_ANNOTATED = re.compile(
    r"bitcoin/bitcoin@[0-9a-f]{7,40} -- v([0-9]+\.[0-9]+), the release"
)
# "Core vX.Y.Z" or "bitcoind vX.Y.Z", allowing the odd punctuation mark
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


def _tracked_files() -> list[Path]:
    """Every path `git` tracks, minus this check's own source and test.

    `git ls-files` is the file set rather than a directory walk: a walk
    would need its own exclude list for `.venv`, `.git` and build
    output, which only moves the enumeration this script exists to stop
    doing from "which files carry a citation" to "which directories are
    not source" -- `git` already answers that question for every other
    file-selecting hook in this tree.
    """
    result = subprocess.run(  # noqa: S603
        [_GIT, "-C", str(_ROOT), "ls-files"],
        capture_output=True,
        check=True,
        encoding="utf-8",
    )
    return [
        _ROOT / line
        for line in result.stdout.splitlines()
        if (_ROOT / line) not in _EXCLUDED_FROM_SCAN
    ]


def _read_text(path: Path) -> str | None:
    """Return path's text, or None where it is not text this check can read."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError, OSError:
        return None


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


def _tree_problems(pinned_version: str) -> list[str]:
    """Every release-annotated citation or bare version claim, tree-wide.

    Unlike `_sha_problems` above, this does not care which file it is
    reading or whether the tree already knew to name it: it is what
    would have caught `rpc/callbacks.py`'s own citation without anybody
    adding it to a list (btclib-org/btclib-node#1014). It necessarily
    re-reads `reorg_test.py` and the workflow too -- harmless, since a
    citation already checked above is still just as much a citation.
    """
    problems = []
    for path in _tracked_files():
        text = _read_text(path)
        if text is None:
            continue
        for match in _ANNOTATED.finditer(text):
            version = match.group(1)
            if version != pinned_version:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(
                    f"{path}:{line}: citation is annotated as release"
                    f" v{version}, integration-bitcoind.yml pins"
                    f" {pinned_version}"
                )
        for match in _VERSION_CLAIM.finditer(text):
            version = match.group(1)
            if version != pinned_version:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(
                    f"{path}:{line}: names bitcoind v{version}.x,"
                    f" integration-bitcoind.yml pins {pinned_version}"
                )
    return problems


def main() -> int:
    """Report every citation or claim a bitcoind pin bump left behind."""
    pinned_version = _pinned_version()
    problems = [
        *_sha_problems(pinned_version),
        *_tree_problems(pinned_version),
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
