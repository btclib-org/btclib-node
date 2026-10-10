# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Named-only arguments, as `transformNamedArguments` folds them.

Core makes the inner arguments of an `OBJ_NAMED_PARAMS` one options object
at the position of the object itself (`src/rpc/server.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag). The expected values are what
`bitcoind` v31.1.0 answers for `dumptxoutset`, whose `rollback` is one
(`tests/integration/dumptxoutset_test.py` sends the same requests).
"""

from typing import Any

import pytest

from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.jsonrpc import transform_named_arguments

NAMES = ("path", "type", "options|rollback")
NAMED_ONLY = ("rollback",)


def transform(params: dict[str, Any]) -> list[Any]:
    """Return `params` mapped onto `dumptxoutset`'s positions."""
    return transform_named_arguments(params, NAMES, NAMED_ONLY)


@pytest.mark.parametrize(
    ("params", "positions"),
    [
        ({"path": "a"}, ["a"]),
        ({"path": "a", "rollback": 3}, ["a", None, {"rollback": 3}]),
        ({"path": "a", "type": "x", "rollback": 3}, ["a", "x", {"rollback": 3}]),
        ({"path": "a", "options": {"rollback": 3}}, ["a", None, {"rollback": 3}]),
        ({"rollback": 3}, [None, None, {"rollback": 3}]),
        ({"args": ["a", "x"], "rollback": 3}, ["a", "x", {"rollback": 3}]),
    ],
)
def test_the_named_only_keys_are_the_options_object(
    params: dict[str, Any], positions: list[Any]
) -> None:
    """A named-only key is a key of the object at its position."""
    assert transform(params) == positions


def test_the_object_and_its_keys_conflict() -> None:
    """Core names the object and the first key."""
    with pytest.raises(RpcError) as caught:
        transform({"path": "a", "options": {}, "rollback": 3})
    assert caught.value.message == "Parameter options conflicts with parameter rollback"


def test_a_positional_array_and_a_named_only_key_meet_at_the_object() -> None:
    """The named-only keys do not count as a position given twice."""
    with pytest.raises(RpcError) as caught:
        transform({"args": ["a", "x", {}], "rollback": 3})
    assert caught.value.message == (
        "Parameter options specified twice both as positional and named argument"
    )
