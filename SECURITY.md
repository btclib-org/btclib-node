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

No version has been released yet — `RELEASING.md` has why. Once one has,
only the latest release is supported; a fix is published as a new
release, and nothing is backported.

Wheels and sdist are published to PyPI with PEP 740 attestations, through
a workflow that no long-lived token can authenticate for (PyPI Trusted
Publishing), so a distribution can be traced back to the workflow run and
the commit it was built from.

The same files are attached to the GitHub release, and those copies carry
a build provenance attestation of their own, signed in the run that built
them:

```shell
gh attestation verify btclib_node-<version>-py3-none-any.whl \
  --repo btclib-org/btclib-node \
  --signer-workflow btclib-org/btclib-node/.github/workflows/release.yml
```

`--signer-workflow` is what makes that say which workflow signed, rather
than accepting any attestation this repository has. The signed statement
is attached to the release as well, as `<tag>.attestation.jsonl`, so
`--bundle <tag>.attestation.jsonl` runs the same check reading it from
disk instead of asking GitHub for it; one attestation covers the wheel,
the sdist and the bill of materials.

## Limitations, not vulnerabilities

Known, recorded, and each an open issue rather than something to report
again.

- **The JSON-RPC listener binds every interface** — `0.0.0.0`, with no
  configuration option to bind loopback instead — **and authenticates
  nothing.** The method table it serves carries `stop` and
  `sendrawtransaction`, so anybody who can reach the port can stop the
  node and make it announce a transaction. Run it where nothing else can
  reach that port. btclib-org/btclib-node#27.
- **What a peer may ask for is not bounded by what asking costs it.** A
  short request can commit this node to a long reply, and nothing limits
  what one peer may have in flight. btclib-org/btclib-node#101.
- **`Development Status :: 3 - Alpha` is the claim `pyproject.toml`
  makes**, and it is the right one to read the two above against: this
  node has downloaded and validated the chain, which is not the same as
  having been run against somebody trying to make it do otherwise.
