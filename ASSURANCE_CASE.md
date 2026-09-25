# Assurance case

[SECURITY](./SECURITY.md) states what a user can and cannot expect of
btclib-node in terms of security. This page argues why those
expectations hold: the threat model, the trust boundaries, how secure
design principles are applied, and how common implementation weaknesses
are countered. Each argument below names the file, the test or the
workflow that supports it; where SECURITY.md already states a fact,
this page points at it instead of repeating it. The components named
here are the ones [ARCHITECTURE](./ARCHITECTURE.md) describes.

## What is claimed

- **A block, a transaction, a script and an address this node accepts
  or builds agree with Bitcoin Core.**
  `.github/workflows/integration-bitcoind.yml` runs a disposable
  regtest `bitcoind` against a fresh node over p2p and checks the tip,
  `tests/integration/reorg_test.py` checks that a chain split follows
  Core off an abandoned branch, and
  `tests/integration/backpressure_test.py` checks that the receive
  bound below actually engages against a real daemon serving blocks
  faster than this node validates them.
- **Octets from a peer or a caller either parse into what they claim to
  be or are refused, and nothing else.** `tests/property_test.py`'s own
  property — "over unconstrained octets, a declared entry point either
  returns or raises `BTClibException`, and nothing else" — is checked by
  Hypothesis over the domain it describes and extended by the harnesses
  under `fuzz/`, run under ClusterFuzzLite in `.github/workflows/fuzz.yml`.
- **What one connection may cost this node, and how many inbound
  connections it holds, are bounded.** A single peer cannot commit this
  node past `MAX_QUEUED_SEND_BYTES`, a fixed sum of a block's and a
  filter answer's own sizes (`src/btclib_node/p2p/connection.py`),
  closed as btclib-org/btclib-node#101, and `P2pManager.server` closes
  an inbound peer past `Config.max_connections`'s inbound share before
  building anything for it (`src/btclib_node/p2p/manager.py`), closed
  as btclib-org/btclib-node#1054; SECURITY.md's *Limitations* states
  what this node still does not do once its inbound slots are taken.
- **Inbound connections do not stop this node dialling.** The outbound
  dial target counts only the connections `P2pManager` dialled off its
  own draw, not inbound or `-connect`/`-addnode` ones
  (`src/btclib_node/p2p/manager.py`), closed as
  btclib-org/btclib-node#1065.
- **A published distribution is what this tree built.** SECURITY.md's
  *Supported versions* states how that is verified.
- **What is not claimed.** `Development Status :: 3 - Alpha`
  (`pyproject.toml`) is the claim this node makes about itself: it has
  downloaded and validated a chain, which SECURITY.md's own closing line
  says is not the same as having been run against somebody trying to
  make it do otherwise. Production consensus-safety is not asserted
  anywhere in this tree.

## Threat model

btclib-node is an application, not a library: it opens sockets of its
own, on both surfaces ARCHITECTURE.md's *The protocol and the RPC
surface* describes, and it writes to a datadir on disk. The command
below lists the top-level name of every module `src/` imports, at any
depth and in any spelling of the statement — `bitcoin_core_rpc` and
`rocksdict` beside btclib itself, and otherwise the standard library.

```shell
python3 - <<'EOF'
import ast, pathlib
names = set()
for p in pathlib.Path("src").rglob("*.py"):
    for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            names.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            names.add(n.module.split(".")[0])
print(sorted(names))
EOF
```

`bitcoin_core_rpc` is a required dependency, and `src/` imports only
`RPCErrorCode` and `chain_from_network` from it — its error-code and
chain vocabulary, eagerly loaded by the package's own `__init__` — never
`BitcoinCoreRpcClient` or a transport, which the package loads lazily
and only `tests/` (never shipped) asks for. `asyncio` and `socket` are
what `P2pManager` and `RpcManager` open their listeners and connections
through; `rocksdict` is the store; `multiprocessing` is `Node.worker_pool`
under a GIL interpreter.

**What is defended.**

- The correctness of every verdict this node reaches over a peer's or a
  caller's input: a block or a transaction accepted that Core would
  reject, a valid one refused, or an answer that disagrees with what the
  chain state on disk actually holds.
- This process, against octets built to make a parser raise an exception
  this tree does not document, or allocate without bound.
- The chain state on disk, against a bit flipped by the medium under it,
  and against being read from a second process while a `Node` already
  has it open.
- The user's control over what serves the RPC port and to whom: the
  caller-supplied `Config.rpc_host`/`-rpcbind` decides who can reach
  it, and the cookie and `-rpcauth` decide who it answers.

**The adversaries.**

- A remote peer, over p2p: a handshake, a block, a transaction, an
  address, a filter request, or any other message this node's protocol
  handlers read.
- Whoever can reach the RPC port: a request's credential is checked
  before its body is decoded, and a caller without an accepted one gets
  a 401 and nothing else.
- A party tampering with the medium the datadir sits on, or opening the
  same datadir from a second process.
- A party tampering with a distribution between this tree and the user.

**What is not defended**, each stated in SECURITY.md's *Limitations, not
vulnerabilities*:

- the JSON-RPC listener, against a caller holding an accepted
  credential, who may call every method, and against whoever can read
  the plain HTTP it is sent over
- the inbound p2p slots, against whoever fills them first
- anything SECURITY.md attributes to btclib rather than to this tree —
  the constant-time properties of the arithmetic btclib's own
  [assurance case](https://github.com/btclib-org/btclib/blob/main/ASSURANCE_CASE.md)
  states, since btclib-node holds no private key of its own and calls
  btclib only for verification, never for signing

Nor is the interpreter, the operating system, or the RocksDB build this
node runs on: an application shares its process with all three and has
no defence against them.

## Trust boundaries

**The caller and the RPC surface.** `src/btclib_node/rpc/` is where a
local caller's request crosses in. `RpcConnection` bounds what it will
read before decoding anything — `MAX_HEADER_BYTES` and `MAX_BODY_BYTES`
in `rpc/connection.py` — and a body `json.loads` cannot parse answers
JSON-RPC 2.0's own `PARSE_ERROR` rather than closing the socket with
nothing said. Before that decoding, `RpcConnection.run` checks the
request's credential against `rpc/auth.py`'s `RpcAuth` and answers one
it does not accept with a 401, so no handler sees a request from a
caller without the cookie or an `-rpcauth` password.
`rpc/callbacks.py`'s own module docstring states the rest of the
boundary: every caller that is accepted may call every handler.

**btclib-node and btclib.** Every object on the wire and every
consensus rule crosses this boundary rather than being reimplemented:
`src/btclib_node/interpreter.py` calls `btclib.script.engine.verify_transaction`
and `verify_input`, dispatched across `Node.worker_pool`, and trusts
btclib's own verdict the way ARCHITECTURE.md's *Validation* describes.
A flaw in the arithmetic or the parsing on the far side of this boundary
is btclib's to fix, and SECURITY.md's *What belongs here, and what
belongs upstream* says where each kind of defect is reported.

**Octets from the network.** `src/btclib_node/p2p/connection.py` is
where a peer's own bytes cross in, ahead of btclib's wire codec:
`Connection.parse_messages` peeks a header's own length field before
building anything from it, checks the magic against the chain this node
runs, and bounds what it will buffer in either direction —
`MAX_PROTOCOL_MESSAGE_LENGTH` on what one message may claim to be,
`MAX_QUEUED_RECV_BYTES` on what may sit unprocessed, `MAX_QUEUED_SEND_BYTES`
on what this node will queue back out — `getdata` and `getcfilters`
paced against that last bound, checked before every item rather than
once a whole answer is built, and `headers` and `addr` sized into
headroom of their own since each answers a request in one message and
neither is frequent enough to need a pacing point, the module's own
comment beside `MAX_QUEUED_SEND_BYTES` arguing the sizing in full
(btclib-org/btclib-node#101). What crosses this boundary already framed
is handed to btclib's own `Message.parse` for the codec itself.

**The store.** `src/btclib_node/db.py` is what every index opens its
datadir through, and it is the one place a bit flipped on disk is
caught: every record carries its own checksum, verified on every read,
which is RocksDB's own design and not a scheme built by hand
(`db.py`'s own docstring argues it against Bitcoin Core's LevelDB in
full). A corrupted key or value is a `StoreCorruptionError`, never a
silently wrong answer, and a second process opening the same datadir is
refused by RocksDB's own directory `LOCK` rather than let in the way a
WAL-backed store would let a second reader in.

**The environment and the files.** `-datadir`, `-conf` and every other
CLI flag `cli.py` reads, and `bitcoin.conf` inside the datadir it names,
are the operator's own input, not a remote party's — `cli.py`'s own
module docstring is where each flag is named against Bitcoin Core's
equivalent. Nothing under `src/` opens a file the operator did not name,
directly or through the datadir.

**Bitcoin Core, as an oracle rather than a dependency.**
`.github/workflows/integration-bitcoind.yml` is the one place this
node's own answers are checked against a `bitcoind` it does not talk to
in any other job, over p2p rather than over RPC, matching what a real
peer would see.

## Secure design principles

Saltzer and Schroeder's principles, over the layering ARCHITECTURE.md
describes.

- **Economy of mechanism.** One class is where every index's storage is
  decided (`db.py`), one dispatch is where a fork of validation work is
  fanned out (`Node.worker_pool`, chosen by one predicate,
  `_pool_factory`), and one exception hierarchy groups a failure by what
  actually went wrong rather than by which module raises it
  (`exceptions.py`'s own docstring argues the grouping).
- **Fail-safe defaults.** `Config.rpc_host` defaults to loopback, and
  widening it to another interface is an operator's explicit
  `-rpcbind`, matching Core's own `rpcbind`/`rpcallowip` default. A
  second process cannot silently share a datadir already open: RocksDB's
  own `LOCK` refuses it rather than allowing concurrent, uncoordinated
  writers.
- **Complete mediation.** A p2p message is bounded and its magic checked
  before it is parsed at all, never after; an RPC body is bounded before
  `json.loads` ever sees it. `_drain_message_queues` and `_step_chain`
  (`src/btclib_node/__init__.py`) wrap every handler call in one
  `try`/`except Exception`, so a handler that raises on input neither of
  them expected is logged rather than silently skipping the mediation
  around it.
- **Open design.** The code, the fuzz corpus and harnesses, and the
  limitations are published; SECURITY.md states what is known rather
  than leaving it to be found again.
- **Least privilege.** `src/` loads no RPC client of its own and no
  transport, the census under *Threat model* shows; the only sockets
  this process opens are the p2p and RPC listeners and the outbound
  peer connections `P2pManager.connect` makes, and the only files it
  opens are its own datadir and whatever `-conf`/`-datadir` name.
- **Psychological acceptability.** A malformed RPC request answers
  JSON-RPC 2.0's own error object rather than closing the socket with
  nothing said, so a caller can tell its own mistake from this node
  refusing the call outright.
- **Layering.** ARCHITECTURE.md's own sections are this: the loop does
  not parse wire objects itself, the protocol and RPC surfaces do not
  validate consensus rules themselves, and validation does not open a
  socket or a file itself.

**Which thread reaches a piece of state decides whether it needs a
lock, not which callback names it.** ARCHITECTURE.md's *The protocol and
the RPC surface* is the argument in full: `Mempool` is reached from one
thread only and carries no lock, and `PeerDB` is reached from two and
carries two, taken separately and never nested. A change that moves a
callback across that boundary without re-reading this argument is what
`REVIEWING.md` asks a reviewer to check for.

## Common implementation weaknesses

Weaknesses from MITRE's CWE list that a node of this kind is exposed to,
and what counters each.

- **Improper input validation (CWE-20).** The trust boundaries above,
  and the harnesses under `fuzz/`: `fuzz_framing.py` for this tree's own
  framing arithmetic ahead of btclib's wire codec, `fuzz_rpc_head.py` for
  the HTTP header arithmetic ahead of `json.loads`, and
  `fuzz_process_message.py` for `p2p/callbacks.py`'s own handlers.
- **Race conditions on shared state (CWE-362).** Countered by
  construction rather than by a runtime check in most of this tree:
  ARCHITECTURE.md's *The protocol and the RPC surface* is the argument
  for which state needs a lock, and `PeerDB`'s own two separately-taken,
  never-nested locks are the one piece of state actually reached from
  two threads.
- **Uncaught exceptions on hostile input (CWE-248, CWE-755).**
  `tests/property_test.py`'s property, over the harnesses' own declared
  entry points: unconstrained octets either parse into what they claim
  to be or raise `BTClibException`, and nothing else.
  `tests/fuzz_corpus_test.py` checks that every corpus seed still
  parses. Inside `Node`'s own loop, `_drain_message_queues` and
  `_step_chain` catch what a handler raises and log it rather than
  ending the process.
- **Uncontrolled resource consumption (CWE-400, CWE-770).**
  `MAX_HEADER_BYTES`/`MAX_BODY_BYTES` on the RPC surface,
  `MAX_PROTOCOL_MESSAGE_LENGTH`/`MAX_QUEUED_RECV_BYTES`/`MAX_QUEUED_SEND_BYTES`
  and the pacing beside them on the p2p surface. SECURITY.md's
  *Limitations* states what is bounded and what is not yet.
- **Weak randomness (CWE-330, CWE-338).** `ruff`'s flake8-bandit family,
  selected whole in `pyproject.toml`, flags a bare `random` import under
  `src/`; `download.py`'s own trickle-relay schedule and
  `p2p/callbacks.py`'s own address-sampling jitter draw from
  `random.SystemRandom` rather than the default generator precisely
  because each is a choice a peer is meant not to be able to predict,
  and `p2p/connection.py`'s handshake and keep-alive nonces draw from
  `secrets` directly.
- **Improper verification of a signature or a chain (CWE-347).** Checked
  against Bitcoin Core, not merely against this tree's own suite: the
  regtest oracle under *What is claimed* above, and the vendored vectors
  `.github/workflows/vendored-vectors.yml` compares with upstream on a
  schedule.
- **Deserialization of untrusted data (CWE-502).** `json.loads` on an
  RPC body is the only deserializer of untrusted data `src/` calls
  directly, and it is the standard library's own, bounded ahead of the
  call by `MAX_BODY_BYTES`; a p2p message is parsed by btclib rather
  than unpickled or unmarshalled. The census under *Threat model* lists
  neither `pickle` nor `marshal` nor `shelve`. `.github/workflows/codeql.yml`
  analyses the code and the workflows.
- **Type confusion (CWE-843).** mypy runs with `strict = true`
  (`pyproject.toml`) over this tree and its suite, as a hook of the lint
  gate in `.pre-commit-config.yaml`.
- **Code that is wrong and still passes.** Line and branch coverage is
  held at 100% by `fail_under` in `pyproject.toml`, and
  `.github/mutation/interpreter.toml`, run by `.github/workflows/mutation.yml`,
  asks whether the suite notices a wrong line inside
  `src/btclib_node/interpreter.py`, the consensus entry point
  ARCHITECTURE.md's *Validation* names.
- **Supply chain.** SECURITY.md's *Supported versions* describes the
  attestations. `uv.lock` pins every dependency, and the suite and the
  lint gate install with `--locked` (`CONTRIBUTING.md`'s own commands);
  `deps-latest.yml` and `deps-oldest.yml` are the deliberate exception,
  each resolving fresh so a break in what the pin hides is caught on a
  schedule rather than never. Every third-party action is pinned to a
  commit sha; `actionlint`, `zizmor` and `detect-secrets` run as hooks in
  `.pre-commit-config.yaml`.
