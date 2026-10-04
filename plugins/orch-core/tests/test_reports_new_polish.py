"""F: Reports and New ticket per the visual review: no false precision (durations and sample sizes), neighbouring
status segments that stay apart, a headed By type card, and New ticket copy with the workspace's real prefix."""
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

CSS = Path(__file__).resolve().parents[1] / "src" / "orch" / "dashboard" / "static" / "app.css"


@pytest.mark.parametrize("hours, text", [(None, "–"), (0.0, "< 1 h"), (0.4, "< 1 h"), (5.4, "5 h"), (47.0, "47 h"),
                                         (60.0, "2.5 d")])
def test_duration_has_no_false_precision(hours, text):
    from orch.dashboard.data.metrics import duration
    assert duration(hours) == text


def test_reports_name_the_sample_and_never_show_zero_point_zero(dash):
    html = dash.get("/reports").text
    assert "0.0 d" not in html and "0.0 h" not in html
    assert "no ticket done in this period" in html and "no question answered in this period" in html
    assert '<h2 id="by-type-h">By type</h2>' in html
    assert "Open as Markdown" in html and "Copy as Markdown" not in html


def test_neighbouring_status_segments_differ(dash, put):
    put("backlog"); put("open"); put("in-progress"); put("testing", sections={"Verification": "ok"})
    html = dash.get("/reports").text
    assert "dist-seg-neu dist-backlog" in html and "dist-seg-info dist-testing" in html
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.dist-seg\.dist-backlog \{ background: var\(--line2\); \}", css)
    assert re.search(r"\.dist-seg\.dist-testing \{[^}]*outline: 2px solid var\(--info-mark\)", css)


def test_new_ticket_copy_uses_the_workspace_prefix(configure):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    ws = configure(id={"prefix": "ACME", "pad": 4})
    client = TestClient(create_app(ws, "tok"))
    client.get("/?token=tok")
    html = client.get("/new").text
    assert '"refine ACME-00xx"' in html and "L-00xx" not in html
