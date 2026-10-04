"""D: every search box filters as you type (app.js data-live-search, driven by tests/js/live_search.js): 200 ms after
the last key, only the named regions are swapped, the box keeps focus and caret; Enter and plain GET still work."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_live_search_behaviour_in_app_js():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "live_search.js"), str(ROOT / "src/orch/dashboard/static/app.js")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "live search ok" in r.stdout


def test_the_board_search_is_live_and_names_the_regions_it_swaps(dash, put):
    put("open", title="Export as CSV")
    html = dash.get("/board").text
    form = re.search(r'<form class="filters"[^>]*>', html).group(0)
    assert 'data-live-search="#board-tabs, #board-results"' in form and 'method="get"' in form
    assert 'id="board-tabs"' in html and 'id="board-results"' in html
    # the box itself is outside the swapped regions, so it is never replaced while typing
    assert html.index('name="q"') < html.index('id="board-results"')
    assert html.index('id="board-tabs"') < html.index('name="q"')


def test_the_list_view_search_is_live_too(dash, put):
    html = dash.get("/board?view=list").text
    assert 'data-live-search="#board-tabs, #board-results"' in html


def test_the_search_still_works_as_a_plain_get(dash, put):
    put("open", title="Export as CSV")
    put("open", title="Something else")
    results = dash.get("/board?q=export").text.split('id="board-results"', 1)[1]
    assert "Export as CSV" in results and "Something else" not in results


def test_the_addon_search_widget_is_live(ws, monkeypatch):
    from fastapi.testclient import TestClient
    from addon_fixtures import loaded
    from orch.addons.loader import AddonRegistry
    from orch.addons.widgets import Search, Text
    from orch.dashboard import views
    from orch.dashboard.app import create_app

    class Finder:
        def widgets(self, slot, view):
            return [Search("find", view.params.get("find", ""), "title"), Text("hits")]
    ws._addons = AddonRegistry(ws, {"demo": loaded(ws, Finder(), capabilities=["provider", "page", "settings"],
                                                   menu={"title": "Demo", "icon": "status"})})
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    html = c.get("/addons/demo/").text
    assert 'data-live-search="#addon-page"' in html and 'id="addon-page"' in html
