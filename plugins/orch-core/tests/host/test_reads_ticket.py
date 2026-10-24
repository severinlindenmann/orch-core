"""H6b ``ticket.get`` (§0.8, §0.8.4, §0.8.5) through a real host: every field equals the model view and the signed
events; the visibility, closed-ticket, cut-flag and frame rules; the v1.4 and v1.5 (``read_ext/1``, ``read_ext/2``)
fields."""

from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from orch import canon
from orch.host import reads_ticket
from orch.host.reads import iso
from orch.identity import new_ulid
from orch.model.types import ts
from tests.host.human_helpers import Human, err, ok
from tests.host.live_helpers import SESSION, jpos_of, principal_of
from tests.host.live_helpers import world as make_world
from tests.host.test_dispatch_matrix import as_agent_class


@pytest.fixture
def w(tmp_path):
    w = make_world(tmp_path)
    try:
        yield w
    finally:
        w.close()
        w.lab.close()


# --- helpers --------------------------------------------------------------------------------------------------------


def get(c, ref, **args):
    return ok(c.req("ticket.get", {"ref": ref, **args}))["data"]


def model(w, uid):
    """The model view of the ticket (what ``Viewer.ticket`` hands the handler)."""
    return w.h.run(w.service.call(lambda: w.service.store.ticket(uid)))


def events_of(w, uid):
    return w.lab.ws.read_events(uid)


def agent_ev(w, uid, typ, session=None, **payload):
    """An agent-actor event appended by the host's own store (what a served agent op would append)."""
    return w.host_append(
        lambda ws: ({"type": typ, "actor": {**ws.agent, **({"session": session} if session else {})}, **payload}, uid)
    )


def add_file(c, w, key, name, data, *, agent=True, **args):
    files = {"path": {"b64": base64.b64encode(data).decode()}}
    auth = {"grant": w.grant, "session": SESSION} if agent else {"session": SESSION}
    return ok(
        c.req("artifact.add", {"ref": key, "path": name, "kind": "report", **args}, files=files, idem=True, **auth)
    )


def link_repo(w, uid, name="app"):
    w.person_event("settings.changed", set={"repos": {name: {"path": str(w.lab.tmp / name)}}})  # a configured repo

    def make(ws):
        sets = {"ticket.links": {"repos": [name], "branches": {name: "main"}, "prs": [], "external": []}}
        return {"type": "ticket.updated", "actor": ws.agent, "base_rev": ws.base_rev(uid, sets, {}), "set": sets}, uid

    w.host_append(make)


def push(w, uid, sha, before=None, name="app", ref="refs/heads/main"):
    payload = {"repo_name": name, "repo_id": f"local:{name}", "ref": ref, "sha": sha, "before": before}

    def run():
        out = w.service.store.host_append("branch.pushed", uid, payload)
        w.service.sync()
        return out

    w.lab.ws.tick()
    return w.h.run(w.service.call(run))


def sha(n):
    return f"{n:040x}"


def full(w, **verdict):
    """A ticket through the whole flow, done: a question asked and answered, both early gates approved, an evidence
    artifact, a task with a receipt, a verdict."""
    h = Human(w)
    uid, key = w.ticket("rich")
    a = h.agent
    a("claim", {"ref": key})
    for sid in ("context", "requirements", "out_of_scope", "plan", "decisions", "verification"):
        a("section.set", {"ref": key, "section": sid, "message": f"the {sid}"})
    a("ac.add", {"ref": key, "text": "it works"})
    a("task.add", {"ref": key, "text": "do it", "verify": "make test", "proves": ["AC1"]})
    a("ask", {"ref": key, "text": "which?", "option": ["a | a", "b | b"], "why": "because", "rec": "a"})
    h.run("approve", {"ref": key, "gate": "requirements"})
    h.run("approve", {"ref": key, "gate": "plan"})
    h.run("answer", {"ref": key, "question": "Q1", "option": "a"})
    data = b"the proof bytes"
    add_file(h.c, w, key, "proof.txt", data, ac="AC1")
    receipt = {"cmd": "make test", "exit": 0, "ms": 5, "repo": None, "commit": None}
    agent_ev(w, uid, "task.done", task="T1", receipt=receipt, text="did it")
    agent_ev(w, uid, "ticket.submitted")
    w.person_event("people.changed", uid, role="reviewers", add=[w.lab.owner], remove=[])
    h.run("verdict", {"ref": key, "outcome": "pass", **verdict})
    return SimpleNamespace(h=h, c=h.c, uid=uid, key=key, data=data, receipt=receipt)


def walk(o):
    """Every key of a JSON value, at any depth."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_the_caps_and_the_op_are_served(w):
    c = w.client()
    assert {"query/1", "read_ext/1", "read_ext/2", "text/1"} <= set(c.welcome["caps"])
    assert c.welcome["ops"]["ticket.get"] == 1 and c.welcome["ops"]["ticket.text"] == 1


# --- every field equals the model view and the signed events -------------------------------------------------------


def test_every_field_equals_the_model_view(w):
    f = full(w)
    view, events = model(w, f.uid), events_of(w, f.uid)
    by_id = {e["id"]: e for e in events}
    r = get(f.c, f.key)
    assert set(r) == {
        "doc", "log", "log_more", "questions", "criteria", "gates", "sub_cursor", "status", "settled", "tasks",
        "verdict", "artifacts", "claim", "source", "pushes", "sections", "members_v",
    }  # fmt: skip
    assert r["status"] == view.status == "done" and r["settled"] is True and "closed" not in r

    # the doc: the ticket.json keys merged with the derived state
    doc = r["doc"]
    assert (doc["uid"], doc["key"], doc["title"], doc["type"], doc["status"]) == (
        f.uid,
        f.key,
        "rich",
        "feature",
        "done",
    )
    assert doc["owner"] == view.owner and doc["visibility"] == "workspace" and doc["links"]["repos"] == []
    assert doc["acceptance"] == [{"id": "AC1", "text": "it works", "evidence": list(view.acceptance[0].evidence)}]
    assert doc["sections"]["requirements"] == "the requirements" and doc["current_state"] == ""

    # questions: options and answer as the events signed them
    (q,) = r["questions"]
    asked = next(e for e in events if e["type"] == "question.asked")
    answered = next(e for e in events if e["type"] == "question.answered")
    assert q["state"] == "answered" and q["blocking"] is True
    assert q["options"] == asked["question"]["options"] and q["recommended"] == "a" and q["why"] == "because"
    assert (q["text"], q["to_ref"]) == (asked["question"]["text"], asked["question"]["to"])
    assert (q["asked_at"], q["asked_seq"]) == (asked["at"], asked["seq"]) and q["asked_by"]["kind"] == "agent"
    assert q["to"] == [w.lab.owner] and q["answered_by"] == w.lab.owner and q["answered_seq"] == answered["seq"]
    assert q["answer"] == {
        "by": w.lab.owner, "at": answered["at"], "seq": answered["seq"], "option": "a", "text": None,
        "auth": answered["auth"], "id": answered["id"],
        # H12d-2 (§0.8.6 item 9.11, READER_VIEWS_1): who signed the counting answer, with which custody
        "signer": {"device": answered["actor"]["device"], "auth": answered["auth"]},
    }  # fmt: skip
    from orch.identity.reader import parse_signer

    assert parse_signer("ticket.get.questions.answer", q) == answered["auth"]  # the production parser accepts it
    assert (q["qid"], q["hash"]) == (asked["qid"], asked["hash"]) == (view.questions[0].qid, view.questions[0].hash)

    # criteria and tasks: the receipt counts as evidence now
    (ac,) = r["criteria"]
    assert ac["covered"] is True and ac["tasks"] == [{"id": "T1", "evidence": True}]
    (task,) = r["tasks"]
    done = next(e for e in events if e["type"] == "task.done")
    assert task["state"] == view.tasks[0].state == "done" and task["leased_by"] is None
    assert task["receipt"] == {**f.receipt, "log": None, "seq": done["seq"], "at": done["at"], "evidence": True}
    assert task["by"]["kind"] == "agent" and task["at"] == done["at"] and task["reason"] is None

    # gates: the model's, the decisions as signed
    for name, g in r["gates"].items():
        gv = view.gates[name]
        assert g["state"] == ("n/a" if not gv.applies else "approved" if gv.approved else "open")
        assert (g["gen"], g["applies"], g["waiting"], g["blocked"], g["count"]) == (
            gv.gen, gv.applies, gv.waiting, gv.blocked, gv.needed,
        )  # fmt: skip
        assert g["approved_by"] == list(gv.counting) and g["eligible"] == list(gv.eligible)
        assert g["policy"]["approvers"] == list(gv.policy["approvers"]) and g["policy"]["count"] == gv.policy["count"]
        assert g["missing"] == list(gv.missing) and g["revoked_decisions"] == list(gv.revoked_decisions)
        assert [d["id"] for d in g["decisions"]] == [d.id for d in gv.decisions]
        for d in g["decisions"]:
            e = by_id[d["id"]]
            assert d["kind"] == {"gate.approved": "approved", "verdict.given": "pass"}[e["type"]]
            assert (d["person"], d["at"], d["seq"], d["gen"]) == (e["actor"]["id"], e["at"], e["seq"], e["gate_gen"])
            assert (d["hash"], d["policy_hash"], d["auth"]) == (e["hash"], e["policy_hash"], e["auth"])
            assert d["counting"] is True and d["voided"] is False
    assert r["gates"]["requirements"]["state"] == "approved" and r["gates"]["code"]["state"] == "n/a"
    assert r["gates"]["verify"]["decisions"][0]["source_sha"] == []
    assert "source_sha" not in r["gates"]["requirements"]["decisions"][0]  # only verify and code decisions

    # the verdict is current while it counts for the verify gate's generation
    verdict = next(e for e in events if e["type"] == "verdict.given")
    assert r["verdict"] == {
        "outcome": "pass", "person": w.lab.owner, "at": verdict["at"], "seq": verdict["seq"],
        "gen": verdict["gate_gen"], "text": None, "source_sha": [], "current": True, "counting": True,
        "voided": False, "auth": verdict["auth"],
    }  # fmt: skip
    assert verdict["gate_gen"] == view.gates["verify"].gen

    # the manifest
    (art,) = r["artifacts"]
    va = view.artifacts[0]
    assert (art["name"], art["kind"], art["digest"], art["ac"], art["evidence"]) == (
        va.name,
        va.kind,
        va.digest,
        va.ac,
        True,
    )
    assert art["bytes"] == len(f.data) and art["replaced"] == 0 and art["by"]["kind"] == "agent"

    # claim, source, pushes, sections
    assert (r["claim"] is None) == (view.claim is None) and r["source"] == [] and r["pushes"] == []
    assert r["sections"].keys() == {"context", "requirements", "out_of_scope", "plan", "decisions", "verification"}


def test_a_verdict_from_before_a_reopen_is_returned_with_current_false(w):
    f = full(w)
    assert get(f.c, f.key)["verdict"]["current"] is True
    f.h.run("close", {"ref": f.key, "resolution": "other"})
    f.h.run("reopen", {"ref": f.key})  # a reopen raises every gate's generation
    r = get(f.c, f.key)
    verify = model(w, f.uid).gates["verify"]
    d = verify.decisions[0]
    assert r["status"] == "open" and r["verdict"]["current"] is False
    assert (r["verdict"]["counting"], r["verdict"]["voided"]) == (d.counting, d.voided)
    assert r["verdict"]["gen"] < r["gates"]["verify"]["gen"] == verify.gen and r["gates"]["verify"]["state"] == "open"


def test_evidence_digests_match_the_manifest_and_artifact_get(w):
    h = Human(w)
    uid, key = w.ticket("evidence")
    h.agent("claim", {"ref": key})
    for text in ("agent proof", "person proof", "no proof"):
        h.agent("ac.add", {"ref": key, "text": text})
    add_file(h.c, w, key, "a.txt", b"agent bytes", ac="AC1")
    add_file(h.c, w, key, "u.txt", b"unattended bytes", agent=False)  # no grant: never evidence
    prepared = h.prepare(
        "artifact.add", {"ref": key, "path": "p.txt", "ac": "AC2", "kind": "screenshot"},
        files={"path": {"b64": base64.b64encode(b"person bytes").decode()}},
    )  # fmt: skip
    ok(h.submit_res(prepared["request"]["id"], h.sign(prepared).events))
    r = get(h.c, key)
    manifest = {a.name: a for a in model(w, uid).artifacts}
    ev = {c["id"]: c["evidence"] for c in r["criteria"]}
    assert ev["AC1"] == [
        {"artifact": "a.txt", "digest": digest(b"agent bytes"), "kind": "report", "attested_by": "agent"}
    ]
    assert ev["AC2"] == [
        {"artifact": "p.txt", "digest": digest(b"person bytes"), "kind": "screenshot", "attested_by": "person"}
    ]
    assert ev["AC3"] == [] and {c["id"]: c["covered"] for c in r["criteria"]} == {
        "AC1": True,
        "AC2": True,
        "AC3": False,
    }
    arts = {a["name"]: a for a in r["artifacts"]}
    assert list(arts) == sorted(arts) == ["a.txt", "p.txt", "u.txt"]
    for name, a in arts.items():  # the digests are the manifest's, which artifact.get reads the file by
        assert a["digest"] == manifest[name].digest
    assert arts["a.txt"]["digest"] == digest(b"agent bytes") and arts["a.txt"]["bytes"] == len(b"agent bytes")
    assert arts["u.txt"]["evidence"] is False and arts["u.txt"]["by"]["unattended"] is True
    assert "unattended" not in arts["a.txt"]["by"] and arts["a.txt"]["evidence"] is True
    for name, a in arts.items():  # H4a2 serves artifact.get: its digest equals ticket.get's
        got = ok(h.c.req("artifact.get", {"ref": key, "name": name, "offset": 0}))["data"]
        assert got["digest"] == a["digest"]


def test_an_addon_artifact_has_no_digest_and_bytes_but_addon_and_ref():
    """The shape the builder gives an addon artifact (§0.8: ``digest``/``bytes`` null, ``addon`` and ``ref``)."""
    core = SimpleNamespace(
        name="chart", kind="diagram", digest=None, bytes=None, ac=None, task=None, label=None, addon="viz", ref="r1",
        event="e1", evidence_ok=False,
    )  # fmt: skip
    event = {"id": "e1", "seq": 4, "at": "2026-09-21T00:00:00Z", "actor": {"kind": "addon", "id": "viz"}}
    x = SimpleNamespace(t=SimpleNamespace(artifacts={"chart": core}), by_id={"e1": event}, of_type=lambda *t: [])
    (a,), more = reads_ticket._artifacts(x)
    assert more is False and a["digest"] is None and a["bytes"] is None and (a["addon"], a["ref"]) == ("viz", "r1")
    assert a["evidence"] is False and a["replaced"] == 0 and a["by"] == {"kind": "addon", "id": "viz"}


# --- LogItem.data ---------------------------------------------------------------------------------------------------

TABLE = {
    "ticket.created": {"ticket_type", "title", "owner"},
    "ticket.updated": {"paths", "sections"},
    "status.changed": {"from", "to", "reason"},
    "ticket.moved": {"from", "to", "reason"},
    "ticket.submitted": set(),
    "handoff.written": set(),
    "ticket.closed": {"resolution", "duplicate_of", "text", "text_cut"},
    "ticket.reopened": {"text", "text_cut"},
    "visibility.changed": {"restricted"},
    "people.changed": {"role", "add", "remove"},
    "policy.changed": {"gates"},
    "claim.taken": {"takeover"},
    "claim.released": {"session", "reason"},
    "task.started": {"task"},
    "task.reopened": {"task", "reason"},
    "task.done": {"task", "receipt", "log", "text", "text_cut"},
    "task.skipped": {"task", "reason"},
    "task.blocked": {"task", "reason"},
    "log.added": {"text", "text_cut"},
    "question.asked": {"question", "blocking", "to"},
    "question.answered": {"question", "option", "text", "text_cut"},
    "gate.approved": {"gate", "gen"},
    "gate.changes_requested": {"gate", "gen", "text", "text_cut"},
    "verdict.given": {"outcome", "gen", "text", "text_cut", "source_sha"},
    "gate.invalidated": {"gate", "cause", "voided"},
    "branch.pushed": {"repo_name", "repo_id", "ref", "sha", "before_sha"},
    "artifact.added": {"name", "kind", "digest", "bytes", "ac", "task", "label", "addon", "replaces"},
    "artifact.replaced": {"name", "kind", "digest", "bytes", "ac", "task", "label", "addon", "replaces"},
    "edit.external": {"sections", "voided_gates", "normalised"},
    "projection.repaired": {"path", "cause"},
    "restore": {"from_seq", "reason"},
    "invalid.acknowledged": {"invalid_seq"},
}


def test_log_data_holds_only_the_fields_of_its_type_table_and_no_secret(w):
    f = full(w)
    agent_ev(w, f.uid, "log.added", text="note")
    r = get(f.c, f.key, log_limit=500)
    types = {i["type"] for i in r["log"]}
    assert {"ticket.created", "claim.taken", "ticket.updated", "question.asked", "gate.approved", "artifact.added",
            "task.done", "verdict.given", "people.changed"} <= types  # fmt: skip
    for item in r["log"]:
        assert set(item) == {"seq", "type", "at", "actor", "summary", "data"}
        assert set(item["data"]) <= TABLE[item["type"]], item
        assert len(item["summary"]) <= 200 and "sig" not in item["actor"]
    done = next(i for i in r["log"] if i["type"] == "task.done")
    assert done["data"] == {
        "task": "T1", "receipt": {"cmd": "make test", "exit": 0, "ms": 5, "repo": None, "commit": None},
        "text": "did it",
    }  # fmt: skip
    verdict = next(i for i in r["log"] if i["type"] == "verdict.given")
    assert verdict["data"] == {"outcome": "pass", "gen": verdict["data"]["gen"], "source_sha": []}
    art = next(i for i in r["log"] if i["type"] == "artifact.added")
    assert art["data"]["digest"] == digest(f.data) and art["data"]["bytes"] == len(f.data)
    banned = {"sig", "host_sig", "secret", "secret_hash", "prev", "based_on", "hash_v", "roster_v", "pk_pub", "wsk_pub"}
    assert not set(walk(r)) & banned


def test_a_long_comment_is_a_snip_cut_on_a_scalar_boundary(w):
    h = Human(w)
    uid, key = w.ticket("long")
    text = "ab" + "€" * 400 + "😀" * 3  # 3-byte and 4-byte scalars: byte 1024 falls inside a euro sign
    assert len(text.encode()) > 1024
    w.write(key, text)
    item = next(i for i in get(h.c, key)["log"] if i["type"] == "log.added")
    cut = item["data"]["text"]
    assert item["data"]["text_cut"] is True and set(item["data"]) == {"text", "text_cut"}
    assert len(cut.encode()) <= 1024 and text.startswith(cut) and len(cut.encode()) > 1024 - 4
    assert cut == text.encode()[:1024].decode("utf-8", errors="ignore") and "�" not in cut
    created = next(i for i in get(h.c, key)["log"] if i["type"] == "ticket.created")
    assert "text_cut" not in created["data"]
    w.write(key, "x" * 1024)  # exactly 1024 bytes: not cut, so no sibling
    assert get(h.c, key)["log"][-1]["data"] == {"text": "x" * 1024}


def test_log_paging_and_the_summary_state_before_the_first_returned_event(w):
    h = Human(w)
    uid, key = w.ticket("paging")
    h.agent("claim", {"ref": key})
    h.agent("ac.add", {"ref": key, "text": "first"})
    h.agent("ac.edit", {"ref": key, "ac": "AC1", "text": "second"})
    h.agent("ac.add", {"ref": key, "text": "third"})
    whole = get(h.c, key)["log"]
    assert [i["summary"] for i in whole if i["type"] == "ticket.updated"] == ["AC1 added", "AC1 edited", "AC2 added"]
    edited = next(i["seq"] for i in whole if i["summary"] == "AC1 edited")
    tail = get(h.c, key, log_since=edited - 1)
    assert [i["summary"] for i in tail["log"]] == ["AC1 edited", "AC2 added"]  # the state before the first event
    assert tail["log_more"] is False
    n = len(whole)
    assert get(h.c, key, log_limit=n)["log_more"] is False  # exactly the limit: nothing cut
    page = get(h.c, key, log_limit=n - 1)
    assert page["log_more"] is True and [i["seq"] for i in page["log"]] == [i["seq"] for i in whole[:-1]]
    rest = get(h.c, key, log_since=page["log"][-1]["seq"], log_limit=n)
    assert [i["seq"] for i in rest["log"]] == [whole[-1]["seq"]] and rest["log_more"] is False
    past = get(h.c, key, log_since=n)
    assert past["log"] == [] and past["log_more"] is False


# --- sections: hashes only ------------------------------------------------------------------------------------------


def test_sections_hold_hashes_only(w):
    h = Human(w)
    uid, key = w.ticket("sections")
    h.agent("claim", {"ref": key})
    for text in ("old secret text one", "old secret text two", "the current text"):
        h.agent("section.set", {"ref": key, "section": "context", "message": text})
    r = get(h.c, key)
    revs = r["sections"]["context"]
    events = [e for e in events_of(w, uid) if e["type"] == "ticket.updated"]
    assert [x["seq"] for x in revs] == [e["seq"] for e in events] and len(revs) == 3
    for x, e in zip(revs, events, strict=True):
        assert set(x) == {"seq", "at", "hash", "actor", "via", "refs"} and x["via"] == "edit"
        assert x["hash"] == e["sections"]["context"]["hash"] and x["actor"]["kind"] == "agent" and x["refs"] == []
    blob = json.dumps(r["sections"])
    assert "old secret text" not in blob and "current text" not in blob
    assert "old secret text" not in json.dumps(r["log"]) and r["doc"]["sections"]["context"] == "the current text"
    assert "sections_more" not in r


def test_section_revisions_of_a_removal_and_an_external_edit():
    """A removed section has ``hash: null``; an ``edit.external`` is ``via: external``; refs are sorted names."""
    events = [
        {"seq": 2, "at": "2026-09-21T00:00:00Z", "type": "ticket.updated", "actor": {"kind": "agent", "id": "a"},
         "sections": {"plan": {"hash": "sha256:" + "a" * 64, "refs": ["z.png", "a.png"]}}},
        {"seq": 3, "at": "2026-09-21T00:00:01Z", "type": "edit.external", "actor": {"kind": "host", "id": None},
         "sections": {"plan": None}},
    ]  # fmt: skip
    x = SimpleNamespace(of_type=lambda *types: [e for e in events if e["type"] in types])
    revs, more = reads_ticket._sections(x)
    assert more == {} and [(r["via"], r["hash"], r["refs"]) for r in revs["plan"]] == [
        ("edit", "sha256:" + "a" * 64, ["a.png", "z.png"]),
        ("external", None, []),
    ]
    assert revs["plan"][1]["actor"] == {"kind": "host", "id": None}


# --- closed and done tickets (§0.8.4) ------------------------------------------------------------------------------


def test_a_closed_ticket_is_read_by_a_viewer_who_may_see_it_and_a_reopened_one_loses_closed(w):
    h = Human(w)
    uid, key = w.ticket("closing")
    w.write(key, "before")
    h.run("close", {"ref": key, "resolution": "obsolete", "message": "no longer needed " + "é" * 700})
    r = get(h.c, key)
    close = next(e for e in events_of(w, uid) if e["type"] == "ticket.closed")
    assert r["status"] == "closed" and r["settled"] is True and r["doc"]["status"] == "closed"
    assert r["closed"] == {
        "resolution": "obsolete", "duplicate_of": None, "at": close["at"], "by": w.lab.owner,
        "text": close["text"], "seq": close["seq"], "event_id": close["id"],
    }  # fmt: skip
    assert len(r["closed"]["text"].encode()) > 1024  # closed.text is the full stored text, not a Snip
    assert r["log"][-1]["type"] == "ticket.closed" and r["log"][-1]["data"]["text_cut"] is True
    h.run("reopen", {"ref": key, "message": "again"})
    after = get(h.c, key)
    assert after["status"] == "open" and after["settled"] is False and "closed" not in after
    assert [i["type"] for i in after["log"]][-2:] == ["ticket.closed", "ticket.reopened"]
    h.run("close", {"ref": key, "resolution": "wont_do"})
    again = get(h.c, key)
    assert again["closed"]["resolution"] == "wont_do" and again["closed"]["text"] is None  # no text: null
    assert again["closed"]["seq"] > close["seq"]


def test_a_closed_ticket_the_viewer_may_not_see_is_not_found(w):
    h = Human(w)
    uid, key = w.ticket("hidden and closed")
    w.restrict(uid, [w.quinn.ref])  # not the owner
    open_uid, open_key = w.ticket("workspace-visible and closed")
    h.run("close", {"ref": open_key, "resolution": "other"})
    w.person_event("ticket.closed", uid, resolution="other")  # an owner off the list may still close it
    assert err(h.c.req("ticket.get", {"ref": key})) == "not_found"  # the owner is not on the list
    assert get(h.c, open_key)["status"] == "closed"
    as_agent_class(w)  # no-person
    n = w.client()
    assert err(n.req("ticket.get", {"ref": key})) == "not_found"
    assert get(n, open_key)["closed"]["resolution"] == "other"
    assert err(n.req("ticket.get", {"ref": "DEMO-9999"})) == "not_found"


def test_a_restricted_closed_ticket_is_read_by_a_member_on_its_list(tmp_path):
    q = make_world(tmp_path, map_to="quinn")
    try:
        uid, key = q.ticket("for quinn")
        q.restrict(uid, [q.quinn.ref])
        q.person_event("ticket.closed", uid, resolution="other")
        quinn = q.client()
        r = get(quinn, key)
        assert r["status"] == "closed" and r["doc"]["visibility"] == {"restricted": [q.quinn.ref]}
        hidden_uid, hidden = q.ticket("not for quinn")
        q.restrict(hidden_uid, [q.lab.owner])
        q.person_event("ticket.closed", hidden_uid, resolution="other")
        assert err(quinn.req("ticket.get", {"ref": hidden})) == "not_found"
        assert err(quinn.req("ticket.get", {"ref": hidden_uid})) == "not_found"  # by uid too
    finally:
        q.close()
        q.lab.close()


def test_duplicate_of_an_invisible_ticket_is_a_key_only(w):
    h = Human(w)
    dup_uid, dup_key = w.ticket("the duplicate")
    orig_uid, orig_key = w.ticket("the original, secret title")
    w.restrict(orig_uid, [w.lab.owner])  # the owner sees it; no-person does not
    h.run("close", {"ref": dup_key, "resolution": "duplicate", "duplicate_of": orig_key})
    as_agent_class(w)
    n = w.client()
    r = get(n, dup_key)
    assert r["closed"]["duplicate_of"] == orig_key
    closed_item = next(i for i in r["log"] if i["type"] == "ticket.closed")
    assert closed_item["data"] == {"resolution": "duplicate", "duplicate_of": orig_key}
    assert "secret title" not in json.dumps(r)
    assert err(n.req("ticket.get", {"ref": orig_key})) == "not_found"


# --- cut flags: exactly when cut ---------------------------------------------------------------------------------


def test_the_cut_limits_are_the_contracts():
    assert (reads_ticket.DECISIONS_MAX, reads_ticket.PUSHES_MAX, reads_ticket.SECTION_REVS_MAX) == (100, 50, 20)
    assert (reads_ticket.TAKEOVERS_MAX, reads_ticket.ARTIFACTS_MAX) == (20, 1000)


def change_requests(w, uid, key, c, n):
    for _ in range(n):
        g = get(c, key)["gates"]["requirements"]
        w.person_event(
            "gate.changes_requested", uid, gate="requirements", gate_gen=g["gen"], hash=g["hash"],
            policy_hash=g["policy_hash"], text=f"change at {w.lab.ws.clock[0]}",
        )  # fmt: skip


def test_more_flags_are_set_exactly_when_cut(w, monkeypatch):
    for name in ("DECISIONS_MAX", "PUSHES_MAX", "SECTION_REVS_MAX", "TAKEOVERS_MAX", "ARTIFACTS_MAX"):
        monkeypatch.setattr(reads_ticket, name, 2)
    h = Human(w)
    uid, key = w.ticket("cuts")
    c = h.c
    h.agent("claim", {"ref": key})
    for sid in ("context", "requirements", "out_of_scope"):
        h.agent("section.set", {"ref": key, "section": sid, "message": f"the {sid}"})
    link_repo(w, uid)

    def flags(r):
        g = r["gates"]["requirements"]
        return {
            "decisions": ("decisions_more" in g, len(g["decisions"])),
            "pushes": ("pushes_more" in r, len(r["pushes"])),
            "sections": (r.get("sections_more", {}), len(r["sections"]["context"])),
            "takeovers": ("takeovers_more" in r["claim"], len(r["claim"]["takeovers"])),
            "artifacts": ("artifacts_more" in r, len(r["artifacts"])),
        }

    # the cap (2) of each: context already has one edit
    h.agent("section.set", {"ref": key, "section": "context", "message": "the context, again"})
    change_requests(w, uid, key, c, 2)
    push(w, uid, sha(1))
    push(w, uid, sha(2), before={"repo_id": "local:app", "ref": "refs/heads/main", "sha": sha(1)})
    sessions = [get(c, key)["claim"]["session"]]
    for _ in range(2):
        sessions.append("s_" + new_ulid())
        agent_ev(w, uid, "claim.taken", session=sessions[-1], takeover={"from_session": sessions[-2], "reason": "t"})
    for i in range(2):
        add_file(c, w, key, f"f{i}.txt", f"bytes {i}".encode())
    assert flags(get(c, key)) == {
        "decisions": (False, 2), "pushes": (False, 2), "sections": ({}, 2), "takeovers": (False, 2),
        "artifacts": (False, 2),
    }  # fmt: skip
    assert "sections_more" not in get(c, key)
    # one more of each: the oldest are cut and the flags appear
    h.agent("section.set", {"ref": key, "section": "context", "message": "the context, a third time"})
    change_requests(w, uid, key, c, 1)
    push(w, uid, sha(3), before={"repo_id": "local:app", "ref": "refs/heads/main", "sha": sha(2)})
    last = "s_" + new_ulid()
    agent_ev(w, uid, "claim.taken", session=last, takeover={"from_session": sessions[-1], "reason": "once more"})
    add_file(c, w, key, "f2.txt", b"bytes 2")
    r = get(c, key)
    assert flags(r) == {
        "decisions": (True, 2), "pushes": (True, 2), "sections": ({"context": True}, 2), "takeovers": (True, 2),
        "artifacts": (True, 2),
    }  # fmt: skip
    assert r["sections_more"] == {"context": True} and r["artifacts_more"] is True and r["pushes_more"] is True
    events = events_of(w, uid)
    assert [p["sha"] for p in r["pushes"]] == [sha(2), sha(3)]  # the newest, oldest first
    asked = [e["id"] for e in events if e["type"] == "gate.changes_requested"]
    assert [d["id"] for d in r["gates"]["requirements"]["decisions"]] == asked[-2:]
    edits = [e["seq"] for e in events if e["type"] == "ticket.updated" and "context" in e.get("sections", {})]
    assert [x["seq"] for x in r["sections"]["context"]] == edits[-2:]
    assert [a["name"] for a in r["artifacts"]] == ["f0.txt", "f1.txt"]  # the first by name
    assert r["claim"]["takeovers"][-1]["session"] == last and len(r["claim"]["takeovers"]) == 2
    assert flags(get(c, key, log_limit=1))["pushes"] == (True, 2)  # unaffected by the log window


# --- v1.5: hashes, the claim's idle_until, Closed.seq/event_id ------------------------------------------------------


def test_the_v15_hashes_equal_the_model_and_the_signed_events(w):
    f = full(w)
    view, events = model(w, f.uid), events_of(w, f.uid)
    r = get(f.c, f.key)
    by_id = {e["id"]: e for e in events}
    for name, g in r["gates"].items():
        gv = view.gates[name]
        assert g["hash"] == gv.hash and g["policy_hash"] == gv.policy_hash
        assert g["hash12"] == g["hash"].removeprefix("sha256:")[:12] and g["hash"].startswith("sha256:" + g["hash12"])
        for d in g["decisions"]:
            e = by_id[d["id"]]
            assert (d["hash"], d["policy_hash"]) == (e["hash"], e["policy_hash"])  # what the event signed
    approved = r["gates"]["requirements"]["decisions"][0]
    assert approved["hash"] == next(e for e in events if e["type"] == "gate.approved")["hash"]


def test_a_blocked_gate_has_no_policy_hash(w):
    h = Human(w)
    uid, key = w.ticket("blocked")
    before = get(h.c, key)["gates"]["verify"]
    assert before["blocked"] is False and before["policy_hash"] is not None

    # a ticket override is intersected with the workspace default: when that leaves no token the gate is blocked
    def pol(*approvers):
        return {"approvers": sorted(approvers), "count": 1, "not": [], "applies": "all", "independent": False}

    w.person_event("policy.changed", uid, gates={"verify": pol("reviewers", "watchers")})  # still has "reviewers"
    w.person_event("policy.changed", gates={"verify": pol("owner")})  # the workspace default moves on: nothing left
    g = get(h.c, key)["gates"]["verify"]
    assert g["blocked"] is True and g["policy_hash"] is None and g["eligible"] == []
    assert g["hash"] is not None and g["hash12"] == g["hash"].removeprefix("sha256:")[:12]


def test_the_claim_is_in_full_with_idle_until_and_lapses(w):
    h = Human(w)
    uid, key = w.ticket("claimed")
    assert get(h.c, key)["claim"] is None
    h.agent("claim", {"ref": key})
    w.lab.ws.tick(30)
    h.agent("ac.add", {"ref": key, "text": "later activity"})
    events = events_of(w, uid)
    taken = next(e for e in events if e["type"] == "claim.taken")
    view = model(w, uid)
    r = get(h.c, key)["claim"]
    last = events[-1]["at"]
    assert last != taken["at"]
    ttl = w.service.store.state.workspace.settings["claim_ttl_min"]
    assert r["session"] == view.claim.session == taken["actor"]["session"]
    assert r["agent"] == taken["actor"]["id"] and r["for"] == taken["actor"]["for"] == w.lab.owner
    assert r["grant"] == taken["actor"]["grant"] == view.claim.grant
    assert (r["since"], r["last_at"], r["live"], r["lapsed"]) == (taken["at"], last, True, None)
    assert r["takeovers"] == [] and "takeovers_more" not in r
    assert r["idle_until"] == iso(ts(last) + ttl * 60)
    assert get(h.c, key)["doc"]["claim"] == {"session": r["session"], "live": True}
    w.lab.ws.clock[0] += ttl * 60 + 5  # past the idle window: the claim no longer lives, but is not yet released
    lapsed = get(h.c, key)["claim"]
    assert lapsed["live"] is False and lapsed["lapsed"] == "expired" and lapsed["idle_until"] == r["idle_until"]
    assert model(w, uid).claim.live is False


def test_a_takeover_is_listed_with_its_session_and_reason(w):
    h = Human(w)
    uid, key = w.ticket("takeover")
    h.agent("claim", {"ref": key})
    first = get(h.c, key)["claim"]["session"]
    second = "s_" + new_ulid()
    agent_ev(w, uid, "claim.taken", session=second, takeover={"from_session": first, "reason": "stuck"})
    claim = get(h.c, key)["claim"]
    ev = next(e for e in reversed(events_of(w, uid)) if e["type"] == "claim.taken")
    assert claim["session"] == second
    assert claim["takeovers"] == [{"at": ev["at"], "session": second, "from_session": first, "reason": "stuck"}]


def test_closed_seq_and_event_id_name_the_closing_event(w):
    h = Human(w)
    uid, key = w.ticket("closed ids")
    h.run("close", {"ref": key, "resolution": "other", "message": "done with it"})
    closed = get(h.c, key)["closed"]
    ev = next(e for e in events_of(w, uid) if e["type"] == "ticket.closed")
    assert (closed["seq"], closed["event_id"]) == (ev["seq"], ev["id"])
    args = {"ref": uid, "seq": closed["seq"], "field": "text", "event_id": closed["event_id"]}
    assert ok(h.c.req("ticket.text", args))["data"]["text"] == closed["text"] == "done with it"


# --- source and pushes; receipts follow the source ---------------------------------------------------------------


def test_source_equals_the_latest_branch_pushed_and_a_receipt_follows_it(w):
    h = Human(w)
    uid, key = w.ticket("source")
    h.agent("claim", {"ref": key})
    link_repo(w, uid, "app")
    h.agent("ac.add", {"ref": key, "text": "proved by a task"})
    h.agent("task.add", {"ref": key, "text": "t", "verify": "make test", "proves": ["AC1"]})
    assert get(h.c, key)["source"] == [] and get(h.c, key)["pushes"] == []
    push(w, uid, sha(1))
    push(w, uid, sha(2), before={"repo_id": "local:app", "ref": "refs/heads/main", "sha": sha(1)})
    receipt = {"cmd": "make test", "exit": 0, "ms": 1, "repo": "app", "commit": sha(2)}
    agent_ev(w, uid, "task.done", task="T1", receipt=receipt)
    r = get(h.c, key)
    pushed = [e for e in events_of(w, uid) if e["type"] == "branch.pushed"]
    assert r["source"] == [{"name": "app", "repo": "local:app", "ref": "refs/heads/main", "sha": sha(2)}]
    assert r["source"][0]["sha"] == pushed[-1]["sha"] == model(w, uid).source_list[0]["sha"]
    assert [p["seq"] for p in r["pushes"]] == [e["seq"] for e in pushed]  # oldest first
    assert r["pushes"][0]["before"] is None
    assert r["pushes"][1] == {
        "seq": pushed[1]["seq"], "at": pushed[1]["at"], "repo_name": "app", "repo_id": "local:app",
        "ref": "refs/heads/main", "sha": sha(2),
        "before": {"repo_id": "local:app", "ref": "refs/heads/main", "sha": sha(1)},
    }  # fmt: skip
    assert r["tasks"][0]["receipt"]["evidence"] is True and r["criteria"][0]["tasks"] == [
        {"id": "T1", "evidence": True}
    ]
    assert r["criteria"][0]["covered"] is True
    push(w, uid, sha(3), before={"repo_id": "local:app", "ref": "refs/heads/main", "sha": sha(2)})  # the code moved on
    r = get(h.c, key)
    assert r["source"][0]["sha"] == sha(3) and r["tasks"][0]["receipt"]["evidence"] is False
    assert r["criteria"][0]["tasks"] == [{"id": "T1", "evidence": False}] and r["criteria"][0]["covered"] is False
    assert r["tasks"][0]["receipt"]["commit"] == sha(2)  # the receipt is shown as stored


# --- who is shown ---------------------------------------------------------------------------------------------


def test_question_addressees_and_gate_eligible_are_limited_to_persons_who_can_see_the_ticket(w):
    h = Human(w)
    uid, key = w.ticket("who sees")
    h.agent("claim", {"ref": key})
    h.agent("ask", {"ref": key, "text": "for quinn?", "text_question": True, "to": w.quinn.ref})
    h.agent("ask", {"ref": key, "text": "for the ticket owner?", "text_question": True, "to": "ticket_owner"})
    w.person_event("people.changed", uid, role="reviewers", add=[w.quinn.ref], remove=[])
    r = get(h.c, key)
    assert [q["to"] for q in r["questions"]] == [[w.quinn.ref], [w.lab.owner]]
    assert [q["to_ref"] for q in r["questions"]] == [w.quinn.ref, "ticket_owner"]
    assert r["gates"]["verify"]["eligible"] == [w.quinn.ref]
    w.restrict(uid, [w.lab.owner])  # quinn can no longer see the ticket
    r = get(h.c, key)
    assert r["questions"][0]["to"] == [w.lab.owner]  # falls back to the owners and maintainers who can see it
    assert r["questions"][1]["to"] == [w.lab.owner]
    assert w.quinn.ref not in json.dumps(r["gates"]) and r["gates"]["verify"]["eligible"] == []
    assert model(w, uid).questions[0].addressees == (w.quinn.ref,)  # the model's own list still names quinn


# --- the snapshot cursor, the frame limit, the unauthenticated viewer ---------------------------------------


def test_sub_cursor_is_the_journal_position_of_the_read(w):
    h = Human(w)
    uid, key = w.ticket("cursor")
    r = get(h.c, key)
    assert jpos_of(r["sub_cursor"]) == jpos_of(w.snapshot_cursor(principal_of()))
    w.write(key, "one more")
    later = get(h.c, key)
    assert jpos_of(later["sub_cursor"]) > jpos_of(r["sub_cursor"]) and later["log"][-1]["data"]["text"] == "one more"


def test_a_result_over_the_frame_is_too_large_and_the_hint_names_log_limit(w, monkeypatch):
    h = Human(w)
    uid, key = w.ticket("big")
    for i in range(5):
        w.write(key, f"note {i} " + "x" * 800)
    assert len(json.dumps(get(h.c, key))) > 5000
    monkeypatch.setattr(reads_ticket, "RESULT_MAX", 5000)
    res = h.c.req("ticket.get", {"ref": key})
    assert err(res) == "result.too_large" and "log_limit" in res["error"]["hint"] and res["error"]["retryable"] is False
    assert get(h.c, key, log_limit=1)["log_more"] is True  # a smaller log fits


def test_no_person_reads_a_workspace_ticket_and_never_a_restricted_one(w):
    uid, key = w.ticket("open to all")
    hidden_uid, hidden = w.ticket("zebracorn secret")
    w.restrict(hidden_uid, [w.lab.owner])
    as_agent_class(w)
    n = w.client()
    assert get(n, key)["doc"]["title"] == "open to all"
    res = n.req("ticket.get", {"ref": hidden})
    assert err(res) == "not_found" and "zebracorn" not in json.dumps(res)
    assert err(n.req("ticket.get", {"ref": hidden_uid})) == "not_found"
    assert err(n.req("ticket.get", {"ref": key, "log_limit": 501})) == "invalid.input"
    assert err(n.req("ticket.get", {"ref": key, "extra": 1})) == "invalid.input"


# --- H12e (§11A.14): doc.head, questions[].answer.id and members_v from one snapshot -------------------------------


def test_doc_head_is_the_last_event_of_the_log_and_members_v_the_roster_version(w):
    f = full(w)
    events = events_of(w, f.uid)
    r = get(f.c, f.key, log_limit=1)  # the head never depends on the log page
    assert r["doc"]["head"] == {"seq": events[-1]["seq"], "head": canon.event_head(events[-1])}
    assert r["members_v"] == w.service.store.state.workspace.roster_v
    (q,) = r["questions"]
    answered = next(e for e in events if e["type"] == "question.answered")
    assert q["answer"]["id"] == answered["id"]  # the counting event's id: a phone resolves its own answer by it
    agent_ev(w, f.uid, "log.added", text="one more")
    w.person_event("role.changed", person=w.quinn.ref, role="maintainer")
    r2 = get(f.c, f.key)
    last = events_of(w, f.uid)[-1]
    assert r2["doc"]["head"] == {"seq": last["seq"], "head": canon.event_head(last)} != r["doc"]["head"]
    assert r2["members_v"] == r["members_v"] + 1


def test_doc_head_and_members_v_are_read_under_one_store_lock(w, monkeypatch):
    """One snapshot: the handler holds the store lock from the cursor to the last field, so another writer cannot land
    between ``doc.head``, the questions and ``members_v``."""
    f = full(w)
    store = w.service.store
    depth = {"n": 0, "max_outside": 0}
    real = type(store).locked

    import contextlib

    @contextlib.contextmanager
    def counting(self):
        depth["n"] += 1
        try:
            with real(self):
                yield
        finally:
            depth["n"] -= 1

    monkeypatch.setattr(type(store), "locked", counting)
    seen = []
    real_doc = reads_ticket._doc

    def doc(x):
        seen.append(depth["n"])
        return real_doc(x)

    monkeypatch.setattr(reads_ticket, "_doc", doc)
    get(f.c, f.key)
    assert seen and all(n >= 1 for n in seen)


def test_an_open_question_has_no_answer_id_and_a_stale_answer_none(w):
    h = Human(w)
    uid, key = w.ticket("q")
    h.agent("ask", {"ref": key, "text": "which?", "option": ["a | a", "b | b"]})
    (q,) = get(h.c, key)["questions"]
    assert q["state"] == "open" and "answer" not in q
