# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""BIP325: whether a signet block carries a valid solution for its challenge.

Core's `CheckSignetBlockSolution` (`src/signet.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the coinbase's last BIP141
witness commitment output carries, appended after the 32-byte commitment
itself, a second push holding a 4-byte header and a solution -- a
scriptSig and a witness stack. Stripping that second push back to the
bare header and rehashing the coinbase is what two synthetic
transactions, built and discarded inside `assert_valid_solution` alone,
commit to instead of the untouched block, so the signers are signing the
block they will end up validating and not one a peer could still swap
the commitment out from under. The second transaction's single input is
run through `btclib.script.engine.verify_input` against the first's
single output -- `SIGNET_CHALLENGE`, the only challenge `chains.SigNet`
ever runs, its own docstring says why -- with `BLOCK_SCRIPT_VERIFY_FLAGS`'
own four flags and none of the others, the same set
`CheckSignetBlockSolution` verifies with.

A coinbase carrying no such second push is not a refusal: BIP325 reads
that as the trivial `OP_TRUE` challenge, satisfied by every solution
including the empty one, so the synthetic spend carries an empty
scriptSig and witness instead, unearthed from nothing to parse. A second
push that starts with the header but does not parse as a scriptSig and a
witness stack, or carries a byte beyond them, is `bad-signet-blksig` all
the same -- Core's own `catch` around its `SpanReader` answers
`std::nullopt` for it, which `CheckSignetBlockSolution` reports exactly
as it reports a solution that fails to verify.
"""

from typing import TYPE_CHECKING

from btclib import var_bytes, var_int
from btclib.hashes import hash256, merkle_root_and_mutated_from_hashes
from btclib.script import Witness
from btclib.script.engine import verify_input
from btclib.script.engine.flags import ScriptFlag
from btclib.script.script import op_code_spans, serialize
from btclib.tx import OutPoint, Tx, TxIn, TxOut
from btclib.utils import bytesio_from_binarydata

from btclib_node.exceptions import MisbehavingError

if TYPE_CHECKING:
    from btclib.block import Block

    from btclib_node.chains import Chain

__all__ = ["SIGNET_CHALLENGE", "assert_valid_solution"]

# SigNetParams's own default (`src/kernel/chainparams.cpp`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the only signet
# `chains.SigNet` runs, its own docstring says why, so this is the one
# challenge this whole module ever checks a solution against.
SIGNET_CHALLENGE = bytes.fromhex(
    "512103ad5e0edad18cb1f0fc0d28a3d4f1f3e445640337489abb10404f2d1e086be4"
    "30210359ef5021964fe22d6f8e05b2463c9540ce96883fe3b278760f048f5189f2e6c452ae"
)

# BLOCK_SCRIPT_VERIFY_FLAGS (`src/signet.cpp`, same sha)
_SCRIPT_FLAGS = (
    ScriptFlag.P2SH | ScriptFlag.WITNESS | ScriptFlag.DERSIG | ScriptFlag.NULLDUMMY
)

# BIP141's own aa21a9ed header, and the length of OP_RETURN, its
# push-length byte and the 32-byte commitment that follow it --
# MINIMUM_WITNESS_COMMITMENT (`src/consensus/validation.h`, same sha)
_COMMITMENT_PREFIX = bytes.fromhex("6a24aa21a9ed")
_COMMITMENT_LENGTH = len(_COMMITMENT_PREFIX) + 32

# SIGNET_HEADER (`src/signet.cpp`, same sha)
_SIGNET_HEADER = bytes.fromhex("ecc7daa2")


def _witness_commitment_output_index(coinbase: Tx) -> int | None:
    """Return the coinbase output BIP141's witness commitment sits at, or None.

    Core's `GetWitnessCommitmentIndex` (`src/consensus/validation.h`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the *last* output whose
    script starts with `_COMMITMENT_PREFIX` and is at least
    `_COMMITMENT_LENGTH` long, not the first -- an output appended after
    the miner's own could not be told from one it meant otherwise.
    """
    index = None
    for i, tx_out in enumerate(coinbase.vout):
        script = tx_out.script_pub_key.script
        if len(script) >= _COMMITMENT_LENGTH and script.startswith(_COMMITMENT_PREFIX):
            index = i
    return index


def _push_data(script: bytes, op_code: int, start: int, stop: int) -> bytes:
    """Return the bytes one `op_code_spans` span pushes, or b"" for a non-push.

    Reproduces `read_op_code`'s own offset arithmetic (`script.py`) to
    recover the data a span's `(start, stop)` already bounds -- that
    function returns the span and not the slice, since most of its
    other callers want the boundary and not the bytes. The bounds
    (75, 76, 78) are `read_op_code`'s own bare ones -- matched rather
    than named, as `block_index.py`'s own locator comment names the
    same reason for its own bare 10: naming them here would claim a
    meaning Core's own opcode table never gave them.
    """
    if not 0 < op_code <= 78:  # noqa: PLR2004
        return b""
    data_start = start + 1
    if op_code > 75:  # noqa: PLR2004
        size = 2 ** (op_code - 76)
        data_start += size
    return script[data_start:stop]


def _fetch_and_clear_signet_section(script: bytes) -> tuple[bytes, bytes | None]:
    """Strip the first push starting with `_SIGNET_HEADER` back to the header.

    Core's `FetchAndClearCommitmentSection` (`src/signet.cpp`, same sha):
    walks `script` op by op, rebuilding it unchanged except for the
    first push whose data both starts with `_SIGNET_HEADER` and carries
    more than just it -- trimmed to the header alone, its own remainder
    returned as the solution. Every push is re-emitted through
    `serialize`, Core's own `CScript::operator<<`, which always writes
    the minimal push encoding regardless of how the original was
    encoded -- a non-minimal push elsewhere in the script comes back
    minimal, matching Core's own rebuild rather than a byte-for-byte
    copy of it. `(script, None)` where nothing matches, BIP325's own
    licence for the trivial `OP_TRUE` challenge: nothing to sign is what
    an empty solution already is.
    """
    out = bytearray()
    solution = None
    found = False
    for op_code, start, stop in op_code_spans(script):
        data = _push_data(script, op_code, start, stop)
        if data:
            if (
                not found
                and len(data) > len(_SIGNET_HEADER)
                and data.startswith(_SIGNET_HEADER)
            ):
                solution = data[len(_SIGNET_HEADER) :]
                data = data[: len(_SIGNET_HEADER)]
                found = True
            out += serialize([data])
        else:
            out += script[start:stop]
    if not found:
        return script, None
    return bytes(out), solution


def _parse_solution(solution: bytes) -> tuple[bytes, Witness]:
    """Parse `solution` as a scriptSig and a witness stack, BIP325's own shape.

    Core's `SpanReader` reads of `tx_spending.vin[0].scriptSig` and
    `.scriptWitness.stack`, and its own `if (!v.empty())` after them: a
    solution carrying a byte past the two is `bad-signet-blksig` exactly
    as one that fails to parse at all, so both raise here, left to
    `assert_valid_solution`'s own catch-all to fold into one message.
    """
    stream = bytesio_from_binarydata(solution)
    script_sig = var_bytes.parse(stream)
    count = var_int.parse(stream)
    stack = [var_bytes.parse(stream) for _ in range(count)]
    if stream.read():
        # a plain ValueError, not MisbehavingError: assert_valid_solution's
        # own `except MisbehavingError: raise` would otherwise re-raise
        # this untagged, the one failure here not carrying the
        # `bad-signet-blksig:` prefix every other one does
        err_msg = "extraneous data in the signet solution"
        raise ValueError(err_msg)
    return script_sig, Witness(stack, check_validity=False)


def _signet_transactions(
    block: Block, coinbase: Tx, commitment_index: int
) -> tuple[Tx, Tx]:
    """Return (to_sign, to_spend), BIP325's own synthetic spend and its output.

    `SignetTxs::Create` (`src/signet.cpp`, same sha). Every field of
    both is checked nowhere else: they exist only to be handed to
    `verify_input`, `check_validity=False` throughout, the way
    `interpreter.py`'s own worker task takes what `Tx.parse` already
    validated once and asks nothing of it again.
    """
    commitment_script = coinbase.vout[commitment_index].script_pub_key.script
    stripped, solution = _fetch_and_clear_signet_section(commitment_script)
    script_sig, witness = (
        _parse_solution(solution) if solution is not None else (b"", Witness())
    )

    new_vout = list(coinbase.vout)
    new_vout[commitment_index] = TxOut(
        coinbase.vout[commitment_index].value, stripped, check_validity=False
    )
    modified_coinbase = Tx(
        coinbase.version,
        coinbase.lock_time,
        coinbase.vin,
        new_vout,
        check_validity=False,
    )
    leaves = [
        hash256(
            modified_coinbase.serialize(include_witness=False, check_validity=False)
        )
    ]
    leaves.extend(
        hash256(tx.serialize(include_witness=False, check_validity=False))
        for tx in block.transactions[1:]
    )
    signet_merkle_root = merkle_root_and_mutated_from_hashes(leaves, hash256)[0]

    # VectorWriter's own field order in SignetTxs::Create: nVersion,
    # hashPrevBlock, the modified merkle root, nTime, each in the raw
    # wire order a header's own serialize writes them -- not the
    # display order BlockHeader.previous_block_hash is read back in,
    # which is why this reverses it rather than using it as is.
    header = block.header
    block_commitment = (
        header.version.to_bytes(4, "little", signed=True)
        + header.previous_block_hash[::-1]
        + signet_merkle_root
        + int(header.time.timestamp()).to_bytes(4, "little")
    )

    to_spend = Tx(
        0,
        0,
        [TxIn(OutPoint(check_validity=False), b"\x00", 0, check_validity=False)],
        [TxOut(0, SIGNET_CHALLENGE, check_validity=False)],
        check_validity=False,
    )
    # CScript(OP_0) << block_data: appended after the dummy OP_0 already
    # in scriptSig, not replacing it -- CHECKMULTISIG's own off-by-one
    # still wants that dummy element on the stack ahead of it.
    to_spend.vin[0].script_sig = b"\x00" + serialize([block_commitment])

    to_sign = Tx(
        0,
        0,
        [
            TxIn(
                OutPoint(to_spend.id, 0, check_validity=False),
                script_sig,
                0,
                witness,
                check_validity=False,
            )
        ],
        [TxOut(0, b"\x6a", check_validity=False)],  # OP_RETURN
        check_validity=False,
    )
    return to_sign, to_spend


def assert_valid_solution(block: Block, chain: Chain) -> None:
    """Assert that `block` carries a valid BIP325 solution for its challenge.

    Genesis is exempt, as Core's own first line is
    (`CheckSignetBlockSolution`, same sha): it carries no witness
    commitment to solve against and predates the signature rule
    entirely. Every other block without one, or whose commitment parses
    to no usable solution, or whose solution does not satisfy
    `SIGNET_CHALLENGE`, is `bad-signet-blksig` (`BLOCK_CONSENSUS`, which
    `MaybePunishNodeForBlock` punishes, `net_processing.cpp`, same sha)
    -- a `MisbehavingError`, matching every other refusal
    `p2p.callbacks.block` and `rpc.callbacks.submit_block` already raise
    for a body failing `Block.assert_valid`.

    Called for a signet chain only -- this module carries no rule of
    its own that gates that, `main.assert_valid_block`'s own
    `isinstance(chain, SigNet)` does, ahead of calling this.
    """
    if block.header.hash == chain.genesis.hash:
        return
    if not block.transactions:
        err_msg = "bad-signet-blksig: no coinbase in block"
        raise MisbehavingError(err_msg)
    coinbase = block.transactions[0]
    commitment_index = _witness_commitment_output_index(coinbase)
    if commitment_index is None:
        err_msg = "bad-signet-blksig: no witness commitment in block"
        raise MisbehavingError(err_msg)

    try:
        to_sign, to_spend = _signet_transactions(block, coinbase, commitment_index)
        verify_input([to_spend.vout[0]], to_sign, 0, _SCRIPT_FLAGS)
    except Exception as e:
        # btclib's own exceptions, BTClibValueError chief among them,
        # caught broadly because a malformed solution can also reach the
        # interpreter as a plain IndexError or struct.error -- Core's own
        # SpanReader/VerifyScript pair answers both alike, folding into
        # CheckSignetBlockSolution's one bool
        err_msg = f"bad-signet-blksig: {e}"
        raise MisbehavingError(err_msg) from e
