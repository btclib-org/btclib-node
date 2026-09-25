# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A test that leaves a task pending on an event loop fails.

`tests/conftest.py`'s `_record_pending_at_close` notes a task still
referenced as its loop closes, `_record_destroyed_pending` one the
collector frees while pending, and its `pytest_runtest_teardown` fails
the test through `fail_on_pending_tasks` (btclib-org/btclib-node#1107).
"""

import asyncio
import gc
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import (
    _CURRENT_TEST_NODEID,
    _PENDING_TASKS,
    fail_on_pending_tasks,
)

_ROOT = Path(__file__).parents[2]


async def _wait_on(future: asyncio.Future[None]) -> None:
    await future


def test_a_task_alive_at_close_is_recorded_once_and_fails_the_drain() -> None:
    """The close records the task once, and draining the record fails."""
    loop = asyncio.new_event_loop()
    task = loop.create_task(_wait_on(loop.create_future()))
    # one pass of the loop, so the task is suspended on its future rather
    # than never started, which would be an unawaited coroutine instead
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    loop.close()

    assert not task.done()
    (entry,) = _PENDING_TASKS
    assert entry.startswith(f"{_CURRENT_TEST_NODEID[0]}: <Task pending ")
    assert "_wait_on()" in entry
    assert entry.endswith(", pending when its loop closed")
    # already reported, so its destruction is not reported a second time
    assert not task._log_destroy_pending  # type: ignore[attr-defined]
    with pytest.raises(pytest.fail.Exception, match="left pending") as failure:
        fail_on_pending_tasks()
    assert entry in str(failure.value)
    assert not _PENDING_TASKS


def test_a_task_the_collector_frees_before_close_fails_the_drain() -> None:
    """A task nothing but its own cycle holds is recorded as it is freed."""
    loop = asyncio.new_event_loop()
    # the default handler would log what is recorded here too
    loop.set_exception_handler(lambda _loop, _context: None)
    task = loop.create_task(_wait_on(loop.create_future()))
    loop.run_until_complete(asyncio.sleep(0))
    # the cycle is then all that reaches it
    del task
    gc.collect()
    # freed before the close, so the close has nothing left to find
    assert not asyncio.all_tasks(loop)
    loop.close()

    (entry,) = _PENDING_TASKS
    assert entry.startswith(f"{_CURRENT_TEST_NODEID[0]}: <Task pending ")
    assert "_wait_on()" in entry
    assert entry.endswith(", freed by the collector while pending")
    with pytest.raises(pytest.fail.Exception, match="left pending") as failure:
        fail_on_pending_tasks()
    assert entry in str(failure.value)
    assert not _PENDING_TASKS


def test_a_loop_closed_with_nothing_pending_records_nothing() -> None:
    """A loop whose tasks all finished closes without a record."""
    loop = asyncio.new_event_loop()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    gc.collect()

    assert not _PENDING_TASKS
    fail_on_pending_tasks()


def test_another_exception_handed_to_the_handler_records_nothing() -> None:
    """Only a task freed while pending is recorded; the handler still runs."""
    handled: list[dict[str, object]] = []
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda _loop, context: handled.append(context))
    loop.call_exception_handler({"message": "something else"})
    loop.close()

    assert handled == [{"message": "something else"}]
    assert not _PENDING_TASKS


# The `conftest.py` each run below is given: the hookimpls re-exported
# from `tests.conftest`, whose import is also what patches
# `BaseEventLoop`, rather than a stand-in for either, and `LateClose`,
# which leaves a task pending after the last test, where only
# `pytest_sessionfinish` can report it.
_CONFTEST = (
    f"import sys\nsys.path.insert(0, {str(_ROOT)!r})\n"
    "import asyncio\n"
    "import pytest\n"
    "from tests.conftest import (  # noqa: F401\n"
    "    pytest_runtest_protocol,\n"
    "    pytest_runtest_teardown,\n"
    "    pytest_sessionfinish,\n"
    "    pytest_testnodedown,\n"
    ")\n"
    "\n\n"
    "async def wait_on(future):\n"
    "    await future\n"
    "\n\n"
    "class LateClose:\n"
    "    @pytest.hookimpl(tryfirst=True)\n"
    "    def pytest_sessionfinish(self):\n"
    "        loop = asyncio.new_event_loop()\n"
    "        self.task = loop.create_task(wait_on(loop.create_future()))\n"
    "        loop.run_until_complete(asyncio.sleep(0))\n"
    "        loop.close()\n"
    "\n\n"
    "def pytest_configure(config):\n"
    "    config.pluginmanager.register(LateClose())\n"
)

_DRAINS = (
    "def test_drains():\n"
    "    loop = asyncio.new_event_loop()\n"
    "    loop.run_until_complete(asyncio.sleep(0))\n"
    "    loop.close()\n"
)

_LEAKS = (
    "def test_leaks_alive():\n"
    "    loop = asyncio.new_event_loop()\n"
    "    task = loop.create_task(wait_on(loop.create_future()))\n"
    "    loop.run_until_complete(asyncio.sleep(0))\n"
    "    loop.close()\n"
    "    assert not task.done()\n"
    "\n\n"
    "def test_leaks_cycle():\n"
    "    loop = asyncio.new_event_loop()\n"
    "    loop.create_task(wait_on(loop.create_future()))\n"
    "    loop.run_until_complete(asyncio.sleep(0))\n"
    "    gc.collect()\n"
    "    assert not asyncio.all_tasks(loop)\n"
    "    loop.close()\n"
    "\n\n"
    # left to the collection the teardown runs
    "def test_leaks_unclosed():\n"
    "    loop = asyncio.new_event_loop()\n"
    "    loop.create_task(wait_on(loop.create_future()))\n"
    "    loop.run_until_complete(asyncio.sleep(0))\n"
    "\n\n"
)


def _run(tmp_path: Path, workers: str, tests: str) -> tuple[int, str]:
    """Run `tests` under `_CONFTEST`, returning the exit code and the output.

    A subprocess because what is under test is the hooks' place in
    pytest's own teardown and session end, which a call into this session
    cannot exercise without failing it.
    """
    (tmp_path / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    (tmp_path / "test_loops.py").write_text(
        "import asyncio\nimport gc\n\nfrom conftest import wait_on\n\n\n" + tests,
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            workers,
            "-rE",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    return completed.returncode, completed.stdout + completed.stderr


# once in process, and once through an xdist worker, which hands what it
# finds after its last test to the controller
_WORKERS = pytest.mark.parametrize("workers", ["-n0", "-n1"])


@_WORKERS
def test_a_run_fails_each_test_that_left_a_task_pending(
    tmp_path: Path, workers: str
) -> None:
    """Each leaking test errors at its own teardown; the clean one passes."""
    returncode, output = _run(tmp_path, workers, _LEAKS + _DRAINS)

    assert returncode == 1, output
    assert "4 passed, 3 errors" in output, output
    for leak in ("alive", "cycle", "unclosed"):
        assert f"ERROR test_loops.py::test_leaks_{leak}" in output, output
        assert f"test_loops.py::test_leaks_{leak}: <Task pending " in output


@_WORKERS
def test_a_task_left_after_the_last_test_fails_the_run(
    tmp_path: Path, workers: str
) -> None:
    """With every test clean, what `LateClose` leaves still fails the run."""
    returncode, output = _run(tmp_path, workers, _DRAINS)

    assert returncode == 1, output
    assert "1 passed in" in output, output
    assert "tasks left pending on an event loop after the last test" in output
    assert "<after the last test>: <Task pending " in output, output
