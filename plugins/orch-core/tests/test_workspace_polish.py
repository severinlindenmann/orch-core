"""F: Workspace polish (visual review top-10 #8) and the density switch: tabs (Addons default / Setup / Phones /
Advanced), addon cards with a real switch, where they show up, what they need and their settings in a disclosure (labels above fields), the raw config
in <details>, relative repository paths, orch check levels as statuses, and comfortable/compact on the page root."""
import re

import pytest

from addon_fixtures import GOOD, make_addon
from orch.addons import discovery, userfiles

pytest.importorskip("fastapi")

ORIGIN = {"origin": "http://testserver"}
SETTINGS = {**GOOD, "name": "alpha", "title": "Alpha",
            "settings_schema": [{"key": "repo", "label": "Repository to watch", "type": "text"}]}


@pytest.fixture
def client(ws, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    defaults = tmp_path / "defaults"
    make_addon(defaults, manifest=SETTINGS).rename(defaults / "alpha")
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: defaults)
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _panels(html):
    return dict(re.findall(r'<div class="ws-panel" id="tab-(\w+)" data-tab-panel="\w+"( hidden)?>', html))


def test_addons_is_the_default_tab_and_the_others_are_hidden(client):
    html = client.get("/workspace").text
    assert _panels(html) == {"addons": "", "setup": " hidden", "widgets": " hidden", "phones": " hidden",
                                "remote": " hidden", "advanced": " hidden"}
    tabs = html[html.index('<nav class="tabs ws-tabs"'):html.index("</nav>", html.index('<nav class="tabs ws-tabs"'))]
    assert re.findall(r'data-tab="(\w+)"', tabs) == ["addons", "setup", "widgets", "phones", "remote", "advanced"]
    assert 'href="/workspace" class="on" data-tab="addons" aria-current="true"' in tabs
    assert _panels(client.get("/workspace?tab=advanced").text)["advanced"] == ""
    assert _panels(client.get("/workspace?tab=bogus").text)["addons"] == ""


def test_panels_hold_their_sections(client):
    html = client.get("/workspace").text
    def panel(name):
        start = html.index(f'id="tab-{name}"')
        return html[start:html.find('<div class="ws-panel"', start + 10)]
    assert 'id="addons"' in panel("addons")
    assert "Setup checks" in panel("setup") and "Repositories" in panel("setup") and 'id="shortcuts"' in panel("setup")
    assert 'id="density"' in panel("setup")
    adv = panel("advanced")
    assert "orch check" in adv and "Housekeeping" in adv and 'href="/design"' in adv
    assert '<details class="config-raw"><summary>Show config.json</summary><pre>' in adv
    assert "static/ <span" not in html  # an empty static/ is not shown


def test_redirects_land_on_their_tab(client, ws):
    r = client.post("/workspace/shortcuts", data={"on": "0"}, headers=ORIGIN, follow_redirects=False)
    assert r.headers["location"].startswith("/workspace?tab=setup")
    js = (__import__("pathlib").Path(__file__).resolve().parents[1] / "src/orch/dashboard/static/app.js").read_text()
    assert 'closest("[data-tab-panel][hidden]")' in js  # a #fragment in a hidden panel opens it


def _card(html, name):
    card = html[html.index(f'<li class="addon-item" id="addon-{name}"'):]
    return card[:card.index("</section>")]


def test_addon_card_has_a_switch_and_its_settings_in_a_disclosure(client, ws):
    card = _card(client.get("/workspace").text, "alpha")
    assert re.search(r'<button type="submit" class="switch" role="switch" aria-checked="false"><span class="switch-track" '
                     r'aria-hidden="true"></span><span class="sr-only">Alpha enabled here</span><span aria-hidden="true">Off</span></button>', card)
    assert "turn on" not in card and "turn off" not in card
    settings = card[card.index('<details class="addon-settings"'):]
    assert '<summary class="btn btn-quiet">Settings <span class="muted">(1)</span></summary>' in settings
    assert '<label class="field-col">Repository to watch <input type="text" name="repo"' in settings
    assert '<button type="submit" form="settings-form-alpha" class="btn btn-primary">Save settings</button>' in settings
    client.post("/workspace/addons/alpha/enable", data={"enabled": "1"}, headers=ORIGIN)
    html = client.get("/workspace").text
    assert 'role="switch" aria-checked="true"' in html
    assert html.index('id="addons-on-h"') < html.index('id="addon-alpha"')  # an addon that is on moves to "On"


def test_addon_card_says_what_it_does_where_it_shows_up_and_what_it_needs(client, ws, monkeypatch):
    from orch.dashboard import launch
    monkeypatch.setattr(launch, "which", lambda b: None)  # git missing
    card = _card(client.get("/workspace").text, "alpha")
    assert '<p class="addon-desc">Example</p>' in card
    assert "Its own page in the menu: Hello status" in card
    assert '<rect class="am-zone is-on" x="3"' in card  # the preview map fills the menu
    assert '<span class="req-text">git not found</span><span class="req-hint">install git' in card
    html = client.get("/workspace").text
    assert "1 addon needs a look" not in html  # missing tools matter only once it is on
    client.post("/workspace/addons/alpha/enable", data={"enabled": "1"}, headers=ORIGIN)
    html = client.get("/workspace").text
    assert '1 addon needs a look' in html and '<a class="lnk" href="#addon-alpha">Alpha</a>' in html
    assert 'data-attention="1"' in _card(html, "alpha")
    monkeypatch.setattr(launch, "which", lambda b: "/usr/bin/" + b)
    assert '<span class="req-text">git installed</span>' in _card(client.get("/workspace").text, "alpha")


def test_addon_filter_is_hidden_until_js_runs(client, ws):
    html = client.get("/workspace").text
    assert '<form class="addons-filter" data-addon-filter role="search" aria-label="Filter addons" hidden>' in html
    js = (__import__("pathlib").Path(__file__).resolve().parents[1] / "src/orch/dashboard/static/app.js").read_text()
    assert 'form[data-addon-filter]' in js and "form.hidden = false" in js


def test_surfaces_and_requirements_from_the_manifest(monkeypatch):
    from orch.addons.manifest import parse_manifest
    from orch.dashboard import addon_catalog, launch
    monkeypatch.setattr(launch, "which", lambda b: "/bin/" + b)
    m = parse_manifest({**GOOD, "menu": None, "capabilities": ["provider", "panel", "decisions", "launch", "settings"],
                        "slots": ["today.summary", "ticket.code"], "binaries": [], "env": ["GH_TOKEN"]})
    assert [s["label"] for s in addon_catalog.surfaces("x", m)] == [
        "A tile on Today", "Ticket page, Code", "Questions for you on Today", "Start agent: how sessions start"]
    assert addon_catalog.requirements(m, trust="trusted") == [{"role": "neu", "text": "Reads GH_TOKEN when set", "hint": None}]
    bare = parse_manifest({**GOOD, "binaries": [], "env": []})
    assert addon_catalog.requirements(bare, trust="trusted") == [{"role": "ok", "text": "Nothing to install", "hint": None}]
    assert addon_catalog.requirements(bare, trust="untrusted")[0]["text"] == "Trust this version first"
    assert addon_catalog.surfaces("graph", parse_manifest({**GOOD, "menu": None, "capabilities": [], "settings_schema": []}))[0]["label"] == \
        "The Graph page in the menu"
    assert addon_catalog.surfaces("schedules", parse_manifest({**GOOD, "menu": None, "capabilities": [], "settings_schema": []}))[0]["label"] == \
        "The Schedules page in the menu"


def test_repositories_show_relative_paths(tmp_path):
    from orch.dashboard.routes_workspace import _relative
    root = tmp_path / "ws"
    assert _relative(root / "ingest", root) == "./ingest"
    assert _relative(tmp_path / "shared", root) == "../shared"
    assert _relative(root, root) == "."
    tpl = (__import__("pathlib").Path(__file__).resolve().parents[1] / "src/orch/dashboard/templates/workspace.html").read_text()
    assert '<span class="path" title="{{ r.path }}">{{ r.rel }}</span>' in tpl
    assert 'ui.status("warn", "commit check " ~ r.hook)' in tpl  # says what is missing


def test_check_levels_are_statuses(client, ws, put):
    from orch.dashboard import setup_state
    html = client.get("/workspace?tab=advanced").text
    adv = html[html.index('id="tab-advanced"'):]
    for level in re.findall(r'<td class="no-prefix" data-label="Level">(.*?)</td>', adv):
        assert re.match(r'<span class="chip chip-(err|warn|neu)"><svg class="i"', level)


def test_density_is_a_workspace_setting_on_the_page_root(client, ws):
    assert 'data-density="comfortable"' in client.get("/").text
    r = client.post("/workspace/density", data={"density": "compact"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/workspace?tab=setup")
    assert userfiles.density(ws.root) == "compact"
    assert 'data-density="compact"' in client.get("/board").text
    html = client.get("/workspace?tab=setup").text
    assert '<button type="submit" name="density" value="compact" class="btn" aria-pressed="true">' in html
    client.post("/workspace/density", data={"density": "huge"}, headers=ORIGIN)
    assert userfiles.density(ws.root) == "compact"  # unknown values change nothing
    assert client.post("/workspace/density", data={"density": "comfortable"}).status_code == 403  # same origin only
    assert userfiles.density(ws.root) == "compact"


def test_the_version_is_in_the_menu_and_the_setup_tab_has_an_about_card(client):
    import orch
    html = client.get("/workspace?tab=setup").text
    assert f"orch {orch.__version__}</a>" in html  # the menu and the page head link to the card
    card = html.split('id="about"', 1)[1].split("</section>", 1)[0]
    assert f"<b>{orch.__version__}</b>" in card and "Installed as" in card and "<code>orch update</code>" in card


def test_density_and_motion_tokens_are_honoured():
    from pathlib import Path
    static = Path(__file__).resolve().parents[1] / "src/orch/dashboard/static"
    tokens = (static / "tokens.css").read_text(encoding="utf-8")
    assert "[data-density=compact] {" in tokens and "--dur-1: 0ms;" in tokens
    css = (static / "app.css").read_text(encoding="utf-8")
    assert "animation: drawer-in var(--dur-3)" in css and "transition: transform var(--dur-2)" in css
    assert re.search(r"@media \(prefers-reduced-motion: reduce\) \{[^@]*animation: none", css)



EXTERNAL = {"addons": [
    {"name": "far-away", "title": "Far away", "description": "Lives in its own repository.",
     "repo": "https://github.com/example/far-away", "path": "addons/far-away", "needs": ["An account"],
     "adds": {"capabilities": ["provider", "page", "panel"], "slots": ["ticket.sync"], "menu": {"title": "Far", "icon": "share"},
              "remote_humans": True}},
    {"name": "Bad Name", "title": "x", "description": "x", "repo": "https://github.com/example/x"},
    {"name": "no-https", "title": "x", "description": "x", "repo": "http://example.com/x"},
    {"name": "climbs", "title": "x", "description": "x", "repo": "https://github.com/example/x", "path": "../etc"},
]}


def test_more_addons_lists_external_ones_with_their_install_commands(client, ws):
    import json
    from orch.addons.discovery import default_addons_dir
    (default_addons_dir() / "external.json").write_text(json.dumps(EXTERNAL), encoding="utf-8")
    html = client.get("/workspace").text
    more = html[html.index('id="addons-more"'):]
    assert '<li class="addon-item addon-external" id="more-far-away"' in more
    cmd = "orch addon install https://github.com/example/far-away --path addons/far-away"
    assert f'<code>{cmd}</code><button type="button" class="btn btn-quiet copy-fix" data-copy="{cmd}">Copy</button>' in more
    assert "Its own page in the menu: Far" in more and "Ticket page, Sync" in more and "Pairs phones (Phones tab)" in more
    assert "An account" in more and "climbs" not in more and "no-https" not in more and "Bad Name" not in more
    assert "<form" not in more[:more.index('class="addon-own"')]  # nothing installs from the page


def test_an_installed_or_unreadable_external_list_shows_nothing(tmp_path, monkeypatch):
    import json
    from orch.dashboard import addon_catalog
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path)
    assert addon_catalog.external(set()) == []  # no file
    (tmp_path / "external.json").write_text("{not json", encoding="utf-8")
    assert addon_catalog.external(set()) == []
    (tmp_path / "external.json").write_text(json.dumps(EXTERNAL), encoding="utf-8")
    assert [e["name"] for e in addon_catalog.external(set())] == ["far-away"]
    assert addon_catalog.external({"far-away"}) == []


def test_the_shipped_external_list_is_well_formed():
    import json
    from pathlib import Path
    data = json.loads((Path(__file__).resolve().parents[1] / "addons" / "external.json").read_text(encoding="utf-8"))
    from orch.dashboard.addon_catalog import _entry
    assert data["addons"] and all(_entry(e) is not None for e in data["addons"])
