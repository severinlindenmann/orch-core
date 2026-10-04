import pytest

pytest.importorskip("fastapi")

from orch.core import store
from orch.core.questions import build_questions


def _q(**over):
    raw = {"text": "One repo or one per environment?", "why": "Decides layout",
           "options": [{"key": "A", "label": "One repo", "cost": "mixed permissions"}, {"key": "B", "label": "Per env"}],
           "recommended": "A", **over}
    return build_questions([raw], [], "2026-09-30T09:00Z")[0]


def test_detail_page(dash, ws, put, aops, tmp_path):
    tid = put("backlog", title="Back up nightly config", sections={"Ask": "Please **back it up**."},
              questions=[_q()])
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    aops.artifact_add(tid, img)
    r = dash.get(f"/t/{tid}")
    assert r.status_code == 200
    body = r.text
    assert "Back up nightly config" in body and "<strong>back it up</strong>" in body
    # story page: an empty gate says "Not written yet"; no Approve form that Ops.approve would refuse
    gate = body.split('id="gate-requirements"', 1)[1].split("</section>", 1)[0]
    assert "Not written yet." in gate and f'action="/t/{tid}/approve"' not in body
    assert 'value="A"' in body and "checked" in body and "recommended" in body and "mixed permissions" in body
    assert f'/a/{tid}/shot.png' in body and "added artifact shot.png" in body


def test_actions_depend_on_status(dash, put):
    testing = put("testing")
    body = dash.get(f"/t/{testing}").text
    assert ">Accept, mark done</button>" in body and "Send back" in body and "Approve requirements" not in body
    working = put("in-progress", size="xs", sections={"Verification": "ok", "Tasks": "- [x] T1 the work"})
    body = dash.get(f"/t/{working}").text
    assert "<option>testing</option>" in body and "<option>backlog</option>" in body


def test_answered_question_shows_answer(dash, put):
    q = _q()
    q.update(answer="B", note="keep it simple", answered="2026-09-30T10:00Z", via="dashboard")
    tid = put("backlog", questions=[q])
    body = dash.get(f"/t/{tid}").text
    assert "Answer: <b>B</b>" in body and "keep it simple" in body and 'name="qid"' not in body


def test_not_found_and_broken(dash, ws):
    assert dash.get("/t/L-0404").status_code == 404
    (ws.status_dir("open") / "L-0077-broken.md").write_text("---\nid: [\n---\nbody text\n", encoding="utf-8")
    r = dash.get("/t/L-0077")
    assert r.status_code == 422 and "cannot be read" in r.text and "body text" in r.text


def test_raw_view(dash, ws, put):
    tid = put("open")
    r = dash.get(f"/t/{tid}/raw")
    assert r.status_code == 200 and f"id: {tid}" in r.text and f"/t/{tid}/edit" in r.text


def test_artifact_headers(dash, ws, put, aops, tmp_path):
    tid = put("open")
    page = tmp_path / "report.html"
    page.write_text("<script>alert(1)</script>", encoding="utf-8")
    aops.artifact_add(tid, page)
    r = dash.get(f"/a/{tid}/report.html")
    assert r.status_code == 200
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    assert f'<iframe class="preview" sandbox src="/a/{tid}/report.html"' in dash.get(f"/t/{tid}").text


def _preview_case(put, aops, tmp_path, sha_of):
    """A ticket whose Findings hold an html block for drawn.html, plus drawn.html and other.html as artifacts."""
    import hashlib
    page = b"<p>x</p>"
    (tmp_path / "drawn.html").write_bytes(page)
    (tmp_path / "other.html").write_bytes(page)
    sha = sha_of(hashlib.sha256(page).hexdigest())
    tid = put("open", sections={"Findings": '```orch\n{"html": "artifact:drawn.html", "sha256": "' + sha + '"}\n```'})
    for name in ("drawn.html", "other.html"):
        aops.artifact_add(tid, tmp_path / name)
    return tid


def _iframe(tid, name):
    return f'<iframe class="preview" sandbox src="/a/{tid}/{name}"'


def test_html_artifact_a_widget_frame_draws_gets_no_second_preview(html_on, dash, put, aops, tmp_path):
    tid = _preview_case(put, aops, tmp_path, lambda good: good)
    body = dash.get(f"/t/{tid}").text
    assert _iframe(tid, "other.html") in body and _iframe(tid, "drawn.html") not in body


def test_a_wrong_pin_keeps_the_artifact_preview(html_on, dash, put, aops, tmp_path):
    tid = _preview_case(put, aops, tmp_path, lambda good: "0" * 64)
    body = dash.get(f"/t/{tid}").text
    assert _iframe(tid, "drawn.html") in body and _iframe(tid, "other.html") in body


def test_html_off_keeps_the_artifact_preview(dash, put, aops, tmp_path):  # no html_on: agent HTML is off
    tid = _preview_case(put, aops, tmp_path, lambda good: good)
    assert _iframe(tid, "drawn.html") in dash.get(f"/t/{tid}").text


@pytest.mark.parametrize("path", [
    "..%2F..%2Fconfig.json",
    "%2e%2e/%2e%2e/config.json",
    "%2Fetc%2Fpasswd",
    "missing.png",
])
def test_artifact_paths_cannot_escape(dash, ws, put, path):
    tid = put("open")
    (ws.artifacts_dir / tid).mkdir(parents=True)
    assert dash.get(f"/a/{tid}/{path}").status_code == 404
    assert dash.get(f"/a/..%2F/config.json").status_code == 404


@pytest.mark.parametrize("meta", [
    {"external": "abc"},
    {"external": {"key": "jira"}},
    {"external": 123},
    {"prs": "abc"},
    {"prs": {"repo": "x"}},
    {"prs": 123},
    {"branches": ["main"]},
    {"claim": "abc"},
])
def test_detail_page_survives_odd_frontmatter(dash, put, meta):
    tid = put("open", **meta)
    assert dash.get(f"/t/{tid}").status_code == 200


def test_only_http_links_are_clickable(dash, put):
    tid = put("open", external=[{"key": "JIRA-1", "url": "javascript:alert(1)"},
                                {"key": "JIRA-2", "url": "https://jira.example/JIRA-2"}],
              prs=[{"repo": "infra", "state": "open", "url": "JaVaScRiPt:alert(2)"},
                   {"repo": "app", "state": "merged", "url": "http://git.example/pr/3"}])
    body = dash.get(f"/t/{tid}").text
    assert "javascript:" not in body.lower()
    assert "JIRA-1" in body and "infra · open" in body and "app #3 · merged" in body
    assert 'href="https://jira.example/JIRA-2"' in body and 'href="http://git.example/pr/3"' in body


PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")


def test_dashboard_pages_send_a_csp(dash, ws, put):
    tid = put("open")
    for url in ("/", f"/t/{tid}", f"/t/{tid}/raw", "/new", "/workspace", "/t/L-9999"):
        assert dash.get(url).headers.get("content-security-policy") == PAGE_CSP, url
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    locked = TestClient(create_app(ws, "tok")).get("/")
    assert locked.status_code == 401 and locked.headers.get("content-security-policy") == PAGE_CSP


@pytest.mark.parametrize("name,body", [
    ("page.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><form action="/t/x/comment" method="post"/></html>'),
    ("feed.xml", "<?xml version='1.0'?><root/>"),
    ("page.xht", "<html/>"),
    ("page.shtml", "<html/>"),
    ("notes.md", "# hi"),
    ("shot.png", "\x89PNG"),
    ("data.bin", "x"),
])
def test_every_non_pdf_artifact_is_sandboxed(dash, put, aops, tmp_path, name, body):
    tid = put("open")
    src = tmp_path / name
    src.write_text(body, encoding="utf-8")
    aops.artifact_add(tid, src)
    csp = dash.get(f"/a/{tid}/{name}").headers["content-security-policy"]
    assert csp.startswith("sandbox;") and "form-action 'none'" in csp


def test_pdf_artifact_keeps_viewer(dash, put, aops, tmp_path):
    tid = put("open")
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    aops.artifact_add(tid, src)
    r = dash.get(f"/a/{tid}/doc.pdf")
    assert r.status_code == 200 and "content-security-policy" not in r.headers


def test_raw_view_of_broken_ticket_offers_no_edit(dash, ws, put):
    from orch.core import store
    tid = put("open")
    path = store.resolve(ws, tid).path
    path.write_text("---\nid: [unclosed\n---\n", encoding="utf-8")
    r = dash.get(f"/t/{tid}/raw")
    assert r.status_code == 200 and "unclosed" in r.text and f"/t/{tid}/edit" not in r.text
