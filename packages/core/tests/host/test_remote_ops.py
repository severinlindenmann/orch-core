"""R2 T2a (#1590): the host's remote verbs, the one revocation point and the expiry sweeper.

The owner's unsigned verbs (off, code, device remove, key rotate) and reads, column P and a workspace owner only; every
line that ends access closes streams under the workspace store lock; a policy or a device past its end date is
swept. Turning remote access on and confirming a device are person ops (``test_remote_policy.py``)."""

from __future__ import annotations

from typing import Any

import pytest

from orch.crypto.encoding import b64u
from orch.host import remote, remotefacts
from orch.host.remote import devices, enrollment, policy, wrk
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.test_remote_policy import point, remote_args

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

DAY_MS = 86_400_000


def call(served, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return served.client().req(op, args or {}, idem=True)


def run_on(served, fn, *a):
    """Run ``fn`` on the workspace's worker thread, like an op does."""
    return served.service.submit(fn, *a).result(timeout=30)


def clock_ms(served) -> int:
    return int(served.lab.ws.clock[0] * 1000)


def turn_on(human, **over) -> None:
    human.run("remote.set", remote_args(**over))


def model(served) -> dict[str, Any]:
    return run_on(served, remote.remote_state, served.service)


def Pending_for(served, pid: str, n: int) -> None:  # noqa: N802
    """A pending device in memory (what a verified enroll message leaves), for tests that start at the confirmation."""
    pending = {
        "name": f"Laptop {n}", "transport_pub": b64u(bytes([n]) * 32), "presence_pub": point(),
        "code_id": "rc_" + f"{n:032x}",
    }  # fmt: skip
    now = clock_ms(served)
    enrollment._ws(served.service).pending[pid] = enrollment.Pending(  # noqa: SLF001
        pid=pid, workspace_id=served.service.workspace_id, code_id=pending["code_id"], stream_id="s" * 32,
        name=pending["name"], presence_kind="platform", transport_pub=pending["transport_pub"],
        presence_pub=pending["presence_pub"], fingerprint="0" * 64, words="", sign_count=0, expires_ms=now + 600_000,
    )  # fmt: skip


def add_device(human, served, n: int = 1, level: str = "work", days: int = 30) -> str:
    """Enroll through the real pieces: a pending device in memory, then the owner's confirmation with the proof."""
    pid = "rp_" + f"{n:032x}"
    Pending_for(served, pid, n)
    prepared, done, _ = human.run("remote.device_confirm", {"pending": pid, "level_cap": level, "days": days})
    return done["data"]["device_id"]


class Revoker:
    """Registered on a stream: records the reason and whether the workspace store lock was held at that moment."""

    def __init__(self, store) -> None:
        self.store, self.calls, self.held = store, [], []

    def __call__(self, reason: str) -> None:
        self.held.append(self.store.lock_held())  # this thread holds the workspace store lock right now
        self.calls.append(reason)


# --- who may ---


@pytest.mark.parametrize("op", sorted(remote.REMOTE_OPS))
def test_agent_column_g_refused_every_verb(served, op):
    args = {"device_id": "rd_" + "0" * 32} if op == "remote.device_remove" else {}
    res = served.client().req(op, args, grant=served.grant, session=SESSION, idem=True)
    assert res["ok"] is False and res["error"]["code"] == "op.not_allowed", res


def test_the_person_reads_and_off_when_never_on(human, served):
    st = ok(call(served, "remote.status"))["data"]
    assert st["on"] is False and st["devices"] == [] and st["codes"] == 0 and st["wrk_fp"] is None
    assert ok(call(served, "remote.off"))["data"] == {"changed": False}  # nothing to record
    assert ok(call(served, "remote.pending"))["data"] == {"pending": []}
    assert ok(call(served, "remote.sessions"))["data"] == {"sessions": []}


def test_a_bad_argument_and_a_missing_device_are_invalid_input(human, served):
    assert err(call(served, "remote.off", {"x": 1})) == "invalid.input"
    assert err(call(served, "remote.device_remove", {"device_id": "nope"})) == "invalid.input"
    assert err(call(served, "remote.device_remove", {"device_id": "rd_" + "0" * 32})) == "invalid.input"


def test_off_by_the_owner_records_a_host_line_and_status_says_why(human, served):
    turn_on(human)
    assert ok(call(served, "remote.status"))["data"]["on"] is True
    assert ok(call(served, "remote.off"))["data"] == {"changed": True}
    st = ok(call(served, "remote.status"))["data"]
    assert st["on"] is False and st["off_reason"] == "owner"
    assert model(served)["off_reason"] == "owner"


def test_off_from_device_seam_owner_only(human, served):
    turn_on(human)
    dev = add_device(human, served)
    with pytest.raises(Exception) as unknown:  # a device that is not listed cannot turn anything off
        run_on(served, policy.turn_off, served.service, "device", "rd_" + "9" * 32)
    assert unknown.value.code == "role.denied"
    assert model(served)["on"] is True
    assert run_on(served, policy.turn_off, served.service, "device", dev) is True  # an owner's own device
    r = model(served)
    assert r["on"] is False and r["off_reason"] == "device"
    assert run_on(served, policy.turn_off, served.service, "device", dev) is False  # already off


# --- revocation: one lock ---


def test_off_mid_session_closes_and_revokes_under_store_lock(human, served):
    turn_on(human)
    dev = add_device(human, served)
    table = remote.streams(served.service)
    a, b = Revoker(served.service.store), Revoker(served.service.store)
    table.register("a" * 32, dev, a)
    table.register("b" * 32, "rd_" + "7" * 32, b)
    ok(call(served, "remote.off"))
    assert a.calls == ["remote.off"] and b.calls == ["remote.off"]  # every stream, reason remote.off
    assert a.held == [True] and b.held == [True]  # the revokers ran INSIDE the store lock
    assert served.service.store.lock_held() is False  # and it is free again after the op
    assert sorted(table.take_closing()) == [("a" * 32, "remote.off"), ("b" * 32, "remote.off")]


def test_every_ending_line_closes_what_it_must(human, served):
    turn_on(human)
    d1, d2 = add_device(human, served, 1), add_device(human, served, 2)
    table = remote.streams(served.service)

    def open_three() -> tuple[Revoker, Revoker]:
        r1, r2 = Revoker(served.service.store), Revoker(served.service.store)
        table.register("1" * 32, d1, r1)
        table.register("2" * 32, d2, r2)
        return r1, r2

    r1, r2 = open_three()
    ok(call(served, "remote.device_remove", {"device_id": d1}))  # only that device's streams, reason revoked
    assert r1.calls == ["revoked"] and r1.held == [True] and r2.calls == []
    turn_on(human, level="view")  # any remote.changed, also a narrowing, closes all
    assert r2.calls == ["revoked"] and r2.held == [True]
    r3, r4 = open_three()
    r3.calls.clear()
    ok(call(served, "remote.key_rotate"))  # key_rotated closes all (and removes the devices)
    assert "revoked" in r4.calls and r4.held and all(r4.held)


def test_a_full_hub_join_closes_every_stream(human, served):
    turn_on(human)
    table = remote.streams(served.service)
    r = Revoker(served.service.store)
    table.register("1" * 32, "rd_" + "1" * 32, r)
    run_on(served, remote.on_commit, served.service, "workspace", {"type": "hub.joined", "mode": "full"})
    assert r.calls == ["remote.off"]
    r2 = Revoker(served.service.store)
    table.register("2" * 32, "rd_" + "1" * 32, r2)
    run_on(served, remote.on_commit, served.service, "workspace", {"type": "hub.joined", "mode": "selective"})
    assert r2.calls == []  # a selective join leaves sessions alone
    run_on(served, remote.on_commit, served.service, "ticket-uid", {"type": "remote.disabled"})
    assert r2.calls == []  # only the workspace log counts


def test_remote_sessions_lists_the_open_streams(human, served):
    turn_on(human)
    table = remote.streams(served.service)
    table.register("b" * 32, "rd_" + "2" * 32)
    table.register("a" * 32, "rd_" + "1" * 32)
    none = {"name": None, "person": None, "level": None, "since_ms": None}  # not opened: nothing is known yet
    rows = [
        {"stream_id": "a" * 32, "device_id": "rd_" + "1" * 32, **none},
        {"stream_id": "b" * 32, "device_id": "rd_" + "2" * 32, **none},
    ]
    assert ok(call(served, "remote.sessions"))["data"] == {"sessions": rows}


def test_the_commit_hook_runs_the_gate_rules_once_per_commit(human, served, monkeypatch):
    turn_on(human)
    seen: list[str] = []
    real = remote.gate.StreamTable.on_commit
    monkeypatch.setattr(
        remote.gate.StreamTable, "on_commit", lambda self, log, ev: (seen.append(ev["type"]), real(self, log, ev))
    )
    ok(call(served, "remote.off"))
    assert seen.count("remote.disabled") == 1


def test_a_failing_revoker_never_keeps_another_stream_open(human, served):
    turn_on(human)
    table = remote.streams(served.service)
    good = Revoker(served.service.store)

    def boom(reason: str) -> None:
        raise RuntimeError("revoker bug")

    table.register("1" * 32, "rd_" + "1" * 32, boom)
    table.register("2" * 32, "rd_" + "1" * 32, good)
    ok(call(served, "remote.off"))
    assert good.calls == ["remote.off"] and model(served)["on"] is False  # the commit stands too


# --- the expiry sweeper ---


def test_expiry_sweeper_disables_and_closes(human, served):
    turn_on(human, days=1)
    short = add_device(human, served, 1, days=30)
    table = remote.streams(served.service)
    r = Revoker(served.service.store)
    table.register("1" * 32, short, r)
    assert run_on(served, policy.sweep, served.service) == {"disabled": False, "removed": []}  # nothing due yet
    served.lab.ws.clock[0] += 2 * 86_400  # past the policy's end date, before the device's
    out = run_on(served, policy.sweep, served.service)
    assert out == {"disabled": True, "removed": []}
    assert model(served)["on"] is False and model(served)["off_reason"] == "expired"
    assert r.calls == ["remote.off"] and r.held == [True]
    served.lab.ws.clock[0] += 31 * 86_400  # the device runs out
    r2 = Revoker(served.service.store)
    table.register("2" * 32, short, r2)
    out = run_on(served, policy.sweep, served.service)
    assert out == {"disabled": False, "removed": [short]}
    assert model(served)["devices"] == {} and r2.calls == ["revoked"] and r2.held == [True]


def test_effective_policy_reads_the_end_date_before_the_sweeper_runs(human, served):
    turn_on(human, days=1)
    r = model(served)
    now = clock_ms(served)
    assert policy.effective(r, now).on is True
    p = policy.effective(r, now + 2 * DAY_MS)
    assert p.on is False and p.off_reason == "expired"
    assert policy.effective({}, now) == policy.Policy(False, None, None, None, None)
    assert policy.effective(None, now).on is False


def test_the_sweeper_thread_runs_a_pass_on_the_worker(human, served, monkeypatch):
    turn_on(human, days=1)
    served.lab.ws.clock[0] += 2 * 86_400
    sweeper = policy.Sweeper(served.service, interval=0.05)
    sweeper.start()
    try:
        for _ in range(100):
            if model(served)["on"] is False:
                break
            sweeper.join(0.05)
        assert model(served)["on"] is False
    finally:
        sweeper.stop()
        sweeper.join(2)


# --- WRK ---


def test_wrk_rotate_ends_sessions_and_removes_devices(human, served):
    turn_on(human)
    d1, d2 = add_device(human, served, 1), add_device(human, served, 2)
    first = ok(call(served, "remote.code"))["data"]
    old = wrk.load(served.service)
    assert old is not None
    table = remote.streams(served.service)
    r = Revoker(served.service.store)
    table.register("1" * 32, d1, r)
    out = ok(call(served, "remote.key_rotate"))["data"]
    new = wrk.load(served.service)
    assert new is not None and new != old and out["wrk_fp"] == wrk.wrk_fp(wrk.public(new)).hex()
    assert out["removed"] == sorted([d1, d2])
    assert r.calls[0] == "revoked" and all(r.held)  # sessions ended, under the lock
    assert model(served)["devices"] == {} and model(served)["wrk_fp"] == out["wrk_fp"]
    # R2 review L3: one line does it all, no separate removal lines follow the rotation
    kinds = [e["type"] for e, _ in served.service.store.events_after("workspace", 0, 1000)]
    assert kinds[-1] == "remote.key_rotated" and "remote.device_removed" not in kinds
    assert enrollment.outstanding(served.service, clock_ms(served)) == {}  # the old code pinned the old key
    assert first["code_id"] not in enrollment.outstanding(served.service, clock_ms(served))


def test_the_wrk_file_is_private_and_held_secret(human, served):
    ok(call(served, "remote.code"))
    path = wrk.path(served.service)
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    from orch.host import heldsecrets

    scalar = wrk.load(served.service)
    assert scalar in heldsecrets.held_secrets(served.service)
    assert b64u(scalar).encode() in heldsecrets.held_secrets(served.service)


def test_a_wrk_file_of_another_shape_counts_as_absent(human, served):
    ok(call(served, "remote.code"))
    wrk.path(served.service).write_text('{"v":2,"wrk":"x"}')
    wrk.path(served.service).chmod(0o600)
    assert wrk.load(served.service) is None


# --- codes ---


def test_a_code_is_shown_once_logged_without_its_secret_and_limited_to_four(human, served):
    first = ok(call(served, "remote.code"))["data"]
    assert first["code"].startswith(f"orch-rc1.{served.service.workspace_id}.")
    secret_b64 = first["code"].split(".")[2]
    assert len(secret_b64) >= 64 and first["expires_ms"] == clock_ms(served) + enrollment.CODE_TTL_MS
    events = [
        e for e, _ in served.service.store.events_after("workspace", 0, 1 << 30) if e["type"].startswith("remote.")
    ]
    assert [e["type"] for e in events][-1] == "remote.code_issued"
    assert secret_b64 not in str(events) and first["code"] not in str(events)
    for _ in range(3):
        ok(call(served, "remote.code"))
    assert err(call(served, "remote.code")) == "remote.full"  # at most 4 outstanding
    assert ok(call(served, "remote.status"))["data"]["codes"] == 4
    served.lab.ws.clock[0] += 16 * 60  # they expire
    ok(call(served, "remote.code"))


def test_the_pending_source_is_plugged_into_the_facts(human, served):
    assert remotefacts._pending_source is remote._pending_lookup  # noqa: SLF001


def test_devices_remove_and_lookup(human, served):
    turn_on(human)
    dev = add_device(human, served)
    r = model(served)
    now = clock_ms(served)
    owners = devices.owner_ids(served.service.store.state.workspace.members)
    assert devices.lookup(r, dev, now, owners)["level_cap"] == "work"
    assert devices.lookup(r, dev, r["devices"][dev]["expires_ms"], owners) is None  # expired: not listed
    assert devices.lookup(r, "rd_" + "5" * 32, now, owners) is None and devices.lookup(None, dev, now, owners) is None
    assert ok(call(served, "remote.device_remove", {"device_id": dev}))["data"] == {"device_id": dev, "removed": True}
    assert model(served)["devices"] == {}


# --- a person who is removed or stops being an owner: their devices never pass ---


def _host_event(served, who, typ: str, **payload) -> dict[str, Any]:
    """A person line appended by the host's own store on the worker (so the commit hook runs), like ``human.submit``."""
    ws, service = served.lab.ws, served.service
    ws.tick()

    def run() -> dict[str, Any]:
        store = service.store
        old_store, ws.store = ws.store, store
        old_check, store.append_check = store.append_check, None
        try:
            with store.locked():  # the state fresh (the lab's own store may have appended since)
                return store.append(ws.person_event(who, "workspace", typ, **payload), log="workspace").event
        finally:
            store.append_check, ws.store = old_check, old_store

    return run_on(served, run)


def _two_owners(human, served):
    """The first owner has a device and an open stream; a second owner exists. Returns (device, stream revoker, p2)."""
    turn_on(human)
    dev = add_device(human, served)
    p2 = served.lab.add_member()
    _host_event(served, served.lab.ws.owner, "role.changed", person=p2.ref, role="owner")
    r = Revoker(served.service.store)
    remote.streams(served.service).register("1" * 32, dev, r)
    return dev, r, p2


def _gate_open(served, dev: str, n: int) -> str | None:
    body = {"stream_id": f"{n:032x}", "device_id": dev, "kind": "session"}
    return run_on(served, lambda: remote.gate.open(served.service, body, clock_ms(served)))


def test_member_removed_closes_open_stream_and_refuses_new(human, served):
    dev, r, p2 = _two_owners(human, served)
    assert (
        _gate_open(served, dev, 1) == "remote.version"
    )  # an owner's device passes the gate (no adapter is attached: the host serves no session)
    _host_event(served, p2, "member.removed", person=served.lab.ws.owner.ref)
    assert r.calls == ["revoked"] and r.held == [True]  # closed inside the store lock
    assert _gate_open(served, dev, 2) == "remote.device"
    assert dev not in model(served)["devices"]


def test_owner_demoted_to_member_closes_open_stream_and_refuses_new(human, served):
    dev, r, p2 = _two_owners(human, served)
    _host_event(served, p2, "role.changed", person=served.lab.ws.owner.ref, role="member")
    assert r.calls == ["revoked"] and r.held == [True]
    assert _gate_open(served, dev, 2) == "remote.device"
    assert dev not in model(served)["devices"]


def test_lookup_refuses_device_of_non_owner(human, served):
    turn_on(human)
    dev = add_device(human, served)
    r, now = model(served), clock_ms(served)
    me = served.lab.ws.owner.ref
    assert devices.lookup(r, dev, now, {me})["device_id"] == dev
    assert devices.lookup(r, dev, now, set()) is None  # layer a alone: no commit hook ran, the check still refuses
    assert devices.lookup(r, dev, now, {"p_" + "9" * 32}) is None


def test_device_removed_reason_member_appended_once(human, served, monkeypatch):
    dev, _, p2 = _two_owners(human, served)
    lines: list[tuple[str, dict[str, Any]]] = []
    real = remote.append_host

    def spy(service, typ, **payload):
        lines.append((typ, payload))
        return real(service, typ, **payload)

    monkeypatch.setattr(remote, "append_host", spy)
    _host_event(served, p2, "role.changed", person=served.lab.ws.owner.ref, role="member")
    _host_event(served, p2, "role.changed", person=served.lab.ws.owner.ref, role="viewer")  # nothing left to drop
    assert lines == [("remote.device_removed", {"device_id": dev, "reason": "member"})]
    assert model(served)["devices"] == {}
