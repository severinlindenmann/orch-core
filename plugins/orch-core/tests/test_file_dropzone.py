"""The file drop zone (static/files.js): drop, Choose files and screenshots pasted into the Ask box, each file listed
with a thumbnail, its size and Remove, the server's per-file limit named and checked before sending. Without JS the
native file input works as before, and the server's handling and limits are unchanged."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from orch.core import artifacts, store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "orch" / "dashboard" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_file_dropzone_js():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "file_dropzone.js"), str(STATIC / "files.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "file dropzone ok" in r.stdout


def test_new_ticket_says_screenshots_can_be_pasted_and_works_without_js(dash, ws):
    html = dash.get("/new").text
    form = html.split('action="/new" enctype="multipart/form-data"', 1)[1].split("</form>", 1)[0]
    # the hint under Ask, tied to the field (shown with JS only: pasting needs files.js)
    assert re.search(r'<textarea id="ask" name="ask"[^>]*aria-describedby="ask-paste-hint"', form)
    assert '<span class="muted js-only" id="ask-paste-hint" data-paste-hint>You can paste screenshots here' in form
    assert "html:not(.js) .js-only { display: none; }" in (STATIC / "files.css").read_text(encoding="utf-8")
    # the no-JS fallback is the native input itself, posting name=files, several at once; files.js enhances it in place
    limit = artifacts.max_bytes(ws)
    assert f'<input id="files" type="file" name="files" multiple data-dropzone data-paste-from="ask" data-max-bytes="{limit}">' in form
    assert limit == 50 * 1024 * 1024 and "Up to 50 MB per file." in form
    layout = (ROOT / "src" / "orch" / "dashboard" / "templates" / "layout.html").read_text(encoding="utf-8")
    assert "static_url('files.js') }}\" defer" in layout and "static_url('files.css')" in layout


def test_ticket_upload_has_the_zone_and_the_configured_limit(configure):
    ws = configure(artifacts={"max_mb": 2.5})
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    c.post("/new", data={"title": "One", "ask": "x"})
    html = c.get("/t/L-0001").text
    form = html.split('action="/t/L-0001/artifacts" enctype="multipart/form-data"', 1)[1].split("</form>", 1)[0]
    assert f'name="files" multiple required data-dropzone data-max-bytes="{int(2.5 * 1024 * 1024)}"' in form
    assert "Up to 2.5 MB per file." in form
    # what the zone sends (a pasted screenshot and a file, in one post) lands as before; the server still refuses a
    # file over the limit whatever the page said
    r = c.post("/t/L-0001/artifacts", files=[("files", ("screenshot-20261007-090503.png", b"png", "image/png")),
                                             ("files", ("notes.txt", b"n", "text/plain"))], follow_redirects=False)
    assert "err=" not in r.headers["location"]
    assert (ws.artifacts_dir / "L-0001" / "screenshot-20261007-090503.png").exists()
    assert (ws.artifacts_dir / "L-0001" / "notes.txt").exists()
    r = c.post("/t/L-0001/artifacts", files=[("files", ("big.bin", b"x" * (3 * 1024 * 1024), "application/octet-stream"))],
               follow_redirects=False)
    assert "err=" in r.headers["location"] and not (ws.artifacts_dir / "L-0001" / "big.bin").exists()
    assert store.load(ws, "L-0001")[1].id == "L-0001"
