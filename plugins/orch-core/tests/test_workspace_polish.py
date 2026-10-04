"""F: Workspace polish (visual review top-10 #8) and the density switch: tabs (Addons default / Setup / Phones /
Advanced), addon rows with a real switch and their settings in a disclosure (labels above fields), the raw config
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
    assert _panels(html) == {"addons": "", "setup": " hidden", "phones": " hidden", "advanced": " hidden"}
    tabs = html[html.index('<nav class="tabs ws-tabs"'):html.index("</nav>", html.index('<nav class="tabs ws-tabs"'))]
    assert re.findall(r'data-tab="(\w+)"', tabs) == ["addons", "setup", "phones", "advanced"]
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


def test_addon_row_has_a_switch_and_its_settings_in_a_disclosure(client, ws):
    html = client.get("/workspace").text
    row = html[html.index('<li class="addon-row" id="addon-alpha">'):]
    row = row[:row.index("</li>")]
    assert re.search(r'<button type="submit" class="switch" role="switch" aria-checked="false"><span class="switch-track" '
                     r'aria-hidden="true"></span>Enabled here</button>', row)
    assert "turn on" not in row and "turn off" not in row
    settings = row[row.index('<details class="addon-settings"'):]
    assert '<summary class="btn btn-quiet">Settings</summary>' in settings
    assert '<label class="field-col">Repository to watch <input type="text" name="repo"' in settings
    assert '<button type="submit" class="btn btn-primary">Save settings</button>' in settings
    client.post("/workspace/addons/alpha/enable", data={"enabled": "1"}, headers=ORIGIN)
    assert 'role="switch" aria-checked="true"' in client.get("/workspace").text


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


def test_density_and_motion_tokens_are_honoured():
    from pathlib import Path
    static = Path(__file__).resolve().parents[1] / "src/orch/dashboard/static"
    tokens = (static / "tokens.css").read_text(encoding="utf-8")
    assert "[data-density=compact] {" in tokens and "--dur-1: 0ms;" in tokens
    css = (static / "app.css").read_text(encoding="utf-8")
    assert "animation: drawer-in var(--dur-3)" in css and "transition: transform var(--dur-2)" in css
    assert re.search(r"@media \(prefers-reduced-motion: reduce\) \{[^@]*animation: none", css)
