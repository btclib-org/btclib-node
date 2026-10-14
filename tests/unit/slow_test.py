# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Which tests a run skips as slow, and when it skips none.

The decision is a function of the collected items and one flag, so this
asks it of stand-ins for the items; `tests/slow/` is where a real slow
test lives.
"""

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from tests.conftest import skip_slow

if TYPE_CHECKING:
    import pytest


def an_item(*, slow: bool) -> Any:
    """Build the part of a `pytest.Item` that `skip_slow` reads and writes."""
    added: list[pytest.MarkDecorator] = []
    return SimpleNamespace(
        get_closest_marker=lambda name: (name == "slow" and slow) or None,
        add_marker=added.append,
        added=added,
    )


def test_slow_items_are_skipped_by_default() -> None:
    """Only the item carrying `slow` is given a skip, and it says why."""
    slow, quick = an_item(slow=True), an_item(slow=False)
    skip_slow([slow, quick], run_slow=False)
    assert quick.added == []
    (mark,) = slow.added
    assert mark.name == "skip"
    assert "--run-slow" in mark.kwargs["reason"]


def test_run_slow_skips_nothing() -> None:
    """`--run-slow` leaves every item as collected."""
    slow = an_item(slow=True)
    skip_slow([slow], run_slow=True)
    assert slow.added == []
