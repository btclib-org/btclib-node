# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`DirectoryLock`: Core's messages, and who it refuses.

`bitcoind` here is the release `integration-bitcoind.yml` pins.
"""

import gc
from typing import TYPE_CHECKING

import pytest

from btclib_node.dirlock import LOCK_FILE, DirectoryLock
from btclib_node.exceptions import DirectoryLockError
from tests import held_by_another_process, lock_from_another_process
from tests.secret_free import assert_secret_free
from tests.unit.rpc import LEAK_WIF

if TYPE_CHECKING:
    from pathlib import Path


def test_another_process_holding_the_directory_is_refused_with_cores_message(
    tmp_path: Path,
) -> None:
    """`bitcoind`'s words for a second instance, named for this node."""
    with held_by_another_process(tmp_path):
        with pytest.raises(DirectoryLockError) as excinfo:
            DirectoryLock(tmp_path, default_path=tmp_path)
        assert str(excinfo.value) == (
            f"Cannot obtain a lock on directory {tmp_path}."
            " btclib-node is probably already running."
        )
    lock = DirectoryLock(tmp_path)
    lock.release()


def test_the_lock_is_a_file_in_the_directory(tmp_path: Path) -> None:
    """Core's own `.lock`, left in place once released, as Core leaves it."""
    lock = DirectoryLock(tmp_path)
    lock.release()
    assert (tmp_path / LOCK_FILE).is_file()


def test_a_directory_the_lock_file_cannot_be_opened_in_is_refused_with_cores_message(
    tmp_path: Path,
) -> None:
    """Core's `ErrorWrite`, which a missing directory reaches as well."""
    missing = tmp_path / "missing"
    with pytest.raises(DirectoryLockError) as excinfo:
        DirectoryLock(missing, default_path=missing)
    assert str(excinfo.value) == (
        f"Cannot write to directory '{missing}'; check permissions."
    )


def test_this_process_is_not_refused_and_holds_until_its_last_release(
    tmp_path: Path,
) -> None:
    """Core's own table: a held path answers success, and stays locked."""
    first = DirectoryLock(tmp_path)
    second = DirectoryLock(tmp_path)
    first.release()
    # a second release of the same holder is not the other one's
    first.release()
    assert lock_from_another_process(tmp_path) != "locked"
    second.release()
    assert lock_from_another_process(tmp_path) == "locked"


def test_a_lock_nothing_holds_any_more_is_released(tmp_path: Path) -> None:
    """The collector releases a lock whose `release` nobody called."""
    lock = DirectoryLock(tmp_path)
    assert lock_from_another_process(tmp_path) != "locked"
    del lock
    gc.collect()
    assert lock_from_another_process(tmp_path) == "locked"


def test_a_directory_an_option_gave_is_named_by_its_length(tmp_path: Path) -> None:
    """ISS 2533: a key given as the directory is in neither refusal."""
    held = tmp_path / LEAK_WIF
    held.mkdir()
    with held_by_another_process(held):
        with pytest.raises(DirectoryLockError) as excinfo:
            DirectoryLock(held, default_path=tmp_path)
        assert str(excinfo.value) == (
            f"Cannot obtain a lock on directory <{len(str(held))} characters>."
            " btclib-node is probably already running."
        )
        assert_secret_free(excinfo.value, [LEAK_WIF])
    missing = held / "missing"
    with pytest.raises(DirectoryLockError) as excinfo:
        DirectoryLock(missing)
    assert str(excinfo.value) == (
        f"Cannot write to directory <{len(str(missing))} characters>; "
        "check permissions."
    )
    assert_secret_free(excinfo.value, [LEAK_WIF])
