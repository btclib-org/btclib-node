# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for the bitcoind-pin-versus-Core-citation check of `.github/scripts`.

The script's own module docstring is the argument for what it checks and
why; this exercises both readings of it. `test_this_tree_agrees_with_itself`
runs it unmodified, against this tree's own files, which is the same
question the pre-commit hook asks on every commit. Most other tests point
`_tracked_files` at a fixture directory instead of the real tree, so each
can make exactly one of the ways the tree could stop agreeing true and
check that the script says so -- a pin bumped alone, one citation edited
and the rest left behind, a version claim left at the old release, a
citation living in a file with no special name at all -- and that an
unmodified fixture says nothing is wrong.
`test_a_citation_in_an_unlisted_file_is_caught` is the regression test
for btclib-org/btclib-node#1014: `rpc/callbacks.py` carried the
release-annotated form and was on no list, so a version-tuple check the
way this module worked before #1014 would pass it silently, and the
fixture in that test is built the same way.

`test_tracked_files_excludes_self_test_and_changelogs` and
`test_a_stale_version_claim_in_changelog_is_not_flagged_end_to_end`
exercise that same exclusion against a throwaway git repository, rather
than through a fixture the other tests monkeypatch `_tracked_files`
away with -- the first at `_tracked_files` itself, the second through
`main`, so the exclusion is proved to be `_tracked_files`'s own doing
and not an artefact of every other test choosing not to look.

The script is loaded by path, `.github/scripts` being no package.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = (
    Path(__file__).parents[1] / ".github" / "scripts" / "check_core_citation_pin.py"
)
# resolved once, the same reason the script under test resolves its own
# git this way rather than trusting a bare name to PATH's search order
_GIT = shutil.which("git") or "git"

_WORKFLOW = """\
      - name: Install bitcoind
        id: bitcoind
        uses: ./.github/actions/install-bitcoind
        with:
          version: "31.1"
          sha256: b80d9c3e04da78fb6f0569685673418cf686fadba9042d926d13fb87ff503f9e
# connecting to bitcoind and reaching its tip took 0.11s measured
# against a local build of Bitcoin Core v31.1.0 on an otherwise idle
# machine
"""

_REORG_TEST = """\
# How many blocks the two branches share, and it is Core's own coinbase
# maturity: `COINBASE_MATURITY`, `src/consensus/consensus.h`,
# at bitcoin/bitcoin@9be056a8a7 -- v31.1, the release
# `integration-bitcoind.yml` pins and this module therefore runs
# against.
#
# What dates the headers, both
# at bitcoin/bitcoin@9be056a8a7. `tests.GENESIS_TIME` is not a day
# behind the clock.
    (`src/consensus/tx_check.cpp`, at bitcoin/bitcoin@9be056a8a7), and
    the tip (`src/rpc/mining.cpp`, at bitcoin/bitcoin@9be056a8a7). A
"""

_CONFTEST = """\
# measured directly, starting bitcoind v31.1.0 and reaching a first
# successful `getblockchaininfo` call took under two seconds
"""

_ERRORS = """\
    Measured against a real `bitcoind` (v31.1.0, `-regtest`) answering
"""

# stands in for a citation living somewhere the original, pre-#1014
# script never looked: `rpc/callbacks.py`'s own `add_node`, which
# carried this exact form and was on neither of that script's two
# hardcoded lists
_UNLISTED_SITE = """\
    The empty-`node` refusal is master's own fix rather than this
    tree's own pinned `bitcoind`'s:
    at bitcoin/bitcoin@9be056a8a7 -- v31.1, the release
    `integration-bitcoind.yml` pins, answers an empty `node` with a
    silent, do-nothing success instead.
"""

# an append-only entry the way CHANGELOG.md and RELEASE_NOTES.md write
# one: a past measurement against the release pinned when it was
# written, which stays true -- and stays written that way -- long after
# a later bump makes the number look stale to anyone not reading it as
# history
_CHANGELOG_ENTRY = """\
- Exercised against a real `bitcoind` v31.1.0 with
  `BTCLIB_NODE_INTEGRATION=1`.
"""


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Return the script, imported by path, registered before it runs."""
    spec = importlib.util.spec_from_file_location("check_core_citation_pin", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "check_core_citation_pin", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def agreeing_tree(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ModuleType:
    """Point the script at a fixture tree where every file agrees.

    `unlisted_site` carries a release-annotated citation under a name
    that means nothing to this script -- no `_REORG_TEST`, nothing a
    version-claim tuple would have named -- which is the shape
    `_tracked_files` exists to reach without anybody adding an entry
    for it.
    """
    workflow = tmp_path / "integration-bitcoind.yml"
    reorg_test = tmp_path / "reorg_test.py"
    conftest = tmp_path / "conftest.py"
    errors = tmp_path / "errors.py"
    unlisted_site = tmp_path / "some_unrelated_module.py"
    workflow.write_text(_WORKFLOW, encoding="utf-8")
    reorg_test.write_text(_REORG_TEST, encoding="utf-8")
    conftest.write_text(_CONFTEST, encoding="utf-8")
    errors.write_text(_ERRORS, encoding="utf-8")
    unlisted_site.write_text(_UNLISTED_SITE, encoding="utf-8")
    monkeypatch.setattr(script, "_WORKFLOW", workflow)
    monkeypatch.setattr(script, "_REORG_TEST", reorg_test)
    monkeypatch.setattr(
        script,
        "_tracked_files",
        lambda: [workflow, reorg_test, conftest, errors, unlisted_site],
    )
    return script


def test_this_tree_agrees_with_itself(script: ModuleType) -> None:
    """This repository's own files, read unmodified, name one release."""
    assert script.main() == 0


def test_an_agreeing_fixture_names_one_release(agreeing_tree: ModuleType) -> None:
    """The fixture itself is a positive control: unmodified, it passes."""
    assert agreeing_tree.main() == 0


def test_a_pin_bumped_alone_is_caught(
    agreeing_tree: ModuleType, tmp_path: Path
) -> None:
    """Raising the pin without touching a citation is what #856 is about."""
    (tmp_path / "integration-bitcoind.yml").write_text(
        _WORKFLOW.replace('"31.1"', '"31.2"'), encoding="utf-8"
    )
    assert agreeing_tree.main() == 1


def test_one_citation_edited_and_the_rest_left_behind(
    agreeing_tree: ModuleType, tmp_path: Path
) -> None:
    """A half-applied edit leaves the module disagreeing with itself."""
    (tmp_path / "reorg_test.py").write_text(
        _REORG_TEST.replace(
            "(`src/consensus/tx_check.cpp`, at bitcoin/bitcoin@9be056a8a7)",
            "(`src/consensus/tx_check.cpp`, at bitcoin/bitcoin@deadbeef00)",
        ),
        encoding="utf-8",
    )
    assert agreeing_tree.main() == 1


def test_no_citation_at_all_is_caught_rather_than_read_as_clean(
    agreeing_tree: ModuleType, tmp_path: Path
) -> None:
    """A broken pattern must not read the same as an agreeing tree."""
    (tmp_path / "reorg_test.py").write_text(
        _REORG_TEST.replace("bitcoin/bitcoin@9be056a8a7", "nothing-here"),
        encoding="utf-8",
    )
    assert agreeing_tree.main() == 1


def test_an_unannotated_citation_is_caught(
    agreeing_tree: ModuleType, tmp_path: Path
) -> None:
    """Removing the tag name beside the sha defeats the whole check."""
    (tmp_path / "reorg_test.py").write_text(
        _REORG_TEST.replace(" -- v31.1, the release", ""), encoding="utf-8"
    )
    assert agreeing_tree.main() == 1


def test_a_version_claim_left_at_the_old_release_is_caught(
    agreeing_tree: ModuleType, tmp_path: Path
) -> None:
    """conftest.py and errors.py are in scope, not only reorg_test.py."""
    (tmp_path / "conftest.py").write_text(
        _CONFTEST.replace("v31.1.0", "v31.0.0"), encoding="utf-8"
    )
    assert agreeing_tree.main() == 1


def test_an_unrelated_action_pin_is_not_read_as_a_bitcoind_version(
    agreeing_tree: ModuleType, tmp_path: Path
) -> None:
    """A `# vX.Y.Z` beside an action pin is not a bitcoind version claim."""
    action_pin = (
        "        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        " # v7.0.1\n"
    )
    (tmp_path / "integration-bitcoind.yml").write_text(
        _WORKFLOW + action_pin, encoding="utf-8"
    )
    assert agreeing_tree.main() == 0


def test_a_citation_in_an_unlisted_file_is_caught(
    agreeing_tree: ModuleType, tmp_path: Path
) -> None:
    """btclib-org/btclib-node#1014: a citation site needs no entry to be seen.

    `some_unrelated_module.py` is not `_REORG_TEST` and was never on a
    version-claim tuple; only reading every tracked file, rather than a
    named few, catches this.
    """
    (tmp_path / "some_unrelated_module.py").write_text(
        _UNLISTED_SITE.replace(" -- v31.1, the release", " -- v31.0, the release"),
        encoding="utf-8",
    )
    assert agreeing_tree.main() == 1


def test_tracked_files_excludes_self_test_and_changelogs(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_tracked_files` drops its own source, its test, and both history files.

    Built against a throwaway git repository rather than the real tree,
    so this is a test of the filter itself and not of what happens to be
    committed here today.
    """
    kept = tmp_path / "kept.py"
    kept.write_text("kept\n", encoding="utf-8")
    self_source = tmp_path / "check_core_citation_pin.py"
    self_source.write_text("self\n", encoding="utf-8")
    self_test = tmp_path / "check_core_citation_pin_test.py"
    self_test.write_text("test\n", encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_CHANGELOG_ENTRY, encoding="utf-8")
    release_notes = tmp_path / "RELEASE_NOTES.md"
    release_notes.write_text(_CHANGELOG_ENTRY, encoding="utf-8")

    subprocess.run(  # noqa: S603
        [_GIT, "init", "-q", str(tmp_path)], check=True
    )
    subprocess.run(  # noqa: S603
        [_GIT, "-C", str(tmp_path), "add", "-A"], check=True
    )

    monkeypatch.setattr(script, "_ROOT", tmp_path)
    monkeypatch.setattr(
        script,
        "_EXCLUDED_FROM_SCAN",
        (self_source, self_test, changelog, release_notes),
    )

    assert script._tracked_files() == [kept]


def test_a_stale_version_claim_in_changelog_is_not_flagged_end_to_end(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `CHANGELOG.md` entry naming an old release is history, not drift.

    `CHANGELOG.md:2857` in this tree's own history carries exactly this
    shape -- a past measurement against the then-pinned release -- and
    must never be read as a claim about the pin today. Real `git
    ls-files` under a throwaway repository and the real `_tracked_files`
    filter, so `changelog` is genuinely offered to `_tree_problems` and
    the exclusion is what keeps it from being flagged, not the fixture
    choosing not to include it. `release_notes`, this script's own
    source and its own test carry the identical stale text, proving the
    same exclusion for all four at once.
    """
    workflow = tmp_path / "integration-bitcoind.yml"
    reorg_test = tmp_path / "reorg_test.py"
    changelog = tmp_path / "CHANGELOG.md"
    release_notes = tmp_path / "RELEASE_NOTES.md"
    self_source = tmp_path / "check_core_citation_pin.py"
    self_test = tmp_path / "check_core_citation_pin_test.py"
    workflow.write_text(
        _WORKFLOW.replace('"31.1"', '"31.9"').replace("v31.1.0", "v31.9.0"),
        encoding="utf-8",
    )
    reorg_test.write_text(
        _REORG_TEST.replace("v31.1, the release", "v31.9, the release"),
        encoding="utf-8",
    )
    # every one of these four still names the old release, on purpose
    changelog.write_text(_CHANGELOG_ENTRY, encoding="utf-8")
    release_notes.write_text(_CHANGELOG_ENTRY, encoding="utf-8")
    self_source.write_text(_ERRORS, encoding="utf-8")
    self_test.write_text(_CONFTEST, encoding="utf-8")

    subprocess.run([_GIT, "init", "-q", str(tmp_path)], check=True)  # noqa: S603
    subprocess.run([_GIT, "-C", str(tmp_path), "add", "-A"], check=True)  # noqa: S603

    monkeypatch.setattr(script, "_ROOT", tmp_path)
    monkeypatch.setattr(script, "_WORKFLOW", workflow)
    monkeypatch.setattr(script, "_REORG_TEST", reorg_test)
    monkeypatch.setattr(
        script,
        "_EXCLUDED_FROM_SCAN",
        (self_source, self_test, changelog, release_notes),
    )

    assert script.main() == 0
