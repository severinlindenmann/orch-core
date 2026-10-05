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
