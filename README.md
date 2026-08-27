# btclib-node

<!-- The badges are what the reader decides with: the first line says what
this is and whether it can be used, and the second whether it works. A
badge that reports no state -- "we use ruff", "we use uv" -- reports a
choice instead, and those are in CONTRIBUTING.md, beside the prose that
says how the choice is enforced. One badge per line keeps a change to one
line and every line inside MD013, whose 80 columns bind only where a space
follows them.
The index badges answer "package or version not found" until a first
release reaches PyPI, which CONTRIBUTING.md's *A release path, and
nothing published on it yet* dates rather than explains away.
The OpenSSF Scorecard badge sits in the sentinels' calendar order like
every other: section 10 of btclib-org/.github's README gives scorecard a
day/hour row, and the exception that section states for it -- its
triggers are the action's own -- is to the trigger rule rather than to
the calendar. No Read the Docs badge: no project there is connected to
this repository, which REPOSITORY.md's *What is not configured, and why*
records with the reason. -->
[![GitHub release](https://img.shields.io/github/v/release/btclib-org/btclib-node.svg)](https://github.com/btclib-org/btclib-node/releases)
[![PyPI version](https://img.shields.io/pypi/v/btclib-node.svg?logo=pypi)](https://pypi.python.org/pypi/btclib-node/)
[![downloads](https://static.pepy.tech/badge/btclib-node)](https://pepy.tech/project/btclib-node)
[![supported Python versions](https://img.shields.io/pypi/pyversions/btclib-node.svg?logo=python)](https://pypi.python.org/pypi/btclib-node/)
[![implementation](https://img.shields.io/pypi/implementation/btclib-node.svg)](https://pypi.python.org/pypi/btclib-node/)
[![development status](https://img.shields.io/pypi/status/btclib-node.svg)](https://pypi.python.org/pypi/btclib-node/)
[![license](https://img.shields.io/github/license/btclib-org/btclib-node.svg)](https://github.com/btclib-org/btclib-node/blob/main/LICENSE)

[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/btclib-org/btclib-node/main.svg)](https://results.pre-commit.ci/latest/github/btclib-org/btclib-node/main)
[![test workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/test.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/test.yml)
[![vendored-vectors workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/vendored-vectors.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/vendored-vectors.yml)
[![mutation workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/mutation.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/mutation.yml)
[![lint workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/lint.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/lint.yml)
[![links workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/links.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/links.yml)
[![docs workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/docs.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/docs.yml)
[![codeql workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/codeql.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/codeql.yml)
[![deps-latest workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/deps-latest.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/deps-latest.yml)
[![bootstrap-dns workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/bootstrap-dns.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/bootstrap-dns.yml)
[![os-macos workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/os-macos.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/os-macos.yml)
[![integration-bitcoind workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/integration-bitcoind.yml/badge.svg)](https://github.com/btclib-org/btclib-node/actions/workflows/integration-bitcoind.yml)

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/btclib-org/btclib-node/badge)](https://scorecard.dev/viewer/?uri=github.com/btclib-org/btclib-node)

btclib-node is a full node bitcoin implementation written in Python and
based on btclib.

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
- Releases: <https://github.com/btclib-org/btclib-node/releases> — none
  yet, `RELEASING.md` has why
- [CHANGELOG.md](./CHANGELOG.md) for what changed, and
  [RELEASE_NOTES.md](./RELEASE_NOTES.md) for what a release asks a user
  to act on. There is no release: what anybody runs is a checkout of
  `main`, and `CONTRIBUTING.md`'s *A release path, and nothing published
  on it yet* is what the version in `pyproject.toml` and the one tag are
  for
- [REPOSITORY.md](./REPOSITORY.md) for the settings that live outside the
  tree

---

The btclib organization and its projects are actively supported by
[DGI](https://dgi.io) and [CheckSig](https://checksig.com).
