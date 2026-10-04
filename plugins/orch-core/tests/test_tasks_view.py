from orch.core import query, store, tasks_view


def test_view_resolves_every_ref_kind(ws_root, configure, aops, hops, working, plan_approved):
    ws2 = configure(git={"repos": {"hub": {}}},
                    external_trackers=[{"prefix": "ABC", "pattern": "ABC-\\d+", "url": "https://jira.test/browse/{key}"}])
    (ws_root / "hub" / "jobs").mkdir(parents=True)
    (ws_root / "hub" / "jobs" / "x.yml").write_text("a", encoding="utf-8")
    other = aops.new("Access").id
    aops.ask(working, [{"text": "Which schedule?", "options": ["2am", "4am"], "blocking": False}])
    aops.task_add(working, [
        {"text": "Migrate", "refs": ["file:hub/jobs/x.yml#L2-4", "file:hub/missing.py", "ac:2", "ac:9",
                                     f"ticket:{other}", "ext:ABC-7", "q:Q1", "static:notes/a.md",
                                     "artifact:inv.csv", "section:Decisions", "url:https://docs.test/x — docs"]},
        {"text": "Grant", "owner": "human", "needs": ["T1"]}])
    plan_approved(working)
    aops.task_start(working, "T1")
    v = tasks_view.view(ws2, store.load(ws2, working)[1])
    assert (v["format"], v["doing"], v["next"], v["open"]) == ("orch.tasks.v1", "T1", "T1", ["T1", "T2"])
    assert v["can_move_to_testing"] is False and v["summary"]["total"] == 2 and v["error"] is None
    refs = {(r["kind"], r["target"]): r for r in v["tasks"][0]["refs"]}
    f = refs[("file", "hub/jobs/x.yml#L2-4")]
    assert (f["repo"], f["path"], f["lines"], f["exists"]) == ("hub", "jobs/x.yml", "2-4", True)
    assert refs[("file", "hub/missing.py")]["exists"] is False
    assert refs[("ac", "2")]["text"] == "cost compared" and refs[("ac", "9")]["exists"] is False
    assert refs[("ticket", other)]["status"] == "backlog" and refs[("ticket", other)]["title"] == "Access"
    assert refs[("ext", "ABC-7")]["url"] == "https://jira.test/browse/ABC-7"
    assert refs[("q", "Q1")]["text"] == "Which schedule?" and refs[("q", "Q1")]["answered"] is False
    assert refs[("static", "notes/a.md")]["exists"] is False
    assert refs[("artifact", "inv.csv")]["url"] == f"/a/{working}/inv.csv"
    assert refs[("section", "Decisions")]["exists"] is True
    assert refs[("url", "https://docs.test/x")]["label"] == "docs"
    assert v["tasks"][1]["needs_open"] == ["T1"] and v["tasks"][1]["owner"] == "human"


def test_blocked_on_an_open_question_waits_on_you(ws, aops, working):
    aops.ask(working, [{"text": "FYI?", "type": "confirm", "blocking": False}])
    aops.task_add(working, [{"text": "a"}])
    aops.task_block(working, "T1", "needs the answer", on="q1")
    t = tasks_view.view(ws, store.load(ws, working)[1])["tasks"][0]
    assert t["on_ref"] == {"kind": "question", "id": "Q1", "exists": True, "answered": False}
    assert t["waits_on_you"] is True


def test_can_move_to_testing_is_the_real_rule(ws, aops, hops, working, close_tasks):
    aops.set_section(working, "Verification", "ran it")
    aops.task_add(working, [{"text": "the work"}])
    assert tasks_view.view(ws, store.load(ws, working)[1])["can_move_to_testing"] is False  # plan not approved
    aops.set_section(working, "Plan", "one step")
    hops.approve(working, "plan")
    assert tasks_view.view(ws, store.load(ws, working)[1])["can_move_to_testing"] is False  # T1 still open
    aops.task_done(working, "T1")
    assert tasks_view.view(ws, store.load(ws, working)[1])["can_move_to_testing"] is True


def test_view_of_a_broken_section_reports_instead_of_raising(ws, put):
    tid = put("in-progress", sections={"Tasks": "- [ ] T1 a\nfree text"})
    path, t = store.load(ws, tid)
    v = tasks_view.view(ws, t)
    assert v["error"].startswith("Tasks line 2") and v["tasks"] == [] and v["open"] == []
    assert query.ticket_view(ws, path, t)["tasks"]["error"] == v["error"]


def test_view_without_ref_resolution_skips_exists_probes(ws, aops, working, monkeypatch):
    aops.task_add(working, [{"text": "a", "refs": ["file:hub/x.py", "static:n.md"]}])
    calls = []
    monkeypatch.setattr(tasks_view, "_exists", lambda *a: calls.append(a) or False)
    t = store.load(ws, working)[1]
    v = tasks_view.view(ws, t, resolve_refs=False)
    assert calls == [] and [r["kind"] for r in v["tasks"][0]["refs"]] == ["file", "static"]
    assert all(r["exists"] is None for r in v["tasks"][0]["refs"])
    tasks_view.view(ws, t)
    assert len(calls) == 2


def test_exists_checks_a_literal_path_only(tmp_path):
    (tmp_path / "a1.md").write_text("x", encoding="utf-8")
    assert tasks_view._exists(tmp_path, "a1.md") is True
    assert tasks_view._exists(tmp_path, "a[0-9].md") is False


def test_today_resolves_no_refs(dash, aops, working, monkeypatch):
    aops.task_add(working, [{"text": "a", "refs": ["file:hub/x.py"]}])
    calls = []
    monkeypatch.setattr(tasks_view, "_exists", lambda *a: calls.append(a) or False)
    assert dash.get("/").status_code == 200 and calls == []


def test_ticket_ref_keeps_its_target_and_adds_resolved(ws_root, configure, aops, working):
    ws2 = configure(external_trackers=[{"prefix": "ABC", "pattern": "ABC-\\d+", "url": "https://jira.test/browse/{key}"}])
    other = aops.new("Access", external="ABC-7").id
    aops.task_add(working, [{"text": "a", "refs": ["ticket:ABC-7", f"ticket:{other}", "ticket:ZZZ-1"]}])
    refs = tasks_view.view(ws2, store.load(ws2, working)[1])["tasks"][0]["refs"]
    assert [(r["target"], r["resolved"]) for r in refs] == [("ABC-7", other), (other, other), ("ZZZ-1", None)]
    from orch.dashboard.data import tasks as tasks_data
    assert tasks_data._ref_chip(refs[0])["url"] == f"/t/{other}"
