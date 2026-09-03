# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for the bitcoind-pin-versus-Core-citation check of `.github/scripts`.

The script's own module docstring is the argument for what it checks and
why; this exercises both readings of it. `test_this_tree_agrees_with_itself`
runs it unmodified, against this tree's own files, which is the same
question the pre-commit hook asks on every commit. Every other test
points its module-level paths at a fixture directory instead, so each
can make exactly one of the three ways the tree could stop agreeing true
and check that the script says so -- a pin bumped alone, one citation
edited and the rest left behind, a version claim left at the old
release -- and that an unmodified fixture says nothing is wrong.

The script is loaded by path, `.github/scripts` being no package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = (
    Path(__file__).parents[1] / ".github" / "scripts" / "check_core_citation_pin.py"
)

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
    """Point the script at a fixture tree where every file agrees."""
    workflow = tmp_path / "integration-bitcoind.yml"
    reorg_test = tmp_path / "reorg_test.py"
    conftest = tmp_path / "conftest.py"
    errors = tmp_path / "errors.py"
    workflow.write_text(_WORKFLOW, encoding="utf-8")
    reorg_test.write_text(_REORG_TEST, encoding="utf-8")
    conftest.write_text(_CONFTEST, encoding="utf-8")
    errors.write_text(_ERRORS, encoding="utf-8")
    monkeypatch.setattr(script, "_WORKFLOW", workflow)
    monkeypatch.setattr(script, "_REORG_TEST", reorg_test)
    monkeypatch.setattr(script, "_VERSION_CLAIM_FILES", (workflow, conftest, errors))
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
