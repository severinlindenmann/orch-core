"""Dark AI Factory: the files an epic names must be in a child's commit, not only mentioned (the first live run: the
request misspelled the names, the children faithfully built elepthans.json and elpehant.html, and one child left its
file untracked and still counted as proven). Real git and real runner-made clones; a fake for the recipe's commands."""
import shutil

import pytest

from orch.core import epics, factory_built as fb, factory_close, factory_release as fr, factory_report, \
    factory_sessions as fs, store
from test_factory_clones import _g, _msg, _programs, _recipe, _refine, fa, fh, fws, remote, bin_dir  # noqa: F401,E501
from test_factory_release import Fake, _not_stopping  # noqa: F401

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
ASKED = "One page elephants.html that reads elephants.json"


def _to_testing(fa, cid, close_tasks):
    """In testing with evidence the strict rules accept (the release starts no stage on less)."""
    fa.claim(cid)
    close_tasks(fa, cid)
    fa.set_section(cid, "Verification", "- AC1: ran `pytest -q` on the branch, 3 passed")
    fa.move(cid, "testing")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """A fresh cache; and no move precheck: these tests put children in testing in states the precheck refuses (a
    session from before it, or a human's move) to prove the Ready report, the release and the close catch them too.
    tests/test_factory_move_precheck.py tests the precheck itself."""
    fb._CACHE.clear()
    monkeypatch.setattr(fb, "move_refusal", lambda ws, t: None)
    yield
    fb._CACHE.clear()


def _epic(fws, fa, fh, human, asked=ASKED, **charter):
    e = fa.new("Elephants", type="epic")
    _refine(fa, e.id, plan=None)
    fa.set_section(e.id, "Requirements", asked)
    fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, **charter})
    fs.arm(fws, human, epics.delegation(fws, store.load(fws, e.id)[1])["id"])
    return e.id


def _child(fws, fa, human, eid, title, files=None, untracked=None, reqs=None):
    c = fa.new(title, epic=eid)
    _refine(fa, c.id)
    fa.set_section(c.id, "Requirements", reqs or f"Write {' '.join(files or untracked or {})}")
    fa.epic_auto_approve(c.id)
    clone, why = fc_ensure(fws, human, c.id)
    for name, body in (files or {}).items():
        (clone / name).write_text(body, encoding="utf-8")
        _g(clone, "add", name)
    if files:
        _g(clone, "commit", "-q", *_msg(c.id))
    for name, body in (untracked or {}).items():
        (clone / name).write_text(body, encoding="utf-8")
    return c.id


def fc_ensure(fws, human, cid):
    from orch.core import factory_clones
    clone, why = factory_clones.ensure(fws, human, cid)
    assert why == "", why
    return clone, why


def _live_example(fws, fa, fh, human, close_tasks, **charter):
    """The live run: one child committed the misspelled data file, the other left its misspelled page untracked."""
    eid = _epic(fws, fa, fh, human, **charter)
    a = _child(fws, fa, human, eid, "data", files={"elepthans.json": "[]\n"})
    b = _child(fws, fa, human, eid, "page", files={"notes.txt": "x\n"}, untracked={"elpehant.html": "<p>x</p>\n"})
    for c in (a, b):
        _to_testing(fa, c, close_tasks)
    return eid, a, b


def test_the_ready_report_names_what_is_in_no_commit_and_uncommitted_work(fws, fa, fh, human, close_tasks,
                                                                           remote):  # noqa: F811
    eid, a, b = _live_example(fws, fa, fh, human, close_tasks)
    rep = factory_report.ready(fws, store.load(fws, eid)[1])
    bt = rep["coverage"]["built"]
    assert bt["unknown"] == []
    # the page was left untracked (not in a commit), the data file is in one under the misspelled name
    assert bt["missing"] == [{"name": "elephants.html", "similar": []},
                             {"name": "elephants.json", "similar": ["elepthans.json"]}]
    assert [d["id"] for d in bt["dirty"]] == [b] and any("elpehant.html" in x for x in bt["dirty"][0]["lines"])
    pytest.importorskip("fastapi")
    from test_dark_dashboard import _client
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Not in any child's commit: elephants.html, elephants.json (found similar: elepthans.json)" in html
    assert "has uncommitted work in its clone" in html and f'data-dirty="{b}"' in html


def test_a_named_file_in_no_childs_commit_blocks_the_merge(fws, fa, fh, human, close_tasks, remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, a, b = _live_example(fws, fa, fh, human, close_tasks, release="merge")
    fake = Fake()
    lines = fr.tick(fws, human, fake)
    assert not fake.calls, lines
    assert any("Not in any child's commit: elephants.html, elephants.json (found similar: elepthans.json)" in x
               for x in lines), lines
    assert [r["code"] for r in factory_report.stopped(fws, store.load(fws, eid)[1])] == ["release-blocked"]


def test_once_the_named_files_are_committed_the_merge_runs(fws, fa, fh, human, close_tasks, remote):  # noqa: F811
    from orch.core import factory_clones
    fr.set_recipe(fws, human, _recipe(remote))
    eid = _epic(fws, fa, fh, human, release="merge")
    a = _child(fws, fa, human, eid, "data", files={"elephants.json": "[]\n"})
    b = _child(fws, fa, human, eid, "page", files={"elephants.html": "<p>x</p>\n"})
    for c in (a, b):
        _to_testing(fa, c, close_tasks)
    assert factory_clones.record(fws, a) and fb.built(fws, store.load(fws, eid)[1], fb.epic_files(
        store.load(fws, eid)[1]), factory_report._kids(fws, store.load(fws, eid)[1], None))["missing"] == []
    fr.tick(fws, human, Fake())
    st = fr.status(fws, store.load(fws, eid)[1], epics.delegation(fws, store.load(fws, eid)[1]))
    assert st["stages"][0]["state"] == "proven"


def test_the_auto_close_waits_for_named_files_and_committed_work(fws, fa, fh, human, close_tasks, remote):  # noqa: F811
    eid, a, b = _live_example(fws, fa, fh, human, close_tasks, close=True)
    e = store.load(fws, eid)[1]
    texts = [x["text"] for x in factory_close.blockers(fws, e, epics.delegation(fws, e)) if x["code"] == "built"]
    assert "Not in any child's commit: elephants.html, elephants.json (found similar: elepthans.json)" in texts
    assert f"{b} has uncommitted work in its clone" in texts
    assert factory_close.tick(fws, human) == [] and store.load(fws, eid)[1].status == "open"


def test_near_matches_help_and_never_decide():
    assert fb.similar(["data/elepthans.json", "src/app.py"], "elephants.json") == ["data/elepthans.json"]
    assert fb.similar(["giraffes.json"], "elephants.json") == []
    assert fb.has(["Web/Elephants.HTML"], "elephants.html") and not fb.has(["xelephants.html"], "elephants.html")
    assert fb.missing(["a.json"], [["b/a.json"]]) == []


def test_a_planted_submodule_with_a_clean_filter_never_runs_and_is_named(fws, fa, fh, human, close_tasks, remote,
                                                                         tmp_path):  # noqa: F811
    eid = _epic(fws, fa, fh, human, close=True)
    a = _child(fws, fa, human, eid, "data", files={"elephants.json": "[]\n", "elephants.html": "<p>x</p>\n"})
    clone, _ = fc_ensure(fws, human, a)
    marker = tmp_path / "MARKER"
    sub = clone / "sub"
    sub.mkdir()
    _g(sub, "init", "-q")
    (sub / "f").write_text("a\n", encoding="utf-8")
    (sub / ".gitattributes").write_text("* filter=m\n", encoding="utf-8")
    _g(sub, "add", "f", ".gitattributes")
    _g(sub, "commit", "-q", "-m", "s")
    _g(sub, "config", "filter.m.clean", f"touch {marker}; cat")
    (clone / ".gitattributes").write_text("* filter=x diff=x\n", encoding="utf-8")  # names drivers nobody configured
    _g(clone, "add", "sub")  # a gitlink in the index
    (sub / "f").write_text("b\n", encoding="utf-8")  # the submodule's work tree changed: a nested status would filter
    _to_testing(fa, a, close_tasks)
    st = fb.uncommitted(fws, a)
    assert st["ok"] and st["submodules"] == ["sub"] and not marker.exists()
    rep = factory_report.ready(fws, store.load(fws, eid)[1])
    assert rep["coverage"]["built"]["subs"] == [{"id": a, "paths": ["sub"]}] and not marker.exists()
    e = store.load(fws, eid)[1]
    texts = [x["text"] for x in factory_close.blockers(fws, e, epics.delegation(fws, e))]
    assert any("submodule orch does not inspect" in t for t in texts) and not marker.exists(), texts


def test_a_clone_whose_state_cannot_be_read_is_never_clean(fws, fa, fh, human, close_tasks, remote,
                                                           monkeypatch):  # noqa: F811
    from orch.core import factory_clones
    eid = _epic(fws, fa, fh, human, close=True)
    a = _child(fws, fa, human, eid, "data", files={"elephants.json": "[]\n", "elephants.html": "<p>x</p>\n"})
    _to_testing(fa, a, close_tasks)
    monkeypatch.setattr(factory_clones, "fetch_from", lambda ws, child, fn: None)  # locked, moved or unreadable
    assert fb.uncommitted(fws, a) == fb.UNREADABLE
    fb._CACHE.clear()
    bt = factory_report.ready(fws, store.load(fws, eid)[1])["coverage"]["built"]
    assert {"id": a, "why": "the state of its clone could not be read"} in bt["unknown"]
    e = store.load(fws, eid)[1]
    texts = [x["text"] for x in factory_close.blockers(fws, e, epics.delegation(fws, e)) if x["code"] == "built"]
    assert f"the state of {a}'s clone could not be read" in texts
    pytest.importorskip("fastapi")
    from test_dark_dashboard import _client
    html = _client(fws).get(f"/factory/{eid}").text
    assert f'data-built-unknown="{a}"' in html and "Unreadable" in html


def test_the_merge_block_goes_to_the_child_that_names_the_file_and_says_how_to_retry(fws, fa, fh, human, close_tasks,
                                                                                   remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid = _epic(fws, fa, fh, human, release="merge")
    a = _child(fws, fa, human, eid, "data", files={"elephants.json": "[]\n"})
    b = _child(fws, fa, human, eid, "page", files={"notes.txt": "x\n"}, reqs="Write elephants.html")
    for c in (a, b):
        _to_testing(fa, c, close_tasks)
    lines = fr.tick(fws, human, Fake())
    blocked = fr._blocked_record(fws, eid)
    assert blocked["unit"] == b and f"Retry release once {b} (or another child) commits it" in blocked["why"], lines



# -- the third live run's add/add conflict: two clones each add elefant.json ------------------------------------------

def _two_adds(fws, fa, fh, human, close_tasks, **charter):
    eid = _epic(fws, fa, fh, human, asked="A page elefant.html that reads elefant.json", **charter)
    data = _child(fws, fa, human, eid, "data", files={"elefant.json": "[1]\n"})
    page = _child(fws, fa, human, eid, "page", files={"elefant.html": "<p>x</p>\n", "elefant.json": "[2]\n"},
                  reqs="Write elefant.html, which reads elefant.json")  # the page child made its own copy
    for c in (data, page):
        _to_testing(fa, c, close_tasks)
    return eid, data, page


def test_two_children_adding_one_file_are_named_at_ready(fws, fa, fh, human, close_tasks, remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, data, page = _two_adds(fws, fa, fh, human, close_tasks, release="merge")
    bt = factory_report.ready(fws, store.load(fws, eid)[1])["coverage"]["built"]
    assert bt["double"] == [{"path": "elefant.json", "children": [data, page]}]
    pytest.importorskip("fastapi")
    from test_dark_dashboard import _client
    html = _client(fws).get(f"/factory/{eid}").text
    assert f"{data} and {page} both add elefant.json: the release will conflict." in html


def test_two_children_adding_one_file_stop_the_release_before_any_merge(fws, fa, fh, human, close_tasks,
                                                                        remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, data, page = _two_adds(fws, fa, fh, human, close_tasks, release="merge")
    fake = Fake()
    lines = fr.tick(fws, human, fake)
    assert not fake.calls, lines  # nothing merged, not even the first child
    reasons = factory_report.stopped(fws, store.load(fws, eid)[1])
    assert [r["code"] for r in reasons] == ["release-blocked"]
    assert (f"{data} and {page} both add elefant.json: the release will conflict; remove it from one child (send "
            "it back) and Retry") in reasons[0]["text"]


def test_two_children_adding_one_file_never_close_by_themselves(fws, fa, fh, human, close_tasks,
                                                                remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, data, page = _two_adds(fws, fa, fh, human, close_tasks, close=True)
    e = store.load(fws, eid)[1]
    texts = [x["text"] for x in factory_close.blockers(fws, e, epics.delegation(fws, e)) if x["code"] == "built"]
    assert f"{data} and {page} both add elefant.json: the release will conflict" in texts
    assert factory_close.tick(fws, human) == [] and store.load(fws, eid)[1].status == "open"


def test_a_merge_conflict_names_its_paths_and_says_retry_will_not_help(fws, fa, fh, human, close_tasks,
                                                                       remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid = _epic(fws, fa, fh, human, asked="A data file elefant.json", release="merge")
    data = _child(fws, fa, human, eid, "data", files={"elefant.json": "[1]\n"})
    _to_testing(fa, data, close_tasks)
    fake = Fake()
    fake.results["pr merge"] = {"code": 1, "out": "Auto-merging elefant.json\nCONFLICT (add/add): Merge conflict "
                                                  "in elefant.json\nAutomatic merge failed\n"}
    fr.tick(fws, human, fake)
    (r,) = factory_report.stopped(fws, store.load(fws, eid)[1])
    assert r["code"] == "release-conflict" and r["label"] == "Merge conflict"
    assert f"the merge of {data} conflicts in elefant.json (add/add): two children changed the same file; this " \
           "will not go away on Retry; send one child back" == r["text"]
    assert fr.conflicts("CONFLICT (content): Merge conflict in a/b.txt\nCONFLICT (modify/delete): c.txt deleted "
                        "in HEAD and modified in x.") == ["a/b.txt (content)",
                                                         "c.txt deleted in HEAD and modified in x. (modify/delete)"]


def test_the_prompts_say_each_file_has_one_maker_and_where_the_verification_file_goes():
    from orch.core import factory_runner
    planner = factory_runner.planner_prompt("L-0001")
    assert "Every file is created by exactly one child" in planner and "make them one child" in planner
    assert "says that it does not create it" in planner and "never require that file in its own commit" in planner
    worker = factory_runner.factory_work_prompt("L-0002", None, clone_tmp="/abs/ws/orchestrator/temporary")
    assert "Do not create a file your ticket says another child creates" in worker
    assert "outside the repository and never commit it" in worker
    assert "/abs/ws/orchestrator/temporary/L-0002-verification.md" in worker and "outside this clone" in worker


def test_a_later_child_adding_a_file_an_earlier_merge_added_is_stopped_too(fws, fa, fh, human, close_tasks,
                                                                           remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid = _epic(fws, fa, fh, human, asked="A page elefant.html that reads elefant.json", release="merge")
    data = _child(fws, fa, human, eid, "data", files={"elefant.json": "[1]\n", "elefant.html": "<p>0</p>\n"})
    _to_testing(fa, data, close_tasks)
    fake = Fake()
    fr.tick(fws, human, fake)
    assert fr.unit_state(fws, eid, "merge", data)["state"] == "proven"
    page = _child(fws, fa, human, eid, "page", files={"elefant.json": "[2]\n"}, reqs="Uses elefant.json")
    _to_testing(fa, page, close_tasks)
    n = len(fake.calls)
    fr.tick(fws, human, fake)
    assert len(fake.calls) == n  # the second merge never starts
    reasons = factory_report.stopped(fws, store.load(fws, eid)[1])
    assert any(f"{data} and {page} both add elefant.json" in r["text"] for r in reasons), reasons
