# ruff: noqa: E501
"""B6 (owner decision E): the signing review in three layers: a headline, the content formatted like the ticket
page, the prompt; the full dump behind ``d`` / ``--details``. Same signed material; only the layout changes."""

from __future__ import annotations

import re

import pytest

from orch.host import signcontent, signer
from tests.host import human_helpers
from tests.host.test_human import ready

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

WS = lambda w: ("Acme", "DEMO")  # noqa: E731


def build(prepared, verified=True):
    return signcontent.build(prepared, verified=verified)


APPROVE = """\
Approve the requirements of DEMO-0001 (key per host; signed id …<TAIL>)
in workspace Acme (DEMO)
Verification code: <FP>

Context
  | the context

Out of scope
  | the out_of_scope

Requirements
  | the requirements

Acceptance criteria
  [ ] AC1  it works

Applies under
  type: feature
  rule for this gate: 1 approval from owner; applies to all tickets

Empty, not shown: Summary.
"""

NEW = """\
Create ticket DEMO-0002 · Elefant Chart (key per host; signed id …<TAIL>)
in workspace Acme (DEMO)
Verification code: <FP>

Details
  type: chore
  owner: <OWNER>
"""


def test_the_plain_view_of_an_approval_is_headline_and_content_without_hashes(human, served):
    _, key = human.ticket("Owner CLI test")
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    text = signer.plain(p, WS)
    assert str(text) == APPROVE.replace("<TAIL>", p["log"][-8:]).replace("<FP>", p["fingerprint"])
    # A-3: the dashboard says "check that the verification code matches the one in the terminal": the same 12 hex, labelled,
    # on the third line, before any content (and the full review's own `fingerprint:` line is still in the details)
    assert text.splitlines()[2] == f"Verification code: {p['fingerprint']}" and re.fullmatch(
        r"[0-9a-f]{12}", p["fingerprint"]
    )
    for hidden in ("sha256", "uid ", "pid ", "self-declared", "policy", "gate input"):
        assert hidden not in text
    assert "fingerprint: " + p["fingerprint"] in text.details and "gate input:" in text.details
    assert "ticket key per host, not proven" in text.details and "self-declared, unverified" in text.details
    assert "key per host; signed id" in text.splitlines()[0]  # the host-vouched key is labelled next to the key
    assert "not proven" not in text and "(empty)" not in text  # N12/N13: noise stays out of the plain view
    assert (
        "section summary" in text.details and "Changes" not in text
    )  # ... but the empty section is in the full review


def test_the_plain_view_of_a_new_ticket(human, served):
    _, key = human.ticket("Owner CLI test")
    p = human.prepare("new", {"title": "Elefant Chart", "type": "chore"})
    assert str(signer.plain(p, WS)) == NEW.replace("<OWNER>", p["events"][0]["owner"]).replace(
        "<TAIL>", p["log"][-8:]
    ).replace("<FP>", p["fingerprint"])


def test_the_content_comes_from_the_signed_events_and_material_only(human, served):
    _, key = human.ticket("Owner CLI test")
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    c = build(p)
    assert c["headline"].startswith("Approve the requirements of DEMO-0001")
    assert (c["ticket"]["key"], c["ticket"]["title"]) == ("DEMO-0001", "")  # a gate does not bind the title
    assert c["acceptance"] == [{"id": "AC1", "text": "it works", "checked": False}]
    assert {s["id"]: s["text"] for s in c["sections"]}["requirements"] == "the requirements"
    # a tampered text is no longer what the events sign: the signer refuses before it renders
    bad = {**p, "material": {**p["material"], "texts": {**p["material"]["texts"], "requirements": "other"}}}
    with pytest.raises(signer.SignerRefused):
        human.check(bad)


def test_changed_fields_show_before_and_after_and_tasks_are_numbered(human, served):
    _, key = human.ticket("Owner CLI test")
    p = human.prepare("set", {"ref": key, "pairs": ["priority=high"]})
    c = build(p)
    assert c["fields_changed"] == [{"field": "priority", "before": "medium", "after": "high"}]
    assert c["headline"].startswith("Change the priority of DEMO-0001")
    plan = {  # the plan gate binds the tasks with their verify commands
        "op": "approve",
        "events": [{"type": "gate.approved"}],
        "material": {
            "kind": "gate",
            "gate": "plan",
            "ticket": {"key": "DEMO-0001", "uid": "u"},
            "texts": {},
            "policy": {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False},
            "input": {
                "fields": {},
                "tasks": [
                    {"id": "T1", "text": "write it", "verify": {"cmd": "pytest -q"}, "proves": ["AC1"]},
                    {"id": "T2", "text": "ship it", "verify": None, "proves": []},
                ],
            },
        },
    }
    c = build(plan)
    assert c["headline"].startswith("Approve the plan of DEMO-0001")
    assert c["tasks"] == [
        {"n": 1, "id": "T1", "title": "write it (proves AC1)", "verify": ["pytest -q"]},
        {"n": 2, "id": "T2", "title": "ship it"},
    ]
    assert "  1. [T1] write it (proves AC1)\n       verify: pytest -q\n  2. [T2] ship it\n" in "\n".join(
        signcontent.text(c, signer._clean)
    )


def test_a_control_character_never_reaches_the_plain_view(human, served):
    _, key = human.ticket("Owner CLI test")
    p = human.prepare("log", {"ref": key, "text": "line one\nline two"})
    text = str(signer.plain(p, WS))
    assert "  | line one\n  | line two\n" in text
    esc = {**p, "events": [{**p["events"][0], "text": "x\x1b[2Jy"}]}
    assert "\x1b" not in str(signcontent.text(build(esc), signer._clean))


def test_the_view_of_a_click_carries_the_headline_and_the_content_next_to_the_unchanged_lines(human, served):
    from orch.click import view

    _, key = human.ticket("Owner CLI test")
    p = human.prepare("log", {"ref": key, "text": "note"})
    v = view.build(p, WS, device=p["events"][0]["actor"]["device"], expires_at="2026-01-01T00:00:00Z", artifacts=None)
    assert v["headline"].startswith("Add a note to DEMO-0001") and v["content"]["sections"][0]["text"] == "note"
    assert v["lines"] == signer.render(p, WS).rstrip("\n").split("\n")  # the full review is still there, line for line
    assert v["risk"]
