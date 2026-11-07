# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `.github/scripts/check_vendored_pin.py`.

The script asks `git` and `gh`, and `gh` reaches the network. Every
test that runs a function able to reach either replaces `subprocess.run`
with a stub that answers from a list or a table and records each call;
the others call `_entries`, or `main` on its usage, which reach neither.
The `script` fixture patches `time.sleep`, so no test sleeps.

The script is loaded by path, `.github/scripts` being no package.
"""

from __future__ import annotations

import importlib.util
import runpy
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


# a run of one digit, which detect-secrets does not read as a key
_COMMIT = "1" * 40
_BLOB = "2" * 40
_OTHER = "3" * 40

# one block of each shape `_entries` tells apart, in the layout of
# tests/_data/README.md and scripts/seeds/README.md: a `## ` heading
# naming the file, a fenced `text` block under it. Two headings stand
# before the first block, and the block is the nearer one's. The second
# block carries no `blob`, so it is no pin. The third has no heading of
# its own, so it is reported under the last one seen
_README = f"""\
# Vendored

## `unpinned.txt`

Not vendored yet, so no block.

## `a.txt`

```text
repo    upstream/one
path    data/a.txt
commit  {_COMMIT}  2026-01-02
blob    {_BLOB}
pulled  2026-01-03
behind  0 revisions; that commit is the tip of the path
```

## `derived.txt`

```text
repo    upstream/one
path    data/derived.txt
commit  {_COMMIT}
```

```text
repo    upstream/two
path    data/b.txt
ref     v1.0
commit  {_COMMIT}
blob    {_BLOB}
```
"""
# `a.txt` alone
_ONE_PIN = _README.split("## `derived.txt`", maxsplit=1)[0]


def _answer(
    monkeypatch: pytest.MonkeyPatch,
    script: ModuleType,
    table: dict[tuple[str, ...], str],
) -> list[tuple[str, ...]]:
    """Make each call answer its stdout from table, exit status 0."""
    calls: list[tuple[str, ...]] = []

    def run(
        args: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=table[args], stderr="")

    monkeypatch.setattr(script.subprocess, "run", run)
    return calls


def _upstream(
    script: ModuleType,
    *,
    local: str = _BLOB,
    upstream: str = _BLOB,
    latest: str = _COMMIT,
    ref: str = "master",
) -> dict[tuple[str, ...], str]:
    """Answer what `check` asks of `a.txt` in `_README`, agreeing by default."""
    return {
        (script._GIT, "hash-object", "a.txt"): local,
        (script._GH, "api", "repos/upstream/one", "--jq", ".default_branch"): (
            "master"
        ),
        (
            script._GH,
            "api",
            f"repos/upstream/one/git/trees/{ref}:data",
            "--jq",
            '.tree[] | select(.path == "a.txt") | .sha',
        ): upstream,
        (
            script._GH,
            "api",
            "--method",
            "GET",
            "repos/upstream/one/commits",
            "-f",
            "path=data/a.txt",
            "-f",
            "per_page=1",
            "--jq",
            ".[0].sha",
        ): latest,
    }


def _entry(script: ModuleType, ref: str | None = None) -> object:
    """`a.txt` of `_README`, pinned to `ref` where one is given."""
    return script.Entry("a.txt", "upstream/one", "data/a.txt", _COMMIT, _BLOB, ref)


def test_entries_reads_each_full_pin_under_its_heading(script: ModuleType) -> None:
    """A block without `blob` is no pin; a block takes the last heading seen."""
    assert script._entries(_README) == [
        _entry(script),
        script.Entry(
            "derived.txt", "upstream/two", "data/b.txt", _COMMIT, _BLOB, "v1.0"
        ),
    ]


def test_a_pin_that_agrees_everywhere_has_no_problem(
    monkeypatch: pytest.MonkeyPatch, script: ModuleType
) -> None:
    """The local file, the upstream blob and the newest commit all agree."""
    _answer(monkeypatch, script, _upstream(script))
    assert script.check(_entry(script), "README.md") == []


@pytest.mark.parametrize(
    ("answers", "problem"),
    [
        pytest.param({"local": _OTHER}, "the file in this tree hashes to", id="local"),
        pytest.param({"upstream": ""}, "has no data/a.txt at master", id="gone"),
        pytest.param({"upstream": _OTHER}, f"pinned blob {_BLOB}", id="blob"),
        pytest.param({"latest": _OTHER}, f"pinned to commit {_COMMIT}", id="commit"),
    ],
)
def test_each_disagreement_is_named(
    monkeypatch: pytest.MonkeyPatch,
    script: ModuleType,
    answers: dict[str, str],
    problem: str,
) -> None:
    """Each question the script asks fails on its own."""
    _answer(monkeypatch, script, _upstream(script, **answers))
    (found,) = script.check(_entry(script), "README.md")
    assert found.startswith("a.txt: ")
    assert problem in found


def test_a_path_with_no_commit_upstream_is_not_called_behind(
    monkeypatch: pytest.MonkeyPatch, script: ModuleType
) -> None:
    """An empty answer to the newest-commit query is no drift of its own."""
    _answer(monkeypatch, script, _upstream(script, latest=""))
    assert script.check(_entry(script), "README.md") == []


def test_a_ref_pin_is_read_at_its_tag_and_asks_no_newest_commit(
    monkeypatch: pytest.MonkeyPatch, script: ModuleType
) -> None:
    """A tag stands in for the default branch; the commit query is skipped."""
    calls = _answer(monkeypatch, script, _upstream(script, ref="v1.0"))
    assert script.check(_entry(script, "v1.0"), "README.md") == []
    assert [call[2] for call in calls] == [
        "a.txt",
        "repos/upstream/one/git/trees/v1.0:data",
    ]


def test_main_without_a_readme_prints_its_usage(
    monkeypatch: pytest.MonkeyPatch,
    script: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No README named, or two, is the usage, exit status 2."""
    usage = "usage: check_vendored_pin.py <README path>\n"
    monkeypatch.setattr(sys, "argv", ["check_vendored_pin.py"])
    assert script.main() == 2
    assert capsys.readouterr().err == usage
    monkeypatch.setattr(sys, "argv", ["check_vendored_pin.py", "a.md", "b.md"])
    assert script.main() == 2
    assert capsys.readouterr().err == usage


def _main(
    monkeypatch: pytest.MonkeyPatch,
    script: ModuleType,
    tmp_path: Path,
    readme: str,
    table: dict[tuple[str, ...], str],
) -> int:
    """Run `main` over readme, written under tmp_path, answering from table."""
    path = tmp_path / "README.md"
    path.write_text(readme, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_vendored_pin.py", str(path)])
    _answer(monkeypatch, script, table)
    return int(script.main())


def test_main_prints_each_drift_and_fails(
    monkeypatch: pytest.MonkeyPatch,
    script: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A drift is printed with its marker, and the exit status is 1."""
    readme = _ONE_PIN
    table = _upstream(script, latest=_OTHER)
    assert _main(monkeypatch, script, tmp_path, readme, table) == 1
    assert capsys.readouterr().out.startswith("DRIFT: a.txt: pinned to commit")


def test_main_on_branch_pins_says_they_are_at_the_tip(
    monkeypatch: pytest.MonkeyPatch,
    script: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean run over branch-tracked pins says only what it checked."""
    table = _upstream(script)
    assert _main(monkeypatch, script, tmp_path, _ONE_PIN, table) == 0
    assert capsys.readouterr().out == (
        "Every branch-tracked pin is still byte for byte, still at upstream's tip.\n"
    )


def test_main_on_ref_pins_says_they_are_at_their_tag(
    monkeypatch: pytest.MonkeyPatch,
    script: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean run over ref-pinned pins says only what it checked."""
    readme = _ONE_PIN.replace("commit  ", "ref     v1.0\ncommit  ")
    table = _upstream(script, ref="v1.0")
    assert _main(monkeypatch, script, tmp_path, readme, table) == 0
    assert capsys.readouterr().out == (
        "Every ref-pinned pin is still byte for byte, still at its own tag.\n"
    )


def test_the_main_guard_exits_with_what_main_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run as `__main__`, the script exits with `main`'s status.

    `runpy.run_path` runs the file in this interpreter, where coverage
    measures it, and the usage path reaches neither `git` nor `gh`.
    """
    monkeypatch.setattr(sys, "argv", ["check_vendored_pin.py"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
    assert exc.value.code == 2
