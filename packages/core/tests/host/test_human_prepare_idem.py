"""v1.5: ``human.prepare`` honours ``opts.idem`` (§0.7, §0.8.1): a retry gives the same request."""

from __future__ import annotations

from orch.host.control_client import new_idem
from orch.host.requests import MAX_PENDING
from tests.host import human_helpers
from tests.host.dispatch_helpers import UID
from tests.host.human_helpers import err, ok
from tests.host.test_human import requests_dir

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

#: the hub hand-off's op (a bundled add-on): ``human.prepare {handoff: true}`` stores only an ``addon.grant``
GRANT = {"name": "wiki"}


def test_a_retry_with_the_same_idem_returns_the_same_request(human, served):
    _, key = human.ticket()
    idem = new_idem()
    a = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    b = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    assert a["request"]["id"] == b["request"]["id"]
    assert "code" not in a["request"] and "code" not in b["request"]
    assert "duplicate" not in b
    hidem = new_idem()
    h1 = human.prepare("addon.grant", GRANT, handoff=True, idem=hidem)  # the hub hand-off: the one unbound prepare
    h2 = human.prepare("addon.grant", GRANT, handoff=True, idem=hidem)
    assert "code" not in h1["request"] and h1["request"] == h2["request"]


def test_a_retry_shows_the_current_state(human, served):
    idem = new_idem()
    a = human.prepare("addon.grant", GRANT, handoff=True, idem=idem)
    assert a["request"]["state"] == "prepared"
    ok(human.c.req("human.get", {"request_id": a["request"]["id"], "signer": human.signer}))
    b = human.prepare("addon.grant", GRANT, handoff=True, idem=idem)
    assert b["request"]["id"] == a["request"]["id"] and b["request"]["state"] == "bound"


def test_other_arguments_are_idem_conflict(human, served):
    _, key = human.ticket()
    idem = new_idem()
    human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    assert err(human.prepare_res("log", {"ref": key, "text": "y"}, idem=idem)) == "idem.conflict"
    # the signer or hand-off is part of the binding
    gidem = new_idem()
    human.prepare("addon.grant", GRANT, idem=gidem)
    assert err(human.prepare_res("addon.grant", GRANT, handoff=True, idem=gidem)) == "idem.conflict"


def _plant(served, principal, idem, args, namespace_store=None):
    """An outcome record of another principal (or workspace) under ``idem``, written through the store itself."""

    def run():
        out = namespace_store or served.service.outcomes
        intent = out.begin(principal, idem, "human.prepare", args, planned=(), deferred=True)
        out.commit(intent, {"request": {"id": "hr_foreign"}})

    served.service.submit(run).result(10)


def test_another_principal_who_used_the_idem_first_does_not_answer_this_one(human, served):
    _, key = human.ticket()
    idem = new_idem()
    foreign = {"op": "log", "args": {"ref": key, "text": "x"}, "signer": human.signer, "handoff": False}
    _plant(served, "uid:99999", idem, foreign)
    a = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    assert a["request"]["id"] != "hr_foreign"
    assert human.prepare("log", {"ref": key, "text": "x"}, idem=idem)["request"]["id"] == a["request"]["id"]
    # the other principal's record is untouched and still its own
    assert served.service.outcomes.lookup("uid:99999", idem)["result"]["request"]["id"] == "hr_foreign"


def test_a_grant_principal_is_no_uid_principal(human, served):
    _, key = human.ticket()
    idem = new_idem()
    body = {"op": "log", "args": {"ref": key, "text": "x"}, "signer": human.signer}
    assert err(human.c.req("human.prepare", body, grant=served.grant, idem=idem)) == "human_only"
    a = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)  # the refused grant call stored nothing under it
    assert a["request"]["id"]
    principal = f"g:{served.grant}@{UID}"
    foreign = {"op": "log", "args": {"ref": key, "text": "x"}, "signer": human.signer, "handoff": False}
    _plant(served, principal, idem, foreign)
    assert human.prepare("log", {"ref": key, "text": "x"}, idem=idem)["request"]["id"] == a["request"]["id"]


def test_another_workspace_has_its_own_outcome_namespace(human, served, tmp_path):
    from orch.host.outcomes import OutcomeStore

    _, key = human.ticket()
    idem = new_idem()
    other = OutcomeStore(tmp_path / "other", "ws_other", served.service.outcomes.host_uid, clock=served.service.clock)
    (tmp_path / "other").mkdir()
    other.open()
    foreign = {"op": "log", "args": {"ref": key, "text": "x"}, "signer": human.signer, "handoff": False}
    _plant(served, f"uid:{UID}", idem, foreign, namespace_store=other)
    a = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    assert a["request"]["id"] != "hr_foreign"
    assert other.key("ws_other", f"uid:{UID}", idem) != served.service.outcomes.key(served.wid, f"uid:{UID}", idem)


def test_a_request_that_is_no_longer_stored_answers_idem_expired(human, served):
    idem = new_idem()
    a = human.prepare("addon.grant", GRANT, handoff=True, idem=idem)
    ok(human.c.req("human.cancel", {"request_id": a["request"]["id"]}))
    (requests_dir(served) / a["request"]["id"]).unlink()
    assert err(human.prepare_res("addon.grant", GRANT, handoff=True, idem=idem)) == "idem.expired"


def test_the_person_is_part_of_the_binding(human, served):
    _, key = human.ticket()
    idem = new_idem()
    human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    out = served.service.outcomes
    rec = out.read(out.key(served.wid, f"uid:{UID}", idem))
    assert rec["args_normalised"]["person"] == human.person.ref
    assert rec["binding"].startswith("sha256:")


def test_a_failed_answer_does_not_leave_an_orphan_that_a_retry_duplicates(human, served, monkeypatch):
    _, key = human.ticket()
    idem = new_idem()
    out = served.service.outcomes
    real = out.commit
    boom = []

    def commit(intent, result):
        if not boom:
            boom.append(1)
            raise OSError("disk full")
        return real(intent, result)

    monkeypatch.setattr(out, "commit", commit)
    assert err(human.prepare_res("log", {"ref": key, "text": "x"}, idem=idem)) in {"internal", "host.unavailable"}
    assert len(list(requests_dir(served).iterdir())) == 1
    a = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    assert (
        len(list(requests_dir(served).iterdir())) == 1
        and a["request"]["id"] == next(requests_dir(served).iterdir()).name
    )
    assert human.prepare("log", {"ref": key, "text": "x"}, idem=idem)["request"]["id"] == a["request"]["id"]


def test_after_cancel_or_submit_the_same_idem_answers_the_outcome(human, served):
    _, key = human.ticket()
    i1, i2 = new_idem(), new_idem()
    p1 = human.prepare("log", {"ref": key, "text": "c"}, idem=i1)
    ok(human.c.req("human.cancel", {"request_id": p1["request"]["id"]}))
    again = human.prepare("log", {"ref": key, "text": "c"}, idem=i1)
    assert again["request"]["id"] == p1["request"]["id"] and again["request"]["state"] == "cancelled"
    p2 = human.prepare("log", {"ref": key, "text": "s"}, idem=i2)
    signed = human.sign(p2)
    ok(human.submit_res(p2["request"]["id"], signed.events))
    again = human.prepare("log", {"ref": key, "text": "s"}, idem=i2)
    assert again["request"]["id"] == p2["request"]["id"] and again["request"]["state"] == "submitted"


def test_no_idem_creates_a_new_request_each_time(human, served):
    _, key = human.ticket()
    a = human.prepare("log", {"ref": key, "text": "x"})
    b = human.prepare("log", {"ref": key, "text": "x"})
    assert a["request"]["id"] != b["request"]["id"]


def test_dry_run_is_unaffected_and_stores_nothing(human, served):
    _, key = human.ticket()
    idem = new_idem()
    d = human.prepare("log", {"ref": key, "text": "x"}, dry_run=True, idem=idem)
    assert d["request"] is None
    d2 = human.prepare("log", {"ref": key, "text": "other"}, dry_run=True, idem=idem)
    assert d2["request"] is None
    real = human.prepare("log", {"ref": key, "text": "z"}, idem=idem)  # nothing was stored by the dry runs
    assert real["request"]["id"]


def test_a_refused_prepare_stores_nothing_so_the_retry_runs_again(human, served):
    _, key = human.ticket()
    idem = new_idem()
    assert err(human.prepare_res("log", {"ref": key}, idem=idem)) == "invalid.input"
    assert human.prepare("log", {"ref": key, "text": "ok"}, idem=idem)["request"]["id"]


def test_the_pending_quota_counts_the_request_once(human, served):
    _, key = human.ticket()
    idem = new_idem()
    first = human.prepare("log", {"ref": key, "text": "n0"}, idem=idem)
    for _ in range(3):
        assert human.prepare("log", {"ref": key, "text": "n0"}, idem=idem)["request"]["id"] == first["request"]["id"]
    for i in range(1, MAX_PENDING):
        human.prepare("log", {"ref": key, "text": f"n{i}"})
    assert err(human.prepare_res("log", {"ref": key, "text": "extra"})) == "quota.host"
    # a retry at the quota still finds its request
    assert human.prepare("log", {"ref": key, "text": "n0"}, idem=idem)["request"]["id"] == first["request"]["id"]


def _fail_commit_once(served, monkeypatch):
    out = served.service.outcomes
    real = out.commit
    boom = []

    def commit(intent, result):
        if not boom:
            boom.append(1)
            raise OSError("disk full")
        return real(intent, result)

    monkeypatch.setattr(out, "commit", commit)


def test_a_restart_reconcile_keeps_the_request_of_a_stuck_prepare(human, served, monkeypatch):
    _, key = human.ticket()
    idem = new_idem()
    _fail_commit_once(served, monkeypatch)
    human.prepare_res("log", {"ref": key, "text": "x"}, idem=idem)
    rid = next(requests_dir(served).iterdir()).name
    done = served.service.submit(served.service.outcomes.reconcile_all).result(10)
    assert ("human.prepare", "committed") in done
    a = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    assert a["request"]["id"] == rid and a["request"]["state"] == "bound"
    assert len(list(requests_dir(served).iterdir())) == 1


def test_outcome_get_keeps_the_request_of_a_stuck_prepare(human, served, monkeypatch):
    _, key = human.ticket()
    idem = new_idem()
    _fail_commit_once(served, monkeypatch)
    human.prepare_res("log", {"ref": key, "text": "x"}, idem=idem)
    rid = next(requests_dir(served).iterdir()).name
    got = ok(human.c.req("outcome.get", {"idem": idem}))["data"]
    assert got["state"] == "committed" and got["result"]["request"]["id"] == rid
    assert human.prepare("log", {"ref": key, "text": "x"}, idem=idem)["request"]["id"] == rid
    assert len(list(requests_dir(served).iterdir())) == 1


def test_a_stuck_prepare_whose_request_is_long_gone_is_a_tombstone(human, served, monkeypatch):
    from tests.host.test_human import tick

    _, key = human.ticket()
    idem = new_idem()
    _fail_commit_once(served, monkeypatch)
    human.prepare_res("log", {"ref": key, "text": "x"}, idem=idem)
    (requests_dir(served) / next(requests_dir(served).iterdir()).name).unlink()
    tick(served, 25 * 3600)
    assert err(human.prepare_res("log", {"ref": key, "text": "x"}, idem=idem)) == "idem.expired"
    assert not list(requests_dir(served).iterdir())


def test_an_unreadable_request_does_not_hide_the_error_or_leave_the_key_running(human, served, monkeypatch):
    from orch.host.errors import HostError

    _, key = human.ticket()
    idem = new_idem()
    _fail_commit_once(served, monkeypatch)
    store = served.service.requests
    real = store.read
    monkeypatch.setattr(store, "read", lambda rid: (_ for _ in ()).throw(HostError("workspace.unavailable", "x")))
    assert err(human.prepare_res("log", {"ref": key, "text": "x"}, idem=idem)) != "outcome.unknown"
    monkeypatch.setattr(store, "read", real)
    assert not served.service.outcomes._running


def _person_cases(key):
    pol = {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False}
    return (
        ("move", {"ref": key, "to": "backlog"}, {"ref": key, "to": "open"}),
        ("policy.set", {"gates": {"plan": pol}}, {"gates": {"plan": {**pol, "count": 2}}}),
        ("settings.set", {"set": {"grant_hours": 4}}, {"set": {"grant_hours": 5}}),
    )


def test_prepare_idem_for_the_host_person_ops(human, served):
    _, key = human.ticket()
    for op, args, other in _person_cases(key):
        idem = new_idem()
        a = human.prepare(op, args, idem=idem)
        b = human.prepare(op, args, idem=idem)
        assert a["request"]["id"] == b["request"]["id"], op
        assert err(human.prepare_res(op, other, idem=idem)) == "idem.conflict", op


def test_every_op_prepare_accepts_also_works_with_an_idem(human, served):
    from orch.host import review
    from orch.host.dispatch import NOT_SERVED
    from orch.model.emits import EMITS_V1

    accepted = sorted(
        n for n in {*EMITS_V1, *review.HOST_PERSON_OPS} if review.person_op_types(n) and n not in NOT_SERVED
    )
    assert {"move", "policy.set", "settings.set", "log"} <= set(accepted)
    for op in accepted:
        # empty args are refused by the op's input check, but the idem step must not fail on an unknown operation first
        res = human.prepare_res(op, {}, idem=new_idem())
        if not res["ok"]:  # some ops prepare fine with no args; the rest are refused, never an internal error
            assert err(res) != "internal", op
