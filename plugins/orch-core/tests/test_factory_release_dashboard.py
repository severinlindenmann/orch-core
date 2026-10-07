"""Dark AI Factory phase 6 on the dashboard: the ring's Merge and Dev steps (lit only from proven release records),
the run view's release panel and its human-only Retry release, and the "Release up to" choice of a Dark start. A fake
runner stands in for every command: no test runs a real gh, git, merge or deploy."""
import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from orch.core import epics, factory_release as fr, factory_report, store  # noqa: E402
from orch.dashboard.data import factory as factory_data  # noqa: E402
from test_factory_release import MERGE, Fake, _refine  # noqa: E402,F401
from test_factory_release import _not_stopping, bin_dir, fa, fh, fws, ready, recipe, remote  # noqa: E402,F401


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


def test_ring_gets_merge_and_dev_after_evidence_lit_only_from_proven_records(fws, ready, human):
    eid, _, _ = ready()
    run = _run(fws, eid)
    assert run["names"] == ["Understand", "Plan", "Build", "Evidence", "Merge", "Dev", "Done"]
    assert _marks(run)["Evidence"] == "done" and _marks(run)["Merge"] != "done" and _marks(run)["Dev"] == "todo"
    fr.tick(fws, human, Fake())
    run = _run(fws, eid)
    assert _marks(run)["Merge"] == "done" and _marks(run)["Dev"] == "done" and _marks(run)["Done"] != "done"
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Step 7 of 7" in html and 'pathLength="140"' in html and "--ring-gap: 122" in html


def test_a_failed_or_unproven_stage_lights_nothing(fws, ready, human):
    eid, (c,), _ = ready()
    fake = Fake()
    fake.results["pr view"] = {"code": 0, "out": "OPEN\n"}  # the check's output is not what the recipe expects
    fr.tick(fws, human, fake)
    run = _run(fws, eid)
    assert _marks(run)["Merge"] == "stop" and _marks(run)["Dev"] == "todo" and run["state"] == "stopped"
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Release stage failed" in html and "Retry release" in html and "Read the stage" in html
    assert "OPEN" in html  # the output tail, under its disclosure


def test_a_charter_without_a_release_keeps_five_steps(fws, ready, human):
    eid, _, _ = ready(release="none")
    run = _run(fws, eid)
    assert run["names"] == list(factory_data.STEPS) and run["release"] is None
    assert "Release up to" not in _client(fws).get(f"/factory/{eid}").text


def test_retry_release_is_the_humans_only(fws, ready, human, monkeypatch):
    eid, (c,), _ = ready()
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


def test_sensitive_stop_shows_the_paths_escaped_and_a_retry(fws, ready, human):
    eid, _, _ = ready(files={".github/<script>x.yml": "x\n"})
    fr.tick(fws, human, Fake())
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Sensitive path touched" in html and "&lt;script&gt;x.yml" in html and "<script>x.yml" not in html
    assert "data-release-sensitive" in html and "merge by hand" in html


def test_new_ticket_release_choice_needs_a_recipe(fws, human, recipe):
    html = _client(fws).get("/new").text
    box = html[html.index("data-release-choice"):html.index("</fieldset>", html.index("data-release-choice"))]
    assert re.search(r'value="merge"[^>]*disabled', box) and re.search(r'value="dev"[^>]*disabled', box)
    assert 'value="none" checked' in box
    assert "Release needs a recipe: run <code>orch factory release set --file recipe.json</code> in a terminal" in box
    fr.set_recipe(fws, human, {**recipe, "stages": [MERGE]})
    box = _client(fws).get("/new").text
    assert not re.search(r'value="merge"[^>]*disabled', box) and re.search(r'value="dev"[^>]*disabled', box)
    assert "Dev needs a dev stage" in box


def _new(c, **over):
    once = re.search(r'name="once" value="([^"]+)"', c.get("/new").text).group(1)
    data = {"title": "Export", "mode": "dark", "ask": "Build the export.", "done_when": "It exports.", "size": "m",
            "priority": "normal", "once": once, "confirm_dark": "dark", **over}
    return _post(c, "/new", **data)


def test_new_ticket_signs_the_release_only_with_a_recipe(fws, human, recipe):
    c = _client(fws)
    n = len(list(store.scan(fws)))
    r = _new(c, release="dev")
    assert r.status_code == 422 and "No release can be signed" in r.text and len(list(store.scan(fws))) == n
    r = _new(c, mode="factory", release="merge")
    assert r.status_code == 422 and "Only a Dark AI Factory signs a release" in r.text
    fr.set_recipe(fws, human, recipe)
    r = _new(c, release="dev")
    eid = _loc(r).split("/factory/")[1].split("?")[0]
    assert epics.delegation(fws, store.load(fws, eid)[1])["release"] == "dev"


def test_a_plain_factory_start_from_new_ticket_signs_no_release(fws, human, recipe):
    fr.set_recipe(fws, human, recipe)
    c = _client(fws)
    n = len(list(store.scan(fws)))
    r = _new(c, mode="factory", release="dev")  # posted in AI Factory mode: refused, never silently dropped
    assert r.status_code == 422 and "Only a Dark AI Factory signs a release: nothing was created." in r.text
    assert len(list(store.scan(fws))) == n
    r = _new(c, mode="factory", release="none")
    eid = _loc(r).split("/factory/")[1].split("?")[0]
    assert "release" not in epics.delegation(fws, store.load(fws, eid)[1])


def test_an_epic_page_ai_factory_start_refuses_a_posted_release(fws, fa, human, recipe):
    fr.set_recipe(fws, human, recipe)
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    c = _client(fws)
    seen = epics.charter(fws, store.load(fws, e.id)[1])["content_hash"]
    r = _post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, start="factory", release="dev")
    assert "only+a+Dark+AI+Factory+signs+a+release" in _loc(r).replace("%20", "+")
    assert epics.delegation(fws, store.load(fws, e.id)[1]) is None
    r = _post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, start="factory", release="none")
    assert "err=" not in _loc(r) and "release" not in epics.delegation(fws, store.load(fws, e.id)[1])


def test_epic_page_dark_start_signs_the_release_choice(fws, fa, human, recipe):
    fr.set_recipe(fws, human, recipe)
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    c = _client(fws)
    html = c.get(f"/t/{e.id}").text
    assert "data-release-choice" in html and "never before its release window opens" in html
    seen = epics.charter(fws, store.load(fws, e.id)[1])["content_hash"]
    r = _post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, start="dark", confirm_dark="dark",
              release="merge")
    assert "err=" not in _loc(r)
    assert epics.delegation(fws, store.load(fws, e.id)[1])["release"] == "merge"


def test_copy_is_truthful(fws, ready, human):
    from pathlib import Path
    import orch
    eid, _, _ = ready()
    pages = [_client(fws).get(u).text for u in ("/new", f"/factory/{eid}", f"/t/{eid}")]
    tpl = Path(orch.__file__).parent / "dashboard" / "templates"
    texts = pages + [(tpl / n).read_text() for n in ("new.html", "ticket.html", "factory_run.html", "_factory.html")]
    for t in texts:
        low = t.lower()
        for claim in ("deploys to production", "closes the children by itself", "closes children automatically",
                      "there is no release", "skips the release window", "production is not built"):
            assert claim not in low, claim
    assert "nothing releases to production" in pages[1].lower()  # this run signs dev, not production
    assert "never before its release window opens" in pages[0]  # the production choice says when it may run


def test_an_out_of_date_stage_is_shown_stale_not_proven(fws, ready, human):
    from test_factory_release import _branch
    eid, (c,), _ = ready()
    fr.tick(fws, human, Fake())
    assert _marks(_run(fws, eid))["Merge"] == "done"
    _branch(fws.root, f"feat/{c.lower()}-work", {"src/more.py": "x\n"}, start=f"feat/{c.lower()}-work")
    fr.tick(fws, human, Fake())
    run = _run(fws, eid)
    assert _marks(run)["Merge"] != "done" and _marks(run)["Dev"] != "done" and run["state"] == "stopped"
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Release out of date" in html and "out of date" in html and "Retry release" in html


def test_the_docs_example_recipe_is_a_valid_recipe(ws):
    from pathlib import Path
    import json as _json
    import orch
    doc = (Path(orch.__file__).parents[2] / "docs" / "factory.md").read_text(encoding="utf-8")
    block = doc[doc.index("**Example** (an example only"):]
    raw = block[block.index("```json") + 7:block.index("```", block.index("```json") + 7)]
    rec = fr.check_recipe(_json.loads(raw), ws, check_programs=False)
    merge = rec["stages"][0]
    assert merge["check"]["expect"] == "{base} {sha} MERGED" and merge["precheck"]["expect"] == "0"
    assert fr.sensitive("app/migrations/1.sql", rec["sensitive_paths"]) and fr.sensitive("migrations/1.sql",
                                                                                          rec["sensitive_paths"])
    assert fr.sensitive("deploy/k8s/app.yml", rec["sensitive_paths"])
    prod = rec["stages"][2]
    assert prod["name"] == "production" and prod["window_hours"] == 20 and prod["rollback"]["check"]["expect"] == "ok"
    assert prod["check"]["expect"] == "{sha}"
