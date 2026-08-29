# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Every `fuzz/corpus/` seed still parses, and reserializes to itself.

`fuzz/corpus/<harness>/` is a *seed* corpus: the valid serializations a
fuzzer starts from, which is what gives its first mutations somewhere
useful to be near. This module is what holds them to that, so a seed
that stops being a valid serialization is a red run here rather than a
starting point silently worth less every week.

**An input a crash was found on does not belong in that directory.**
What fixes such a crash is a parser that refuses the input, and this
module asserts the opposite of every file there -- so the regression
would be red the day it was fixed, and the only way back to green would
be to delete it. A crash the sentinel finds becomes an ordinary test in
this suite instead, naming the octets and what the parser is now
expected to do with them.

Neither `fuzz/fuzz_*.py` nor a seed is imported: every harness imports
atheris at module level, and atheris is installed by the `fuzz`
dependency group alone, which nothing but `fuzz.yml` asks for -- so this
module parses a harness's source with `ast` and resolves what it *names*
against the installed package instead.

`ENTRY_POINTS` is that name: a module-level tuple of `"module:Qual.name"`
literals, which `_entry_points` below reads with `ast.literal_eval`. What
it buys is that a harness aimed at something this tree no longer has
fails in the suite, on the pull request that moved the name, rather than
on the sentinel's own day.

A seed is accepted where a declared entry point returns rather than
refusing it, and `_parsed` treats the same family the harness
suppresses as a refusal -- `fuzz/fuzz_reject.py`'s own docstring argues
which it is. `_REFUSED`, not `None`, is that refusal's own marker:
`fuzz.fuzz_process_message:dispatch` (issue #698) is a dispatch through
a node rather than a parse, so `None` is its own ordinary, *accepted*
return, and reusing it for "refused" would count that harness's every
accepted seed as refused instead.

`fuzz_process_message.py` is the one harness this module does import,
because its own `ENTRY_POINTS` -- `dispatch`, module docstring there --
is not reachable through the installed package the way the other three
harnesses' own are, and needs no atheris to run: `_resolve` below loads
it from `fuzz/` by path rather than through `importlib.import_module`,
once, and keeps it, since `dispatch` reuses one `Node` across calls by
design and a fresh import per call would rebuild it every time instead.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from btclib.exceptions import BTClibException

if TYPE_CHECKING:
    from types import ModuleType

_FUZZ = Path(__file__).parent.parent / "fuzz"
_CORPUS = _FUZZ / "corpus"

# What `_parsed` returns for a spec that refused `data` -- never `None`,
# module docstring above is where that is argued.
_REFUSED = object()

_loaded_fuzz_modules: dict[str, ModuleType] = {}


def _harnesses() -> tuple[Path, ...]:
    """Return every fuzz/fuzz_*.py, sorted for a stable parametrize order."""
    return tuple(sorted(_FUZZ.glob("fuzz_*.py")))


def _entry_points(path: Path) -> tuple[str, ...]:
    """Return the specs a harness's own module-level ENTRY_POINTS names."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    declared: list[tuple[str, ...]] = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "ENTRY_POINTS"
    ]
    return tuple(spec for specs in declared for spec in specs)


def _load_fuzz_module(module_name: str) -> ModuleType:
    """Load a `fuzz.<name>` module from `fuzz/` by path, once, and keep it.

    Not `importlib.import_module`: `fuzz/` carries no `__init__.py` and
    is not on `sys.path` through the installed package the way
    `btclib_node` is, so this is the one harness `_resolve` reaches by
    path -- module docstring above is where that split, and why it is
    kept rather than reloaded per call, are argued.
    """
    if module_name not in _loaded_fuzz_modules:
        path = (_FUZZ.parent / Path(*module_name.split("."))).with_suffix(".py")
        module_spec = importlib.util.spec_from_file_location(module_name, path)
        if module_spec is None or module_spec.loader is None:
            err_msg = f"cannot load {module_name} from {path}"
            raise ImportError(err_msg)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        _loaded_fuzz_modules[module_name] = module
    return _loaded_fuzz_modules[module_name]


def test_a_fuzz_module_spec_that_cannot_be_built_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safety branch `_load_fuzz_module` carries for a spec it cannot build.

    Never true through `_resolve`'s own module names: every one of them
    is forced to a `.py` suffix before `spec_from_file_location` sees it,
    and that function always resolves a loader for one. Driven directly
    instead, `spec_from_file_location` patched to answer the way it does
    for a suffix it cannot handle -- `None` -- which is the one case
    `_load_fuzz_module` refuses rather than executing.
    """
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *a, **k: None)
    with pytest.raises(ImportError, match="cannot load"):
        _load_fuzz_module("fuzz.unreachable_698")


def _resolve(spec: str) -> Any:
    """Import what "module:Qual.name" names, against the installed package."""
    module_name, _, qualname = spec.partition(":")
    obj: Any = (
        _load_fuzz_module(module_name)
        if module_name.startswith("fuzz.")
        else importlib.import_module(module_name)
    )
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _seeds(harness: str) -> tuple[Path, ...]:
    """Return one harness's own seed files, sorted."""
    return tuple(sorted((_CORPUS / harness).glob("*.bin")))


def _parsed(spec: str, data: bytes) -> Any:
    """Return what `spec` parses `data` into, or `_REFUSED` where it refuses.

    The family is the one the harness suppresses, so what counts as a
    refusal here is what counts as one there.
    """
    try:
        return _resolve(spec)(data)
    except BTClibException:
        return _REFUSED


_HARNESSES = _harnesses()
_SEEDS = tuple((path.stem, seed) for path in _HARNESSES for seed in _seeds(path.stem))
_SEED_IDS = [str(seed.relative_to(_CORPUS)) for _, seed in _SEEDS]


def test_the_corpus_is_not_empty() -> None:
    """Every assertion below quantifies over _HARNESSES and _SEEDS."""
    assert _HARNESSES, f"{_FUZZ} holds no fuzz_*.py"
    assert _SEEDS, f"{_CORPUS} holds no seed"


@pytest.mark.parametrize("path", _HARNESSES, ids=lambda p: p.stem)
def test_a_harness_declares_entry_points(path: Path) -> None:
    """A harness naming nothing is one this module cannot check."""
    assert _entry_points(path), f"{path.name} declares no non-empty ENTRY_POINTS"


@pytest.mark.parametrize("path", _HARNESSES, ids=lambda p: p.stem)
def test_a_declared_entry_point_resolves(path: Path) -> None:
    """What a harness names is still callable in this tree."""
    for spec in _entry_points(path):
        assert callable(_resolve(spec)), f"{path.name}'s {spec} is not callable"


@pytest.mark.parametrize("path", _HARNESSES, ids=lambda p: p.stem)
def test_a_harness_has_a_corpus_directory(path: Path) -> None:
    """A harness with no seed is one nothing in the suite has ever run."""
    assert _seeds(path.stem), f"fuzz/corpus/{path.stem}/ holds no seed"


def test_a_corpus_directory_has_a_harness() -> None:
    """A directory outliving the harness it was named for is dead weight."""
    named = {path.stem for path in _HARNESSES}
    orphans = sorted(d.name for d in _CORPUS.iterdir() if d.name not in named)
    assert not orphans, f"fuzz/corpus/ directories with no harness: {orphans}"


@pytest.mark.parametrize("seed", [seed for _, seed in _SEEDS], ids=_SEED_IDS)
def test_a_seed_has_no_trailing_newline(seed: Path) -> None:
    r"""A seed is wire octets, and a fixer appending to one invalidates it.

    `fuzz_rpc_head`'s own seeds are the one exception, not by luck but
    by protocol: HTTP's header terminator is itself `b"\r\n\r\n"`, so
    every valid seed for that harness ends in a newline as a matter of
    the wire format `parse_request_head` reads, not as a fixer's
    accidental addition -- `.pre-commit-config.yaml`'s own
    `end-of-file-fixer`/`mixed-line-ending` excludes, measured against
    exactly this directory (issue #516), are the other half of this
    same fact.
    """
    if seed.parent.name == "fuzz_rpc_head":
        return
    assert not seed.read_bytes().endswith(b"\n"), f"{seed} ends with a newline"


@pytest.mark.parametrize(("harness", "seed"), _SEEDS, ids=_SEED_IDS)
def test_a_seed_parses_and_reserializes_to_itself(harness: str, seed: Path) -> None:
    """A declared entry point accepts the seed, reserializing it where it can.

    Both halves at once for a parser-style entry point, the second
    being what says the first accepted the octets it was given rather
    than a prefix of them. An entry point that dispatches rather than
    parses -- `None` on acceptance, `_REFUSED` module-level comment
    above is where that is argued -- has nothing to reserialize and is
    held to the first half alone.
    """
    data = seed.read_bytes()
    specs = _entry_points(_FUZZ / f"{harness}.py")
    accepted = {
        spec: parsed
        for spec in specs
        if (parsed := _parsed(spec, data)) is not _REFUSED
    }
    assert accepted, f"{seed} is refused by every entry point {harness} declares"
    mismatched = [
        spec
        for spec, obj in accepted.items()
        if obj is not None and obj.serialize() != data
    ]
    assert not mismatched, f"{seed} does not reserialize to itself under {mismatched}"


def test_a_callback_level_refusal_of_fuzz_process_message_is_not_accepted() -> None:
    """`fuzz.fuzz_process_message:dispatch` tells a refusal from acceptance.

    `p2p.callbacks.ping` calls `Ping.parse`, which raises
    `BTClibValueError` for a nonce shorter than the eight bytes the wire
    format carries -- a refusal `handle_p2p`'s own `except` logs and does
    not discourage the peer for (`main.handle_p2p`, `p2p/main.py`), which
    `dispatch` used to answer `None` for indistinguishably from genuine
    acceptance (issue #698's own review, round 1, finding 2), the same
    `None` a well-formed ping is accepted with. `module._TYPES.index`
    finds where `ping` sits in the sorted selector rather than the index
    being hardcoded here, since a command added or removed from
    `p2p.callbacks` would otherwise silently point this test at a
    different command instead of failing loudly.
    """
    spec = "fuzz.fuzz_process_message:dispatch"
    module = _load_fuzz_module("fuzz.fuzz_process_message")
    ping_selector = module._TYPES.index("ping")
    refused = bytes([ping_selector]) + b"\x01\x02"
    accepted = bytes([ping_selector]) + b"\x00" * 8
    assert _parsed(spec, refused) is _REFUSED
    assert _parsed(spec, accepted) is None


def test_a_malformed_payload_is_refused() -> None:
    """The control on the two tests above: acceptance can fail.

    An empty payload is what every entry point declared here refuses --
    `btclib.var_int.parse` having nothing to read for the three parsers,
    and `fuzz.fuzz_process_message:dispatch` (issue #698) having no
    selector byte to pick a message type with -- so this says the tests
    above measure acceptance rather than reporting it.
    """
    specs = [spec for path in _HARNESSES for spec in _entry_points(path)]
    assert specs
    assert all(_parsed(spec, b"") is _REFUSED for spec in specs)
