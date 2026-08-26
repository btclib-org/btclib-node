# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for what this package exports.

`py.typed` says the types are supported, and which names are public is
the other half of that sentence -- section 7 of the organization
standard reserves this bullet from the escape clause that excuses the
rest of the convention tests, for exactly that reason.

Every module and package under `src/btclib_node/` declares `__all__`,
at every depth, the way `btclib`'s own `tests/all_test.py` -- the shape
this is ported from -- asks of `btclib`. A name is public here because a
list says so, not because it happens to lack a leading underscore, and
the tests below are what keeps that list true rather than a reviewer
noticing it drift.

Written against the names rather against a total, so that a deliberate
addition is one line here and an accidental one is a failure.
"""

import ast
from importlib import import_module
from pathlib import Path
from pkgutil import walk_packages
from types import ModuleType
from typing import TYPE_CHECKING

import btclib_node

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# What a module defines without a leading underscore and deliberately
# does not export, with the reason beside each entry: a name added here
# is a decision, and a name that has to be added here to make the suite
# pass is one that was about to become public by accident.
UNEXPORTED = {
    # the loop's own idle-wait tuning, read by nothing outside it
    # (IDLE_SLEEP_SECONDS); STOP_TIMEOUT is read and patched in place by
    # the shutdown tests, which is the suite exercising its own module
    # rather than a caller importing the name
    "btclib_node": ["IDLE_SLEEP_SECONDS", "STOP_TIMEOUT"],
    # called only by the four leaves' own constructors, in this module
    "btclib_node.chains": ["create_genesis"],
    # the module-level singleton `Config.chain`'s default already closes
    # over; nothing outside config.py reaches for the constant itself
    "btclib_node.config": ["DEFAULT_CHAIN"],
    # download.py's own burst size, read only where it is defined
    "btclib_node.download": ["MAX_BLOCKS_PER_GETDATA_BURST"],
    # update_chain's own failure-path helpers, called from nowhere else
    "btclib_node.main": ["finish_sync", "update_header_index"],
    # inputs to the one figure filter_size.py publishes, not needed on
    # their own outside it
    "btclib_node.p2p.filter_size": [
        "BYTES_PER_FILTER_ELEMENT",
        "ELEMENTS_PER_BUSY_MODERN_BLOCK",
    ],
    # the header/body split point, read only inside the parser that uses it
    "btclib_node.rpc.connection": ["HEADER_TERMINATOR"],
    # type_error's own vocabulary lookup, called from nowhere else
    "btclib_node.rpc.errors": ["json_type_name"],
}


def public_name(dotted: str) -> bool:
    """Whether every component of a dotted module name is public."""
    return not any(part.startswith("_") for part in dotted.split("."))


def package_modules() -> list[ModuleType]:
    """Return every module and package of this package, private ones out.

    Found rather than listed: one added to `btclib_node` is one these
    tests ask about, and the walk is the whole tree rather than the top
    level, the packages having submodules a caller reaches by name --
    `btclib_node.p2p.address`, `btclib_node.rpc.errors` -- and not
    everything reachable from the package root alone.
    """
    return [
        btclib_node,
        *(
            import_module(name)
            for _, name, _ in walk_packages(btclib_node.__path__, "btclib_node.")
            if public_name(name)
        ),
    ]


def module_scope(body: Iterable[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield the statements a module executes in its own namespace.

    Every statement of the body, and then inside each compound one,
    which runs at module scope too: an import in a module-level `try`,
    `if` or `with` binds a global exactly as a top-level one does. A
    function or a class opens a scope of its own, so an import in
    either binds nothing here, and neither is descended into.
    """
    for node in body:
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        nested: list[ast.stmt] = []
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(node, field, None)
            if isinstance(statements, list):
                nested += statements
        for clause in (*getattr(node, "handlers", ()), *getattr(node, "cases", ())):
            nested += clause.body
        yield from module_scope(nested)


def imported_names_in(source: str) -> set[str]:
    """Return the names the import statements of one module source bind."""
    return {
        alias.asname or alias.name.split(".")[0]
        for node in module_scope(ast.parse(source).body)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


def imported_names(module: ModuleType) -> set[str]:
    """Return the names a module's own import statements bind.

    Read off the source rather than the module object, there being
    nothing in a module's namespace to say how a name got there --
    `chainstate/__init__.py` imports `BlockIndex` to build `Chainstate`
    with it, and that import binds the name in `vars(module)` exactly as
    defining it there would.
    """
    return imported_names_in(Path(str(module.__file__)).read_text(encoding="utf-8"))


def defined_public_names(module: ModuleType) -> set[str]:
    """Return the public names a module defines itself.

    Everything in its namespace, minus the underscored, minus what it
    imported, minus a submodule: importing a submodule makes it an
    attribute of its package (`btclib_node.p2p` carries `address` as
    soon as anything imports it), and that attribute is not a name the
    package itself defined.
    """
    imported = imported_names(module)
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and name not in imported
        and not isinstance(value, ModuleType)
    }


def test_every_module_declares_all() -> None:
    """Every module and package under `src/btclib_node/` declares `__all__`.

    An empty list is a legitimate answer for a module with nothing
    public of its own -- `btclib_node.p2p`'s own `__init__.py` only
    documents what its submodules publish -- so the declaration is
    checked to be there rather than to be non-empty.
    """
    modules = package_modules()
    assert len(modules) > 1, "the walk found only the package root"
    for module in modules:
        names = getattr(module, "__all__", None)
        assert names is not None, f"{module.__name__} declares no __all__"


def test_every_exported_name_exists() -> None:
    """An `__all__` entry that names nothing is a broken `import *`."""
    for module in package_modules():
        for name in module.__all__:
            assert hasattr(module, name), f"{module.__name__}.{name} is not there"


def test_the_import_scan_reaches_a_module_level_try_and_match() -> None:
    """A module-level `try`/`except` or `match`/`case` still binds a global.

    The census above is only as good as this scan: an optional dependency
    imported in a `try`, or a name imported in one arm of a `match`, is
    exactly the kind of import `defined_public_names` has to recognise as
    imported rather than defined, and reading `tree.body` alone would
    have missed both. A function body and a class body are not module
    scope, so an import inside either binds nothing here.
    """
    source = (
        "try:\n"
        "    from dependency import PublicType\n"
        "except ImportError:\n"
        "    from fallback import PublicType\n"
        "match flavor:\n"
        "    case 'a':\n"
        "        import from_case\n"
        "def f():\n"
        "    import local\n"
        "class C:\n"
        "    import attribute\n"
    )
    assert imported_names_in(source) == {"PublicType", "from_case"}


def test_nothing_becomes_public_by_accident() -> None:
    """Every public name is exported or recorded in `UNEXPORTED`.

    This is the check the underscore convention alone cannot make: a
    helper that grows into a name callers depend on does so silently,
    where a declared list takes an edit. `sorted` is what the failure
    reads as -- the names not accounted for, against the ones that are.
    """
    for module in package_modules():
        kept_out = sorted(defined_public_names(module) - set(module.__all__))
        assert kept_out == UNEXPORTED.get(module.__name__, []), (
            f"{module.__name__} defines public names that are neither"
            f" exported nor recorded in UNEXPORTED: {kept_out}"
        )
