"""Dark AI Factory: the files an epic names must be in a child's commit, not only mentioned (the first live run: the
request misspelled the names, the children faithfully built elepthans.json and elpehant.html, and one child left its
file untracked and still counted as proven). Real git and real runner-made clones; a fake for the recipe's commands."""
import shutil

import pytest

from orch.core import epics, factory_built as fb, factory_close, factory_release as fr, factory_report, \
    factory_sessions as fs, store
from test_factory_clones import _g, _msg, _programs, _recipe, _refine, _to_testing, fa, fh, fws, remote, bin_dir  # noqa: F401,E501
from test_factory_release import Fake, _not_stopping  # noqa: F401

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
ASKED = "One page elephants.html that reads elephants.json"


@pytest.fixture(autouse=True)
def _fresh():
    fb._CACHE.clear()
    yield
    fb._CACHE.clear()


def _epic(fws, fa, fh, human, **charter):
    e = fa.new("Elephants", type="epic")
    _refine(fa, e.id, plan=None)
    fa.set_section(e.id, "Requirements", ASKED)
    fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, **charter})
    fs.arm(fws, human, epics.delegation(fws, store.load(fws, e.id)[1])["id"])
    return e.id


def _child(fws, fa, human, eid, title, files=None, untracked=None):
    c = fa.new(title, epic=eid)
    _refine(fa, c.id)
    fa.set_section(c.id, "Requirements", f"Write {' '.join(files or untracked or {})}")
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
