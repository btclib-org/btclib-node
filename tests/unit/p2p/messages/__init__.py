# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import pytest

from btclib_node.p2p.messages import (
    WrongChecksumError,
    add_headers,
    get_payload,
    verify_headers,
)


def test_valid():
    message_name = "message"
    payload = b"\x00"
    magic = b"\xff\xff\xff\xff"
    msg = magic + add_headers(message_name, payload)
    verify_headers(msg)
    assert get_payload(msg)[0] == message_name
    assert get_payload(msg)[1] == payload


def test_invalid_tamper():
    message_name = "message"
    payload = b"\x00"
    magic = b"\xff\xff\xff\xff"
    msg = magic + add_headers(message_name, payload)
    tampered_msg = msg[:-1] + b"\x01"
    with pytest.raises(WrongChecksumError):
        verify_headers(tampered_msg)


def test_invalid_length():
    message_name = "message"
    payload = b"\x00"
    magic = b"\xff\xff\xff\xff"
    msg = magic + add_headers(message_name, payload)
    short_msg = msg[:-1]
    with pytest.raises(ValueError):
        verify_headers(short_msg)
