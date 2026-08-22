# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What the run is held to, whatever the tests in it do.

A suite of nodes and sockets is a suite where a test can stop
answering, and an unbounded test that stops answering stops the run:
in CI it sits there until the job's own limit kills it, with nothing
said about which test it was. So the bound is part of the harness, and
this is what asks whether it is still there.
"""


def test_every_test_is_bounded(pytestconfig):
    # the value is measured and reasoned about where it is set,
    # pyproject.toml; what matters here is that a bound exists at all,
    # which it does not if the plugin is dropped from the dependency
    # group or the setting from the configuration
    assert pytestconfig.pluginmanager.hasplugin("timeout")
    assert float(pytestconfig.getini("timeout")) > 0
