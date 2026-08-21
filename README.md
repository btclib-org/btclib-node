# btclib_node

[btclib_node](https://github.com/btclib-org/btclib_node) is a bitcoin node with its consensus and network code written in python, using the [btclib](https://github.com/btclib-org/btclib) bitcoin library.

**btclib_node** succeded in downloading and validating the entire bitcoin blokchain, starting from version 0.1.0 and, as far as I can tell, is the first python implementatin that was able to do so

## Test, develop, and contribute

The project uses [uv](https://docs.astral.sh/uv/) as its only prerequisite;
it fetches the interpreter and every dependency group itself.

    uv sync

To test, coverage included:

    uv run pytest

To run the lint gate:

    uv run pre-commit run --all-files
