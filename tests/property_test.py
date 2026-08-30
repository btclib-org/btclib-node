# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The property layer under the harnesses in `fuzz/`.

Section 7 of [the organization standard][std] keys this layer on the
property section 10 keys the fuzzer on -- nobody standing between a
parser and an adversary who chooses the bytes -- and this tree has that
property: `fuzz.yml`'s own header says so, and each harness under
`fuzz/` names what it owns of the surface. The two answer different
questions. A property test answers *does this hold over the domain I
described*; a fuzzer answers *what is in the domain I did not
describe*, and the second presupposes the first, a fuzzer extending no
described domain having nothing to contradict. Until
btclib-org/btclib-node#742 this tree ran the second without the first.

The property is the one every harness in `fuzz/` already states in
prose and encodes as a `contextlib.suppress`: **over unconstrained
octets, a declared entry point either returns or raises
`BTClibException`, and nothing else.** Each `fuzz_target` there
suppresses exactly that family and lets atheris report anything else as
a finding; each test here suppresses exactly that family and lets
pytest report anything else as a failure. The same sentence, over a
domain this file describes and `fuzz.yml`'s ten minutes then extend.

`ENTRY_POINTS` is the walk, not a list written here: the specs come
from the harnesses' own module-level declarations, read by
`fuzz_corpus_test`'s `_entry_points` and resolved by its `_resolve`.
Reusing those rather than writing a second walk is the point -- two
walks over one fact are two things that can disagree, and a harness
added for a new parser gets a property test by being added, the way it
already gets a corpus check. That those helpers are underscore-named is
what says they are this suite's own and not a public surface; they are
imported here rather than copied for the same reason
`fuzz_corpus_test`'s own docstring gives for parsing `ENTRY_POINTS`
instead of hard-coding it.

**What the walk leaves out, and why that is structural rather than a
list.** `_resolve` already splits its specs in two: a `fuzz.`-prefixed
module is loaded from `fuzz/` by path, everything else is imported from
the installed package. This file draws for the second kind only. The
`fuzz.`-prefixed one today is `fuzz.fuzz_process_message:dispatch`,
which is a dispatch through a whole `Node` rather than a parse -- it
owns a node across calls by design (`fuzz_corpus_test`'s own docstring),
so five hundred generated inputs would drive a node rather than a
parser, and section 7's own carve-out is that a subject which is not a
parser does not owe this layer. The split is read off `_resolve`'s
existing behaviour rather than restated as a name, so nothing here has
to be edited when a harness is added. Should a `fuzz.`-prefixed spec
ever name a parser, or a package-resolved one stop being one, that
coincidence is what would need revisiting -- said here because the
reason the line falls where it does is not visible from the line
itself.

[std]: https://github.com/btclib-org/.github/blob/main/README.md
"""

from __future__ import annotations

import contextlib

import pytest
from btclib.exceptions import BTClibException
from hypothesis import given
from hypothesis import strategies as st

from tests.fuzz_corpus_test import _entry_points, _harnesses, _resolve


def _parser_specs() -> tuple[str, ...]:
    """Return every declared entry point that resolves through the package.

    The `fuzz.`-prefixed ones are excluded, for the reason the module
    docstring above argues: `_resolve` reaches those by path out of
    `fuzz/`, and what they name is a dispatch rather than a parser.
    """
    return tuple(
        spec
        for path in _harnesses()
        for spec in _entry_points(path)
        if not spec.startswith("fuzz.")
    )


def test_the_walk_finds_a_parser_to_draw_for() -> None:
    """An empty walk would make every property below vacuously true.

    `pytest.mark.parametrize` over an empty sequence collects nothing
    and reports no failure, so the layer would report green having
    stated nothing at all -- the shape CLAUDE.md's own union bullet
    calls a check that can only report the absence of damage. This is
    the guard against it.
    """
    assert _parser_specs()


@pytest.mark.parametrize("spec", _parser_specs())
@given(data=st.binary())
def test_a_parser_refuses_only_within_its_own_family(spec: str, data: bytes) -> None:
    """Unconstrained octets leave a parser returning, or refusing in family.

    The statement each harness in `fuzz/` makes with its own
    `contextlib.suppress`, over a described domain. What escapes the
    suppression is either a crash or a refusal outside the family the
    caller tolerates, and each of those is a defect in the parser rather
    than in this test -- `data` is unconstrained octets handed straight
    to it, with nothing decoding or validating in between.
    """
    with contextlib.suppress(BTClibException):
        _resolve(spec)(data)
