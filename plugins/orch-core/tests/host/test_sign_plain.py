# ruff: noqa: E501
"""B6 (owner decision E): the signing review in three layers: a headline, the content formatted like the ticket
page, the prompt; the full dump behind ``d`` / ``--details``. Same signed material; only the layout changes."""

from __future__ import annotations

import json
import os
import pty
import threading
import time

import pytest

from orch.host import signcontent, signer
from orch.ops import human as ops_human
from tests.host import human_helpers
from tests.host.test_human import ready

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

WS = lambda w: ("Acme", "DEMO")  # noqa: E731


def build(prepared, verified=True):
    return signcontent.build(prepared, verified=verified)


APPROVE = """\
Approve the requirements of DEMO-0001 (key per host; signed id …<TAIL>)
in workspace Acme (DEMO)

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

Fields
  type: chore
  owner: <OWNER>
"""


def test_the_plain_view_of_an_approval_is_headline_and_content_without_hashes(human, served):
    _, key = human.ticket("Owner CLI test")
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    text = signer.plain(p, WS)
    assert str(text) == APPROVE.replace("<TAIL>", p["log"][-8:])
    for hidden in ("sha256", "fingerprint", "uid ", "pid ", "self-declared", "policy", "gate input"):
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
    assert str(signer.plain(p, WS)) == NEW.replace("<OWNER>", p["events"][0]["owner"]).replace("<TAIL>", p["log"][-8:])


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
    v = view.build(
        p, WS, kind="click", device=p["events"][0]["actor"]["device"], expires_at="2026-01-01T00:00:00Z", artifacts=None
    )
    assert v["headline"].startswith("Add a note to DEMO-0001") and v["content"]["sections"][0]["text"] == "note"
    assert v["lines"] == signer.render(p, WS).rstrip("\n").split("\n")  # the full review is still there, line for line
    assert v["risk"]


# --- the prompt -------------------------------------------------------------------------------------------------------


def _prompt(monkeypatch, text, answers):
    """Run ``review_prompt`` on a pty; the test types ``answers``, one after each prompt. Returns (result, shown)."""
    master, slave = pty.openpty()
    monkeypatch.setattr(ops_human, "open_tty", lambda flags: os.dup(slave))
    shown = bytearray()
    done = threading.Event()

    def person():
        queue = list(answers)
        seen = 0
        while queue and not done.is_set():
            try:
                shown.extend(os.read(master, 65536))
            except OSError:
                return
            prompts = shown.count(b"): ") + shown.count(b"stops: ")
            if prompts > seen:
                seen = prompts
                time.sleep(0.3)  # the prompt flushes pending input once it is written: type after that
                os.write(master, queue.pop(0).encode() + b"\n")

    t = threading.Thread(target=person, daemon=True)
    t.start()
    watchdog = threading.Timer(8, lambda: os.write(master, b"\n"))  # never hang a test run
    watchdog.start()
    try:
        result = ops_human.review_prompt(text, "DEMO-0001/ABCD1234")
    finally:
        watchdog.cancel()
        done.set()
        os.close(slave)
        t.join(2)
        os.close(master)
    return result, shown.decode()


def _review():
    r = signer.Review("Approve the requirements of DEMO-0001\n\n")
    r.details = "FULL DUMP\nfingerprint: abc\n"
    return r


def test_the_prompt_shows_the_plain_view_and_d_prints_the_full_dump(monkeypatch):
    ok, shown = _prompt(monkeypatch, _review(), ["d", "DEMO-0001/ABCD1234"])
    assert ok and "Type DEMO-0001/ABCD1234 to continue (d = details): " in shown
    assert shown.index("Approve the requirements") < shown.index("FULL DUMP")  # plain first, details on request


def test_without_d_the_details_are_not_shown(monkeypatch):
    ok, shown = _prompt(monkeypatch, _review(), ["DEMO-0001/ABCD1234"])
    assert ok and "FULL DUMP" not in shown


def test_a_wrong_word_stops(monkeypatch):
    ok, _ = _prompt(monkeypatch, _review(), ["d", "yes"])
    assert not ok


def test_details_first_shows_the_dump_without_d(monkeypatch):
    r = _review()
    r.details_first = True
    ok, shown = _prompt(monkeypatch, r, ["DEMO-0001/ABCD1234"])
    assert ok and "FULL DUMP" in shown and "Approve the requirements" not in shown


def test_a_review_without_details_keeps_the_old_prompt(monkeypatch):
    ok, shown = _prompt(monkeypatch, "plain text\n", ["DEMO-0001/ABCD1234"])
    assert ok and "and Enter to continue; anything else stops: " in shown


# --- the invariant: nothing that carries authority or identity is left out of the plain view -----------------------------

from orch.host import review  # noqa: E402
from orch.identity import GrantToken  # noqa: E402,F401

SCHEMA_STAMPS = frozenset({"seq", "at", "prev", "ws_seq", "host_sig"})
UID = "01J9ZP0000000000000000000A"
CERT = {
    "o": {"person_id": "f" * 32, "device_id": "e" * 32, "scopes_max": ["look", "decide"], "expires_ms": 1234567},
    "sig": "x",
}
POLICY = {"approvers": ["owner"], "count": 2, "not": ["author"], "applies": "all", "independent": True}


def signable_types() -> list[str]:
    from orch.model.emits import EMITS_V1

    out: set[str] = set()
    for op in [*EMITS_V1, *review.HOST_PERSON_OPS]:
        out |= set(review.person_op_types(op) or ())
    return sorted(out)


def schema_properties(typ: str) -> set[str]:
    from orch import schema

    return set(schema.load(f"event.{typ}")["properties"]) - review.ENVELOPE - SCHEMA_STAMPS


@pytest.mark.parametrize("typ", signable_types())
def test_every_schema_property_of_a_person_event_is_classified(typ):
    """A key added to an event schema fails here until it is either shown by the plain view or in TECHNICAL_KEYS."""
    assert typ in signcontent.SHOWN, f"classify {typ} in signcontent.SHOWN"
    unclassified = schema_properties(typ) - signcontent.SHOWN[typ] - signcontent.TECHNICAL_KEYS
    assert not unclassified, f"{typ}: classify {sorted(unclassified)} in signcontent.SHOWN or TECHNICAL_KEYS"
    assert signcontent.SHOWN[typ] <= schema_properties(typ) | {"set"}, f"{typ}: SHOWN names a key the schema lacks"


VALUES = {
    "name": "Marker-Name", "kind": "marker-kind", "label": "marker-label", "ac": "AC9", "task": "T9", "ref": "marker-ref",
    "addon": "marker-addon", "bytes": 4242, "sha256": "sha256:" + "ab" * 32, "version": "9.9.9", "package_sha256": "sha256:" + "cd" * 32,
    "capabilities": ["marker-cap"], "artifact_kinds": ["marker-akind"], "fields": ["marker-field"], "replaces": "marker-old",
    "device": "d_" + "1" * 32, "cert": CERT, "device_cert": CERT, "pk_pub": "marker-pubkey", "person": "p_" + "2" * 32,
    "role": "maintainer", "outcome": "pass", "text": "marker text", "source_sha": [{"repo": "app", "ref": "refs/heads/x", "sha": "a" * 40}],
    "grant": "gr_" + "3" * 26, "scope": "workable", "verbs": "agent", "hours": 7, "issued_at": "2026-10-16T10:00:00Z",
    "expires_at": "2026-10-16T17:00:00Z", "reason": "marker-reason", "question": "q1", "option": "a", "qid": "q9",
    "duplicate_of": "DEMO-0007", "resolution": "duplicate", "key": "DEMO-0002", "ticket_type": "chore", "title": "Marker Title",
    "owner": "p_" + "4" * 32, "add": ["p_" + "7" * 32], "remove": ["p_" + "8" * 32], "from": "open", "to": "backlog", "gate": "plan",
    "gates": {"plan": POLICY}, "set": {"grant_hours": 5}, "sections": {"summary": {"hash": "sha256:" + "ee" * 32, "refs": ["marker-art"]}},
}  # fmt: skip


def sample(typ: str) -> dict:
    event = {k: VALUES[k] for k in sorted(signcontent.SHOWN[typ])}
    if typ == "addon.granted":
        event["sections"] = ["marker-section"]
    if typ == "ticket.updated":
        event["set"] = {"ticket.priority": "high", "ticket.acceptance": [{"id": "AC1", "text": "marker ac"}]}
        event["base_rev"] = {"ticket.priority": "sha256:" + "11" * 32}
    if typ == "question.asked":
        event["question"] = "marker question"
    if typ == "question.answered":
        event["question"] = "q1"
    return {"type": typ, "actor": {"kind": "person", "id": "p_" + "5" * 32, "device": "d_" + "6" * 32}, **event}


def material_for(typ: str, event: dict) -> dict:
    ticket = {"key": "DEMO-0001", "uid": UID}
    if typ in ("gate.approved", "gate.changes_requested", "verdict.given"):
        return {
            "kind": "gate", "gate": "plan", "ticket": ticket, "policy": POLICY, "texts": {"summary": "marker summary"},
            "input": {"fields": {"ticket_type": "chore", "size": "s", "acceptance": [{"id": "AC1", "text": "marker ac"}], "links": None, "addons": {}},
                      "tasks": [], "artifacts": {}, "receipts": {}, "source_sha": [], "prior": {}},
        }  # fmt: skip
    if typ == "question.answered":
        return {
            "kind": "question",
            "ticket": ticket,
            "question": {"id": "q1", "text": "marker question?", "options": [{"key": "a", "label": "Marker option"}]},
        }
    return {
        "kind": "payload",
        "ticket": ticket,
        "texts": {"summary": "marker summary"},
        "before": {"ticket.priority": "low", "grant_hours": 8},
    }


def flat(value) -> str:
    return json.dumps(value, sort_keys=True)


@pytest.mark.parametrize("typ", signable_types())
def test_the_plain_view_shows_every_key_it_claims_to_show(typ):
    event = sample(typ)
    content = build({"op": "x", "events": [event], "material": material_for(typ, event)})
    text = flat(content) + "\n".join(signcontent.text(content, signer._clean))
    for key in signcontent.SHOWN[typ]:
        value = event[key]
        needles = []
        if isinstance(value, str) and value.startswith("sha256:"):
            needles.append(value[7:19])  # a digest is shown short
        elif key in ("pk_pub", "cert", "device_cert"):
            needles.append(signcontent._fp(value))  # a key or certificate is shown by its fingerprint
        elif key in ("sections", "set", "gates", "source_sha"):
            needles.append(
                "marker"
                if key == "sections"
                else {
                    "set": "priority" if typ == "ticket.updated" else "grant_hours",
                    "gates": "approval",
                    "source_sha": "aaaaaaaaaaaa",
                }[key]
            )
        elif key in ("gate", "question"):
            needles.append("plan" if key == "gate" else "marker question")
        elif key == "ticket_type":
            needles.append("chore")
        else:
            needles.append(
                str(value) if not isinstance(value, (list, dict)) else str(value[0]) if isinstance(value, list) else ""
            )
        assert all(n in text for n in needles), f"{typ}: {key} is not in the plain view"


def test_a_key_the_plain_view_does_not_know_falls_back_to_the_full_review(human, served):
    _, key = human.ticket("Owner CLI test")
    p = human.prepare("log", {"ref": key, "text": "x"})
    event = {**p["events"][0], "future_field": {"x": 1}}
    future = {**p, "events": [event], "material": {**p["material"], "review": [review.of(event, p["material"])]}}
    with pytest.raises(signcontent.PlainUnavailable):
        build(future)
    plain = signer.plain(future, WS)  # nothing is hidden: the whole review is the only view
    assert "future_field" in str(plain) and "fingerprint: " in str(plain) and not plain.details
    with pytest.raises(signcontent.PlainUnavailable):
        build({**p, "events": [{**p["events"][0], "type": "nonsense.event"}]})


def test_a_gate_input_key_the_plain_view_does_not_know_falls_back(human, served):
    _, key = human.ticket("Owner CLI test")
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    for where in ("input", "fields"):
        bad = json.loads(json.dumps(p))
        target = bad["material"]["input"] if where == "input" else bad["material"]["input"]["fields"]
        target["future_gate_key"] = 1
        with pytest.raises(signcontent.PlainUnavailable):
            build(bad)


def test_authority_is_in_the_plain_view_of_a_grant_a_member_and_a_gate(human, served):
    g = sample("grant.issued")
    c = build({"op": "grant", "events": [g], "material": material_for("grant.issued", g)})
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert rows["scope"] == "workable" and rows["verbs"] == "agent" and rows["until"] == "2026-10-16T17:00:00Z"
    assert "scope workable, verbs agent" in c["headline"]
    m = sample("member.added")
    c = build({"op": "member.add", "events": [m], "material": material_for("member.added", m)})
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert rows["person"] == m["person"] and rows["role"] == "maintainer" and len(rows["key fingerprint"]) == 12
    assert rows["device certificate device id"] == "e" * 32 and "look, decide" in rows["device certificate scopes max"]
    _, key = human.ticket("Owner CLI test")
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    rows = {r["field"]: r["after"] for r in build(p)["fields_changed"]}
    assert "approval" in rows["rule for this gate"] and "owner" in rows["rule for this gate"]


def test_b8_lengths_read_as_days_and_hours_in_the_headline_and_the_settings_note(human, served):
    g = {**sample("grant.issued"), "hours": 72}
    c = build({"op": "grant", "events": [g], "material": material_for("grant.issued", g)})
    assert c["headline"].startswith("Give agents access for 3 days (")
    assert "for 3 days," in " ".join(c["notes"])
    assert signcontent.span(8) == "8 hours" and signcontent.span(24) == "1 day" and signcontent.span(168) == "7 days"
    event = {**sample("settings.changed"), "set": {"grant_hours": 168}}
    c = build({"op": "settings.set", "events": [event], "material": material_for("settings.changed", event)})
    assert any("up to 7 days" in n for n in c["notes"])


def test_a_before_the_host_alone_vouches_for_is_labelled(human, served):
    s = human.prepare("set", {"ref": human.ticket("T")[1], "pairs": ["priority=high"]})
    (row,) = build(s)["fields_changed"]
    assert row["before"] == "medium" and "before_note" not in row  # bound by the event's base_rev
    event = sample("settings.changed")
    c = build({"op": "settings.set", "events": [event], "material": material_for("settings.changed", event)})
    (row,) = c["fields_changed"]
    assert row["before"] == "8" and row["before_note"] == "per host, not signed"
    assert "(per host, not signed)" in "\n".join(signcontent.text(c, signer._clean))
    created = sample("ticket.created")
    c = build({"op": "new", "events": [created], "material": material_for("ticket.created", created)})
    assert {"field": "owner", "before": None, "after": created["owner"]} in c["fields_changed"]


# --- verified: what the caller vouched for ------------------------------------------------------------------------------


def test_an_unverified_build_labels_every_before_and_leaves_out_the_policy(human, served):
    _, key = human.ticket("Owner CLI test")
    ready(human, key)
    s = human.prepare("set", {"ref": key, "pairs": ["priority=high"]})
    (row,) = build(s, verified=True)["fields_changed"]
    assert row["before"] == "medium" and "before_note" not in row  # bound by the event's base_rev, and checked
    (row,) = build(s, verified=False)["fields_changed"]
    assert row["before_note"] == "per host, not checked"
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    assert "rule for this gate" in {r["field"] for r in build(p, verified=True)["fields_changed"]}
    assert "rule for this gate" not in {r["field"] for r in build(p, verified=False)["fields_changed"]}
    settings = sample("settings.changed")
    c = build(
        {"op": "settings.set", "events": [settings], "material": material_for("settings.changed", settings)}, False
    )
    assert c["fields_changed"][0]["before_note"] == "per host, not checked"


def test_a_question_shows_its_options_and_labels_what_the_question_hash_does_not_bind():
    ev = {
        "type": "question.answered",
        "question": "q1",
        "option": "a",
        "text": "because",
        "hash": "sha256:" + "ab" * 32,
    }
    material = {
        "kind": "question",
        "ticket": {"key": "DEMO-0001", "uid": UID},
        "question": {
            "id": "q1",
            "text": "Which way?",
            "options": [{"key": "a", "label": "Left", "cost": "1d"}, {"key": "b", "label": "Right"}],
            "to": "p_" + "7" * 32,
            "blocking": True,
        },
    }
    c = build({"op": "answer", "events": [ev], "material": material})
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert rows["option a"] == "Left (cost 1d)" and rows["option b"] == "Right"
    assert (
        rows["addressed to"].endswith("(per host, not signed)")
        and rows["blocks the ticket"] == "true (per host, not signed)"
    )
    assert c["headline"].startswith("Answer “Which way?” on DEMO-0001") and c["headline"].endswith("with Left")
    material["question"]["future"] = 1
    with pytest.raises(signcontent.PlainUnavailable):
        build({"op": "answer", "events": [ev], "material": material})


# --- round 1 of the Opus review ---------------------------------------------------------------------------------------


def test_a_line_break_in_a_value_cannot_fake_structure():
    """HIGH-1: a verify command or an acceptance text with a newline is ONE visible line; a section is a block under marks."""
    evil = "pytest\n  2. Tidy workspace: rm -rf ~/src"
    plan = {
        "op": "approve",
        "events": [{"type": "gate.approved", "gate": "plan"}],
        "material": {
            "kind": "gate",
            "gate": "plan",
            "ticket": {"key": "DEMO-0001", "uid": UID},
            "texts": {"summary": "line a\n  2. fake"},
            "policy": {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False},
            "input": {
                "fields": {"acceptance": [{"id": "AC1", "text": "works\nAC9 [ ] also"}]},
                "tasks": [{"id": "T1", "text": "do it\n  3. other", "verify": {"cmd": evil}, "proves": []}],
            },
        },
    }
    c = build(plan)
    assert "\n" not in c["tasks"][0]["verify"][0] and "\n" not in c["tasks"][0]["title"]
    assert "\n" not in c["acceptance"][0]["text"] and " ⏎ " in c["acceptance"][0]["text"]
    lines = signcontent.text(c, signer._clean)
    assert [x for x in lines if x.lstrip().startswith("2.")] == []  # no line of its own starts a second task
    assert "       verify: pytest ⏎   2. Tidy workspace: rm -rf ~/src" in lines
    assert "  1. [T1] do it ⏎   3. other" in lines
    assert "  | line a" in lines and "  |   2. fake" in lines  # a section: every line under a mark
    ev = {"type": "ticket.moved", "from": "open", "to": "backlog", "reason": "x\nowner: evil"}
    c = build(
        {"op": "move", "events": [ev], "material": {"kind": "payload", "ticket": {"key": "DEMO-0001", "uid": UID}}}
    )
    assert all("\n" not in r["after"] for r in c["fields_changed"])


def test_the_ticket_key_is_the_hosts_word_and_the_signed_id_tail_is_shown():
    """MEDIUM-1."""
    ev = {"type": "log.added", "text": "x"}
    c = build({"op": "log", "events": [ev], "material": {"kind": "payload", "ticket": {"key": "ORCH-12", "uid": UID}}})
    assert (
        c["headline"].endswith(f"(key per host; signed id …{UID[-8:]})") and "key per host" in c["ticket"]["key_note"]
    )
    assert c["ticket"]["uid_tail"] == UID[-8:]


def test_prior_source_and_receipts_are_checked_and_shown():
    """LOW L2/L3: unknown keys in prior or source_sha fall back; a receipt shows its event; tasks show their ids."""
    base = {
        "op": "approve",
        "events": [{"type": "gate.approved", "gate": "verify"}],
        "material": {
            "kind": "gate",
            "gate": "verify",
            "ticket": {"key": "DEMO-0001", "uid": UID},
            "texts": {},
            "policy": POLICY,
            "input": {
                "fields": {},
                "tasks": [],
                "receipts": {"T1": {"event": "01EVENT", "repo": "app", "commit": "c" * 40, "exit": 0}},
                "source_sha": [{"repo": "app", "ref": "refs/heads/x", "sha": "a" * 40}],
                "prior": {"plan": {"gen": 2, "approvals": ["p_" + "1" * 32]}},
            },
        },
    }
    c = build(base)
    assert any("event 01EVENT" in (r["after"] or "") for r in c["fields_changed"])
    for where, extra in (
        ("prior", {"plan": {"gen": 2, "approvals": [], "sneaky": 1}}),
        ("source_sha", [{"repo": "a", "ref": "b", "sha": "c", "x": 1}]),
    ):
        bad = json.loads(json.dumps(base))
        bad["material"]["input"][where] = extra
        with pytest.raises(signcontent.PlainUnavailable):
            build(bad)


def test_notes_are_a_list():
    g = sample("grant.issued")
    c = build({"op": "grant", "events": [g], "material": material_for("grant.issued", g)})
    assert isinstance(c["notes"], list) and "note" not in c
