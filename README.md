# btclib-node

<!-- The badges are what the reader decides with, in three groups: what
the software is and whether it can be used, whether it works, and what
the OpenSSF makes of it. Section 2 of btclib-org/.github's README.md
fixes which badges a tree carries, the ref each workflow-status badge
reads, and the order they go in; this block follows it rather than
restating it.

Inside the second group the gates come first, in the order a commit
meets them, and the sentinels follow in the order section 10's calendar
schedules them -- the badge order *is* the calendar order over that
subset, which is why the two move together or not at all. The day and
hour each sentinel owns live in that section rather than here, where a
reader wanting the schedule finds it still true.

One badge per line keeps a change to one line and every line inside
MD013, whose 80 columns bind only where a space follows them.

A badge that reports no state -- "we use ruff", "we use uv" -- reports a
choice instead, and those are in CONTRIBUTING.md, beside the prose that
says how the choice is enforced.

The index badges resolve against a real project since v2026.8.27; they
answered "package or version not found" before it, which
CONTRIBUTING.md's *A release path, and what it has published* records
along with the command that reads the index back. -->
[![PyPI version](https://img.shields.io/pypi/v/btclib-node.svg?logo=pypi)](https://pypi.python.org/pypi/btclib-node/)
[![GitHub release](https://img.shields.io/github/v/release/btclib-org/btclib-node.svg)](https://github.com/btclib-org/btclib-node/releases)
[![development status](https://img.shields.io/pypi/status/btclib-node.svg)](https://pypi.python.org/pypi/btclib-node/)
[![license](https://img.shields.io/github/license/btclib-org/btclib-node.svg)](https://github.com/btclib-org/btclib-node/blob/main/LICENSE)
[![downloads](https://static.pepy.tech/badge/btclib-node)](https://pepy.tech/projects/btclib-node)
[![supported Python versions](https://img.shields.io/pypi/pyversions/btclib-node.svg?logo=python)](https://pypi.python.org/pypi/btclib-node/)
[![implementation](https://img.shields.io/pypi/implementation/btclib-node.svg)](https://pypi.python.org/pypi/btclib-node/)
[![wheel](https://img.shields.io/pypi/wheel/btclib-node.svg)](https://pypi.python.org/pypi/btclib-node/)

[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/btclib-org/btclib-node/main.svg)](https://results.pre-commit.ci/latest/github/btclib-org/btclib-node/main)
[![lint workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/lint.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/lint.yml)
[![test workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/test.yml)
[![docs workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/docs.yml)
[![documentation build](https://app.readthedocs.org/projects/btclib-node/badge/?version=latest)](https://btclib-node.readthedocs.io)
[![vendored-vectors workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/vendored-vectors.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/vendored-vectors.yml)
[![bootstrap-dns workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/bootstrap-dns.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/bootstrap-dns.yml)
[![mutation workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/mutation.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/mutation.yml)
[![fuzz workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/fuzz.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/fuzz.yml)
[![integration-bitcoind workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/integration-bitcoind.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/integration-bitcoind.yml)
[![deps-latest workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/deps-latest.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/deps-latest.yml)
[![os-macos workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/os-macos.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/os-macos.yml)
[![os-ubuntu workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/os-ubuntu.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/os-ubuntu.yml)
[![links workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/links.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/links.yml)
[![codeql workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/codeql.yml)

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/btclib-org/btclib-node/badge)](https://scorecard.dev/viewer/?uri=github.com/btclib-org/btclib-node)

btclib-node is a full node bitcoin implementation written in Python and
based on btclib.

To install, or upgrade:

```shell
python -m pip install --upgrade btclib-node
```

Python 3.14 or newer: `pyproject.toml`'s `requires-python` says so and
says why, and pip refuses the wheel below it rather than installing
something that will not import. `btclib-node` is the command this puts
on `PATH`; `btclib-node -h` lists every option, spelled the way Bitcoin
Core spells its own. Read *Security* before running the node — the
classifier is `3 - Alpha` and the JSON-RPC listener is not something to
expose.

A checkout of `main` is a cycle in progress, declaring the month with no
day, so it is not the same thing as what pip installs;
[CONTRIBUTING.md](./CONTRIBUTING.md) has how to work from one.

## Running a node

[Running a node](./docs/source/running_a_node.md) is what comes after
`pip install`: the command against each of the four chains, pointing it
at a peer of your own, reading its progress, the RPC methods it
answers, what it validates and what it does not, and what a mainnet
sync has actually been measured to cost.

## Security

The JSON-RPC listener binds every interface and authenticates nothing —
[SECURITY.md](./SECURITY.md) carries that and the rest of what is known
and recorded rather than a vulnerability to report again, and how to
report one that is not already there.

## Contributing

[CONTRIBUTING.md](./CONTRIBUTING.md)'s last section has the commands, and
they are the ones CI runs: `uv sync` builds the environment, `uv run
pytest` is the suite and its coverage floor, and the lint gate is
`.pre-commit-config.yaml` run as `lint.yml` runs it. uv is the only tool
that has to be installed; it fetches the interpreter and every dependency
group itself.

[REVIEWING.md](./REVIEWING.md) is what a pull request here is answered
against.

## Links

- Source: <https://github.com/btclib-org/btclib-node>
- Releases: <https://github.com/btclib-org/btclib-node/releases>
- [CHANGELOG.md](./CHANGELOG.md) for what changed, and
  [RELEASE_NOTES.md](./RELEASE_NOTES.md) for what a release asks a user
  to act on. `pyproject.toml` between releases declares the month a
  cycle is open on rather than a version anybody can install, and
  `CONTRIBUTING.md`'s *A release path, and what it has published* is
  what that shape is for
- [REPOSITORY.md](./REPOSITORY.md) for the settings that live outside the
  tree

---

The btclib organization and its projects are actively supported by
[DGI](https://dgi.io) and [CheckSig](https://checksig.com).
