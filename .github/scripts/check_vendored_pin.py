# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Re-check a vendored-pins README's own pins against upstream.

That file documents its own procedure under "Re-checking a pin": a local
`git hash-object` against the recorded `blob`, and a `commits?path=`
query against the recorded `commit`, to answer whether the pin is still
byte for byte and still at upstream's tip. This runs both, over every
heading the README carries a full repo/path/commit/blob quadruple for.
An entry the README calls derived rather than vendored carries no
upstream blob, and is out of scope.

An entry pinned to a release rather than to upstream's default branch
carries a fifth field, `ref`, naming the tag -- `scripts/seeds/README.md`
is that case (btclib-org/btclib-node#1227): its lists move ahead of a
release on upstream's own master, so comparing against the default
branch would flag every one of them as drift a human already knows
about and does not want re-reported. `ref` set reads `blob` against that
tag's own tree instead of the branch's, and skips the "newest commit"
check entirely -- a tag does not gain new commits, so the question that
check asks has no answer for one.

Unlike btclib's own `check_vendored_vectors.py`, this opens no tracking
issue on drift. Every other scheduled workflow in this tree that reports
on something outside its own commits -- links.yml, bootstrap-dns.yml --
fails the run and leaves it there for whoever reads the Actions tab,
carrying no `issues: write` to do otherwise; a handful of pins is not
the case for this tree's first exception to that.

    python3 .github/scripts/check_vendored_pin.py tests/_data/README.md
    python3 .github/scripts/check_vendored_pin.py scripts/seeds/README.md
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
# the fenced block's key/value lines, "ref" among them (module docstring
# above). "pulled" and the free-text "behind" line are not read here: a
# human updates both once a drift this script reports is actually
# resolved, which is the decision neither this script nor the workflow
# it runs in gets to make
_FIELD = re.compile(r"^(repo|path|commit|blob|ref)\s+(\S+)", re.MULTILINE)

# argv[0] plus the README path -- not a choice of anybody's
_ARGV = 2


@dataclass(frozen=True)
class Entry:
    """One pin this script can re-check: a local file, a live commit.

    `ref` is `None` for a pin tracking upstream's default branch, and a
    tag name for one pinned to a release instead -- module docstring
    above.
    """

    heading: str
    repo: str
    path: str
    commit: str
    blob: str
    ref: str | None = None


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
        repo, path, commit, blob, ref = (
            fields.get("repo"),
            fields.get("path"),
            fields.get("commit"),
            fields.get("blob"),
            fields.get("ref"),
        )
        if repo and path and commit and blob:
            entries.append(Entry(heading, repo, path, commit, blob, ref))
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


def check(entry: Entry, readme_path: str) -> list[str]:
    """Every way entry's pin disagrees with this tree or with upstream.

    A `ref`-pinned entry (module docstring above) is read against that
    ref rather than the default branch, and skips the "newest commit"
    check below: a tag gains no commits after the fact, so the question
    that check asks -- has upstream moved past what is pinned -- is
    already answered by the pin being to a release rather than to
    master.
    """
    problems = []
    local_blob = _local_blob(entry.heading)
    if local_blob != entry.blob:
        problems.append(
            f"{entry.heading}: the file in this tree hashes to"
            f" {local_blob}, {readme_path} records {entry.blob}"
        )
    ref = entry.ref or _default_branch(entry.repo)
    upstream_blob = _upstream_blob(entry.repo, entry.path, ref)
    if upstream_blob is None:
        problems.append(
            f"{entry.heading}: {entry.repo} has no {entry.path} at"
            f" {ref} any more -- renamed, moved or deleted upstream"
        )
    elif upstream_blob != entry.blob:
        problems.append(
            f"{entry.heading}: pinned blob {entry.blob}, {entry.repo}'s"
            f" {ref} carries {upstream_blob} at {entry.path}"
        )
    if entry.ref is not None:
        return problems
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
    problems = [p for entry in entries for p in check(entry, str(readme_path))]
    for problem in problems:
        print(f"DRIFT: {problem}")
    if not problems:
        # Two different claims, printed only for the entries each is true
        # of: "at upstream's tip" is not what a `ref`-pinned entry (module
        # docstring above) was checked against, and saying so for one
        # would overstate what passing it means.
        if any(entry.ref is None for entry in entries):
            print(
                "Every branch-tracked pin is still byte for byte, still at upstream's tip."
            )
        if any(entry.ref is not None for entry in entries):
            print("Every ref-pinned pin is still byte for byte, still at its own tag.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
