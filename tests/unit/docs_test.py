# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Every module btclib-node ships is documented.

The pages under `docs/source/` are hand written, which invites drift, and
telling contributors to re-run `sphinx-apidoc -f` is no answer: `-f`
regenerates every page from the template, discarding the hand-tuned index
and the myst links to the markdown files. What drift costs is a module
absent from the automodule directives -- and therefore from the published
documentation -- with nothing anywhere to say so; this test is the thing
that says so.

A test rather than a workflow step. It needs no environment the suite
does not already have, it runs on every interpreter of the matrix
instead of on one runner, and `tests-passed` gates it without a line
being added to any `needs` list.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_PACKAGE_DIR = _ROOT / "src" / "btclib_node"
_DOCS_DIR = _ROOT / "docs" / "source"

# what a documented module looks like to sphinx: ".. automodule:: name",
# whatever indentation and options follow it
_AUTOMODULE = re.compile(r"^\s*\.\.\s+automodule::\s+(\S+)\s*$", re.MULTILINE)


def _documented_in(text: str) -> set[str]:
    """Every module one documentation page publishes the members of.

    An automodule stanza and nothing else. A toctree line naming
    `btclib_node.p2p` is not one: it makes the package's *page* reachable
    from `btclib_node.rst`, which is what a toctree is for, and says
    nothing about whether that page renders the package's own
    `__init__` -- the docstring, and the names re-exported flat that a
    caller reads `btclib_node.p2p` for. Counting it as documentation is
    what would let a package keep its page, its submodules and its
    toctree line while losing itself out of the middle of them.

    Nothing is lost by not counting it, because the toctree is gated
    where it belongs: a page no toctree includes is `toc.not_included`,
    which the `-W` of the documentation workflow turns into a failure.
    """
    return set(_AUTOMODULE.findall(text))


def _documented() -> set[str]:
    """Every module the documentation sources carry a stanza for."""
    names: set[str] = set()
    for page in _DOCS_DIR.glob("*.rst"):
        names.update(_documented_in(page.read_text(encoding="utf-8")))
    return names


def _is_public(parts: tuple[str, ...]) -> bool:
    """Whether a module path names something a user is meant to import.

    `__init__` is the package itself and not a private name, which is the
    only reason this is not a one-line `startswith("_")`. Nothing under
    `src/btclib_node/` is itself named with a leading underscore today --
    unlike btclib's own `_ripemd160` -- so this tree has no case of the
    other answer yet; the function is still read individually rather than
    replaced with the one-liner, so that the day one arrives it is
    already handled rather than silently exempted.
    """
    return not any(part.startswith("_") for part in parts if part != "__init__")


def _shipped(package_dir: Path = _PACKAGE_DIR) -> set[str]:
    """Every dotted name a user can import from an installed btclib-node.

    Read off the source tree rather than by walking the imported package
    with pkgutil: a module missing from the documentation is usually a
    module just added, and this way noticing it does not depend on it
    being importable.

    `package_dir` is a parameter and not always `_PACKAGE_DIR` so that
    `test_a_private_module_is_skipped` below can trip the private-module
    branch on a synthetic tree: nothing under `src/btclib_node/` is
    itself private today, so that branch is otherwise never taken by the
    real one, which is exactly the shape the coverage floor refuses to
    leave unexercised.
    """
    names = {"btclib_node"}
    for path in sorted(package_dir.rglob("*.py")):
        parts = path.relative_to(package_dir).with_suffix("").parts
        if not _is_public(parts):
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        names.add(".".join(("btclib_node", *parts)))
    return names


# the two directions are separate tests because they fail for opposite
# reasons and are fixed in opposite files: something undocumented is a
# missing stanza in docs/source, something documented that no longer exists
# is a stanza left behind by a rename
def test_every_module_is_documented() -> None:
    """Verify every shipped module has a stanza in docs/source."""
    undocumented = _shipped() - _documented()
    assert not undocumented, (
        "not documented in docs/source: "
        + ", ".join(sorted(undocumented))
        + " -- add an automodule stanza (do not run sphinx-apidoc -f,"
        " which discards the hand-tuned pages)"
    )


def test_no_documented_module_has_gone_away() -> None:
    """Verify no automodule stanza names a module the tree lost."""
    stale = _documented() - _shipped()
    assert not stale, "documented in docs/source but not shipped: " + ", ".join(
        sorted(stale)
    )


def test_the_docs_sources_were_found_at_all() -> None:
    """Guard the two tests above against passing vacuously.

    Both compare against a set built by globbing, and a wrong `_DOCS_DIR`
    would make `_documented()` empty, which `test_no_documented_module_has
    _gone_away` reports as success.
    """
    assert _DOCS_DIR.is_dir()
    assert (_DOCS_DIR / "btclib_node.rst").is_file()
    assert len(_documented()) > 5


@pytest.mark.parametrize("name", sorted(_shipped()))
def test_shipped_module_is_a_dotted_btclib_node_name(name: str) -> None:
    """The set the assertions above are built on holds what it claims.

    A bug in `_shipped()` -- a private module slipping through, a stray
    path separator -- would otherwise show up as a confusing failure of
    the two tests above rather than as a failure here.
    """
    assert name == "btclib_node" or name.startswith("btclib_node.")
    assert not any(part.startswith("_") for part in name.split("."))


def test_a_private_module_is_skipped(tmp_path: Path) -> None:
    """`_shipped` never lists a private module, on a tree that has one.

    `src/btclib_node/` itself has none to walk into, so this is the only
    place the `not _is_public(parts)` branch of `_shipped` is taken at
    all -- a synthetic package, built here, one public module and one
    private one beside it.
    """
    (tmp_path / "public.py").touch()
    (tmp_path / "_private.py").touch()
    assert _shipped(package_dir=tmp_path) == {"btclib_node", "btclib_node.public"}


def test_a_toctree_line_is_not_a_stanza() -> None:
    """A package page is documentation of the package only if it says so.

    The two tests above are only as good as this scan, and the shape it
    has to tell apart is the one every per-package page here has: a
    toctree line for `btclib_node.p2p` in `btclib_node.rst`, stanzas for
    the submodules in `btclib_node.p2p.rst`, and the package's own stanza
    at the end of it. Read the toctree as documentation and dropping that
    last stanza passes -- the suite finding the name in the toctree, and
    sphinx warning about nothing, autodoc having no idea a page was meant
    to carry it.
    """
    source = (
        ".. toctree::\n"
        "   :maxdepth: 4\n"
        "\n"
        "   btclib_node.p2p.messages\n"
        "\n"
        ".. automodule:: btclib_node.p2p.messages.errors\n"
        "   :members:\n"
    )
    assert _documented_in(source) == {"btclib_node.p2p.messages.errors"}


@pytest.mark.parametrize(
    ("parts", "public"),
    [
        (("chains",), True),
        (("p2p", "address"), True),
        (("__init__",), True),
        (("p2p", "__init__"), True),
        (("_internal",), False),
        (("p2p", "_helpers"), False),
        (("_internal", "address"), False),
    ],
)
def test_is_public(parts: tuple[str, ...], *, public: bool) -> None:
    """Both answers, and the two shapes this tree itself has none of.

    Every module under `src/btclib_node/` is public today, unlike
    btclib's own `_ripemd160.py`; the parametrization still covers a
    private module and a private package the same way btclib's test
    does, so the function is exercised on the shape it exists for even
    though this tree has not needed it yet.
    """
    assert _is_public(parts) is public
