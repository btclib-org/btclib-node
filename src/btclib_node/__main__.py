# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`python -m btclib_node`, the other way to reach `cli.main`.

`[project.scripts]` (`pyproject.toml`) installs a console script that
reaches the same function through its own generated shim; this module
is for a checkout or a virtualenv nobody has run `pip install` in yet.

The `if __name__ == "__main__":` guard below is ordinary practice for a
package's own `__main__.py` -- calling `main` at import time would run
it as a side effect of `import btclib_node.__main__` -- but it is not
what keeps `-m btclib_node` clear of issue #579: `multiprocessing.
spawn`'s own `_fixup_main_from_name` special-cases any module whose
name ends in `.__main__` and returns without re-running it at all
("__main__.py files for packages, directories, zip archives, etc, run
their 'main only' code unconditionally, so we don't even try to
populate anything in __main__", `multiprocessing/spawn.py`, read at
CPython 3.14.7, this tree's own pinned interpreter). A spawned pool
worker whose parent was started with `-m btclib_node` never re-executes
this file's body either way. `cli.py`'s own module docstring is where
the route that *is* load-bearing for issue #579 -- the console
script's own generated shim -- is argued.
"""

from btclib_node.cli import main

if __name__ == "__main__":
    main()
