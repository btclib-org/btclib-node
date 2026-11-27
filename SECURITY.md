# Security policy

## Reporting a vulnerability

If you have found a security vulnerability, please do not open a GitHub
issue: an issue is public from the moment it is filed, and so is the
window between filing it and a fix being released.

Report it privately instead, by
[opening a security advisory](https://github.com/btclib-org/btclib-node/security/advisories/new).
Only the maintainers can see it, the discussion stays private until an
advisory is published, and a CVE can be requested from it if the
vulnerability warrants one.

If you have no GitHub account, or would rather not use it for this,
responsible disclosure by email to *security at btclib dot org* is
equally welcome.

A report is acknowledged within 7 days, and a fix or a published advisory
follows within 90 days.

A report triaged as easy to rediscover is fixed as soon as possible, since
anyone can find it in the meantime, and its advisory is published with the
fix.

## What belongs here, and what belongs upstream

This is a whole node: `src/btclib_node/interpreter.py` validates a block
against consensus rules itself, `src/btclib_node/chainstate/` carries the
block index and the UTXO set those rules are checked against, and
`src/btclib_node/p2p/` and `src/btclib_node/rpc/` are what an untrusted
peer and a local caller each reach this process through. A defect in any
of those — a block this node accepts that Bitcoin Core would reject or
the reverse, a p2p message that can wedge a connection or exhaust memory
before its own length is even read, a UTXO index a reorg leaves
inconsistent, an RPC handler that trusts a parameter it should bound — is
this repository's to fix. A flaw that lets funds be stolen, lost or
locked is a vulnerability to report here too, and an output consensus can
spend but standard relay policy refuses counts as locked.

What belongs to [btclib](https://github.com/btclib-org/btclib/security/policy)
is the primitives and the wire serialization this node calls rather than
reimplements: elliptic-curve arithmetic, script and transaction parsing,
the message and address encodings. Report it wherever you found it,
though: routing a report is the maintainers' job, not the reporter's, and
a doubt about which project owns a flaw is not a reason to keep it to
yourself.

## Security review

The latest security review is dated 2026-09-30.
[pmazzocchi](https://github.com/pmazzocchi) did it against the
[assurance case](./ASSURANCE_CASE.md), and
[issue 1559](https://github.com/btclib-org/btclib-node/issues/1559) records it.

The review's scope is the tree at commit `02b2ed6effd2`, and release v2026.9.24
where its RPC code differs.
[What changed since](https://github.com/btclib-org/btclib-node/compare/02b2ed6effd25567c6e17c4adf53276f514a7fa5...main)
is that commit's comparison with `main`.

## Supported versions

Only the latest release is supported: a fix is published as a new
release, and nothing is backported.

Wheels and sdist are published to PyPI with PEP 740 attestations, through
a workflow that no long-lived token can authenticate for (PyPI Trusted
Publishing), so a distribution can be traced back to the workflow run and
the commit it was built from.

The same files are attached to the GitHub release, and those copies carry
a build provenance attestation of their own, signed in the run that built
them:

```shell
signer=btclib-org/.github/.github/workflows/reusable-build.yml
id=$(gh api repos/btclib-org/btclib-node --jq .id)
cert='.[].verificationResult.signature.certificate'
```

```shell
gh attestation verify <file> --owner btclib-org \
  --signer-workflow "${signer:?}" \
  --source-ref refs/tags/v<version> --format json \
  --jq "${cert:?}.sourceRepositoryIdentifier" | grep -qx "${id:?}"
```

`<file>` is a distribution file from the release. The exit code is `0`
only where a verified attestation names this repository. The repository
is compared by its id, which a rename keeps: `--repo` compares the
current name and refuses every release signed before a rename.

`--signer-workflow` names the workflow that signed, and narrows what
passes: under `--owner` the command passes without it. For a release
built by the organization's `reusable-build.yml`, which `release.yml`
calls, that is that workflow: an attestation made inside a called
workflow names the callee as its signer, while the certificate still
names this repository as the source. `--source-ref` is what keeps a
build of a branch from passing as the release. A release from v2026.9.24
on, made before this repository called `reusable-build.yml`, was signed
by `reusable-attest.yml`, named the same way. Through v2026.9.4 the
signer is `release.yml` itself, so for those releases `signer` is
`btclib-org/btclib-node/.github/workflows/release.yml`. All three take
`--source-ref refs/tags/v<version>`.
No path verifies a release another signed. The signed statement is attached to the
release as well, as `<tag>.intoto.jsonl`, or as `<tag>.attestation.jsonl` on a
release that carries that name instead, so `--bundle <that file>` runs the
same check reading it from disk instead of asking GitHub for it; one
attestation covers the wheel, the sdist and the bill of materials.

## Where this node departs from Bitcoin Core

This node speaks BIP324's v2 transport to its peers and refuses v1 unless
started with `-v1transport=1` (btclib-org/btclib-node#1190). Bitcoin Core
accepts v1, and falls back to it for a peer that does not offer v2.

v1 is plaintext. An observer on the path reads every message. v1's
checksum is not a key: a party on the path can rewrite a message and
recompute it, and the peer cannot tell. v2 encrypts and authenticates
every packet, so reading the traffic takes an active attacker in the
middle of the handshake, and a changed packet ends the connection.

BIP324 has a v2 node accept inbound v1 "to minimize risk of network
partitions", and retry with v1 a dial that a false `NODE_P2P_V2` in addr
relay sent to a v1-only peer. Core does both. This node does neither,
and the cost is peers:

- an automatic outbound connection, a feeler included, goes only to an
  address advertising `NODE_P2P_V2` or carrying exactly
  `SEEDS_SERVICE_FLAGS`, as every seed does. It chooses from fewer
  candidates, which makes it easier to surround with peers of one party,
  and a v1-only seed, or a v1-only peer falsely advertised as
  `NODE_P2P_V2`, is dialled and lost;
- an inbound v1 peer, a light client among them, is dropped on the first
  16 bytes of its `version` message, so those peers have fewer nodes to
  reach;
- an `-addnode`, `-connect` or `-seednode` peer that speaks v1 alone is
  never reached: it is dialled with v2, drops the connection, and is not
  retried with v1. `addnode` and `addconnection` with `v2transport` false
  are refused.

`-v1transport=1` restores Bitcoin Core's behaviour. `-v2transport=0`
turns v1 on by itself, and `-v2transport=0 -v1transport=0` is refused at
start.

This node does not make a `wallets` directory (btclib-org/btclib-node#1523).
Bitcoin Core's `InitConfig` makes `<datadir>/wallets` and
`<datadir>/<chain>/wallets` with each data directory it creates
(`src/common/init.cpp`, at bitcoin/bitcoin@f0de87f040, the v32.1 tag), so
that a wallet enabled later does not mix with the other files. This node has
no wallet. A data directory it made is opened by Core with its wallets in
the top-level directory, since Core uses `wallets` only where it exists.

The `scantxoutset` refusal "Cannot derive script without private keys" does
not quote the descriptor, which may hold a private key. A config line that
fails to parse, and a command-line argument refused as an invalid parameter,
are quoted as ASSURANCE_CASE.md's CWE-209 entry says. So are the key of an
unknown configuration value, which is also given its file and line, and a
section name, in the unrecognized-section warning and in the log of the
configuration, and a `settings.json` name, in the unknown-name warning, in
the duplicate-key refusal and in the log of the settings. A `settings.json`
that is not an object is refused by its value's type. Bitcoin Core quotes
each of these whole, at bitcoin/bitcoin@f0de87f040, the v32.1 tag: the
descriptor at `rpc/util.cpp:1376`, the line in `GetConfigOptions`
(`src/common/config.cpp`), the argument in `ParseParameters`
(`src/common/args.cpp:234`), the key in `ReadConfigStream`
(`src/common/config.cpp:110`), the section in `AppInitParameterInteraction`
(`src/init.cpp:991`) and `logArgsPrefix` (`src/common/args.cpp:988`), the
`settings.json` name in `ReadSettingsFile`'s warning
(`src/common/args.cpp:479`), in `ReadSettings`'s refusal
(`src/common/settings.cpp:110`) and in `LogArgs`
(`src/common/args.cpp:1007`), and the value that is not an object in
`ReadSettings` (`src/common/settings.cpp:101`). A refusal reaches stderr,
logs and the RPC caller.

Core does not read `rawpkh()`, `rawnode()` or `rawleaf()`, nor a
`musig()` where a script belongs. At top level and in `tr()`, its
`scantxoutset` refusal of one quotes the expression whole, a private key
in it included (`ParseScript` in `src/script/descriptor.cpp`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag). This node's refusal writes
the expression's arguments as `...`, as in
`'rawnode(...)' is not a valid descriptor function`. It keeps the name
before them where the name has at most `MAX_QUOTED_NAME` characters or
is `musig` after a key origin Core reads, and what follows them where
that is a derivation path, each step of it below 2^31, as `/0/*` in
`'musig(...)/0/*'`. It writes `...` for either otherwise.

Core's `scantxoutset` refusal of a key it cannot read quotes the key, or
the text it reads as one, which may be a private key given by mistake
(`ParsePubkeyInner` in `src/script/descriptor.cpp`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag). This node's refusal writes
`...` in its place, as in `pk(): Pubkey '...' is invalid`,
`wpkh(): key '...' is not valid` and
`wpkh(): Key '...' is invalid due to whitespace`. Where it gives
bip32_descriptors' refusal instead, that refusal names the key by its
index alone, as for a key path value Core refuses.

Core's `dumptxoutset` refusals quote the path it was given whole, a key
given by mistake in its place included (`src/rpc/blockchain.cpp`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag). This node keeps Core's other
words and writes the path by its length: `<52 characters> already exists.
If you are sure this is what you want, move it out of the way first` and
`Couldn't open file <64 characters> for writing.`

Core's `scantxoutset` refusal of a `tr()` leaf that no function reads
quotes the leaf whole, a key given as a leaf included (`ParseScript` in
`src/script/descriptor.cpp:2794`, at bitcoin/bitcoin@f0de87f040, the
v32.1 tag). This node writes the leaf's arguments as `(...)`, and quotes
the result where it has at most `MAX_QUOTED_NAME` characters, as in
`'foo(...)' is not a valid descriptor function`. It writes a longer leaf
`...`, as in `'...' is not a valid descriptor function` for a key given
as a leaf or for `tr(K,foo(1)/0/0/0/0/0/0/0)`, except a leaf that holds
a function the paragraph on `rawpkh()` above names, which is written as
that paragraph says. Where Core refuses a leaf before it, as `rawpkh(H)`
in `tr(K,{rawpkh(H),K})`, the refusal is that leaf's.

Core's `scantxoutset` refusal of a threshold it cannot read, in a
`multi()`, `sortedmulti()`, `multi_a()` or `sortedmulti_a()`, quotes the
threshold whole, which may be a key given in its place (`ParseScript` in
`src/script/descriptor.cpp`, at bitcoin/bitcoin@f0de87f040, the v32.1
tag). This node's refusal quotes it as the next paragraph quotes a name,
so that a key there is written `<52 characters>` between Core's quotes.

Core's RPC refusals quote what a caller gave where a hash, a hex string or
a name belongs, a key given by mistake in its place included (`ParseHashV`
and `ParseHexV` in `src/rpc/util.cpp` among them, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag). This node's refusal writes
`...` where Core writes such a string or its bytes, and keeps Core's other
words, as in `txid must be of length 64 (not 52, for '...')` and `Must
submit previous header (...) first`. `generateblock` writes `'...'`, as in
`Transaction '...' not in mempool.`, since Core's sentence there has no
quotes around the string. A name, such as a network, a hash type or a
named parameter, is quoted where it has at most `MAX_QUOTED_NAME`
characters (`btclib_node/constants.py`) and is given by its length
otherwise, as in `Network not recognized: <52 characters>`. A transaction
that `testmempoolaccept` or `submitpackage` cannot decode is named by its
index, in the words of Core's `combinerawtransaction`: `TX decode failed
for tx 1. Make sure the tx has at least one input.`

Core's start-up refusals of an `-rpccookieperms` and an `-rpcallowip` value
quote the value whole, a key given by mistake in its place included
(`InitRPCAuthentication` in `src/httprpc.cpp:260` and `InitHTTPAllowList`
in `src/httpserver.cpp:103`, at bitcoin/bitcoin@f0de87f040, the v32.1 tag).
This node keeps Core's other words. An `-rpccookieperms` value stands where
a mode's name belongs, so it is quoted as the paragraph above quotes a
name, as in `Invalid -rpccookieperms=<52 characters>; must be one of
'owner', 'group', or 'all'.` An `-rpcallowip` value stands where a subnet
belongs, so the refusal writes `'...'` in its place and names the value by
its position among the `-rpcallowip` values, counted from 1 in the order
Core's `GetArgs` lists them: `Invalid -rpcallowip subnet specification:
'...' (value 3 of -rpcallowip). Valid values are ...`.

Core's other start-up refusals of an option's value, and its refusal of an
argument that is not an option, quote the value whole too (`InvalidPortErrMsg`,
`ResolveErrMsg` and `AmountErrMsg` in `src/common/messages.cpp` among them, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag). This node keeps Core's other words.
A value that stands where a name belongs, as a chain, a logging category or a
P2P permission, is quoted as a name is above, as in `Unknown chain <52
characters>.` Any other value but a path is written `'...'`, or `...` where
Core's sentence puts it in parentheses, as in `Invalid minimum work specified
(...), must be up to 64 hex digits` and `Command line contains unexpected token
'...', see btclib-node -h for a list of options.` Such a value of an option
that takes several is also named by its position, as an `-rpcallowip` value is:
`Cannot resolve -bind address: '...' (value 2 of -bind)`. A `-whitebind`
address that names no port is quoted, as Core quotes it, where the log of the
settings below writes an address whole. Core's `Bind address family for %s not
supported` writes the address a `-bind` or `-whitebind` value resolves to, an
I2P host's label included (`BindListenPort` in `src/net.cpp:3396`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag). This node writes that address whole
where the log of the settings below writes the value whole, and otherwise
`'...'` and the value's position, as in `Bind address family for '...' (value 1
of -bind) not supported`. Core's `Duplicate binding configuration for address
%s` writes such an address too (`AppInitMain` in `src/init.cpp:2368`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag), and this node writes it the same
way, as in `Duplicate binding configuration for address '...' (value 2 of
-bind)`.

Core's start-up refusals of a path write it whole, a key given by mistake
in place of an option's value included (`InitConfig` in
`src/common/init.cpp:22`, `ReadConfigFiles` in `src/common/config.cpp:152`
and `LockDirectory` in `src/init.cpp:1237` among them, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag). This node keeps Core's other
words. A path built from defaults alone, as the default data directory's
`settings.json`, is written as Core writes it. A path that holds an option's
value, as a `-datadir`, `-blocksdir`, `-conf`, `includeconf`, `-settings` or
`-rpccookiefile` value, is given by its length: `Specified data directory
<52 characters> does not exist.` An `includeconf` value is also named by its
position, counted from 1 in the order the files are read: `Failed to include
configuration file <13 characters> (value 2 of -includeconf)`. Python's error
text for such a path, the warnings a configuration file's reading writes, and
the log's `Config file:` and `Generated RPC authentication cookie` lines take
the same form.

Core's start-up warnings and log lines write an option's value whole too, a key
given by mistake included: the double-negative warning (`InterpretValue` in
`src/common/args.cpp:114`), the `-test` warning (`AppInitParameterInteraction`
in `src/init.cpp:1180`), the RPC server's bind lines (`InitHTTPServer` in
`src/httpserver.cpp:1367` and `:1380`) and the warning of a notify command that
fails (`runCommand` in `src/common/system.cpp:53`), at
bitcoin/bitcoin@f0de87f040, the v32.1 tag. This node keeps Core's other words,
and writes the value as the start-up refusals above write theirs. A double
negative's value is `'...'`, as in `Parsed potentially confusing
double-negative -listen='...'`, except `0`, written whole as Core writes it:
`-listen=0` for `-nolisten=0`. A `-test` value is quoted as a name, so that a
key there is written `<52 characters>` between Core's quotes, except a name
Core's `TEST_OPTIONS_DOC` lists, which is written whole. An RPC host is written
whole where it looks up and the log of the settings below writes it whole, and
so is the address it resolves to, which Core writes where a bind fails
(`src/httpserver.cpp:1375`). Any other host is `'...'` in each of these lines,
the failed bind's included, and an `-rpcbind` value is also named by its
position: `Could not bind RPC on address '...' (value 2 of -rpcbind) port 8332:
Address lookup failed.` A notify command is `...`, followed by the option it is
the value of: `runCommand error: system(...) returned 1 (value 2 of
-shutdownnotify)`.

Core writes a peer's destination whole, a key given by mistake in its place
included, in a debug line where the destination resolves to an invalid address,
and in an info line where it resolves to an address already connected
(`ConnectNode` in `src/net.cpp:421` and `:427`, at bitcoin/bitcoin@f0de87f040,
the v32.1 tag). The destination is the value of an option or of an RPC's
argument, as of `-connect` or `addnode`. This node writes it as the log of the
settings below writes an address, in those lines and in its own lines of a
destination already connected by name and of a dial that does not come up:
`Dial to '...' did not come up`. So a DNS name is `'...'` too, and these lines
do not say which name was dialled.

Core's log of the settings writes every value whole but a `SENSITIVE`
option's from the configuration file or the command line, which it writes
`****`. So a key given by mistake to any other option is written whole, and so
is every `settings.json` value (`logArgsPrefix` and `LogArgs` in
`src/common/args.cpp:993` and `:1007`, at bitcoin/bitcoin@f0de87f040, the v32.1
tag). This node writes `****` for a sensitive option's value wherever it was
read, and for the value of a `settings.json` name no option has, which may be a
sensitive one misspelled. It writes any other value by its shape
(btclib-org/btclib-node#2556), as in `Command-line arg: blocknotify='...'`:

- a name, as a chain, a logging category or a `-test` value, whole where the
  warnings above write it whole, and by its length otherwise;
- an address whole where it resolves without DNS and none of its parts between
  the separators of an address, as a dot or a colon, is longer than
  `MAX_QUOTED_NAME` characters. The onion or I2P host an address starts with is
  excepted where it has the shape Core writes one in: 56 lowercase base32
  characters before `.onion`, or 52 before `.b32.i2p`
  (btclib-org/btclib-node#2570). Its port is not excepted, nor is a host of
  another case or length, as a WIF given as an I2P label. An I2P label is any
  32 bytes in base32, so a key written that way before `.b32.i2p` is written
  whole. Any other address is `'...'`. Resolving alone is not enough: a port
  may carry any number of leading zeros, and macOS reads a key's decimal, or
  its hex after `0x`, as an IPv4 address, and any text after an IPv6 address's
  `%` as its scope;
- a path by its length, and a notify command and an `-rpcwhitelist` value,
  which may hold a user name, as `'...'`;
- any other value whole where it has at most `MAX_QUOTED_NAME` characters, as
  a number, an amount or a port has, and `'...'` otherwise, as an
  `-assumevalid` hash is, since a key in hex looks the same;
- a `settings.json` number by the same rules, and an array or an object as
  `'...'`.

What a refusal may print about a secret is the organization's rule, section 7's
*A refusal carries no secret* in
[the organization's standard](https://github.com/btclib-org/.github/blob/main/README.md).

`-reindex-chainstate` wipes the undo data with the coins
(btclib-org/btclib-node#1415). Bitcoin Core keeps it:
`BlockManager::WriteBlockUndo` writes no undo data for a block that has
some (`src/node/blockstorage.cpp`, at bitcoin/bitcoin@f0de87f040, the
v32.1 tag). This node writes it again as it connects each block, and wipes
it because `BlockDB.add_rev_block` skips a block that already has a patch,
so without the wipe a corrupt record would survive the rebuild. A stop
between the chainstate wipe and the undo wipe keeps the old patches until
a start with the option wipes them.

The log escapes C1 controls, U+0080 to U+009F, as `\x` and two hex digits,
beside the C0 controls and DEL that Bitcoin Core escapes too
(btclib-org/btclib-node#1952). Core's `BCLog::LogEscapeMessage` escapes bytes
below 0x20 and 0x7f (`src/logging.cpp`, at bitcoin/bitcoin@aa76bd266d). A C1
control's UTF-8 is 0xc2 and a byte from 0x80 to 0x9f, so Core writes it as it
is, and a terminal that decodes UTF-8 and honours C1 controls acts on it in a
log it shows, as it would on an ESC. The cost is a log line that differs from
Core's where a message holds such a character.

Where the reader of a named pipe goes during `dumptxoutset`, this node answers
`AutoFile::write`'s failure (btclib-org/btclib-node#2187). Bitcoin Core
answers that where a write fails, and `Error closing`'s message where the
bytes left fit its `FILE` buffer and only the close fails
(`src/rpc/blockchain.cpp`, at bitcoin/bitcoin@f0de87f040, v32.1). The two
cases split on a buffer size in Core's C library, so no client can rely on
which text it gets. The failure and its RPC code, `RPC_MISC_ERROR`, are the
same. The cost is a message that differs from Core's where the reader goes
near the end of a dump.

## Limitations, not vulnerabilities

The [assurance case](./ASSURANCE_CASE.md) is the threat model these are
written against, and the argument for what this file does promise.

Known and recorded, rather than something to report again.

- **Every caller the JSON-RPC listener accepts may call every method
  `-rpcwhitelist` leaves it, over plain HTTP.** It accepts Core's
  cookie, `-rpcauth` and `-rpcuser`/`-rpcpassword` users
  (btclib-org/btclib-node#1055, btclib-org/btclib-node#1070), and with
  no `-rpcwhitelist`, Core's default too, whoever holds one of those
  credentials can call `stop` and `sendrawtransaction`. The credential
  crosses the wire as HTTP Basic, unencrypted: the listener has no TLS,
  as Core's has none. Core
  [dropped `-rpcssl`](https://github.com/bitcoin/bitcoin/blob/master/doc/release-notes/release-notes-0.12.0.md#rpc-ssl-support-dropped)
  to remove OpenSSL from the daemon, and says its RPC
  ["does not use encryption" and "has not been hardened to withstand
  arbitrary Internet traffic"](https://github.com/bitcoin/bitcoin/blob/master/doc/JSON-RPC-interface.md#security).
  The listener binds loopback by default
  (btclib-org/btclib-node#27), and `-rpcbind` binds elsewhere only beside
  `-rpcallowip`, which then decides which sources are answered at all, as
  in Core. Reach it from another machine only through a tunnel (SSH,
  stunnel or a VPN), and never expose it to the Internet.
- **BIP324 does not authenticate the peer.** A party in the middle of the
  handshake reads and relays everything; comparing `getpeerinfo`'s
  `session_id` with the peer's operator over another channel is what
  detects it. Nor does v2 hide the timing and sizes of the packets.
- **`Development Status :: 3 - Alpha` is the claim `pyproject.toml`
  makes**, and it is the right one to read the limitations above against: this
  node has downloaded and validated the chain, which is not the same as
  having been run against somebody trying to make it do otherwise.
