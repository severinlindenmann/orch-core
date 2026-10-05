"""Artifacts are linked in the ticket: `orch artifact add` registers files and URLs in the frontmatter with a kind
and optionally the task or criterion they prove; unlinked files and URLs are found; a move to testing warns."""
import json

import pytest

from orch.core import artifacts, store
from orch.core.events import read_events
from orch.errors import UsageError, ValidationError


def _png(tmp_path, name="shot.png", data=b"\x89PNG\r\n\x1a\nfake"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _meta(ws, tid):
    return store.load(ws, tid)[1]


# -- registering ---------------------------------------------------------------------------------------------------

def test_adding_a_file_registers_it_in_the_ticket(ws, aops, working, tmp_path):
    dest = aops.artifact_add(working, _png(tmp_path), label="Login page after the fix", ac=1)
    t = _meta(ws, working)
    [e] = t.meta["artifacts"]
    assert e["name"] == "shot.png" and e["kind"] == "screenshot" and e["ac"] == 1
    assert e["label"] == "Login page after the fix"
    assert e["sha256"] == artifacts.file_sha256(dest) and e["size"] == dest.stat().st_size
    assert "added artifact shot.png (screenshot) for AC1" in t.section("Log")
    ev = read_events(ws, working)[-1]
    assert ev.kind == "artifact.added" and ev.data["name"] == "shot.png" and ev.data["ac"] == 1


def test_adding_a_url_registers_a_link(ws, aops, working):
    e = aops.artifact_link(working, "https://github.com/acme/repo/actions/runs/1", label="CI run on the PR",
                           kind="build")
    t = _meta(ws, working)
    assert t.meta["artifacts"] == [{**e}]
    assert e["url"] == "https://github.com/acme/repo/actions/runs/1" and e["kind"] == "build"
    assert read_events(ws, working)[-1].data["url"] == e["url"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "ftp://x/y", "data:text/html,x",
                                 "https://ex.com/‮evil", "https://ex.com/a b", "https://"])
def test_only_plain_web_links_are_accepted(aops, working, url):
    with pytest.raises((UsageError, ValidationError)):
        aops.artifact_link(working, url)


def test_kinds_are_checked_and_guessed(ws, aops, working, tmp_path):
    with pytest.raises(UsageError):
        aops.artifact_add(working, _png(tmp_path), kind="selfie")
    log = tmp_path / "run.log"
    log.write_text("ok", encoding="utf-8")
    aops.artifact_add(working, log)
    assert _meta(ws, working).meta["artifacts"][0]["kind"] == "log"
    assert aops.artifact_link(working, "https://ex.com/d")["kind"] == "link"


def test_task_and_criterion_must_exist(ws, aops, working, tmp_path):
    with pytest.raises(ValidationError, match="AC3"):
        aops.artifact_add(working, _png(tmp_path), ac=3)
    with pytest.raises(ValidationError, match="T9"):
        aops.artifact_link(working, "https://ex.com/x", task="T9")
    _, ids = aops.task_add(working, [{"text": "the work"}])
    assert aops.artifact_link(working, "https://ex.com/x", task=ids[0])["task"] == ids[0]


def test_labels_with_hidden_characters_are_refused(aops, working, tmp_path):
    with pytest.raises(ValidationError):
        aops.artifact_add(working, _png(tmp_path), label="fine‮txt.exe")
    with pytest.raises(ValidationError):
        aops.artifact_link(working, "https://ex.com", label="two\nlines")


def test_file_names_are_sanitised_and_stay_in_the_ticket_folder(ws, aops, working, tmp_path):
    src = _png(tmp_path)
    for bad, good in (("../../etc/x.png", "x.png"), ("..\\..\\y.png", "-..-y.png"), (".hidden.png", "hidden.png"),
                      ("a​b.png", "ab.png")):
        dest = aops.artifact_add(working, src, name=bad)
        assert dest.parent == ws.artifacts_dir / working and dest.name == good
    with pytest.raises(UsageError):
        aops.artifact_add(working, src, name="..")


def test_files_over_the_size_limit_are_refused(configure, agent, tmp_path, put):
    from orch.core.ops import Ops
    ws = configure(artifacts={"mode": "local", "max_mb": 0.001})
    tid = put("in-progress")
    big = tmp_path / "big.csv"
    big.write_bytes(b"x" * 5000)
    with pytest.raises(ValidationError, match="larger"):
        Ops(ws, agent).artifact_add(tid, big)
    assert not (ws.artifacts_dir / tid / "big.csv").exists()


def test_replace_updates_the_hash(ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path))
    with pytest.raises(ValidationError):
        aops.artifact_add(working, _png(tmp_path, data=b"other"))
    aops.artifact_add(working, _png(tmp_path, data=b"other"), replace=True)
    [e] = _meta(ws, working).meta["artifacts"]
    assert e["sha256"] == artifacts.file_sha256(ws.artifacts_dir / working / "shot.png")


def test_a_file_already_in_the_ticket_folder_is_registered_in_place(ws, aops, working):
    d = ws.artifacts_dir / working
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.html").write_text("<p>r</p>", encoding="utf-8")
    dest = aops.artifact_add(working, d / "report.html")
    assert dest == d / "report.html"
    assert _meta(ws, working).meta["artifacts"][0]["name"] == "report.html"


def test_inline_evidence_goes_into_verification(ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path), label="Jobs page lists 14 serverless jobs", ac=1, inline=True)
    t = _meta(ws, working)
    assert "- AC1: Jobs page lists 14 serverless jobs\n  ![Jobs page lists 14 serverless jobs](artifact:shot.png)" \
        in t.section("Verification")
    from orch.core import evidence
    assert evidence.criteria(t)[0].proven
    aops.artifact_link(working, "https://ex.com/cost", label="Cost comparison sheet", ac=2, inline=True)
    assert "- AC2: Cost comparison sheet\n  [Cost comparison sheet](https://ex.com/cost)" in \
        _meta(ws, working).section("Verification")


def test_inline_needs_a_criterion(aops, working, tmp_path):
    with pytest.raises(UsageError):
        aops.artifact_add(working, _png(tmp_path), inline=True)


# -- discovery -----------------------------------------------------------------------------------------------------

def test_scan_registers_files_written_straight_into_the_folder(ws, aops, working):
    d = ws.artifacts_dir / working
    (d / "sub").mkdir(parents=True)
    (d / "a.png").write_bytes(b"a")
    (d / "sub" / "b.csv").write_text("x", encoding="utf-8")
    (d / ".a.png.123.tmp").write_bytes(b"tmp")
    t = _meta(ws, working)
    assert artifacts.unregistered(ws, t) == ["a.png", "sub/b.csv"]
    assert aops.artifact_scan(working) == ["a.png", "sub/b.csv"]
    t = _meta(ws, working)
    assert [e["name"] for e in t.meta["artifacts"]] == ["a.png", "sub/b.csv"]
    assert artifacts.unregistered(ws, t) == []
    assert aops.artifact_scan(working) == []


def test_scan_registers_static_notes_of_the_ticket(ws, aops, working):
    d = ws.static_dir / working
    d.mkdir(parents=True)
    (d / "notes.md").write_text("n", encoding="utf-8")
    assert aops.artifact_scan(working) == [f"static:{working}/notes.md"]
    assert _meta(ws, working).meta["artifacts"][0] == {**_meta(ws, working).meta["artifacts"][0],
                                                       "static": f"{working}/notes.md", "kind": "report"}


def test_mentions_find_unlinked_urls_and_files(ws, aops, working):
    aops.link(working, pr="https://github.com/acme/repo/pull/7", repo="r")
    aops.set_section(working, "Verification",
                     "- AC1: CI green https://github.com/acme/repo/actions/runs/9 and see /tmp/shots/after.png\n"
                     "- AC2: PR https://github.com/acme/repo/pull/7")
    t = _meta(ws, working)
    assert artifacts.mentions(t) == ["https://github.com/acme/repo/actions/runs/9", "/tmp/shots/after.png"]
    aops.artifact_link(working, "https://github.com/acme/repo/actions/runs/9", kind="build")
    assert artifacts.mentions(_meta(ws, working)) == ["/tmp/shots/after.png"]


def test_check_warns_about_unlinked_files_mentions_and_changed_files(ws, aops, working, tmp_path):
    from orch.core.check import run_checks
    aops.artifact_add(working, _png(tmp_path))
    (ws.artifacts_dir / working / "loose.log").write_text("l", encoding="utf-8")
    (ws.artifacts_dir / working / "shot.png").write_bytes(b"swapped")
    aops.set_section(working, "Verification", "- AC1: dashboard https://dash.example.com/d/1 shows it")
    codes = {(f.code, f.level) for f in run_checks(ws, emit_events=False) if f.ticket == working}
    assert {("artifact-unlinked", "warning"), ("artifact-mention", "warning"), ("artifact-changed", "warning")} <= codes


def test_check_flags_inline_images_without_alt_or_registration(ws, aops, working, tmp_path):
    from orch.core.check import run_checks
    aops.artifact_add(working, _png(tmp_path))
    aops.set_section(working, "Verification", "- AC1: looks right\n  ![](artifact:shot.png)\n  ![x](artifact:nope.png)")
    found = {f.code: f.message for f in run_checks(ws, emit_events=False) if f.ticket == working}
    assert "shot.png" in found["artifact-image-alt"]
    assert "nope.png" in found["artifact-ref-missing"]


# -- handing over --------------------------------------------------------------------------------------------------

def _ready(aops, hops, tid, close_tasks, verification):
    aops.set_section(tid, "Plan", "1. migrate")
    hops.approve(tid, "plan")
    close_tasks(aops, tid)
    aops.set_section(tid, "Verification", verification)


def test_move_to_testing_warns_about_unlinked_evidence(ws, aops, hops, working, close_tasks):
    _ready(aops, hops, working, close_tasks,
           "- AC1: 14 jobs on serverless, CI https://ci.example.com/run/5\n- AC2: cost sheet in out/cost.csv")
    aops.move(working, "testing")
    text = " ".join(aops.warnings)
    assert "https://ci.example.com/run/5" in text and "out/cost.csv" in text and "orch artifact add" in text


def test_move_to_testing_registers_loose_files_and_warns(ws, aops, hops, working, close_tasks):
    _ready(aops, hops, working, close_tasks, "- AC1: 14 jobs on serverless\n- AC2: cost compared in the sheet")
    d = ws.artifacts_dir / working
    d.mkdir(parents=True, exist_ok=True)
    (d / "cost.csv").write_text("x", encoding="utf-8")
    t = aops.move(working, "testing")
    assert t.status == "testing" and [e["name"] for e in t.meta["artifacts"]] == ["cost.csv"]
    assert any("cost.csv" in w for w in aops.warnings)


def test_move_to_testing_warns_when_visual_criteria_have_no_artifacts(ws, aops, hops, close_tasks):
    t = aops.new("Show thresholds")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] The daily report shows a threshold column")
    hops.approve(t.id, "requirements")
    aops.claim(t.id)
    _ready(aops, hops, t.id, close_tasks, "- AC1: the column appears for all 14 meters")
    moved = aops.move(t.id, "testing")
    assert moved.status == "testing"
    assert any("no artifacts" in w and "AC1" in w for w in aops.warnings)


# -- CLI -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def as_agent(monkeypatch, aops):
    from orch import actor
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    monkeypatch.setenv("ORCH_SESSION", aops.actor.session)
    monkeypatch.setattr(actor, "is_interactive", lambda: False)


def test_cli_adds_urls_and_files_with_kind_task_and_criterion(ws, working, as_agent, tmp_path, capsys):
    from orch.cli import run
    capsys.readouterr()
    assert run(["artifact", "add", working, "--url", "https://ci.example.com/run/5", "--label", "CI run",
                "--kind", "build", "--ac", "2", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["url"] == "https://ci.example.com/run/5"
    assert run(["artifact", "add", working, str(_png(tmp_path)), "--ac", "1", "--inline",
                "--label", "Jobs page with 14 serverless jobs"]) == 0
    assert run(["artifact", "add", working]) == 2  # neither a file nor --url
    capsys.readouterr()
    assert run(["artifact", "list", working, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r.get("name") or r.get("url"), r["kind"]) for r in rows] == [
        ("https://ci.example.com/run/5", "build"), ("shot.png", "screenshot")]


def test_cli_list_flags_unlinked_files_and_scan_links_them(ws, working, as_agent, capsys):
    from orch.cli import run
    d = ws.artifacts_dir / working
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.png").write_bytes(b"x")
    capsys.readouterr()
    assert run(["artifact", "list", working]) == 0
    assert "not linked" in capsys.readouterr().out
    assert run(["artifact", "scan", working]) == 0
    assert "x.png" in capsys.readouterr().out
    assert _meta(ws, working).meta["artifacts"][0]["name"] == "x.png"


# -- ticket document (phone) ---------------------------------------------------------------------------------------

def test_document_lists_artifacts_by_name_label_and_kind_only(ws, aops, working, tmp_path):
    import jsonschema
    from orch.core.schema import SCHEMA_VERSION, ticket_document, ticket_schema
    aops.artifact_add(working, _png(tmp_path), label="Jobs page", ac=1)
    aops.artifact_link(working, "https://dash.example.com/d/1?token=secret", label="Cost dashboard", kind="report")
    doc = ticket_document(ws, _meta(ws, working))
    assert SCHEMA_VERSION == "1.9.0"
    sha = _meta(ws, working).meta["artifacts"][0]["sha256"]
    assert doc["artifact_items"] == [
        {"source": "file", "kind": "screenshot", "label": "Jobs page", "name": "shot.png", "sha256": sha, "ac": 1,
         "by": "agent:claude-code:7f3c9a21"},
        {"source": "link", "kind": "report", "label": "Cost dashboard", "by": "agent:claude-code:7f3c9a21"}]
    assert "secret" not in json.dumps(doc["artifact_items"])
    assert doc["artifacts"] == ["shot.png"]
    jsonschema.validate(doc, ticket_schema())


def test_document_items_never_carry_urls_paths_or_bad_hashes(ws, aops, working, tmp_path):
    from orch.core.schema import ticket_document
    aops.artifact_link(working, "https://dash.example.com/d/1", label="see https://dash.example.com/d/1?token=s")
    aops.artifact_link(working, "https://ci.example.com/run/5", label="www.ci.example.com/run/5")
    aops.artifact_add(working, _png(tmp_path))
    d = ws.static_dir / working
    d.mkdir(parents=True)
    (d / "notes.md").write_text("n", encoding="utf-8")
    aops.artifact_scan(working)
    path, t = store.load(ws, working)
    t.meta["artifacts"][2]["sha256"] = "not-a-hash"
    store.save(ws, t, path)
    items = ticket_document(ws, _meta(ws, working))["artifact_items"]
    assert [i["label"] for i in items] == ["link", "link", "shot.png", "notes.md"]
    assert "sha256" not in items[2]
    assert "example.com" not in json.dumps(items) and working + "/" not in json.dumps(items)
