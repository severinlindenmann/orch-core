"""Dark AI Factory phase 6 on the dashboard: the ring's Merge and Dev steps (lit only from proven release records),
the run view's release panel and its human-only Retry release, and the "Release up to" choice of a Dark start. A fake
runner stands in for every command: no test runs a real gh, git, merge or deploy."""
import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from orch.core import epics, factory_release as fr, factory_report, store  # noqa: E402
from orch.dashboard.data import factory as factory_data  # noqa: E402
from test_factory_release import RECIPE, Fake, _ready_epic, _refine  # noqa: E402,F401
from test_factory_release import _trusted_programs, fa, fh, fws  # noqa: E402,F401  (fixtures)


def _client(ws):
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _post(c, url, **data):
    return c.post(url, data=data, follow_redirects=False)


def _loc(resp):
    return resp.headers.get("location", "")


def _run(ws, eid):
    return factory_data.run_view(ws, store.load(ws, eid)[1])


def _marks(run):
    return dict(zip(run["names"], run["marks"]))


def test_every_release_reason_says_what_the_human_can_do():
    assert set(fr.RELEASE_CODES) <= set(factory_data._CAN)


def test_ring_gets_merge_and_dev_after_evidence_lit_only_from_proven_records(fws, fa, fh, human, close_tasks):
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks)
    run = _run(fws, eid)
    assert run["names"] == ["Understand", "Plan", "Build", "Evidence", "Merge", "Dev", "Done"]
    assert _marks(run)["Evidence"] == "done" and _marks(run)["Merge"] != "done" and _marks(run)["Dev"] == "todo"
    fr.tick(fws, human, Fake())
    run = _run(fws, eid)
    assert _marks(run)["Merge"] == "done" and _marks(run)["Dev"] == "done" and _marks(run)["Done"] != "done"
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Step 7 of 7" in html and 'pathLength="140"' in html and "--ring-gap: 122" in html


def test_a_failed_or_unproven_stage_lights_nothing(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    fake.results["pr view"] = {"code": 0, "out": "OPEN\n"}  # the check's output is not what the recipe expects
    fr.tick(fws, human, fake)
    run = _run(fws, eid)
    assert _marks(run)["Merge"] == "stop" and _marks(run)["Dev"] == "todo" and run["state"] == "stopped"
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Release stage failed" in html and "Retry release" in html and "Read the stage" in html
    assert "OPEN" in html  # the output tail, under its disclosure


def test_a_charter_without_a_release_keeps_five_steps(fws, fa, fh, human, close_tasks):
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks, release="none")
    run = _run(fws, eid)
    assert run["names"] == list(factory_data.STEPS) and run["release"] is None
    assert "Release up to" not in _client(fws).get(f"/factory/{eid}").text


def test_retry_release_is_the_humans_only(fws, fa, fh, human, close_tasks, monkeypatch):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    fake.results["pr merge"] = {"code": 1}
    fr.tick(fws, human, fake)
    cl = _client(fws)
    data = {"stage": "merge", "unit": c}
    assert cl.post(f"/factory/{eid}/release/retry", data=data, headers={"origin": "http://evil.example"},
                   follow_redirects=False).status_code == 403
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    r = _post(cl, f"/factory/{eid}/release/retry", **data)
    assert "err=" in _loc(r) and "agent+harness" in _loc(r)
    assert factory_report.stopped(fws, store.load(fws, eid)[1])[0]["code"] == "release-failed"
    monkeypatch.delenv("ORCH_HARNESS")
    r = _post(cl, f"/factory/{eid}/release/retry", **data)
    assert "err=" not in _loc(r) and _loc(r).startswith(f"/factory/{eid}")
    assert factory_report.stopped(fws, store.load(fws, eid)[1]) == []
    r = _post(cl, f"/factory/{eid}/release/retry", **data)
    assert "err=" in _loc(r)  # nothing failed now: no second retry


def test_sensitive_stop_shows_the_paths_escaped_and_a_retry(fws, fa, fh, human, close_tasks):
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fr.tick(fws, human, Fake(paths=[".github/<script>x.yml"]))
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Sensitive path touched" in html and "&lt;script&gt;x.yml" in html and "<script>x.yml" not in html
    assert "data-release-sensitive" in html and "Merge by hand" in html


def test_new_ticket_release_choice_needs_a_recipe(fws, human):
    html = _client(fws).get("/new").text
    box = html[html.index("data-release-choice"):html.index("</fieldset>", html.index("data-release-choice"))]
    assert re.search(r'value="merge"[^>]*disabled', box) and re.search(r'value="dev"[^>]*disabled', box)
    assert 'value="none" checked' in box
    assert "Release needs a recipe: run <code>orch factory release set --file recipe.json</code> in a terminal" in box
    fr.set_recipe(fws, human, {"stages": [RECIPE["stages"][0]]})
    box = _client(fws).get("/new").text
    assert not re.search(r'value="merge"[^>]*disabled', box) and re.search(r'value="dev"[^>]*disabled', box)
    assert "Dev needs a dev stage" in box


def _new(c, **over):
    once = re.search(r'name="once" value="([^"]+)"', c.get("/new").text).group(1)
    data = {"title": "Export", "mode": "dark", "ask": "Build the export.", "done_when": "It exports.", "size": "m",
            "priority": "normal", "once": once, "confirm_dark": "dark", **over}
    return _post(c, "/new", **data)


def test_new_ticket_signs_the_release_only_with_a_recipe(fws, human):
    c = _client(fws)
    n = len(list(store.scan(fws)))
    r = _new(c, release="dev")
    assert r.status_code == 422 and "No release can be signed" in r.text and len(list(store.scan(fws))) == n
    r = _new(c, mode="factory", release="merge")
    assert r.status_code == 303 or "err=" not in _loc(r)
    fr.set_recipe(fws, human, RECIPE)
    r = _new(c, release="dev")
    eid = _loc(r).split("/factory/")[1].split("?")[0]
    assert epics.delegation(fws, store.load(fws, eid)[1])["release"] == "dev"


def test_a_plain_factory_start_from_new_ticket_signs_no_release(fws, human):
    fr.set_recipe(fws, human, RECIPE)
    c = _client(fws)
    r = _new(c, mode="factory", release="dev")
    eid = _loc(r).split("/factory/")[1].split("?")[0]
    assert "release" not in epics.delegation(fws, store.load(fws, eid)[1])


def test_epic_page_dark_start_signs_the_release_choice(fws, fa, human):
    fr.set_recipe(fws, human, RECIPE)
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    c = _client(fws)
    html = c.get(f"/t/{e.id}").text
    assert "data-release-choice" in html and "nothing releases to production" in html
    seen = epics.charter(fws, store.load(fws, e.id)[1])["content_hash"]
    r = _post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, start="dark", confirm_dark="dark",
              release="merge")
    assert "err=" not in _loc(r)
    assert epics.delegation(fws, store.load(fws, e.id)[1])["release"] == "merge"


def test_copy_is_truthful(fws, fa, fh, human, close_tasks):
    from pathlib import Path
    import orch
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks)
    pages = [_client(fws).get(u).text for u in ("/new", f"/factory/{eid}", f"/t/{eid}")]
    tpl = Path(orch.__file__).parent / "dashboard" / "templates"
    texts = pages + [(tpl / n).read_text() for n in ("new.html", "ticket.html", "factory_run.html", "_factory.html")]
    for t in texts:
        low = t.lower()
        for claim in ("releases to production by", "deploys to production", "closes the children by itself",
                      "closes children automatically", "there is no release"):
            assert claim not in low, claim
    assert "nothing releases to production" in pages[1].lower()
