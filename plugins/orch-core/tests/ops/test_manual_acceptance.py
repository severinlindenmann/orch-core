"""Manual acceptance in the verify review and submit's evidence check."""

from orch import canon
from orch.host.acceptance import build_accept
from orch.model.accept import label
from tests.ops.test_task_ac import claimed, fill_and_approve


def attach(ws, uid, text):
    store = ws.other()
    previous, ws.store = ws.store, store
    try:
        statement = build_accept("AC1", text)
        raw = statement.encode()
        name = "accept-AC1-1.md"
        event = ws.person_event(
            ws.owner,
            uid,
            "artifact.added",
            name=name,
            kind="feedback",
            ac="AC1",
            label=label("AC1", text),
            sha256=canon.artifact_digest(raw),
            bytes=len(raw),
        )
        store.append(event, log=uid, artifacts={name: raw})
    finally:
        ws.store = previous
        store.close()


def test_edited_acceptance_cannot_submit(ws, cli):
    claimed(cli)
    uid = fill_and_approve(ws, cli)
    cli("section", "set", "verification", "-m", "manually checked")
    attach(ws, uid, "it works")
    assert ws.view("1").acceptance[0].evidence
    cli("ac", "edit", "AC1", "different wording")
    # Renew the approvals voided by editing the criterion, isolating the evidence refusal.
    ws.human("approve", uid, "requirements")
    ws.human("approve", uid, "plan")
    result = cli.j("submit")
    assert result.err_code == "ac.evidence_missing"
    assert "AC1 has no evidence" in result.doc["error"]["message"]


def test_verify_review_uses_rehashed_statement_bytes(hws, agent, me):
    from orch.ops.human import Human
    from tests.ops.humans import make_ticket

    key = make_ticket(agent)
    uid = hws.uid(key)
    attach(hws, uid, "the build is green for Load the tables")
    # Use the production review over a frozen view; the byte cache must first be filled by artifacts_ok.
    store = hws.other()
    try:
        from types import SimpleNamespace

        from orch.ops.caps import Files

        human = object.__new__(Human)
        human.call = SimpleNamespace(store=store)
        human.ctx = SimpleNamespace(caps=SimpleNamespace(files=Files.direct()))
        view = store.ticket(uid)
        human.artifacts_ok(view, "verify")
        text = "\n".join(human.review(view, "verify"))
        assert f"AC1: accepted manually by {hws.owner.ref} (no automated check)" in text
    finally:
        store.close()
    agent("ac", "edit", "AC1", "new wording", "--ref", key)
    store = hws.other()
    try:
        human.call = SimpleNamespace(store=store)
        view = store.ticket(uid)
        human.artifacts_ok(view, "verify")
        assert "AC1: accepted for an earlier wording, no longer counts" in "\n".join(human.review(view, "verify"))
    finally:
        store.close()
