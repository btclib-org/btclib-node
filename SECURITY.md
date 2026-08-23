# Security policy

## Reporting a vulnerability

Never as a GitHub issue: an issue is public from the moment it is filed,
and so is the window between filing it and there being a fix.

There is no advisory form here. Private vulnerability reporting is off
on this repository:

```shell
gh api repos/btclib-org/btclib-node/private-vulnerability-reporting
# {"enabled":false}
```

so `/security/advisories/new` is not a route a reporter has. Turning it
on is a repository setting rather than a file, and
btclib-org/btclib-node#136 is where it is tracked; `REPOSITORY.md`
records the answer that call gives today. Until it is on, responsible
disclosure by email to *security at btclib dot org* is the channel, and
it reaches the same maintainers.

Do not use btclib's advisory form for a defect in this node. Its scope
is that library's, so a report filed there is a report filed against
another project, and the delay is the reporter's to spend for nothing.

## What belongs here, and what belongs upstream

This node validates *with*
[btclib](https://github.com/btclib-org/btclib/security/advisories/new):
the keys, the scripts, the transactions and the serialization of
everything on the wire are that library's. Where the wrong thing is
parsed, signed or verified — rather than the wrong thing done with a
correct answer — the defect most likely belongs to it.

What belongs here is what this node does with that answer:

- what it accepts from a peer, and what a single message may cost the
    process it is handled in — `btclib_node/p2p/`, its connections, its
    handlers, and the queues between them and the main loop
- what it puts on the wire unasked: what is announced, when, and what
    the timing of an announcement says about where it came from
- what the chain state accepts as valid and what it does when something
    it accepted turns out not to be — `btclib_node/chainstate/`,
    `btclib_node/block_db/`, and the store beneath them
- what the JSON-RPC surface exposes, and to whom
- what is written into the data directory, and what reading it back can
    be made to do

Consensus and the protocol themselves are
[Bitcoin Core](https://github.com/bitcoin/bitcoin/security/policy)'s to
define: this node disagreeing with it is a defect here, and the
definition is a question for upstream.

Report it wherever you found it, though: routing a report is the
maintainers' job and not the reporter's, and a doubt about which project
owns a flaw is not a reason to keep it to yourself.

## Supported versions

There is no supported version and nothing to upgrade to. Nothing is
published to an index:

```shell
curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/btclib-node/json
# 404
```

and the tag this repository carries has no artifact attached to it. What
anybody runs is a checkout of `main`, so a fix reaches them when they
pull it. `RELEASING.md` says what a release will be when there is one,
and what it will owe a reader then.

## Limitations, not vulnerabilities

Known, recorded, and each an open issue rather than something to report
again.

- **The JSON-RPC listener binds every interface** — `0.0.0.0`, with no
    configuration option to bind loopback instead — **and authenticates
    nothing.** The method table it serves carries `stop` and
    `sendrawtransaction`, so anybody who can reach the port can stop the
    node and make it announce a transaction. Run it where nothing else
    can reach that port. btclib-org/btclib-node#27.
- **What a peer may ask for is not bounded by what asking costs it.** A
    short request can commit this node to a long reply, and nothing
    limits what one peer may have in flight. btclib-org/btclib-node#101.
- **`Development Status :: 3 - Alpha` is the claim `pyproject.toml`
    makes**, and it is the right one to read the rest of this file
    against: this node has downloaded and validated the chain, which is
    not the same as having been run against somebody trying to make it
    do otherwise.
