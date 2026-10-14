# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests too slow for every run, each carrying `pytest.mark.slow`.

A run without `--run-slow` skips them, `tests/conftest.py` being where
that is decided. `tests/slow/` is in `testpaths` and left out of the
coverage ratchet, a body that is skipped being an uncovered line rather
than a defect. What runs them is `slow.yml`. `tests/README.md` has the
tier.
"""
