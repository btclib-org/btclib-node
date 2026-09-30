# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Every served RPC's own Core help text, and `help` itself.

`HELP_TEXT` is `RPCHelpMan::ToString`'s own literal answer for each
method this node serves (`src/rpc/blockchain.cpp`, `src/rpc/net.cpp`,
`src/rpc/mempool.cpp`, `src/rpc/mining.cpp`, `src/rpc/server.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag) -- read back from a regtest
bitcoind v31.1.0's own `help <command>`, the way `disconnectnode`'s own
help text already was before this module existed
(btclib-org/btclib-node#1193). `rpc.main._execute` raises this same text
under `RPC_MISC_ERROR` for a call outside its method's declared argument
count -- `RPCMethod::HandleRequest`'s own `HelpResult`, caught by
`ExecuteCommand` (`src/rpc/server.cpp:874-887`, at
bitcoin/bitcoin@b91d983f66) -- and every
callback in `rpc.callbacks` raises it in place of its own former one-line
usage string for a call short of a required argument or carrying a value
`self.ToString()` refuses in Core, both being the identical
`std::runtime_error(self.ToString())` shape (measured against `addnode`
and `setban`'s own invalid-command refusal, byte for byte). `help_rpc`
below answers it for a command named explicitly, and un-truncated, where
Core's own bare listing keeps only each entry's first line.

`CATEGORY` is each command's own heading in Core's `help`'s bare
listing (`CRPCTable::help`, `src/rpc/server.cpp:69-117`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag), sorted the way that
function sorts `vCommands`: by category, and by
name within it. `help_rpc` groups this node's own served commands
under those same headings and in that same order, the way Core's own
bare listing would if run against a `bitcoind` serving only this
node's own method table -- no method here is hidden, so none is left
out of that listing the way Core leaves a `category == "hidden"` entry
out of its own (`help_rpc`'s own docstring is where that check is
argued absent rather than merely missing).
"""

from typing import Any

from btclib_node.rpc.errors import type_error

__all__ = ["CATEGORY", "HELP_TEXT", "answer_help"]

_HELP_GETBESTBLOCKHASH = (
    "getbestblockhash\n"
    "\n"
    "Returns the hash of the best (tip) block in the most-work fully-validated chain.\n"
    "\n"
    "Result:\n"
    '"hex"    (string) the block hash, hex-encoded\n'
    "\n"
    "Examples:\n"
    "> bitcoin-cli getbestblockhash \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getbestblockhash", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETBLOCK = (
    'getblock "blockhash" ( verbosity )\n'
    "\n"
    "If verbosity is 0, returns a string that is serialized, hex-encoded data for block 'hash'.\n"
    "If verbosity is 1, returns an Object with information about block <hash>.\n"
    "If verbosity is 2, returns an Object with information about block <hash> and information about each transaction.\n"
    "If verbosity is 3, returns an Object with information about block <hash> and information about each transaction, including prevout information for inputs (only for unpruned blocks in the current best chain).\n"
    "\n"
    "Arguments:\n"
    "1. blockhash    (string, required) The block hash\n"
    "2. verbosity    (numeric, optional, default=1) 0 for hex-encoded data, 1 for a JSON object, 2 for JSON object with transaction data, and 3 for JSON object with transaction data including prevout information for inputs\n"
    "\n"
    "Result (for verbosity = 0):\n"
    "\"hex\"    (string) A string that is serialized, hex-encoded data for block 'hash'\n"
    "\n"
    "Result (for verbosity = 1):\n"
    "{                                 (json object)\n"
    '  "hash" : "hex",                 (string) the block hash (same as provided)\n'
    '  "confirmations" : n,            (numeric) The number of confirmations, or -1 if the block is not on the main chain\n'
    '  "size" : n,                     (numeric) The block size\n'
    '  "strippedsize" : n,             (numeric) The block size excluding witness data\n'
    '  "weight" : n,                   (numeric) The block weight as defined in BIP 141\n'
    '  "coinbase_tx" : {               (json object) Coinbase transaction metadata\n'
    '    "version" : n,                (numeric) The coinbase transaction version\n'
    '    "locktime" : n,               (numeric) The coinbase transaction\'s locktime (nLockTime)\n'
    '    "sequence" : n,               (numeric) The coinbase input\'s sequence number (nSequence)\n'
    '    "coinbase" : "hex",           (string) The coinbase input\'s script\n'
    '    "witness" : "hex"             (string, optional) The coinbase input\'s first (and only) witness stack element, if present\n'
    "  },\n"
    '  "height" : n,                   (numeric) The block height or index\n'
    '  "version" : n,                  (numeric) The block version\n'
    '  "versionHex" : "hex",           (string) The block version formatted in hexadecimal\n'
    '  "merkleroot" : "hex",           (string) The merkle root\n'
    '  "tx" : [                        (json array) The transaction ids\n'
    '    "hex",                        (string) The transaction id\n'
    "    ...\n"
    "  ],\n"
    '  "time" : xxx,                   (numeric) The block time expressed in UNIX epoch time\n'
    '  "mediantime" : xxx,             (numeric) The median block time expressed in UNIX epoch time\n'
    '  "nonce" : n,                    (numeric) The nonce\n'
    '  "bits" : "hex",                 (string) nBits: compact representation of the block difficulty target\n'
    '  "target" : "hex",               (string) The difficulty target\n'
    '  "difficulty" : n,               (numeric) The difficulty\n'
    '  "chainwork" : "hex",            (string) Expected number of hashes required to produce the chain up to this block (in hex)\n'
    '  "nTx" : n,                      (numeric) The number of transactions in the block\n'
    '  "previousblockhash" : "hex",    (string, optional) The hash of the previous block (if available)\n'
    '  "nextblockhash" : "hex"         (string, optional) The hash of the next block (if available)\n'
    "}\n"
    "\n"
    "Result (for verbosity = 2):\n"
    "{                   (json object)\n"
    "  ...,              Same output as verbosity = 1\n"
    '  "tx" : [          (json array)\n'
    "    {               (json object)\n"
    '      ...,          The transactions in the format of the getrawtransaction RPC. Different from verbosity = 1 "tx" result\n'
    '      "fee" : n     (numeric, optional) The transaction fee in BTC, omitted if block undo data is not available\n'
    "    },\n"
    "    ...\n"
    "  ]\n"
    "}\n"
    "\n"
    "Result (for verbosity = 3):\n"
    "{                                        (json object)\n"
    "  ...,                                   Same output as verbosity = 2\n"
    '  "tx" : [                               (json array)\n'
    "    {                                    (json object)\n"
    '      "vin" : [                          (json array)\n'
    "        {                                (json object)\n"
    "          ...,                           The same output as verbosity = 2\n"
    '          "prevout" : {                  (json object) (Only if undo information is available)\n'
    '            "generated" : true|false,    (boolean) Coinbase or not\n'
    '            "height" : n,                (numeric) The height of the prevout\n'
    '            "value" : n,                 (numeric) The value in BTC\n'
    '            "scriptPubKey" : {           (json object)\n'
    '              "asm" : "str",             (string) Disassembly of the output script\n'
    '              "desc" : "str",            (string) Inferred descriptor for the output\n'
    '              "hex" : "hex",             (string) The raw output script bytes, hex-encoded\n'
    '              "address" : "str",         (string, optional) The Bitcoin address (only if a well-defined address exists)\n'
    '              "type" : "str"             (string) The type (one of: nonstandard, anchor, pubkey, pubkeyhash, scripthash, multisig, nulldata, witness_v0_scripthash, witness_v0_keyhash, witness_v1_taproot, witness_unknown)\n'
    "            }\n"
    "          }\n"
    "        },\n"
    "        ...\n"
    "      ]\n"
    "    },\n"
    "    ...\n"
    "  ]\n"
    "}\n"
    "\n"
    "Examples:\n"
    '> bitcoin-cli getblock "00000000c937983704a73af28acdec37b049d214adbda81d7e2a3dd146f6ed09"\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getblock", "params": ["00000000c937983704a73af28acdec37b049d214adbda81d7e2a3dd146f6ed09"]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETBLOCKCHAININFO = (
    "getblockchaininfo\n"
    "\n"
    "Returns an object containing various state info regarding blockchain processing.\n"
    "\n"
    "Result:\n"
    "{                                         (json object)\n"
    '  "chain" : "str",                        (string) current network name (main, test, testnet4, signet, regtest)\n'
    '  "blocks" : n,                           (numeric) the height of the most-work fully-validated chain. The genesis block has height 0\n'
    '  "headers" : n,                          (numeric) the current number of headers we have validated\n'
    '  "bestblockhash" : "str",                (string) the hash of the currently best block\n'
    '  "bits" : "hex",                         (string) nBits: compact representation of the block difficulty target\n'
    '  "target" : "hex",                       (string) The difficulty target\n'
    '  "difficulty" : n,                       (numeric) the current difficulty\n'
    '  "time" : xxx,                           (numeric) The block time expressed in UNIX epoch time\n'
    '  "mediantime" : xxx,                     (numeric) The median block time expressed in UNIX epoch time\n'
    '  "verificationprogress" : n,             (numeric) estimate of verification progress [0..1]\n'
    '  "initialblockdownload" : true|false,    (boolean) (debug information) estimate of whether this node is in Initial Block Download mode\n'
    '  "chainwork" : "hex",                    (string) total amount of work in active chain, in hexadecimal\n'
    '  "size_on_disk" : n,                     (numeric) the estimated size of the block and undo files on disk\n'
    '  "pruned" : true|false,                  (boolean) if the blocks are subject to pruning\n'
    '  "pruneheight" : n,                      (numeric, optional) the first block unpruned, all previous blocks were pruned (only present if pruning is enabled)\n'
    '  "automatic_pruning" : true|false,       (boolean, optional) whether automatic pruning is enabled (only present if pruning is enabled)\n'
    '  "prune_target_size" : n,                (numeric, optional) the target size used by pruning (only present if automatic pruning is enabled)\n'
    '  "signet_challenge" : "hex",             (string, optional) the block challenge (aka. block script), in hexadecimal (only present if the current network is a signet)\n'
    '  "warnings" : [                          (json array) any network and blockchain warnings (run with `-deprecatedrpc=warnings` to return the latest warning as a single string)\n'
    '    "str",                                (string) warning\n'
    "    ...\n"
    "  ]\n"
    "}\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli getblockchaininfo \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getblockchaininfo", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETBLOCKCOUNT = (
    "getblockcount\n"
    "\n"
    "Returns the height of the most-work fully-validated chain.\n"
    "The genesis block has height 0.\n"
    "\n"
    "Result:\n"
    "n    (numeric) The current block count\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli getblockcount \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getblockcount", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETBLOCKHASH = (
    "getblockhash height\n"
    "\n"
    "Returns hash of block in best-block-chain at height provided.\n"
    "\n"
    "Arguments:\n"
    "1. height    (numeric, required) The height index\n"
    "\n"
    "Result:\n"
    '"hex"    (string) The block hash\n'
    "\n"
    "Examples:\n"
    "> bitcoin-cli getblockhash 1000\n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getblockhash", "params": [1000]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETBLOCKHEADER = (
    'getblockheader "blockhash" ( verbose )\n'
    "\n"
    "If verbose is false, returns a string that is serialized, hex-encoded data for blockheader 'hash'.\n"
    "If verbose is true, returns an Object with information about blockheader <hash>.\n"
    "\n"
    "Arguments:\n"
    "1. blockhash    (string, required) The block hash\n"
    "2. verbose      (boolean, optional, default=true) true for a json object, false for the hex-encoded data\n"
    "\n"
    "Result (for verbose = true):\n"
    "{                                 (json object)\n"
    '  "hash" : "hex",                 (string) the block hash (same as provided)\n'
    '  "confirmations" : n,            (numeric) The number of confirmations, or -1 if the block is not on the main chain\n'
    '  "height" : n,                   (numeric) The block height or index\n'
    '  "version" : n,                  (numeric) The block version\n'
    '  "versionHex" : "hex",           (string) The block version formatted in hexadecimal\n'
    '  "merkleroot" : "hex",           (string) The merkle root\n'
    '  "time" : xxx,                   (numeric) The block time expressed in UNIX epoch time\n'
    '  "mediantime" : xxx,             (numeric) The median block time expressed in UNIX epoch time\n'
    '  "nonce" : n,                    (numeric) The nonce\n'
    '  "bits" : "hex",                 (string) nBits: compact representation of the block difficulty target\n'
    '  "target" : "hex",               (string) The difficulty target\n'
    '  "difficulty" : n,               (numeric) The difficulty\n'
    '  "chainwork" : "hex",            (string) Expected number of hashes required to produce the current chain\n'
    '  "nTx" : n,                      (numeric) The number of transactions in the block\n'
    '  "previousblockhash" : "hex",    (string, optional) The hash of the previous block (if available)\n'
    '  "nextblockhash" : "hex"         (string, optional) The hash of the next block (if available)\n'
    "}\n"
    "\n"
    "Result (for verbose=false):\n"
    "\"hex\"    (string) A string that is serialized, hex-encoded data for block 'hash'\n"
    "\n"
    "Examples:\n"
    '> bitcoin-cli getblockheader "00000000c937983704a73af28acdec37b049d214adbda81d7e2a3dd146f6ed09"\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getblockheader", "params": ["00000000c937983704a73af28acdec37b049d214adbda81d7e2a3dd146f6ed09"]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETCHAINTIPS = (
    "getchaintips\n"
    "\n"
    "Return information about all known tips in the block tree, including the main chain as well as orphaned branches.\n"
    "\n"
    "Result:\n"
    "[                        (json array)\n"
    "  {                      (json object)\n"
    '    "height" : n,        (numeric) height of the chain tip\n'
    '    "hash" : "hex",      (string) block hash of the tip\n'
    '    "branchlen" : n,     (numeric) zero for main chain, otherwise length of branch connecting the tip to the main chain\n'
    '    "status" : "str"     (string) status of the chain, "active" for the main chain\n'
    "                         Possible values for status:\n"
    '                         1.  "invalid"               This branch contains at least one invalid block\n'
    '                         2.  "headers-only"          Not all blocks for this branch are available, but the headers are valid\n'
    '                         3.  "valid-headers"         All blocks are available for this branch, but they were never fully validated\n'
    '                         4.  "valid-fork"            This branch is not part of the active chain, but is fully validated\n'
    '                         5.  "active"                This is the tip of the active main chain, which is certainly valid\n'
    "  },\n"
    "  ...\n"
    "]\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli getchaintips \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getchaintips", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETMEMPOOLENTRY = (
    'getmempoolentry "txid"\n'
    "\n"
    "Returns mempool data for given transaction\n"
    "\n"
    "Arguments:\n"
    "1. txid    (string, required) The transaction id (must be in mempool)\n"
    "\n"
    "Result:\n"
    "{                                       (json object)\n"
    '  "vsize" : n,                          (numeric) virtual transaction size as defined in BIP 141. This is different from actual serialized size for witness transactions as witness data is discounted.\n'
    '  "weight" : n,                         (numeric) transaction weight as defined in BIP 141.\n'
    '  "time" : xxx,                         (numeric) local time transaction entered pool in seconds since 1 Jan 1970 GMT\n'
    '  "height" : n,                         (numeric) block height when transaction entered pool\n'
    '  "descendantcount" : n,                (numeric) number of in-mempool descendant transactions (including this one)\n'
    '  "descendantsize" : n,                 (numeric) virtual transaction size of in-mempool descendants (including this one)\n'
    '  "ancestorcount" : n,                  (numeric) number of in-mempool ancestor transactions (including this one)\n'
    '  "ancestorsize" : n,                   (numeric) virtual transaction size of in-mempool ancestors (including this one)\n'
    "  \"chunkweight\" : n,                    (numeric) sigops-adjusted weight (as defined in BIP 141 and modified by '-bytespersigop') of this transaction's chunk\n"
    '  "wtxid" : "hex",                      (string) hash of serialized transaction, including witness data\n'
    '  "fees" : {                            (json object)\n'
    '    "base" : n,                         (numeric) transaction fee, denominated in BTC\n'
    '    "modified" : n,                     (numeric) transaction fee with fee deltas used for mining priority, denominated in BTC\n'
    '    "ancestor" : n,                     (numeric) transaction fees of in-mempool ancestors (including this one) with fee deltas used for mining priority, denominated in BTC\n'
    '    "descendant" : n,                   (numeric) transaction fees of in-mempool descendants (including this one) with fee deltas used for mining priority, denominated in BTC\n'
    '    "chunk" : n                         (numeric) transaction fees of chunk, denominated in BTC\n'
    "  },\n"
    '  "depends" : [                         (json array) unconfirmed transactions used as inputs for this transaction\n'
    '    "hex",                              (string) parent transaction id\n'
    "    ...\n"
    "  ],\n"
    '  "spentby" : [                         (json array) unconfirmed transactions spending outputs from this transaction\n'
    '    "hex",                              (string) child transaction id\n'
    "    ...\n"
    "  ],\n"
    '  "bip125-replaceable" : true|false,    (boolean) Whether this transaction signals BIP125 replaceability or has an unconfirmed ancestor signaling BIP125 replaceability. (DEPRECATED)\n'
    "                                        \n"
    '  "unbroadcast" : true|false            (boolean) Whether this transaction is currently unbroadcast (initial broadcast not yet acknowledged by any peers)\n'
    "}\n"
    "\n"
    "Examples:\n"
    '> bitcoin-cli getmempoolentry "mytxid"\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getmempoolentry", "params": ["mytxid"]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETMEMPOOLINFO = (
    "getmempoolinfo\n"
    "\n"
    "Returns details on the active state of the TX memory pool.\n"
    "\n"
    "Result:\n"
    "{                                       (json object)\n"
    '  "loaded" : true|false,                (boolean) True if the initial load attempt of the persisted mempool finished\n'
    '  "size" : n,                           (numeric) Current tx count\n'
    '  "bytes" : n,                          (numeric) Sum of all virtual transaction sizes as defined in BIP 141. Differs from actual serialized size because witness data is discounted\n'
    '  "usage" : n,                          (numeric) Total memory usage for the mempool\n'
    '  "total_fee" : n,                      (numeric) Total fees for the mempool in BTC, ignoring modified fees through prioritisetransaction\n'
    '  "maxmempool" : n,                     (numeric) Maximum memory usage for the mempool\n'
    '  "mempoolminfee" : n,                  (numeric) Minimum fee rate in BTC/kvB for tx to be accepted. Is the maximum of minrelaytxfee and minimum mempool fee\n'
    '  "minrelaytxfee" : n,                  (numeric) Current minimum relay fee for transactions\n'
    '  "incrementalrelayfee" : n,            (numeric) minimum fee rate increment for mempool limiting or replacement in BTC/kvB\n'
    '  "unbroadcastcount" : n,               (numeric) Current number of transactions that haven\'t passed initial broadcast yet\n'
    '  "fullrbf" : true|false,               (boolean) True if the mempool accepts RBF without replaceability signaling inspection (DEPRECATED)\n'
    '  "permitbaremultisig" : true|false,    (boolean) True if the mempool accepts transactions with bare multisig outputs\n'
    '  "maxdatacarriersize" : n,             (numeric) Maximum number of bytes that can be used by OP_RETURN outputs in the mempool\n'
    '  "limitclustercount" : n,              (numeric) Maximum number of transactions that can be in a cluster (configured by -limitclustercount)\n'
    '  "limitclustersize" : n,               (numeric) Maximum size of a cluster in virtual bytes (configured by -limitclustersize)\n'
    '  "optimal" : true|false                (boolean) If the mempool is in a known-optimal transaction ordering\n'
    "}\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli getmempoolinfo \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getmempoolinfo", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETRAWMEMPOOL = (
    "getrawmempool ( verbose mempool_sequence )\n"
    "\n"
    "Returns all transaction ids in memory pool as a json array of string transaction ids.\n"
    "\n"
    "Hint: use getmempoolentry to fetch a specific transaction from the mempool.\n"
    "\n"
    "Arguments:\n"
    "1. verbose             (boolean, optional, default=false) True for a json object, false for array of transaction ids\n"
    "2. mempool_sequence    (boolean, optional, default=false) If verbose=false, returns a json object with transaction list and mempool sequence number attached.\n"
    "\n"
    "Result (for verbose = false):\n"
    "[           (json array)\n"
    '  "hex",    (string) The transaction id\n'
    "  ...\n"
    "]\n"
    "\n"
    "Result (for verbose = true):\n"
    "{                                         (json object)\n"
    '  "transactionid" : {                     (json object)\n'
    '    "vsize" : n,                          (numeric) virtual transaction size as defined in BIP 141. This is different from actual serialized size for witness transactions as witness data is discounted.\n'
    '    "weight" : n,                         (numeric) transaction weight as defined in BIP 141.\n'
    '    "time" : xxx,                         (numeric) local time transaction entered pool in seconds since 1 Jan 1970 GMT\n'
    '    "height" : n,                         (numeric) block height when transaction entered pool\n'
    '    "descendantcount" : n,                (numeric) number of in-mempool descendant transactions (including this one)\n'
    '    "descendantsize" : n,                 (numeric) virtual transaction size of in-mempool descendants (including this one)\n'
    '    "ancestorcount" : n,                  (numeric) number of in-mempool ancestor transactions (including this one)\n'
    '    "ancestorsize" : n,                   (numeric) virtual transaction size of in-mempool ancestors (including this one)\n'
    "    \"chunkweight\" : n,                    (numeric) sigops-adjusted weight (as defined in BIP 141 and modified by '-bytespersigop') of this transaction's chunk\n"
    '    "wtxid" : "hex",                      (string) hash of serialized transaction, including witness data\n'
    '    "fees" : {                            (json object)\n'
    '      "base" : n,                         (numeric) transaction fee, denominated in BTC\n'
    '      "modified" : n,                     (numeric) transaction fee with fee deltas used for mining priority, denominated in BTC\n'
    '      "ancestor" : n,                     (numeric) transaction fees of in-mempool ancestors (including this one) with fee deltas used for mining priority, denominated in BTC\n'
    '      "descendant" : n,                   (numeric) transaction fees of in-mempool descendants (including this one) with fee deltas used for mining priority, denominated in BTC\n'
    '      "chunk" : n                         (numeric) transaction fees of chunk, denominated in BTC\n'
    "    },\n"
    '    "depends" : [                         (json array) unconfirmed transactions used as inputs for this transaction\n'
    '      "hex",                              (string) parent transaction id\n'
    "      ...\n"
    "    ],\n"
    '    "spentby" : [                         (json array) unconfirmed transactions spending outputs from this transaction\n'
    '      "hex",                              (string) child transaction id\n'
    "      ...\n"
    "    ],\n"
    '    "bip125-replaceable" : true|false,    (boolean) Whether this transaction signals BIP125 replaceability or has an unconfirmed ancestor signaling BIP125 replaceability. (DEPRECATED)\n'
    "                                          \n"
    '    "unbroadcast" : true|false            (boolean) Whether this transaction is currently unbroadcast (initial broadcast not yet acknowledged by any peers)\n'
    "  },\n"
    "  ...\n"
    "}\n"
    "\n"
    "Result (for verbose = false and mempool_sequence = true):\n"
    "{                            (json object)\n"
    '  "txids" : [                (json array)\n'
    '    "hex",                   (string) The transaction id\n'
    "    ...\n"
    "  ],\n"
    '  "mempool_sequence" : n     (numeric) The mempool sequence value.\n'
    "}\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli getrawmempool true\n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getrawmempool", "params": [true]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETTXOUT = (
    'gettxout "txid" n ( include_mempool )\n'
    "\n"
    "Returns details about an unspent transaction output.\n"
    "\n"
    "Arguments:\n"
    "1. txid               (string, required) The transaction id\n"
    "2. n                  (numeric, required) vout number\n"
    "3. include_mempool    (boolean, optional, default=true) Whether to include the mempool. Note that an unspent output that is spent in the mempool won't appear.\n"
    "\n"
    "Result (If the UTXO was not found):\n"
    "null    (json null)\n"
    "\n"
    "Result (Otherwise):\n"
    "{                             (json object)\n"
    '  "bestblock" : "hex",        (string) The hash of the block at the tip of the chain\n'
    '  "confirmations" : n,        (numeric) The number of confirmations\n'
    '  "value" : n,                (numeric) The transaction value in BTC\n'
    '  "scriptPubKey" : {          (json object)\n'
    '    "asm" : "str",            (string) Disassembly of the output script\n'
    '    "desc" : "str",           (string) Inferred descriptor for the output\n'
    '    "hex" : "hex",            (string) The raw output script bytes, hex-encoded\n'
    '    "type" : "str",           (string) The type, eg pubkeyhash\n'
    '    "address" : "str"         (string, optional) The Bitcoin address (only if a well-defined address exists)\n'
    "  },\n"
    '  "coinbase" : true|false     (boolean) Coinbase or not\n'
    "}\n"
    "\n"
    "Examples:\n"
    "\n"
    "Get unspent transactions\n"
    "> bitcoin-cli listunspent \n"
    "\n"
    "View the details\n"
    '> bitcoin-cli gettxout "txid" 1\n'
    "\n"
    "As a JSON-RPC call\n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "gettxout", "params": ["txid", 1]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETTXOUTSETINFO = (
    'gettxoutsetinfo ( "hash_type" hash_or_height use_index )\n'
    "\n"
    "Returns statistics about the unspent transaction output set.\n"
    "Note this call may take some time if you are not using coinstatsindex.\n"
    "\n"
    "Arguments:\n"
    "1. hash_type         (string, optional, default=\"hash_serialized_3\") Which UTXO set hash should be calculated. Options: 'hash_serialized_3' (the legacy algorithm), 'muhash', 'none'.\n"
    "2. hash_or_height    (string or numeric, optional, default=the current best block) The block hash or height of the target height (only available with coinstatsindex).\n"
    "3. use_index         (boolean, optional, default=true) Use coinstatsindex, if available.\n"
    "\n"
    "Result:\n"
    "{                                     (json object)\n"
    '  "height" : n,                       (numeric) The block height (index) of the returned statistics\n'
    '  "bestblock" : "hex",                (string) The hash of the block at which these statistics are calculated\n'
    '  "txouts" : n,                       (numeric) The number of unspent transaction outputs\n'
    '  "bogosize" : n,                     (numeric) Database-independent, meaningless metric indicating the UTXO set size\n'
    '  "hash_serialized_3" : "hex",        (string, optional) The serialized hash (only present if \'hash_serialized_3\' hash_type is chosen)\n'
    '  "muhash" : "hex",                   (string, optional) The serialized hash (only present if \'muhash\' hash_type is chosen)\n'
    '  "transactions" : n,                 (numeric, optional) The number of transactions with unspent outputs (not available when coinstatsindex is used)\n'
    '  "disk_size" : n,                    (numeric, optional) The estimated size of the chainstate on disk (not available when coinstatsindex is used)\n'
    '  "total_amount" : n,                 (numeric) The total amount of coins in the UTXO set\n'
    '  "total_unspendable_amount" : n,     (numeric, optional) The total amount of coins permanently excluded from the UTXO set (only available if coinstatsindex is used)\n'
    '  "block_info" : {                    (json object, optional) Info on amounts in the block at this block height (only available if coinstatsindex is used)\n'
    '    "prevout_spent" : n,              (numeric) Total amount of all prevouts spent in this block\n'
    '    "coinbase" : n,                   (numeric) Coinbase subsidy amount of this block\n'
    '    "new_outputs_ex_coinbase" : n,    (numeric) Total amount of new outputs created by this block\n'
    '    "unspendable" : n,                (numeric) Total amount of unspendable outputs created in this block\n'
    '    "unspendables" : {                (json object) Detailed view of the unspendable categories\n'
    '      "genesis_block" : n,            (numeric) The unspendable amount of the Genesis block subsidy\n'
    '      "bip30" : n,                    (numeric) Transactions overridden by duplicates (no longer possible with BIP30)\n'
    '      "scripts" : n,                  (numeric) Amounts sent to scripts that are unspendable (for example OP_RETURN outputs)\n'
    '      "unclaimed_rewards" : n         (numeric) Fee rewards that miners did not claim in their coinbase transaction\n'
    "    }\n"
    "  }\n"
    "}\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli gettxoutsetinfo \n"
    '> bitcoin-cli gettxoutsetinfo "none"\n'
    '> bitcoin-cli gettxoutsetinfo "none" 1000\n'
    '> bitcoin-cli gettxoutsetinfo "none" \'"00000000c937983704a73af28acdec37b049d214adbda81d7e2a3dd146f6ed09"\'\n'
    "> bitcoin-cli -named gettxoutsetinfo hash_type='muhash' use_index='false'\n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "gettxoutsetinfo", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "gettxoutsetinfo", "params": ["none"]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "gettxoutsetinfo", "params": ["none", 1000]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "gettxoutsetinfo", "params": ["none", "00000000c937983704a73af28acdec37b049d214adbda81d7e2a3dd146f6ed09"]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_PRUNEBLOCKCHAIN = (
    "pruneblockchain height\n"
    "\n"
    "Attempts to delete block and undo data up to a specified height or timestamp, if eligible for pruning.\n"
    "Requires `-prune` to be enabled at startup. While pruned data may be re-fetched in some cases (e.g., via `getblockfrompeer`), local deletion is irreversible.\n"
    "\n"
    "Arguments:\n"
    "1. height    (numeric, required) The block height to prune up to. May be set to a discrete height, or to a UNIX epoch time\n"
    "             to prune blocks whose block time is at least 2 hours older than the provided timestamp.\n"
    "\n"
    "Result:\n"
    "n    (numeric) Height of the last block pruned\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli pruneblockchain 1000\n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "pruneblockchain", "params": [1000]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETRPCINFO = (
    "getrpcinfo\n"
    "\n"
    "Returns details of the RPC server.\n"
    "\n"
    "Result:\n"
    "{                          (json object)\n"
    '  "active_commands" : [    (json array) All active commands\n'
    "    {                      (json object) Information about an active command\n"
    '      "method" : "str",    (string) The name of the RPC command\n'
    '      "duration" : n       (numeric) The running time in microseconds\n'
    "    },\n"
    "    ...\n"
    "  ],\n"
    '  "logpath" : "str"        (string) The complete file path to the debug log\n'
    "}\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli getrpcinfo \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getrpcinfo", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_HELP = (
    'help ( "command" )\n'
    "\n"
    "List all commands, or get help for a specified command.\n"
    "\n"
    "Arguments:\n"
    "1. command    (string, optional, default=all commands) The command to get help on\n"
    "\n"
    "Result:\n"
    '"str"    (string) The help text\n'
)

# Core's own `stop()` builds both lines from one `CLIENT_NAME` --
# `static const std::string RESULT{CLIENT_NAME " stopping"}` and
# `"Request a graceful shutdown of " CLIENT_NAME "."`
# (`src/rpc/server.cpp:145-170`, `bitcoin/bitcoin@9be056a8a7`, the
# v31.1 tag). A real bitcoind's own `help stop` therefore answers with
# *its* `CLIENT_NAME`, "Bitcoin Core" -- copying that reading verbatim
# here would describe a string this node's own `stop` never returns.
# `callbacks.stop`'s own literal `"Btclib node stopping"` is what
# stands in its place, `help_test.py`'s own
# `test_stop_help_names_what_stop_actually_returns` tying the two.
_HELP_STOP = (
    "stop\n"
    "\n"
    "Request a graceful shutdown of Btclib node.\n"
    "\n"
    "Result:\n"
    "\"str\"    (string) A string with the content 'Btclib node stopping'\n"
)

_HELP_SUBMITBLOCK = (
    'submitblock "hexdata" ( "dummy" )\n'
    "\n"
    "Attempts to submit new block to network.\n"
    "See https://en.bitcoin.it/wiki/BIP_0022 for full specification.\n"
    "\n"
    "Arguments:\n"
    "1. hexdata    (string, required) the hex-encoded block data to submit\n"
    "2. dummy      (string, optional, default=ignored) dummy value, for compatibility with BIP22. This value is ignored.\n"
    "\n"
    "Result (If the block was accepted):\n"
    "null    (json null)\n"
    "\n"
    "Result (Otherwise):\n"
    '"str"    (string) According to BIP22\n'
    "\n"
    "Examples:\n"
    '> bitcoin-cli submitblock "mydata"\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "submitblock", "params": ["mydata"]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_ADDNODE = (
    'addnode "node" "command" ( v2transport )\n'
    "\n"
    "Attempts to add or remove a node from the addnode list.\n"
    "Or try a connection to a node once.\n"
    "Nodes added using addnode (or -connect) are protected from DoS disconnection and are not required to be\n"
    "full nodes/support SegWit as other outbound peers are (though such peers will not be synced from).\n"
    "Addnode connections are limited to 8 at a time and are counted separately from the -maxconnections limit.\n"
    "\n"
    "Arguments:\n"
    "1. node           (string, required) The IP address/hostname optionally followed by :port of the peer to connect to\n"
    "2. command        (string, required) 'add' to add a node to the list, 'remove' to remove a node from the list, 'onetry' to try a connection to the node once\n"
    "3. v2transport    (boolean, optional, default=set by -v2transport) Attempt to connect using BIP324 v2 transport protocol (ignored for 'remove' command)\n"
    "\n"
    "Result:\n"
    "null    (json null)\n"
    "\n"
    "Examples:\n"
    '> bitcoin-cli addnode "192.168.0.6:8333" "onetry" true\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "addnode", "params": ["192.168.0.6:8333", "onetry" true]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_CLEARBANNED = (
    "clearbanned\n"
    "\n"
    "Clear all banned IPs.\n"
    "\n"
    "Result:\n"
    "null    (json null)\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli clearbanned \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "clearbanned", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_DISCONNECTNODE = (
    'disconnectnode ( "address" nodeid )\n'
    "\n"
    "Immediately disconnects from the specified peer node.\n"
    "\n"
    "Strictly one out of 'address' and 'nodeid' can be provided to identify the node.\n"
    "\n"
    "To disconnect by nodeid, either set 'address' to the empty string, or call using the named 'nodeid' argument only.\n"
    "\n"
    "Arguments:\n"
    "1. address    (string, optional, default=fallback to nodeid) The IP address/port of the node\n"
    "2. nodeid     (numeric, optional, default=fallback to address) The node ID (see getpeerinfo for node IDs)\n"
    "\n"
    "Result:\n"
    "null    (json null)\n"
    "\n"
    "Examples:\n"
    '> bitcoin-cli disconnectnode "192.168.0.6:8333"\n'
    '> bitcoin-cli disconnectnode "" 1\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "disconnectnode", "params": ["192.168.0.6:8333"]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "disconnectnode", "params": ["", 1]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETCONNECTIONCOUNT = (
    "getconnectioncount\n"
    "\n"
    "Returns the number of connections to other nodes.\n"
    "\n"
    "Result:\n"
    "n    (numeric) The connection count\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli getconnectioncount \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getconnectioncount", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETNETWORKINFO = (
    "getnetworkinfo\n"
    "\n"
    "Returns an object containing various state info regarding P2P networking.\n"
    "\n"
    "Result:\n"
    "{                                                    (json object)\n"
    '  "version" : n,                                     (numeric) the server version\n'
    '  "subversion" : "str",                              (string) the server subversion string\n'
    '  "protocolversion" : n,                             (numeric) the protocol version\n'
    '  "localservices" : "hex",                           (string) the services we offer to the network\n'
    '  "localservicesnames" : [                           (json array) the services we offer to the network, in human-readable form\n'
    '    "str",                                           (string) the service name\n'
    "    ...\n"
    "  ],\n"
    '  "localrelay" : true|false,                         (boolean) true if transaction relay is requested from peers\n'
    '  "timeoffset" : n,                                  (numeric) the time offset\n'
    '  "connections" : n,                                 (numeric) the total number of connections\n'
    '  "connections_in" : n,                              (numeric) the number of inbound connections\n'
    '  "connections_out" : n,                             (numeric) the number of outbound connections\n'
    '  "networkactive" : true|false,                      (boolean) whether p2p networking is enabled\n'
    '  "networks" : [                                     (json array) information per network\n'
    "    {                                                (json object)\n"
    '      "name" : "str",                                (string) network (ipv4, ipv6, onion, i2p, cjdns)\n'
    '      "limited" : true|false,                        (boolean) is the network limited using -onlynet?\n'
    '      "reachable" : true|false,                      (boolean) is the network reachable?\n'
    '      "proxy" : "str",                               (string) ("host:port") the proxy that is used for this network, or empty if none\n'
    '      "proxy_randomize_credentials" : true|false     (boolean) Whether randomized credentials are used\n'
    "    },\n"
    "    ...\n"
    "  ],\n"
    '  "relayfee" : n,                                    (numeric) minimum relay fee rate for transactions in BTC/kvB\n'
    '  "incrementalfee" : n,                              (numeric) minimum fee rate increment for mempool limiting or replacement in BTC/kvB\n'
    '  "localaddresses" : [                               (json array) list of local addresses\n'
    "    {                                                (json object)\n"
    '      "address" : "str",                             (string) network address\n'
    '      "port" : n,                                    (numeric) network port\n'
    '      "score" : n                                    (numeric) relative score\n'
    "    },\n"
    "    ...\n"
    "  ],\n"
    '  "warnings" : [                                     (json array) any network and blockchain warnings (run with `-deprecatedrpc=warnings` to return the latest warning as a single string)\n'
    '    "str",                                           (string) warning\n'
    "    ...\n"
    "  ]\n"
    "}\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli getnetworkinfo \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getnetworkinfo", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETPEERINFO = (
    "getpeerinfo\n"
    "\n"
    "Returns data about each connected network peer as a json array of objects.\n"
    "\n"
    "Result:\n"
    "[                                         (json array)\n"
    "  {                                       (json object)\n"
    '    "id" : n,                             (numeric) Peer index\n'
    '    "addr" : "str",                       (string) (host:port) The IP address/hostname optionally followed by :port of the peer\n'
    '    "addrbind" : "str",                   (string, optional) (ip:port) Bind address of the connection to the peer\n'
    '    "addrlocal" : "str",                  (string, optional) (ip:port) Local address as reported by the peer\n'
    '    "network" : "str",                    (string) Network (ipv4, ipv6, onion, i2p, cjdns, not_publicly_routable)\n'
    '    "mapped_as" : n,                      (numeric, optional) Mapped AS (Autonomous System) number at the end of the BGP route to the peer, used for diversifying\n'
    "                                          peer selection (only displayed if the -asmap config option is set)\n"
    '    "services" : "hex",                   (string) The services offered\n'
    '    "servicesnames" : [                   (json array) the services offered, in human-readable form\n'
    '      "str",                              (string) the service name if it is recognised\n'
    "      ...\n"
    "    ],\n"
    '    "relaytxes" : true|false,             (boolean) Whether we relay transactions to this peer\n'
    '    "last_inv_sequence" : n,              (numeric) Mempool sequence number of this peer\'s last INV\n'
    '    "inv_to_send" : n,                    (numeric) How many txs we have queued to announce to this peer\n'
    '    "lastsend" : xxx,                     (numeric) The UNIX epoch time of the last send\n'
    '    "lastrecv" : xxx,                     (numeric) The UNIX epoch time of the last receive\n'
    '    "last_transaction" : xxx,             (numeric) The UNIX epoch time of the last valid transaction received from this peer\n'
    '    "last_block" : xxx,                   (numeric) The UNIX epoch time of the last block received from this peer\n'
    '    "bytessent" : n,                      (numeric) The total bytes sent\n'
    '    "bytesrecv" : n,                      (numeric) The total bytes received\n'
    '    "conntime" : xxx,                     (numeric) The UNIX epoch time of the connection\n'
    '    "timeoffset" : n,                     (numeric) The time offset in seconds\n'
    '    "pingtime" : n,                       (numeric, optional) The last ping time in seconds, if any\n'
    '    "minping" : n,                        (numeric, optional) The minimum observed ping time in seconds, if any\n'
    '    "pingwait" : n,                       (numeric, optional) The duration in seconds of an outstanding ping (if non-zero)\n'
    '    "version" : n,                        (numeric) The peer version, such as 70001\n'
    '    "subver" : "str",                     (string) The string version\n'
    '    "inbound" : true|false,               (boolean) Inbound (true) or Outbound (false)\n'
    '    "bip152_hb_to" : true|false,          (boolean) Whether we selected peer as (compact blocks) high-bandwidth peer\n'
    '    "bip152_hb_from" : true|false,        (boolean) Whether peer selected us as (compact blocks) high-bandwidth peer\n'
    '    "startingheight" : n,                 (numeric, optional) (DEPRECATED, returned only if config option -deprecatedrpc=startingheight is passed) The starting height (block) of the peer\n'
    '    "presynced_headers" : n,              (numeric) The current height of header pre-synchronization with this peer, or -1 if no low-work sync is in progress\n'
    '    "synced_headers" : n,                 (numeric) The last header we have in common with this peer\n'
    '    "synced_blocks" : n,                  (numeric) The last block we have in common with this peer\n'
    '    "inflight" : [                        (json array)\n'
    "      n,                                  (numeric) The heights of blocks we're currently asking from this peer\n"
    "      ...\n"
    "    ],\n"
    '    "addr_relay_enabled" : true|false,    (boolean) Whether we participate in address relay with this peer\n'
    '    "addr_processed" : n,                 (numeric) The total number of addresses processed, excluding those dropped due to rate limiting\n'
    '    "addr_rate_limited" : n,              (numeric) The total number of addresses dropped due to rate limiting\n'
    '    "permissions" : [                     (json array) Any special permissions that have been granted to this peer\n'
    '      "str",                              (string) bloomfilter (allow requesting BIP37 filtered blocks and transactions),\n'
    "                                          noban (do not ban for misbehavior; implies download),\n"
    "                                          forcerelay (relay transactions that are already in the mempool; implies relay),\n"
    "                                          relay (relay even in -blocksonly mode, and unlimited transaction announcements),\n"
    "                                          mempool (allow requesting BIP35 mempool contents),\n"
    "                                          download (allow getheaders during IBD, no disconnect after maxuploadtarget limit),\n"
    "                                          addr (responses to GETADDR avoid hitting the cache and contain random records with the most up-to-date info).\n"
    "                                          \n"
    "      ...\n"
    "    ],\n"
    '    "minfeefilter" : n,                   (numeric) The minimum fee rate for transactions this peer accepts\n'
    '    "bytessent_per_msg" : {               (json object)\n'
    '      "msg" : n,                          (numeric) The total bytes sent aggregated by message type\n'
    "                                          When a message type is not listed in this json object, the bytes sent are 0.\n"
    "                                          Only known message types can appear as keys in the object.\n"
    "      ...\n"
    "    },\n"
    '    "bytesrecv_per_msg" : {               (json object)\n'
    '      "msg" : n,                          (numeric) The total bytes received aggregated by message type\n'
    "                                          When a message type is not listed in this json object, the bytes received are 0.\n"
    "                                          Only known message types can appear as keys in the object and all bytes received\n"
    "                                          of unknown message types are listed under '*other*'.\n"
    "      ...\n"
    "    },\n"
    '    "connection_type" : "str",            (string) Type of connection: \n'
    "                                          outbound-full-relay (default automatic connections),\n"
    "                                          block-relay-only (does not relay transactions or addresses),\n"
    "                                          inbound (initiated by the peer),\n"
    "                                          manual (added via addnode RPC or -addnode/-connect configuration options),\n"
    "                                          addr-fetch (short-lived automatic connection for soliciting addresses),\n"
    "                                          feeler (short-lived automatic connection for testing addresses),\n"
    "                                          private-broadcast (short-lived automatic connection for broadcasting privacy-sensitive transactions).\n"
    "                                          Please note this output is unlikely to be stable in upcoming releases as we iterate to\n"
    "                                          best capture connection behaviors.\n"
    '    "transport_protocol_type" : "str",    (string) Type of transport protocol: \n'
    "                                          detecting (peer could be v1 or v2),\n"
    "                                          v1 (plaintext transport protocol),\n"
    "                                          v2 (BIP324 encrypted transport protocol).\n"
    "                                          \n"
    '    "session_id" : "str"                  (string) The session ID for this connection, or "" if there is none ("v2" transport protocol only).\n'
    "                                          \n"
    "  },\n"
    "  ...\n"
    "]\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli getpeerinfo \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getpeerinfo", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_LISTBANNED = (
    "listbanned\n"
    "\n"
    "List all manually banned IPs/Subnets.\n"
    "\n"
    "Result:\n"
    "[                              (json array)\n"
    "  {                            (json object)\n"
    '    "address" : "str",         (string) The IP/Subnet of the banned node\n'
    '    "ban_created" : xxx,       (numeric) The UNIX epoch time the ban was created\n'
    '    "banned_until" : xxx,      (numeric) The UNIX epoch time the ban expires\n'
    '    "ban_duration" : xxx,      (numeric) The ban duration, in seconds\n'
    '    "time_remaining" : xxx     (numeric) The time remaining until the ban expires, in seconds\n'
    "  },\n"
    "  ...\n"
    "]\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli listbanned \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "listbanned", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_PING = (
    "ping\n"
    "\n"
    "Requests that a ping be sent to all other nodes, to measure ping time.\n"
    "Results are provided in getpeerinfo.\n"
    "Ping command is handled in queue with all other commands, so it measures processing backlog, not just network ping.\n"
    "\n"
    "Result:\n"
    "null    (json null)\n"
    "\n"
    "Examples:\n"
    "> bitcoin-cli ping \n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "ping", "params": []}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_SETBAN = (
    'setban "subnet" "command" ( bantime absolute )\n'
    "\n"
    "Attempts to add or remove an IP/Subnet from the banned list.\n"
    "\n"
    "Arguments:\n"
    "1. subnet      (string, required) The IP/Subnet (see getpeerinfo for nodes IP) with an optional netmask (default is /32 = single IP)\n"
    "2. command     (string, required) 'add' to add an IP/Subnet to the list, 'remove' to remove an IP/Subnet from the list\n"
    "3. bantime     (numeric, optional, default=0) time in seconds how long (or until when if [absolute] is set) the IP is banned (0 or empty means using the default time of 24h which can also be overwritten by the -bantime startup argument)\n"
    "4. absolute    (boolean, optional, default=false) If set, the bantime must be an absolute timestamp expressed in UNIX epoch time\n"
    "\n"
    "Result:\n"
    "null    (json null)\n"
    "\n"
    "Examples:\n"
    '> bitcoin-cli setban "192.168.0.6" "add" 86400\n'
    '> bitcoin-cli setban "192.168.0.0/24" "add"\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "setban", "params": ["192.168.0.6", "add", 86400]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_GETRAWTRANSACTION = (
    'getrawtransaction "txid" ( verbosity "blockhash" )\n'
    "\n"
    "By default, this call only returns a transaction if it is in the mempool. If -txindex is enabled\n"
    "and no blockhash argument is passed, it will return the transaction if it is in the mempool or any block.\n"
    "If a blockhash argument is passed, it will return the transaction if\n"
    "the specified block is available and the transaction is in that block.\n"
    "\n"
    "Hint: Use gettransaction for wallet transactions.\n"
    "\n"
    "If verbosity is 0 or omitted, returns the serialized transaction as a hex-encoded string.\n"
    "If verbosity is 1, returns a JSON Object with information about the transaction.\n"
    "If verbosity is 2, returns a JSON Object with information about the transaction, including fee and prevout information.\n"
    "\n"
    "Arguments:\n"
    "1. txid         (string, required) The transaction id\n"
    "2. verbosity    (numeric, optional, default=0) 0 for hex-encoded data, 1 for a JSON object, and 2 for JSON object with fee and prevout\n"
    "3. blockhash    (string, optional) The block in which to look for the transaction\n"
    "\n"
    "Result (if verbosity is not set or set to 0):\n"
    "\"str\"    (string) The serialized transaction as a hex-encoded string for 'txid'\n"
    "\n"
    "Result (if verbosity is set to 1):\n"
    "{                                    (json object)\n"
    '  "in_active_chain" : true|false,    (boolean, optional) Whether specified block is in the active chain or not (only present with explicit "blockhash" argument)\n'
    '  "blockhash" : "hex",               (string, optional) the block hash\n'
    '  "confirmations" : n,               (numeric, optional) The confirmations\n'
    '  "blocktime" : xxx,                 (numeric, optional) The block time expressed in UNIX epoch time\n'
    '  "time" : n,                        (numeric, optional) Same as "blocktime"\n'
    '  "hex" : "hex",                     (string) The serialized, hex-encoded data for \'txid\'\n'
    '  "txid" : "hex",                    (string) The transaction id (same as provided)\n'
    '  "hash" : "hex",                    (string) The transaction hash (differs from txid for witness transactions)\n'
    '  "size" : n,                        (numeric) The serialized transaction size\n'
    '  "vsize" : n,                       (numeric) The virtual transaction size (differs from size for witness transactions)\n'
    '  "weight" : n,                      (numeric) The transaction\'s weight (between vsize*4-3 and vsize*4)\n'
    '  "version" : n,                     (numeric) The version\n'
    '  "locktime" : xxx,                  (numeric) The lock time\n'
    '  "vin" : [                          (json array)\n'
    "    {                                (json object)\n"
    '      "coinbase" : "hex",            (string, optional) The coinbase value (only if coinbase transaction)\n'
    '      "txid" : "hex",                (string, optional) The transaction id (if not coinbase transaction)\n'
    '      "vout" : n,                    (numeric, optional) The output number (if not coinbase transaction)\n'
    '      "scriptSig" : {                (json object, optional) The script (if not coinbase transaction)\n'
    '        "asm" : "str",               (string) Disassembly of the signature script\n'
    '        "hex" : "hex"                (string) The raw signature script bytes, hex-encoded\n'
    "      },\n"
    '      "txinwitness" : [              (json array, optional)\n'
    '        "hex",                       (string) hex-encoded witness data (if any)\n'
    "        ...\n"
    "      ],\n"
    '      "sequence" : n                 (numeric) The script sequence number\n'
    "    },\n"
    "    ...\n"
    "  ],\n"
    '  "vout" : [                         (json array)\n'
    "    {                                (json object)\n"
    '      "value" : n,                   (numeric) The value in BTC\n'
    '      "n" : n,                       (numeric) index\n'
    '      "scriptPubKey" : {             (json object)\n'
    '        "asm" : "str",               (string) Disassembly of the output script\n'
    '        "desc" : "str",              (string) Inferred descriptor for the output\n'
    '        "hex" : "hex",               (string) The raw output script bytes, hex-encoded\n'
    '        "address" : "str",           (string, optional) The Bitcoin address (only if a well-defined address exists)\n'
    '        "type" : "str"               (string) The type (one of: nonstandard, anchor, pubkey, pubkeyhash, scripthash, multisig, nulldata, witness_v0_scripthash, witness_v0_keyhash, witness_v1_taproot, witness_unknown)\n'
    "      }\n"
    "    },\n"
    "    ...\n"
    "  ]\n"
    "}\n"
    "\n"
    "Result (for verbosity = 2):\n"
    "{                                    (json object)\n"
    "  ...,                               Same output as verbosity = 1\n"
    '  "fee" : n,                         (numeric, optional) transaction fee in BTC, omitted if block undo data is not available\n'
    '  "vin" : [                          (json array)\n'
    "    {                                (json object) utxo being spent\n"
    "      ...,                           Same output as verbosity = 1\n"
    '      "prevout" : {                  (json object, optional) The previous output, omitted if block undo data is not available\n'
    '        "generated" : true|false,    (boolean) Coinbase or not\n'
    '        "height" : n,                (numeric) The height of the prevout\n'
    '        "value" : n,                 (numeric) The value in BTC\n'
    '        "scriptPubKey" : {           (json object)\n'
    '          "asm" : "str",             (string) Disassembly of the output script\n'
    '          "desc" : "str",            (string) Inferred descriptor for the output\n'
    '          "hex" : "hex",             (string) The raw output script bytes, hex-encoded\n'
    '          "address" : "str",         (string, optional) The Bitcoin address (only if a well-defined address exists)\n'
    '          "type" : "str"             (string) The type (one of: nonstandard, anchor, pubkey, pubkeyhash, scripthash, multisig, nulldata, witness_v0_scripthash, witness_v0_keyhash, witness_v1_taproot, witness_unknown)\n'
    "        }\n"
    "      }\n"
    "    },\n"
    "    ...\n"
    "  ]\n"
    "}\n"
    "\n"
    "Examples:\n"
    '> bitcoin-cli getrawtransaction "mytxid"\n'
    '> bitcoin-cli getrawtransaction "mytxid" 1\n'
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "getrawtransaction", "params": ["mytxid", 1]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
    '> bitcoin-cli getrawtransaction "mytxid" 0 "myblockhash"\n'
    '> bitcoin-cli getrawtransaction "mytxid" 1 "myblockhash"\n'
    '> bitcoin-cli getrawtransaction "mytxid" 2 "myblockhash"\n'
)

_HELP_SENDRAWTRANSACTION = (
    'sendrawtransaction "hexstring" ( maxfeerate maxburnamount )\n'
    "\n"
    "Submit a raw transaction (serialized, hex-encoded) to the network.\n"
    "\n"
    "If -privatebroadcast is disabled, then the transaction will be put into the\n"
    "local mempool of the node and will be sent unconditionally to all currently\n"
    "connected peers, so using sendrawtransaction for manual rebroadcast will degrade\n"
    "privacy by leaking the transaction's origin, as nodes will normally not\n"
    "rebroadcast non-wallet transactions already in their mempool.\n"
    "\n"
    "If -privatebroadcast is enabled, then the transaction will be sent only via\n"
    "dedicated, short-lived connections to Tor or I2P peers or IPv4/IPv6 peers\n"
    "via the Tor network. This conceals the transaction's origin. The transaction\n"
    "will only enter the local mempool when it is received back from the network.\n"
    "\n"
    "A specific exception, RPC_TRANSACTION_ALREADY_IN_UTXO_SET, may throw if the transaction cannot be added to the mempool.\n"
    "\n"
    "Related RPCs: createrawtransaction, signrawtransactionwithkey\n"
    "\n"
    "Arguments:\n"
    "1. hexstring        (string, required) The hex string of the raw transaction\n"
    '2. maxfeerate       (numeric or string, optional, default="0.10") Reject transactions whose fee rate is higher than the specified value, expressed in BTC/kvB.\n'
    "                    Fee rates larger than 1BTC/kvB are rejected.\n"
    "                    Set to 0 to accept any fee rate.\n"
    "3. maxburnamount    (numeric or string, optional, default=\"0.00\") Reject transactions with provably unspendable outputs (e.g. 'datacarrier' outputs that use the OP_RETURN opcode) greater than the specified value, expressed in BTC.\n"
    "                    If burning funds through unspendable outputs is desired, increase this value.\n"
    "                    This check is based on heuristics and does not guarantee spendability of outputs.\n"
    "                    \n"
    "\n"
    "Result:\n"
    '"hex"    (string) The transaction hash in hex\n'
    "\n"
    "Examples:\n"
    "\n"
    "Create a transaction\n"
    '> bitcoin-cli createrawtransaction "[{\\"txid\\" : \\"mytxid\\",\\"vout\\":0}]" "{\\"myaddress\\":0.01}"\n'
    "Sign the transaction, and get back the hex\n"
    '> bitcoin-cli signrawtransactionwithwallet "myhex"\n'
    "\n"
    "Send the transaction (signed hex)\n"
    '> bitcoin-cli sendrawtransaction "signedhex"\n'
    "\n"
    "As a JSON-RPC call\n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "sendrawtransaction", "params": ["signedhex"]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

_HELP_TESTMEMPOOLACCEPT = (
    'testmempoolaccept ["rawtx",...] ( maxfeerate )\n'
    "\n"
    "Returns result of mempool acceptance tests indicating if raw transaction(s) (serialized, hex-encoded) would be accepted by mempool.\n"
    "\n"
    "If multiple transactions are passed in, parents must come before children and package policies apply: the transactions cannot conflict with any mempool transactions or each other.\n"
    "\n"
    "If one transaction fails, other transactions may not be fully validated (the 'allowed' key will be blank).\n"
    "\n"
    "The maximum number of transactions allowed is 25.\n"
    "\n"
    "This checks if transactions violate the consensus or policy rules.\n"
    "\n"
    "See sendrawtransaction call.\n"
    "\n"
    "Arguments:\n"
    "1. rawtxs          (json array, required) An array of hex strings of raw transactions.\n"
    "     [\n"
    '       "rawtx",    (string)\n'
    "       ...\n"
    "     ]\n"
    '2. maxfeerate      (numeric or string, optional, default="0.10") Reject transactions whose fee rate is higher than the specified value, expressed in BTC/kvB.\n'
    "                   Fee rates larger than 1BTC/kvB are rejected.\n"
    "                   Set to 0 to accept any fee rate.\n"
    "\n"
    "Result:\n"
    "[                                 (json array) The result of the mempool acceptance test for each raw transaction in the input array.\n"
    "                                  Returns results for each transaction in the same order they were passed in.\n"
    "                                  Transactions that cannot be fully validated due to failures in other transactions will not contain an 'allowed' result.\n"
    "                                  \n"
    "  {                               (json object)\n"
    '    "txid" : "hex",               (string) The transaction hash in hex\n'
    '    "wtxid" : "hex",              (string) The transaction witness hash in hex\n'
    '    "package-error" : "str",      (string, optional) Package validation error, if any (only possible if rawtxs had more than 1 transaction).\n'
    '    "allowed" : true|false,       (boolean, optional) Whether this tx would be accepted to the mempool and pass client-specified maxfeerate. If not present, the tx was not fully validated due to a failure in another tx in the list.\n'
    "    \"vsize\" : n,                  (numeric, optional) Virtual transaction size as defined in BIP 141. This is different from actual serialized size for witness transactions as witness data is discounted (only present when 'allowed' is true)\n"
    "    \"fees\" : {                    (json object, optional) Transaction fees (only present if 'allowed' is true)\n"
    '      "base" : n,                 (numeric) transaction fee in BTC\n'
    '      "effective-feerate" : n,    (numeric) the effective feerate in BTC per KvB. May differ from the base feerate if, for example, there are modified fees from prioritisetransaction or a package feerate was used.\n'
    '      "effective-includes" : [    (json array) transactions whose fees and vsizes are included in effective-feerate.\n'
    '        "hex",                    (string) transaction wtxid in hex\n'
    "        ...\n"
    "      ]\n"
    "    },\n"
    '    "reject-reason" : "str",      (string, optional) Rejection reason (only present when \'allowed\' is false)\n'
    '    "reject-details" : "str"      (string, optional) Rejection details (only present when \'allowed\' is false and rejection details exist)\n'
    "  },\n"
    "  ...\n"
    "]\n"
    "\n"
    "Examples:\n"
    "\n"
    "Create a transaction\n"
    '> bitcoin-cli createrawtransaction "[{\\"txid\\" : \\"mytxid\\",\\"vout\\":0}]" "{\\"myaddress\\":0.01}"\n'
    "Sign the transaction, and get back the hex\n"
    '> bitcoin-cli signrawtransactionwithwallet "myhex"\n'
    "\n"
    "Test acceptance of the transaction (signed hex)\n"
    "> bitcoin-cli testmempoolaccept '[\"signedhex\"]'\n"
    "\n"
    "As a JSON-RPC call\n"
    '> curl --user myusername --data-binary \'{"jsonrpc": "2.0", "id": "curltest", "method": "testmempoolaccept", "params": [["signedhex"]]}\' -H \'content-type: application/json\' http://127.0.0.1:8332/\n'
)

# Core's own per-method help text, keyed by RPC name -- `arg_names`'s own
# keys in `rpc.callbacks`, checked against them by
# `tests/unit/rpc/help_test.py`.
HELP_TEXT: dict[str, str] = {
    "getbestblockhash": _HELP_GETBESTBLOCKHASH,
    "getblock": _HELP_GETBLOCK,
    "getblockchaininfo": _HELP_GETBLOCKCHAININFO,
    "getblockcount": _HELP_GETBLOCKCOUNT,
    "getblockhash": _HELP_GETBLOCKHASH,
    "getblockheader": _HELP_GETBLOCKHEADER,
    "getchaintips": _HELP_GETCHAINTIPS,
    "getmempoolentry": _HELP_GETMEMPOOLENTRY,
    "getmempoolinfo": _HELP_GETMEMPOOLINFO,
    "getrawmempool": _HELP_GETRAWMEMPOOL,
    "gettxout": _HELP_GETTXOUT,
    "gettxoutsetinfo": _HELP_GETTXOUTSETINFO,
    "pruneblockchain": _HELP_PRUNEBLOCKCHAIN,
    "help": _HELP_HELP,
    "stop": _HELP_STOP,
    "getrpcinfo": _HELP_GETRPCINFO,
    "submitblock": _HELP_SUBMITBLOCK,
    "addnode": _HELP_ADDNODE,
    "clearbanned": _HELP_CLEARBANNED,
    "disconnectnode": _HELP_DISCONNECTNODE,
    "getconnectioncount": _HELP_GETCONNECTIONCOUNT,
    "getnetworkinfo": _HELP_GETNETWORKINFO,
    "getpeerinfo": _HELP_GETPEERINFO,
    "listbanned": _HELP_LISTBANNED,
    "ping": _HELP_PING,
    "setban": _HELP_SETBAN,
    "getrawtransaction": _HELP_GETRAWTRANSACTION,
    "sendrawtransaction": _HELP_SENDRAWTRANSACTION,
    "testmempoolaccept": _HELP_TESTMEMPOOLACCEPT,
}

# Each command's own category, Core's own `RPCMethod`'s registration
# argument (`src/rpc/*.cpp`), read off a real `bitcoind`'s own `help`
# bare listing rather than the source, since that argument is not part
# of what `help <command>` itself ever prints.
CATEGORY: dict[str, str] = {
    "getbestblockhash": "Blockchain",
    "getblock": "Blockchain",
    "getblockchaininfo": "Blockchain",
    "getblockcount": "Blockchain",
    "getblockhash": "Blockchain",
    "getblockheader": "Blockchain",
    "getchaintips": "Blockchain",
    "getmempoolentry": "Blockchain",
    "getmempoolinfo": "Blockchain",
    "getrawmempool": "Blockchain",
    "gettxout": "Blockchain",
    "gettxoutsetinfo": "Blockchain",
    "pruneblockchain": "Blockchain",
    "help": "Control",
    "stop": "Control",
    "getrpcinfo": "Control",
    "submitblock": "Mining",
    "addnode": "Network",
    "clearbanned": "Network",
    "disconnectnode": "Network",
    "getconnectioncount": "Network",
    "getnetworkinfo": "Network",
    "getpeerinfo": "Network",
    "listbanned": "Network",
    "ping": "Network",
    "setban": "Network",
    "getrawtransaction": "Rawtransactions",
    "sendrawtransaction": "Rawtransactions",
    "testmempoolaccept": "Rawtransactions",
}

# `CRPCTable::help`'s own sort key, `category + name`
# (`src/rpc/server.cpp:78-79`, at bitcoin/bitcoin@9be056a8a7, the
# v31.1 tag) -- category first, alphabetically, and
# name within it -- rebuilt here from `CATEGORY` and `HELP_TEXT` rather
# than written out by hand a second time, which is what would go stale
# the day a served method's category changes and this list does not.
_BARE_LISTING = "\n\n".join(
    "== {} ==\n{}".format(
        category,
        "\n".join(
            HELP_TEXT[name].split("\n", 1)[0]
            for name in sorted(
                (n for n, c in CATEGORY.items() if c == category), key=str.lower
            )
        ),
    )
    for category in sorted(set(CATEGORY.values()))
)


def answer_help(params: list[Any]) -> str:
    r"""Answer `help`: every served command's usage, or one command's own.

    With no argument (or an empty string, its own declared default),
    Core's own bare listing (`CRPCTable::help`, `src/rpc/server.cpp`,
    same tag), grouped by category and truncated to each entry's own
    usage line the way that function's own `strHelp.substr(0,
    strHelp.find('\n'))` truncates it -- restricted to the commands
    `rpc.callbacks.callbacks` actually serves, none of them hidden, so
    the `category == "hidden"` skip that function's own loop condition
    carries is nothing to reproduce here (`CATEGORY`'s own module
    docstring). With a command's name, that command's own untruncated
    help, or, for a name nothing here serves, Core's own literal
    `"help: unknown command: %s"` (`src/rpc/server.cpp:114`) -- a
    successful reply, not a refusal, matching Core answering it as
    `RPCResult::Type::STR` rather than raising. `rpc.callbacks.help_rpc`
    is the shared-signature wrapper `handle_rpc` actually dispatches to,
    this function taking `params` alone since it reads no node state.
    """
    command = params[0] if params and params[0] is not None else ""
    if not isinstance(command, str):
        raise type_error(1, "command", command, "string")
    if not command:
        return _BARE_LISTING
    help_text = HELP_TEXT.get(command)
    if help_text is None:
        return f"help: unknown command: {command}"
    return help_text.rstrip("\n")
