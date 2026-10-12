"""v1.5: ``human.prepare`` honours ``opts.idem`` (§0.7, §0.8.1): a retry gives the same request."""

from __future__ import annotations

from orch.host.control_client import new_idem
from orch.host.requests import MAX_PENDING
from tests.host import human_helpers
from tests.host.human_helpers import err, ok

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def test_a_retry_with_the_same_idem_returns_the_same_request(human, served):
    _, key = human.ticket()
    idem = new_idem()
    a = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    b = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    assert a["request"]["id"] == b["request"]["id"]
    assert a["request"]["code"] == b["request"]["code"]
    assert "duplicate" not in b
    hidem = new_idem()
    h1 = human.prepare("log", {"ref": key, "text": "h"}, handoff=True, idem=hidem)
    h2 = human.prepare("log", {"ref": key, "text": "h"}, handoff=True, idem=hidem)
    assert h1["request"]["code"] and h1["request"] == h2["request"]


def test_a_retry_shows_the_current_state(human, served):
    _, key = human.ticket()
    idem = new_idem()
    a = human.prepare("log", {"ref": key, "text": "x"}, handoff=True, idem=idem)
    assert a["request"]["state"] == "prepared"
    ok(human.c.req("human.get", {"request_id": a["request"]["id"], "signer": human.signer}))
    b = human.prepare("log", {"ref": key, "text": "x"}, handoff=True, idem=idem)
    assert b["request"]["id"] == a["request"]["id"] and b["request"]["state"] == "bound"


def test_other_arguments_are_idem_conflict(human, served):
    _, key = human.ticket()
    idem = new_idem()
    human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    assert err(human.prepare_res("log", {"ref": key, "text": "y"}, idem=idem)) == "idem.conflict"
    assert err(human.prepare_res("log", {"ref": key, "text": "x"}, handoff=True, idem=idem)) == "idem.conflict"


def test_another_principal_with_the_same_idem_gets_its_own_request(human, served):
    _, key = human.ticket()
    idem = new_idem()
    a = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    # a record of another principal under the same idem (same arguments) is no answer for this one
    foreign = {"op": "log", "args": {"ref": key, "text": "x"}, "signer": human.signer, "handoff": False}

    def other():
        out = served.service.outcomes
        intent = out.begin("uid:99999", idem, "human.prepare", foreign, planned=(), deferred=True)
        out.commit(intent, {"request": {"id": "hr_foreign"}})

    served.service.submit(other).result(10)
    b = human.prepare("log", {"ref": key, "text": "x"}, idem=idem)
    assert b["request"]["id"] == a["request"]["id"]
    c = human.prepare("log", {"ref": key, "text": "q"}, idem=new_idem())
    assert c["request"]["id"] != a["request"]["id"]


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
