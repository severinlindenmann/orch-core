"""External edits (§5.8): body.md becomes edit.external; every other change is reverted with projection.repaired."""

from __future__ import annotations

import json

import pytest

from orch import canon
from orch.store import render_ticket
from tests.store.helpers import Env


@pytest.fixture
def ready(env: Env):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.fill(uid)
    env.approve(uid, "requirements")
    assert s.state.tickets[uid].gates["requirements"].approved
    return env, s, uid


def body(env, uid):
    return env.path(uid, "body.md")


def types(events):
    return [e["type"] for e in events]


def test_editing_body_md_becomes_edit_external_and_voids_the_approved_gate(ready):
    env, s, uid = ready
    gen = s.state.tickets[uid].gates["requirements"].gen
    body(env, uid).write_text(body(env, uid).read_text().replace("req", "req and more"))
    (ev,) = s.scan()
    assert ev["type"] == "edit.external" and ev["actor"] == {"kind": "host"}
    assert list(ev["sections"]) == ["requirements"]
    assert ev["sections"]["requirements"] == {"hash": canon.section_hash("req and more"), "refs": []}
    assert ev["voided_gates"] == ["requirements"] and ev["normalised"] is False
    v = s.state.tickets[uid]
    assert not v.gates["requirements"].approved and v.gates["requirements"].gen == gen + 1
    assert v.sections["requirements"]["hash"] == canon.section_hash("req and more")
    assert s.body_sections(uid)["requirements"] == "req and more"
    assert s.scan() == [] and s.reports == []  # answered once


def test_unbound_section_edit_voids_nothing(ready):
    env, s, uid = ready
    body(env, uid).write_text(body(env, uid).read_text() + "\n## Current state\n\nnotes by hand\n")
    (ev,) = s.scan()
    assert list(ev["sections"]) == ["current_state"] and ev["voided_gates"] == []
    assert s.state.tickets[uid].gates["requirements"].approved


def test_edits_with_references_keep_them_even_when_unknown(ready):
    env, s, uid = ready
    body(env, uid).write_text(body(env, uid).read_text().replace("nothing", "see (artifact:ghost.png)"))
    (ev,) = s.scan()
    assert ev["sections"]["out_of_scope"]["refs"] == ["ghost.png"]  # unknown refs stay, the gate is incomplete


def test_an_edit_is_answered_before_the_next_event_of_that_ticket(ready):
    env, s, uid = ready
    body(env, uid).write_text(body(env, uid).read_text().replace("ctx", "changed context"))
    r = env.log(uid, "agent works on")
    assert types(r.healed) == ["edit.external"] and r.event["seq"] == r.healed[0]["seq"] + 1
    assert r.event["based_on"] == canon.event_head(r.healed[0])  # the agent's event follows the head it now sees
    assert s.body_sections(uid)["context"] == "changed context"


def test_a_signed_decision_on_a_stale_generation_is_refused_after_an_external_edit(ready):
    env, s, uid = ready
    g = s.state.tickets[uid].gates["plan"]
    stale = env.person_event(
        env.owner, uid, "gate.approved", gate="requirements", gate_gen=1, hash=g.hash, policy_hash=g.policy_hash
    )
    body(env, uid).write_text(body(env, uid).read_text().replace("ctx", "edited"))
    from orch.store import StoreError

    with pytest.raises(StoreError) as e:
        s.append(stale, log=uid)
    assert e.value.code in ("gate.stale", "gate.status", "gate.not_eligible", "gate.incomplete", "gate.not_applicable")


def test_crlf_and_non_nfc_text_is_normalised_and_flagged(ready):
    env, s, uid = ready
    raw = body(env, uid).read_bytes().replace(b"req", "réq".encode()).replace(b"\n", b"\r\n")
    body(env, uid).write_bytes(raw)
    (ev,) = s.scan()
    assert ev["normalised"] is True and ev["sections"]["requirements"]["hash"] == canon.section_hash("réq")
    assert b"\r" not in body(env, uid).read_bytes()  # rewritten normalised
    assert s.scan() == []


def test_a_body_that_is_not_a_body_is_reverted_from_the_hosts_copy(ready):
    env, s, uid = ready
    good = body(env, uid).read_bytes()
    for evil in (
        b"## Hacked\n\nunknown heading\n",
        good + b"\n## Plan\n\n```\nunterminated fence\n",
        b"text before any heading\n" + good,
        good.replace(b"req", b"r\x00q"),
        good + b"\n## Context\n\nsecond context\n",
        b"\xff\xfe not utf-8",
    ):
        body(env, uid).write_bytes(evil)
        (ev,) = s.scan()
        assert ev["type"] == "projection.repaired" and ev["path"] == "body.md" and ev["cause"] == "external_edit"
        assert body(env, uid).read_bytes() == good
    assert s.state.tickets[uid].gates["requirements"].approved  # nothing was installed, nothing was voided


def test_deleting_body_md_removes_the_sections(ready):
    env, s, uid = ready
    body(env, uid).unlink()
    (ev,) = s.scan()
    assert ev["type"] == "edit.external" and set(ev["sections"]) == {"context", "requirements", "out_of_scope"}
    assert all(v is None for v in ev["sections"].values()) and ev["voided_gates"] == ["requirements"]


def test_editing_ticket_json_is_reverted_and_named_in_projection_repaired(ready):
    env, s, uid = ready
    good = env.path(uid, "ticket.json").read_bytes()
    doc = json.loads(good)
    doc["title"] = "pwned"
    doc["priority"] = "urgent"
    doc["acceptance"] = []
    env.path(uid, "ticket.json").write_text(json.dumps(doc))
    before = s.state.tickets[uid]
    (ev,) = s.scan()
    assert ev["type"] == "projection.repaired" and ev["path"] == "ticket.json" and ev["cause"] == "external_edit"
    assert ev["fields"] == ["ticket.acceptance", "ticket.priority", "ticket.title"]
    assert env.path(uid, "ticket.json").read_bytes() == good
    assert s.state.tickets[uid].fields == before.fields and s.state.tickets[uid].gates["requirements"].approved


def test_an_unparseable_or_deleted_ticket_json_is_rebuilt_from_events(ready):
    env, s, uid = ready
    good = env.path(uid, "ticket.json").read_bytes()
    env.path(uid, "ticket.json").write_text("{ not json")
    (ev,) = s.scan()
    assert ev["type"] == "projection.repaired" and "fields" not in ev
    env.path(uid, "ticket.json").unlink()
    (ev,) = s.scan()
    assert ev["type"] == "projection.repaired"
    v = s.state.tickets[uid]
    assert env.path(uid, "ticket.json").read_bytes() == good == render_ticket(uid, v.key, v.fields)


def test_ticket_json_is_repaired_before_the_next_event_of_the_ticket(ready):
    env, s, uid = ready
    doc = json.loads(env.path(uid, "ticket.json").read_bytes())
    doc["title"] = "sneaky"
    env.path(uid, "ticket.json").write_text(json.dumps(doc))
    r = env.update(uid, {"ticket.priority": "high"})
    assert types(r.healed) == ["projection.repaired"]
    on_disk = json.loads(env.path(uid, "ticket.json").read_bytes())
    assert on_disk["title"] == "A ticket" and on_disk["priority"] == "high"


def test_a_closed_ticket_reverts_a_changed_bound_section_from_the_copy(ready):
    env, s, uid = ready
    env.close(uid)
    good = body(env, uid).read_bytes()
    body(env, uid).write_bytes(good.replace(b"req", b"ATTACK"))
    (ev,) = s.scan()
    assert ev["type"] == "projection.repaired" and ev["path"] == "body.md" and ev["fields"] == ["body.requirements"]
    assert body(env, uid).read_bytes() == good
    assert s.state.tickets[uid].sections["requirements"]["hash"] == canon.section_hash("req")


def test_a_closed_ticket_keeps_an_unbound_edit_and_reverts_the_bound_one(ready):
    env, s, uid = ready
    env.close(uid)
    text = body(env, uid).read_text().replace("req", "ATTACK") + "\n## Current state\n\nhand notes\n"
    body(env, uid).write_text(text)
    evs = s.scan()
    assert types(evs) == ["edit.external", "projection.repaired"]
    assert list(evs[0]["sections"]) == ["current_state"]
    now = s.body_sections(uid)
    assert now["requirements"] == "req" and now["current_state"] == "hand notes"


def test_a_forged_state_body_copy_is_ignored(ready):
    env, s, uid = ready
    env.close(uid)
    forged = body(env, uid).read_bytes().replace(b"req", b"EVIL")
    (env.root / ".state" / "body" / f"{uid}.md").write_bytes(forged)  # the copy an attacker prepared ...
    body(env, uid).write_bytes(forged)  # ... and the same text in body.md
    assert s.scan() == []  # the log has no way to accept it, and the copy can't be used to "revert" to it
    codes = [r.code for r in s.reports]
    assert codes == ["store.torn_write"] and "no valid copy" in s.reports[0].detail
    assert s.state.tickets[uid].sections["requirements"]["hash"] == canon.section_hash("req")
    assert (env.root / ".state" / "body" / f"{uid}.md").read_bytes() == forged  # never promoted to anything


def test_a_forged_state_body_copy_is_not_used_on_an_open_ticket_either(ready):
    env, s, uid = ready
    (env.root / ".state" / "body" / f"{uid}.md").write_bytes(b"## Context\n\nforged copy\n")
    body(env, uid).write_bytes(b"## Hacked\n\nx\n")  # not a body: would be reverted from the (forged) copy
    assert s.scan() == []
    assert s.reports and s.reports[0].code == "store.torn_write"
    assert body(env, uid).read_bytes() == b"## Hacked\n\nx\n"  # we refuse to write the forged text
    assert s.state.tickets[uid].sections["context"]["hash"] == canon.section_hash("ctx")


def test_edits_are_found_when_the_store_opens(ready):
    env, s, uid = ready
    s.close()
    body(env, uid).write_text(body(env, uid).read_text().replace("ctx", "while closed"))
    doc = json.loads(env.path(uid, "ticket.json").read_bytes())
    doc["size"] = "xl"
    env.path(uid, "ticket.json").write_text(json.dumps(doc))
    s2 = env.open()
    log = env.read_events(uid)
    assert [e["type"] for e in log[-2:]] == ["projection.repaired", "edit.external"]
    assert s2.scan() == []


def test_keys_jsonl_is_repaired_and_never_makes_a_key_reusable(ready):
    env, s, uid = ready
    (env.root / "keys.jsonl").write_bytes(b"")
    (ev,) = s.scan()
    assert ev["type"] == "projection.repaired" and ev["path"] == "keys.jsonl" and ev["cause"] == "keys_mismatch"
    assert (env.root / "keys.jsonl").read_bytes().count(b"\n") == 1
    assert s.next_key() == "DEMO-0002"


def test_config_json_follows_the_events_but_keeps_its_unsigned_name(ready):
    env, s, uid = ready
    cfg = json.loads((env.root / "config.json").read_text())
    cfg["workspace"]["name"] = "Renamed by hand"
    cfg["members"][0]["role"] = "viewer"
    (env.root / "config.json").write_text(json.dumps(cfg))
    (ev,) = s.scan()
    assert ev["type"] == "projection.repaired" and ev["path"] == "config.json"
    again = json.loads((env.root / "config.json").read_text())
    assert again["members"][0]["role"] == "owner" and again["workspace"]["name"] == "Renamed by hand"


def test_external_edit_gates_survive_a_fresh_replay(ready):
    env, s, uid = ready
    body(env, uid).write_text(body(env, uid).read_text().replace("req", "other"))
    s.scan()
    from tests.store.test_append import replayed

    fresh = replayed(env)  # replay recomputes voided_gates itself and refuses a mismatch
    assert not fresh.chain_errors and not fresh.workspace.invalid
    assert not fresh.tickets[uid].gates["requirements"].approved and not fresh.tickets[uid].frozen
