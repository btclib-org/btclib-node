# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What needs something this repository does not ship: a real `bitcoind`.

Every module here skips itself unless `BTCLIB_NODE_INTEGRATION` is set,
`conftest.py`'s own `bitcoind_path` fixture being where that is decided
and named. Section 7 of the organization standard is why this directory
exists apart from `tests/unit/` and `tests/functional/`, both of which
run against nothing but this repository's own code: `tests/integration/`
is excluded from `testpaths` and from the coverage ratchet, and is
covered instead by `integration-bitcoind.yml`, which fails if what it
runs skips rather than actually reaching a node.
"""
