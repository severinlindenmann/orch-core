"""AI Factory: planner quality. What the epic asked for against what its children's text mentions (the Ready report's
coverage block), the planner prompt's one-child-per-deliverable rule and the planner's own model. Text only: a mention
is never reported as built."""
import json

import pytest

from orch.core import factory_report, factory_runner, store
from orch.dashboard import launch
from test_factory_runner import Fake, _tick, _trusted_programs, fa, fh, fws  # noqa: F401

ASK = ("One HTML file named elephants.html that reads a short data file elephants.json and shows the elephant "
       "populations as a bar chart.")
DONE = "- [ ] elephants.html opens in a browser and shows a bar per country\n- [ ] the data lives in elephants.json"


def _epic(fa, fh, req=ASK, ac=DONE, approve=True):
    e = fa.new("Elephants", type="epic")
    fa.set_section(e.id, "Requirements", req)
    fa.set_section(e.id, "Acceptance criteria", ac)
    if approve:
        fh.approve(e.id, "requirements", delegate={"factory": True})
    return e.id


def _child(fa, eid, title, req, ac):
    c = fa.new(title, epic=eid)
    fa.set_section(c.id, "Requirements", req)
    fa.set_section(c.id, "Acceptance criteria", ac)
    fa.set_section(c.id, "Plan", "1. do it")
    return c.id


def _cov(ws, eid):
    return factory_report.coverage(ws, store.load(ws, eid)[1])


def test_named_files_finds_names_with_extensions_and_paths_in_backticks():
    text = ("Write `web/index` and elephants.html, then ELEPHANTS.JSON and docs/Read.me.md. Not e.g. this, not "
            "`npm run build` nor https://x.org/a.html; `tools/` counts, so does `a.py`.")
    assert factory_report.named_files(text) == ["web/index", "elephants.html", "elephants.json", "docs/read.me.md",
                                                "tools/", "a.py"]
    assert factory_report.named_files("") == [] and factory_report.named_files("no file here.") == []


def test_the_live_example_one_design_child_leaves_both_files_uncovered(fws, fa, fh):
    eid = _epic(fa, fh)
    _child(fa, eid, "Design elephant chart component", "Design the chart component: its data schema and a mockup.",
           "- [ ] a schema for the population data is written\n- [ ] a mockup of the chart exists")
    c = _cov(fws, eid)
    assert c["files"] == ["elephants.html", "elephants.json"]
    assert c["uncovered"] == ["elephants.html", "elephants.json"] and c["readable"]
    assert c["asked"][0].startswith("elephants.html opens in a browser")
    assert not factory_report.coverage_ok(fws, store.load(fws, eid)[1])


def test_a_child_per_named_file_covers_the_epic(fws, fa, fh):
    eid = _epic(fa, fh)
    a = _child(fa, eid, "Page", "Build the page.", "- [ ] elephants.html renders the chart")
    b = _child(fa, eid, "Data", "Write `elephants.json`.", "- [ ] it parses as JSON")
    c = _cov(fws, eid)
    assert c["uncovered"] == [] and c["covered"] == {"elephants.html": [a], "elephants.json": [b]}
    assert factory_report.coverage_ok(fws, store.load(fws, eid)[1])


def test_a_mention_inside_another_name_does_not_count(fws, fa, fh):
    eid = _epic(fa, fh, req="Write out.csv", ac="- [ ] out.csv has a header")
    _child(fa, eid, "x", "Write about.csv and out.csv.bak", "- [ ] layout.csv exists")
    assert _cov(fws, eid)["uncovered"] == ["out.csv"]


def test_no_named_file_is_ok_and_an_unreadable_child_is_not(fws, fa, fh, monkeypatch):
    eid = _epic(fa, fh, req="Make the export faster", ac="- [ ] it is faster")
    _child(fa, eid, "x", "r", "- [ ] a")
    epic = store.load(fws, eid)[1]
    assert factory_report.coverage_ok(fws, epic)
    monkeypatch.setattr(factory_report, "_kids", lambda ws, epic, entries: None)
    assert factory_report.coverage(fws, epic)["readable"] is False and not factory_report.coverage_ok(fws, epic)


def test_the_ready_report_carries_the_coverage(fws, fa, fh, close_tasks):
    _ready_design_only(fws, fa, fh, close_tasks)


def _ready_design_only(fws, fa, fh, close_tasks):
    eid = _epic(fa, fh)
    cid = _child(fa, eid, "Design elephant chart component", "Design it.", "- [ ] a mockup exists")
    fa.epic_auto_approve(cid)
    fa.claim(cid)
    close_tasks(fa, cid)
    fa.set_section(cid, "Verification", "- AC1: drew the mockup and saw it")
    fa.move(cid, "testing")
    rep = factory_report.ready(fws, store.load(fws, eid)[1])
    assert rep is not None and rep["coverage"]["uncovered"] == ["elephants.html", "elephants.json"]
    return eid


def test_the_ready_card_warns_about_what_no_child_mentions(fws, fa, fh, close_tasks):
    pytest.importorskip("fastapi")
    from test_factory_dashboard import _card, _client
    eid = _ready_design_only(fws, fa, fh, close_tasks)
    for url in ("/", f"/t/{eid}", f"/factory/{eid}"):
        card = _card(_client(fws).get(url).text, "ready")
        assert "The epic asked for:" in card and "elephants.html opens in a browser" in card
        assert "text, not checked as built" in card
        assert "Not mentioned by any child: elephants.html, elephants.json" in card
        assert 'data-coverage="gap"' in card and "verified" not in card.lower().replace("not verifiable", "")


# -- the planner prompt --------------------------------------------------------------------------------------------

def test_the_planner_prompt_asks_for_one_child_per_named_deliverable():
    p = factory_runner.planner_prompt("L-0001")
    for words in ("list every concrete deliverable", "exact name with its extension", "at least one child per "
                  "deliverable and one deliverable per child", "exact file name into the child's Acceptance criteria",
                  "no design, spec, mockup or research child unless the epic asks for a design", "no placeholder child",
                  "xs, s or m, never larger", "`orch log L-0001 -m \"...\"`", factory_runner.CRITERION_EXAMPLE):
        assert words in p, words
    assert "\n" not in p


# -- the planner's model -------------------------------------------------------------------------------------------

def _write(body):
    p = launch.factory_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body), encoding="utf-8")


def test_planner_model_is_validated_like_model():
    _write({"command": launch.DEFAULT_FACTORY_COMMAND, "planner_model": "claude-opus-4"})
    assert launch.load_planner_model() == "claude-opus-4" and launch.load_factory_command()[1] is None
    for bad in ("x; rm -rf ~", "", "-x", 7, ["opus"], "a" * 80, "opus --dangerously-skip-permissions"):
        _write({"command": launch.DEFAULT_FACTORY_COMMAND, "planner_model": bad})
        argv, why = launch.load_factory_command()
        assert launch.load_planner_model() is None and argv == launch.DEFAULT_FACTORY_COMMAND, bad
        assert "planner_model is not a model name" in why
    _write({"command": launch.DEFAULT_FACTORY_COMMAND, "planner_model": "x", "other": 1})
    assert launch.load_planner_model() is None and "is ignored" in launch.load_factory_command()[1]


def test_with_model_replaces_or_adds_the_model_before_the_prompt():
    base = launch.DEFAULT_FACTORY_COMMAND
    assert launch.with_model(base, "opus") == [*base[:-1], "--model", "opus", "{prompt}"]
    for cmd in ([*base[:1], "--model", "haiku", *base[1:]], [*base[:1], "--model=haiku", *base[1:]]):
        out = launch.with_model(cmd, "opus")
        assert out.count("--model") == 1 and out[-3:] == ["--model", "opus", "{prompt}"] and "haiku" not in out
        assert launch.factory_command_error(out) is None
    assert launch.with_model(base, None) == base and launch.with_model(base, "a b") == base


def test_the_planner_runs_the_planner_model_and_children_the_work_model(fws, fa, fh, human):
    from orch.core import epics, factory_sessions as fs
    _write({"command": ["claude", "--model", "haiku", *launch.DEFAULT_FACTORY_COMMAND[1:]], "planner_model": "opus"})
    eid = _epic(fa, fh, req="Write out.csv", ac="- [ ] out.csv exists")
    d = epics.delegation(fws, store.load(fws, eid)[1])
    fs.arm(fws, fh.actor, d["id"])
    fake = Fake()
    lines = _tick(fws, human, fake)
    assert len(fake.started) == 1, lines
    ((_, _, argv),) = fake.started
    assert argv[argv.index("--model") + 1] == "opus" and argv.count("--model") == 1
    cid = _child(fa, eid, "x", "Write out.csv", "- [ ] out.csv exists")
    fa.epic_auto_approve(cid)
    for b in fs.bindings(fws):  # the planner's session ends; the child's starts with the work model
        fake.stop(b["name"])
    _tick(fws, human, fake)
    child = [a for _, _, a in fake.started if any(cid in w for w in a)]
    assert child and child[-1][child[-1].index("--model") + 1] == "haiku"
