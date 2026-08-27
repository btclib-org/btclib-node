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
which it is.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

import pytest
from btclib.exceptions import BTClibException

_FUZZ = Path(__file__).parent.parent / "fuzz"
_CORPUS = _FUZZ / "corpus"


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


def _resolve(spec: str) -> Any:
    """Import what "module:Qual.name" names, against the installed package."""
    module_name, _, qualname = spec.partition(":")
    obj: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _seeds(harness: str) -> tuple[Path, ...]:
    """Return one harness's own seed files, sorted."""
    return tuple(sorted((_CORPUS / harness).glob("*.bin")))


def _parsed(spec: str, data: bytes) -> Any:
    """Return what `spec` parses `data` into, or None where it refuses.

    The family is the one the harness suppresses, so what counts as a
    refusal here is what counts as one there.
    """
    try:
        return _resolve(spec)(data)
    except BTClibException:
        return None


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
    """A seed is wire octets, and a fixer appending to one invalidates it."""
    assert not seed.read_bytes().endswith(b"\n"), f"{seed} ends with a newline"


@pytest.mark.parametrize(("harness", "seed"), _SEEDS, ids=_SEED_IDS)
def test_a_seed_parses_and_reserializes_to_itself(harness: str, seed: Path) -> None:
    """A declared entry point accepts the seed and writes it back out.

    Both halves at once, the second being what says the first accepted
    the octets it was given rather than a prefix of them.
    """
    data = seed.read_bytes()
    specs = _entry_points(_FUZZ / f"{harness}.py")
    accepted = {
        spec: parsed for spec in specs if (parsed := _parsed(spec, data)) is not None
    }
    assert accepted, f"{seed} is refused by every entry point {harness} declares"
    mismatched = [spec for spec, obj in accepted.items() if obj.serialize() != data]
    assert not mismatched, f"{seed} does not reserialize to itself under {mismatched}"


def test_a_malformed_payload_is_refused() -> None:
    """The control on the two tests above: acceptance can fail.

    An empty payload is what every entry point declared here refuses,
    `btclib.var_int.parse` having nothing to read, so this says the
    tests above measure acceptance rather than reporting it.
    """
    specs = [spec for path in _HARNESSES for spec in _entry_points(path)]
    assert specs
    assert all(_parsed(spec, b"") is None for spec in specs)
