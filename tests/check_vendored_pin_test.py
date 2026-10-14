# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for the retry of `_run` in `.github/scripts/check_vendored_pin.py`.

`subprocess.run` is faked and `time.sleep` is patched, so no test reaches
the network or sleeps.

The script is loaded by path, `.github/scripts` being no package.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "check_vendored_pin.py"


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Return the script, imported by path, with its pause made instant."""
    spec = importlib.util.spec_from_file_location("check_vendored_pin", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "check_vendored_pin", module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    return module


def _fake_run(
    monkeypatch: pytest.MonkeyPatch, script: ModuleType, outcomes: list[tuple[int, str]]
) -> list[tuple[str, ...]]:
    """Make each call answer the next (returncode, text) of outcomes."""
    calls: list[tuple[str, ...]] = []

    def run(
        args: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        code, text = outcomes[len(calls) - 1]
        return subprocess.CompletedProcess(
            args,
            code,
            stdout=text if code == 0 else "",
            stderr="" if code == 0 else text,
        )

    monkeypatch.setattr(script.subprocess, "run", run)
    return calls


def test_a_call_that_fails_once_is_tried_again(
    monkeypatch: pytest.MonkeyPatch, script: ModuleType
) -> None:
    """A failure followed by a success returns the success."""
    calls = _fake_run(monkeypatch, script, [(1, "HTTP 502"), (0, " main\n")])
    assert script._run("gh", "api", "repos/x/y") == "main"
    assert len(calls) == 2


def test_a_call_that_always_fails_is_tried_a_bounded_number_of_times(
    monkeypatch: pytest.MonkeyPatch, script: ModuleType
) -> None:
    """A call that never succeeds is run `_ATTEMPTS` times and then exits."""
    calls = _fake_run(monkeypatch, script, [(1, "HTTP 502")] * 10)
    with pytest.raises(SystemExit):
        script._run("gh", "api", "repos/x/y")
    assert len(calls) == script._ATTEMPTS


def test_the_final_failure_names_the_command_and_the_stderr(
    monkeypatch: pytest.MonkeyPatch, script: ModuleType
) -> None:
    """The exit message carries the command and `gh`'s stderr."""
    _fake_run(monkeypatch, script, [(1, "HTTP 502\n")] * 10)
    with pytest.raises(SystemExit) as exc:
        script._run("gh", "api", "repos/x/y")
    message = str(exc.value)
    assert "gh api repos/x/y" in message
    assert "HTTP 502" in message


def test_a_call_that_passes_is_run_once(
    monkeypatch: pytest.MonkeyPatch, script: ModuleType
) -> None:
    """A call that succeeds the first time is not run again."""
    calls = _fake_run(monkeypatch, script, [(0, "abc\n")])
    assert script._run("git", "hash-object", "f") == "abc"
    assert len(calls) == 1


def test_the_pause_is_taken_between_attempts_only(
    monkeypatch: pytest.MonkeyPatch, script: ModuleType
) -> None:
    """The pause follows each failed attempt but the last."""
    pauses: list[float] = []
    _fake_run(monkeypatch, script, [(1, "x")] * 10)
    monkeypatch.setattr(script.time, "sleep", pauses.append)
    with pytest.raises(SystemExit):
        script._run("gh")
    assert pauses == [script._PAUSE_SECONDS] * (script._ATTEMPTS - 1)


def test_a_recovered_attempt_leaves_its_stderr_in_the_log(
    monkeypatch: pytest.MonkeyPatch,
    script: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed attempt that a retry recovers prints its stderr."""
    _fake_run(monkeypatch, script, [(1, "HTTP 502\n"), (0, "main\n")])
    assert script._run("gh", "api", "repos/x/y") == "main"
    err = capsys.readouterr().err
    assert "gh api repos/x/y" in err
    assert "HTTP 502" in err
