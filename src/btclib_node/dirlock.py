# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`DirectoryLock`, the one process a data directory belongs to.

Core's own `util::LockDirectory` (`src/util/fs_helpers.cpp`) over its
`fsbridge::FileLock` (`src/util/fs.cpp`), both at
bitcoin/bitcoin@9be056a8a7: a `.lock` file in the directory, created
where it is missing, and an exclusive lock on it that fails at once
rather than waiting. `fcntl.lockf` is `fcntl(F_SETLK)` with `F_WRLCK`
over the whole file, the call Core makes, so a `bitcoind` and a
`btclib-node` over one directory refuse each other too; on Windows
`msvcrt.locking` takes the place of Core's `LockFileEx`.

Core keeps what it holds in a table keyed by the lock file's path, and
answers a request for a path already there with success, so the lock
excludes other processes and never the one holding it. So does this
table. Core's entries last as long as the process; a process here can
build and stop one `Node` after another, so each entry counts its
holders instead and comes off with the last `release`.
"""

import os
import sys
import threading
from typing import TYPE_CHECKING

from btclib_node.constants import CLIENT_NAME
from btclib_node.exceptions import DirectoryLockError

if TYPE_CHECKING:
    from pathlib import Path

if sys.platform == "win32":  # pragma: no cover -- the ubuntu cells hold the floor

    import msvcrt

    def _try_lock(fd: int) -> None:
        # one byte from offset 0, which every `LockFileEx` range Core
        # takes over the same file overlaps
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


__all__ = ["LOCK_FILE", "DirectoryLock"]

# Core's own `lockfile_name`, `LockDirectory`'s second argument
# (`src/init.cpp`, same sha)
LOCK_FILE = ".lock"

# Core's own `dir_locks`: each lock file this process holds, by path, as
# its descriptor and how many `DirectoryLock`s hold it. A POSIX lock is
# the process's own, so a second descriptor locking the same file would
# succeed, and closing either would release both: one descriptor per
# path is what keeps the lock on while any holder remains. Reentrant,
# because `DirectoryLock.__del__` takes it too, and a collection can run
# that on a thread already inside it.
_held: dict[str, list[int]] = {}
_held_guard = threading.RLock()


class DirectoryLock:
    """An exclusive lock on `directory`, held until `release`.

    Raises `DirectoryLockError` with Core's own message: "Cannot write
    to directory ..." where the lock file cannot be opened, "Cannot
    obtain a lock on directory ..." where another process holds it
    (`LockDirectory` in `src/init.cpp`, same sha).
    """

    def __init__(self, directory: Path) -> None:
        """Lock `directory`, which has to exist already."""
        self._holding = False
        self.directory = directory
        self._key = str(directory / LOCK_FILE)
        with _held_guard:
            if self._key in _held:
                _held[self._key][1] += 1
                self._holding = True
                return
            try:
                # Core's `fopen(..., "a")` and then `open(O_RDWR)`, as one
                fd = os.open(self._key, os.O_RDWR | os.O_CREAT, 0o666)
            except OSError as error:
                err_msg = f"Cannot write to directory '{directory}'; "
                err_msg += "check permissions."
                raise DirectoryLockError(err_msg) from error
            try:
                _try_lock(fd)
            except OSError as error:
                os.close(fd)
                err_msg = f"Cannot obtain a lock on directory {directory}. "
                err_msg += f"{CLIENT_NAME} is probably already running."
                raise DirectoryLockError(err_msg) from error
            _held[self._key] = [fd, 1]
            self._holding = True

    def release(self) -> None:
        """Stop holding the lock; the last holder's call unlocks the file.

        A second call does nothing.
        """
        with _held_guard:
            if not self._holding:
                return
            self._holding = False
            entry = _held[self._key]
            entry[1] -= 1
            if not entry[1]:
                del _held[self._key]
                os.close(entry[0])

    def __del__(self) -> None:
        """Release the lock if nothing did, as `Node.__del__` does its pool."""
        self.release()
