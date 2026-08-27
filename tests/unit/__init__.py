# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests mostly mirroring `src/btclib_node/`'s own layout, module for module.

A test here is free to reach into the object under test directly, even
where -- as in `init_test.py` -- that object is a fully started node of
its own. `tests/README.md` declares the split from `functional/` and
`integration/`, and why it exists.
"""
