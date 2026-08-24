# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from typing import TYPE_CHECKING, Any, cast

from btclib.exceptions import BTClibException, BTClibValueError
from btclib.p2p.address import ServiceFlags
from btclib.p2p.addrv2 import BIP155Network
from btclib.p2p.handshake import Version
from btclib.tx import Tx

from btclib_node.constants import P2pConnStatus
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.main import verify_mempool_acceptance
from btclib_node.p2p.address import ip_and_port
from btclib_node.rpc.errors import RpcError, RpcErrorCode, bool_param, json_type_name

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import Connection


def get_best_block_hash(node: Node, conn: Connection, _: list[Any]) -> bytes:
    return node.chainstate.block_index.active_chain[-1]


def get_block_count(node: Node, conn: Connection, _: list[Any]) -> int:
    # the genesis block is active_chain[0] and Core's own height for it
    # is 0 (src/validation.h's nHeight on the genesis CBlockIndex), so
    # the count is the list's own last index, not its length
    return len(node.chainstate.block_index.active_chain) - 1


# Core's own five names for `-chain=` and `getblockchaininfo`'s own
# "chain" member, keyed by the network name Chain.name (chains.py)
# carries -- the two vocabularies btclib-org/btclib's own
# `chain_from_network` translates between, kept local rather than
# imported from `bitcoin_core_rpc`: that package is a dependency of
# btclib's fetcher and not one this node declares for itself.
# `testnet4` is missing on both sides -- chains.py has no such `Chain`.
_CORE_CHAIN_NAMES = {
    "mainnet": "main",
    "testnet": "test",
    "signet": "signet",
    "regtest": "regtest",
}


def get_blockchain_info(node: Node, conn: Connection, _: list[Any]) -> dict[str, Any]:
    """Answer `getblockchaininfo` with the one member a caller checks.

    `BitcoinCoreFetcher.assert_network` (btclib) and
    `BitcoinCoreRpcClient.assert_chain` (`bitcoin_core_rpc`) call this
    once before their first fetch, by default, and read `chain` alone --
    proven by asking a real client of a real node here for
    `get_best_block_id` before this callback existed: the very first
    call failed `-32601 Method not found` on `getblockchaininfo`, not on
    the method it asked for. `SigNet` here carries no configurable
    challenge (chains.py's own genesis is the one public signet), so
    the `signet_challenge` member `assert_chain` also reads on that
    chain is not answered.
    """
    return {"chain": _CORE_CHAIN_NAMES[node.chain.name]}


def get_block_hash(node: Node, conn: Connection, params: list[Any]) -> bytes:
    active_chain = node.chainstate.block_index.active_chain

    if not params:
        # the same mechanism get_block_header's own missing-argument
        # case answers with: RPCMethod::HandleRequest throws HelpResult
        # for a call short of a required argument, and ExecuteCommand's
        # `catch (const std::exception& e)` turns that into
        # JSONRPCError(RPC_MISC_ERROR, e.what()), src/rpc/server.cpp
        # :884-886. Unquoted, unlike blockhash's own usage string:
        # RPCArg::ToString(oneline=true) quotes an argument's name only
        # for Type::STR/STR_HEX, and height is Type::NUM
        # (src/rpc/blockchain.cpp:585), which formats bare
        # (src/rpc/util.cpp:1265-1286)
        raise RpcError(RpcErrorCode.MISC_ERROR, "getblockhash height")

    height = params[0]
    if isinstance(height, bool) or not isinstance(height, (int, float)):
        # height is declared RPCArg::Type::NUM (src/rpc/blockchain.cpp
        # :585); RPCMethod::HandleRequest checks a declared argument's
        # JSON type before the handler body runs, src/rpc/util.cpp
        # :653-661 -- a JSON bool is its own VBOOL, not VNUM
        # (src/rpc/util.cpp:878-890), so it is refused here the same
        # way blockhash's own wrong-typed argument is
        raise RpcError(
            RpcErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(height)} is "
            "not of expected type number",
        )
    if isinstance(height, float):
        # a JSON number literal written with a decimal point or
        # exponent is still VNUM, so it passes the check above, but
        # UniValue::getInt<int>()'s std::from_chars fails on it
        # regardless of its value; the std::runtime_error("JSON integer
        # out of range") it throws is ExecuteCommand's generic
        # `catch (const std::exception&)` case, RPC_MISC_ERROR and not
        # RPC_TYPE_ERROR (src/rpc/server.cpp:884-886, src/univalue
        # /include/univalue.h:139-150)
        raise RpcError(RpcErrorCode.MISC_ERROR, "JSON integer out of range")

    if height < 0 or height >= len(active_chain):
        # src/rpc/blockchain.cpp:599-601: one check either direction,
        # and the same message both ways -- height < 0 is what used to
        # read the active chain from its own end instead of raising
        raise RpcError(RpcErrorCode.INVALID_PARAMETER, "Block height out of range")

    return active_chain[height]


def get_block_header(
    node: Node, conn: Connection, params: list[Any]
) -> dict[str, Any] | str:
    block_index = node.chainstate.block_index

    if not params:
        # Core answers a missing required argument with its own help
        # text under RPC_MISC_ERROR: RPCMethod::HandleRequest throws
        # HelpResult for a call short of its required arguments, and
        # ExecuteCommand's `catch (const std::exception& e)` is what
        # turns that into JSONRPCError(RPC_MISC_ERROR, e.what())
        raise RpcError(RpcErrorCode.MISC_ERROR, 'getblockheader "blockhash"')
    if not isinstance(params[0], str):
        # RPCMethod::HandleRequest checks a declared argument's JSON
        # type before the handler body runs at all, src/rpc/util.cpp
        # :653-661 -- blockhash is declared RPCArg::Type::STR_HEX, so a
        # blockhash of any other JSON type never reaches ParseHashV and
        # is refused here the same way, before bytes.fromhex sees it
        raise RpcError(
            RpcErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(params[0])} is "
            "not of expected type string",
        )

    # verbose, src/rpc/blockchain.cpp:617: RPCArg::Type::BOOL,
    # RPCArg::Default{true}. Read and type-checked up front, the same
    # as blockhash above and for the same reason: HandleRequest checks
    # every declared argument's type before any of the handler's own
    # work runs, not only the first one
    verbose = bool_param(params, 1, default=True)

    try:
        block_hash = bytes.fromhex(params[0])
    except ValueError as error:
        # ParseHashV, src/rpc/util.cpp:125, down to the sentence
        raise RpcError(
            RpcErrorCode.INVALID_PARAMETER,
            f"hash must be hexadecimal string (not '{params[0]}')",
        ) from error
    try:
        block_info = block_index.get_block_info(block_hash)
    except KeyError as error:
        # a hash nothing indexed is a question about a block, not a
        # fault of this node: src/rpc/blockchain.cpp:665
        raise RpcError(
            RpcErrorCode.INVALID_ADDRESS_OR_KEY, "Block not found"
        ) from error
    header = block_info.header

    if not verbose:
        # src/rpc/blockchain.cpp:668-673: the same eighty bytes a peer
        # is sent on the wire, hex-encoded rather than the JSON object
        return header.serialize().hex()

    # the blocks this node has validated and connected, which is what
    # Core hands blockheaderToJSON: `ActiveChain().Tip()`, at
    # src/rpc/blockchain.cpp:661
    active_chain = block_index.active_chain

    out: dict[str, Any] = header.to_dict()
    out["hash"] = header.hash

    # the block's own height, which is what Core answers with for a
    # block off the active chain as much as for one on it. `BlockInfo`
    # carries it for every header the index holds, where a position in
    # active_chain is a number only the validated ones have.
    height = block_info.index
    out["height"] = height
    on_active_chain = height < len(active_chain) and active_chain[height] == block_hash

    # Core's ComputeNextBlockAndDepth, src/rpc/blockchain.cpp:126: a
    # depth is counted from the active chain's tip, and a block that
    # chain does not hold at its own height is answered with -1 rather
    # than a number. A header whose block was never downloaded is one of
    # those, so header sync reports nothing as confirmed.
    out["confirmations"] = len(active_chain) - height if on_active_chain else -1
    if height > 0:
        # the header's own parent, which for a block on the active chain
        # is active_chain[height - 1] and for one off it is the fork's
        # ancestor: Core answers with pprev either way
        out["previousblockhash"] = header.previous_block_hash
    # `next` is the active chain's block at height + 1 and only where
    # this block is its parent, which is the same condition read the
    # other way round: nothing follows a block that chain does not hold
    if on_active_chain and height < len(active_chain) - 1:
        out["nextblockhash"] = active_chain[height + 1]
    out["chainwork"] = block_index.chainwork[block_hash]

    return out


def service_names(services: int) -> list[str]:
    """Return the service bits the way Core's getpeerinfo names them.

    `serviceFlagsToStr`, which is a walk over the set bits from the
    least significant up rather than over the names: a bit a member
    names contributes that name without the NODE_ prefix Core's own
    enum carries, and a bit none names contributes "UNKNOWN[2^n]"
    rather than nothing. Core reserves a range of bits for temporary
    experiments and sends everything else through the BIP process, so a
    bit nobody here has heard of is a service and not an error -- and
    dropping it would report a peer as offering less than it said it
    does.
    """
    names: list[str] = []
    for bit in range(int(services).bit_length()):
        if not services >> bit & 1:
            continue
        flag = ServiceFlags(1 << bit)
        names.append(
            f"UNKNOWN[2^{bit}]"
            if flag.name is None
            else flag.name.removeprefix("NODE_")
        )
    return names


def get_peer_info(node: Node, conn: Connection, _: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for id, p2p_conn in node.p2p_manager.connections.items():
        if p2p_conn.status == P2pConnStatus.Connected:
            try:
                addr = p2p_conn.client.getpeername()
                addrbind = p2p_conn.client.getsockname()
            # A peer disconnecting mid-lookup is not worth logging a
            # second time; its own connection state already reports it.
            except Exception:  # noqa: S112
                continue

            # status Connected is only reached after callbacks.verack,
            # which refuses to advance without a version already parsed;
            # cast rather than checked, since nothing here can repair a
            # connection that reached Connected without one
            version_message = cast(Version, p2p_conn.version_message)
            services = version_message.services
            addr_recv = version_message.addr_recv

            conn_dict: dict[str, Any] = {}
            conn_dict["id"] = id
            # Core writes addrbind with `CService::ToStringAddrPort`,
            # and addrlocal from the string `CopyStats` builds with it;
            # its addr is `m_addr_name`, which is that same string only
            # where the peer was not dialled by name. Here addr is
            # `getpeername`'s and never a name, so one formatter serves
            # them all.
            conn_dict["addr"] = ip_and_port(addr[0], addr[1])
            conn_dict["addrbind"] = ip_and_port(addrbind[0], addrbind[1])
            conn_dict["addrlocal"] = ip_and_port(str(addr_recv.ip), addr_recv.port)
            # `.name` and not a lookup that tolerates a bare int: a
            # BIP155 id no member names reaches PeerDB but cannot reach
            # a Connection, `peer_address` building only the two IP
            # networks and `dial` refusing everything but IPv4. An
            # address of a network this node learns to speak has to
            # come through here, which is where that is noticed. Cast
            # rather than asserted: a test double stands in for the
            # enum member here without being one.
            network_id = cast(BIP155Network, p2p_conn.address.network_id)
            conn_dict["network"] = network_id.name.lower()
            conn_dict["lastsend"] = p2p_conn.last_send
            conn_dict["lastrecv"] = p2p_conn.last_receive
            conn_dict["last_block"] = p2p_conn.last_block_timestamp
            conn_dict["pingtime"] = p2p_conn.latency
            conn_dict["version"] = version_message.version
            conn_dict["services"] = f"{services:016x}"
            conn_dict["servicesnames"] = service_names(services)
            conn_dict["inbound"] = p2p_conn.inbound

            out.append(conn_dict)

    return out


def get_connection_count(node: Node, conn: Connection, _: list[Any]) -> int:
    # Core's own `getconnectioncount` counts every entry of `m_nodes`
    # (`CConnman::GetNodeCount`), which holds a socket from the moment
    # it is accepted or dialled -- before its handshake, not only after
    manager = node.p2p_manager
    return len(manager.connections) + len(manager.pending_connections)


def get_mempool_info(node: Node, conn: Connection, _: list[Any]) -> dict[str, Any]:
    mempool = node.mempool
    out = {"loaded": True, "size": mempool.size, "bytes": mempool.bytesize}
    return out


def get_raw_mempool(
    node: Node, conn: Connection, params: list[Any]
) -> dict[str, Any] | list[str]:
    # verbose and mempool_sequence, both RPCArg::Type::BOOL,
    # RPCArg::Default{false}: src/rpc/mempool.cpp:694-695
    verbose = bool_param(params, 0, default=False)
    include_sequence = bool_param(params, 1, default=False)

    if verbose and include_sequence:
        # MempoolToJSON refuses the combination outright rather than
        # answering one and dropping the other: src/rpc/mempool.cpp
        # :608-611
        raise RpcError(
            RpcErrorCode.INVALID_PARAMETER,
            "Verbose results cannot contain mempool sequence values.",
        )

    if verbose:
        return {
            tx.id.hex(): {
                "size": tx.size,
                "vsize": tx.vsize,
                "weight": tx.weight,
                "wtxid": tx.hash.hex(),
            }
            for tx in node.mempool.transactions.values()
        }

    txids = [txid.hex() for txid in node.mempool.txid_index]
    if not include_sequence:
        # MempoolToJSON's plain-array answer, src/rpc/mempool.cpp:624-634
        return txids
    # MempoolToJSON's other shape, src/rpc/mempool.cpp:635-639
    return {"txids": txids, "mempool_sequence": node.mempool.sequence}


def get_raw_transaction(
    node: Node, conn: Connection, params: list[Any]
) -> dict[str, Any] | str:
    """`getrawtransaction`, for a mempool transaction or a named block's.

    No `-txindex` equivalent: this node keeps no lookup from every
    txid it has ever confirmed to the block that holds it, so a
    transaction is answered for exactly the two cases Core itself
    falls back to without one -- the mempool by itself
    (`src/rpc/rawtransaction.cpp:313-314`, `!g_txindex`), and a block
    named explicitly, searched rather than indexed. Both are read-only
    lookups against `block_index` and `block_db`, which already hold
    every validated block for reasons of their own; this adds no store.

    `btclib`'s own `BitcoinCoreFetcher.get_tx` calls this with a txid
    alone, verbosity 0 being its `_call`'s implicit default -- the
    shape it always gets, unconditionally, below.
    """
    if not params:
        # the same shape as getblockheader's own missing-argument case:
        # RPCMethod::HandleRequest's HelpResult, RPC_MISC_ERROR
        # (src/rpc/server.cpp:884-886)
        raise RpcError(
            RpcErrorCode.MISC_ERROR,
            'getrawtransaction "txid" ( verbose ) ( "blockhash" )',
        )
    if not isinstance(params[0], str):
        # txid is declared RPCArg::Type::STR_HEX, type-checked before
        # the handler body runs, same as blockhash above
        raise RpcError(
            RpcErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(params[0])} is "
            "not of expected type string",
        )
    try:
        txid = bytes.fromhex(params[0])
    except ValueError as error:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMETER,
            f"parameter 1 must be hexadecimal string (not '{params[0]}')",
        ) from error

    # Core declares this argument NUM with allow_bool=true
    # (src/rpc/rawtransaction.cpp:286); this node answers only the
    # default and the boolean shape every other verbose flag here
    # already takes, and not Core's 2 -- fee and prevout data come from
    # undo data this node does not keep alongside a block
    verbose = bool_param(params, 1, default=False)

    block_hash: bytes | None = None
    if len(params) > 2 and params[2] is not None:
        if not isinstance(params[2], str):
            raise RpcError(
                RpcErrorCode.TYPE_ERROR,
                f"JSON value of type {json_type_name(params[2])} is "
                "not of expected type string",
            )
        try:
            block_hash = bytes.fromhex(params[2])
        except ValueError as error:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMETER,
                f"parameter 3 must be hexadecimal string (not '{params[2]}')",
            ) from error

    tx: Tx | None
    block_height: int | None = None
    if block_hash is None:
        tx = node.mempool.get_tx(txid)
        if tx is None:
            raise RpcError(
                RpcErrorCode.INVALID_ADDRESS_OR_KEY,
                "No such mempool transaction. This node keeps no "
                "transaction index; name the block it confirmed in to "
                "look there instead. Use gettransaction for wallet "
                "transactions.",
            )
    else:
        try:
            block_info = node.chainstate.block_index.get_block_info(block_hash)
        except KeyError as error:
            raise RpcError(
                RpcErrorCode.INVALID_ADDRESS_OR_KEY, "Block hash not found"
            ) from error
        block = node.block_db.get_block(block_hash)
        if block is None:
            raise RpcError(RpcErrorCode.MISC_ERROR, "Block not available")
        tx = next((t for t in block.transactions if t.id == txid), None)
        if tx is None:
            raise RpcError(
                RpcErrorCode.INVALID_ADDRESS_OR_KEY,
                "No such transaction found in the provided block. Use "
                "gettransaction for wallet transactions.",
            )
        block_height = block_info.index

    if not verbose:
        return tx.serialize(True).hex()

    # to_dict()'s own keys are Core's decoderawtransaction ones (its own
    # docstring), str | int | list -- narrower than this callback's
    # declared return, so the three below need the wider type spelled
    # out, the same as get_block_header's out: dict[str, Any] above it
    out: dict[str, Any] = tx.to_dict()
    out["hex"] = tx.serialize(True).hex()
    if block_hash is not None and block_height is not None:
        active_chain = node.chainstate.block_index.active_chain
        on_active_chain = (
            block_height < len(active_chain)
            and active_chain[block_height] == block_hash
        )
        out["in_active_chain"] = on_active_chain
        out["blockhash"] = block_hash.hex()
        out["confirmations"] = (
            len(active_chain) - block_height if on_active_chain else -1
        )
    return out


# the two reject reasons `verify_mempool_acceptance` can fail with,
# named once so that `test_mempool_accept` and `send_raw_transaction`
# answer the same verdict about the same transaction rather than
# drifting apart the way btclib-org/btclib-node#83 found them
_MISSING_PREVOUTS_REASON = "Missing prevouts"
_INVALID_SCRIPT_REASON = "Invalid signatures or script"
# Core's own reject reason for the same refusal, `TxValidationResult::
# TX_RECONSIDERABLE`/`TX_MEMPOOL_POLICY` invalidated with "mempool
# full" (`validation.cpp`, bitcoin/bitcoin@58a7869f86) once
# `LimitMempoolSize` has run and the transaction just submitted is not
# among what it kept -- `HandleATMPError` (`node/transaction.cpp`, same
# commit) turns that into `TransactionError::MEMPOOL_REJECTED`, and
# `RPCErrorFromTransactionError` (`rpc/util.cpp`) answers it with
# `RPC_TRANSACTION_REJECTED`, which `rpc/protocol.h` declares as a bare
# alias of `RPC_VERIFY_REJECTED` (`-26`) -- the same code this tree's
# own `RpcErrorCode.VERIFY_REJECTED` already answers a transaction the
# mempool refused with, above. btclib-org/btclib-node#293
_MEMPOOL_FULL_REASON = "Mempool is full"


def test_mempool_accept(
    node: Node, conn: Connection, params: list[Any]
) -> list[dict[str, Any]]:
    rawtxs = params[0]
    out: list[dict[str, Any]] = []
    for rawtx in rawtxs:
        try:
            tx = Tx.parse(rawtx)
        except BTClibValueError:
            out.append({"allowed": False, "reject-reason": "Invalid serialization"})
            continue

        tx_res: dict[str, Any] = {
            "txid": tx.id,
            "wtxid": tx.hash,
            "allowed": False,
            "vsize": tx.vsize,
        }
        try:
            verify_mempool_acceptance(node, tx)
            tx_res["allowed"] = True
        except BTClibValueError:
            tx_res["reject-reason"] = _INVALID_SCRIPT_REASON
        except MissingPrevoutError:
            tx_res["reject-reason"] = _MISSING_PREVOUTS_REASON
        except Exception:
            tx_res["reject-reason"] = "Unknown error"
        out.append(tx_res)
    return out


def send_raw_transaction(node: Node, conn: Connection, params: list[Any]) -> str:
    rawtx = params[0]
    if not isinstance(rawtx, str):
        # hexstring is declared RPCArg::Type::STR_HEX
        # (src/rpc/mempool.cpp:72), type-checked before the handler
        # body runs, the same as blockhash and txid above
        raise RpcError(
            RpcErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(rawtx)} is "
            "not of expected type string",
        )
    try:
        tx = Tx.parse(rawtx)
    except BTClibException as error:
        # Core's own RPC_DESERIALIZATION_ERROR, src/rpc/mempool.cpp: a
        # rawtx that never was a transaction, not one the mempool below
        # looked at and refused. Tx.parse raises BTClibValueError for a
        # string it cannot even decode and BTClibRuntimeError for one
        # too short for what it declares -- `BTClibException`, neither
        # itself raised, is the base both share and the one clause this
        # catches them with
        raise RpcError(
            RpcErrorCode.DESERIALIZATION_ERROR,
            "TX decode failed. Make sure the tx has at least one input.",
        ) from error
    try:
        fee = verify_mempool_acceptance(node, tx)
    except MissingPrevoutError as exc:
        # Core's own missing-inputs code, RPC_VERIFY_ERROR
        # (src/rpc/protocol.h): a transaction this node cannot verify
        # for want of what it spends, not one it refuses
        raise RpcError(RpcErrorCode.VERIFY_ERROR, _MISSING_PREVOUTS_REASON) from exc
    except BTClibValueError as exc:
        # Core's own RPC_VERIFY_REJECTED: the mempool looked at the
        # transaction and refused it
        raise RpcError(RpcErrorCode.VERIFY_REJECTED, _INVALID_SCRIPT_REASON) from exc
    # `Mempool.add_tx` now evicts to make room rather than refusing
    # outright past its old `is_full()` gate (btclib-org/btclib-node#294),
    # so whether this call is answered with the refusal below is no
    # longer knowable before making it: a transaction that clears
    # whatever eviction would otherwise remove is kept even into a
    # mempool already at its limit, and one that does not is evicted
    # right back out, `add_tx` answering `False` either way a caller
    # of this method could tell apart before calling it. Answering
    # `tx.id.hex()` regardless of that boolean would tell the caller
    # this transaction was kept when it was not -- the same defect #277
    # fixed on the peer-to-peer path, `p2p/callbacks.py`'s `tx` handler.
    kept = node.mempool.add_tx(tx, fee)
    if kept:
        to_announce = tx
    elif tx.id in node.mempool.txid_index:
        # `add_tx` declined for the other reason it can: this txid is
        # already held, possibly under a different witness -- and
        # therefore a different wtxid -- than what was just resubmitted.
        # Announcing the resubmitted object's own wtxid here, rather
        # than the mempool's, would queue a wtxid nothing holds:
        # `Mempool.add_tx`'s own comment on #277 is the defect this
        # substitution avoids, one call site over. `BroadcastTransaction`
        # (`node/transaction.cpp`, bitcoin/bitcoin@58a7869f86) makes the
        # identical substitution for the identical reason -- "Use the
        # mempool's wtxid for reannouncement" -- rather than
        # reannouncing what was just submitted. The type is wider than
        # the invariant: `get_tx` cannot answer `None` once `txid_index`
        # holds `tx.id`, checked on this very branch, so this is a cast
        # rather than a check dead on every path that reaches it,
        # matching `Connection.send_version`'s own `self.manager.port`.
        # btclib-org/btclib-node#293
        to_announce = cast(Tx, node.mempool.get_tx(tx.id))
    else:
        # Neither already held nor kept: `Mempool._evict_to_limit` ran
        # and took this transaction right back out for being the worst
        # one held once `Mempool.bytesize_limit` was restored -- exactly
        # the case `_MEMPOOL_FULL_REASON`'s own comment names, Core's
        # `TX_RECONSIDERABLE` "mempool full". btclib-org/btclib-node#294
        raise RpcError(RpcErrorCode.VERIFY_REJECTED, _MEMPOOL_FULL_REASON)
    node.p2p_manager.broadcast_raw_transaction(to_announce, fee)
    return tx.id.hex()


def ping(node: Node, conn: Connection, _: list[Any]) -> None:
    node.p2p_manager.ping_all()


def stop(node: Node, conn: Connection, _: list[Any]) -> str:
    return "Btclib node stopping"


callbacks = {
    "getbestblockhash": get_best_block_hash,
    "getblockcount": get_block_count,
    "getblockchaininfo": get_blockchain_info,
    "getblockhash": get_block_hash,
    "getblockheader": get_block_header,
    "getpeerinfo": get_peer_info,
    "getconnectioncount": get_connection_count,
    "getmempoolinfo": get_mempool_info,
    "getrawmempool": get_raw_mempool,
    "getrawtransaction": get_raw_transaction,
    "testmempoolaccept": test_mempool_accept,
    "sendrawtransaction": send_raw_transaction,
    "ping": ping,
    "stop": stop,
}
