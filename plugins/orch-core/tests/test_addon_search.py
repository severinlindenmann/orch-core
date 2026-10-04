"""R1 (A3 plan Task 1): the Search widget and SlotView.params, as A1 shipped them. A1's own tests in
test_addon_dashboard.py cover caps, core keys, slots and the form; these add what the wiki page relies on."""
import pytest

from addon_fixtures import loaded
from orch.addons.loader import AddonRegistry
from orch.addons.runtime import MAX_PARAM_LEN, MAX_PARAMS, SlotView, clean_params
from orch.addons.widgets import Search, Text, widget_problems

QUOTE_XSS = '"><script>alert(1)</script>'  # breaks out of value="…" unless the attribute is escaped
OVER = {"capabilities": ["provider", "page", "settings"], "menu": {"title": "Demo", "icon": "status"}}


class Finder:
    def __init__(self):
        self.providers, self.seen = [], []

    def widgets(self, slot, view):
        self.seen.append((slot, dict(view.params)))
        q = view.params.get("find", "")
        return [Search("find", q, "title or text"), Text(f"results for {q}")]


@pytest.fixture
def finder(ws):
    obj = Finder()
    ws._addons = AddonRegistry(ws, {"demo": loaded(ws, obj, **OVER)})
    return obj


@pytest.fixture
def client(ws, finder, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def test_search_box_round_trips_the_query_escaped(client, finder):
    r = client.get("/addons/demo/", params={"find": QUOTE_XSS})
    assert r.status_code == 200 and QUOTE_XSS not in r.text and "<script>alert(1)" not in r.text
    assert 'role="search"' in r.text and 'name="find"' in r.text
    assert 'value="&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in r.text
    assert finder.seen[-1] == ("page.demo", {"find": QUOTE_XSS})  # the addon gets the raw value, unescaped


def test_empty_search_shows_an_empty_box(client, finder):
    r = client.get("/addons/demo/")
    assert r.status_code == 200 and 'name="find"' in r.text and 'value=""' in r.text
    assert finder.seen[-1] == ("page.demo", {})


@pytest.mark.parametrize("name", ["Q-1", "1q", "", "q" * 51])
def test_bad_search_names(ws, name):
    m = loaded(ws, Finder(), **OVER).manifest
    assert any(".name" in p for p in widget_problems(Search(name), slot="page.demo", manifest=m))


def test_page_view_without_a_query_has_empty_params(ws):
    la = loaded(ws, Finder(), **OVER)
    assert SlotView(ws, la, "page.demo").params == {}
    assert SlotView(ws, la, "ticket.code", params={"q": "x"}).params == {}


def test_cleaned_params_reach_the_page_view_capped(ws):
    la = loaded(ws, Finder(), **OVER)
    raw = {"q": "x" * 500, **{f"k{i}": "v" for i in range(20)}}
    view = SlotView(ws, la, "page.demo", params=clean_params(raw))
    assert len(view.params) == MAX_PARAMS and view.params["q"] == "x" * MAX_PARAM_LEN
