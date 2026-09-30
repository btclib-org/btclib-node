# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for the not-affected list of the bill-of-materials writer.

`.github/scripts/generate_sbom.py` reads a wheel's `METADATA` and the bytes
of the two distribution files, and, where the tree keeps
`.github/vex.toml`, the findings that list records. The wheels here are
synthetic and carry the metadata each test is about; the sdist is a file
with content, only its digest being read. What is asserted is the
`vulnerabilities` array and nothing else of the document.

The script is loaded by path, `.github/scripts` being no package.
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "generate_sbom.py"

# an instant in August 2026, as SOURCE_DATE_EPOCH carries one: seconds
_EPOCH = 1786407122
_METADATA = """Metadata-Version: 2.4
Name: btclib-node
Version: 2026.9
Summary: A Bitcoin full node
License-Expression: MIT
Requires-Python: >=3.10
"""


@pytest.fixture
def script() -> ModuleType:
    """Return the script, imported by path."""
    spec = importlib.util.spec_from_file_location("generate_sbom", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sbom(
    script: ModuleType, directory: Path, requirements: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Return the document for a synthetic wheel and sdist in `directory`.

    `directory` doubles as the repository root the script reads the list
    from, and holds no `.gitmodules`, so no submodule is scanned.
    """
    text = _METADATA + "".join(f"Requires-Dist: {r}\n" for r in requirements)
    wheel = directory / "btclib_node-2026.9-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("btclib_node-2026.9.dist-info/METADATA", text)
    sdist = directory / "btclib_node-2026.9.tar.gz"
    sdist.write_bytes(b"an sdist, read for its digest alone")
    document: dict[str, Any] = script.build_sbom(wheel, sdist, _EPOCH, directory)
    return document


def write_vex(root: Path, *entries: str) -> None:
    """Write the tree's not-affected list from these table bodies."""
    (root / ".github").mkdir(exist_ok=True)
    text = "".join(f"[[not_affected]]\n{entry}\n" for entry in entries)
    (root / ".github" / "vex.toml").write_text(text, encoding="utf-8")


_FINDING = """id = "GHSA-xxxx-xxxx-xxxx"
source = "GitHub Advisories"
component = "Some_Dep"
justification = "code_not_reachable"
detail = "The vulnerable parser is never called."
"""


def test_a_tree_with_no_list_states_no_vulnerabilities(
    script: ModuleType, tmp_path: Path
) -> None:
    """No file leaves the key out, not an empty array."""
    assert "vulnerabilities" not in sbom(script, tmp_path)


def test_a_finding_reaches_the_document_against_its_component(
    script: ModuleType, tmp_path: Path
) -> None:
    """The finding names the dependency by its `bom-ref` and says why."""
    write_vex(tmp_path, _FINDING)
    document = sbom(script, tmp_path, requirements=("some-dep>=1",))

    assert document["vulnerabilities"] == [
        {
            "id": "GHSA-xxxx-xxxx-xxxx",
            "source": {"name": "GitHub Advisories"},
            "affects": [{"ref": "pkg:pypi/some-dep"}],
            "analysis": {
                "state": "not_affected",
                "justification": "code_not_reachable",
                "detail": "The vulnerable parser is never called.",
            },
        }
    ]


def test_a_finding_may_name_the_distribution_itself(
    script: ModuleType, tmp_path: Path
) -> None:
    """The root component is a component too, its own code being a subject."""
    root = sbom(script, tmp_path)["metadata"]["component"]
    write_vex(tmp_path, _FINDING.replace("Some_Dep", root["name"]))
    document = sbom(script, tmp_path)

    (finding,) = document["vulnerabilities"]
    assert finding["affects"] == [{"ref": root["bom-ref"]}]


def test_a_finding_for_a_component_the_document_lacks_is_refused(
    script: ModuleType, tmp_path: Path
) -> None:
    """A finding that answers nothing is an error, not a silent omission."""
    write_vex(tmp_path, _FINDING)
    with pytest.raises(SystemExit, match="some-dep, which this document"):
        sbom(script, tmp_path)


@pytest.mark.parametrize(
    "entry",
    [
        _FINDING.replace("code_not_reachable", "not_reachable"),
        _FINDING.replace('detail = "The vulnerable parser is never called."\n', ""),
        _FINDING + 'severity = "low"\n',
        _FINDING.replace("The vulnerable parser is never called.", ""),
    ],
    ids=["justification", "missing key", "extra key", "empty value"],
)
def test_a_malformed_finding_is_refused(
    script: ModuleType, tmp_path: Path, entry: str
) -> None:
    """A finding is stated whole or not at all."""
    write_vex(tmp_path, entry)
    with pytest.raises(SystemExit):
        sbom(script, tmp_path, requirements=("some-dep>=1",))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "holds no `\\[\\[not_affected\\]\\]` entry"),
        ("not_affected = [1]\n", "must be a list of"),
        ("[not_affected]\nid = 'a'\n", "must be a list of"),
        ("not_affected = [\n", "does not parse"),
    ],
    ids=["empty file", "list of integers", "single table", "unparsable"],
)
def test_a_list_the_rule_does_not_allow_is_refused_cleanly(
    script: ModuleType, tmp_path: Path, text: str, message: str
) -> None:
    """Each shape of a bad file stops the run with a message."""
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "vex.toml").write_text(text, encoding="utf-8")
    with pytest.raises(SystemExit, match=message):
        sbom(script, tmp_path)
