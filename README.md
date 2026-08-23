# btclib-node

btclib-node is a bitcoin node with its consensus and network code written
in python, using the [btclib](https://github.com/btclib-org/btclib)
bitcoin library.

**btclib-node** succeeded in downloading and validating the entire bitcoin
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
against. A vulnerability is reported as the
[security policy](https://github.com/btclib-org/btclib-node/security/policy)
says, never as an issue; that policy is the organization's, this
repository publishing nothing for a policy of its own to travel with,
and the section below is what it cannot say for this tree.

- Source: <https://github.com/btclib-org/btclib-node>
- [CHANGELOG.md](./CHANGELOG.md) for what changed. There is no release:
  what anybody runs is a checkout of `main`, and
  [CONTRIBUTING.md](./CONTRIBUTING.md)'s *A version, and no release* is
  what the version in `pyproject.toml` and the one tag are for
- [REPOSITORY.md](./REPOSITORY.md) for the settings that live outside the
  tree

## Limitations, not vulnerabilities

Known, recorded, and each an open issue rather than something to report
again.

- **The JSON-RPC listener binds every interface** — `0.0.0.0`, with no
    configuration option to bind loopback instead — **and authenticates
    nothing.** The method table it serves carries `stop` and
    `sendrawtransaction`, so anybody who can reach the port can stop the
    node and make it announce a transaction. Run it where nothing else
    can reach that port. btclib-org/btclib-node#27.
- **What a peer may ask for is not bounded by what asking costs it.** A
    short request can commit this node to a long reply, and nothing
    limits what one peer may have in flight. btclib-org/btclib-node#101.
- **`Development Status :: 3 - Alpha` is the claim `pyproject.toml`
    makes**, and it is the right one to read the two above against: this
    node has downloaded and validated the chain, which is not the same as
    having been run against somebody trying to make it do otherwise.
