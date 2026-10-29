"""R11 P09: approving the requirements and the plan of an existing ticket with ONE signature (host/seqgates.py,
kind ``approve_gates``): two ordinary ``approve`` steps, one review, the gate rules of core unchanged."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from orch import canon
from orch.host import seqgates, sequence, signcontent, signer
from orch.host.sequence import Aborted, SequenceInvalid
from tests.host import human_helpers
from tests.host.dispatch_helpers import err
from tests.host.human_helpers import ok
from tests.host.test_click import click  # noqa: F401
from tests.host.test_sequence import Rig, doc, intent_of
from tests.host.test_sequence_click import prepare as click_prepare
from tests.host.test_sequence_edit import edit_intent

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

pytestmark = pytest.mark.slow  # a host


def ready(human, tmp_path):
    """A chore with text for both gates, a criterion and a task: the requirements and the plan are both approvable."""
    rig = Rig(
        human,
        tmp_path,
        intent_of(
            ticket_type="chore",
            title="Add a hello.txt",
            set={},
            sections={"summary": "One line.", "context": "Why.", "requirements": "Write hello.txt.", "plan": "Do it."},
            acceptance=["hello.txt exists"],
        ),
    )
    key = rig.driver().run(rig.first())["key"]
    task = {
        "add": [{"text": "Write the file", "verify": "test -f hello.txt", "proves": ["AC1"]}],
        "edit": {},
        "was": [],
    }
    intent = edit_intent(
        human, key, tasks=task, acceptance={"was": [{"id": "AC1", "text": "hello.txt exists"}], "edit": {}, "add": []}
    )
    erig = Rig(human, tmp_path / "e", intent)
    erig.driver().run(erig.first())
    return key


def gates_intent(human, key, **over):
    """The intent the dashboard server builds: ticket.get plus the host's gate.input of both gates."""
    data = ok(human.c.req("ticket.get", {"ref": key}))["data"]
    inputs = {g: ok(human.c.req("gate.input", {"ref": key, "gate": g}))["data"] for g in seqgates.GATES}
    return {**seqgates.intent_of_ticket(data, inputs), **over}


def rig_for(human, tmp_path, key, **over):
    return Rig(human, tmp_path / "g", gates_intent(human, key, **over))


def test_both_gates_are_approved_by_one_run_with_two_ordinary_events(human, tmp_path):
    key = ready(human, tmp_path)
    rig = rig_for(human, tmp_path, key)
    out = rig.driver().run(rig.first())
    assert out["landed"] == 2 and len(rig.submitted) == 2
    got = doc(human, key)
    assert got["gates"]["requirements"]["state"] == "approved" and got["gates"]["plan"]["state"] == "approved"
    events = [e for _, evs, _ in rig.submitted for e in evs]
    assert [(e["type"], e["gate"]) for e in events] == [("gate.approved", "requirements"), ("gate.approved", "plan")]
    # the plan approval follows the requirements approval this signer signed (the host's prior names exactly it)
    prior = rig.prepared[0]["material"]["input"]["prior"]["requirements"]["approvals"]
    assert prior == [events[0]["id"]]


def test_each_event_equals_what_a_single_gate_approval_signs(human, tmp_path):
    key = ready(human, tmp_path)
    single = human.prepare("approve", {"ref": key, "gate": "requirements"})
    rig = rig_for(human, tmp_path, key)
    first = rig.first()
    a, b = single["events"][0], first["events"][0]
    # same type, gate, gate_gen, hash and policy_hash: the sequence adds nothing and changes no gate rule
    assert {k: a[k] for k in ("type", "gate", "gate_gen", "hash", "policy_hash")} == {
        k: b[k] for k in ("type", "gate", "gate_gen", "hash", "policy_hash")
    }
    assert sequence.expected_payload(rig.intent, 0, rig.person) == {"type": "gate.approved", "gate": "requirements"}
    assert sequence.expected_payload(rig.intent, 1, rig.person) == {"type": "gate.approved", "gate": "plan"}


def test_the_view_shows_both_texts_in_full(human, tmp_path):
    key = ready(human, tmp_path)
    rig = rig_for(human, tmp_path, key)
    first = rig.first()
    human.check(first)
    content = signcontent.build(first, verified=True)
    print("SECTIONS", [(s["id"], s["title"]) for s in content["sections"]])
    text = {s["id"]: s["text"] for s in content["sections"]}
    assert text["requirements"] == "Write hello.txt." and text["plan:plan"] == "Do it."
    assert content["headline"].startswith("Approve requirements and plan of DEMO-")
    assert content["ticket"]["title"] == "Add a hello.txt"
    assert [t["title"] for t in content["tasks"]] == ["Write the file (proves AC1)"]
    assert content["acceptance"][0]["text"] == "hello.txt exists"
    assert any("two approvals" in n for n in content["notes"])
    full = signer.render(first, lambda w: ("ws", "DEMO"))
    assert "Write hello.txt." in full and "2 signatures, ONE confirmation" in full


def test_agents_are_refused_at_step_zero(human, served, tmp_path):
    key = ready(human, tmp_path)
    rig = rig_for(human, tmp_path, key)
    op, args = sequence.step_args(rig.intent, 0, None)
    res = human.c.req("human.prepare", {"op": op, "args": args, "sequence": rig.meta0()}, grant=served.grant)
    assert err(res) == "human_only"


def test_a_text_changed_after_the_review_is_refused_before_anything_is_signed(human, tmp_path):
    key = ready(human, tmp_path)
    intent = gates_intent(human, key)
    was = canon.section_hash("Do it.")
    edit = Rig(
        human, tmp_path / "x", edit_intent(human, key, sections={"plan": {"was": was, "text": "Do it differently."}})
    )
    edit.driver().run(edit.first())
    rig = Rig(human, tmp_path / "g2", intent)  # the old review: its head is behind
    res = human.c.req(
        "human.prepare",
        {
            "op": "approve",
            "args": {"ref": key, "gate": "requirements"},
            "signer": human.signer,
            "sequence": rig.meta0(),
        },
    )
    assert err(res) == "human.request_mismatch"
    assert doc(human, key)["gates"]["requirements"]["state"] != "approved"


def test_a_foreign_event_between_the_steps_aborts_and_leaves_requirements_approved_only(human, tmp_path):
    key = ready(human, tmp_path)
    rig = rig_for(human, tmp_path, key)
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        if args.get("gate") == "plan":  # C: anything but this signer's own step-0 event refuses step 1
            ok(human.c.req("log", {"ref": key, "message": "foreign"}, grant=human.served.grant, session="s", idem=True))
        return real(op, args, meta)

    rig.port.prepare = prepare
    with pytest.raises(Aborted):
        rig.driver().run(first)
    got = doc(human, key)["gates"]
    assert got["requirements"]["state"] == "approved" and got["plan"]["state"] != "approved"
    assert sum(1 for _, evs, _ in rig.submitted for e in evs if e["type"] == "gate.approved") == 1


def _tamper(human, tmp_path, mutate):
    key = ready(human, tmp_path)
    rig = rig_for(human, tmp_path, key)
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        got = copy.deepcopy(real(op, args, meta))
        if args.get("gate") == "plan":
            inp = got["material"]["input"]
            mutate(inp, got)
            try:  # a consistent hash where one exists (canon refuses an extra key): only the content check can refuse
                got["events"][0]["hash"] = canon.gate_hash(inp)
            except canon.HashError:
                pass
        return got

    rig.port.prepare = prepare
    check = rig.port.check
    # the signer's own hash check is skipped for the tampered step: only the sequence's content check is under test
    rig.port.check = lambda p: None if p["events"][0].get("gate") == "plan" else check(p)
    with pytest.raises(Aborted, match="changed"):
        rig.driver().run(first)
    assert sum(1 for _, evs, _ in rig.submitted for e in evs if e["type"] == "gate.approved") == 1


def test_prior_naming_another_event_is_refused(human, tmp_path):
    _tamper(human, tmp_path, lambda i, g: i["prior"]["requirements"].update(approvals=["01ARZ3NDEKTSV4RRFFQ69G5FAV"]))


def test_prior_with_an_extra_approval_is_refused(human, tmp_path):
    _tamper(human, tmp_path, lambda i, g: i["prior"]["requirements"]["approvals"].append("01ARZ3NDEKTSV4RRFFQ69G5FAV"))


def test_prior_with_an_extra_key_is_refused(human, tmp_path):
    _tamper(human, tmp_path, lambda i, g: i["prior"]["requirements"].update(extra=1))


def test_prior_missing_is_refused(human, tmp_path):
    _tamper(human, tmp_path, lambda i, g: i.update(prior={}))


def test_a_plan_text_other_than_the_reviewed_one_is_refused(human, tmp_path):
    def swap(inp, got):
        inp["sections"]["plan"] = canon.section_hash("Something else.")
        got["material"]["texts"]["plan"] = "Something else."

    _tamper(human, tmp_path, swap)


def _lie(human, tmp_path, step, mutate):
    """A lying host changes what a gate binds that the View never shows, then recomputes the hash, the review, the
    digest and the fingerprint so that every host-side consistency check passes. The signer's own check (NOT skipped)
    and the sequence's content check must still refuse before anything is signed at that step."""
    from orch.host import review as rv

    key = ready(human, tmp_path)
    rig = rig_for(human, tmp_path, key)
    first = rig.first()
    real = rig.prepare
    signed = []

    def lie(got):
        inp = got["material"]["input"]
        mutate(inp)
        got["events"][0]["hash"] = canon.gate_hash(inp)
        got["material"]["review"] = [rv.of(e, got["material"]) for e in got["events"]]
        got["review_digest"] = rv.review_digest(got["material"])
        got["fingerprint"] = rv.fingerprint(got["review_digest"], got["events"], got["op"])
        return got

    def prepare(op, args, meta):
        got = copy.deepcopy(real(op, args, meta))
        return lie(got) if args.get("gate") == step else got

    rig.port.prepare = prepare
    sign = rig.port.sign
    rig.port.sign = lambda p: (signed.append(p["events"][0]["gate"]), sign(p))[1]
    return rig, first, signed


_ART = {"spec": {"kind": "report", "digest": "sha256:" + "a" * 64, "ac": None, "task": None}}


@pytest.mark.parametrize("step", ["requirements", "plan"])
@pytest.mark.parametrize(
    "mutate",
    [
        lambda i: i["artifacts"].update(_ART),
        lambda i: i["fields"].update(size="xl"),
        lambda i: i["fields"].update(addons={"x": {"y": 1}}),
        lambda i: i.update(addon_packages={"x": "sha256:" + "b" * 64}),
    ],
    ids=["artifact", "size", "addon-field", "addon-package"],
)
def test_m1_a_lying_host_cannot_add_what_the_view_never_shows(human, tmp_path, step, mutate):
    rig, first, signed = _lie(human, tmp_path, step, mutate)
    if step == "requirements":
        # step 0 is the request the person confirmed: it is checked by signer.check (sequence content check) at once
        with pytest.raises(Exception):  # noqa: B017,PT011 - SignerRefused or Aborted: nothing may be signed
            human.check(lie_first(rig, first, mutate))
        return
    with pytest.raises(Aborted):
        rig.driver().run(first)
    assert signed == ["requirements"]  # the plan was never signed


def lie_first(rig, first, mutate):
    import copy as _c

    from orch.host import review as rv

    got = _c.deepcopy(first)
    inp = got["material"]["input"]
    mutate(inp)
    got["events"][0]["hash"] = canon.gate_hash(inp)
    got["material"]["review"] = [rv.of(e, got["material"]) for e in got["events"]]
    got["review_digest"] = rv.review_digest(got["material"])
    got["fingerprint"] = rv.fingerprint(got["review_digest"], got["events"], got["op"])
    return got


def test_m1_the_size_is_in_the_intent_and_in_the_view(human, tmp_path):
    key = ready(human, tmp_path)
    assert gates_intent(human, key)["size"] is None
    rig = rig_for(human, tmp_path, key, size="m")  # the person reviewed size m, the host's gates say none: refused (L1)
    res = human.c.req(
        "human.prepare",
        {
            "op": "approve",
            "args": {"ref": key, "gate": "requirements"},
            "signer": human.signer,
            "sequence": rig.meta0(),
        },
    )
    assert err(res) == "human.request_mismatch"


def test_l2_the_step_one_failure_says_the_requirements_are_approved_and_the_plan_waits(click, served, human, tmp_path):  # noqa: F811
    key = ready(human, tmp_path)
    _, p = click_prepare(click, gates_intent(human, key))
    v = click.view(p["request_id"])
    click.mono.t += 2
    state = {}

    def tamper(op, args, res):
        if op == "human.submit" and "n" not in state:  # after step 0 landed, somebody else writes
            state["n"] = 1
            human.agent("log", {"ref": key, "message": "foreign"})
        return res

    click.tamper = tamper
    with pytest.raises(Exception) as e:  # noqa: PT011
        click.sign(p["request_id"], v["view_digest"])
    assert "The requirements are approved; the plan waits for you." in str(e.value)


def test_the_digest_is_stable_and_binds_both_texts(human, tmp_path):
    key = ready(human, tmp_path)
    one = sequence.validate_intent(gates_intent(human, key))
    again = sequence.validate_intent(gates_intent(human, key))
    other = copy.deepcopy(one)
    other["plan"]["sections"]["plan"] = "Do it differently."
    d = lambda i: sequence.digest_of("a" * 32, "p_" + "1" * 32, i)  # noqa: E731
    assert d(one) == d(again) and d(one) != d(other)


def test_one_click_signs_both_approvals_and_the_headline_is_the_signers_own(click, served, human, tmp_path):  # noqa: F811
    key = ready(human, tmp_path)
    _, p = click_prepare(click, gates_intent(human, key))
    v = click.view(p["request_id"])
    assert v["view"]["headline"] == f"Approve requirements and plan: {key}"
    assert v["view"]["headline_detail"] == "\u201cAdd a hello.txt\u201d"
    assert v["view"]["risk_lines"] == [] and v["view"]["confirm"] == {"kind": "click"}
    assert {s["id"] for s in v["view"]["content"]["sections"]} >= {"requirements", "plan:plan"}
    click.mono.t += 2
    click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 2
    gates = doc(human, key)["gates"]
    assert gates["requirements"]["state"] == "approved" and gates["plan"]["state"] == "approved"


# --- pure units: the policy rule (B) and the closed intent ------------------------------------------------------


_UID = "0" * 26
_AC = [{"id": "AC1", "text": "a"}]
_TASKS = [{"id": "T1", "text": "t", "verify": None, "proves": ["AC1"]}]
_INTENT = {
    "v": 1, "kind": "approve_gates", "ticket": {"key": "DEMO-0001", "uid": _UID}, "ticket_type": "chore", "size": None,
    "title": "T", "head": "sha256:" + "0" * 64, "acceptance": _AC,
    "requirements": {"sections": {"requirements": "r"}},
    "plan": {"sections": {"plan": "p"}, "tasks": _TASKS},
}  # fmt: skip


def _input(gate, text, **over):
    inp = {
        "workspace_id": "w", "uid": _UID, "gate": gate, "schema": "orch.ticket/2", "hash_v": 1,
        "sections": {gate: canon.section_hash(text)}, "tasks": _TASKS if gate == "plan" else [],
        "fields": {"ticket_type": "chore", "size": None, "acceptance": _AC, "links": None, "addons": {}},
        "addon_packages": {}, "artifacts": {}, "receipts": {}, "source_sha": [], "prior": {},
    }  # fmt: skip
    return {**inp, **over}


def _gate(gate="requirements", text="r", **kw):
    base = dict(applies=True, blocked=False, hash="sha256:" + "1" * 64, approved=False, missing=(), eligible=("p",),
                needed=1, counting=(), input=_input(gate, text))  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


def _store(req=None, plan=None, uid=_UID):
    gates = {"requirements": req or _gate(), "plan": plan or _gate("plan", "p")}
    return SimpleNamespace(ticket=lambda key: SimpleNamespace(uid=uid, gates=gates))


def _refused(store, person="p"):
    class E(Exception):
        pass

    with pytest.raises(E):
        seqgates.host_check_first(store, person, _INTENT, err=lambda code, why: E(code))


def test_offered_when_one_approval_completes_the_requirements():
    seqgates.host_check_first(_store(), "p", _INTENT, err=lambda c, w: Exception(c))


@pytest.mark.parametrize(
    "req",
    [
        {"needed": 2},  # more than one approval needed
        {"needed": 1, "counting": ("q",)},  # someone else already counts (the prior would hold two ids)
        {"eligible": ("q",)},  # the person may not decide
        {"approved": True},
        {"missing": ("acceptance",)},
        {"blocked": True},
    ],
)
def test_refused_at_step_zero_when_one_approval_does_not_complete_the_requirements(req):
    _refused(_store(req=_gate(**req)))


def test_refused_when_the_person_may_not_decide_the_plan_or_it_is_approved():
    _refused(_store(plan=_gate(eligible=("q",))))
    _refused(_store(plan=_gate(approved=True)))


def test_refused_when_a_gate_binds_an_artifact_or_an_addon_field():
    art = {"spec": {"kind": "report", "digest": "sha256:" + "a" * 64, "ac": None, "task": None}}
    _refused(_store(plan=_gate("plan", "p", input=_input("plan", "p", artifacts=art))))
    fields = {"ticket_type": "chore", "size": None, "acceptance": _AC, "links": None, "addons": {"x": {"y": 1}}}
    _refused(_store(req=_gate(input=_input("requirements", "r", fields=fields))))


def test_l1_a_plan_or_requirement_changed_after_the_review_is_refused_before_step_zero():
    _refused(_store(plan=_gate("plan", "p changed", input=_input("plan", "p changed"))))
    _refused(_store(req=_gate(input=_input("requirements", "r changed"))))
    changed = {"ticket_type": "chore", "size": "xl", "acceptance": _AC, "links": None, "addons": {}}
    _refused(_store(plan=_gate("plan", "p", input=_input("plan", "p", fields=changed))))


def test_a_ticket_with_another_uid_is_refused():
    _refused(_store(uid="V" * 26))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda i: i.update(extra=1),
        lambda i: i.update(kind="edit_ticket"),
        lambda i: i["plan"].update(tasks=[]),
        lambda i: i["plan"]["tasks"][0].update(proves=["AC9"]),
        lambda i: i["requirements"]["sections"].update(Bad="x"),
        lambda i: i["acceptance"].append(dict(i["acceptance"][0])),
        lambda i: i.update(head="nope"),
        lambda i: i.update(size="xxl"),
        lambda i: i["plan"]["sections"].update(plan="x\r\ny"),
    ],
)
def test_the_intent_is_closed(mutate):
    i = copy.deepcopy(_INTENT)
    assert sequence.validate_intent(copy.deepcopy(i))["kind"] == "approve_gates"
    mutate(i)
    with pytest.raises(SequenceInvalid):
        sequence.validate_intent(i)
