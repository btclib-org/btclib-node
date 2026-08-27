# Release notes

Notable changes are documented here.
[CHANGELOG.md](./CHANGELOG.md) is the record behind them: this file says
what a user has to act on, that one says what changed and why.

Versions are *[calendar versions](https://calver.org/)*, `YYYY.M.D`;
between releases `pyproject.toml` declares the month alone, which is the
shape `RELEASING.md` gives a cycle in progress. The number says when a
release was cut, and it promises nothing about compatibility, so a
breaking change is announced in this file — read it before upgrading,
rather than a digit.

## v2026.8.27

**The first release of btclib-node.** Nothing here is an upgrade: no
version of this package has ever been on an index, so there is no
installation for this one to change the behaviour of and nothing to
read these notes against. `pip install btclib-node` reaches a released
btclib-node for the first time with this version.

What it is, and what it is not, is `README.md`'s: a bitcoin node whose
consensus and network code is python, over
[btclib](https://github.com/btclib-org/btclib). It has downloaded and
validated the whole chain. Its `Development Status` classifier says
`3 - Alpha` and means it — the interfaces are not promised stable, and
this file is where a break in them is announced from the next release
on.

`CHANGELOG.md`'s own `v2026.8.27` section is the record of everything
that went into it, which for a first release is the whole history
rather than a cycle's worth.

### Two things to know before installing

- **The JSON-RPC listener binds every interface and authenticates
  nothing.** [SECURITY.md](./SECURITY.md) carries that and the rest of
  what is known. Do not expose it.
- **`Node.__init__` does not install signal handlers** (#436). If a
  `Node` you build is meant to stop on an operator's `SIGINT`,
  `SIGTERM` or `SIGTSTP`, call `install_signal_handlers(node)` for it,
  the way `scripts/chains/` does right after building the node each of
  them starts. This is listed here rather than under a *Breaking
  changes* heading on purpose: nothing published can have broken,
  there having been nothing published, and the change is a break only
  against the unreleased tree — anyone who was running this from git
  before #467 landed is the only reader it can surprise.
