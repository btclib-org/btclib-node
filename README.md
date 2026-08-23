# btclib_node

[btclib_node](https://github.com/btclib-org/btclib_node) is a bitcoin node
with its consensus and network code written in python, using the
[btclib](https://github.com/btclib-org/btclib) bitcoin library.

**btclib_node** succeeded in downloading and validating the entire bitcoin
blockchain, starting from version 0.1.0 and, as far as I can tell, is the
first python implementation that was able to do so

## Contributing

[CONTRIBUTING.md](./CONTRIBUTING.md)'s last section has the commands, and
they are the ones CI runs: `uv sync` builds the environment, `uv run
pytest` is the suite and its coverage floor, and the lint gate is
`.pre-commit-config.yaml` run as `lint.yml` runs it. uv is the only tool
that has to be installed; it fetches the interpreter and every dependency
group itself.

[REVIEWING.md](./REVIEWING.md) is what a pull request here is answered
against, and [SECURITY.md](./SECURITY.md) is how a vulnerability is
reported and what is this node's to answer for.

- Source: <https://github.com/btclib-org/btclib-node>
- [CHANGELOG.md](./CHANGELOG.md), and [RELEASE_NOTES.md](./RELEASE_NOTES.md)
  for what a release would ask a user to act on
- [REPOSITORY.md](./REPOSITORY.md) for the settings that live outside the
  tree
