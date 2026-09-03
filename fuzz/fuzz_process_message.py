# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""An atheris harness fuzzing `p2p.callbacks.py`'s handlers through a node.

Issue #516 split the octets a stranger's own bytes reach into three
shapes: a pure function of bytes over a codec `btclib` owns, this
tree's own hostile-input arithmetic ahead of that codec, pulled into a
function of octets for the same reason (`fuzz_framing.py`,
`fuzz_rpc_head.py`), and `p2p.callbacks.py`'s own handlers, each of
which needs a `Node`, a `P2pManager` and a `Connection` rather than a
bare buffer -- issue #698 is that third shape.

The first shape is fuzzed where the codec lives, which for BIP61's
`reject` is `btclib` and its own harness over `Reject.parse`. What this
tree owns of that message is `p2p.callbacks.reject`, one of the
handlers this harness drives (issue #827).

Core carries two fuzz targets over that same layer, one message
(`process_message.cpp`) and a sequence of them fed to the same
connection in one call (`process_messages.cpp`, at
bitcoin/bitcoin@ca7162cde5). This harness takes the first shape: one
call, one message, matching every other harness in this directory --
atheris supplies the sequence across many calls instead, on the one
`Node` `_built_node` reuses, rather than inside a single call the way
`process_messages.cpp` schedules several `CSerializedNetMsg`s at once.
What that leaves untested is a defect only a *particular* sequence
within one call triggers, which is `process_messages.cpp`'s own share
of Core's surface and not this harness's.

Core's own fuzz target for this layer, `process_message.cpp`
(at bitcoin/bitcoin@ca7162cde5), builds a whole `TestingSetup` once, in
its own `initialize_process_message`, and reuses most of it across
passes -- but rebuilds the rest on every single one, not only a dirty
one: `connman.Reset()`, `node.banman.reset()`, `node.addrman.reset()`
and `node.peerman.reset()` run unconditionally at the top of every
call, with `node.addrman` and `node.peerman` built fresh right after,
labelled "Reset, so that dangling pointers can be detected by
sanitizers" -- a C++ reason, ASan/MSan catching a stale pointer into an
object a rebuilt `connman`/`addrman`/`peerman` no longer owns, with no
Python analogue: nothing here holds a raw pointer into any of `_node`'s
own attributes across calls for a sanitizer to catch. Only `chainman`
and `mempool` are reset for a reason a Python harness shares --
correctness, not memory safety -- and only conditionally, once a pass
ends with the block index or the mempool's own sequence changed from
what it started at, via `ResetChainmanAndMempool`
(`src/test/util/validation.cpp`, same sha). `_node` below takes neither
shape: `Node(...)` -- unstarted, no listener, no dial, the same
construction `tests/conftest.py`'s own `unstarted_node_context` uses --
is built once, lazily, and reused whole for every call in the process,
the way `g_setup` is, with nothing inside it rebuilt the way Core
rebuilds `connman`/`banman`/`addrman`/`peerman` every single pass, nor
reset the way `chainman`/`mempool` are on a dirty one. What that costs,
and why it is paid anyway, is argued below.

Building one costs orders of magnitude more than building a fresh
`Connection` and driving one message through it on a `Node` already
built -- opening the two `rocksdict` column families `Chainstate` and
`BlockDB` own is what dominates the first, measured with
`time.perf_counter` around each shape in a scratch script rather than
carried here as a figure that ages. `fuzz.yml`'s own per-harness arm --
`-max_total_time` there, which this file does not restate for the same
reason -- is what a difference of that order decides between: this
harness takes the cheap shape, one `Node`, many `Connection`s, one per
call and discarded at the end of it.

What that leaves unreset across calls -- `node.chainstate`,
`node.mempool`, `node.p2p_manager.peer_db` -- is a starker divergence
from Core than "rebuilt every pass" against "reset only when dirty":
none of the three is ever rebuilt or reset for the life of the process,
where `peer_db`'s own closest analogue, `addrman`, is one of the four
Core discards and rebuilds on every single call. That state is what a
real node carries across many peers' messages too, so a defect that only
shows up once some earlier call already changed it is exactly the kind
of finding a fresh `Node` every time would hide -- the same argument
Core's own conditional `chainman`/`mempool` reset already makes for
keeping those two across passes, just not for `addrman`. Rebuilding
`peer_db` alone every call, the way Core rebuilds `addrman`, was
measured against a bare `dispatch` call the same way the whole `Node`
build above was: real cost, a fresh `PeerDB` and the `rocksdict` store
under it costing several times what one `dispatch` call itself costs --
smaller than the order of magnitude the whole `Node` costs against a
call already on one, but not free enough to pay every one of many
calls a fuzzing run makes, for a bug shape nothing here yet has reason
to believe crosses through `peer_db`'s own history rather than
`handle_p2p`'s per-call input alone. What is reset explicitly below is
only what would otherwise grow without bound over a run of many calls
and never itself be read by anything under test: the
`Connection` just used, and this call's own entries in
`node.pending_getdata` and `node.pending_cfilters` -- both keyed by a
`Connection.id` that dies with it, and neither drained by this harness
the way `Node`'s own loop drains them through `p2p.main.resume_getdata`
and `resume_cfilters`, since draining one is a second call's worth of
work this harness does not schedule.

`p2p.main.handle_p2p_handshake` and `handle_p2p` are Core's
`PeerManagerImpl::ProcessMessage` (`net_processing.cpp`,
at bitcoin/bitcoin@ca7162cde5) -- `handle_p2p_handshake` and
`handle_p2p` covering between them the same "one message, whichever
table it dispatches through" Core folds into one function. Neither
raises what a callback raises: each wraps its own dispatch in
`except Exception as e`, discouraging the peer only for a
`BTClibException` and otherwise just logging --
`p2p/main.py`'s own comment there is why: a callback failing on content
that was fine is this node's own bug, not cause to drop the peer that
merely triggered it, and dropping the exception there in production
keeps one bad message from taking the whole loop down over it. A
harness that only calls `handle_p2p` and trusts what escapes it would
therefore never see that bug either -- `main.py`'s own `except` is
exactly the boundary this harness has to see past rather than trust,
which is what `_CrashCapture` below is for: a `logging.Handler`
attached directly to `node.logger` -- the one way `CLAUDE.md`'s own
*Non-obvious facts* says this tree's logger can be observed at all,
`node.logger` being a `Logger(logging.Logger)` built directly rather
than through `logging.getLogger()`, so nothing propagates to a root
handler such as `caplog`'s own -- reading `record.exc_info` off every
`node.logger.exception(...)` call `handle_p2p`/`handle_p2p_handshake`
themselves make, and sorting it onto the same two branches they
themselves already chose between, `isinstance(exc, BTClibException)` --
but keeping *both*, in `escaped` and `refused` rather than reading only
the one `handle_p2p` itself does not discourage the peer for. What
lands in `escaped` is re-raised by `dispatch` below exactly as before,
so `handle_p2p` runs unmodified, exactly as `Node`'s own loop calls it,
and what it would have hidden is still what makes this harness red; what
lands in `refused` is raised too, once `escaped` is checked and found
empty, so a callback's own refusal of `data` -- content `handle_p2p`
itself does not discourage the peer over, and so a call this harness
would otherwise report exactly as it reports genuine acceptance -- is
told apart from acceptance without being mistaken for a crash.

The command a message dispatches under, and the payload its handler
reads, are what `data` supplies; the header around them is not
fuzzed. `data[0]` selects one of the fixed set of commands
`p2p.callbacks.callbacks` and `handshake_callbacks` dispatch (`_TYPES`
below), by index rather than by whatever twelve octets `data` itself
might spell, and `data[1:]` is the payload -- `btclib.p2p.message.
Message(node.chain.magic, command, payload)` is what turns that pair
into a header always well-formed enough to reach a handler, the same
role Core's own `LIMIT_TO_MESSAGE_TYPE` plays for `process_message.cpp`
except applied structurally rather than through an environment variable
this tree's own `fuzz.yml` has no per-target corpus to key one on: Core
samples a command from the same twelve raw octets a `CMessageHeader`
carries, landing on one `ALL_NET_MESSAGE_TYPES` recognizes only by
chance, `LIMIT_TO_MESSAGE_TYPE` existing so OSS-Fuzz's own per-target
corpora can each hold that chance fixed at 1 instead. `frame_message`
(`p2p/connection.py`) and `Message.parse` are `fuzz_framing.py`'s own
target already, so refuzzing a header's magic, length and checksum
here would only retest that harness under a different name, at the
cost of every command missing it by chance the way Core's own raw
sampling does.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import shutil
import sys
import tempfile
from itertools import count
from pathlib import Path
from socket import socket
from typing import TYPE_CHECKING

from btclib.exceptions import BTClibException, BTClibValueError
from btclib.p2p.message import Message

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.callbacks import callbacks, handshake_callbacks
from btclib_node.p2p.connection import Connection
from btclib_node.p2p.main import handle_p2p, handle_p2p_handshake

if TYPE_CHECKING:
    from btclib_node.p2p.manager import P2pManager

# tests/fuzz_corpus_test.py reads this with ast.literal_eval rather than
# by importing this module: the other two harnesses' entry points are
# each importable from `btclib_node` itself, installed into every
# worktree's own `.venv`; `dispatch` below lives in `fuzz/` and is not,
# so `tests/fuzz_corpus_test.py`'s own `_resolve` loads this module by
# path instead of by `importlib.import_module` for a spec naming it --
# argued there.
ENTRY_POINTS = ("fuzz.fuzz_process_message:dispatch",)

# Every command `p2p.main.handle_p2p_handshake` or `handle_p2p` itself
# dispatches, sorted for a reproducible mapping from `data[0]` to a
# command across a run -- the union of `handshake_callbacks` and
# `callbacks`, `p2p/callbacks.py`'s own two tables, which share no key.
_TYPES: tuple[str, ...] = tuple(sorted({**handshake_callbacks, **callbacks}))

_connection_ids = count()
_node: Node | None = None


class _CrashCapture(logging.Handler):
    """Sort what `handle_p2p`'s own `except` logs the way it already did.

    `handle_p2p`/`handle_p2p_handshake` classify a callback's raise on
    one axis, `isinstance(e, BTClibException)`, to decide whether to
    discourage the peer -- this reads the same record and sorts it onto
    the same axis, into `refused` for a `BTClibException` and `escaped`
    for anything else, so `dispatch` below can tell "this call's callback
    refused `data`" apart from "this call's callback raised a bug" apart
    from "nothing was logged, `data` was accepted" -- three outcomes a
    boolean discourage/don't-discourage decision does not itself need to
    keep apart, but a fuzzer classifying a run of inputs does. The module
    docstring above is where the whole of this is argued; both lists are
    read and cleared by `dispatch` around every call, so neither ever
    holds more than the one call just made -- `handle_p2p` and
    `handle_p2p_handshake` each pop and dispatch exactly one message, so
    at most one of the two ever gains an entry in one call.
    """

    def __init__(self) -> None:
        super().__init__()
        self.escaped: list[BaseException] = []
        self.refused: list[BTClibException] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Sort `record.exc_info`'s own exception onto `refused`/`escaped`."""
        exc = record.exc_info[1] if record.exc_info else None
        if exc is None:
            return
        if isinstance(exc, BTClibException):
            self.refused.append(exc)
        else:
            self.escaped.append(exc)


_capture = _CrashCapture()


def _built_node() -> Node:
    """Return the one `Node` this process fuzzes against, building it once.

    `tempfile.mkdtemp()` and not a context manager: this process's own
    lifetime is the scope, the same as the `Node` itself, and nothing
    here ever closes either -- module docstring above. The directory
    `mkdtemp` returns is not removed by that alone, and this module's own
    `tests/fuzz_corpus_test.py` collects into the ordinary suite, so
    every `pytest` worker process that ever imports this module leaves
    one behind under the real `$TMPDIR` on every run, forever, unless
    something removes it -- measured directly: a subprocess that
    imports this module and calls `dispatch` once, then exits normally,
    left its own `fuzz-process-message-*` directory in place, holding
    the two `rocksdict` column families `Chainstate` and `BlockDB` open.
    `atexit.register` below is what an ordinary interpreter exit already
    runs on the way out, unlike `conn.client.close()` in `dispatch`,
    which fires every call rather than once. `ignore_errors=True` because
    this store is never closed before the process itself exits: at the
    moment the hook runs, both column-family handles are still open and
    RocksDB's own directory `LOCK` is still held, and whatever a platform
    refuses to unlink underneath an open handle is not worth an error
    printed from an exit hook nobody can act on.
    """
    global _node  # noqa: PLW0603
    if _node is None:
        data_dir = Path(tempfile.mkdtemp(prefix="fuzz-process-message-"))
        atexit.register(shutil.rmtree, data_dir, ignore_errors=True)
        node = Node(
            config=Config(
                chain="regtest",
                data_dir=data_dir,
                allow_p2p=False,
                allow_rpc=False,
                debug=True,
            )
        )
        node.status = NodeStatus.HeaderSynced
        node.logger.addHandler(_capture)
        _node = node
    return _node


def _connection_for(node: Node, command: str) -> Connection:
    """Build a fresh, unconnected `Connection` and register it for `command`.

    `Connection(...)` opens a real, never-connected socket -- the same
    construction `tests/unit/p2p/callbacks_test.py`'s own
    `a_real_connection` uses -- and `send` is overridden the same way
    that helper's own docstring argues: what it would hand to `conn.loop`
    is an event loop this harness never runs, so a message queued there
    is a coroutine `Connection._deliver` is created and never awaited,
    for as long as this process runs.

    `handshake_callbacks` needs `P2pConnStatus.Open`, `Connection`'s own
    default, and a place in `pending_connections`
    (`p2p.main.handle_p2p_handshake`'s own lookup); `callbacks` needs
    `Connected` and a place in `connections`
    (`p2p.main.handle_p2p`'s own lookup) -- never both, matching how a
    real connection is only ever in one of the two tables at once.
    """
    manager: P2pManager = node.p2p_manager
    conn = Connection(
        manager,
        socket(),
        peer_address("0.0.0.0", 18444),  # noqa: S104
        next(_connection_ids),
        inbound=False,
    )
    conn.send = lambda _msg: None  # type: ignore[method-assign]
    if command in handshake_callbacks:
        manager.pending_connections[conn.id] = conn
    else:
        conn.status = P2pConnStatus.Connected
        manager.connections[conn.id] = conn
    return conn


def dispatch(data: bytes) -> None:
    """Frame `data` as one p2p message and drive it through `handle_p2p`.

    Raises where `p2p.callbacks` or its own construction of the message
    refuses `data` -- a `BTClibException`, the family `fuzz_target` below
    suppresses the same way every other harness in this directory does,
    read back off `_capture.refused` where the refusal happened inside a
    callback rather than before `handle_p2p`/`handle_p2p_handshake` were
    even entered -- and re-raises whatever either of them logged but did
    not, off `_capture.escaped`: the module docstring above is where that
    split, and `_CrashCapture`, are argued. `_capture.escaped` is checked
    first, so a genuine crash is never misreported as a mere refusal
    where a call somehow left both non-empty.
    """
    if not data:
        err_msg = "no message-type selector byte"
        raise BTClibValueError(err_msg)
    command = _TYPES[data[0] % len(_TYPES)]
    payload = data[1:]
    node = _built_node()
    message = Message(node.chain.magic, command, payload)
    conn = _connection_for(node, command)
    manager = node.p2p_manager
    del _capture.escaped[:]
    del _capture.refused[:]
    try:
        conn.buffer += message.serialize()
        conn.parse_messages()
        if manager.handshake_messages:
            handle_p2p_handshake(node)
        elif manager.messages:
            handle_p2p(node)
    finally:
        manager.connections.pop(conn.id, None)
        manager.pending_connections.pop(conn.id, None)
        node.pending_getdata.pop(conn.id, None)
        node.pending_cfilters.pop(conn.id, None)
        # `conn.stop()` only schedules the close onto `conn.loop`
        # (`Connection.stop`/`_close`, `p2p/connection.py`), a loop this
        # harness never runs -- the same reason `send` is overridden
        # above -- so the raw socket `Connection.__init__` opened is
        # closed directly here instead. Left open, pytest's own
        # unraisable-exception hook reports the socket's own finalizer,
        # once garbage collection reaches it, as a failure of whichever
        # test happens to be running at that moment rather than of any
        # seed -- measured directly, against `tests/fuzz_corpus_test.py`
        # before this line was added.
        conn.client.close()
    if _capture.escaped:
        raise _capture.escaped[0]
    if _capture.refused:
        raise _capture.refused[0]


def fuzz_target(data: bytes) -> None:
    """Drive `data` through `dispatch`, the way every harness here does.

    Atheris reports a failure on any exception leaving this function, so
    what is suppressed here is what `dispatch` refusing an input looks
    like; the module docstring above is where that family is argued.
    """
    with contextlib.suppress(BTClibException):
        dispatch(data)


def main() -> None:
    """Wire `fuzz_target` to libFuzzer through atheris.

    `import atheris` deferred to here rather than sitting at module
    level with the other two harnesses' own: this module has to stay
    importable without it installed, for `tests/fuzz_corpus_test.py` to
    resolve `dispatch` -- `ENTRY_POINTS`'s own comment above is where
    that is argued.
    """
    import atheris  # noqa: PLC0415

    atheris.instrument_all()
    atheris.Setup(sys.argv, fuzz_target)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
