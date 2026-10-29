# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Diff this node's RPC surface against a Bitcoin Core OpenRPC document.

The document is what `getopenrpcinfo` answers, written by
`fetch_core_openrpc.py` beside this file; `tests/_data/README.md` says
which release the committed one is. Three comparisons are made:

- method names, against `rpc.callbacks.callbacks`, the dispatch table;
- parameter names and their order, against `rpc.callbacks.arg_names`,
  with the options of an `OBJ_NAMED_PARAMS` position against
  `rpc.callbacks.named_only`;
- result fields, against the `Result` sections of `rpc.help.HELP_TEXT`.

A result field is a path: `.key` for a member, `[]` for an array's
elements, `*` for a member of an object whose keys are data (Core's
`OBJ_DYN`, an `additionalProperties` schema). Where a method has several
results, as `getblock` has one per verbosity, the fields of all of them
are compared together. Types are not compared.

A field help.py documents and the code does not answer is not seen:
this reads help.py, not a live answer. The report says so on its second
line.

The report is sorted, so the same inputs print the same text.

    uv run python scripts/openrpc/diff_openrpc.py \
        tests/_data/core_openrpc.json > scripts/openrpc/divergences.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from btclib_node.rpc.callbacks import arg_names, callbacks, named_only
from btclib_node.rpc.help import HELP_TEXT

_KEY_LINE = re.compile(r'^(\s*)"([^"]*)" : (\S)')
_OPEN_LINE = re.compile(r"^\s*([{\[])(?:\s|$)")
_CLOSE_LINE = re.compile(r"^\s*[}\]],?\s*$")
_ELLIPSIS_LINE = re.compile(r"^\s*\.\.\.(,\s+\S)?")
_LIMIT = (
    "Result fields are read from help.py, not from a live answer: a field"
    " help.py documents and the code does not answer is not seen."
)
_RESULT_HEADER = re.compile(r"^Result( \(.*\))?:$")


@dataclass
class Node:
    """A value in a help text's result: an object, an array or a leaf."""

    kind: str
    # an object whose help text elides members with `...`
    open: bool = False
    # elided with a description, "...,  Same output as verbosity = 1",
    # which speaks for every object inside it too
    elided: bool = False
    members: dict[str, list[Node]] = field(default_factory=dict)
    items: list[Node] = field(default_factory=list)


def _result_sections(text: str) -> list[list[str]]:
    """Return the lines of each `Result` section of one help text."""
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in text.split("\n"):
        if _RESULT_HEADER.match(line):
            current = []
            sections.append(current)
        elif current is not None and line == "":
            current = None
        elif current is not None:
            current.append(line)
    return sections


def _kind(opener: str) -> str:
    return "object" if opener == "{" else "array" if opener == "[" else "leaf"


def parse_result(lines: list[str]) -> Node | None:
    """Return the value one `Result` section describes.

    A line opening `{` or `[` starts a container, a line holding only
    `}` or `]` ends one, and a line `"key" : ...` is an object member.
    A line starting `...` in an object says the object has members the
    text does not list. Where a description follows, as in "...,  Same
    output as verbosity = 1", so does every object inside it; a bare
    `...` may only say that a dynamic object has more keys. Every other
    line is a leaf element, an ellipsis in an array or a description
    continued from the line above.
    """
    root: Node | None = None
    stack: list[Node] = []
    for line in lines:
        if _CLOSE_LINE.match(line):
            del stack[-1:]
            continue
        ellipsis = _ELLIPSIS_LINE.match(line)
        if ellipsis:
            _mark_open(stack, elided=ellipsis.group(1) is not None)
            continue
        found = _line_node(line)
        if found is None:
            if root is None and line.strip():
                root = Node("leaf")
            continue
        node, key = found
        if root is None:
            root = node
        else:
            _attach(stack, node, key)
        if node.kind != "leaf":
            stack.append(node)
    if root is not None:
        _inherit_elided(root, parent_elided=False)
    return root


def _line_node(line: str) -> tuple[Node, str | None] | None:
    """Return the node a line starts and its key, or None for any other line."""
    key_match = _KEY_LINE.match(line)
    if key_match:
        return Node(_kind(key_match.group(3))), key_match.group(2)
    open_match = _OPEN_LINE.match(line)
    if open_match:
        return Node(_kind(open_match.group(1))), None
    return None


def _attach(stack: list[Node], node: Node, key: str | None) -> None:
    """Add `node` to the container it is in, where it is in one."""
    if not stack:
        return
    top = stack[-1]
    if key is not None and top.kind == "object":
        top.members.setdefault(key, []).append(node)
    elif key is None and top.kind == "array":
        top.items.append(node)


def _mark_open(stack: list[Node], *, elided: bool) -> None:
    """Mark open the object an ellipsis line is in, where it is in one."""
    if stack and stack[-1].kind == "object":
        stack[-1].open = True
        stack[-1].elided |= elided


def _inherit_elided(node: Node, *, parent_elided: bool) -> None:
    """Mark open every object inside an elided one."""
    node.elided = node.elided or parent_elided
    node.open = node.open or (node.elided and node.kind == "object")
    children = [sub for subs in node.members.values() for sub in subs] + node.items
    for child in children:
        _inherit_elided(child, parent_elided=node.elided)


def help_results(text: str) -> list[Node]:
    """Return every result a help text documents."""
    return [
        node
        for node in (parse_result(lines) for lines in _result_sections(text))
        if node is not None
    ]


def _alternatives(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a schema's alternatives, `oneOf` and `anyOf` flattened."""
    branches = schema.get("oneOf") or schema.get("anyOf")
    if branches is None:
        return [schema]
    return [leaf for branch in branches for leaf in _alternatives(branch)]


def _is_any(schema: dict[str, Any]) -> bool:
    """Whether a schema says nothing of its value, as Core's `ANY` does."""
    return not {"type", "oneOf", "anyOf", "const"} & schema.keys()


def compare_fields(
    path: str, nodes: list[Node], schemas: list[dict[str, Any]]
) -> list[str]:
    """Return the field divergences between help nodes and Core schemas."""
    flat = [alt for schema in schemas for alt in _alternatives(schema)]
    if not nodes or any(_is_any(alt) for alt in flat):
        return []
    out: list[str] = []
    for kind, compare in (("object", _compare_objects), ("array", _compare_arrays)):
        ours = [node for node in nodes if node.kind == kind]
        core = [schema for schema in flat if schema.get("type") == kind]
        if ours and not core:
            out.append(f"{path or '(result)'}: an {kind} in help.py, not in Core")
        elif ours:
            out += compare(path, ours, core)
    return out


def _compare_objects(
    path: str, objects: list[Node], schemas: list[dict[str, Any]]
) -> list[str]:
    """Compare the members of help objects with those of Core's objects."""
    props, dynamic = _core_members(schemas)
    members: dict[str, list[Node]] = {}
    for node in objects:
        for key, subs in node.members.items():
            members.setdefault(key, []).extend(subs)
    is_open = all(node.open for node in objects)
    out: list[str] = []
    for key in sorted(members.keys() | props.keys()):
        if key in props and key in members:
            out += compare_fields(f"{path}.{key}", members[key], props[key])
        elif key in members and dynamic:
            out += compare_fields(f"{path}.*", members[key], dynamic)
        elif key in members:
            out.append(f"{path}.{key}: in help.py, not in Core")
        elif not is_open:
            out.append(f"{path}.{key}: in Core, not in help.py")
    return out


def _core_members(
    schemas: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Return the schemas of Core's named members, and of its dynamic ones."""
    props: dict[str, list[dict[str, Any]]] = {}
    dynamic: list[dict[str, Any]] = []
    for schema in schemas:
        for key, sub in schema.get("properties", {}).items():
            props.setdefault(key, []).append(sub)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            dynamic.append(extra)
    return props, dynamic


def _compare_arrays(
    path: str, arrays: list[Node], schemas: list[dict[str, Any]]
) -> list[str]:
    """Compare the elements of help arrays with Core's item schemas."""
    items: list[dict[str, Any]] = []
    for schema in schemas:
        sub = schema.get("items", {})
        items += sub if isinstance(sub, list) else [sub]
    elements = [item for node in arrays for item in node.items]
    return compare_fields(f"{path}[]", elements, items)


def _positions(method: str) -> list[tuple[str, ...]]:
    """Return this node's names for each position of `method`."""
    options = set(named_only.get(method, ()))
    return [
        tuple(name for name in entry.split("|") if name not in options)
        for entry in arg_names[method]
    ]


def _core_positions(method: dict[str, Any]) -> list[tuple[str, ...]]:
    return [
        (param["name"], *param.get("x-bitcoin-aliases", ()))
        for param in method["params"]
    ]


def _render(positions: list[tuple[str, ...]]) -> str:
    return "(" + ", ".join("|".join(names) for names in positions) + ")"


def compare_params(name: str, method: dict[str, Any]) -> list[str]:
    """Return the parameter divergences of one method both sides serve."""
    out: list[str] = []
    core, ours = _core_positions(method), _positions(name)
    if core != ours:
        out.append(f"{name}: Core {_render(core)}, btclib-node {_render(ours)}")
    options = named_only.get(name)
    if options:
        index = next(
            i for i, entry in enumerate(arg_names[name]) if options[0] in entry
        )
        schema = method["params"][index]["schema"]
        core_options = tuple(schema.get("properties", {}))
        if core_options != options:
            out.append(
                f"{name} options: Core ({', '.join(core_options)}),"
                f" btclib-node ({', '.join(options)})"
            )
    return out


def diff(document: dict[str, Any]) -> str:
    """Return the report for one OpenRPC document."""
    methods = {method["name"]: method for method in document["methods"]}
    served = set(callbacks)
    lines = [
        f"OpenRPC {document['openrpc']} document, version {document['info']['version']}",
        _LIMIT,
        "",
        "## Methods Core serves and btclib-node does not",
        "",
    ]
    lines += [
        f"{methods[name]['x-bitcoin-category']} {name}"
        for name in sorted(
            methods.keys() - served, key=lambda n: (methods[n]["x-bitcoin-category"], n)
        )
    ]
    lines += ["", "## Methods btclib-node serves and Core does not", ""]
    lines += sorted(served - methods.keys())
    lines += ["", "## Parameters", ""]
    common = sorted(served & methods.keys())
    for name in common:
        lines += compare_params(name, methods[name])
    lines += ["", "## Result fields", ""]
    for name in common:
        nodes = help_results(HELP_TEXT[name]) if name in HELP_TEXT else []
        if not nodes:
            lines.append(f"{name}: no Result in help.py")
            continue
        schema = methods[name]["result"]["schema"]
        lines += [f"{name} {entry}" for entry in compare_fields("", nodes, [schema])]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Print the report for the document named on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path, help="getopenrpcinfo's answer")
    args = parser.parse_args(argv)
    document = json.loads(args.document.read_text(encoding="utf-8"))
    sys.stdout.write(diff(document))
    return 0


if __name__ == "__main__":
    sys.exit(main())
