<!-- markdownlint-disable MD041 -->

# Running a node

What `pip install btclib-node` puts on `PATH`, run against each of the
four chains; pointing it at a peer of your own; reading its progress
while it syncs; the RPC methods it answers; what it validates and what
it does not; and what a mainnet sync has actually been measured to
cost. `README.md` and `SECURITY.md` are the other two pages a new
reader wants first — this one is what comes after `pip install`.

## Starting a node

```shell
pip install btclib-node
btclib-node -h
```

`btclib-node -h` lists the options, read the way Bitcoin Core reads its
own — a single dash, `-datadir` rather than `--datadir`, though the
double-dash spelling is accepted too; a value only after `=`,
`-datadir=<dir>` and never `-datadir <dir>`; and `-no<option>` negating
one. `-help-debug` adds the options Core registers for debugging,
`-regtest` among them. One option selects the chain, mainnet being the
default:

```shell
btclib-node
btclib-node -testnet
btclib-node -signet
btclib-node -regtest
```

Data lives under `-datadir=<dir>` (`~/.btclib` if it is not given),
inside a subdirectory named for the chain — `<dir>/mainnet`,
`<dir>/testnet`, `<dir>/signet`, `<dir>/regtest` — so the four chains
never share one store and switching between them on the same
`-datadir` loses nothing.

An operator's existing `bitcoin.conf` is read from the data directory
without being told to, `-conf=<file>` naming another one; a
`[section]` per chain, `main` included, plus one default section that
applies to every chain, is the same shape Core's own reader uses. An
option taking one value takes it from the command line over the file,
except that `-noregtest`, `-notestnet` and `-nosignet` on the command
line are skipped, so the file's `regtest=1` still selects regtest where
`-regtest=0` would not. A list option, such as `-connect`, `-addnode`,
`-rpcauth`, `-rpcwhitelist` or `-debug`, takes its values from the
command line and the file together: `-connect=X` there and `connect=Y`
in the file dials both. Only a negation on the command line drops the
file's values, and not once a value follows it there:
`-noconnect -connect=X` keeps the file's own values after `X`.
`-debug=0` is a value rather than a negation, so it leaves the file's
`debug=` categories on; `-nodebug` turns them off. `-prune` is accepted,
and `## Pruning` below is what any nonzero value actually does.

`-conf=<file>` naming another file while the data directory still holds
a `bitcoin.conf` refuses to start, as `bitcoind` refuses, that file's
settings going unread; `-allowignoredconf` starts it anyway, the refusal
written to stderr as a warning.

On POSIX, `btclib-node` sets its umask to 0077 before anything else, as
`bitcoind` does. Every directory it creates is then 0700 and every file
0600, the chain directory and `history.log` included. A `Node` built in
another program keeps that program's umask.

## Pruning

`-prune=<n>` matches Core's own three-way split. `<n>` of `1` is manual
pruning: nothing is deleted on its own, and the `pruneblockchain` RPC is
what deletes block and undo data up to a height or a timestamp, both
answering the height of the last block actually pruned. `<n>` at or
above `550` is automatic pruning to roughly `<n>` MiB on disk, tracked
against actual bytes under `blocks/` and re-checked after every block
this node connects. Neither ever reaches within the last 288 blocks
(about two days) of the tip -- `getblockchaininfo`'s own `pruned`,
`pruneheight`, `automatic_pruning` and `prune_target_size` answer for
what a caller wants to check programmatically, `pruneheight` being the
first height still on disk. `<n>` from `2` to `549` refuses to start,
Core's own wording -- too small a target to actually run a node on. A
negative `<n>` refuses to start too, rather than silently pruning.

A pruned node tells a peer so: `NODE_NETWORK_LIMITED` on its own
`version`, `NODE_NETWORK` dropped, matching what Core's own pruned node
advertises. `getrawtransaction`'s block-hash lookup and a peer's own
`getdata` both answer for a block this node has since deleted the way
Core's peer does -- `"Block not available (pruned data)"` over RPC, no
answer at all, or the connection dropped outright, over the wire.
`-connect`/`-addnode` still dial whatever is named; a pruned node
serving another pruned node is between the two operators to arrange,
same as it is for Core.

## Pointing it at a peer of your own

`-connect=<ip>[:port]`, repeatable, dials only the peers named this
way. Naming one turns off DNS seeding and every automatically-drawn
outbound connection — Core's own `-connect` interaction — and defaults
`-listen` off too, unless `-listen`/`-nolisten` overrides it
explicitly. `-addnode=<ip>[:port]` dials a peer alongside the ordinary
draw rather than instead of it. Only a literal IP address is accepted
in either; a hostname is refused rather than resolved
([#573](https://github.com/btclib-org/btclib-node/issues/573)).

This is the answer to "I already have the chain on another node, can
this one validate it against that copy instead of the internet":

```shell
btclib-node -regtest -datadir=<dir> -rpcport=<port> \
  -connect=127.0.0.1:<the peer node p2p port>
```

## Reading progress

`getblockchaininfo` answers `headers` (the height of the best header
chain this node knows, downloaded or not) and `blocks` (the height of
the chain it has fully validated) as two separate members, plus
`initialblockdownload`, a boolean matching Core's own definition —
chain work against a minimum and the tip's own age against a
staleness bound, not merely "out of candidates to try right now."
`getblockcount` answers `blocks` alone, so it does not move during
header sync: `headers` is what to watch while the chain of headers
extends ahead of the blocks arriving behind it. `verificationprogress`
is deliberately absent rather than answered wrong — Core estimates it
from a per-chain assumed transaction rate, and neither that assumption
nor the per-block count it is checked against exists in this tree yet
([#575](https://github.com/btclib-org/btclib-node/issues/575), which
added the other members above).

A call carries HTTP Basic credentials, as it does against `bitcoind`.
At start the node writes `.cookie` in its chain's data directory,
`<datadir>/<chain>/` with `<chain>` one of `mainnet`, `testnet`,
`signet` and `regtest`, and deletes it at stop. `-rpccookiefile=<loc>`
names another file, a relative one under that same directory, and
`-norpccookiefile` writes none. Its mode is 0600 on POSIX, and
`-rpccookieperms=group` or `-rpccookieperms=all` makes it 0640 or 0644;
on Windows none of these modes changes anything, and the ACL of the
directory the cookie is in decides who reads it. It holds
`__cookie__:<password>`, which is what `curl` sends:

```shell
curl -s --user "$(cat <datadir>/<chain>/.cookie)" \
  http://127.0.0.1:<rpc port> \
  -d '{"jsonrpc":"2.0","id":"1","method":"getblockchaininfo"}'
```

`-rpcauth=<user>:<salt>$<hash>`, repeatable and read from
`bitcoin.conf` too, adds a user with a password of its own, beside the
cookie: `share/rpcauth/rpcauth.py` in Bitcoin Core's source generates
the line. `-rpcuser=<user>` and `-rpcpassword=<pw>` add a user whose
password is written in plain text, and a set `-rpcpassword` stops the
cookie being written, as it does in Core, which warns about it at start.
A `#` anywhere on an `rpcpassword=` line of `bitcoin.conf` refuses to
start, since it could be the password's or a comment's. A request
without an accepted credential is answered 401, after a quarter of a
second where it carried a wrong one.

`-rpcwhitelist=<user>:<method>,<method>` lets that user call the methods
it names and no others; given twice for one user, the two lists
intersect. A method outside the list is answered 403, and a batch is
refused whole where any call in it is. Once any `-rpcwhitelist` is set,
a user without one is refused every method, unless
`-rpcwhitelistdefault=0`; `-rpcwhitelistdefault=1` refuses every user
without a whitelist even where none is set.

## RPC methods

Fourteen, each mirroring the Core method of the same name:
`getbestblockhash`, `getblockcount`, `getblockchaininfo`,
`getblockhash`, `getblockheader`, `getpeerinfo`, `getconnectioncount`,
`getmempoolinfo`, `getrawmempool`, `getrawtransaction`,
`testmempoolaccept`, `sendrawtransaction`, `ping`, `stop`.

## What is validated, and what is not

Every header's proof of work, and its retarget and median-time-past
against its ancestors; a block's own structure against its difficulty
bound, on receipt; every script and every signature in it; a coinbase
that pays no more than subsidy plus fees and commits to its own height
under BIP34
([#568](https://github.com/btclib-org/btclib-node/issues/568) and
[#571](https://github.com/btclib-org/btclib-node/issues/571)); a spend
of a coinbase not yet `COINBASE_MATURITY` deep
([#569](https://github.com/btclib-org/btclib-node/issues/569)); the
same coinbase landing twice, BIP30
([#570](https://github.com/btclib-org/btclib-node/issues/570)); and a
transaction's own `nLockTime` together with its BIP68 relative lock
([#572](https://github.com/btclib-org/btclib-node/issues/572)). All
five of those were open questions this tracker carried and are now
closed.

Pruning is honoured, as *Pruning* above says. The
UTXO set carries no commitment a caller can audit against
([#639](https://github.com/btclib-org/btclib-node/issues/639)), and
this node answers no `gettxoutsetinfo` of its own.

## What a sync costs

The first initial block download this tree has measured
([#576](https://github.com/btclib-org/btclib-node/issues/576)).
Machine throughout: darwin 25.6.0, ten cores, CPython 3.14.6 (GIL
build), `btclib-secp256k1` installed. The peer for the header sync and
the block-validation figures was a local, fully-synced `bitcoind`
dialled over loopback as the only peer, `-connect`, DNS seeds off —
none of it crossed the internet.

### Header sync

Measured 2026-08-27 at `origin/main`
`8dfd9d82b5520547cf8df3e4c8b9c229264a7889`, load 3.7 and 5.3: 964,357
headers — the peer's own height plus genesis — in 40.1 seconds, twice,
to the tenth of a second, every header's proof of work and retarget
arithmetic checked, no checkpoint and nothing assumed valid.

### Block validation

Measured the same day, `origin/main`
`7da40ebcd606a7d3ef09fe2a49a2d6d63017c8b1`. Six populated blocks up to
mainnet height 964,000, their prevouts pulled from the peer over RPC
and handed to the same call `update_chain` itself makes
(`interpreter.check_transactions`, no download and nothing written):
roughly 40,000 to 65,000 inputs validated a second, at a load around
3.0 — about 200 microseconds per input per core, one libsecp256k1
verification and the script engine around it. A control against the
same block with one prevout's script swapped for another's was
refused. A second range — the emptiest 136,000 blocks from genesis,
downloaded and validated for 750 seconds and stopped by hand — showed
a blocks-per-second rate falling threefold, but it correlates with the
machine's own rising load at -0.68 across that same run and is not
offered here as an independent cost figure; the store measurement
below, taken under a load held level, is what that falling rate was
actually showing.

### The store

Measured 2026-08-28 at `origin/main`
`0f33f69d711169cab8a2431a1cce5a83c65fc65a`, against this node's
`KeyValueStore` filled to a range of sizes and then given one mainnet
block's own reads, deletes and writes — **on the sqlite3 store this
tree used at the time**; the store has been RocksDB since
[#641](https://github.com/btclib-org/btclib-node/issues/641), and this
figure has not been re-measured against it, there being no committed
script in this tree to rerun. Below about four million rows the store
is write-bound and the cost per operation is nearly flat; a cliff sits
between eight and sixteen million rows, where reads and writes both
jump because the working set stops fitting the page cache — measured
at 2.9 to 16.2 microseconds an operation at eight million rows, and
29.3 to 55.0 microseconds at sixteen million. The peer's own
`gettxoutsetinfo` put the real UTXO set at 165,770,908 outputs, about
ten times the largest size measured here, so every figure below is a
floor and not a forecast. Spends were drawn uniformly across the whole
key space, the worst case a chain can present; drawing 90% of them
from the newest 1% of keys — the locality a real chain has and this
measurement otherwise ignores — bought 8 to 15%, not the gap between
this floor and a real cost.

### What the whole chain would cost

A sampled input count — 193 samples of `getblockstats`, mean 3,655.9
inputs a block — puts the chain at roughly 3.5 billion inputs and 7.2
billion store deletes-and-writes. At the input rate above, script
validation alone is on the order of a day, spread across ten cores.
The store is the larger half and it is single-threaded: at the
eight-million-row cost above it comes to about 1.5 days, and at the
sixteen-million-row cost, about 5.8 days — both measured on a store a
tenth the size a real sync's store reaches, so both are floors on the
true figure rather than an estimate of it.
