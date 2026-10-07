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
this repository's to fix.

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
repo=btclib-org/btclib-node
workflows=btclib-org/.github/.github/workflows
signer=$workflows/reusable-build.yml@refs/heads/main
gh attestation verify btclib_node-<version>-py3-none-any.whl \
  --repo "$repo" --signer-workflow "$signer" --source-ref refs/tags/v<version>
```

`--signer-workflow` names the workflow that signed. For a release built by
the organization's `reusable-build.yml`, which `release.yml` calls, that
is that workflow: an attestation made inside a called workflow names the
callee as its signer, while `--repo` still names this repository as the
source. The flag is required rather than a narrowing, the command
refusing a genuine release without it, and `--source-ref` is what keeps a
build of a branch from passing as the release. A release from v2026.9.24
on, made before this repository called `reusable-build.yml`, was signed
by `reusable-attest.yml`, named the same way. Through v2026.9.4 the
signer is `release.yml` itself, so for those releases `signer` is
`"$repo/.github/workflows/release.yml"`, and there `--signer-workflow`
narrows what passes: without it an attestation from any workflow in this
repository is accepted. All three take `--source-ref refs/tags/v<version>`.
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
`<datadir>/<chain>/wallets` with each data directory it creates, at
bitcoin/bitcoin@9be056a8a7 (v31.1), so that a wallet enabled later does not
mix with the other files. This node has no wallet. A data directory it made
is opened by Core with its wallets in the top-level directory, since Core
uses `wallets` only where it exists.

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
  crosses the wire as HTTP Basic, unencrypted, as Core's has since Core
  dropped `-rpcssl`. The listener binds loopback by default
  (btclib-org/btclib-node#27), and `-rpcbind` binds elsewhere only beside
  `-rpcallowip`, which then decides which sources are answered at all, as
  in Core: do not widen either past a network whose traffic you trust.
- **BIP324 does not authenticate the peer.** A party in the middle of the
  handshake reads and relays everything; comparing `getpeerinfo`'s
  `session_id` with the peer's operator over another channel is what
  detects it. Nor does v2 hide the timing and sizes of the packets.
- **`Development Status :: 3 - Alpha` is the claim `pyproject.toml`
  makes**, and it is the right one to read the limitations above against: this
  node has downloaded and validated the chain, which is not the same as
  having been run against somebody trying to make it do otherwise.
