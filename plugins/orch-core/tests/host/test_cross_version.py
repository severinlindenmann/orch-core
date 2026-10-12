"""H7 cross-version fixtures: an older client against the current host (§0.4).

``fixtures/cross_version/v1_4_clients.json`` holds hello and request frames of the clients of contract v1.4 (the thin
CLI of H4b and the dashboard's host link at 8873a4f8), written by hand from their sources (not captured off a socket;
the file says how). Each is replayed byte for byte (after the
placeholders) on a real host. The rules checked: no shared major is ``fatal host.version_mismatch`` before any
``welcome``; an op the client expects at another version than the host serves is ``op.unsupported`` for that op only
(the connection keeps working, nothing is appended); a frame with an unknown envelope key is ``req.invalid``; every
key an old client reads is still sent with the same JSON type (v1.4 and v1.5 only add keys). Never a silent
mis-handling: a refused request appends nothing."""

from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import Any

import pytest

from orch.host import frames
from orch.host._socket import recv
from orch.host.client import Pin, connect_pinned
from orch.host.control_client import new_idem
from tests.host.dispatch_helpers import SESSION, UID, Served
from tests.host.lifecycle_helpers import Lab

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "cross_version" / "v1_4_clients.json").read_text())
JSON_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}


@pytest.fixture
def served(tmp_path):
    lab = Lab(tmp_path)
    try:
        s = Served(lab)
    except OSError as exc:  # pragma: no cover
        lab.close()
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    try:
        key = s.ticket("Cross-version")[1]
        maker = s.client()  # a task to finish, so a task.done would have something to append to
        steps = (
            ("claim", {"ref": key}),
            ("ac.add", {"text": "it works"}),
            ("task.add", {"text": "the one task"}),
            ("task.start", {"task": "T1"}),
        )
        for op, args in steps:
            res = maker.req(op, args, grant=s.grant, session=SESSION, idem=True)
            assert res["ok"], res
        s.key = key
        yield s
    finally:
        s.close()
        lab.close()


class Raw:
    """One connection that sends recorded frames as they are and reads frames back, nothing interpreted."""

    def __init__(self, served: Served) -> None:
        self.sock = connect_pinned(Pin(UID, served.wid, served.lab.run / served.wid / "host.sock"))
        self.sock.settimeout(10)
        self.decoder = frames.Decoder()
        self.pending: list[Any] = []

    def send(self, frame: dict[str, Any]) -> None:
        self.sock.sendall(frames.encode(frame))

    def read(self) -> dict[str, Any] | None:
        while not self.pending:
            data = recv(self.sock)
            if not data:
                return None
            self.pending.extend(self.decoder.feed(data))
        return self.pending.pop(0)

    def closed(self) -> bool:
        try:
            return self.read() is None
        except (TimeoutError, ConnectionError):
            return True

    def close(self) -> None:
        self.sock.close()


def fill(value: Any, served: Served) -> Any:
    subst = {"$GRANT": served.grant, "$SESSION": SESSION}
    if isinstance(value, dict):
        return {k: fill(v, served) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, served) for v in value]
    if isinstance(value, str):
        if value == "$IDEM":
            return new_idem()
        if value in subst:
            return subst[value]
        return value.replace("$KEY", served.key)
    return value


def hello(served: Served, name: str) -> tuple[Raw, dict[str, Any] | None]:
    raw = Raw(served)
    raw.send(FIXTURE["clients"][name]["hello"])
    return raw, raw.read()


def shape_ok(obj: Any, shape: dict[str, str]) -> list[str]:
    """The keys of ``shape`` missing from ``obj`` or sent with another JSON type (an int is never a bool)."""
    bad = []
    for key, typ in shape.items():
        if key not in obj:
            bad.append(f"{key} missing")
            continue
        value = obj[key]
        want = JSON_TYPES[typ]
        if (typ == "integer" and isinstance(value, bool)) or not isinstance(value, want):
            bad.append(f"{key}: {type(value).__name__}, not {typ}")
    return bad


def log_sizes(served: Served) -> dict[str, int]:
    root = Path(served.lab.root)
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*.jsonl")}


@pytest.mark.parametrize("case", FIXTURE["handshakes"], ids=[h["client"] for h in FIXTURE["handshakes"]])
def test_the_handshake_of_each_recorded_client(served, case):
    raw, first = hello(served, case["client"])
    try:
        expect = case["expect"]
        if "fatal" in expect:
            assert first is not None and first["t"] == "fatal", first
            assert first["error"]["code"] == expect["fatal"]
            assert raw.closed(), "the host closes after fatal"
            return
        assert first is not None and first["t"] == "welcome", first
        assert first["proto"] == expect["proto"]
        for op, version in (expect.get("ops_at") or {}).items():
            assert first["ops"][op] == version, op
    finally:
        raw.close()


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]])
def test_each_recorded_request_is_answered_per_the_version_rules(served, case):
    raw, welcome = hello(served, case["client"])
    try:
        assert welcome is not None and welcome["t"] == "welcome", welcome
        if case.get("after") == "task.done":  # the refusal first, on the same connection
            auth = {"grant": "$GRANT", "session": "$SESSION"}
            first = {"t": "req", "id": "c1", "op": "task.done", "args": {"task": "$KEY/T1"}, "auth": auth}
            raw.send(fill({**first, "opts": {"idem": "$IDEM"}}, served))
            assert raw.read()["error"]["code"] == "op.unsupported"
        before = log_sizes(served)
        raw.send(fill(case["req"], served))
        res = raw.read()
        expect = case["expect"]
        assert res is not None and res["t"] == "res", res
        assert res["ok"] is expect["ok"], res
        if not expect["ok"]:
            assert res["error"]["code"] == expect["code"], res
            if "appends" in expect:
                assert log_sizes(served) == before, "a refused request appended"
            if expect["code"] == "op.unsupported":
                assert "version" in res["error"]["message"]
            raw.send(fill({"t": "req", "id": "z9", "op": "status", "args": {},
                           "auth": {"grant": "$GRANT", "session": "$SESSION"}}, served))  # fmt: skip
            assert raw.read()["ok"] is True, "the connection keeps working after the refusal"
            return
        result = res["result"]
        assert not shape_ok(result, expect.get("shape") or {}), shape_ok(result, expect["shape"])
        data = result["data"]
        assert not shape_ok(data, expect.get("data_shape") or {}), shape_ok(data, expect["data_shape"])
        if "row_shape" in expect:
            rows = data["tickets"] if "tickets" in data else data["rows"]
            assert rows, "the setup made a ticket"
            assert not shape_ok(rows[0], expect["row_shape"]), shape_ok(rows[0], expect["row_shape"])
        if "grant_shape" in expect:
            assert data["grants"]
            assert not shape_ok(data["grants"][0], expect["grant_shape"]), data["grants"][0]
    finally:
        raw.close()


def test_the_fixture_covers_the_rules():
    """Every rule of §0.4 an old client can meet has a recorded case."""
    codes = {c["expect"].get("code") for c in FIXTURE["cases"]}
    assert {"op.unsupported", "req.invalid"} <= codes
    assert any("fatal" in h["expect"] for h in FIXTURE["handshakes"])
    assert {"cli-v1.4", "dashboard-v1.4"} <= {c["client"] for c in FIXTURE["cases"]}


def test_a_current_cli_against_an_older_host_degrades_its_dry_run_and_never_guesses():
    """The other direction (a host before H7 sends no ``result`` on a dry-run ``human.prepare``): the CLI names what
    would be signed instead of inventing the op's result."""
    import orch.ops as ops
    from orch.hostclient import dry_text

    op = ops.get("close")
    old = dry_text(op, {"request": None, "events": [{"type": "ticket.closed"}], "fingerprint": "0" * 12})
    assert old.lines == ["dry-run: nothing was written", "would sign: ticket.closed"]
    new = dry_text(op, {"request": None, "events": [], "result": {"data": {"x": 1}, "key": "DEMO-0001", "seq": 3}})
    assert (new.key, new.seq, new.data) == ("DEMO-0001", 3, {"x": 1})
