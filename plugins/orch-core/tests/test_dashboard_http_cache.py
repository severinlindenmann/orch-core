"""Compression and caching headers: gzip for pages and assets, immutable versioned assets, revalidated HTML."""
import re

import pytest

pytest.importorskip("fastapi")


def test_pages_are_gzipped(dash, put):
    put("open", title="Something to show")
    r = dash.get("/", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
    assert "<html" in r.text  # httpx decodes it


def test_live_stream_and_artifacts_are_never_gzipped(dash, ws, put):
    r = dash.get("/events?timeout=0.1", headers={"accept-encoding": "gzip"})
    assert "content-encoding" not in r.headers
    tid = put("open")
    d = ws.artifacts_dir / tid
    d.mkdir(parents=True)
    (d / "notes.txt").write_text("x" * 5000, encoding="utf-8")
    r = dash.get(f"/a/{tid}/notes.txt", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200 and "content-encoding" not in r.headers


def test_layout_links_versioned_assets_that_are_immutable(dash):
    html = dash.get("/").text
    urls = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
    for name in ("app.css", "tasks.css", "app.js", "early.js"):
        assert any(re.fullmatch(rf"/static/{re.escape(name)}\?v=[0-9a-f]{{12}}", u) for u in urls), name
    css = next(u for u in urls if u.startswith("/static/app.css?v="))
    r = dash.get(css)
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers.get("content-encoding") == "gzip"


def test_unversioned_or_outdated_asset_urls_are_cached_briefly(dash):
    for url in ("/static/app.css", "/static/app.css?v=000000000000"):
        r = dash.get(url)
        assert r.status_code == 200 and "immutable" not in r.headers["cache-control"]
        assert "max-age=" in r.headers["cache-control"]


def test_asset_version_follows_the_file(tmp_path, monkeypatch):
    from orch.dashboard import assets
    monkeypatch.setattr(assets, "STATIC_DIR", tmp_path)
    (tmp_path / "x.css").write_text("a{}", encoding="utf-8")
    first = assets.static_url("x.css")
    (tmp_path / "x.css").write_text("a{color:red}", encoding="utf-8")
    second = assets.static_url("x.css")
    assert first != second and second.startswith("/static/x.css?v=")
    assert assets.static_url("missing.css") == "/static/missing.css"


def test_html_is_revalidated_with_an_etag(dash, put):
    put("open")
    r = dash.get("/board")
    assert r.headers["cache-control"] == "no-cache"
    etag = r.headers["etag"]
    again = dash.get("/board", headers={"if-none-match": etag})
    assert again.status_code == 304 and again.content == b""
