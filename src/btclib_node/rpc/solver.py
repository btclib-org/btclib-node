# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Bitcoin Core's `Solver`, as `scriptPubKey.type` and its `address` read it.

A stand-in for `btclib.script.solver.solver` (btclib-org/btclib#2458,
merged, not yet released), which this tree cannot import while its btclib
floor is a release without it: once the floor moves, delete this module
and import `solver` from there. Its source is the one this is copied
from, which reads `Solver` and `GetTxnOutputType` (`src/script/solver.cpp`)
at bitcoin/bitcoin@9be056a8a7, and it is kept to what an RPC answer needs:
the type name and the solutions, nothing a standardness check adds.

`btclib.script.script_pub_key.type_and_payload` is not a substitute. It
names its types its own way and classifies differently on purpose: it
parses the keys of a p2pk and a p2ms as curve points where `Solver` checks
`CPubKey::ValidSize` only, and it takes a nulldata of one push of at most
80 bytes where `Solver` takes any push-only data. A type name taken from
it is a name Core does not answer for such a script.
"""

from btclib.script.limits import MAX_PUBKEYS_PER_MULTISIG
from btclib.script.script import read_op_code
from btclib.utils import decode_num

__all__ = ["solver"]

_OP_RETURN = 0x6A
_OP_1 = 0x51
_OP_16 = 0x60
_OP_CHECKSIG = 0xAC
_OP_HASH160 = 0xA9
_OP_EQUAL = 0x87
_HASH160_SIZE = 0x14
_OP_CHECKMULTISIG = 0xAE
_OP_PUSHDATA1 = 0x4C
_OP_PUSHDATA2 = 0x4D
_OP_PUSHDATA4 = 0x4E
_MAX_DIRECT_PUSH = 75
_SMALL_NUMBER_MAX = 16
# the one byte a push of -1 is
_MINUS_ONE = 0x81
_P2PKH_SIZE = 25
_MAX_ONE_BYTE = 255
_MAX_TWO_BYTES = 65535

# CPubKey::SIZE and CPubKey::COMPRESSED_SIZE
_PUB_KEY_SIZE = 65
_COMPRESSED_PUB_KEY_SIZE = 33

# The program sizes of `WitnessV0KeyHash`, `WitnessV0ScriptHash` and
# `WitnessV1Taproot`
_WITNESS_V0_KEYHASH_SIZE = 20
_WITNESS_V0_SCRIPTHASH_SIZE = 32
_WITNESS_V1_TAPROOT_SIZE = 32

# The longest script that is a P2SH output, and a witness program's bounds
# (`CScript::IsWitnessProgram`, `src/script/script.cpp`)
_P2SH_SIZE = 23
_MIN_WITNESS_PROGRAM_SCRIPT = 4
_MAX_WITNESS_PROGRAM_SCRIPT = 42


def _valid_pub_key_size(data: bytes) -> bool:
    """Answer `CPubKey::ValidSize`: the length the first byte calls for."""
    if not data:
        return False
    if data[0] in {0x02, 0x03}:
        return len(data) == _COMPRESSED_PUB_KEY_SIZE
    if data[0] in {0x04, 0x06, 0x07}:
        return len(data) == _PUB_KEY_SIZE
    return False


def _witness_program(script: bytes) -> tuple[int, bytes] | None:
    """Return (version, program), as `CScript::IsWitnessProgram` does."""
    if not _MIN_WITNESS_PROGRAM_SCRIPT <= len(script) <= _MAX_WITNESS_PROGRAM_SCRIPT:
        return None
    if script[0] != 0 and not _OP_1 <= script[0] <= _OP_16:
        return None
    if script[1] + 2 != len(script):
        return None
    version = 0 if script[0] == 0 else script[0] - 0x50
    return version, script[2:]


def _op_data(script: bytes, start: int, op_code: int, stop: int) -> bytes:
    """Return the data `GetOp` hands back for the op code at `start`."""
    if 0 < op_code <= _MAX_DIRECT_PUSH:
        return script[start + 1 : stop]
    if _OP_PUSHDATA1 <= op_code <= _OP_PUSHDATA4:
        return script[start + 1 + 2 ** (op_code - 76) : stop]
    return b""


def _is_push_only(script: bytes, start: int) -> bool:
    """Answer `CScript::IsPushOnly` from `start`: nothing above OP_16."""
    while start < len(script):
        span = read_op_code(script, start)
        if span is None or span[0] > _OP_16:
            return False
        start = span[1]
    return True


def _check_minimal_push(data: bytes, op_code: int) -> bool:
    """Answer Core's `CheckMinimalPush`."""
    size = len(data)
    if size == 0:
        return op_code == 0
    # a number OP_1..OP_16 or OP_1NEGATE spells has an op code of its own
    if size == 1 and (1 <= data[0] <= _SMALL_NUMBER_MAX or data[0] == _MINUS_ONE):
        return False
    if size <= _MAX_DIRECT_PUSH:
        return op_code == size
    if size <= _MAX_ONE_BYTE:
        return op_code == _OP_PUSHDATA1
    if size <= _MAX_TWO_BYTES:
        return op_code == _OP_PUSHDATA2
    return True


def _script_number(op_code: int, data: bytes, low: int, high: int) -> int | None:
    """Return the number an op code or a push holds, in [low, high].

    Core's `GetScriptNumber`: OP_1..OP_16 by the op code, a push only if
    minimal, and None for anything else. The four-byte bound of
    `CScriptNum` is not written out: a number of more than four bytes is
    beyond any range asked for here.
    """
    if _OP_1 <= op_code <= _OP_16:
        count = op_code - 0x50
    elif 0 < op_code <= _OP_PUSHDATA4:
        if not _check_minimal_push(data, op_code):
            return None
        count = decode_num(data)
        if data and data[-1] & 0x7F == 0 and (len(data) == 1 or not data[-2] & 0x80):
            return None  # CScriptNum refuses a non-minimal encoding
    else:
        return None
    return count if low <= count <= high else None


def _match_pay_to_pub_key(script: bytes) -> bytes | None:
    for size in (_PUB_KEY_SIZE, _COMPRESSED_PUB_KEY_SIZE):
        if len(script) == size + 2 and script[0] == size and script[-1] == _OP_CHECKSIG:
            pub_key = script[1 : size + 1]
            return pub_key if _valid_pub_key_size(pub_key) else None
    return None


def _match_pay_to_pub_key_hash(script: bytes) -> bytes | None:
    if (
        len(script) == _P2PKH_SIZE
        and script[:3] == b"\x76\xa9\x14"
        and script[23:] == b"\x88\xac"
    ):
        return script[3:23]
    return None


def _match_multisig(script: bytes) -> tuple[int, list[bytes]] | None:
    """Read m and the keys, as Core's `MatchMultisig` does."""
    if not script or script[-1] != _OP_CHECKMULTISIG:
        return None

    span = read_op_code(script, 0)
    if span is None:
        return None
    required = _script_number(
        span[0], _op_data(script, 0, *span), 1, MAX_PUBKEYS_PER_MULTISIG
    )
    if required is None:
        return None
    position = span[1]

    pub_keys: list[bytes] = []
    # the op that ends the loop is the candidate for n: one read and not a
    # key, or none that could be read, which GetOp leaves as
    # OP_INVALIDOPCODE and no data
    op_code, data = 0xFF, b""
    while (span := read_op_code(script, position)) is not None:
        op_code, data = span[0], _op_data(script, position, *span)
        position = span[1]
        if not _valid_pub_key_size(data):
            break
        pub_keys.append(data)
    else:
        op_code, data = 0xFF, b""

    count = _script_number(op_code, data, required, MAX_PUBKEYS_PER_MULTISIG)
    if count is None or len(pub_keys) != count:
        return None
    # only OP_CHECKMULTISIG is left
    if position + 1 != len(script):
        return None
    return required, pub_keys


def _solve_witness_program(
    script: bytes, version: int, program: bytes
) -> tuple[str, list[bytes]]:
    """Classify a witness program, in Core's order."""
    if version == 0 and len(program) == _WITNESS_V0_KEYHASH_SIZE:
        return "witness_v0_keyhash", [program]
    if version == 0 and len(program) == _WITNESS_V0_SCRIPTHASH_SIZE:
        return "witness_v0_scripthash", [program]
    if version == 1 and len(program) == _WITNESS_V1_TAPROOT_SIZE:
        return "witness_v1_taproot", [program]
    if script == b"\x51\x02\x4e\x73":  # CScript::IsPayToAnchor
        return "anchor", []
    if version != 0:
        return "witness_unknown", [bytes([version]), program]
    return "nonstandard", []


def _solve_without_witness(script: bytes) -> tuple[str, list[bytes]]:
    """Classify a script that is neither P2SH nor a witness program."""
    # any data after the OP_RETURN, so long as it is push-only
    if script and script[0] == _OP_RETURN and _is_push_only(script, 1):
        return "nulldata", []

    if (pub_key := _match_pay_to_pub_key(script)) is not None:
        return "pubkey", [pub_key]

    if (pub_key_hash := _match_pay_to_pub_key_hash(script)) is not None:
        return "pubkeyhash", [pub_key_hash]

    if (multisig := _match_multisig(script)) is not None:
        required, pub_keys = multisig
        return "multisig", [bytes([required]), *pub_keys, bytes([len(pub_keys)])]

    return "nonstandard", []


def solver(script_pub_key: bytes) -> tuple[str, list[bytes]]:
    """Classify a script_pub_key as Core's `Solver` does.

    Returns `GetTxnOutputType`'s name for the type and the solutions
    Core fills for it: the script hash for "scripthash"; the key or key
    hash for "pubkey" and "pubkeyhash"; m, each key and n for
    "multisig", m and n as one byte each; the program for the three
    witness types whose version is implied, and the version byte then
    the program for "witness_unknown". "nulldata", "anchor" and
    "nonstandard" have none.

    The checks run in Core's order, which decides the answer: a witness
    program of v0 and a length other than 20 or 32 is "nonstandard" and
    no later shape is tried on it. Keys are checked for their size and
    prefix only, never parsed as points, and nulldata has no size cap
    (that one is relay policy, which `IsStandardTx` applies).
    """
    script = script_pub_key

    if (
        len(script) == _P2SH_SIZE
        and script[0] == _OP_HASH160
        and script[1] == _HASH160_SIZE
        and script[22] == _OP_EQUAL
    ):
        return "scripthash", [script[2:22]]

    if (witness := _witness_program(script)) is not None:
        return _solve_witness_program(script, *witness)

    return _solve_without_witness(script)
