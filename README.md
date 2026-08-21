# btclib_node

[btclib_node](https://github.com/btclib-org/btclib_node) is a bitcoin node
with its consensus and network code written in python, using the
[btclib](https://github.com/btclib-org/btclib) bitcoin library.

**btclib_node** succeeded in downloading and validating the entire bitcoin
blockchain, starting from version 0.1.0 and, as far as I can tell, is the
first python implementation that was able to do so

## Test, develop, and contribute

The project uses [uv](https://docs.astral.sh/uv/) as its only prerequisite;
it fetches the interpreter and every dependency group itself.

```shell
uv sync
```

To test, coverage included:

```shell
uv run pytest
```

To run the lint gate:

```shell
uv run pre-commit run --all-files
```
