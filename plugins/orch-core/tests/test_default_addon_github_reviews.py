from pathlib import Path

import pytest
from test_default_addons import recordings

from orch.addons import userfiles
from orch.addons.scheduler import Scheduler
from orch.core.events import read_events
from orch.testing import FakeRunner, fake_workspace

ADDON = Path(__file__).resolve().parents[1] / "addons" / "github-reviews"
ORIGIN = {"origin": "http://testserver"}
PR_LIST = ["gh", "pr", "list", "--repo", "acme/ticket-orch-demo", "--state", "open", "--limit", "100", "--json", "*"]
RERUN = ("gh", "run", "rerun", "36979598989", "--failed", "--repo", "acme/ticket-orch-demo")


@pytest.fixture
def mc(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    fw = fake_workspace(tmp_path / "demo", prefix="DEMO", repos={"acme-energy-data": {"path": "."}},
                        tickets=[{"title": f"Ticket {n}", "status": "in-progress"} for n in range(1, 10)])
    userfiles.set_enabled(fw.root, "github-reviews", True)
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    client = TestClient(create_app(fw.ws, "tok"))
    assert client.get("/?token=tok").status_code == 200
    la = fw.ws.addons.get("github-reviews")
    assert la is not None and la.kind == "default"
    la.ctx.runner = FakeRunner(recordings(ADDON))
    Scheduler(fw.ws, live=lambda: True).step()
    return fw, client, la


def test_page_lists_prs_and_escapes_titles(mc):
    _, client, _ = mc
    html = client.get("/addons/github-reviews/?state=all").text
    assert "<h1>Code reviews</h1>" in html and "#23" in html and "#91" in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'action="/addons/github-reviews/actions/rerun_failed"' in html
    assert 'data-dialog="Rerun the failed checks of acme/ticket-orch-demo#21 ' in html  # names the pull request (GR-04)
    assert "outside orch" not in html and "Asks GitHub to run the failed checks again" in html


def test_menu_today_and_ticket(mc):
    _, client, _ = mc
    html = client.get("/").text
    assert 'href="/addons/github-reviews/"' in html and "Checks failing" in html
    assert "Review requested on Harness (acme-energy-data) #90" in html
    assert "Harness (acme-energy-data) #23" in client.get("/t/DEMO-0009").text


def test_failing_pr_starts_the_agent_in_fix_checks(mc):
    _, client, _ = mc
    html = client.get("/t/DEMO-0003").text
    assert '<option value="fix-checks" selected>' in html
    assert "Fix the failing checks on https://github.com/acme/ticket-orch-demo/pull/21 for ticket DEMO-0003." in html


def test_rerun_failed_is_a_logged_human_post(mc):
    fw, client, la = mc
    la.ctx.runner = FakeRunner([{"argv": list(RERUN)}])
    r = client.post("/addons/github-reviews/actions/rerun_failed", data={"target": "acme/ticket-orch-demo#21"},
                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "Rerun+started" in r.headers["location"] and la.ctx.runner.calls == [RERUN]
    e = [e for e in read_events(fw.ws) if e.kind == "addon.action"][-1]
    assert e.actor == "human:you" and e.data == {"addon": "github-reviews", "action": "rerun_failed",
                                                 "target": "acme/ticket-orch-demo#21"}


def test_actions_need_origin_and_the_cookie(mc):
    from fastapi.testclient import TestClient
    _, client, la = mc
    la.ctx.runner = FakeRunner([])
    url, data = "/addons/github-reviews/actions/mark_ready", {"target": "acme/ticket-orch-demo#22"}
    assert client.post(url, data=data, follow_redirects=False).status_code == 403
    assert TestClient(client.app).post(url, data=data, headers=ORIGIN, follow_redirects=False).status_code == 401
    assert la.ctx.runner.calls == []


def test_auth_required_keeps_rows_and_offers_login(mc):
    fw, client, la = mc
    la.ctx.runner = FakeRunner([
        {"argv": ["git", "-C", "*", "remote", "get-url", "origin"], "stdout": "git@github.com:acme/ticket-orch-demo.git\n"},
        {"argv": PR_LIST, "returncode": 4, "stderr": "To get started with GitHub CLI, please run:  gh auth login"}], strict=False)
    s = Scheduler(fw.ws, live=lambda: False)
    s.request_refresh("github-reviews")
    s.step()
    html = client.get("/addons/github-reviews/?state=all").text
    assert "login needed" in html and 'data-copy="gh auth login"' in html and "#23" in html


def test_disabled_renders_nothing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    fw = fake_workspace(tmp_path / "off")
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(fw.ws, "tok"))
    assert 'href="/addons/github-reviews/"' not in c.get("/?token=tok").text
    assert c.get("/addons/github-reviews/").status_code == 404


def test_repo_card_labels_are_not_visible_text_and_nothing_overflows_with_author(mc):
    """GR-07: "acme: repository" is a screen-reader name, not a visible line; GR-01: no Author column."""
    _, client, _ = mc
    html = client.get("/addons/github-reviews/?state=all").text
    assert 'aria-label="Harness (acme-energy-data): repository"' in html
    assert 'filter-label" aria-hidden="true">Harness (acme-energy-data): repository' not in html
    assert ">Author<" not in html
