# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import pytest
from btclib.exceptions import BTClibValueError
from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.constants import ProtocolVersion
from btclib_node.p2p.messages.handshake import Version
from tests.helpers import local_addr

MAGIC = RegTest().magic


def a_version(*, user_agent="/Btclib/", relay=True):
    services = 1032 + 1
    return Version(
        version=ProtocolVersion,
        services=services,
        timestamp=1,
        addr_recv=local_addr(1),
        addr_from=local_addr(1, services=services),
        nonce=1,
        user_agent=user_agent,
        start_height=0,
        relay=relay,
    )


def round_trip(msg):
    msg_bytes = msg.to_message(MAGIC).serialize()
    return Version.parse(Message.parse(msg_bytes).payload)


def test_version():
    msg = a_version()
    assert msg == round_trip(msg)


def test_version_without_agent():
    msg = a_version(user_agent="")
    assert msg == round_trip(msg)


def test_a_version_without_the_relay_flag_is_asking_for_relay():
    # BIP37 added the flag at 70001 and made its absence mean true, and
    # Core reads it so: reading the missing octet as a zero says the
    # opposite of what such a peer asked for. None and not False is what
    # keeps the two apart.
    msg = a_version(relay=None)
    assert msg.relay is None
    assert msg.is_relay_requested


def test_a_version_that_omitted_the_flag_is_written_back_without_it():
    with_flag = a_version(relay=False).serialize()
    without = a_version(relay=None).serialize()
    assert len(with_flag) == len(without) + 1
    assert with_flag[:-1] == without
    assert round_trip(a_version(relay=None)) == a_version(relay=None)


@pytest.mark.parametrize("relay", [True, False], ids=["true", "false"])
def test_a_relay_flag_that_is_there_is_what_the_peer_said(relay):
    msg = round_trip(a_version(relay=relay))
    assert msg.relay is relay
    assert msg.is_relay_requested is relay


def test_a_truncated_version_is_refused_rather_than_read_as_zeros():
    # the four octets of the protocol version and nothing else: read
    # short, every field past the cut comes back zero and the message
    # parses, which is a peer with no services and a junk address
    payload = a_version().serialize()[:4]
    with pytest.raises(BTClibValueError, match="not enough data for the services"):
        Version.parse(payload)
