# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Configuration file for the Sphinx documentation builder.

For the full list of built-in configuration values, see the
documentation: https://www.sphinx-doc.org/en/master/usage/configuration.html
"""

import posixpath
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from docutils import nodes
from sphinx.addnodes import pending_xref
from sphinx.transforms.post_transforms import SphinxPostTransform

if TYPE_CHECKING:
    from sphinx.application import Sphinx

# the repository root, two levels up from this file, and the one place
# below that is allowed to name it
ROOT = Path(__file__).parents[2].resolve()
# read once and read twice from: the version below and the github url the
# transform at the bottom builds its links on
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = PYPROJECT["project"]["name"]
# no year, and that is the decision COPYRIGHT and LICENSE state: MIT asks
# for none, and a year is a thing that looks out of date every January
# without anything having changed
project_copyright = "The btclib developers"
author = "The btclib developers"
# read from pyproject.toml, the one place the version is declared, and not
# from importlib.metadata, so the value does not depend on whether this
# distribution happens to be installed in the environment building the
# documentation (btclib-org/.github#1098)
release = PYPROJECT["project"]["version"]

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.doctest",
    "sphinx.ext.coverage",
    "sphinx.ext.githubpages",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

# no sphinx.ext.todo: with todo_include_todos left at its default a
# `.. todo::` renders as nothing at all, so the directive is a note to
# nobody. Without the extension it is an unknown directive, which -W turns
# into a failed build -- the open questions belong in the issue tracker

source_suffix = [".rst", ".md"]

# -n on the build (docs.yml and .readthedocs.yaml both pass it) turns an
# unresolved cross-reference into a warning for -W to fail on. Without an
# inventory to resolve against, a name from outside this tree --
# collections.abc.Iterable, pathlib.Path, btclib.tx.tx.Tx -- reports as
# this tree's own broken link; sphinx's own domain answers for the
# builtins (int, bytes, str), so no mapping is needed for those.
# btclib is mapped alongside python rather than left to the entries
# below: this tree's own public surface carries a btclib type in nearly
# every signature it exports, and btclib publishes its own inventory.
# bitcoin-core-rpc joins it for the same reason, since issue #801's row
# 8: rpc/errors.py imports RPCErrorCode from it directly now, and the
# package publishes its own inventory the same way btclib does
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", (None, "_inventories/python.inv")),
    "btclib": ("https://btclib.readthedocs.io/en/latest/", None),
    "bitcoin-core-rpc": ("https://bitcoin-core-rpc.readthedocs.io/en/latest/", None),
}

# `_inventories/python.inv` is a copy of Python 3.14's inventory and no
# cache: sphinx reads it only when `docs.python.org` fails, so a live site
# always wins. It is the Wayback Machine's capture of 2026-08-28,
# https://web.archive.org/web/20260828181213id_/https://docs.python.org/3/objects.inv
# To refresh it, run this from the repository root and rewrite the version
# and source above:
# curl -fsSL -o docs/source/_inventories/python.inv https://docs.python.org/3/objects.inv
# The `btclib` and `bitcoin-core-rpc` mappings have no copy, for the reason
# below.
#
# every build fetches every mapped inventory, rather than reading back
# the copy sphinx writes beside the doctrees and reuses until that copy
# expires. The copy sits under docs/build, which .gitignore covers, so
# neither a diff nor git status says which of them a build resolved
# against, and
# the same commit then answers green in a worktree building for the first
# time and red in one re-reading a page against a copy taken days
# earlier -- a build re-reading nothing resolves nothing and answers
# green whatever its copy holds, which is why the red comes and goes.
# btclib publishes
# this inventory from its own main, so a name added there since a copy
# was taken resolves for docs.yml's fresh checkout and not for that
# worktree. The other direction is the worse one -- a name btclib has
# removed still resolves off the copy, so a reference the gate exists to
# refuse passes here and fails on docs.yml. -E is not the way out: it
# discards the environment and reads the copy back.
#
# What it costs is a fetch of each inventory on every build, which is
# what docs.yml and .readthedocs.yaml already do, their checkouts
# carrying no copy to read; and a build with no route to a mapped host
# fails under -W, which for `python` the fallback copy above answers.
#
# Not -d, putting the doctrees outside the tree: that relocates the copy
# rather than expiring it, and reaches only the command CONTRIBUTING.md
# documents, leaving those two files building against whatever their own
# doctree directory holds. Not a vendored inventory for btclib or
# bitcoin-core-rpc either, which fixes the reference set at the commit
# that added it and leaves the build unable to say whether btclib still
# publishes the name
intersphinx_cache_limit = 0

# What the mapping above cannot answer for, and each entry below carries
# its own reason rather than a nitpick_ignore_regex that would give the
# check up entirely:
#
# - a name a signature carries, or carries inside another name's own
#   subscript, only because ruff's own "TC" family (pyproject.toml's own
#   comment beside it) moves a typing-only import under
#   `if TYPE_CHECKING:` on this tree's >=3.14 target -- deliberately: "TC"
#   is selected because the import costs nothing at runtime there, PEP
#   649's lazy annotation evaluation being native to that version.
#   Autodoc reads a signature's annotation by evaluating it and reading
#   the resulting class's qualified name back off it; that evaluation
#   fails for a module that only imports the name under TYPE_CHECKING,
#   so the annotation renders under the bare name the source spells it
#   with instead, which no inventory -- btclib's or python's -- carries
#   an unqualified entry for. Tx and TxOut (mempool.py's own get_tx and
#   add_tx, among others), Block (utxo_index.py's and filter_index.py's
#   own add_block), NetworkAddressV2 and Payload (p2p/connection.py,
#   p2p/manager.py), Path (p2p/address.py, chainstate/__init__.py,
#   log.py), ScriptFlag/ScriptFlags (interpreter.py's own get_flags
#   and f) and ScriptPubKey (mining.py's own create_new_block) are
#   this tree's own public API doing exactly what "TC"'s own reason asks
#   of it. p2p/manager.py's own Tx is besides imported under the alias
#   BtclibTx, which resolves to nothing under that name for the same
#   reason even once the alias itself is granted an import.
#   block_index.py's own add_headers carries BlockHeader unconditionally,
#   but nested inside `Iterable[BlockHeader]`, and Iterable is what that
#   file imports under TYPE_CHECKING: one guarded name inside a
#   subscript is enough to keep autodoc from resolving the whole
#   annotation, BlockHeader included. __annotationlib_name_1__ is the
#   same defect once more, surfacing as Python 3.14's own placeholder
#   for a name a *compound* annotation could not resolve, rather than as
#   that name itself. __annotationlib_name_2__ is the same placeholder a
#   second time, over a different compound annotation: main.py's own
#   parent_lookup returns `Callable[[BlockHeader], BlockHeader]`, and
#   block_db/__init__.py's own BlockDB.prune_up_to takes a
#   `hash_at_height: Callable[[int], bytes]` -- Callable itself is what
#   each file imports under TYPE_CHECKING, so the whole subscript is
#   unresolved regardless of BlockHeader already being granted its own
#   entry above. The numbering restarts in each signature, and
#   config.py's own Config.__init__ takes one number per parameter typed
#   `Sequence[str]`, Sequence being what that file imports under
#   TYPE_CHECKING, but the build renders none of them. log.py's own
#   open_history_log restarts the numbering over its own parameters,
#   reaching __annotationlib_name_1__ through _7__ there, so the entries
#   above stop at 7: an entry for a number no signature reaches would
#   hide the next warning that took that number
# - asyncio.AbstractEventLoop, spelled that way everywhere this tree
#   uses it (p2p/manager.py, rpc/manager.py, rpc/connection.py):
#   autodoc reads the qualified name back off the class itself once
#   resolved, and the class's own `__module__` is asyncio's private
#   implementation module, not the public one docs.python.org publishes
#   an inventory entry under
# - a name this tree documents nowhere. block_db/__init__.py's own
#   Coin.parse carries BinaryData, btclib's own alias.py type alias,
#   undocumented there, so `:members:` renders no page for a signature
#   naming it to link to
# - settings_file.Json, a `type` alias: autodoc lists it as data, and a
#   signature names it as a class, which no page has
# - R, txgraph.py's own PEP 695 type parameter for a transaction's
#   name: a signature names it as a class, which no page has
nitpick_ignore = [
    ("py:class", "Tx"),
    ("py:class", "TxOut"),
    ("py:class", "Block"),
    ("py:class", "BlockHeader"),
    ("py:class", "NetworkAddressV2"),
    ("py:class", "Payload"),
    ("py:class", "Path"),
    ("py:class", "BtclibTx"),
    ("py:class", "__annotationlib_name_1__"),
    ("py:class", "__annotationlib_name_2__"),
    ("py:class", "__annotationlib_name_3__"),
    ("py:class", "__annotationlib_name_4__"),
    ("py:class", "__annotationlib_name_5__"),
    ("py:class", "__annotationlib_name_6__"),
    ("py:class", "__annotationlib_name_7__"),
    ("py:class", "asyncio.events.AbstractEventLoop"),
    ("py:class", "BinaryData"),
    ("py:class", "ScriptFlag"),
    ("py:class", "ScriptFlags"),
    ("py:class", "ScriptPubKey"),
    ("py:class", "btclib_node.settings_file.Json"),
    ("py:class", "R"),
]

# anchors for h1 to h6, which is what makes a link to a heading of the same
# markdown file resolve here. Without it myst generates no anchor at all,
# so "[Vendoring](#vendoring)" -- a link GitHub and PyPI both follow, the
# anchor being what those two derive from the heading text -- becomes an
# xref to a target no page has, and -W fails the build. Six is every level
# markdown heads at, which makes the number a fixed point rather than a
# depth re-derived from the files it covers: CONTRIBUTING.md's shared half
# is ported to every repository by section 14 of the organization's
# standard, so a heading added there moves a tree-derived depth in each of
# them at once (btclib-org/.github#715)
myst_heading_anchors = 6

# no suppress_warnings, and myst.xref_missing least of all: the transform
# at the bottom of this file resolves every link the included root files
# carry, so a myst target still missing is a link with nowhere to go and
# -W is what says so. Suppressing that subtype hides the defect rather than
# the noise, because what myst emits for a target it cannot resolve is not
# a visibly broken link, it is an anchor to an id the page does not have

exclude_patterns: list[str] = []


# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
html_theme = "furo"

# no html_static_path: this project overrides no stylesheet and ships no
# image, so the "_static" the sphinx template declares was a directory that
# has never existed, and sphinx warns about it on every build. That warning
# was harmless while nothing read it and is a failure now that
# .readthedocs.yaml builds with -W. Re-add the setting with the directory,
# not before it


# -- Links out of the included root markdown files ----------------------------

# Some pages of the toctree are this repository's root markdown files --
# README, CONTRIBUTING, REVIEWING and CHANGELOG -- each pulled into a
# *_link.md shim by a myst {include}. The code below reads the include of
# every page of docs/source, so adding one needs no edit here. Those files
# are written for the places that read them unrendered: the GitHub file
# view and, for README.md, the PyPI long description. So a root file links
# another as "./CONTRIBUTING.md", the spelling links.yml checks, resolving
# it as a path relative to the file. Sphinx sees them lifted out of the
# tree that makes it correct, and myst resolves not one of those links.
# README.md links it as BLOB plus its path, PyPI resolving no relative
# link against the tree (section 2 of the organization's standard), and
# myst renders that as a link to GitHub.
#
# What myst emits for a relative link is the reason this needs code
# rather than a warning filter: a target myst cannot resolve becomes an
# anchor on the page it is already on, href="#./REVIEWING.md", an id
# nothing has. The build succeeds, -W sees nothing, and lychee reads the
# sources, where the path is right.
#
# The transform below answers each link from the repository rather than from
# a table that would have to be kept in step with this directory: a path a
# page of docs/source includes, or a page of docs/source itself, becomes a
# reference to that page, whether written relative or as BLOB plus the path;
# any other relative path that exists in the tree becomes a link to the file
# on GitHub, and a relative path that exists nowhere is left to myst --
# which reports it, and -W then fails, no suppress_warnings hiding the
# subtype.
#
# Not the {include} directive's :relative-docs: option, which is what it
# looks like the job of. It rewrites destinations that begin with the
# prefix it is given, so "docs/source/" leaves "./REVIEWING.md" untouched;
# and giving it "./" is worse than doing nothing, measured on the sibling
# this pattern is read from (bitcoin-core-rpc) -- the destination becomes
# "../../REVIEWING.md", a path outside srcdir that is no document, sphinx
# reads it as a download, finds nothing to copy, and renders the link text
# with no link at all.
#
# Not copying the root files into this directory at build time either.
# README.md links to LICENSE, which is not part of the documentation --
# no page includes it, which is what decides it -- so copies leave that
# link dead however many are made; and the copies are generated files in
# a source tree, which is a second definition of files that already
# exist.

# main, not a permalink pinned to a commit: these are navigation links
# to files that keep changing, and a reader following one wants the file
# as it stands. The base url comes from pyproject.toml, where every url
# this project publishes is declared
BLOB = f"{PYPROJECT['project']['urls']['repository']}/blob/main/"


# a line opening or closing a fence: the spaces before it, its marker, and
# the info string after it. Behind four spaces, a line at the top level of
# a page is no fence
FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*(.*?)\s*$")

# the info string of an include fence; its argument runs to the end of the line
INCLUDE = re.compile(r"^\{include\}\s+(.+)$")

# the name of the directive an info string opens
DIRECTIVE = re.compile(r"^\{([^}]+)\}")

# the directives whose body this reads as markup, among them the
# admonitions, containers and version notes of docutils and sphinx. Every
# directive not named here is read as code, so an include inside it is
# not read: {code-block}, {eval-rst} and {toctree}, and also some that
# do parse their body, as {figure}, {list-table} and {glossary}. The list
# names only directives MyST renders by parsing their body, and grows
# when a page needs an include inside another one
PARSED = (
    "admonition",
    "attention",
    "caution",
    "danger",
    "error",
    "hint",
    "important",
    "note",
    "tip",
    "warning",
    "seealso",
    "topic",
    "sidebar",
    "epigraph",
    "highlights",
    "pull-quote",
    "compound",
    "container",
    "only",
    "hlist",
    "acks",
    "deprecated",
    "versionadded",
    "versionchanged",
    "versionremoved",
    "version-deprecated",
    "version-added",
    "version-changed",
    "version-removed",
)


def includes(text: str) -> list[str]:
    """Return the paths of the include fences of a page, in page order.

    A fence opening a directive of `PARSED` holds markdown, which myst
    parses, so a fence inside it is read. Any other fence is code, and
    what it holds is shown, not parsed. A fence closes at a bare marker
    of its own character, at least as long as the one that opened it.
    A marker behind spaces may close a fence but opens none: it may sit
    in a list item, which this does not read.

    :param text: the page.
    :returns: the paths, as the fences write them.
    """
    paths: list[str] = []
    # the open fences, innermost last: marker, and whether it is code
    open_fences: list[tuple[str, bool]] = []
    for line in text.splitlines():
        fence = FENCE.match(line)
        if fence is None:
            continue
        indent, marker, info = fence.groups()
        if open_fences:
            top, _ = open_fences[-1]
            if not info and marker[0] == top[0] and len(marker) >= len(top):
                open_fences.pop()
                continue
            if open_fences[-1][1]:
                continue
        if indent:
            continue
        if include := INCLUDE.match(info):
            paths.append(include[1])
        directive = DIRECTIVE.match(info)
        open_fences.append((marker, directive is None or directive[1] not in PARSED))
    return paths


def included(page: Path) -> tuple[str, str] | None:
    """Map the file a page of docs/source includes to the page's docname.

    A page with no include fence renders no file, and maps nothing. One
    with more than one stops the build: a relative link on it would have
    two files to start from, and which one is not a question this can
    answer.
    """
    paths = includes(page.read_text(encoding="utf-8"))
    if len(paths) > 1:
        err_msg = f"{page.name}: {len(paths)} include fences, expected at most one"
        raise ValueError(err_msg)
    if not paths:
        return None
    return str((page.parent / paths[0]).resolve().relative_to(ROOT)), page.stem


# repository-relative path -> the docname whose page renders it
INCLUDED = dict(
    entry
    for entry in map(included, sorted(Path(__file__).parent.glob("*.md")))
    if entry is not None
)


class RootFileLinks(SphinxPostTransform):
    """Resolve the links naming a file of this tree."""

    # ahead of myst's own resolver, which runs at 9 and is what turns an
    # unresolved target into that anchor
    default_priority = 5

    @staticmethod
    def _to_page(node: pending_xref, page: str, anchor: str | None) -> None:
        """Point a myst xref at `page`, a docname."""
        node["refdomain"] = "doc"
        node["reftarget"] = page
        node["reftargetid"] = anchor or None

    def _broken_doc_target(self, node: pending_xref) -> tuple[Path, str | None] | None:
        """Recover the file behind a "doc" xref `path2doc` resolved wrong.

        myst already thinks this resolves; a real docname is a working
        cross-reference and none of this file's business. Anything else is
        `Project.path2doc` stripping a matching suffix off *any* file that
        exists on disk, docs or not (sphinx/project.py), so a link climbing
        out of srcdir into a real file -- a page under docs/source
        linking into README.md -- reads as resolved when it is not.
        Recover the file it actually named: reftarget is that path with
        its suffix gone, and only one of the two the tree parses still
        fits.
        """
        if node["reftarget"] in self.env.all_docs:
            return None
        candidates = (Path(f"{node['reftarget']}{suffix}") for suffix in source_suffix)
        found = next((c for c in candidates if c.is_file()), None)
        return None if found is None else (found, node.get("reftargetid"))

    def _unbound_target(
        self, node: pending_xref, refdoc: str
    ) -> tuple[Path, str | None]:
        """Resolve a link myst left with no domain at all.

        A page that includes a file holds links written relative to that
        file; any other page's links are written relative to wherever the
        page sits in the source tree -- doc2path is what answers that
        without assuming every docname sits flat under docs/source.
        """
        raw, _, anchor = node["reftarget"].partition("#")
        rendered = [path for path, page in INCLUDED.items() if page == refdoc]
        origin = (
            (ROOT / rendered[0]).parent
            if rendered
            else Path(self.env.doc2path(refdoc)).parent
        )
        return origin / raw, anchor or None

    def _source_page(self, target: str) -> str | None:
        """Return the docname of `target` where it is a page of docs/source."""
        stem, suffix = posixpath.splitext(target)
        docname = stem.removeprefix("docs/source/")
        if docname == stem or suffix not in source_suffix:
            return None
        # the file itself, and not only its docname: a missing page.rst
        # would otherwise read as page.md
        if not (ROOT / target).is_file():
            return None
        return docname if docname in self.env.found_docs else None

    def run(self, **_kwargs: object) -> None:
        """Rewrite every link naming a file of this repository."""
        # the list is taken before the tree is edited: replace_self on a
        # node the generator is standing on reparents its children under it
        for node in list(self.document.findall(pending_xref)):
            if node.get("reftype") != "myst":
                continue
            refdomain = node.get("refdomain")
            if refdomain == "doc":
                resolved = self._broken_doc_target(node)
            elif refdomain is None:
                refdoc = node.get("refdoc", self.env.docname)
                resolved = self._unbound_target(node, refdoc)
            else:
                resolved = None
            if resolved is None:
                continue
            found, anchor = resolved
            try:
                # "./tests/README.md" from a shim resolves to
                # "tests/README.md"; a page's own "../../README.md"
                # resolves to "README.md" the same way, against the real
                # filesystem, so a path climbing above ROOT raises rather
                # than reading as one of its own
                target = str(found.resolve().relative_to(ROOT))
            except ValueError:
                # outside the repository: nothing this can answer
                continue
            if target in INCLUDED:
                # handed back to myst as the link it would have been
                # written as, so the page title and the caption are its
                # business and not this file's
                self._to_page(node, INCLUDED[target], anchor)
            elif (ROOT / target).is_file():
                fragment = f"#{anchor}" if anchor else ""
                reference = nodes.reference(
                    "", "", refuri=f"{BLOB}{target}{fragment}", internal=False
                )
                reference.extend(node.children)
                node.replace_self(reference)
        self._blob_links()

    def _blob_links(self) -> None:
        """Turn a BLOB link to a file the docs render into a link to its page.

        myst made each BLOB link an external reference: one naming an
        included file or a page of docs/source becomes the xref myst makes
        from a relative link, which myst's resolver then renders alike.
        """
        for node in list(self.document.findall(nodes.reference)):
            url, _, anchor = node.get("refuri", "").partition("#")
            # the path ends at a query, as `?plain=1`, which the page drops
            url = url.partition("?")[0]
            target = url.removeprefix(BLOB)
            if target == url:
                continue
            page = INCLUDED.get(target) or self._source_page(target)
            if page is None:
                continue
            xref = pending_xref(
                "",
                nodes.inline("", "", *node.children, classes=["xref", "myst"]),
                reftype="myst",
                refexplicit=True,
                refdoc=self.env.docname,
            )
            xref.source, xref.line = node.source, node.line
            self._to_page(xref, page, anchor)
            node.replace_self(xref)


def setup(app: Sphinx) -> None:
    """Register the transform above; sphinx calls this."""
    app.add_post_transform(RootFileLinks)
