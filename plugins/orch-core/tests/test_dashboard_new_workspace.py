import html
import os
import re
import time

import pytest

pytest.importorskip("fastapi")

from orch.core import store


def test_new_form_and_create(dash, ws):
    assert 'name="title"' in dash.get("/new").text
    r = dash.post("/new", data={"title": "Export fails on Mondays", "type": "bug", "size": "s", "priority": "high",
                                "ask": "It fails every Monday."},
                  files=[("files", ("shot.png", b"png", "image/png"))])
    assert r.status_code == 200 and "created L-0001 in backlog" in r.text
    path, t = store.load(ws, "L-0001")
    assert path.parent.name == "backlog" and t.meta["type"] == "bug" and t.meta["priority"] == "high"
    assert t.section("Ask") == "It fails every Monday.\n\n![shot.png](<../../artifacts/L-0001/shot.png>)"
    assert (ws.artifacts_dir / "L-0001" / "shot.png").exists()
    assert 'src="/a/L-0001/shot.png?v=' in r.text  # M: the Artifacts panel shows linked files pinned by their hash


@pytest.mark.parametrize("filename", [
    "shot (1).png",
    "my photo.png",
    "Screenshot 2026-09-30 at 10.23.45.png",
    "a)b.png",
    "weird]name(here.png",
])
def test_new_upload_filename_renders_as_image(dash, ws, filename):
    r = dash.post("/new", data={"title": f"Ticket for {filename}", "ask": "See attached."},
                  files=[("files", (filename, b"png", "image/png"))])
    assert r.status_code == 200
    detail = dash.get("/t/L-0001")
    assert detail.status_code == 200
    assert "<img" in detail.text
    # the served src is percent-encoded; extract it and confirm the artifact route serves it
    m = re.search(r'<img src="(/a/[^"]+)"', detail.text)
    assert m, detail.text
    assert dash.get(m.group(1)).status_code == 200


def test_new_two_uploads_same_name_both_land(dash, ws):
    r = dash.post("/new", data={"title": "Two screenshots", "ask": "Both."},
                  files=[("files", ("image.png", b"one", "image/png")),
                         ("files", ("image.png", b"two", "image/png"))])
    assert r.status_code == 200
    assert (ws.artifacts_dir / "L-0001" / "image.png").read_bytes() == b"one"
    assert (ws.artifacts_dir / "L-0001" / "image-2.png").read_bytes() == b"two"
    _, t = store.load(ws, "L-0001")
    assert "image.png" in t.section("Ask") and "image-2.png" in t.section("Ask")
    assert 'src="/a/L-0001/image.png?v=' in r.text and 'src="/a/L-0001/image-2.png?v=' in r.text


def test_new_upload_failure_after_create_shows_ticket_with_error(dash, ws, monkeypatch):
    from orch.core.ops import Ops
    from orch.errors import ValidationError

    def boom(self, ref, file, name=None):
        raise ValidationError("disk full")

    monkeypatch.setattr(Ops, "artifact_add", boom)
    r = dash.post("/new", data={"title": "Will fail to attach", "ask": "x"},
                  files=[("files", ("shot.png", b"png", "image/png"))])
    assert r.status_code == 200
    assert "created L-0001, but attaching files failed" in r.text and "disk full" in r.text
    # exactly one ticket exists — no duplicate was created on this single submission
    assert [e.id for e in store.scan(ws)] == ["L-0001"]


def test_new_without_files(dash, ws):
    r = dash.post("/new", data={"title": "Plain ticket", "ask": ""})
    assert r.status_code == 200 and store.load(ws, "L-0001")[1].section("Ask") == ""


def test_new_validation_error_keeps_values(dash):
    r = dash.post("/new", data={"title": "   ", "ask": "keep me"})
    assert r.status_code == 422 and "title must not be empty" in r.text and "keep me" in r.text


def test_workspace_page(dash, ws, put):
    put("open")  # open without approved requirements → a check finding
    (ws.static_dir / "scripts").mkdir()
    (ws.static_dir / "scripts" / "export.sql").write_text("select 1", encoding="utf-8")
    r = dash.get("/workspace")
    assert r.status_code == 200
    assert "status-without-gate" in r.text and "scripts/export.sql" in r.text
    assert '"customer": "acme"' in html.unescape(r.text)


def test_workspace_config_is_escaped(configure):
    """config.json values must render HTML-escaped in /workspace (autoescaping stays on)."""
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app

    ws = configure(customer="<script>x</script>")
    client = TestClient(create_app(ws, "tok"))
    assert client.get("/?token=tok").status_code == 200
    r = client.get("/workspace")
    assert r.status_code == 200
    assert "<script>x</script>" not in r.text
    assert "&lt;script&gt;x&lt;/script&gt;" in r.text


def test_tidy_button(dash, ws):
    old = ws.temporary_dir / "old.log"
    old.write_text("x", encoding="utf-8")
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    r = dash.post("/workspace/tidy")
    assert r.status_code == 200 and "removed 1 old file(s)" in r.text and not old.exists()


def test_workspace_static_listing_says_how_many_are_hidden(dash, ws):
    ws.static_dir.mkdir(parents=True, exist_ok=True)
    for i in range(503):
        (ws.static_dir / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
    body = dash.get("/workspace").text
    assert "f0499.txt" in body and "f0500.txt" not in body and "and 3 more" in body
