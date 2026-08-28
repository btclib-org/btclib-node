# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`scripts/chains/*.py` build their `Node` only under the `__main__` guard.

`scripts/` sits outside `testpaths` (`pyproject.toml`), so nothing here
runs those three scripts, and nothing collects a test that lived inside
one even if somebody wrote it there. A future author re-flattening a
script's `if __name__ == "__main__":` body -- the shape issue #579 found
in all three -- is invisible to the suite unless something outside
`scripts/` itself reads them. `Node.__init__`'s own refusal (issue #589)
catches the same mistake at runtime, but only where the process it
built in was actually re-imported under a non-`fork` start method; this
is the cheaper, static half, parsed rather than grepped so that a
wrapped line does not read as compliant.
"""

import ast
import textwrap
from pathlib import Path
from typing import TypeIs

import pytest

_SCRIPTS_DIR = Path(__file__).parents[2] / "scripts" / "chains"
_SCRIPTS = sorted(_SCRIPTS_DIR.glob("*.py"))


def _is_main_guard(node: ast.AST) -> TypeIs[ast.If]:
    """Whether `node` is `if __name__ == "__main__":`."""
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
    )


def _node_call_targets(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Every local name a `Node(...)` call could be written through.

    Two kinds, because `Node` reaches a call site two ways: `from
    btclib_node import Node` (optionally `as` something else) binds a
    bare name straight to it, read off every `ImportFrom` whose module
    is `btclib_node` and whose alias names `Node`; `import btclib_node`
    (optionally `as` something else) binds a module name instead, and a
    call goes through it as `<module_alias>.Node(...)`, read off every
    plain `Import` whose alias names `btclib_node`. Returned separately
    because the two are matched against a different `ast.Call.func`
    shape below -- an `ast.Name` for the first, an `ast.Attribute` for
    the second.
    """
    bare_names: set[str] = set()
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "btclib_node":
            bare_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "Node"
            )
        elif isinstance(node, ast.Import):
            module_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "btclib_node"
            )
    return bare_names, module_names


def _is_node_call(call: ast.Call, bare_names: set[str], module_names: set[str]) -> bool:
    """Whether `call` invokes whatever `Node` is bound to in this module."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in bare_names
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "Node"
        and isinstance(func.value, ast.Name)
        and func.value.id in module_names
    )


def _guard_ranges(tree: ast.Module) -> list[tuple[int, int | None]]:
    """Return the line range of each `__main__` guard in the module.

    Lexical, and deliberately nothing more. An earlier version of this
    followed a bare `name()` call from inside a guard into the function
    it named, so that `def main(): ...` called as `main()` read as
    guarded -- the ordinary shape a script grows into. Four review
    rounds each found a different way to make that resolution answer
    *guarded* for a `Node(...)` that really does run unguarded on the
    re-import: two module-level `def`s sharing a name, a `def` nested
    in the guard shadowing a module-level one, and then a `lambda`, a
    `class` and an `import ... as` doing the same shadowing without
    being a `def` at all. Each fix closed the case it was shown and
    reopened the class somewhere else, because deciding which
    definition a name reaches is Python's own binding semantics, and
    `ast` does not model them -- `symtable` does not answer it either:
    it reports a single plain `def` as assigned, exactly as it reports
    one shadowed by a `lambda`.

    So this asks the one question a parse tree can answer without
    guessing. A `Node(...)` is compliant when it sits inside a guard's
    own lines, and not otherwise. The cost is a false positive on
    `def main(): Node(...)` called from the guard: correct code,
    refused, with a message naming a guard that is already there. That
    is the direction to fail in -- the alternative was a check that
    called an unguarded `Node(...)` compliant, in the one test standing
    between this tree and the defect it exists for (issue #579). None
    of the three scripts under `scripts/chains/` builds its `Node`
    anywhere but directly under the guard, and the first one that wants
    to is where this becomes work rather than a note.
    """
    return [
        (node.lineno, node.end_lineno)
        for node in ast.walk(tree)
        if _is_main_guard(node)
    ]


def test_every_chain_script_has_a_main_guard() -> None:
    """Each script under `scripts/chains/` has found by the glob above."""
    assert [script.name for script in _SCRIPTS] == [
        "mainnet.py",
        "signet.py",
        "testnet.py",
    ]


def test_every_node_call_is_lexically_inside_the_main_guard() -> None:
    """A `Node(...)` call outside the `__main__` guard fails this test.

    Parsed with `ast` rather than matched with a regular expression, so
    a wrapped call is not what decides the answer, and resolved through
    whatever name `Node` is actually bound to in the script -- `from
    btclib_node import Node as N` followed by `N(...)` is still found,
    where matching the literal string `"Node"` would not be. A guard's
    own line range is read from the parsed tree rather than assumed
    from indentation. It is the guard's own range and nothing beyond
    it: `_guard_ranges` above is where that limit is argued, and a
    `Node(...)` inside a `def main():` the guard calls is refused
    rather than followed.
    """
    for script in _SCRIPTS:
        tree = ast.parse(script.read_text(), filename=str(script))
        guard_ranges = _guard_ranges(tree)
        assert guard_ranges, f"{script.name}: no `if __name__ == '__main__':` guard"

        bare_names, module_names = _node_call_targets(tree)
        node_calls = [
            call
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and _is_node_call(call, bare_names, module_names)
        ]
        assert node_calls, f"{script.name}: no `Node(...)` call found"

        for call in node_calls:
            assert any(
                start <= call.lineno <= (end or start) for start, end in guard_ranges
            ), f"{script.name}: Node(...) at line {call.lineno} is outside the guard"


def test_node_call_targets_resolves_an_aliased_bare_import() -> None:
    """`from btclib_node import Node as N` binds `N`, not `module_names`.

    The reviewer's own counter-case for the alias defeating a literal
    `"Node"` match, checked directly against the resolver rather than
    only through the three shipped scripts, none of which alias the
    import.
    """
    tree = ast.parse("from btclib_node import Node as N\n")
    bare_names, module_names = _node_call_targets(tree)
    assert bare_names == {"N"}
    assert module_names == set()


def test_node_call_targets_resolves_a_module_import() -> None:
    """`import btclib_node as bn` binds `bn` as a module name."""
    tree = ast.parse("import btclib_node as bn\n")
    bare_names, module_names = _node_call_targets(tree)
    assert bare_names == set()
    assert module_names == {"bn"}


def test_is_node_call_matches_the_module_attribute_form() -> None:
    """`bn.Node(...)` is a `Node` call when `bn` names `btclib_node`."""
    tree = ast.parse("bn.Node(config=None)\n")
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
    assert _is_node_call(call, bare_names=set(), module_names={"bn"})
    assert not _is_node_call(call, bare_names=set(), module_names=set())


def test_a_node_call_in_a_function_the_guard_calls_is_refused() -> None:
    """The deliberate false positive, pinned so it stays deliberate.

    `def main(): Node(...)` with `if __name__ == "__main__": main()` is
    correct and this test refuses it. `_guard_ranges` is where that
    trade is argued; this pins the behaviour so that whoever changes it
    changes it on purpose, and so that the argument and the code cannot
    drift apart silently.
    """
    source = textwrap.dedent(
        """
        def main():
            Node(config=None)

        if __name__ == "__main__":
            main()
        """
    )
    tree = ast.parse(source)
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Node"
    )
    assert not any(
        start <= call.lineno <= (end or start) for start, end in _guard_ranges(tree)
    )


# Each of these defeated an earlier version of this check that resolved a
# bare `name()` call from inside the guard to the function it named: the
# call the guard makes reaches one binding, a bare call after the guard
# reaches another, and only the second runs on the re-import. They are
# kept because they are the evidence for `_guard_ranges`' own limit, and
# because a resolver reintroduced without them would pass its own tests.
@pytest.mark.parametrize(
    "shadow",
    [
        pytest.param("    def helper():\n        pass\n", id="a-nested-def"),
        pytest.param("    helper = lambda: None  # noqa: E731\n", id="a-lambda"),
        pytest.param("    class helper:\n        pass\n", id="a-class"),
        pytest.param("    import sys as helper\n", id="an-import-as"),
    ],
)
def test_a_node_call_shadowed_out_of_the_guard_is_still_refused(shadow: str) -> None:
    """Every shape that defeated name resolution now reads as a violation.

    The `Node(...)` sits in a module-level `def helper` that the bare
    call after the guard really does reach on a re-import, while the
    call inside the guard reaches whatever `shadow` binds. A resolver
    attributed the guard's range to the module-level definition and
    called the unguarded `Node(...)` compliant; a lexical range cannot,
    because that `Node(...)` is not on a line the guard covers.
    """
    source = "def helper():\n    Node(config=None)\n\n"
    source += 'if __name__ == "__main__":\n' + shadow + "    helper()\n\n"
    source += "helper()\n"
    tree = ast.parse(source)
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Node"
    )
    assert not any(
        start <= call.lineno <= (end or start) for start, end in _guard_ranges(tree)
    )
