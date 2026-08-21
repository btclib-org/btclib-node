# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import struct

from btclib.hashes import hash256


class WrongChecksumError(Exception):
    pass


# does not add p2pwork_string
def add_headers(name: str, payload: bytes):
    command = name + ((12 - len(name)) * "\00")
    payload_len = struct.pack("I", len(payload))
    checksum = hash256(payload)[:4]
    return command.encode() + payload_len + checksum + payload


def verify_headers(message: bytes):
    if len(message) < 24:
        raise ValueError("Not enough data")
    payload_len = int.from_bytes(message[16:20], "little")
    checksum = message[20:24]
    if len(message) < 24 + payload_len:
        raise ValueError("Not enough data")
    if checksum != hash256(message[24 : 24 + payload_len])[:4]:
        raise WrongChecksumError("Wrong checksum, the message might have been tampered")
    return True


def get_payload(message: bytes):
    verify_headers(message)
    message_name = message[4:16].rstrip(b"\x00").decode()
    payload_len = int.from_bytes(message[16:20], "little")
    payload = message[24 : 24 + payload_len]
    return (message_name, payload)
