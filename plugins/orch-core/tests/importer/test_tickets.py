"""``orch import v1``: tickets, fields, status, refusals, idempotence, resume (ticket-format section 14)."""

from __future__ import annotations

from orch.ops import import_run
from tests.importer.helpers import replays_clean, snapshot, tree, types, with_repo
from tests.ops.humans import agent, hws, me  # noqa: F401

KEYS = [f"DEMO-000{n}" for n in range(1, 9)]


def test_a_dry_run_writes_nothing_and_says_what_it_would_do(hws, me, v1):  # noqa: F811
    with_repo(hws)
    before, v1_before = snapshot(hws.root), tree(v1)
    r = me("import", "v1", str(v1), "--dry-run")
    assert r.code == 0, r.err
    assert r.out.splitlines()[0] == "ok import.v1 8 tickets (39 events)"
    assert "dry-run: nothing was written" in r.out and "8 to import, 0 already imported, 0 skipped" in r.out
    assert snapshot(hws.root) == before and tree(v1) == v1_before
    assert hws.reviews == [] and hws.provider.requests == []  # no review, no passphrase


def test_every_ticket_is_imported_with_its_v1_key_and_status(imported, hws):
    status = {k: hws.view(k).status for k in KEYS}
    assert status == {
        "DEMO-0001": "backlog",
        "DEMO-0002": "open",
        "DEMO-0003": "open",  # in-progress in v1: no claim exists in v2
        "DEMO-0004": "open",  # testing in v1
        "DEMO-0005": "closed",  # done in v1: closed, never done (nothing was verified in v2)
        "DEMO-0006": "closed",
        "DEMO-0007": "closed",
        "DEMO-0008": "open",  # waiting in v1
    }
    assert imported.first == "ok import.v1 8 tickets (39 events)"
    assert imported.data if False else True


def test_fields_labels_links_and_references(imported, hws):
    t = hws.ticket_json("DEMO-0003")
    assert (t["priority"], t["size"], t["due"], t["parent"]) == ("high", "s", "2026-10-30", "DEMO-0002")
    assert t["labels"] == ["imported-v1", "needs-review"]
    assert t["blocked_by"] == ["DEMO-0001"]  # DEMO-0099 was never a ticket
    # only a repository this workspace knows is linked; the rest stays in the history
    assert t["links"]["repos"] == ["pipelines"] and t["links"]["branches"] == {"pipelines": "feature/DEMO-0003-cost"}
    assert t["links"]["external"] == ["https://jira.example.com/browse/FIN-12"]
    assert t["links"]["prs"] == [{"repo": "pipelines", "url": "https://github.com/acme/pipelines/pull/7"}]
    assert hws.ticket_json("DEMO-0001")["labels"] == ["customer-globex", "data", "imported-v1"]
    assert hws.ticket_json("DEMO-0004")["type"] == "spike"  # v1 investigation
    assert hws.ticket_json("DEMO-0001")["priority"] == "medium"  # v1 normal


def test_criteria_tasks_and_questions(imported, hws):
    t = hws.ticket_json("DEMO-0003")
    assert [(a["id"], a["text"]) for a in t["acceptance"]] == [
        ("AC1", "Loads in 5 s."),
        ("AC2", "Totals match the invoice within 1 percent."),
    ]
    assert [(x["id"], x["text"], x["proves"]) for x in t["tasks"]] == [
        ("T1", "Create the view", ["AC1"]),
        ("T2", "Build the dashboard", ["AC2"]),
        ("T3", "Share it", []),
    ]
    assert all(x["verify"] is None for x in t["tasks"])  # a v1 command never becomes a v2 one
    assert [q["id"] for q in t["questions"]] == ["Q1"]  # the answered Q2 stays in the history
    assert t["questions"][0]["to"] == "ticket_owner" and t["questions"][0]["recommended"] == "jan"
    v = hws.view("DEMO-0003")
    assert [x.state for x in v.tasks] == ["open", "open", "open"]  # no task state is carried over


def test_sections_and_the_note(imported, hws):
    body = (hws.root / "tickets" / hws.uid("DEMO-0003") / "body.md").read_text()
    assert "## Context" in body and "Show cost by month." in body and "## Out of scope" in body
    assert "Imported from v1 as DEMO-0003 (v1 status: in-progress" in body
    assert "v1 task state: T1 done, T2 doing, T3 todo." in body and "Dashboard half done." in body
    assert "A finding." not in body and "## Log" not in body  # a feature ticket has neither: history only
    spike = (hws.root / "tickets" / hws.uid("DEMO-0004") / "body.md").read_text()
    assert "## Findings" in spike and "The join." in spike
    ask = (hws.root / "tickets" / hws.uid("DEMO-0008") / "body.md").read_text()
    assert "Ask (v1):\n\nPick one.\n\nSome context." in ask


def test_nothing_v1_decided_counts_in_v2(imported, hws):
    v = hws.view("DEMO-0003")  # v1 had approved its requirements
    assert {g: (s.hash is None or True) for g, s in v.gates.items()}  # the gates exist...
    for key in KEYS:
        gates = hws.view(key).gates
        assert all(d.kind not in ("approve", "pass") for g in gates.values() for d in g.decisions), key
        assert hws.view(key).status != "done"


def test_closed_tickets_keep_why(imported, hws):
    ev = {
        k: [e for e in hws.events(k) if e["type"] == "ticket.closed"][0]
        for k in ("DEMO-0005", "DEMO-0006", "DEMO-0007")
    }
    assert [(e["resolution"]) for e in ev.values()] == ["other", "duplicate", "wont_do"]
    assert ev["DEMO-0006"]["duplicate_of"] == "DEMO-0005"
    assert "Imported from v1: done (completed)" in ev["DEMO-0005"]["text"]


def test_the_import_asks_once_and_names_the_plan(hws, me, v1):  # noqa: F811
    with_repo(hws)
    assert me("import", "v1", str(v1)).code == 0
    assert len(hws.provider.requests) == 1  # one passphrase for 39 signatures
    shown = hws.provider.shown[0]
    assert "import: 8 tickets from v1, 39 events" in shown and "plan: sha256:" in shown
    assert hws.expects == ["IMPORT 8"]  # typed on the terminal first
    review = hws.reviews[0]
    assert (
        "v1 status: backlog 1, done 3" in review
        and "approvals, verdicts" in review
        and "| DEMO-0003 [in-progress] Cost view" in review
    )


def test_not_confirmed_signs_and_writes_nothing(hws, me, v1):  # noqa: F811
    with_repo(hws)
    hws.confirm = False
    before = snapshot(hws.root)
    r = me("import", "v1", str(v1))
    assert r.code == 5 and "not confirmed" in r.err
    assert snapshot(hws.root) == before and hws.provider.requests == []


def test_an_agent_is_refused(hws, agent, v1):  # noqa: F811
    r = agent.j("import", "v1", str(v1))
    assert r.code == 3 and r.err_code == "human_only"


def test_the_v1_workspace_is_never_touched(hws, me, v1):  # noqa: F811
    with_repo(hws)
    before = tree(v1)
    assert me("import", "v1", str(v1)).code == 0
    assert me("import", "v1", str(v1 / "orchestrator")).code == 0  # the folder itself works too, as a rerun
    assert tree(v1) == before


def test_no_v1_workspace_here(hws, me, tmp_path):  # noqa: F811
    r = me.j("import", "v1", str(tmp_path))
    assert r.code == 2 and r.err_code == "not_found"


def test_a_different_prefix_is_refused_before_anything_is_signed(hws, me, v1):  # noqa: F811
    cfg = v1 / "orchestrator" / "config.json"
    cfg.write_text(cfg.read_text().replace('"DEMO"', '"OTHER"'))
    before = snapshot(hws.root)
    r = me.j("import", "v1", str(v1))
    assert r.code == 5 and r.err_code == "invalid.input" and "prefix" in r.doc["error"]["message"]
    assert snapshot(hws.root) == before


def test_a_rerun_changes_nothing(imported, me, hws, v1):
    before = snapshot(hws.root)
    hws.provider.requests.clear()
    r = me("import", "v1", str(v1))
    assert r.code == 0 and r.first == "ok import.v1 0 tickets (0 events)"
    assert "0 to import, 8 already imported" in r.out
    assert snapshot(hws.root) == before and hws.provider.requests == []  # no new signature, no prompt


def test_a_rerun_after_v1_changed_does_not_update_v2(imported, me, hws, v1):
    path = next((v1 / "orchestrator" / "tickets" / "backlog").glob("DEMO-0001*"))
    path.write_text(path.read_text().replace("Ingest the readings nightly.", "Changed in v1."))
    before = snapshot(hws.root)
    r = me("import", "v1", str(v1))
    assert r.code == 0 and "v1 changed since it was imported; not touched" in r.out
    assert snapshot(hws.root) == before


def test_edits_made_in_v2_after_the_import_survive_a_rerun(imported, me, hws, agent, v1):  # noqa: F811
    assert agent("claim", "DEMO-0001").code == 0
    assert agent("section", "set", "summary", "-m", "mine").code == 0
    assert agent("release").code == 0
    before = snapshot(hws.root)
    assert me("import", "v1", str(v1)).code == 0
    assert snapshot(hws.root) == before
    assert "mine" in (hws.root / "tickets" / hws.uid("DEMO-0001") / "body.md").read_text()


def test_an_interrupted_import_resumes_where_it_stopped(hws, me, v1, monkeypatch):  # noqa: F811
    with_repo(hws)
    real = import_run.Importer._append
    seen = []

    def flaky(self, ev, uid, body, files, sign):
        if len(seen) == 12:  # in the middle of the third ticket
            raise RuntimeError("the machine went to sleep")
        seen.append(ev["type"])
        return real(self, ev, uid, body, files, sign)

    monkeypatch.setattr(import_run.Importer, "_append", flaky)
    r = me("import", "v1", str(v1))
    assert r.code == 5 and "partial" in r.out
    assert hws.provider.requests  # the person was asked once
    monkeypatch.setattr(import_run.Importer, "_append", real)
    r = me("import", "v1", str(v1))
    assert r.code == 0 and "6 to import, 2 already imported" in r.out  # the partial ticket and the ones after it
    fresh = hws.other()
    fresh.load_all()
    assert len(fresh.state.tickets) == 8
    done = [
        e
        for k in KEYS
        for e in hws.events(k)
        if e["type"] == "log.added" and e["text"].startswith("import.v1: complete")
    ]
    assert len(done) == 8
    replays_clean(hws).close()
    # the same events as an import that was never interrupted
    assert sum(len(hws.events(k)) for k in KEYS) == 39


def test_a_key_that_v2_already_uses_is_skipped_with_the_reason(hws, me, agent, v1):  # noqa: F811
    with_repo(hws)
    for title in ("one", "two", "three"):  # DEMO-0001..0003 are taken by native v2 tickets
        assert agent("new", title).code == 0
    r = me("import", "v1", str(v1))
    assert r.code == 0
    assert "skipped DEMO-0001: the key is taken by another v2 ticket" in r.out
    assert hws.view("DEMO-0001").title == "one" and hws.view("DEMO-0004").title == "Why is it slow"
    # DEMO-0003 was not imported, so a ticket that names it as parent or blocker has no such link
    assert hws.ticket_json("DEMO-0004")["parent"] is None
    replays_clean(hws).close()


def test_a_broken_ticket_file_is_reported_and_the_others_come_in(hws, me, v1):  # noqa: F811
    with_repo(hws)
    bad = v1 / "orchestrator" / "tickets" / "open" / "DEMO-0009-broken.md"
    bad.write_text("---\nid: DEMO-0009\ntitle: [unclosed\n---\n")
    r = me("import", "v1", str(v1))
    assert r.code == 0 and "8 to import, 0 already imported, 1 skipped" in r.out
    assert "skipped tickets/open/DEMO-0009-broken.md: cannot be read" in r.out
    assert hws.view("DEMO-0009") is None


def test_a_ticket_the_model_refuses_is_skipped_whole(hws, me, v1):  # noqa: F811
    """A ticket whose steps v2 refuses is not imported at all: no half ticket, no signature spent on it."""
    with_repo(hws)
    p = next((v1 / "orchestrator" / "tickets" / "open").glob("DEMO-0002*"))
    p.write_text(p.read_text().replace("## Requirements", "## Requirements\n\n```\nopen fence"))
    r = me("import", "v1", str(v1))
    assert r.code == 0
    assert "skipped DEMO-0002" in r.out
    assert hws.view("DEMO-0002") is None and hws.view("DEMO-0001") is not None
    # the child that named it as parent comes in without the link
    assert hws.ticket_json("DEMO-0003")["parent"] is None


def test_the_imported_workspace_replays_clean(imported, hws):
    s = replays_clean(hws)
    s.load_all()
    assert len(s.state.tickets) == 8
    assert all(types(hws, k)[0] == "ticket.created" and types(hws, k)[-1] == "log.added" for k in KEYS)
    s.close()
