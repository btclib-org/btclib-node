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
signer=btclib-org/.github/.github/workflows/reusable-attest.yml
gh attestation verify btclib_node-<version>-py3-none-any.whl \
  --repo "$repo" --signer-workflow "$signer"
```

`--signer-workflow` names the workflow that signed. From v2026.9.24 on
that is the organization's `reusable-attest.yml`, which this
repository's `release.yml` calls: an attestation made inside a called
workflow names the callee as its signer, while `--repo` still names
this repository as the source. For those releases the flag is required
rather than a narrowing, the command refusing a genuine release without
it. Through v2026.9.4 the signer is `release.yml` itself, so for those
releases `signer` is `"$repo/.github/workflows/release.yml"`, and there
the flag narrows what passes: without it an attestation from any
workflow in this repository is accepted. Neither path verifies a
release the other signed. The signed statement is attached to the
release as well, as `<tag>.attestation.jsonl`, so
`--bundle <tag>.attestation.jsonl` runs the same check reading it from
disk instead of asking GitHub for it; one attestation covers the wheel,
the sdist and the bill of materials.

## Limitations, not vulnerabilities

The [assurance case](./ASSURANCE_CASE.md) is the threat model these are
written against, and the argument for what this file does promise.

Known, recorded, and each an open issue rather than something to report
again.

- **The JSON-RPC listener authenticates nothing.** `Config.rpc_host`
  binds loopback by default and `-rpcbind` is what widens it, matching
  Core's own `rpcbind`/`rpcallowip` default (btclib-org/btclib-node#27);
  nothing behind that bind checks who is asking. The method table it
  serves carries `stop` and `sendrawtransaction`, so anybody who can
  reach the port can stop the node and make it announce a transaction.
  Run it where nothing else can reach that port, and do not widen the
  bind without one. btclib-org/btclib-node#1055.
- **Whoever fills the inbound slots first keeps them.** What one peer's
  queue may hold is capped in `p2p/connection.py`, checked before every
  `getdata` and `getcfilters` item rather than once a whole answer is
  built (btclib-org/btclib-node#101), and how many inbound peers are
  held at once is `Config.max_connections`'s inbound share
  (btclib-org/btclib-node#1054). A peer arriving once that share is
  taken is closed, where Core first tries to evict a peer it already
  holds, so connections held open from anywhere lock every later peer
  out (btclib-org/btclib-node#1064).
- **`Development Status :: 3 - Alpha` is the claim `pyproject.toml`
  makes**, and it is the right one to read the two above against: this
  node has downloaded and validated the chain, which is not the same as
  having been run against somebody trying to make it do otherwise.
