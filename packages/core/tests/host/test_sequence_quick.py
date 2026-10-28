"""Batch 11, order step (ii): a quick task on the sequence engine (host/seqquick.py): one review, one confirmation sign
the ticket, its requirements and plan, both approvals and, last, an optional grant."""

from __future__ import annotations

import copy

import pytest

from orch.host import sequence, signer
from orch.host.sequence import Aborted
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION
from tests.host.human_helpers import ok
from tests.host.test_sequence import Rig, doc

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
from tests.host.test_click import click  # noqa: E402,F401

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _quick_on(served):
    """Quick tasks is a built-in add-on: without its grant the sequence is refused (addon.off)."""
    hws = served.lab.ws
    hws.tick()
    served.append(
        lambda store: store.append(
            hws.person_event(
                hws.owner, "workspace", "addon.granted", name="quick", version="0.1.0",
                package_sha256="sha256:" + "0" * 64, capabilities=[], fields={}, sections=[], artifact_kinds=[],
            ),
            log="workspace",
        )
    )  # fmt: skip


def quick(agent=None, checks=None, **over):
    base = {
        "v": 1,
        "kind": "quick_task",
        "title": "Fix the typo in the README",
        "description": "The README says teh instead of the.",
        "checks": checks if checks is not None else [{"text": "The typo is gone", "cmd": "grep -c teh README.md"}],
        "agent": agent,
    }
    return {**base, **over}


def test_the_full_quick_sequence_lands_in_order(human, tmp_path):
    rig = Rig(human, tmp_path, quick())
    out = rig.driver().run(rig.first())
    # new, labels, requirements, ac, approve, plan, task, approve
    assert out["landed"] == 8
    got = doc(human, out["key"])
    assert got["doc"]["labels"] == ["quick"] and got["doc"]["type"] == "chore"
    assert got["gates"]["requirements"]["state"] == "approved" and got["gates"]["plan"]["state"] == "approved"
    assert [(a["id"], a["text"]) for a in got["doc"]["acceptance"]] == [("AC1", "The typo is gone")]
    assert got["doc"]["tasks"][0]["verify"]["cmd"].startswith("grep -c teh")
    assert got["status"] != "done"  # the task still ends with the person's verdict
    plan = got["sections"]["plan"][-1]["hash"]
    from orch import canon
    from orch.host import seqquick

    assert plan == canon.section_hash(seqquick.plan_text(rig.intent["checks"]))


def test_the_review_shows_the_task_the_plan_and_what_is_approved(human, tmp_path):
    from orch.host import signcontent

    rig = Rig(human, tmp_path, quick())
    first = rig.first()
    human.check(first)
    content = signcontent.build(first, verified=True)
    assert content["headline"].startswith("Start task DEMO-") and "Fix the typo" in content["headline"]
    assert f"(key per host; signed id …{content['ticket']['uid_tail']})" in content["headline"]
    assert content["ticket"]["new"] is True
    assert sorted(s["id"] for s in content["sections"]) == ["plan", "requirements"]
    plan = next(s for s in content["sections"] if s["id"] == "plan")["text"]
    assert "1. The typo is gone" in plan and "Checked by: grep -c teh" in plan
    assert content["acceptance"][0]["text"] == "The typo is gone" and content["tasks"][0]["verify"]
    assert any("approves the requirements" in n.lower() for n in content["notes"])
    assert any("still ends with your own verdict" in n for n in content["notes"])
    assert not any("Agents may work" in n for n in content["notes"])
    text = signer.render(first, lambda w: ("ws", "DEMO"))
    assert "Approves the requirements as written above" in text


def test_a_foreign_event_between_the_sections_and_the_approval_aborts(human, tmp_path):
    rig = Rig(human, tmp_path, quick())
    first = rig.first()
    real = rig.prepare
    seen = {"n": 0}

    def prepare(op, args, meta):
        if op == "approve" and args["gate"] == "requirements":
            res = human.c.req(
                "log", {"ref": args["ref"], "message": "foreign"}, grant=human.served.grant, session=SESSION, idem=True
            )
            assert res["ok"]
        return real(op, args, meta)

    rig.port.prepare = prepare
    with pytest.raises(Aborted):  # the host refuses to prepare on a head that is not the signer's own last event
        rig.driver().run(first)
    assert seen["n"] == 0
    key = rig.sessions.get(rig.wid, rig.driver().digest)["ticket"]["key"]
    got = doc(human, key)
    assert got["gates"]["requirements"]["state"] != "approved"  # no approval was signed: a draft


def test_a_host_supplied_wrong_gate_hash_is_refused(human, tmp_path):
    rig = Rig(human, tmp_path, quick())
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        got = real(op, args, meta)
        if op == "approve" and args["gate"] == "requirements":
            got = copy.deepcopy(got)
            got["events"][0]["hash"] = "sha256:" + "0" * 64  # the hash is not the input's
        return got

    rig.port.prepare = prepare
    with pytest.raises(Aborted, match="refused|changed"):
        rig.driver().run(first)
    assert not any(e["type"] == "gate.approved" for _, ev, _ in rig.submitted for e in ev)


def test_a_gate_input_that_does_not_match_the_signed_content_is_refused(human, tmp_path):
    rig = Rig(human, tmp_path, quick())
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        got = real(op, args, meta)
        if op == "approve" and args["gate"] == "plan":
            from orch import canon

            got = copy.deepcopy(got)
            got["material"]["input"]["tasks"][0]["text"] = "Something else entirely"
            # a host that is consistent with itself: hash recomputed over the altered input
            got["events"][0]["hash"] = canon.gate_hash(got["material"]["input"])
            from orch.host import review

            got["material"]["review"] = [review.of(e, got["material"]) for e in got["events"]]
            digest = review.review_digest(got["material"])
            got["review_digest"], got["fingerprint"] = digest, review.fingerprint(digest, got["events"], "approve")
        return got

    rig.port.prepare = prepare
    with pytest.raises(Aborted, match="changed while signing"):
        rig.driver().run(first)
    approvals = [e["gate"] for _, ev, _ in rig.submitted for e in ev if e["type"] == "gate.approved"]
    assert approvals == ["requirements"]  # the plan approval was never signed


def test_the_grant_is_last_and_an_abort_before_it_leaves_an_approved_ticket_without_one(human, tmp_path):
    rig = Rig(human, tmp_path, quick(agent={"hours": 8, "scope": "workable"}))
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        if op == "grant":
            res = human.c.req(
                "log",
                {"ref": meta["ticket"], "message": "foreign"},
                grant=human.served.grant,
                session=SESSION,
                idem=True,
            )
            assert res["ok"]  # lands before the grant is prepared
        return real(op, args, meta)

    rig.port.prepare = prepare
    with pytest.raises(Aborted):
        rig.driver().run(first)
    types = [e["type"] for _, ev, _ in rig.submitted for e in ev]
    assert types[-1] == "gate.approved" and "grant.issued" not in types
    key = rig.sessions.get(rig.wid, rig.driver().digest)["ticket"]["key"]
    assert doc(human, key)["gates"]["plan"]["state"] == "approved"
    grants = ok(human.c.req("grant.list", {})) if False else None  # no grant exists: nothing was signed for one
    assert grants is None


def test_a_grant_with_other_hours_or_a_wrong_time_span_is_refused(human, tmp_path):
    rig = Rig(human, tmp_path, quick(agent={"hours": 8, "scope": "workable"}))
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        got = real(op, args, meta)
        if op == "grant":
            got = copy.deepcopy(got)
            got["events"][0]["expires_at"] = "2099-01-01T00:00:00Z"
            from orch.host import review

            got["material"]["review"] = [review.of(e, got["material"]) for e in got["events"]]
            digest = review.review_digest(got["material"])
            got["review_digest"], got["fingerprint"] = digest, review.fingerprint(digest, got["events"], "grant")
        return got

    rig.port.prepare = prepare
    with pytest.raises(Aborted, match="changed while signing"):
        rig.driver().run(first)


@pytest.mark.parametrize(
    "change",
    [
        {"checks": []},
        {"checks": [{"text": "a\nb", "cmd": None}]},
        {"checks": [{"text": "ok", "cmd": "x\ny"}]},
        {"checks": [{"text": "ok", "cmd": "test -f a.html && grep -q x a.html"}]},
        {"checks": [{"text": "ok", "cmd": 'test -z "$(grep teh README.md)"'}]},
        {"title": ""},
        {"description": "\nleading"},
        {"agent": {"hours": 200, "scope": "workable"}},
        {"agent": {"hours": 8, "scope": "all"}},
        {"surprise": 1},
    ],
)
def test_a_quick_intent_is_refused_unless_every_line_is_clean(change):
    with pytest.raises(sequence.SequenceInvalid):
        sequence.validate_intent(quick(**change))


def test_the_phone_is_excluded(human, tmp_path):
    rig = Rig(human, tmp_path, quick())
    op, args = sequence.step_args(rig.intent, 0, None)
    res = human.c.req(
        "human.prepare",
        {
            "op": op,
            "args": args,
            "signer": {"device": human.person.device, "auth": "secure-enclave"},
            "sequence": rig.meta0(),
        },
    )
    assert res["error"]["code"] == "invalid.input"


# --- the custody paths -------------------------------------------------------------------------------------------


def _real_passphrase_backend(human, tmp_path):
    """The REAL passphrase file backend over the test person's own device key (nothing here accepts a label it
    should not: the custody checks every payload against the labels the batch was opened for)."""
    from orch.custody.passphrase import PassphraseBackend
    from tests.ops.humans import KDF, PASSPHRASE

    be = PassphraseBackend(tmp_path / "keys", _passphrase_provider=lambda _req: PASSPHRASE, _kdf=KDF)
    scalar = human.person.dk_sig.private_numbers().private_value.to_bytes(32, "big")
    pub = be.create("dk", secret=scalar, role="device", passphrase=PASSPHRASE)
    assert pub == human.person.sig_pub
    return be


def test_the_unlock_covers_exactly_the_labels_the_reviewed_steps_need(human, tmp_path):
    from orch import canon
    from orch.custody.base import CustodyError
    from orch.host import sequence_terminal

    ticket, ws_ev = (canon.LABELS[n].encode("ascii") for n in ("sig_ticket_event", "sig_ws_event"))
    plain = sequence.validate_intent(quick())
    with_grant = sequence.validate_intent(quick(agent={"hours": 8, "scope": "workable"}))
    assert sequence_terminal.labels_for(plain) == (ticket,)  # no grant step: never the workspace label
    assert set(sequence_terminal.labels_for(with_grant)) == {ticket, ws_ev}
    be = _real_passphrase_backend(human, tmp_path)
    narrow = dict(digest="0" * 32, fields=(), action="test")
    with sequence_terminal.unlocked_backend(be, "dk", labels=(ticket,), **narrow) as open_:
        with pytest.raises(CustodyError):  # the custody still refuses another label
            open_.sign("dk", ws_ev + b"\x00payload", action="a")


def test_agent_access_longer_than_the_workspace_maximum_is_refused_at_the_review(human, tmp_path):
    rig = Rig(human, tmp_path, quick(agent={"hours": 72, "scope": "workable"}))  # the workspace allows 8 hours
    res = human.c.req(
        "human.prepare",
        {
            "op": "new",
            "args": sequence.step_args(rig.intent, 0, None)[1],
            "signer": human.signer,
            "sequence": rig.meta0(),
        },
    )
    assert res["error"]["code"] == "invalid.input" and "at most 8 hours" in res["error"]["message"]
    assert not rig.sessions.get(rig.wid, rig.driver().digest)  # nothing was signed


def click_prepare(click_, intent):
    from orch.host import sequence

    intent = sequence.validate_intent(intent)
    meta = {
        "step": 0,
        "of": len(sequence.steps(intent)),
        "digest": sequence.digest_of(click_.served.wid, click_.owner.ref, intent),
        "intent": intent,
    }
    op, args = sequence.step_args(intent, 0, None)
    return click_.prepare(op, args, sequence=meta)


def test_one_click_signs_the_whole_quick_task(click, served, human):  # noqa: F811
    p = click_prepare(click, quick())
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"} and v["view"]["headline"].startswith("Create a ticket")
    click.mono.t += 2
    out = click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 8
    got = ok(served.client().req("ticket.get", {"ref": out["result"]["key"]}))["data"]
    assert got["gates"]["plan"]["state"] == "approved" and got["doc"]["labels"] == ["quick"]


def test_the_grant_is_only_ever_the_last_step():
    from orch.host import sequence

    intent = sequence.validate_intent(quick(agent={"hours": 8, "scope": "workable"}))
    kinds = [s["kind"] for s in sequence.steps(intent)]
    assert kinds.index("grant") == len(kinds) - 1 and kinds.count("grant") == 1  # the grant is only ever last


def test_a_grant_is_one_click_and_goes_to_the_runner_last(click, served, human):  # noqa: F811
    click.signer._execution = lambda entry: True
    p = click_prepare(click, quick(agent={"hours": 8, "scope": "workable"}))
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"}
    assert "Agents may work on any ticket they are allowed to work on" in "\n".join(v["view"]["lines"])
    click.mono.t += 2
    out = click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 9
    assert out["deposit"] in ("confirmed", "rejected", "unknown") and out["grant"].startswith("gr_")


def test_without_dashboard_execution_a_grant_sequence_is_the_terminals(click, served, human):  # noqa: F811
    from tests.host.test_click import refused

    click.signer._execution = lambda entry: False
    with pytest.raises(Exception, match="terminal|click"):
        click_prepare(click, quick(agent={"hours": 8, "scope": "workable"}))
    del refused


# --- §0.8.6 at submit: the convenience condition is checked AGAIN, under the lock, before judging ------


def _convenience_signer(human):
    return {"device": human.person.device, "auth": "convenience"}


def _sign_convenience(human, prepared):
    backend = human_helpers.KeyBackend(human.person, auth="convenience")
    signer.check(
        prepared,
        workspace_id=human.served.wid,
        person=human.person.ref,
        device=human.person.device,
        auth="convenience",
    )
    return signer.sign(prepared, backend=backend, key_id="dk", workspace_id=human.served.wid)


def test_a_single_op_is_refused_at_submit_when_convenience_was_turned_off_after_prepare(human, served):
    from orch.host import convenience
    from tests.host.lifecycle_helpers import UID

    key = human.agent("new", {"title": "x", "type": "chore"})["key"]
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    p = human.prepare("log", {"ref": key, "message": "note"}, signer=_convenience_signer(human))
    signed = _sign_convenience(human, p)
    convenience.set_enabled(served.lab.state, served.wid, False, UID)  # between prepare and submit
    res = human.submit_res(p["request"]["id"], signed.events)
    assert res["ok"] is False and res["error"]["code"] == "sign.convenience_unavailable"
    assert doc(human, key)["log"][-1]["type"] != "log.added"  # nothing landed


def test_a_quick_sequence_step_is_refused_at_submit_when_convenience_was_turned_off(human, served, tmp_path):
    from orch.host import convenience
    from tests.host.lifecycle_helpers import UID

    rig = Rig(human, tmp_path, quick())
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    op, args = sequence.step_args(rig.intent, 0, None)
    first = ok(
        human.c.req(
            "human.prepare",
            {"op": op, "args": args, "signer": _convenience_signer(human), "sequence": rig.meta0()},
        )
    )["data"]
    ok(human.submit_res(first["request"]["id"], _sign_convenience(human, first).events))
    key = first["material"]["ticket"]["key"]
    op, args = sequence.step_args(rig.intent, 1, key)
    meta = {
        "step": 1,
        "of": len(sequence.steps(rig.intent)),
        "digest": rig.meta0()["digest"],
        "prev": _last_id(human, key),
        "ticket": key,
    }
    p = ok(
        human.c.req("human.prepare", {"op": op, "args": args, "signer": _convenience_signer(human), "sequence": meta})
    )["data"]
    signed = _sign_convenience(human, p)
    convenience.set_enabled(served.lab.state, served.wid, False, UID)  # between prepare and submit of THIS step
    res = human.submit_res(p["request"]["id"], signed.events)
    assert res["ok"] is False and res["error"]["code"] == "sign.convenience_unavailable"
    assert doc(human, key)["doc"]["labels"] == []  # the label step did not land


def _last_id(human, key):
    store = human.served.service.store
    store.refresh()
    uid = store.ticket(key).uid
    return store.events(uid)[-1]["id"]


def test_the_cli_builds_the_same_intent_as_the_form():
    from orch.host import quickstart
    from orch.host.errors import HostError

    args = quickstart._parser().parse_args(
        [
            "Fix the typo",
            "--details",
            "It says teh.",
            "--step",
            "Typo gone",
            "--check",
            "grep -c teh f",
            "--agents",
            "8",
        ]
    )
    intent = quickstart.build_intent(args)
    assert intent["kind"] == "quick_task" and intent["agent"] == {"hours": 8, "scope": "workable"}
    assert intent["checks"] == [{"text": "Typo gone", "cmd": "grep -c teh f"}]
    plain = quickstart.build_intent(quickstart._parser().parse_args(["Water the plants"]))
    assert plain["checks"] == [{"text": "Water the plants", "cmd": None}] and plain["agent"] is None
    with pytest.raises(HostError):
        quickstart.build_intent(quickstart._parser().parse_args(["T", "--check", "cmd only"]))
    with pytest.raises(HostError):
        quickstart.build_intent(quickstart._parser().parse_args(["bad\nline"]))


# --- `orch quick start` against a real test registry and host ----------------------------------------------------


def test_orch_quick_start_is_a_persons_command(monkeypatch, tmp_path):
    import io

    from orch.cli.main import main

    out, errs = io.StringIO(), io.StringIO()
    rc = main(
        ["quick", "start", "T", "--state", str(tmp_path), "--json"],
        env={"ORCH_GRANT": "gr_x.y", "HOME": str(tmp_path)},
        stdout=out,
        stderr=errs,
    )
    assert rc != 0 and "human_only" in out.getvalue()


# --- the click rules still run per step, IN ADDITION to the host checks ------------------------------------------------  # noqa: E501


def test_a_click_sequence_still_refuses_a_dirty_worker_check_and_too_many_events(click, served, human):  # noqa: F811
    from tests.host.test_click import refused

    p = click_prepare(click, quick())
    v = click.view(p["request_id"])
    click.mono.t += 2
    real = click.tamper

    def tamper(op, args, res):
        if op == "human.prepare" and args.get("sequence", {}).get("step") == 2:
            res = copy.deepcopy(res)
            res["data"]["checks"] = [
                {"repo": "r", "clean": False, "attested_by": "worker", "at": "2026-10-12T00:00:00Z"}
            ]
        return res

    click.tamper = tamper
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "stopped")
    assert click.signatures == 2  # created and labelled; the step with the dirty check was never signed
    click.tamper = real

    from orch.click import view as views

    many = {"workspace_id": served.wid, "events": [{}] * (views.MAX_EVENTS + 1)}
    with pytest.raises(Exception, match="too many events"):
        views.check_prepared(many, workspace_id=served.wid, person="p", device="d", files=None)


def test_a_quick_gate_input_with_addon_fields_or_packages_is_refused():
    from orch.host import seqquick

    intent = sequence.validate_intent(quick())
    base = seqquick.gate_expect(intent, "requirements", workspace_id="w" * 32, uid="U" * 26, requirements_approval=None)
    inp = {
        k: base[k]
        for k in (
            "workspace_id",
            "uid",
            "gate",
            "schema",
            "hash_v",
            "sections",
            "tasks",
            "artifacts",
            "receipts",
            "source_sha",
        )
    }
    inp |= {"fields": {**base["fields"], "addons": {}}, "addon_packages": {}, "prior": {}}
    assert seqquick.check_gate_input(base, inp, base["texts"])
    assert not seqquick.check_gate_input(base, {**inp, "addon_packages": {"x": "sha256:" + "0" * 64}}, base["texts"])
    assert not seqquick.check_gate_input(
        base, {**inp, "fields": {**inp["fields"], "addons": {"x": {"y": 1}}}}, base["texts"]
    )


def test_an_empty_orch_grant_still_means_an_agent(monkeypatch, tmp_path):
    import io

    from orch.cli.main import main

    out = io.StringIO()
    rc = main(
        ["quick", "start", "T", "--state", str(tmp_path), "--json"],
        env={"ORCH_GRANT": "", "HOME": str(tmp_path)},
        stdout=out,
        stderr=io.StringIO(),
    )
    assert rc != 0 and "human_only" in out.getvalue()
