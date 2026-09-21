# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Whose test a leaked thread's exception says it belongs to.

`_pytest.threadexception` drains `threading.excepthook` at the boundary
of a test's own setup, call and teardown, so an exception raised on a
thread nobody joined is attributed to whatever test is at one of those
boundaries when the drain reaches it -- never to the test that started
the thread (btclib-org/btclib-node#1002). Nothing here changes that
attribution; once pytest has reported a test finished, nothing can.
What `tests/conftest.py`'s `_record_thread_origin`,
`thread_exception_origin_note` and `install_thread_exception_origin_hook`
add is a note, on the exception itself, naming the test that actually
started the thread -- landing in the same report pytest already prints,
through `add_note` (PEP 678).
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

from tests.conftest import (
    _CURRENT_TEST_NODEID,
    _THREAD_ORIGIN,
    install_thread_exception_origin_hook,
    thread_exception_origin_note,
)

_ROOT = Path(__file__).parents[2]


def test_a_thread_is_recorded_against_the_test_running_when_start_was_called() -> None:
    """`Thread.start` (patched) records the nodeid current at the call."""
    previous = _CURRENT_TEST_NODEID[0]
    thread = threading.Thread(target=lambda: None)
    try:
        _CURRENT_TEST_NODEID[0] = "tests/example_test.py::test_marker"
        thread.start()
        thread.join(timeout=5)
        assert _THREAD_ORIGIN[thread] == "tests/example_test.py::test_marker"
    finally:
        _CURRENT_TEST_NODEID[0] = previous


def test_no_note_where_the_thread_itself_is_unknown() -> None:
    """`threading.excepthook`'s own documented `args.thread is None` case."""
    args = threading.ExceptHookArgs((RuntimeError, RuntimeError("x"), None, None))
    assert thread_exception_origin_note(args) is None


def test_no_note_where_the_thread_was_never_recorded() -> None:
    """A thread `_record_thread_origin` never saw `start` on has no origin."""
    thread = threading.Thread(target=lambda: None)
    args = threading.ExceptHookArgs((RuntimeError, RuntimeError("x"), None, thread))
    assert thread_exception_origin_note(args) is None


def test_no_note_where_the_origin_is_the_test_now_running() -> None:
    """A thread the running test itself started needs no correction."""
    previous = _CURRENT_TEST_NODEID[0]
    thread = threading.Thread(target=lambda: None)
    try:
        _CURRENT_TEST_NODEID[0] = "tests/example_test.py::test_here"
        _THREAD_ORIGIN[thread] = "tests/example_test.py::test_here"
        args = threading.ExceptHookArgs((RuntimeError, RuntimeError("x"), None, thread))
        assert thread_exception_origin_note(args) is None
    finally:
        _CURRENT_TEST_NODEID[0] = previous
        del _THREAD_ORIGIN[thread]


def test_a_note_names_the_origin_where_it_differs_from_the_running_test() -> None:
    """A thread's exception, attributed elsewhere, says where it started."""
    previous = _CURRENT_TEST_NODEID[0]
    thread = threading.Thread(target=lambda: None)
    try:
        _THREAD_ORIGIN[thread] = "tests/example_test.py::test_the_leak"
        _CURRENT_TEST_NODEID[0] = "tests/example_test.py::test_the_bystander"
        args = threading.ExceptHookArgs((RuntimeError, RuntimeError("x"), None, thread))
        note = thread_exception_origin_note(args)
        assert note is not None
        assert "tests/example_test.py::test_the_leak" in note
    finally:
        _CURRENT_TEST_NODEID[0] = previous
        del _THREAD_ORIGIN[thread]


def test_the_installed_hook_notes_a_mismatched_origin_and_still_chains() -> None:
    """The hook `add_note`s the mismatch and calls the hook it wrapped.

    `threading.excepthook` is swapped for a stand-in first, so the chain
    `install_thread_exception_origin_hook` builds has something of this
    test's own to call rather than whatever the real session installed
    -- and what proves the chain held is the stand-in seeing the same
    `args` this test built.
    """
    previous_hook = threading.excepthook
    previous_nodeid = _CURRENT_TEST_NODEID[0]
    calls: list[threading.ExceptHookArgs] = []
    thread = threading.Thread(target=lambda: None)
    try:
        threading.excepthook = calls.append
        install_thread_exception_origin_hook()
        _THREAD_ORIGIN[thread] = "tests/example_test.py::test_the_leak"
        _CURRENT_TEST_NODEID[0] = "tests/example_test.py::test_the_bystander"
        exc = RuntimeError("boom")
        args = threading.ExceptHookArgs((RuntimeError, exc, None, thread))

        threading.excepthook(args)

        (received,) = calls
        assert received is args
        assert exc.__notes__
        assert "tests/example_test.py::test_the_leak" in exc.__notes__[0]
    finally:
        threading.excepthook = previous_hook
        _CURRENT_TEST_NODEID[0] = previous_nodeid
        del _THREAD_ORIGIN[thread]


def test_a_module_scoped_fixtures_thread_is_attributed_to_the_test_that_built_it(
    tmp_path: Path,
) -> None:
    """A regression guard for the scoping gap a review round of #1002 found.

    An autouse *fixture* cannot record this correctly: a module, class
    or session-scoped fixture a test depends on is always built ahead
    of a function-scoped one, autouse or not, so a thread such a
    fixture starts on the first test of its own scope starts before a
    function-scoped autouse fixture belonging to that same test has
    run at all -- reproduced directly, against a first draft of this
    mechanism built exactly that way, before this test existed. What
    replaced the fixture, `pytest_runtest_protocol` in
    `tests/conftest.py`, is a hookwrapper instead, and a hookwrapper's
    own code ahead of its `yield` runs before every non-wrapper
    implementation of the same hook -- which is where `_pytest.runner`
    builds a fixture of any scope -- so this is not testable
    in-process: what is under test is pytest's own ordering across a
    real collection of more than one test module, the same reason
    `test_a_run_started_from_tests_says_it_is_ungated` above needs a
    real subprocess and not a call into this session's own.

    A `conftest.py` re-exporting `pytest_runtest_protocol` from
    `tests.conftest` is the actual hookimpl under test, not a rewritten
    stand-in that could differ from it in exactly the property this
    guards.
    """
    (tmp_path / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(_ROOT)!r})\n"
        "from tests.conftest import pytest_runtest_protocol  # noqa: F401\n",
        encoding="utf-8",
    )
    (tmp_path / "test_a_module.py").write_text(
        "def test_a1() -> None:\n    pass\n\n\ndef test_a2() -> None:\n    pass\n",
        encoding="utf-8",
    )
    origin_path = tmp_path / "origin.txt"
    (tmp_path / "test_b_module.py").write_text(
        "import threading\n"
        "import pytest\n"
        "from tests.conftest import _THREAD_ORIGIN\n"
        "\n\n"
        "@pytest.fixture(scope='module')\n"
        "def mod_thread() -> threading.Thread:\n"
        "    t = threading.Thread(target=lambda: None)\n"
        "    t.start()\n"
        "    t.join()\n"
        "    return t\n"
        "\n\n"
        "def test_b1(mod_thread: threading.Thread) -> None:\n"
        f"    with open({str(origin_path)!r}, 'w') as f:\n"
        "        f.write(repr(_THREAD_ORIGIN.get(mod_thread)))\n",
        encoding="utf-8",
    )

    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-n0",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert origin_path.read_text() == "'test_b_module.py::test_b1'"


def test_the_installed_hook_adds_no_note_where_none_is_owed_and_still_chains() -> None:
    """No mismatch, no `add_note` call -- and the wrapped hook still runs."""
    previous_hook = threading.excepthook
    previous_nodeid = _CURRENT_TEST_NODEID[0]
    calls: list[threading.ExceptHookArgs] = []
    thread = threading.Thread(target=lambda: None)
    try:
        threading.excepthook = calls.append
        install_thread_exception_origin_hook()
        _THREAD_ORIGIN[thread] = "tests/example_test.py::test_here"
        _CURRENT_TEST_NODEID[0] = "tests/example_test.py::test_here"
        exc = RuntimeError("fine")
        args = threading.ExceptHookArgs((RuntimeError, exc, None, thread))

        threading.excepthook(args)

        (received,) = calls
        assert received is args
        assert not getattr(exc, "__notes__", None)
    finally:
        threading.excepthook = previous_hook
        _CURRENT_TEST_NODEID[0] = previous_nodeid
        del _THREAD_ORIGIN[thread]
