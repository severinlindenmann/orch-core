"""R4 host wiring: the ``unpublished`` seam over a real served workspace (its store holds the evidence through the
worker job), and held evidence shown to the owner, with what to do, in ``host.status.relay`` and in
``orch host status`` (interim for #572)."""

from __future__ import annotations

import types

import pytest

from orch.host import relayhook, relayr4
from orch.host import status as host_status
from orch.host.custody import write_private
from orch.relay import unpublished as up
from orch.wsproto.state import StateDir
from tests.host.human_helpers import lab, served  # noqa: F401 - fixtures
from tests.host.lifecycle_helpers import UID

from .test_relay_hook import _on_worker, _reserve

HELD = {"device_id": "ab" * 16, "person_id": "cd" * 16, "need": "compromised", "ver": 1,
        "digest": "sha256:" + "1" * 64, "at_ms": 1, "state": "revoke"}  # fmt: skip


def _write_held(svc, pairs, **extra):
    path = svc.state_dir / "hosts" / svc.workspace_id / "relay" / up.FILE
    doc = {"v": 2, "cursor": None, "scan": None, "gen": 1, "last_full_ms": 0, "notice": None, "held": pairs,
           "unverifiable": {"n": 0, "first_ms": 0, "last_seen_ms": 0, "clean_scans": 0}, "overflow": 0,
           "rebuilding": False, "last_complete_ms": 0, "failed_turns": 0, "ledger_seq": 1}  # fmt: skip
    write_private(path, {**doc, **extra})


def _held_notes(rel):
    """R4's attention notes: the first link's genesis-pin note (B1) is not about held evidence."""
    return [n for n in rel["attention"] if "genesis pin" not in n["what"]]


def test_host_status_relay_shows_held_evidence_with_what_to_do(served):  # noqa: F811
    svc = served.service
    _reserve(served)
    rel = relayhook.status(svc)
    assert rel["held_evidence"] == [] and _held_notes(rel) == []
    _write_held(svc, [HELD])
    rel = relayhook.status(svc)
    assert rel["held_evidence"] == [{**HELD, "actionable": True}]
    (note,) = _held_notes(rel)
    assert f"d_{'ab' * 16}" in note["what"] and "compromised" in note["what"] and "not published" in note["what"]
    assert "device.revoked" in note["fix"] and "#572" in note["fix"]
    _write_held(svc, [], unverifiable={"n": 2, "first_ms": 1, "last_seen_ms": 1, "clean_scans": 0})
    (note,) = _held_notes(relayhook.status(svc))
    assert "2 items the relay serves could not be verified" in note["what"] and rel["ledger"]["unverifiable"] == 0
    _write_held(svc, [], rebuilding=True)
    assert "being rebuilt" in _held_notes(relayhook.status(svc))[0]["what"]


def test_orch_host_status_names_held_evidence_as_a_problem(served):  # noqa: F811
    svc = served.service
    _reserve(served)
    _write_held(svc, [HELD])
    got = host_status._relay(svc.state_dir, served.wid, UID)  # noqa: SLF001
    assert got["held"] == [{**HELD, "actionable": True}]
    rep = {
        "state_dir": "s", "service": None, "host": {"answers": True, "control": "c"}, "here": None, "problems": [],
        "ok": True,
        "workspaces": [{"prefix": "DEMO", "workspace_id": served.wid, "state": "ok", "socket_answers": True,
                        "person": None, "click_to_sign": False, "root": "/r", "relay": got}],
    }  # fmt: skip
    assert "held_evidence=1" in host_status.render(rep)
    note = relayr4.held_attention("DEMO", got["held"], got["ledger"])
    assert note is not None and note["what"].startswith("DEMO: the relay holds 1 revocation this workspace")


def test_orch_host_status_report_lists_the_problem(served, monkeypatch):  # noqa: F811
    svc = served.service
    _reserve(served)
    _write_held(svc, [HELD, {**HELD, "device_id": "ef" * 16}])
    monkeypatch.setattr(host_status, "_service", lambda *a: None)
    rep = host_status.report(svc.state_dir, {}, svc.state_dir / "run" / "host" / "control.sock")
    probs = [p for p in rep["problems"] if "revocations this workspace has not published" in p["what"]]
    assert len(probs) == 1 and f"d_{'ef' * 16}" in probs[0]["what"] and "device.revoked" in probs[0]["fix"]
    assert not rep["ok"]


def test_an_unsafe_state_file_shows_nothing_and_never_fails_the_status(served):  # noqa: F811
    svc = served.service
    _reserve(served)
    path = svc.state_dir / "hosts" / svc.workspace_id / "relay" / up.FILE
    path.write_text('{"held": [1]}')
    path.chmod(0o644)  # unsafe mode: the checked read refuses
    assert relayr4.held_pairs_at(svc.state_dir, svc.workspace_id, UID) == []
    assert relayhook.status(svc)["held_evidence"] == []


def test_the_seam_holds_through_the_worker_job_on_the_real_store(served, tmp_path):  # noqa: F811
    """``unpublished(service, worker)``: every item goes through ``worker.job`` to the served store."""
    svc = served.service
    jobs = []

    def job(fn):
        jobs.append(fn)
        return svc.submit(fn).result(30)

    state = StateDir(tmp_path / "relay")
    try:
        loop = types.SimpleNamespace(notices={"unpublished": None})
        worker = types.SimpleNamespace(
            job=job, session=types.SimpleNamespace(gen=1), state=state, alarm=lambda c, d: None, loop=loop
        )
        c = relayr4.unpublished(svc, worker)
        assert isinstance(c, up.UnpublishedConsumer)
        assert c._notices() == {"unpublished": None}  # noqa: SLF001 - the bridge loop's last poll
        got = c._hold({"device_id": "00"})  # noqa: SLF001 - garbage: unverifiable, counted by R4
        assert got == "unverifiable" and len(jobs) == 1
        assert c._judge([("d_" + "00" * 16, "p_" + "11" * 16, "lost")]) == ["unknown"]  # noqa: SLF001
        assert c._evidence() == []  # noqa: SLF001
        worker.loop = None
        assert c._notices() is None  # noqa: SLF001
    finally:
        state.close()


def test_the_seam_without_a_store_has_no_result(served, tmp_path):  # noqa: F811
    svc = served.service
    state = StateDir(tmp_path / "relay")
    try:
        worker = types.SimpleNamespace(job=lambda fn: fn(), session=types.SimpleNamespace(gen=1), state=state,
                                       alarm=lambda c, d: None, loop=None)  # fmt: skip
        c = relayr4.unpublished(svc, worker)
        live = svc.store
        svc.store = None
        try:
            with pytest.raises(RuntimeError):
                c._hold({"device_id": "00"})  # noqa: SLF001 - no result: no ack, read again later
        finally:
            svc.store = live
    finally:
        state.close()


del _on_worker
