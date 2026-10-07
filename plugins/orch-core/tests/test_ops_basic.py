import pytest

from orch.core import store
from orch.core.events import read_events
from orch.core.gates import record_approval
from orch.core.ops import Ops
from orch.errors import ClaimError, HumanOnlyError, TicketParseError, TransitionError, UsageError, ValidationError


def test_new_creates_backlog_ticket(ws, aops):
    t = aops.new("Back up nightly config", ask="Please.")
    path, loaded = store.load(ws, t.id)
    assert path.parent.name == "backlog" and loaded.section("Ask") == "Please."
    assert "[claude-code 7f3c] created" in loaded.section("Log")
    assert [e.kind for e in read_events(ws)] == ["ticket.created"]


@pytest.mark.parametrize("kw", [{"type": "saga"}, {"priority": "p1"}, {"size": "xl"}])
def test_new_rejects_bad_choices(aops, kw):
    with pytest.raises(UsageError):
        aops.new("x", **kw)


def test_new_empty_title(aops):
    with pytest.raises(UsageError):
        aops.new("   ")


def test_new_external_url_from_tracker(configure, agent):
    ws = configure(external_trackers=[{"prefix": "ABC", "pattern": "ABC-\\d+", "url": "https://jira/browse/{key}"}])
    t = Ops(ws, agent).new("x", external="abc-123")
    assert t.meta["external"] == [{"key": "ABC-123", "url": "https://jira/browse/ABC-123"}]


def test_new_from_links_both(ws, aops):
    a = aops.new("parent")
    b = aops.new("child", from_ref=a.id)
    assert b.meta["parent"] == a.id
    assert store.load(ws, a.id)[1].meta["follow_ups"] == [b.id]


def test_claim_moves_open_to_in_progress(ws, aops, put):
    tid = put("open")
    t = aops.claim(tid)
    assert t.status == "in-progress" and t.meta["claim"]["session"] == "7f3c9a21-0000"
    assert t.meta["sessions"][0]["harness"] == "claude-code"
    assert store.resolve(ws, tid).status == "in-progress"


def test_claim_conflict_and_expiry(ws, aops, other_agent, put):
    tid = put("open")
    aops.claim(tid)
    with pytest.raises(ClaimError):
        Ops(ws, other_agent).claim(tid)
    path, t = store.load(ws, tid)
    t.meta["claim"]["at"] = "2020-01-01T00:00Z"
    store.save(ws, t, path)
    assert Ops(ws, other_agent).claim(tid).meta["claim"]["harness"] == "copilot"


def test_claim_rejects_backlog_and_blocked(aops, put):
    with pytest.raises(TransitionError):
        aops.claim(put("backlog"))
    blocker = put("open")
    tid = put("open", blocked_by=[blocker])
    with pytest.raises(ValidationError, match=blocker):
        aops.claim(tid)


def test_release_rules(ws, aops, other_agent, hops, put):
    tid = put("open")
    aops.claim(tid)
    with pytest.raises(ClaimError):
        Ops(ws, other_agent).release(tid)
    hops.release(tid)
    assert store.load(ws, tid)[1].meta["claim"]["session"] is None


def test_log_state_sections(ws, aops, hops, put):
    tid = put("in-progress")
    aops.log(tid, "ran dbt\nbuild")
    aops.set_state(tid, "Half done.")
    aops.set_section(tid, "plan", "1. step")
    t = store.load(ws, tid)[1]
    assert t.section("Log").splitlines()[-3].endswith("ran dbt build")
    assert t.section("Current state") == "Half done." and t.section("Plan") == "1. step"
    with pytest.raises(UsageError):
        aops.set_section(tid, "Log", "x")
    with pytest.raises(UsageError):
        aops.set_section(tid, "Nope", "x")
    with pytest.raises(HumanOnlyError):
        aops.set_section(tid, "Ask", "rewritten")
    hops.set_section(tid, "Ask", "clarified by me")


def test_link(ws, aops, put):
    tid = put("in-progress")
    aops.link(tid, branch="feature/x")  # --repo defaults to the workspace's only repo (#14, #214)
    assert store.load(ws, tid)[1].meta["branches"] == {"harness": "feature/x"}
    aops.link(tid, repo="hub", pr="https://x/pr/1", branch="feature/L-1")
    aops.link(tid, repo="hub", pr="https://x/pr/1")  # idempotent
    aops.link(tid, external="TIX-17")
    aops.link(tid, external="tix-17")
    m = store.load(ws, tid)[1].meta
    assert m["prs"] == [{"repo": "hub", "url": "https://x/pr/1", "state": "unknown"}]
    assert m["branches"] == {"harness": "feature/x", "hub": "feature/L-1"} and m["repos"] == ["harness", "hub"]
    assert [x["key"] for x in m["external"]] == ["TIX-17"]


def test_move_rules(ws, aops, hops, human, put):
    tid = put("in-progress")
    with pytest.raises(ValidationError):
        aops.move(tid, "waiting")
    with pytest.raises(HumanOnlyError):
        aops.move(tid, "backlog")
    with pytest.raises(UsageError):
        aops.move(tid, "archived")
    path, t = store.load(ws, tid)
    t.set_section("Plan", "p")
    record_approval(ws, t, "plan", human)
    store.save(ws, t, path)
    hops.move(tid, "backlog")
    t = store.load(ws, tid)[1]
    assert t.status == "backlog" and t.meta["gates"]["plan"]["approved"] is None
    assert read_events(ws, tid)[-1].data == {"from": "in-progress", "to": "backlog"}


def test_artifact_add(ws, aops, put, tmp_path):
    tid = put("in-progress")
    src = tmp_path / "report.html"
    src.write_text("<p>x</p>", encoding="utf-8")
    dest = aops.artifact_add(tid, src)
    assert dest == ws.artifacts_dir / tid / "report.html"
    assert dest.read_text(encoding="utf-8") == "<p>x</p>"
    with pytest.raises(ValidationError):
        aops.artifact_add(tid, src)
    assert aops.artifact_add(tid, src, name="../evil:name.html").name == "evil-name.html"
    assert read_events(ws, tid)[-1].kind == "artifact.added"


def test_artifact_add_failure_leaves_nothing(ws, aops, put, tmp_path, monkeypatch):
    import orch.core.ops as ops_module

    tid = put("in-progress")
    src = tmp_path / "report.html"
    src.write_text("<p>x</p>", encoding="utf-8")

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ops_module.shutil, "copy2", boom)
    with pytest.raises(OSError):
        aops.artifact_add(tid, src, name="report.html")
    dest_dir = ws.artifacts_dir / tid
    dest = dest_dir / "report.html"
    assert not dest.exists()
    assert not dest_dir.exists() or list(dest_dir.iterdir()) == []

    monkeypatch.undo()
    assert aops.artifact_add(tid, src, name="report.html") == dest
    assert dest.read_text(encoding="utf-8") == "<p>x</p>"


def test_ops_do_not_dispatch_to_addons(ws, aops, put):
    tid = put("open")
    aops.claim(tid)
    assert ws._addons is None  # events reach addons through the outbox in orch serve (Task 6)


def test_parse_error_leaves_file_untouched(ws, aops):
    p = ws.status_dir("open") / "L-0005-broken.md"
    p.write_text("---\nid: [x\n---\n", encoding="utf-8")
    before = p.read_bytes()
    with pytest.raises(TicketParseError):
        aops.claim("L-0005")
    assert p.read_bytes() == before
