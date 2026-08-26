# Release notes

Notable changes are documented here.
[CHANGELOG.md](./CHANGELOG.md) is the record behind them: this file says
what a user has to act on, that one says what changed and why.

Versions are *[calendar versions](https://calver.org/)*, `YYYY.M.D`,
once the first one is cut — `RELEASING.md` has why `pyproject.toml`
still declares `0.1.0` today. The number will say when a release was
cut, and it promises nothing about compatibility, so a breaking change
is announced in this file — read it before upgrading, rather than a
digit.

## Unreleased

No release has shipped: this section fills in one landed change at a
time, the way `RELEASING.md`'s *Release to PyPI* reads it back into the
first release's own pull request body.

### Breaking changes

- **`Node.__init__` no longer installs signal handlers** (closes #436).
  Act on it if a `Node` you build is meant to stop on an operator's
  `SIGINT`, `SIGTERM` or `SIGTSTP`: call `install_signal_handlers(node)`
  for it, the way `scripts/chains/` now does right after building the
  node each of them starts. A `Node` built without that call no longer
  responds to any of the three on its own.
