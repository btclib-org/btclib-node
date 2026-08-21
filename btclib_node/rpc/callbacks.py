# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib.exceptions import BTClibValueError
from btclib.tx import Tx

from btclib_node.constants import P2pConnStatus, Services
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.main import verify_mempool_acceptance


def get_best_block_hash(node, conn, _):
    return node.chainstate.block_index.active_chain[-1]


def get_block_hash(node, conn, params):
    return node.chainstate.block_index.active_chain[params[0]]


def get_block_header(node, conn, params):
    block_index = node.chainstate.block_index
    header_index = block_index.header_index

    block_hash = bytes.fromhex(params[0])
    block_info = block_index.get_block_info(block_hash)
    header = header = block_info.header
    out = header.to_dict()
    out["hash"] = header.hash

    # TODO: fix if is not in main chain
    height = header_index.index(block_hash)
    out["height"] = height
    out["confirmations"] = len(header_index) - height
    if height > 0:
        out["previousblockhash"] = header_index[height - 1]
    if height < len(header_index) - 1:
        out["nextblockhash"] = header_index[height + 1]
    out["chainwork"] = block_info.chainwork

    return out


def get_peer_info(node, conn, _):
    out = []
    for id, p2p_conn in node.p2p_manager.connections.items():
        if p2p_conn.status == P2pConnStatus.Connected:
            try:
                addr = p2p_conn.client.getpeername()
                addrbind = p2p_conn.client.getsockname()
            # A peer disconnecting mid-lookup is not worth logging a
            # second time; its own connection state already reports it.
            # Carried over from the old bandit `# nosec B112` suppression
            # here; not the moment to change error handling in a
            # lint-gate PR.
            except Exception:  # noqa: S112
                continue

            services = p2p_conn.version_message.services
            servicesnames = [s.name.upper() for s in Services if services & s]

            conn_dict = {}
            conn_dict["id"] = id
            conn_dict["addr"] = f"{addr[0]}:{addr[1]}"
            conn_dict["addrbind"] = f"{addrbind[0]}:{addrbind[1]}"
            conn_dict["addrlocal"] = str(p2p_conn.version_message.addr_recv)
            conn_dict["network"] = p2p_conn.address.netid.name
            conn_dict["lastsend"] = p2p_conn.last_send
            conn_dict["lastrecv"] = p2p_conn.last_receive
            conn_dict["last_block"] = p2p_conn.last_block_timestamp
            conn_dict["pingtime"] = p2p_conn.latency
            conn_dict["version"] = p2p_conn.version_message.version
            conn_dict["services"] = f"{services:016x}"
            conn_dict["servicesnames"] = servicesnames
            conn_dict["inbound"] = p2p_conn.inbound

            out.append(conn_dict)

    return out


def get_connection_count(node, conn, _):
    return len(node.p2p_manager.connections)


def get_mempool_info(node, conn, _):
    mempool = node.mempool
    out = {"loaded": True, "size": mempool.size, "bytes": mempool.bytesize}
    return out


def get_raw_mempool(node, conn, params):
    verbose = params[0] if params else False
    if verbose:
        return {
            tx.id.hex(): {
                "size": tx.size,
                "vsize": tx.vsize,
                "weigth": tx.weight,
                "wtxid": tx.hash.hex(),
            }
            for tx in node.mempool.transactions.values()
        }
    return {"txids": [txid.hex() for txid in node.mempool.txid_index]}


def test_mempool_accept(node, conn, params):
    rawtxs = params[0]
    out = []
    for rawtx in rawtxs:
        try:
            tx = Tx.parse(rawtx)
        except BTClibValueError:
            out.append({"allowed": False, "reject-reason": "Invalid serialization"})
            continue

        tx_res = {"txid": tx.id, "wtxid": tx.hash, "allowed": False, "vsize": tx.vsize}
        try:
            verify_mempool_acceptance(node, tx)
            tx_res["allowed"] = True
            out.append(tx_res)
        except BTClibValueError:
            tx_res["reject-reason"] = "Invalid signatures or script"
        except MissingPrevoutError:
            tx_res["reject-reason"] = "Missing prevouts"
        except Exception:
            tx_res["reject-reason"] = "Unknown error"
        out.append(tx_res)
    return out


def send_raw_transaction(node, conn, params):
    rawtx = params[0]
    try:
        tx = Tx.parse(rawtx)
        try:
            verify_mempool_acceptance(node, tx)
            node.mempool.add_tx(tx)
            node.p2p_manager.broadcast_raw_transaction(tx)
        except BTClibValueError:
            pass
        finally:
            return tx.id.hex()
    except Exception:
        return None


def ping(node, conn, _):
    node.p2p_manager.ping_all()


def stop(node, conn, _):
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
