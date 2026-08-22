# btclib_node

[btclib_node](https://github.com/btclib-org/btclib_node) is a bitcoin node
with its consensus and network code written in python, using the
[btclib](https://github.com/btclib-org/btclib) bitcoin library.

**btclib_node** succeeded in downloading and validating the entire bitcoin
blockchain, starting from version 0.1.0 and, as far as I can tell, is the
first python implementation that was able to do so

## Test, develop, and contribute

The project uses [uv](https://docs.astral.sh/uv/), which fetches the
interpreter and every dependency group itself.

LevelDB is the one thing it cannot fetch. `plyvel`, the binding this node
stores its blocks and chainstate through, publishes no wheel for macOS at
all and none past CPython 3.12 elsewhere, so it is built from source here
and needs LevelDB's headers and library present first:

```shell
brew install leveldb                     # macOS
sudo apt-get install libleveldb-dev      # Debian, Ubuntu
```

On macOS they land under Homebrew's prefix rather than in the compiler's
default search path, so that build needs to be pointed at them:

```shell
export CPATH="$(brew --prefix leveldb)/include"
export LIBRARY_PATH="$(brew --prefix leveldb)/lib"
```

Then:

```shell
uv sync
```

To test, coverage included:

```shell
uv run pytest
```

Every statement and every branch is covered, and that run fails if any
stops being. A run of less than the whole suite — one file, `-k`, `-m`,
`--last-failed` — cannot clear a floor meant for all of it, so it is not
held to one; `--cov-fail-under` asked for explicitly still applies.

To run the lint gate:

```shell
uv run pre-commit run --all-files
```
