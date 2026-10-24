# ruff: noqa: E501
"""B6 (owner decision E): the signing review in three layers: a headline, the content formatted like the ticket
page, the prompt; the full dump behind ``d`` / ``--details``. Same signed material; only the layout changes."""

from __future__ import annotations

import json
import os
import pty
import re
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
    # A-3: the dashboard says "check that the verification code matches the one in Terminal": the same 12 hex, labelled,
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


HUB_ONLY = {
    "channel", "channel_factor", "hub_id", "snapshot_version", "channel_cert", "uk_cert", "channel_proof", "item_hash",
}  # fmt: skip


def schema_properties(typ: str) -> set[str]:
    from orch import schema

    props = set(schema.load(f"event.{typ}")["properties"]) - review.ENVELOPE - SCHEMA_STAMPS
    # C3b: the hub fields (auth "hub" only) are never part of an event a person signs here; the hub's two signatures
    # cover them. An unknown key in a locally signed event still falls back to the full review.
    return props - HUB_ONLY if "channel_proof" in props else props


@pytest.mark.parametrize("typ", signable_types())
def test_every_schema_property_of_a_person_event_is_classified(typ):
    """A key added to an event schema fails here until it is either shown by the plain view or in TECHNICAL_KEYS."""
    assert typ in signcontent.SHOWN, f"classify {typ} in signcontent.SHOWN"
    unclassified = schema_properties(typ) - signcontent.SHOWN[typ] - signcontent.TECHNICAL_KEYS
    assert not unclassified, f"{typ}: classify {sorted(unclassified)} in signcontent.SHOWN or TECHNICAL_KEYS"
    assert signcontent.SHOWN[typ] <= schema_properties(typ) | {"set"}, f"{typ}: SHOWN names a key the schema lacks"


VALUES = {
    "log": "marker-log", "receipt": {"cmd": "marker-command", "exit": 0, "ms": 1, "repo": None, "commit": None},
    "name": "Marker-Name", "kind": "marker-kind", "label": "marker-label", "ac": "AC9", "task": "T9", "ref": "marker-ref",
    "addon": "marker-addon", "bytes": 4242, "sha256": "sha256:" + "ab" * 32, "version": "9.9.9", "package_sha256": "sha256:" + "cd" * 32,
    "capabilities": ["marker-cap"], "artifact_kinds": ["marker-akind"], "fields": ["marker-field"], "replaces": "marker-old",
    "device": "d_" + "1" * 32, "cert": CERT, "device_cert": CERT, "pk_pub": "marker-pubkey", "person": "p_" + "2" * 32,
    "role": "maintainer", "outcome": "pass", "text": "marker text", "source_sha": [{"repo": "app", "ref": "refs/heads/x", "sha": "a" * 40}],
    "grant": "gr_" + "3" * 26, "scope": "workable", "verbs": "agent", "hours": 7, "issued_at": "2026-10-16T10:00:00Z",
    "expires_at": "2026-10-16T17:00:00Z", "reason": "marker-reason", "question": "q1", "option": "a", "qid": "q9",
    "duplicate_of": "DEMO-0007", "resolution": "duplicate", "key": "DEMO-0002", "ticket_type": "chore", "title": "Marker Title",
    "owner": "p_" + "4" * 32, "add": ["p_" + "7" * 32], "remove": ["p_" + "8" * 32], "from": "open", "to": "backlog", "gate": "plan",
    "gates": {"plan": POLICY}, "set": {"grant_hours": 5},
    "runtime": {"kind": "python-requirements", "lock": "marker-lock.txt", "packages": 3},
    "old": "d_" + "5" * 32, "rotation": {"o": {"kind": "device_rotation", "marker": "rot"}, "sig": "s"},
    "hub_id": "38d8abed52ccfb538da401fd54e64bdc", "hk_root_pub": "marker-hubroot", "mode": "selective", "binding_seq": 3,
    "aliases": [{"person_id": "9" * 32, "user_id": "a" * 32}], "sections": {"summary": {"hash": "sha256:" + "ee" * 32, "refs": ["marker-art"]}},
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
    out = {
        "kind": "payload",
        "ticket": ticket,
        "texts": {"summary": "marker summary"},
        "before": {"ticket.priority": "low", "grant_hours": 8},
    }
    if typ == "grant.revoked":
        out["grants"] = {event["grant"]: _info(event)}
    return out


def _info(ev: dict, **over) -> dict:
    """The host's word about an access taken back (``material["grants"]``): the person's own runner access."""
    return {
        "label": "runner",
        "scope": "workable",
        "expires_at": "2026-10-25T00:37:00Z",
        "person": ev["actor"]["id"],
        **over,
    }


def flat(value) -> str:
    return json.dumps(value, sort_keys=True)


@pytest.mark.parametrize("typ", signable_types())
def test_the_plain_view_shows_every_key_it_claims_to_show(typ):
    event = sample(typ)
    content = build({"op": "x", "events": [event], "material": material_for(typ, event)})
    text = flat(content) + "\n".join(signcontent.text(content, signer._clean))
    for key in signcontent.SHOWN[typ] - signcontent.DETAILS_ONLY.get(typ, frozenset()):
        value = event[key]
        needles = []
        if isinstance(value, str) and value.startswith("sha256:"):
            needles.append(value[7:19])  # a digest is shown short
        elif key in ("pk_pub", "cert", "device_cert", "hk_root_pub", "rotation"):
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
        elif key == "aliases":
            needles.append("a" * 32)  # the hub name of the person
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
    assert "scope" not in rows and rows["who can use it"] == "agents working for you"  # K4: scope/verbs are in words
    assert c["headline"].startswith("Agents may work on any ticket you can see, as you, for 7 hours (until ")
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


def _tz(monkeypatch, name):
    import time

    monkeypatch.setenv("TZ", name)
    time.tzset()


@pytest.fixture
def restore_tz():
    import os
    import time

    old = os.environ.get("TZ")
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_k4_the_grant_review_is_one_plain_sentence_in_local_time(restore_tz, monkeypatch):
    """The headline says what agents may do and until when, in this computer's time with the zone name; scope, verbs,
    the grant id and the UTC times are not in the plain view (they are in the technical details)."""
    g = {
        **sample("grant.issued"),
        "hours": 24,
        "issued_at": "2026-10-20T21:45:37Z",
        "expires_at": "2026-10-21T21:45:37Z",
    }
    request = {"op": "grant", "events": [g], "material": material_for("grant.issued", g)}
    _tz(monkeypatch, "Europe/Zurich")
    c = build(request)
    assert (
        c["headline"]
        == "Agents may work on any ticket you can see, as you, for 1 day (until Wed 23:45 CEST, your time)"
    )
    assert [(r["field"], r["after"]) for r in c["fields_changed"]] == [
        ("who can use it", "agents working for you"),
        ("label", "marker-label"),
    ]
    assert c["notes"] == ["Scope, verbs, grant id and the exact times (UTC) are in the technical details."]
    shown = "\n".join(signcontent.text(c, signer._clean))
    for raw in ("2026-10-2", "gr_" + "3" * 26, "workable", "verbs agent", "scope workable"):
        assert raw not in shown + c["headline"]
    _tz(monkeypatch, "America/New_York")
    assert build(request)["headline"].endswith("(until Wed 17:45 EDT, your time)")
    _tz(monkeypatch, "UTC")
    assert build(request)["headline"].endswith("(until Wed 21:45 UTC, your time)")
    long = {**g, "hours": 168, "expires_at": "2026-10-27T21:45:37Z"}
    assert (
        "(until Tue 27 Oct 22:45 UTC" not in build({**request, "events": [long]})["headline"]
    )  # 22:45 is CET, not UTC
    assert build({**request, "events": [long]})["headline"].endswith(
        "for 7 days (until Tue 27 Oct 21:45 UTC, your time)"
    )


def test_k4_other_scopes_and_unknown_times_keep_the_technical_review(restore_tz, monkeypatch):
    _tz(monkeypatch, "UTC")
    for change in ({"scope": "all"}, {"verbs": ["a.b"]}):
        g = {**sample("grant.issued"), **change}
        c = build({"op": "grant", "events": [g], "material": material_for("grant.issued", g)})
        rows = {r["field"]: r["after"] for r in c["fields_changed"]}
        assert {"scope", "verbs", "from", "until", "grant id"} <= set(rows)
        assert c["headline"].startswith("Give agents access for 7 hours (scope ")


def test_f7_the_ordinary_grant_with_times_set_at_signing_says_what_agents_may_do_without_scope_and_verbs(
    restore_tz, monkeypatch
):
    """The setup review shows the runner grant before its times exist: plain words, no "(scope workable, verbs agent)"."""
    _tz(monkeypatch, "UTC")
    g = {**sample("grant.issued"), "hours": 72, "issued_at": "set at signing", "expires_at": "set at signing"}
    c = build({"op": "grant", "events": [g], "material": material_for("grant.issued", g)})
    assert c["headline"] == "Agents may work on any ticket you can see, as you, for 3 days"
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert "scope" not in rows and "verbs" not in rows and "grant id" not in rows
    assert rows["until"] == "set at signing"  # the signed value is still on a row
    assert "scope workable" not in "\n".join(signcontent.text(c, signer._clean))
    # any other scope or verbs is never put in words: the technical rows stay
    other = {**g, "scope": "all"}
    c = build({"op": "grant", "events": [other], "material": material_for("grant.issued", other)})
    assert {"scope", "verbs", "grant id"} <= {r["field"] for r in c["fields_changed"]}


def test_f7_a_grant_that_is_not_ordinary_shows_its_times_in_local_time_and_as_signed(restore_tz, monkeypatch):
    _tz(monkeypatch, "Europe/Zurich")
    g = {**sample("grant.issued"), "scope": "all", "hours": 24}
    g.update(issued_at="2026-10-20T21:45:37Z", expires_at="2026-10-21T21:45:37Z")
    c = build({"op": "grant", "events": [g], "material": material_for("grant.issued", g)})
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert rows["until"] == "Wed 23:45 CEST (2026-10-21T21:45:37Z)"
    assert rows["from"] == "Tue 23:45 CEST (2026-10-20T21:45:37Z)"


def test_k4_the_details_still_carry_every_signed_field_of_the_grant(human, served):
    p = human.prepare("grant", {"hours": 24, "scope": "workable"})
    review = signer.plain(p, WS)
    ev = p["events"][0]
    for value in (ev["grant"], ev["scope"], ev["issued_at"], ev["expires_at"], "agent"):
        assert value in review.details
        assert value not in review.body if hasattr(review, "body") else True
    assert "Agents may work on any ticket you can see, as you, for 1 day (until " in str(review)


def test_b8_lengths_read_as_days_and_hours_in_the_headline_and_the_settings_note(human, served):
    g = {**sample("grant.issued"), "hours": 72}
    c = build({"op": "grant", "events": [g], "material": material_for("grant.issued", g)})
    assert c["headline"].startswith("Agents may work on any ticket you can see, as you, for 3 days (")
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
    assert rows["addressed to (per host)"] == "p_" + "7" * 32 and rows["blocks the ticket (per host)"] == "yes"
    assert c["fields_title"] == "About this question"  # no "- ->" before values
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


def test_settings_repos_read_as_labelled_rows_per_repo(human, served):
    event = {
        **sample("settings.changed"),
        "set": {
            "repos": {
                "api": {"path": "/work/api2", "remote": "https://example.org/a/api", "default_branch": "main"},
                "gone": None,
                "web": {"path": "/work/web"},
            }
        },
    }
    mat = material_for("settings.changed", event)
    mat["before"] = {"repos": {"api": {"path": "/work/api"}, "gone": {"path": "/work/gone"}, "web": None}}
    c = build({"op": "settings.set", "events": [event], "material": mat})
    rows = {r["field"]: r for r in c["fields_changed"]}
    assert rows["repository api"]["after"] == "changed" and rows["repository web"]["after"] == "added"
    assert rows["repository gone"]["after"] == "removed (the folder and its files stay on disk)"
    assert (rows["repository api: folder"]["before"], rows["repository api: folder"]["after"]) == (
        "/work/api",
        "/work/api2",
    )
    assert (rows["repository api: remote"]["before"], rows["repository api: remote"]["after"]) == (
        "(none)",
        "https://example.org/a/api",
    )
    assert rows["repository api: main branch"]["after"] == "main"
    assert (
        rows["repository web: remote"]["after"] == "(none)" and rows["repository web: main branch"]["after"] == "(none)"
    )
    assert (
        rows["repository gone: folder"]["before"] == "/work/gone"
        and rows["repository gone: folder"]["after"] == "(removed)"
    )
    assert rows["repository api"]["before_note"] == "per host, not signed"


def test_a_repo_change_shows_unchanged_fields_once_without_an_arrow(human, served):
    event = {
        **sample("settings.changed"),
        "set": {
            "repos": {"api": {"path": "/work/api", "remote": "https://example.org/a/api", "default_branch": "dev"}}
        },
    }
    mat = material_for("settings.changed", event)
    mat["before"] = {
        "repos": {"api": {"path": "/work/api", "remote": "https://example.org/a/api", "default_branch": "main"}}
    }
    c = build({"op": "settings.set", "events": [event], "material": mat})
    rows = {r["field"]: r for r in c["fields_changed"]}
    assert (rows["repository api: main branch"]["before"], rows["repository api: main branch"]["after"]) == (
        "main",
        "dev",
    )
    for label, value in (("folder", "/work/api"), ("remote", "https://example.org/a/api")):
        row = rows[f"repository api: {label} (unchanged, per host)"]
        assert row["before"] is None and row["after"] == value  # the signed value stays on the screen
        assert f"repository api: {label}" not in rows


def _repo_event_rows(entry):
    event = {**sample("settings.changed"), "set": {"repos": {"api": entry}}}
    c = build({"op": "settings.set", "events": [event], "material": material_for("settings.changed", event)})
    return c, {r["field"]: r["after"] for r in c["fields_changed"]}


def test_a_repo_value_cannot_pose_as_another_field(human, served):
    spoof = "/x \u00b7 remote git@good:org/r \u00b7 main branch trunk"
    c, rows = _repo_event_rows({"path": spoof, "remote": "https://example.org/real"})
    assert rows["repository api: folder"] == spoof  # one value on the Folder row, whole
    assert rows["repository api: remote"] == "https://example.org/real"  # the real remote has its own row
    assert rows["repository api: main branch"] == "(none)"
    assert not any("git@good" in v for k, v in rows.items() if not k.endswith(": folder"))


def test_a_repo_newline_and_bidi_never_reach_a_row_raw(human, served):
    c, rows = _repo_event_rows({"path": "/x\n/y", "remote": "https://e.org/a\u202eb", "default_branch": "m\x00ain"})
    assert "\n" not in rows["repository api: folder"] and "/x ⏎ /y" == rows["repository api: folder"]
    text = "\n".join(signcontent.text(c, signer._clean))
    for bad in ("\u202e", "\x00"):
        assert bad not in text
    assert text.count("repository api: remote") == 1


def test_an_empty_repo_remote_is_none_not_blank(human, served):
    c, rows = _repo_event_rows({"path": "/x", "remote": "", "default_branch": ""})
    assert rows["repository api: remote"] == "(none)" and rows["repository api: main branch"] == "(none)"


def test_an_unplain_repo_entry_keeps_the_whole_value_row(human, served):
    event = {**sample("settings.changed"), "set": {"repos": {"api": {"path": "/a", "extra": "x"}}}}
    c = build({"op": "settings.set", "events": [event], "material": material_for("settings.changed", event)})
    (row,) = c["fields_changed"]
    assert row["field"] == "repos" and "extra" in row["after"]


@pytest.mark.parametrize("marker", ["(none)", "(removed)"])
def test_a_value_spelled_like_a_marker_keeps_the_whole_value_row(human, served, marker):
    for entry in ({"path": "/x", "default_branch": marker}, {"path": marker}):
        c, rows = _repo_event_rows(entry)
        assert list(rows) == ["repos"] and marker in rows["repos"]  # the generic row: every byte, no plain rows


def test_a_repo_name_outside_the_host_spelling_keeps_the_whole_value_row(human, served):
    event = {**sample("settings.changed"), "set": {"repos": {"Bad Name": {"path": "/x"}}}}
    c = build({"op": "settings.set", "events": [event], "material": material_for("settings.changed", event)})
    assert [r["field"] for r in c["fields_changed"]] == ["repos"]


def test_the_repo_verdict_row_is_always_labelled_per_host(human, served):
    c, _ = _repo_event_rows({"path": "/x"})  # an add: no old value, the verdict still comes from the host's before
    verdict = next(r for r in c["fields_changed"] if r["field"] == "repository api")
    assert verdict["before_note"] == "per host, not signed"


# --- F7: plain reviews of add-ons, answers and people ---------------------------------------------------------------


def _addon(**change):
    ev = {
        "type": "addon.granted", "name": "terminals", "version": "0.1.0", "package_sha256": "sha256:" + "aa" * 32,
        "capabilities": ["pty"], "artifact_kinds": [], "fields": {}, "sections": [],
    }  # fmt: skip
    ev.update(change)
    return {"op": "addon.grant", "events": [ev], "material": {"kind": "payload", "ticket": None}}


def test_g4_an_addon_grant_with_dependencies_is_never_the_short_view_and_says_what_is_installed():
    runtime = {"kind": "python-requirements", "lock": "requirements.txt", "packages": 3}
    c = build(_addon(runtime=runtime))
    assert c["headline"] == "Grant the add-on terminals"
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert "dependencies" in rows and "requirements.txt" in rows["dependencies"] and "3" in rows["dependencies"]
    assert any("Also installs 3 Python packages listed in requirements.txt after you sign" in n for n in c["notes"])
    assert any("no package scripts run" in n for n in c["notes"])
    one = build(_addon(runtime={**runtime, "kind": "node-package-lock", "lock": "package-lock.json", "packages": 1}))
    assert any("Also installs 1 Node package listed in package-lock.json" in n for n in one["notes"])
    for bad in (
        {**runtime, "kind": "go-mod"},
        {**runtime, "extra": 1},
        {"kind": "python-requirements", "lock": "x"},
        {**runtime, "packages": "3"},
        {**runtime, "packages": 0},
        {**runtime, "lock": ["x"]},
        "pip",
    ):
        with pytest.raises(signcontent.PlainUnavailable):
            build(_addon(runtime=bad))


def test_f7_an_addon_grant_says_what_the_addon_may_do_and_keeps_digest_and_raw_fields_out_of_the_main_view():
    c = build(_addon())
    assert c["headline"] == "Turn on Terminals: it says it will open terminals on this computer, as you"
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert rows == {
        "add-on": "terminals",
        "version": "0.1.0",
        "it says it will": "open terminals on this computer, as you",
    }
    assert signcontent.ADDON_TRUST in c["notes"]
    shown = flat(c) + "\n".join(signcontent.text(c, signer._clean))
    for raw in ("aa" * 6, "{}", "(none)", "pty"):
        assert raw not in shown
    assert "package digest" not in {r["field"] for r in c["fields_changed"]}
    assert "technical details" in " ".join(c["notes"])
    two = build(_addon(name="start-agent", capabilities=["pty", "spawn_agent"]))
    assert two["headline"].startswith("Turn on Start agent: it says it will open terminals")
    assert "start agents on this computer" in two["headline"]
    none = build(_addon(name="worktrees", capabilities=[]))
    assert none["headline"] == "Turn on Worktrees: it declares no capabilities"
    assert signcontent.ADDON_TRUST in none["notes"]


def _explorer_digest():
    from orch.addons import builtin
    from orch.addons.package import MANIFEST_FILE, package_digest

    return package_digest({MANIFEST_FILE: builtin.manifest_bytes("explorer")})


def test_the_builtin_file_explorer_says_that_it_lets_owners_read_files():
    c = build(_addon(name="explorer", capabilities=[], package_sha256=_explorer_digest()))
    assert c["headline"] == (
        "Turn on File Explorer: owners can read this workspace's files and git history in the dashboard"
    )
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert rows["it lets owners"] == (
        "read every file of this workspace and its declared repositories, and their git history "
        "(read only; keys, .env files, .git and orch's own files stay locked)"
    )
    assert "declares no capabilities" not in flat(c) and signcontent.ADDON_TRUST in c["notes"]
    # the name alone is not enough: another package called explorer keeps the plain "declares no capabilities" words
    other = build(_addon(name="explorer", capabilities=[], package_sha256="sha256:" + "bb" * 32))
    assert other["headline"] == "Turn on Explorer: it declares no capabilities"


@pytest.mark.parametrize(
    "change",
    [
        {"capabilities": ["pty", "teleport"]},  # a capability the plain words do not know
        {"capabilities": "pty"},
        {"capabilities": ["pty", "pty"]},
        {"fields": {"x": {"type": "string"}}},  # it declares something: every value stays a row
        {"sections": [{"id": "s"}]},
        {"artifact_kinds": ["report"]},
        {"name": "Terminals"},  # not a plain add-on name
        {"name": "ter\nminals"},
        {"version": "1.0\nfake row"},
        {"version": None},
    ],
)
def test_f7_an_addon_with_anything_odd_keeps_every_signed_value_as_a_row(change):
    c = build(_addon(**change))
    fields = {r["field"] for r in c["fields_changed"]}
    assert {"version", "package digest", "capabilities"} <= fields
    assert c["headline"].startswith("Grant the add-on ")
    assert "it says it will" not in fields


def test_f7_turning_an_addon_off_is_plain():
    ev = {"type": "addon.disabled", "name": "worktrees"}
    c = build({"op": "addon.disable", "events": [ev], "material": {"kind": "payload", "ticket": None}})
    assert c["headline"] == "Turn off Worktrees"
    odd = {**ev, "name": "Work Trees"}
    assert (
        build({"op": "addon.disable", "events": [odd], "material": {"kind": "payload", "ticket": None}})["headline"]
        == "Disable the add-on Work Trees"
    )


def _answer(question_extra=None, option="hello", text=None):
    ev = {"type": "question.answered", "question": "Q1", "option": option, "hash": "sha256:" + "ab" * 32}
    if text:
        ev["text"] = text
    q = {
        "id": "Q1", "text": "Which greeting should I write?", "to": "owner", "blocking": True, "recommended": "hello",
        "why": "I need to know before I write the file.",
        "options": [{"key": "hello", "label": "Hello"}, {"key": "hi", "label": "Hi"}],
    }  # fmt: skip
    q.update(question_extra or {})
    return {
        "op": "answer", "events": [ev],
        "material": {"kind": "question", "ticket": {"key": "RA-0001", "uid": UID}, "question": q},
    }  # fmt: skip


def test_f7_an_answer_to_a_question_with_why_and_a_recommendation_is_plain():
    c = build(_answer())
    assert c["headline"].startswith("Answer “Which greeting should I write?” on RA-0001") and c["headline"].endswith(
        "with Hello"
    )
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert rows["your choice"] == "Hello (option hello)"
    assert rows["option hello"] == "Hello" and rows["option hi"] == "Hi"
    assert rows["recommended option"] == "Hello"  # bound by the question hash since #1123
    assert any(s["title"] == "Why it was asked" and "before I write" in s["text"] for s in c["sections"])
    assert c["ticket"]["key"] == "RA-0001"
    view = "\n".join(signcontent.text(c, signer._clean))
    assert "{" not in view and "options" not in view.replace("option ", "")


def test_f7_an_answer_outside_the_options_says_so_and_a_new_question_key_still_falls_back():
    c = build(_answer(option="nope"))
    assert {r["field"]: r["after"] for r in c["fields_changed"]}["your choice"] == "nope (not one of the options)"
    with pytest.raises(signcontent.PlainUnavailable):
        build(_answer({"surprise": 1}))


def test_f7_two_options_with_the_same_label_are_told_apart_in_the_choice_row():
    opts = {"options": [{"key": "a", "label": "Yes"}, {"key": "b", "label": "Yes"}], "recommended": "a"}
    for key in ("a", "b"):
        c = build(_answer(opts, option=key))
        assert {r["field"]: r["after"] for r in c["fields_changed"]}["your choice"] == f"Yes (option {key})"


def test_f7_the_note_is_titled_your_note_and_a_label_equal_to_the_key_is_not_repeated():
    c = build(_answer(text="because"))
    assert [s["title"] for s in c["sections"] if s["id"] == "answer"] == ["Your note"]
    c = build(_answer({"options": [{"key": "Hello", "label": "Hello"}]}, option="Hello"))
    assert {r["field"]: r["after"] for r in c["fields_changed"]}["your choice"] == "Hello"
    # label == key stays suffixed when another option carries that label: {a: "b"} next to {b: "b"}
    dup = {"options": [{"key": "a", "label": "b"}, {"key": "b", "label": "b"}], "recommended": "a"}
    for key in ("a", "b"):
        c = build(_answer(dup, option=key))
        assert {r["field"]: r["after"] for r in c["fields_changed"]}["your choice"] == f"b (option {key})"
    # a free-text answer (no option) keeps "Your answer" for its text
    c = build(_answer(option=None, text="my words"))
    assert [s["title"] for s in c["sections"] if s["id"] == "answer"] == ["Your answer"]


_ME = "p_42e1c38e0488b742" + "0" * 14
MEMBERS = [{"person": _ME, "name": "Severin"}]


def test_f7_the_terminal_shows_a_person_as_name_and_short_id_by_the_dashboard_rules():
    c = {"headline": "Create ticket", "fields_changed": [{"field": "owner", "before": None, "after": _ME}]}
    out = "\n".join(signcontent.text(c, signer._clean, members=MEMBERS))
    assert "owner: Severin (p_42e1c38e0488…; name per host, not signed)" in out and _ME not in out
    out = "\n".join(signcontent.text(c, signer._clean, members=[]))
    assert "owner: p_42e1c38e0488… (unknown person)" in out and _ME not in out
    out = "\n".join(signcontent.text(c, signer._clean, members=[{"person": _ME, "name": "Sev​（x"}]))
    assert "(name not shown)" in out and "Sev" not in out.split("owner:")[1]
    # members not known (None): the id stays as signed, nothing is hidden
    assert _ME in "\n".join(signcontent.text(c, signer._clean))
    # a value that is not a person id is untouched
    c["fields_changed"][0]["after"] = "p_nothex"
    assert "owner: p_nothex" in "\n".join(signcontent.text(c, signer._clean, members=MEMBERS))


def test_f7_members_of_reads_member_list_and_never_raises():
    rows = {"data": {"members": [{"person": _ME, "name": "Severin", "role": "owner", "devices": []}, {"x": 1}]}}
    assert signer.members_of(lambda op, args: rows) == [{"person": _ME, "name": "Severin"}]

    def boom(op, args):
        raise ConnectionError("down")

    assert signer.members_of(boom) == []
    assert signer.members_of(lambda op, args: {"data": None}) == []


def _owner_row(members):
    c = {"headline": "x", "fields_changed": [{"field": "owner", "before": None, "after": _ME}]}
    return [x for x in signcontent.text(c, signer._clean, members=members) if "owner:" in x][0]


def test_f7_a_host_name_never_replaces_or_hides_the_id():
    other = "p_" + "9" * 32
    # a name that imitates ANOTHER member's name (here a Cyrillic look-alike of "Mara"): both are shared, no name at all
    twins = [{"person": _ME, "name": "M\u0430ra"}, {"person": other, "name": "Mara"}]
    row = _owner_row(twins)
    assert "p_42e1c38e0488…" in row and "Mara" not in row and "M\u0430ra" not in row and "shared by 2" in row
    # a name that carries an id lookalike (also fullwidth brackets): not shown, the real id is
    for fake in ("Mara (p_" + "9" * 12 + ")", "\uff08p_" + "9" * 12 + "\uff09", "p_" + "9" * 32):
        row = _owner_row([{"person": _ME, "name": fake}])
        assert "p_42e1c38e0488…" in row and "p_999999999999" not in row and "name not shown" in row
    # a name shown at all is marked as the host's word and sits next to the id
    row = _owner_row([{"person": _ME, "name": "Severin"}])
    assert row.strip() == "owner: Severin (p_42e1c38e0488…; name per host, not signed)"
    # empty, missing or malformed member lists: the short id only
    for members in (
        [],
        [{"person": other, "name": "Mara"}],
        [{"person": _ME}],
        [{"person": _ME, "name": None}],
        [{"person": _ME, "name": 7}],
    ):
        row = _owner_row(members)
        assert "p_42e1c38e0488…" in row and "per host, not signed" not in row, members
    assert _ME in "\n".join(
        signcontent.text(
            {"headline": "x", "fields_changed": [{"field": "owner", "before": None, "after": _ME}]}, signer._clean
        )
    )


def test_f7_a_question_with_the_same_option_key_twice_falls_back():
    q = _answer()
    q["material"]["question"]["options"].append({"key": "hello", "label": "Other"})
    with pytest.raises(signcontent.PlainUnavailable):
        build(q)


def _stop(grant: str, reason: str | None = None) -> dict:
    ev = {"type": "grant.revoked", "actor": sample("grant.issued")["actor"], "grant": grant}
    return {**ev, "reason": reason} if reason else ev


def _revoke(*pairs, op="grant.revoke"):
    """A request taking back several accesses: ``pairs`` are (event, info-overrides or None for "host said nothing")."""
    events = [ev for ev, _ in pairs]
    grants = {ev["grant"]: _info(ev, **over) for ev, over in pairs if over is not None}
    return {"op": op, "events": events, "material": {**material_for("grant.revoked", events[0]), "grants": grants}}


def test_f8_a_stop_says_which_access_in_words_and_keeps_the_id_out_of_them():
    gid = "gr_01M57ABCDEFGHJKMNPQRZMPS4"
    ev = _stop(gid)
    c = build({"op": "grant.revoke", "events": [ev], "material": material_for("grant.revoked", ev)})
    assert c["headline"] == "Take back agent access"  # signed words only
    row = c["fields_changed"][0]
    assert row["after"].startswith("Agent access for this Mac's runner, until ") and row["after"].endswith(
        "(per host, not signed)"
    )
    shown = "\n".join(signcontent.text(c, signer._clean)) + c["headline"] + repr(c)
    assert "gr_" not in shown and gid[-10:] not in shown  # the id is in the technical details (the full review)
    assert "per host, not signed" in shown and "technical details" in shown


def test_f8_an_extend_names_the_replaced_access_by_words_only():
    new = {**sample("grant.issued"), "grant": "gr_0NEWAAAAAAAAAAAAAAAAAAAAA", "label": "runner"}
    old = _stop("gr_0OLDBBBBBBBBBBBBBBBBBBBBB", "extended")
    m = {**material_for("grant.issued", new), "grants": {old["grant"]: _info(old)}}
    c = build({"op": "grant", "events": [new, old], "material": m})
    assert "gr_" not in repr(c)  # neither id is in the plain view: both are in the technical details
    line = next(x for x in c["summary"]["lines"] if "Per host, not signed: that is the agent access" in x)
    assert "for this Mac's runner, until " in line
    same = _stop(new["grant"], "extended")
    with pytest.raises(signcontent.PlainUnavailable):  # the caller then shows the full review
        build({"op": "grant", "events": [new, same], "material": {**m, "grants": {same["grant"]: _info(same)}}})


def test_788_an_extend_whose_old_access_is_not_the_runners_gets_the_full_review():
    new = {**sample("grant.issued"), "grant": "gr_0NEWAAAAAAAAAAAAAAAAAAAAA", "label": "runner"}
    old = _stop("gr_0OLDBBBBBBBBBBBBBBBBBBBBB", "extended")
    base = material_for("grant.issued", new)
    for grants in (
        {old["grant"]: _info(old, label="ci")},  # another label
        {old["grant"]: _info(old, scope="all")},  # another scope
        {old["grant"]: _info(old, person="p_" + "9" * 32)},  # another person's access
        {old["grant"]: _info(old, expires_at="soon")},  # no usable end
        {},  # the host said nothing
        {"gr_0OTHERCCCCCCCCCCCCCCCCCCC": _info(old)},  # about some other access
    ):
        with pytest.raises(signcontent.PlainUnavailable):
            build({"op": "grant", "events": [new, old], "material": {**base, "grants": grants}})
    with pytest.raises(signcontent.PlainUnavailable):
        build({"op": "grant", "events": [new, old], "material": base})  # no grants key at all


def test_788_a_stop_without_the_hosts_word_gets_the_full_review():
    ev = _stop("gr_0OLDBBBBBBBBBBBBBBBBBBBBB")
    for over in (None, {"expires_at": "x"}, {"person": "p_" + "9" * 32}):
        with pytest.raises(signcontent.PlainUnavailable):
            build(_revoke((ev, over)))


def test_788_several_stops_say_how_many_and_keep_each_access_apart():
    a, b, c3 = (_stop(f"gr_0OLDBBBBBBBBBBBBBBBBBBB{ch}{ch}") for ch in "ABC")
    c = build(
        _revoke(
            (a, {"expires_at": "2026-10-25T00:37:00Z"}),
            (b, {"expires_at": "2026-10-26T00:37:00Z"}),
            (c3, {"expires_at": "2026-10-27T00:37:00Z", "label": "ci"}),
        )
    )
    assert c["headline"] == "Take back 3 agent accesses"
    rows = [r["after"] for r in c["fields_changed"] if r["field"].startswith("access taken back")]
    assert len(rows) == 3 and len(set(rows)) == 3 and c["fields_changed"][0]["field"].endswith("1/3")
    assert "gr_" not in repr(c)


def test_788_identical_looking_accesses_are_never_merged():
    a, b = _stop("gr_0OLDAAAAAAAAAAAAAAAAAAAAA"), _stop("gr_0OLDBBBBBBBBBBBBBBBBBBBBB")
    with pytest.raises(signcontent.PlainUnavailable):  # same words for two accesses: the full review (with ids)
        build(_revoke((a, {}), (b, {})))


def test_f8_two_different_accesses_with_one_short_form_are_refused_in_the_plain_view():
    new = {**sample("grant.issued"), "grant": "gr_0AAAAAAAAAAAAAAAAAAAAAAAAA"}
    old = _stop("gr_0BBBBBBBBBBAAAAAAAAAAAAAAA"[:3] + "1" + "gr_0AAAAAAAAAAAAAAAAAAAAAAAAA"[4:], "extended")
    assert old["grant"][-10:] == new["grant"][-10:] and old["grant"] != new["grant"]
    m = {**material_for("grant.issued", new), "grants": {old["grant"]: _info(old)}}
    with pytest.raises(signcontent.PlainUnavailable):
        build({"op": "grant", "events": [new, old], "material": m})


# --- V4: the plain lead of a share approval and of an Extend (WYSIWYS: a summary never replaces the signed text) ----------


def _share_text(kind="secret", views=0, days=7, names=("report.md", "data.csv")):
    from orch.publishclient import approval

    files = [{"name": n, "size": 1200 + i, "sha256": f"{i + 1:x}" * 64} for i, n in enumerate(names)]
    return approval.approval_text(
        staged_id="ps_" + "a" * 32,
        ticket_key="DEMO-0001",
        origin="person",
        kind=kind,
        days=days,
        views=views,
        files=files,
        server="https://pub.example.com",
        share="https://share.example.com",
        manifest="sha256:" + "ab" * 32,
    )


def _share_build(text, op="publish.share"):
    ev = {**sample("log.added"), "text": text}
    return build({"op": op, "events": [ev], "material": material_for("log.added", ev)})


def test_v4_a_share_approval_leads_with_a_plain_summary_and_keeps_the_signed_text():
    text = _share_text()
    c = _share_build(text)
    lines = c["summary"]["lines"]
    assert c["summary"]["title"] == "Share these files"
    joined = "\n".join(lines)
    for needle in ("report.md", "data.csv", "https://pub.example.com", "https://share.example.com", "7 days"):
        assert needle in joined
    assert "Anyone who has the link can open the files." in joined
    assert [s for s in c["sections"] if s["id"] == "share"][0]["text"] == text  # the exact signed text is still there


def test_v4_a_limited_view_link_says_how_many_views():
    c = _share_build(_share_text(kind="once", views=3, days=1))
    joined = "\n".join(c["summary"]["lines"])
    assert "at most 3 times" in joined and "1 day " in joined


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace("Works for: 7 days", "Works for: 7 weeks"),
        lambda t: t + "\nExtra: line",
        lambda t: t.replace("Prepared by: you", "Prepared by: someone"),
        lambda t: t.replace("- report.md  (", "- report.md\nx  ("),
        lambda t: t.replace("Files (2,", "Files (3,"),
        lambda t: t.replace("The files are sent to: https://pub.example.com", "The files are sent to: a b"),
    ],
)
def test_v4_an_odd_share_text_gets_no_summary_but_still_the_whole_text(mutate):
    text = mutate(_share_text())
    c = _share_build(text)
    assert "summary" not in c
    assert [s for s in c["sections"] if s["id"] == "share"][0]["text"] == text


def _extend(**over):
    new = {
        **sample("grant.issued"),
        "grant": "gr_0NEWAAAAAAAAAAAAAAAAAAAAA",
        "label": "runner",
        "scope": "workable",
        "verbs": "agent",
        "hours": 72,
        **over,
    }
    old = _stop("gr_0OLDBBBBBBBBBBBBBBBBBBBBB", "extended")
    material = {**material_for("grant.issued", new), "grants": {old["grant"]: _info(old)}}
    return build({"op": "grant", "events": [new, old], "material": material})


def test_v4_an_extend_is_titled_and_says_running_agents_stop():
    c = _extend(expires_at="2026-10-21T12:00:00Z")
    assert c["headline"].startswith("Extend agent access ") and c["summary"]["title"] == "Extend agent access"
    joined = "\n".join(c["summary"]["lines"])
    assert "per host, not signed" in joined and "3 days" in joined and "ends " in joined
    everything = repr(c) + "\n".join(signcontent.text(c, signer._clean))
    assert "gr_" not in everything  # ids are in the technical details only (issue 788)
    assert c["headline"].startswith("Extend agent access for this Mac's runner, for 3 days")
    # the warning is said once, in the notes (the terminal prints no summary lines; the dashboard shows both)
    assert repr(c).count("Running agents will stop.") == 1
    assert "\n".join(signcontent.text(c, signer._clean)).count("Running agents will stop.") == 1
    assert "Running agents will stop." in "\n".join(c["notes"])


def test_v4_an_extend_with_other_scope_verbs_or_label_keeps_the_generic_review():
    for over in ({"scope": "all"}, {"verbs": "all"}, {"label": "other"}, {"hours": "x"}):
        c = _extend(**over)
        assert "summary" not in c and not c["headline"].startswith("Extend agent access"), over


def test_v4_only_one_share_event_of_the_share_op_is_summarised():
    one_text = _share_text()
    other = _share_text(names=("other.txt",))
    ev1, ev2 = ({**sample("log.added"), "text": t} for t in (one_text, other))
    two = build({"op": "publish.share", "events": [ev1, ev2], "material": material_for("log.added", ev1)})
    assert "summary" not in two  # the first share's files are never hidden behind the second's summary
    assert "summary" not in _share_build(one_text, op="log.add")  # not the share op: the text alone


def test_v4_the_terminal_view_of_an_extend_keeps_the_length_and_the_end():
    c = _extend(expires_at="2026-10-21T12:00:00Z")
    first = signcontent.text(c, signer._clean)[0]
    assert "3 days" in first and "until " in first and "your time" in first and "gr_" not in first


def test_g2_an_estimate_says_points_and_that_it_cancels_approvals(human, served):
    _, key = human.ticket("Owner CLI test")
    c = build(human.prepare("set", {"ref": key, "pairs": ["size=m"]}))
    row = c["fields_changed"][0]
    assert (row["field"], row["after"]) == ("estimate", "3 points (size M)")
    assert c["headline"].startswith("Change the estimate of DEMO-0001")
    note = " ".join(c["notes"])
    assert "cancels any existing approval of: requirements, plan, verify, code." in note
    assert "approved again" in note
    assert "cancels" in "\n".join(signcontent.text(c, signer._clean))


def test_g2_a_change_that_no_gate_binds_makes_no_cancel_claim(human, served):
    _, key = human.ticket("Owner CLI test")
    c = build(human.prepare("set", {"ref": key, "pairs": ["priority=high"]}))
    assert not any("cancels" in n for n in c.get("notes") or [])


def _cancel_notes(ev):
    out: dict = {}
    signcontent._cancel_note(ev, out)
    return " ".join(out.get("notes") or [])


def test_g2_the_cancel_note_follows_the_cascade_of_later_gates():
    assert "requirements, plan, verify, code." in _cancel_notes({"sections": {"summary": {}}})
    assert "verify, code." in _cancel_notes({"sections": {"verification": {}}})
    assert "of: plan, verify, code." in _cancel_notes({"sections": {"plan": {}}})
    assert "of: verify, code." in _cancel_notes({"set": {"ticket.links": []}})


def test_g2_an_addon_section_or_field_gets_the_generic_note():
    assert "may also cancel that approval and every later one" in _cancel_notes({"sections": {"estimate.notes": {}}})
    assert "may also cancel that approval and every later one" in _cancel_notes(
        {"set": {"ticket.addons.estimate.points": 3}}
    )


def test_g2_the_size_scale_matches_the_schema_and_the_dashboard():
    import json
    import re
    from pathlib import Path

    schema = json.loads((Path(signcontent.__file__).parents[1] / "schema/schemas/ticket.json").read_text())
    enum = next(_find_enum(schema))["enum"]
    assert set(enum) == set(signcontent._SIZE_POINTS)
    ts = Path(__file__).parents[4] / "addons/dashboard/src/api/estimate.ts"
    if ts.exists():
        pairs = re.findall(r"size: '(\w+)', points: (\d+)", ts.read_text())
        assert {k: int(v) for k, v in pairs} == signcontent._SIZE_POINTS


def _find_enum(node):
    if isinstance(node, dict):
        if node.get("enum") and "xl" in node["enum"]:
            yield node
        for v in node.values():
            yield from _find_enum(v)
    elif isinstance(node, list):
        for v in node:
            yield from _find_enum(v)


def test_g5_repeated_notes_appear_once_and_cancel_notes_merge_in_gate_order():
    notes = [
        "This cancels any existing approval of: plan, verify, code. Each one has to be approved again.",
        "Other note.",
        "This cancels any existing approval of: requirements, plan, verify, code. Each one has to be approved again.",
        "This cancels any existing approval of: plan, verify, code. Each one has to be approved again.",
        "Other note.",
        "One confirmation signs all 3 steps.",
    ]
    out = signcontent._dedupe_notes({"headline": "x", "notes": notes})["notes"]
    assert out == [
        "This cancels any existing approval of: requirements, plan, verify, code. Each one has to be approved again.",
        "Other note.",
        "One confirmation signs all 3 steps.",
    ]
    assert signcontent._dedupe_notes({"headline": "x"}) == {"headline": "x"}
    odd = "This cancels any existing approval of: wizardry. Each one has to be approved again."
    assert signcontent._dedupe_notes({"notes": [odd, odd]})["notes"] == [odd]


def test_u3_the_title_drops_the_key_qualifier_and_names_a_new_ticket():
    t = {"key": "DEMO-1", "title": "", "uid_tail": "ABCD1234", "key_note": "n"}
    q = " (key per host; signed id …ABCD1234)"
    new = signcontent._with_title({"headline": f"Create ticket DEMO-1 · Elefant Chart{q}", "ticket": dict(t)})
    assert new["title"] == "New ticket DEMO-1: Elefant Chart" and new["headline"].endswith(q)  # headline: unchanged
    seq = signcontent._with_title({"headline": f"Create DEMO-1{q}: Elefant Chart", "ticket": dict(t)})
    assert seq["title"] == "New ticket DEMO-1: Elefant Chart"
    for head in (f"Change the plan of DEMO-1{q}", f"Edit DEMO-1{q}"):
        out = signcontent._with_title({"headline": head, "ticket": dict(t)})
        assert out["headline"] == head and "signed id" not in out["title"]
    assert "title" not in signcontent._with_title({"headline": "Change the workspace settings"})


def test_u3_ticket_new_comes_from_the_events_not_the_headline(human, served):
    _, key = human.ticket("Owner CLI test")
    ready(human, key)
    approve = signcontent.build(human.prepare("approve", {"ref": key, "gate": "requirements"}), verified=True)
    assert approve["ticket"]["new"] is False and approve["ticket"]["uid_tail"]
    created = signcontent.build(human.prepare("new", {"title": "Elefant Chart", "type": "chore"}), verified=True)
    assert created["ticket"]["new"] is True


def test_u3_only_a_creation_drops_the_cancel_notes_and_never_another_note():
    cancel = "This cancels any existing approval of: plan. Each one has to be approved again."
    addon = "An add-on field or section can also be part of an approval; if it is, this may also cancel it."
    other = "Agents that are running now stop."
    out = signcontent._new_ticket(
        {"notes": [cancel, other, addon], "fields_changed": [{"field": "type", "before": None, "after": "feature"}]}
    )
    assert out["notes"] == [other] and out["fields_title"] == "Details"
    only = signcontent._new_ticket({"notes": [cancel]})
    assert "notes" not in only
    changed = signcontent._new_ticket({"fields_changed": [{"field": "type", "before": "bug", "after": "feature"}]})
    assert "fields_title" not in changed  # a row with an earlier value is a change, not a detail


def test_verify_manual_acceptances_are_host_told_and_closed():
    event = {"type": "gate.approved", "gate": "verify"}
    material = material_for("gate.approved", event)
    material["gate"] = "verify"
    material["manual_acceptances"] = [{"ac": "AC1", "name": "accept-AC1-1.md", "by": "p_" + "1" * 32}]
    request = {"op": "approve", "events": [event], "material": material}
    content = build(request)
    rows = {r["field"]: r["after"] for r in content["fields_changed"]}
    assert rows["accepted manually"] == "AC1 by p_" + "1" * 32 + " (per host)"
    material["manual_acceptances"][0]["unknown"] = "refuse"
    with pytest.raises(signcontent.PlainUnavailable):
        build(request)


def test_gate_material_lists_only_live_authorised_manual_acceptances():
    from types import SimpleNamespace

    from orch.host.human import HumanOps
    from tests.model.test_acceptance import _two_members, _withdraw_after_wording_change, artifact
    from tests.schema.examples import digest

    w, uid = _two_members()
    _withdraw_after_wording_change(w, uid)
    w.tev(uid, "artifact.added", "sev", **artifact(name="accept-AC1-3.md", sha256=digest("statement3")))
    w.tev(uid, "artifact.added", "mara", **artifact(name="accept-AC1-4.md", sha256=digest("statement4")))
    store = SimpleNamespace(state=w.state(), ticket=lambda _: w.view(uid), body_sections=lambda _: {})
    material = HumanOps._material(
        store, uid, [{"type": "gate.approved", "gate": "verify"}], SimpleNamespace(pair=None), []
    )
    assert material["manual_acceptances"] == [{"ac": "AC1", "name": "accept-AC1-3.md", "by": w.people["sev"]}]
    assert all("label" not in a and "by" not in a for a in material["input"]["artifacts"].values())


def test_788_an_extend_is_signed_with_one_unlock_of_the_workspace_event_label_only():
    """Issue 788: the two events of one reviewed Extend are signed with ONE passphrase; the batch may sign the
    workspace event label and nothing else; one event alone still signs with its own prompt."""
    import contextlib

    from orch import canon

    opened, singles = [], []

    class Fake:
        name, person_capable, auth = "test", True, "passphrase"

        def sign(self, key_id, payload, *, action):
            singles.append(action)
            return b"\x01" * 64

        @contextlib.contextmanager
        def unlocked(self, key_id, *, action, digest, labels, fields=()):
            opened.append((action, labels, fields))
            yield lambda payload: b"\x02" * 64

    new = {**sample("grant.issued"), "grant": "gr_0NEWAAAAAAAAAAAAAAAAAAAAA", "auth": "passphrase", "hash_v": 1}
    old = {**_stop("gr_0OLDBBBBBBBBBBBBBBBBBBBBB", "extended"), "auth": "passphrase", "hash_v": 1}
    prepared = {"op": "grant", "log": "workspace", "events": [new, old]}
    signed = signer.sign(prepared, backend=Fake(), key_id="dk", workspace_id="7" * 32)
    assert len(signed.events) == 2 and all(e["sig"] for e in signed.events) and singles == []
    assert len(opened) == 1 and opened[0][1] == (canon.LABELS["sig_ws_event"].encode("ascii"),)
    signer.sign({**prepared, "events": [new]}, backend=Fake(), key_id="dk", workspace_id="7" * 32)
    assert len(opened) == 1 and len(singles) == 1  # a single event keeps its own prompt


def test_788_the_hosts_word_never_reaches_a_headline_or_summary_as_if_it_were_signed():
    """material["grants"] is the host's word (unsigned): what it says about the access is only in rows and lines
    labelled "per host, not signed"; the headline and the summary title are the signed words."""
    ev = _stop("gr_0OLDBBBBBBBBBBBBBBBBBBBBB")
    lie = "Totally harmless demo access"
    c = build(_revoke((ev, {"label": lie, "scope": "all"})))
    assert c["headline"] == "Take back agent access" and lie not in c["headline"]
    rows = [r["after"] for r in c["fields_changed"] if r["field"].startswith("access taken back")]
    assert rows and all(r.endswith("(per host, not signed)") for r in rows)
    text = "\n".join(signcontent.text(c, signer._clean))
    assert text.splitlines()[0] == "Take back agent access"
    new = {**sample("grant.issued"), "grant": "gr_0NEWAAAAAAAAAAAAAAAAAAAAA", "label": "runner"}
    old = _stop("gr_0OLDBBBBBBBBBBBBBBBBBBBBB", "extended")
    m = {**material_for("grant.issued", new), "grants": {old["grant"]: _info(old)}}
    ext = build({"op": "grant", "events": [new, old], "material": m})
    assert "Mac's runner, until" not in ext["headline"]  # the signed label of the NEW access may name the runner
    assert all("per host, not signed" in x.lower() for x in ext["summary"]["lines"] if "this Mac's runner, until" in x)
