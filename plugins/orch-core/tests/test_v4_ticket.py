"""M (v4 ticket page): a journey bar with dates (Asked → Agreed → Doing → Proven → Done), "What we agreed" and "Proof
so far" with evidence tiles (an empty tile is missing proof), tasks with their artifacts, and an Artifacts panel in the
aside grouped by kind, each item tied to its task or criterion, with a "not linked yet" group."""
import re

import pytest

pytest.importorskip("fastapi")

from orch.core import store  # noqa: E402


def _png(tmp_path, name, data=b"\x89PNG-x"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _journey(html):
    bar = html.split('<ol class="journey" aria-label="Journey">', 1)[1].split("</ol>", 1)[0]
    return re.findall(r'<li class="jstage jstage-(\w+)"( aria-current="step")?>\s*<b class="jstage-name"><span aria-hidden="true">.</span> (\w+)</b>'
                      r'.*?<span class="jstage-note[^"]*">(.*?)</span>\s*</li>', bar, re.S)


def test_the_journey_has_five_stages_with_dates_and_who(dash, ws, working, aops, plan_approved):
    plan_approved(working)
    aops.task_add(working, [{"text": "a"}, {"text": "b"}])
    stages = _journey(dash.get(f"/t/{working}").text)
    assert [s[2] for s in stages] == ["Asked", "Agreed", "Doing", "Proven", "Done"]
    assert [s[0] for s in stages] == ["done", "done", "now", "todo", "todo"]
    assert [bool(s[1]) for s in stages] == [False, False, True, False, False]
    assert re.match(r"\d\d\.\d\d · by \S+", stages[0][3])
    assert re.match(r"\d\d\.\d\d · req \+ plan, you", stages[1][3])
    assert "tasks 0/2" in stages[2][3] and "AC 0/2" in stages[3][3] and stages[4][3] == "your verdict"


def test_a_done_ticket_has_every_stage_done_and_done_current(dash, put):
    """An agent-written status: done with no ledger entry is never "accepted" (R26 for Done)."""
    tid = put("done", gates={"verify": {"verdict": "done", "at": "2026-10-02T08:00Z", "by": "human:you"}})
    html = dash.get(f"/t/{tid}").text
    stages = _journey(html)
    assert all(s[0] == "done" for s in stages) and stages[4][1]
    assert "closed, not signed here" in stages[4][3] and "accepted" not in stages[4][3]
    done = html.split('<ol class="journey"', 1)[1].split("</ol>", 1)[0].split("Done</b>", 1)[1]
    assert 'class="jstage-note jstage-warn"' in done


def test_a_signed_verdict_reads_accepted(dash, ws, aops, hops, working, plan_approved, close_tasks):
    plan_approved(working)
    close_tasks(aops, working)
    aops.set_section(working, "Verification", "- AC1: ok\n- AC2: ok")
    aops.move(working, "testing")
    from orch.core.epics import verdict_hash
    hops.verdict(working, "done", expected_hash=verdict_hash([store.load(ws, working)[1]], ws))
    stages = _journey(dash.get(f"/t/{working}").text)
    assert stages[4][3].endswith("accepted")


def test_phone_caption_names_the_stages(dash, put):
    tid = put("testing", sections={"Verification": "ok"})
    cap = re.search(r'<p class="journey-caption muted" aria-hidden="true">(.*?)</p>', dash.get(f"/t/{tid}").text).group(1)
    assert cap.startswith("Asked ✓ · Agreed ✓ · Doing ✓ · Proven") and cap.endswith("Done")


def test_proof_so_far_has_evidence_tiles(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path, "jobs.png"), ac=1, inline=True, label="jobs page")
    html = dash.get(f"/t/{working}?open=all").text
    proof = html.split('id="proven"', 1)[1].split("</details>", 1)[0]
    assert '<h2 class="chapter-title">Proof so far</h2>' in html and '<h2 class="chapter-title">What we agreed</h2>' in html
    sha = store.load(ws, working)[1].meta["artifacts"][0]["sha256"]
    # the criterion's images show as a thumbnail strip under it, pinned to the bytes it links; a missing proof keeps its
    # dashed tile
    strip = proof.split('class="plain art-strip"', 1)[1].split("</ul>", 1)[0]
    assert re.search(rf'<img src="/a/{working}/jobs.png\?v={sha[:16]}"', strip)
    assert f'href="/t/{working}/view/jobs.png?v={sha[:16]}" data-viewer' in strip
    assert '<span class="ev-tile proof-tile ev-tile-missing" aria-hidden="true"><span class="ev-cap">no evidence yet</span>' in proof


def test_tasks_show_their_artifacts(dash, aops, working, plan_approved, tmp_path):
    plan_approved(working)
    _, ids = aops.task_add(working, [{"text": "the policy"}])
    aops.artifact_add(working, _png(tmp_path, "policy.json", b"{}"), task=ids[0], label="policy")
    html = dash.get(f"/t/{working}?open=all").text
    task = html.split(f'id="task-{ids[0]}"', 1)[1].split("</li>", 1)[0]
    assert 'class="chip chip-neu task-art"' in task and "policy" in task


def test_the_artifacts_panel_groups_by_kind_and_ties_items_to_tasks_and_criteria(dash, ws, aops, working, tmp_path):
    _, ids = aops.task_add(working, [{"text": "the work"}])
    aops.artifact_add(working, _png(tmp_path, "shot.png"), ac=1, label="cluster list")
    aops.artifact_add(working, _png(tmp_path, "dry-run.csv", b"a,b"), task=ids[0])
    aops.artifact_link(working, "https://ci.example.com/run/5", label="CI run", kind="build", task=ids[0])
    (ws.artifacts_dir / working / "loose.log").write_text("x", encoding="utf-8")
    html = dash.get(f"/t/{working}").text
    aside = html.split("<aside", 1)[1]
    panel = aside.split('<section class="card art-panel" id="artifacts"', 1)[1].split('<section class="card act-card"', 1)[0]
    # not in testing: grouped by orch's kinds, each group's images as a grid and its other files as rows with a type icon
    titles = re.findall(r'<h3 class="artifact-kind" id="art-g-\w+">([^<]+?) <span', panel)
    assert titles == ["Screenshots", "Datasets", "Builds"]
    grid = panel.split('class="plain art-grid"', 1)[1].split("</ul>", 1)[0]
    assert "<img " in grid and 'aria-label="cluster list, AC1"' in grid
    assert '<span class="fi fi-csv" aria-hidden="true">CSV</span>' in panel and '<span class="fi fi-link" aria-hidden="true">↗</span>' in panel
    assert f'href="#task-{ids[0]}"' in panel and "loose.log" in panel and "in the folder not linked" in panel
    assert 'aria-current="true">By type</a>' in panel
    assert 'rel="noopener noreferrer"' in panel  # a web link never sends the referrer
    assert aside.index('id="artifacts"') < aside.index('id="log"')


def test_a_file_not_linked_yet_is_named_never_drawn(dash, ws, working):
    """Fix round 3: an unlinked file is a name with a file icon; no thumbnail outside the binding."""
    d = ws.artifacts_dir / working
    d.mkdir(parents=True, exist_ok=True)
    (d / "loose.png").write_bytes(b"\x89PNG-x")
    panel = dash.get(f"/t/{working}").text.split('id="artifacts"', 1)[1].split("</section>", 1)[0]
    assert "in the folder not linked" in panel and "loose.png" in panel and "<img" not in panel


def test_agreed_names_a_human_only_when_the_ledger_signs_it(dash, ws, put):
    """Fix round 3 (R26): an agent-written approval (approved, hash, via phone) with no ledger entry is not credited."""
    from orch.core import store
    from orch.core.gates import gate_hash
    tid = put("open", sections={"Requirements": "- r", "Acceptance criteria": "- [ ] a"})
    path, t = store.load(ws, tid)
    t.meta["gates"] = {"requirements": {"approved": "2026-10-02T08:00Z", "hash": gate_hash(t, "requirements"),
                                        "by": "human:you", "via": "phone:iPhone"}}
    store.save(ws, t, path)
    stages = _journey(dash.get(f"/t/{tid}").text)
    agreed = stages[1][3]
    assert "approval not signed here" in agreed and "you" not in agreed and "phone" not in agreed
    html = dash.get(f"/t/{tid}").text
    assert 'class="jstage-note jstage-warn"' in html


def test_doing_stage_in_progress_never_says_not_started(dash, working, plan_approved):
    plan_approved(working)  # worked on, no tasks and no code yet
    doing = _journey(dash.get(f"/t/{working}").text)[2]
    assert doing[0] == "now" and "not started" not in doing[3] and "under way" in doing[3]


def test_epic_name_drops_the_epics_own_lead_in():
    from orch.dashboard.views import TEMPLATES
    mod = TEMPLATES.env.get_template("_ticket_card.html").module
    name = lambda title: str(mod.epic_name({"id": "DEMO-1", "title": title})).strip()  # noqa: E731
    assert name("Epic: harden the pipeline") == "harden the pipeline"
    assert name("Harden the pipeline") == "Harden the pipeline" and name("") == "DEMO-1"
