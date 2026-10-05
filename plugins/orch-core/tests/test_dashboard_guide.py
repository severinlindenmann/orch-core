import re

import pytest

pytest.importorskip("fastapi")


def test_guide_renders_with_key_headings(dash):
    r = dash.get("/guide")
    assert r.status_code == 200
    for heading in ("Agents do the work. You make the calls.", "Where a person has to say yes", "Your day in Mission Control",
                    "Addons that meet the work where it is", "Inside Claude Code", "What you get as a team lead",
                    "Compared with just letting an agent run", "Get started in three steps"):
        assert heading in r.text
    assert "from your phone" in r.text and "<svg" in r.text
    assert "https://" not in r.text and "http://" not in r.text.replace("http://www.w3.org", "")  # no external assets


def test_guide_is_in_the_menu_and_marked_current(dash):
    html = dash.get("/guide").text
    current = re.findall(r'<a[^>]*aria-current="page"[^>]*>(.*?)</a>', html, re.S)
    assert len(current) == 1 and "How it works" in current[0]
    assert 'href="/guide"' in dash.get("/board").text


def test_guide_needs_the_dashboard_token(ws):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    assert TestClient(create_app(ws, "tok")).get("/guide").status_code in (401, 403)


# --- guide.section: an enabled, trusted addon adds its own section -------------------------------------------------

import json  # noqa: E402

from addon_fixtures import GOOD  # noqa: E402

XSS = "<script>alert(1)</script>"
SECTION_FACTORY = f'''
from orch.addons.widgets import Card, Text


class Guide:
    def __init__(self, ctx):
        self.providers = []

    def widgets(self, slot, view):
        if slot != "guide.section":
            return []
        return [Card("Phone fixture section", (Text("Answer from the train. {XSS}"),))]


def create(ctx):
    return Guide(ctx)
'''
GENERIC_PHONE = "A phone companion addon can pair a phone"


@pytest.fixture
def guide_addon(tmp_path, monkeypatch):
    """A custom addon that fills guide.section, installed but neither trusted nor enabled."""
    from orch.addons import discovery, userfiles
    from orch.dashboard.launch import config_dir
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path / "no-defaults")
    folder = config_dir() / "addons" / "hello-status"
    (folder / "hello_status").mkdir(parents=True)
    manifest = {**GOOD, "capabilities": ["panel", "decisions"], "slots": ["guide.section"], "menu": None,
                "remote_humans": True, "settings_schema": []}
    del manifest["menu"]
    (folder / "orch-addon.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "hello_status" / "__init__.py").write_text(SECTION_FACTORY, encoding="utf-8")
    userfiles.record_install("hello-status", source={"kind": "path", "path": str(tmp_path)}, version="0.1.0",
                             requires_api="2", folder=folder)
    return folder


def _guide_html(ws):
    from fastapi.testclient import TestClient
    from orch.addons.loader import AddonRegistry
    from orch.dashboard.app import create_app
    ws._addons = AddonRegistry.load(ws)
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    r = c.get("/guide")
    assert r.status_code == 200
    return r.text


def _trust(folder):
    from orch.addons import userfiles
    from orch.addons.manifest import load_manifest
    userfiles.record_trust("hello-status", folder, load_manifest(folder))


def test_no_addon_section_when_none_is_enabled(dash):
    html = dash.get("/guide").text
    assert "gd-addons" not in html and GENERIC_PHONE in html


def test_enabled_trusted_addon_section_renders_escaped_and_replaces_the_generic_phone_text(ws, guide_addon):
    from orch.addons import userfiles
    _trust(guide_addon)
    userfiles.set_enabled(ws.root, "hello-status", True)
    html = _guide_html(ws)
    assert 'class="gd-addons"' in html and "Phone fixture section" in html and "Answer from the train." in html
    assert XSS not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # addon text is escaped like any widget
    assert GENERIC_PHONE not in html  # a remote_humans addon with a section replaces core's generic paragraph


def test_installed_but_not_enabled_addon_adds_nothing(ws, guide_addon):
    _trust(guide_addon)
    html = _guide_html(ws)
    assert "Phone fixture section" not in html and "gd-addons" not in html and GENERIC_PHONE in html


def test_enabled_but_untrusted_addon_is_not_imported_and_adds_nothing(ws, guide_addon):
    from orch.addons import userfiles
    (guide_addon / "hello_status" / "__init__.py").write_text("raise AssertionError('imported')\n", encoding="utf-8")
    userfiles.set_enabled(ws.root, "hello-status", True)
    html = _guide_html(ws)
    assert "Phone fixture section" not in html and GENERIC_PHONE in html


def test_a_failing_guide_section_never_breaks_the_page(ws, guide_addon):
    from orch.addons import userfiles
    (guide_addon / "hello_status" / "__init__.py").write_text(
        SECTION_FACTORY.replace('return [Card(', 'raise RuntimeError("boom")\n        return [Card('), encoding="utf-8")
    _trust(guide_addon)
    userfiles.set_enabled(ws.root, "hello-status", True)
    html = _guide_html(ws)
    assert "Get started in three steps" in html and "could not render" in html
