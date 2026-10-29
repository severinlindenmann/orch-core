"""R2 T2b (#1590): the stream_open gate, the byte budget, the stream table and the pump (hub contract 10.2c).

The gate reads the policy and the device list from the workspace model under the store lock; the contract vectors
``gate_*`` and ``frame_cost_*`` are replayed against :func:`gate.open` and :func:`gate.frame_cost`."""

from __future__ import annotations

import contextlib
import json
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from orch.host.remote import enrollment, gate, pump
from orch.hubclient.tlslink.client import LinkError

CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "hub"
DOC = json.loads((CONTRACT / "vectors_remote.json").read_text())
NOW = 1_800_000_000_000
DEV = "rd_" + "a1" * 16
OWNER = "p_" + "0f" * 16
CODE = "rc_" + "c3" * 16
SID = "5e" * 16


def sid(n: int) -> str:
    return f"{n:032x}"


class Store:
    def __init__(self, remote: dict[str, Any] | None) -> None:
        owners = {OWNER, *(d["person"] for d in ((remote or {}).get("devices") or {}).values())}
        members = {p: SimpleNamespace(role="owner") for p in owners}  # every device's person is an owner here
        self.state = SimpleNamespace(workspace=SimpleNamespace(remote=remote, members=members))
        self.lock = threading.RLock()
        self.held = 0

    @contextlib.contextmanager
    def locked(self):
        with self.lock:
            self.held += 1
            try:
                yield
            finally:
                self.held -= 1


def service(on: bool = True, until: int | None = NOW + 86_400_000, devices: dict[str, Any] | None = None):
    devs = {DEV: {"person": OWNER, "expires_ms": NOW + 86_400_000, "level_cap": "work"}} if devices is None else devices
    remote = {"on": on, "until_ms": until, "devices": devs, "version": 1}
    svc = SimpleNamespace(store=Store(remote), clock=lambda: NOW / 1000, workspace_id="0" * 32)
    svc.terminator = mock.Mock()  # a spy: nothing may reach the terminator or the dashboard through the gate
    svc.submit = lambda fn, *a: _done(fn(*a))
    return svc


def _done(v: Any) -> Future:
    f: Future = Future()
    f.set_result(v)
    return f


def body(stream: str = SID, device: str = DEV, kind: str = "session") -> dict[str, str]:
    return {"stream_id": stream, "device_id": device, "kind": kind}


# --- the contract vectors ---------------------------------------------------------------------------------------------


def _prepared(c: dict[str, Any]) -> tuple[Any, gate.StreamTable]:
    """A service whose model, stream table and counters are exactly the vector's state."""
    a, st = c["args"], c["args"]["state"]
    r = st["remote"]
    devices = {d["device_id"]: d for d in st["devices"]["devices"]}
    svc = service(on=r["on"], until=r["expires_ms"], devices=devices)
    t = gate.table(svc)
    dev = a["body"]["device_id"]
    for i in range(max(st["streams_workspace"], st["streams_device"])):  # the device's streams count for the workspace
        t.register(sid(100 + i), dev if i < st["streams_device"] else "rd_" + "ee" * 16)
    for _ in range(st["handshakes_device"]):
        t.note_handshake(dev, a["now_ms"] - 1000)
    for _ in range(st["enrolls"]):
        t.note_enroll(a["now_ms"] - 1000)
    for s in st["seen_streams"]:
        assert t.remember(s, a["now_ms"] - 1000)
    return svc, t


GATE_CASES = [c for c in DOC["cases"] if c["check"] == "gate"]


@pytest.mark.parametrize("c", GATE_CASES, ids=[c["name"] for c in GATE_CASES])
def test_gate_vector(c: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    a, want = c["args"], c["expect"]
    svc, _ = _prepared(c)
    got = gate.open(svc, a["body"], a["now_ms"], codes=a["state"]["codes"])
    if "ok" not in want:
        assert got == want["error"], c.get("note")
        return
    assert got == "remote.version"  # it passed every check; R3 has not built the session yet
    monkeypatch.setattr(gate, "STREAMS_ACCEPT", True)  # with the switch flipped the same open is taken and registered
    svc2, t2 = _prepared(c)
    assert gate.open(svc2, a["body"], a["now_ms"], codes=a["state"]["codes"]) is None
    assert t2.is_open(a["body"]["stream_id"])


COST_CASES = [c for c in DOC["cases"] if c["check"] == "frame_cost"]


@pytest.mark.parametrize("c", COST_CASES, ids=[c["name"] for c in COST_CASES])
def test_frame_cost_vector(c: dict[str, Any]) -> None:
    assert {"cost": gate.frame_cost(c["args"]["signed"])} == c["expect"]


def test_constants_match_the_contract() -> None:
    k = DOC["constants"]
    assert gate.FRAME_MIN_COST == 1024
    assert gate.BYTE_BUDGET == {"life": 1 << 30, "per_min": 64 << 20}
    assert gate.LIMITS == {
        "streams_per_device": 4,
        "streams_per_workspace": 16,
        "handshakes_per_min": 10,
        "enrolls_per_min": 3,
    }
    assert k is not None


# --- the gate ---------------------------------------------------------------------------------------------------------


def test_open_while_off_remote_off_no_terminator_call(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = service(on=False)
    reg = mock.Mock()
    monkeypatch.setattr(gate.StreamTable, "register", reg)
    assert gate.open(svc, body(), NOW) == "remote.off"
    assert gate.open(svc, body(sid(2)), NOW) == "remote.off"
    reg.assert_not_called()
    assert svc.terminator.mock_calls == []
    assert gate.table(svc).count_workspace() == 0


def test_open_past_the_end_date_is_off() -> None:
    assert gate.open(service(until=NOW), body(), NOW) == "remote.off"
    assert gate.open(service(until=None), body(), NOW) == "remote.version"


def test_off_answered_before_device() -> None:
    svc = service(on=False, devices={})  # no devices at all, and an unknown one asked for
    assert gate.open(svc, body(device="rd_" + "99" * 16), NOW) == "remote.off"
    on = service(on=True, devices={})
    assert gate.open(on, body(device="rd_" + "99" * 16), NOW) == "remote.device"


def test_device_expired_or_bad_level_is_remote_device() -> None:
    svc = service(devices={DEV: {"person": OWNER, "expires_ms": NOW, "level_cap": "work"}})
    assert gate.open(svc, body(), NOW) == "remote.device"
    svc = service(devices={DEV: {"person": OWNER, "expires_ms": NOW + 1, "level_cap": "root"}})
    assert gate.open(svc, body(), NOW) == "remote.device"


def test_device_read_from_the_model_every_time() -> None:
    svc = service()
    assert gate.open(svc, body(sid(1)), NOW) == "remote.version"
    svc.store.state.workspace.remote["devices"].clear()  # the model changed (device removed)
    assert gate.open(svc, body(sid(2)), NOW) == "remote.device"
    svc.store.state.workspace.remote["on"] = False
    assert gate.open(svc, body(sid(3)), NOW) == "remote.off"


def test_gate_runs_under_the_store_lock() -> None:
    svc = service()
    seen: list[int] = []
    real = gate.model_remote.is_on

    def spy(core: Any, now: int) -> bool:
        seen.append(svc.store.held)
        return real(core, now)

    with mock.patch.object(gate.model_remote, "is_on", spy):
        gate.open(svc, body(), NOW)
    assert seen == [1]


def test_limits_4_16_10(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "STREAMS_ACCEPT", True)
    devs = {f"rd_{i:032x}": {"person": OWNER, "expires_ms": NOW + 3_600_000, "level_cap": "view"} for i in range(1, 7)}
    svc = service(devices=devs)
    ids = list(devs)
    n = iter(range(1, 1000))
    for _ in range(4):  # 4 per device
        assert gate.open(svc, body(sid(next(n)), ids[0]), NOW) is None
    assert gate.open(svc, body(sid(next(n)), ids[0]), NOW) == "remote.busy"
    for d in ids[1:4]:  # 4 + 3*4 = 16 per workspace
        for _ in range(4):
            assert gate.open(svc, body(sid(next(n)), d), NOW) is None
    assert gate.table(svc).count_workspace() == 16
    assert gate.open(svc, body(sid(next(n)), ids[4]), NOW) == "remote.busy"
    # 10 handshakes per minute per device, streams or not
    monkeypatch.setattr(gate, "STREAMS_ACCEPT", False)
    svc2 = service(devices=devs)
    for _ in range(10):
        assert gate.open(svc2, body(sid(next(n)), ids[5]), NOW) == "remote.version"
    assert gate.open(svc2, body(sid(next(n)), ids[5]), NOW) == "remote.busy"
    assert gate.open(svc2, body(sid(next(n)), ids[5]), NOW + 60_001) == "remote.version"  # a minute later


def test_enroll_rate_3_per_minute_and_codes() -> None:
    svc = service(on=False)
    codes = {CODE: NOW + 1000}
    for i in range(3):
        assert gate.open(svc, body(sid(i + 1), CODE, "enroll"), NOW, codes=codes) == "remote.version"
    assert gate.open(svc, body(sid(9), CODE, "enroll"), NOW, codes=codes) == "remote.busy"
    assert gate.open(service(on=False), body(sid(5), CODE, "enroll"), NOW) == "remote.no_code"  # default: no codes
    gate.CODES = lambda s, now: {CODE: NOW + 1000}
    try:
        assert gate.open(service(on=False), body(sid(6), CODE, "enroll"), NOW) == "remote.version"
    finally:
        gate.CODES = enrollment.outstanding


def test_stream_id_used_once() -> None:
    svc = service()
    assert gate.open(svc, body(), NOW) == "remote.version"
    assert gate.open(svc, body(), NOW) == "replay"
    assert gate.open(svc, body(), NOW + 12 * 3600 * 1000) == "remote.version"  # the 12 h window has passed


def test_seen_set_is_bounded() -> None:
    t = gate.StreamTable()
    for i in range(gate.SEEN_MAX):
        assert t.remember(sid(i), NOW)
    assert not t.remember(sid(99999), NOW)  # full: the open would be busy
    assert t.remember(sid(99999), NOW + gate.SEEN_TTL_MS)


def test_passing_open_answers_remote_version_until_r3() -> None:
    svc = service()
    t = gate.table(svc)
    assert gate.STREAMS_ACCEPT is False
    assert gate.open(svc, body(), NOW) == "remote.version"
    assert t.count_workspace() == 0 and not t.is_open(SID)  # never registered
    assert svc.terminator.mock_calls == []


def test_malformed_body_is_refused() -> None:
    assert gate.open(service(), {"stream_id": "x", "device_id": DEV, "kind": "session"}, NOW) == "malformed"
    assert gate.open(service(), body(device=CODE), NOW) == "malformed"  # a code id on a session


# --- the byte budget --------------------------------------------------------------------------------------------------


def test_budget_life_and_rolling_minute() -> None:
    b = gate.Budget(life=10_000, per_min=4096)
    assert b.charge(2048, 0) and b.charge(2048, 1)
    assert not b.charge(1024, 2)  # over the minute
    assert not b.charge(1, 70_000)  # once over, closed for good
    b = gate.Budget(life=10_000, per_min=4096)
    assert b.charge(4096, 0) and b.charge(4096, 61_000) and b.charge(1800, 122_000)  # a rolling minute
    assert not b.charge(1800, 122_001)  # life 10 000 exhausted


def test_budget_config_may_lower_never_raise() -> None:
    b = gate.Budget(life=1 << 40, per_min=1 << 40)
    assert (b.life, b.per_min) == (gate.BYTE_BUDGET["life"], gate.BYTE_BUDGET["per_min"])
    assert not b.charge(gate.BYTE_BUDGET["per_min"] + 1, 0)


def test_budget_over_closes_busy() -> None:
    t = gate.StreamTable()
    revoked: list[str] = []
    t.register(SID, DEV, revoked.append)
    big = {"stream_id": SID, "seq": 1, "bytes": "A" * 4}
    assert t.inbound(big, NOW, cost=gate.BYTE_BUDGET["per_min"]) is None
    big2 = {**big, "seq": 2}
    assert t.inbound(big2, NOW, cost=1024) == "remote.busy"
    assert revoked == ["remote.busy"] and not t.is_open(SID)
    assert t.take_closing() == [(SID, "remote.busy")]
    assert t.inbound({**big, "seq": 3}, NOW) is None  # a closed stream's frames are dropped


def test_inbound_seq_must_follow() -> None:
    t = gate.StreamTable()
    t.register(SID, DEV)
    assert t.inbound({"stream_id": SID, "seq": 1, "bytes": "AA"}, NOW) is None
    assert t.inbound({"stream_id": SID, "seq": 3, "bytes": "AA"}, NOW) == "malformed"
    assert not t.is_open(SID)


# --- revocation ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        ({"type": "remote.disabled", "by": "owner"}, "remote.off"),
        ({"type": "remote.changed"}, "revoked"),
        ({"type": "remote.key_rotated"}, "revoked"),
        ({"type": "hub.joined", "mode": "full"}, "remote.off"),
    ],
)
def test_on_commit_closes_every_stream(event: dict[str, Any], reason: str) -> None:
    svc = service()
    t = gate.table(svc)
    got: list[str] = []
    t.register(sid(1), DEV, got.append)
    t.register(sid(2), "rd_" + "bb" * 16, got.append)
    gate.on_commit(svc, "workspace", event)
    assert got == [reason, reason] and t.count_workspace() == 0
    assert sorted(t.take_closing()) == sorted([(sid(1), reason), (sid(2), reason)])


def test_on_commit_leaves_the_rest_alone() -> None:
    svc = service()
    t = gate.table(svc)
    t.register(sid(1), DEV)
    for ev in ({"type": "hub.joined", "mode": "selective"}, {"type": "ticket.created"}, {"type": "remote.code_issued"}):
        gate.on_commit(svc, "workspace", ev)
    gate.on_commit(svc, "ticket-log", {"type": "remote.disabled"})  # only the workspace log counts
    assert t.is_open(sid(1))


def test_on_commit_device_removed_closes_only_that_device() -> None:
    svc = service()
    t = gate.table(svc)
    t.register(sid(1), DEV)
    t.register(sid(2), "rd_" + "bb" * 16)
    gate.on_commit(svc, "workspace", {"type": "remote.device_removed", "device_id": DEV})
    assert not t.is_open(sid(1)) and t.is_open(sid(2))


def test_revoker_runs_inside_the_store_lock_and_one_failure_does_not_stop_the_rest() -> None:
    svc = service()
    t = gate.table(svc)
    held: list[int] = []
    t.register(sid(1), DEV, lambda r: (_ for _ in ()).throw(RuntimeError("x")))
    t.register(sid(2), DEV, lambda r: held.append(svc.store.held))
    with svc.store.locked():  # the commit hook is called from inside the store's critical section
        gate.on_commit(svc, "workspace", {"type": "remote.disabled", "by": "owner"})
    assert held == [1] and t.count_workspace() == 0


# --- the pump -----------------------------------------------------------------------------------------------


class FakeLink:
    def __init__(self, *frames: tuple[str, int, dict[str, Any]]) -> None:
        self.frames = list(frames)
        self.answers: list[tuple[int, str | None]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def next_stream(self, wait: float = 0.0):
        return self.frames.pop(0) if self.frames else None

    def answer_stream_open(self, seq: int, error: str | None = None) -> None:
        self.answers.append((seq, error))

    def send_stream(self, op: str, b: dict[str, Any]) -> None:
        self.sent.append((op, b))


def test_pump_answers_open_with_the_gate_code() -> None:
    svc = service(on=False)
    link = FakeLink(("stream_open", 7, body()))
    assert pump.pump(svc, link) is True
    assert link.answers == [(7, "remote.off")] and link.sent == []
    assert svc.terminator.mock_calls == []
    assert pump.pump(svc, link) is False


def test_pump_passing_open_is_remote_version() -> None:
    svc = service()
    link = FakeLink(("stream_open", 3, body()))
    pump.pump(svc, link)
    assert link.answers == [(3, "remote.version")]


def test_stream_data_unknown_stream_dropped() -> None:
    svc = service()
    link = FakeLink(
        ("stream_data", 4, {"stream_id": SID, "seq": 1, "bytes": "AAAA"}),
        ("stream_close", 5, {"stream_id": SID, "reason": "closed"}),
    )
    assert pump.pump(svc, link) and pump.pump(svc, link)
    assert link.answers == [] and link.sent == []  # nothing back: no reply, no close frame
    assert gate.table(svc).count_workspace() == 0


def test_pump_sends_close_frames_after_a_revocation() -> None:
    svc = service()
    t = gate.table(svc)
    t.register(SID, DEV)
    gate.on_commit(svc, "workspace", {"type": "remote.disabled", "by": "owner"})
    link = FakeLink()
    assert pump.pump(svc, link) is False
    assert link.sent == [("stream_close", {"stream_id": SID, "reason": "remote.off"})]


def test_pump_closes_on_bad_seq_and_sends_the_frame() -> None:
    svc = service()
    gate.table(svc).register(SID, DEV)
    link = FakeLink(("stream_data", 2, {"stream_id": SID, "seq": 5, "bytes": "AAAA"}))
    pump.pump(svc, link)
    assert link.sent == [("stream_close", {"stream_id": SID, "reason": "malformed"})]


def test_pump_survives_a_refused_close_frame() -> None:
    svc = service()
    gate.table(svc).register(SID, DEV)
    gate.table(svc).close_all("revoked")

    class Bad(FakeLink):
        def send_stream(self, op: str, b: dict[str, Any]) -> None:
            raise LinkError("malformed")

    assert pump.flush_closing(svc, Bad()) == 0
    assert gate.table(svc).take_closing() == []
