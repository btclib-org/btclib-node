# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Re-check tests/_data/README.md's vendored pin against upstream.

That file documents its own procedure under "Re-checking a pin": a local
`git hash-object` against the recorded `blob`, and a `commits?path=`
query against the recorded `commit`, to answer whether the pin is still
byte for byte and still at upstream's tip. This runs both, over every
heading the README carries a full repo/path/commit/blob quadruple for --
one today, `tests/unit/chainstate/_data/blockfilters.json`, its sibling
entry being out of scope by the README's own account: derived rather
than vendored, with no upstream blob to compare against.

Unlike btclib's own `check_vendored_vectors.py`, this opens no tracking
issue on drift. Every other scheduled workflow in this tree that reports
on something outside its own commits -- links.yml, bootstrap-dns.yml --
fails the run and leaves it there for whoever reads the Actions tab,
carrying no `issues: write` to do otherwise; one pin is not the case for
this tree's first exception to that.

    python3 .github/scripts/check_vendored_pin.py tests/_data/README.md
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# resolved once: a bare "git" or "gh" in a subprocess list is a partial
# executable path relying on PATH's own search order rather than naming
# what actually runs
_GIT = shutil.which("git") or "git"
_GH = shutil.which("gh") or "gh"

# a vendored entry's own `## ` heading, the local, repo-relative path to
# the file the fenced block below it pins
_HEADING = re.compile(r"^## `(.+)`$", re.MULTILINE)
# the fenced block's key/value lines. "pulled" and the free-text
# "behind" line are not read here: a human updates both once a drift
# this script reports is actually resolved, which is the decision
# neither this script nor the workflow it runs in gets to make
_FIELD = re.compile(r"^(repo|path|commit|blob)\s+(\S+)", re.MULTILINE)

# argv[0] plus the README path -- not a choice of anybody's
_ARGV = 2


@dataclass(frozen=True)
class Entry:
    """One pin this script can re-check: a local file, a live commit."""

    heading: str
    repo: str
    path: str
    commit: str
    blob: str


def _entries(readme: str) -> list[Entry]:
    """Every heading in readme carrying a full repo/path/commit/blob pin."""
    entries: list[Entry] = []
    heading = ""
    pos = 0
    for match in re.finditer(r"```text\n(.*?)\n```", readme, re.DOTALL):
        headings_before = _HEADING.findall(readme[pos : match.start()])
        if headings_before:
            heading = headings_before[-1]
        pos = match.end()
        fields = dict(_FIELD.findall(match.group(1)))
        repo, path, commit, blob = (
            fields.get("repo"),
            fields.get("path"),
            fields.get("commit"),
            fields.get("blob"),
        )
        if repo and path and commit and blob:
            entries.append(Entry(heading, repo, path, commit, blob))
    return entries


def _run(*args: str) -> str:
    """Run a read-only command and return its stripped stdout."""
    result = subprocess.run(  # noqa: S603
        args, capture_output=True, check=True, encoding="utf-8"
    )
    return result.stdout.strip()


def _local_blob(path: str) -> str:
    """Return the git blob SHA-1 of the file this tree carries at path."""
    return _run(_GIT, "hash-object", path)


def _default_branch(repo: str) -> str:
    """Return repo's default branch, upstream's own tip rather than a guess."""
    return _run(_GH, "api", f"repos/{repo}", "--jq", ".default_branch")


def _upstream_blob(repo: str, path: str, ref: str) -> str | None:
    """Return the blob SHA-1 of path in repo at ref, or None if it is gone."""
    directory, _, name = path.rpartition("/")
    sha = _run(
        _GH,
        "api",
        f"repos/{repo}/git/trees/{ref}:{directory}",
        "--jq",
        f'.tree[] | select(.path == "{name}") | .sha',
    )
    return sha or None


def _latest_commit(repo: str, path: str) -> str | None:
    """Return the sha of the most recent commit touching path, or None."""
    sha = _run(
        _GH,
        "api",
        "--method",
        "GET",
        f"repos/{repo}/commits",
        "-f",
        f"path={path}",
        "-f",
        "per_page=1",
        "--jq",
        ".[0].sha",
    )
    return sha or None


def check(entry: Entry) -> list[str]:
    """Every way entry's pin disagrees with this tree or with upstream."""
    problems = []
    local_blob = _local_blob(entry.heading)
    if local_blob != entry.blob:
        problems.append(
            f"{entry.heading}: the file in this tree hashes to"
            f" {local_blob}, tests/_data/README.md records {entry.blob}"
        )
    branch = _default_branch(entry.repo)
    upstream_blob = _upstream_blob(entry.repo, entry.path, branch)
    if upstream_blob is None:
        problems.append(
            f"{entry.heading}: {entry.repo} has no {entry.path} on"
            f" {branch} any more -- renamed, moved or deleted upstream"
        )
    elif upstream_blob != entry.blob:
        problems.append(
            f"{entry.heading}: pinned blob {entry.blob}, {entry.repo}'s"
            f" {branch} now carries {upstream_blob} at {entry.path}"
        )
    latest = _latest_commit(entry.repo, entry.path)
    if latest is not None and latest != entry.commit:
        problems.append(
            f"{entry.heading}: pinned to commit {entry.commit}, the"
            f" newest touching {entry.path} in {entry.repo} is now"
            f" {latest}"
        )
    return problems


def main() -> int:
    """Check every pin the README named on argv carries, and say so."""
    if len(sys.argv) != _ARGV:
        print(f"usage: {Path(sys.argv[0]).name} <README path>", file=sys.stderr)
        return 2
    readme_path = Path(sys.argv[1])
    entries = _entries(readme_path.read_text(encoding="utf-8"))
    problems = [p for entry in entries for p in check(entry)]
    for problem in problems:
        print(f"DRIFT: {problem}")
    if not problems:
        print("Every vendored pin is still byte for byte, still at upstream's tip.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
