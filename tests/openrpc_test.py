# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `scripts/openrpc`, the diff against Core's OpenRPC document.

`test_the_committed_report_is_current` holds `divergences.txt` to what
`diff_openrpc.py` reports today, so a change to the RPC tables or to the
help texts rewrites it in the same pull request. The other tests drive
each comparison, and `fetch_core_openrpc.py` against a stand-in
`bitcoind`; the scripts are loaded by path, `scripts/` being no package.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

_DIR = Path(__file__).parents[1] / "scripts" / "openrpc"
_DOCUMENT = Path(__file__).parent / "_data" / "core_openrpc.json"


def _load(name: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _DIR / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves its annotations through sys.modules
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def diff(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Return `diff_openrpc.py`, loaded by path."""
    return _load("diff_openrpc", monkeypatch)


@pytest.fixture
def fetch(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Return `fetch_core_openrpc.py`, loaded by path."""
    return _load("fetch_core_openrpc", monkeypatch)


def test_the_committed_report_is_current(diff: ModuleType) -> None:
    """`divergences.txt` is what the script reports of this tree."""
    document = json.loads(_DOCUMENT.read_text(encoding="utf-8"))
    committed = (_DIR / "divergences.txt").read_text(encoding="utf-8")
    assert diff.diff(document) == committed, (
        "regenerate with: uv run python scripts/openrpc/diff_openrpc.py"
        " tests/_data/core_openrpc.json > scripts/openrpc/divergences.txt"
    )


def test_main_prints_the_report(
    diff: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command line prints the same report."""
    assert diff.main([str(_DOCUMENT)]) == 0
    assert capsys.readouterr().out == (_DIR / "divergences.txt").read_text(
        encoding="utf-8"
    )


_HELP = """\
mycall

Result (for verbose = false):
"hex"    (string) the hash

Result (for verbose = true):
{                       (json object)
  "a" : n,              (numeric) a number
                        continued on a second line
  "b" : {               (json object)
    "c" : "str"         (string)
  },
  "list" : [            (json array)
    {                   (json object)
      ...,              Same output as somewhere else
      "d" : {           (json object)
        "e" : n
      }
    },
    "str",              (string)
    ...
  ],
  "dyn" : {             (json object)
    "key" : n,          (numeric)
    ...
  }
}

Examples:
> bitcoin-cli mycall
"""


def test_help_results_reads_every_result_section(diff: ModuleType) -> None:
    """Each `Result` section is one value, nested as the text nests it."""
    leaf, obj = diff.help_results(_HELP)
    assert leaf.kind == "leaf"
    assert obj.kind == "object"
    assert sorted(obj.members) == ["a", "b", "dyn", "list"]
    assert sorted(obj.members["b"][0].members) == ["c"]
    assert not obj.open
    (element,) = obj.members["list"][0].items
    # the description after `...,` speaks for the objects inside too
    assert element.open
    assert element.members["d"][0].open
    # a bare `...` opens its object alone
    assert obj.members["dyn"][0].open
    assert not obj.members["dyn"][0].elided


def test_help_results_skips_a_text_without_a_result(diff: ModuleType) -> None:
    """A text with no `Result`, or an empty one, documents nothing."""
    assert diff.help_results("stop\n\nArguments:\n1. wait\n") == []
    assert diff.help_results("x\n\nResult:\n}\n\n") == []


def _obj(**props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "additionalProperties": False}


def test_fields_in_one_side_only_are_reported(diff: ModuleType) -> None:
    """A member on one side only is reported, with its side."""
    (_, node) = diff.help_results(_HELP)
    schema = _obj(
        a={"type": "number"},
        b=_obj(c={"type": "string"}, z={"type": "string"}),
        list={"type": "array", "items": _obj(d=_obj(e={"type": "number"}))},
        dyn={"type": "object", "additionalProperties": {"type": "number"}},
        new={"type": "boolean"},
    )
    assert diff.compare_fields("", [node], [schema]) == [
        ".b.z: in Core, not in help.py",
        ".new: in Core, not in help.py",
    ]
    del schema["properties"]["a"]
    assert diff.compare_fields("", [node], [schema])[0] == ".a: in help.py, not in Core"


def test_an_open_object_lacks_nothing(diff: ModuleType) -> None:
    """An object whose text elides members is not said to lack any."""
    (_, node) = diff.help_results(_HELP)
    element = node.members["list"][0].items
    schema = _obj(d=_obj(e={"type": "number"}), txid={"type": "string"})
    assert diff.compare_fields(".list[]", element, [schema]) == []


def test_alternatives_are_compared_together(diff: ModuleType) -> None:
    """Core's `oneOf` and `anyOf` alternatives are one set of fields."""
    (_, node) = diff.help_results(_HELP)
    b = node.members["b"]
    one_of = {"oneOf": [_obj(c={"type": "string"}), {"anyOf": [{"type": "null"}]}]}
    assert diff.compare_fields(".b", b, [one_of]) == []


def test_a_schema_saying_nothing_is_not_compared(diff: ModuleType) -> None:
    """Core's `ANY`, and a value help.py does not document, compare nothing."""
    (_, node) = diff.help_results(_HELP)
    assert diff.compare_fields("", [node], [{}]) == []
    assert diff.compare_fields("", [], [_obj()]) == []


def test_a_container_core_does_not_answer_is_reported(diff: ModuleType) -> None:
    """An object or array Core does not answer is reported as such."""
    (_, node) = diff.help_results(_HELP)
    assert diff.compare_fields("", [node], [{"type": "string"}]) == [
        "(result): an object in help.py, not in Core"
    ]
    lst = node.members["list"]
    assert diff.compare_fields(".list", lst, [{"type": "string"}]) == [
        ".list: an array in help.py, not in Core"
    ]


def test_fixed_array_items_are_compared_by_position(diff: ModuleType) -> None:
    """Core's fixed-length arrays give their items as a list."""
    (_, node) = diff.help_results(_HELP)
    lst = node.members["list"]
    fixed = {"type": "array", "items": [_obj(d=_obj(e={"type": "number"}))]}
    assert diff.compare_fields(".list", lst, [fixed]) == []


def test_parameters_and_their_aliases(diff: ModuleType) -> None:
    """A position matches by its name and its aliases, in order."""
    method = {
        "params": [
            {"name": "blockhash", "schema": {}},
            {"name": "verbosity", "x-bitcoin-aliases": ["verbose"], "schema": {}},
        ]
    }
    assert diff.compare_params("getblock", method) == []
    method["params"].pop()
    assert diff.compare_params("getblock", method) == [
        "getblock: Core (blockhash), btclib-node (blockhash, verbosity|verbose)"
    ]


def test_named_options_are_compared(diff: ModuleType) -> None:
    """The options of a named-parameter position are compared too."""
    method = {
        "params": [
            {"name": "path", "schema": {}},
            {"name": "type", "schema": {}},
            {"name": "options", "schema": _obj(rollback={}, in_memory={})},
        ]
    }
    assert diff.compare_params("dumptxoutset", method) == []
    method["params"][2]["schema"] = _obj(rollback={}, in_memory={}, other={})
    assert diff.compare_params("dumptxoutset", method) == [
        (
            "dumptxoutset options: Core (rollback, in_memory, other),"
            " btclib-node (rollback, in_memory)"
        )
    ]


def test_methods_on_one_side_only_are_listed(diff: ModuleType) -> None:
    """A method on one side only is listed, Core's with its category."""
    document = {
        "openrpc": "1.4.1",
        "info": {"version": "x"},
        "methods": [
            {
                "name": "getblockcount",
                "params": [],
                "result": {"schema": {"type": "number"}},
                "x-bitcoin-category": "blockchain",
            },
            {
                "name": "getbalance",
                "params": [],
                "result": {"schema": {"type": "number"}},
                "x-bitcoin-category": "wallet",
            },
        ],
    }
    report = diff.diff(document)
    assert report.startswith("OpenRPC 1.4.1 document, version x\n")
    assert "\nwallet getbalance\n" in report
    assert "\ngetbestblockhash\n" in report
    assert "getblockcount" not in report


def test_a_method_with_no_result_in_help_is_said_so(
    diff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A served method whose help text has no `Result` is listed."""
    monkeypatch.setitem(diff.HELP_TEXT, "getblockcount", "getblockcount\n")
    document = {
        "openrpc": "1.4.1",
        "info": {"version": "x"},
        "methods": [
            {
                "name": "getblockcount",
                "params": [],
                "result": {"schema": {"type": "number"}},
                "x-bitcoin-category": "blockchain",
            }
        ],
    }
    assert "getblockcount: no Result in help.py\n" in diff.diff(document)


# --- fetch_core_openrpc.py


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_wait_for_retries_until_an_answer(fetch: ModuleType) -> None:
    """A refused or warming-up call is asked again until it answers."""
    clock = _Clock()
    answers: list[object] = [OSError(), fetch.WarmingUpError(), "doc"]

    def call() -> object:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    got = fetch.wait_for(call, alive=lambda: True, clock=clock, sleep=clock.sleep)
    assert got == "doc"
    assert clock.now == pytest.approx(2 * fetch.POLL_INTERVAL)


def test_wait_for_gives_up_at_the_deadline(fetch: ModuleType) -> None:
    """A node that never answers fails once the deadline passes."""
    clock = _Clock()

    def call() -> object:
        raise OSError

    with pytest.raises(RuntimeError, match=r"did not answer within 1\.0s"):
        fetch.wait_for(
            call, alive=lambda: True, clock=clock, sleep=clock.sleep, timeout=1.0
        )
    assert clock.now >= 1.0


def test_wait_for_stops_when_the_process_exits(fetch: ModuleType) -> None:
    """A node that exits fails at once."""
    with pytest.raises(RuntimeError, match="exited"):
        fetch.wait_for(lambda: "doc", alive=lambda: False)


class _Handler(BaseHTTPRequestHandler):
    """Answer each call with the reply the server was given."""

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        self.server.requests.append(  # type: ignore[attr-defined]
            (self.headers["Authorization"], json.loads(self.rfile.read(length)))
        )
        status, reply = self.server.reply  # type: ignore[attr-defined]
        body = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server() -> Iterator[HTTPServer]:
    """Serve `_Handler` on a free loopback port."""
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def test_rpc_call_answers_the_result(
    fetch: ModuleType, server: HTTPServer, tmp_path: Path
) -> None:
    """A call carries the cookie and answers the result."""
    cookie = tmp_path / ".cookie"
    cookie.write_text("__cookie__:secret\n")
    server.reply = (200, {"result": {"ok": 1}, "error": None})  # type: ignore[attr-defined]
    port = server.server_address[1]
    assert fetch.rpc_call(port, cookie, "getopenrpcinfo", [True]) == {"ok": 1}
    ((auth, request),) = server.requests  # type: ignore[attr-defined]
    assert auth == "Basic X19jb29raWVfXzpzZWNyZXQ="
    assert request["method"] == "getopenrpcinfo"
    assert request["params"] == [True]


def test_rpc_call_raises_on_an_error(
    fetch: ModuleType, server: HTTPServer, tmp_path: Path
) -> None:
    """Warmup is the one error asked again; any other is raised."""
    cookie = tmp_path / ".cookie"
    cookie.write_text("u:p")
    port = server.server_address[1]
    warmup = {"result": None, "error": {"code": -28, "message": "Loading"}}
    server.reply = (500, warmup)  # type: ignore[attr-defined]
    with pytest.raises(fetch.WarmingUpError, match="Loading"):
        fetch.rpc_call(port, cookie, "x", [])
    other = {"result": None, "error": {"code": -32601, "message": "Method not found"}}
    server.reply = (404, other)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="Method not found"):
        fetch.rpc_call(port, cookie, "x", [])


def test_rpc_call_without_a_cookie_is_not_ready(
    fetch: ModuleType, tmp_path: Path
) -> None:
    """No cookie yet is an `OSError`, which `wait_for` asks again."""
    with pytest.raises(OSError, match="No such file"):
        fetch.rpc_call(fetch.free_port(), tmp_path / ".cookie", "x", [])


# A stand-in bitcoind: it writes its cookie where bitcoind does, and
# answers every call with the same document until it is terminated.
_FAKE_BITCOIND = """\
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

opts = dict(a.lstrip("-").split("=", 1) for a in sys.argv[1:] if "=" in a)
chain = Path(opts["datadir"]) / "regtest"
chain.mkdir(parents=True, exist_ok=True)

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"result": {"methods": ["é"]}, "error": None}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

httpd = HTTPServer(("127.0.0.1", int(opts["rpcport"])), H)
(chain / ".cookie").write_text("__cookie__:x")
httpd.serve_forever()
"""


def test_main_writes_the_document(
    fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The document is written indented, with a final newline."""
    bitcoind = tmp_path / "bitcoind.py"
    bitcoind.write_text(_FAKE_BITCOIND, encoding="utf-8")
    # Windows starts no script by its shebang, so the stand-in runs
    # under this Python, with the arguments `fetch` gives bitcoind
    popen = subprocess.Popen
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda args, **kwargs: popen([sys.executable, *args], **kwargs),
    )
    output = tmp_path / "core-openrpc.json"
    assert fetch.main([str(bitcoind), str(output)]) == 0
    assert output.read_text(encoding="utf-8") == '{\n  "methods": [\n    "é"\n  ]\n}\n'
