# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Core's `CheckTransaction` reject reasons for what `Tx.assert_valid` refuses.

Core's `CheckTransaction` (`src/consensus/tx_check.cpp`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag) names each refusal with one
word. Core answers that word for a lone transaction, from `PreChecks`,
and for a block holding it, from `CheckBlock` (`src/validation.cpp`,
same sha), which hands the transaction's reject reason on as the
block's. `check_transaction_reason` answers it here for both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from btclib.exceptions import BTClibTypeError, BTClibValueError

if TYPE_CHECKING:
    from btclib.exceptions import BTClibException
    from btclib.tx import Tx

__all__ = ["check_transaction_reason"]


# Core's own `MAX_MONEY` (`src/consensus/amount.h`,
# at bitcoin/bitcoin@f0de87f040, the v32.1 tag): 21e6 BTC in satoshi, the
# same bound btclib's own `valid_sats_amount` enforces under a private
# name (`btclib/amount.py`) -- read again here only to tell
# `bad-txns-vout-negative` from `bad-txns-vout-toolarge` apart, one
# shared btclib message answering for both. btclib-org/btclib-node#1375
_MAX_SATOSHI = 21_000_000 * 100_000_000


# Every exact `Tx.assert_valid` message this translates one for one,
# barring the amount-range messages `_amount_reject_reason` below
# disambiguates and the oversize one `_reject_reason` matches by prefix,
# both carrying a value `Tx.assert_valid` computed and this dict cannot
# spell in advance.
_EXACT_REJECT_REASONS = {
    "Missing inputs": "bad-txns-vin-empty",
    "Missing outputs": "bad-txns-vout-empty",
    "the same outpoint is spent twice": "bad-txns-inputs-duplicate",
    "Invalid coinbase script size": "bad-cb-length",
    "coinbase input in a non-coinbase transaction": "bad-txns-prevout-null",
}

# `Tx.assert_valid`'s own oversize message (`btclib/tx/tx.py`, since
# btclib 2026.9.30, btclib-org/btclib#2420): "invalid transaction size:
# {size} * {WITNESS_SCALE_FACTOR} > {MAX_BLOCK_WEIGHT}", the last two
# numbers fixed but the first the transaction's own stripped size, so
# this is a prefix rather than an entry of `_EXACT_REJECT_REASONS`
# above. btclib-org/btclib-node#1447
_OVERSIZE_MESSAGE_PREFIX = "invalid transaction size:"


def _amount_reject_reason(tx: Tx, message: str) -> str | None:
    """Disambiguate the one btclib message shared by two of Core's own reasons.

    `valid_sats_amount` (`btclib/amount.py`) raises the identical
    "invalid satoshi amount" text for a negative value and for one
    above `MAX_MONEY`, where Core's own `CheckTransaction` tells them
    apart as `bad-txns-vout-negative` and `bad-txns-vout-toolarge`; this
    reads the field itself to say which. `None` where `message` is
    neither this nor the total-amount check's own message.
    """
    if message.startswith("invalid total output amount"):
        return "bad-txns-txouttotal-toolarge"
    if message.startswith("invalid satoshi amount"):
        for tx_out in tx.vout:
            if tx_out.value < 0:
                return "bad-txns-vout-negative"
            if tx_out.value > _MAX_SATOSHI:
                return "bad-txns-vout-toolarge"
    return None


def _reject_reason(tx: Tx, error: BTClibException) -> str:
    """Name `error`, a `Tx.assert_valid` refusal, Core's own way.

    `Tx.assert_valid`'s own docstring already claims it checks what
    Core's `CheckTransaction` checks of a lone transaction
    (`src/consensus/tx_check.cpp`, at bitcoin/bitcoin@f0de87f040, the
    v32.1 tag), and since btclib 2026.9.30 in the same order too:
    vin-empty, vout-empty, the size, the outputs, the duplicate inputs,
    the coinbase/prevout-null rule last (btclib-org/btclib#2417,
    btclib-org/btclib#2422). This does not run any rule a second time
    -- `error` is already btclib's own verdict, whichever rule
    `assert_valid` raised first in its own order -- it only answers
    which of Core's own reject reasons that message names, relying on
    that order matching `CheckTransaction`'s for a transaction that
    violates more than one rule at once to answer the same one of the
    two Core would: two inputs, one of them the null outpoint, and no
    outputs now answers `bad-txns-vout-empty` here, as it does in Core,
    `Tx.assert_valid` having reached the empty `vout` before the
    coinbase/prevout-null check reads either input
    (btclib-org/btclib-node#1375).

    Every message `assert_valid` can raise for a `Tx` read from the
    wire without checking it, as `rpc.callbacks._decode_hex_tx` and
    `Block.parse` read one, is named below, barring the type and
    field-range checks a parsed transaction cannot fail: `Tx.parse`
    builds each field with the type and width its encoding gives it. A
    message this does not recognize is `error` itself, re-raised --
    this tree's own equivalent of Core's `Assume(false)` for a consensus
    check the engine disagrees with itself about.
    """
    message = str(error)
    if message in _EXACT_REJECT_REASONS:
        return _EXACT_REJECT_REASONS[message]
    if message.startswith(_OVERSIZE_MESSAGE_PREFIX):
        return "bad-txns-oversize"
    reason = _amount_reject_reason(tx, message)
    if reason is not None:
        return reason
    raise error


def check_transaction_reason(tx: Tx) -> str | None:
    """Return Core's own `CheckTransaction` reject reason for `tx`, or None.

    `tx.assert_valid()` is the rule, the same one this node already
    trusts to judge a transaction, and this only translates a refusal
    into the reason string Core's own JSON-RPC answers name -- it never
    decides validity on its own account, `ARCHITECTURE.md`'s own
    *What is delegated, and what is not*. `_reject_reason`'s own
    docstring is where a transaction violating more than one of
    `CheckTransaction`'s rules at once is argued: since btclib
    2026.9.30, `assert_valid`'s own check order matches
    `CheckTransaction`'s, so the rule it raises first is the one Core
    would too (btclib-org/btclib#2417, btclib-org/btclib#2422).

    Core's `bad-txns-oversize` -- a transaction whose own non-witness
    size alone already exceeds a block's weight limit -- used to have
    no `Tx.assert_valid` check behind it to translate, and a
    transaction breaking only that rule was answered as one this node
    accepted rather than refused (btclib-org/btclib-node#1447). Closed
    by the same 2026.9.30 that fixed the check order:
    `Tx.assert_valid` now raises its own message for it, ahead of the
    per-output amount checks in `CheckTransaction`'s own order
    (btclib-org/btclib#2420), and `_reject_reason` matches that message
    by its fixed prefix, the transaction's own stripped size being the
    one part of it that varies.
    """
    try:
        tx.assert_valid()
    except (BTClibValueError, BTClibTypeError) as error:
        return _reject_reason(tx, error)
    return None
