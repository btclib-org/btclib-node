# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from typing import TYPE_CHECKING, Any, cast

from btclib.exceptions import BTClibValueError
from btclib.p2p.address import ServiceFlags
from btclib.p2p.addrv2 import BIP155Network
from btclib.p2p.handshake import Version
from btclib.tx import Tx

from btclib_node.constants import P2pConnStatus
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.main import verify_mempool_acceptance
from btclib_node.p2p.address import ip_and_port
from btclib_node.rpc.errors import RpcError, RpcErrorCode, json_type_name

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import Connection


def get_best_block_hash(node: Node, conn: Connection, _: list[Any]) -> bytes:
    return node.chainstate.block_index.active_chain[-1]


def get_block_hash(node: Node, conn: Connection, params: list[Any]) -> bytes:
    return node.chainstate.block_index.active_chain[int(params[0])]


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
    verbose = True
    if len(params) > 1 and params[1] is not None:
        if not isinstance(params[1], bool):
            raise RpcError(
                RpcErrorCode.TYPE_ERROR,
                f"JSON value of type {json_type_name(params[1])} is "
                "not of expected type bool",
            )
        verbose = params[1]

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
    out["chainwork"] = block_info.chainwork

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
    return len(node.p2p_manager.connections)


def get_mempool_info(node: Node, conn: Connection, _: list[Any]) -> dict[str, Any]:
    mempool = node.mempool
    out = {"loaded": True, "size": mempool.size, "bytes": mempool.bytesize}
    return out


def get_raw_mempool(node: Node, conn: Connection, params: list[Any]) -> dict[str, Any]:
    verbose = params[0] if params else False
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
    return {"txids": [txid.hex() for txid in node.mempool.txid_index]}


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
            tx_res["reject-reason"] = "Invalid signatures or script"
        except MissingPrevoutError:
            tx_res["reject-reason"] = "Missing prevouts"
        except Exception:
            tx_res["reject-reason"] = "Unknown error"
        out.append(tx_res)
    return out


def send_raw_transaction(node: Node, conn: Connection, params: list[Any]) -> str | None:
    rawtx = params[0]
    try:
        tx = Tx.parse(rawtx)
    except Exception:
        return None
    try:
        verify_mempool_acceptance(node, tx)
        node.mempool.add_tx(tx)
        node.p2p_manager.broadcast_raw_transaction(tx)
    except BTClibValueError:
        # tolerated here alone, and only until #83 decides what a
        # rejected transaction is answered with
        pass
    return tx.id.hex()


def ping(node: Node, conn: Connection, _: list[Any]) -> None:
    node.p2p_manager.ping_all()


def stop(node: Node, conn: Connection, _: list[Any]) -> str:
    return "Btclib node stopping"


callbacks = {
    "getbestblockhash": get_best_block_hash,
    "getblockhash": get_block_hash,
    "getblockheader": get_block_header,
    "getpeerinfo": get_peer_info,
    "getconnectioncount": get_connection_count,
    "getmempoolinfo": get_mempool_info,
    "getrawmempool": get_raw_mempool,
    "testmempoolaccept": test_mempool_accept,
    "sendrawtransaction": send_raw_transaction,
    "ping": ping,
    "stop": stop,
}
