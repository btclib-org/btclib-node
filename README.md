# btclib-node

<!-- Section 2 of the organization's standard says which badges sit here. -->
[![PyPI version](https://img.shields.io/pypi/v/btclib-node.svg?logo=pypi)](https://pypi.python.org/pypi/btclib-node/)
[![GitHub release](https://img.shields.io/github/v/release/btclib-org/btclib-node.svg)](https://github.com/btclib-org/btclib-node/releases)
[![development status](https://img.shields.io/pypi/status/btclib-node.svg)](https://pypi.python.org/pypi/btclib-node/)
[![license](https://img.shields.io/github/license/btclib-org/btclib-node.svg)](https://github.com/btclib-org/btclib-node/blob/main/LICENSE)
[![downloads](https://static.pepy.tech/badge/btclib-node)](https://pepy.tech/projects/btclib-node)
[![supported Python versions](https://img.shields.io/pypi/pyversions/btclib-node.svg?logo=python)](https://pypi.python.org/pypi/btclib-node/)
[![implementation](https://img.shields.io/pypi/implementation/btclib-node.svg)](https://pypi.python.org/pypi/btclib-node/)
[![wheel](https://img.shields.io/pypi/wheel/btclib-node.svg)](https://pypi.python.org/pypi/btclib-node/)

[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/14816/badge)](https://www.bestpractices.dev/projects/14816)
[![OpenSSF Baseline](https://www.bestpractices.dev/projects/14816/baseline)](https://www.bestpractices.dev/projects/14816)

btclib-node is a full node bitcoin implementation written in Python and
based on btclib.

To install, or upgrade:

```shell
python -m pip install --upgrade btclib-node
```

Python 3.14 or newer: `pyproject.toml`'s `requires-python` says so and
says why, and pip refuses the wheel below it rather than installing
something that will not import. `btclib-node` is the command this puts
on `PATH`; `btclib-node -h` lists its options, spelled and read the way
Bitcoin Core's own are, and `-help-debug` adds the ones Core registers
for debugging. Read *Security* before running the node — the
classifier is `3 - Alpha` and the JSON-RPC listener is not something to
expose.

A checkout of `main` is a cycle in progress, declaring the month with no
day, so it is not the same thing as what pip installs;
[CONTRIBUTING.md](https://github.com/btclib-org/btclib-node/blob/main/CONTRIBUTING.md)
has how to work from one.

## Running a node

[Running a node](https://github.com/btclib-org/btclib-node/blob/main/docs/source/running_a_node.md)
is what comes after `pip install`: the command against each of the four
chains, pointing it at a peer of your own, reading its progress, the RPC
methods it answers, what it validates and what it does not, and what
script validation costs on mainnet.

## Architecture

[ARCHITECTURE.md](https://github.com/btclib-org/btclib-node/blob/main/ARCHITECTURE.md)
is the design: the loop `Node` runs, the protocol and the RPC surface
around it, and the store underneath.
[ASSURANCE_CASE.md](https://github.com/btclib-org/btclib-node/blob/main/ASSURANCE_CASE.md)
is why the security section below holds.

## Security

The JSON-RPC listener takes Bitcoin Core's cookie, `-rpcauth` and
`-rpcuser`/`-rpcpassword` credentials, and any caller it accepts may
call every method its `-rpcwhitelist` leaves it —
[SECURITY.md](https://github.com/btclib-org/btclib-node/blob/main/SECURITY.md)
carries that and the rest of what is known and recorded rather than a
vulnerability to report again, and how to report one that is not
already there.

## Contributing

Development is LLM-based: humans are welcome, as long as they are
comfortable with agentic development ;-). A contribution must have been
read and understood by the person who submits it.
[`CONTRIBUTING.md`](https://github.com/btclib-org/btclib-node/blob/main/CONTRIBUTING.md#working-with-an-llm)
has the rules.

[CONTRIBUTING.md](https://github.com/btclib-org/btclib-node/blob/main/CONTRIBUTING.md)'s
last section has the commands, and they are the ones CI runs: `uv sync`
builds the environment, `uv run pytest` is the suite and its coverage
floor, and the lint gate is `.pre-commit-config.yaml` run as `lint.yml`
runs it. uv is the only tool that has to be installed; it fetches the
interpreter and every dependency group itself.

How the organization decides, and who holds which role, is its
[GOVERNANCE.md](https://github.com/btclib-org/.github/blob/main/GOVERNANCE.md);
what it intends to do, and what it deliberately does not, is its
[ROADMAP.md](https://github.com/btclib-org/.github/blob/main/ROADMAP.md).

[REVIEWING.md](https://github.com/btclib-org/btclib-node/blob/main/REVIEWING.md)
is what a pull request here is answered against.

## Links

- Source: <https://github.com/btclib-org/btclib-node>
- Releases: <https://github.com/btclib-org/btclib-node/releases>
- [CHANGELOG.md](https://github.com/btclib-org/btclib-node/blob/main/CHANGELOG.md)
  for what changed, and
  [RELEASE_NOTES.md](https://github.com/btclib-org/btclib-node/blob/main/RELEASE_NOTES.md)
  for what a release asks a user to act on. `pyproject.toml` between
  releases declares the month a cycle is open on rather than a version
  anybody can install, and `CONTRIBUTING.md`'s *A release path, and what
  it has published* is what that shape is for
- [REPOSITORY.md](https://github.com/btclib-org/btclib-node/blob/main/REPOSITORY.md)
  for the settings that live outside the tree

## Status

<!-- Section 2 of the organization's standard says which badges sit here. -->
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/btclib-org/btclib-node/main.svg)](https://results.pre-commit.ci/latest/github/btclib-org/btclib-node/main)
[![lint workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/lint.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/lint.yml?query=branch%3Amain)
[![test workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/test.yml?query=branch%3Amain)
[![docs workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/docs.yml?query=branch%3Amain)
[![documentation build](https://app.readthedocs.org/projects/btclib-node/badge/?version=latest)](https://btclib-node.readthedocs.io)
[![vendored-vectors workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/vendored-vectors.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/vendored-vectors.yml?query=branch%3Amain)
[![bootstrap-dns workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/bootstrap-dns.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/bootstrap-dns.yml?query=branch%3Amain)
[![mutation workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/mutation.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/mutation.yml?query=branch%3Amain)
[![fuzz workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/fuzz.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/fuzz.yml?query=branch%3Amain)
[![integration-bitcoind workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/integration-bitcoind.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/integration-bitcoind.yml?query=branch%3Amain)
[![deps-latest workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/deps-latest.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/deps-latest.yml?query=branch%3Amain)
[![pypi-install workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/pypi-install.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/pypi-install.yml?query=branch%3Amain)
[![deps-oldest workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/deps-oldest.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/deps-oldest.yml?query=branch%3Amain)
[![os-macos workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/os-macos.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/os-macos.yml?query=branch%3Amain)
[![slow workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/slow.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/slow.yml?query=branch%3Amain)
[![os-ubuntu workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/os-ubuntu.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/os-ubuntu.yml?query=branch%3Amain)
[![links workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/links.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/links.yml?query=branch%3Amain)
[![sdist-rebuild workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/sdist-rebuild.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/sdist-rebuild.yml?query=branch%3Amain)
[![codeql workflow status](https://github.com/btclib-org/btclib-node/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/btclib-org/btclib-node/actions/workflows/codeql.yml?query=branch%3Amain)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/btclib-org/btclib-node/badge)](https://scorecard.dev/viewer/?uri=github.com/btclib-org/btclib-node)

---

The btclib organization and its projects are actively supported by
[DGI](https://dgi.io) and [CheckSig](https://checksig.com).
