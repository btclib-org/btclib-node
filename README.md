# btclib-node

btclib-node is a bitcoin node with its consensus and network code written
in python, using the [btclib](https://github.com/btclib-org/btclib)
bitcoin library.

**btclib-node** succeeded in downloading and validating the entire bitcoin
blockchain, starting from version 0.1.0 and, as far as I can tell, is the
first python implementation that was able to do so

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
