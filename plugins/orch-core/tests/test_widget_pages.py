"""Ticket widgets on a wiki page (docs/widgets.md, "Widgets on a wiki page"): placement, files by digest in `_files/`,
links, the frame route /wp/, checks, text alternatives for search and the copy a page made from a ticket carries."""
import base64
import hashlib
import json
import re
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from orch.addons import userfiles  # noqa: E402
from orch.dashboard.markdown import md_page_filter  # noqa: E402
from orch.widgets import pages  # noqa: E402

F = "```"
FOLDER = "orchestrator/wiki"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
MERMAID = {"source": "flowchart LR\n  a --> b", "alt": "a then b"}
BARS = {"type": "bars", "title": "Bundle size", "data": {"main": 412, "branch": 286}}


def fence(obj) -> str:
    return f"{F}orch\n{json.dumps(obj)}\n{F}"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def pin_t(ws, obj: dict) -> dict:
    from orch.widgets import registry
    _, current = registry.template_state(ws.home, obj["widget"], None)
    return {**obj, "sha256": current or "0" * 64}


def put_file(ws, name: str, blob: bytes):
    d = ws.root / FOLDER / pages.FILES
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(blob)
    return f"{pages.FILES}/{name}"


def draw(ws, text, here="decisions/DEMO-0001"):
    return md_page_filter(text, "wiki", here, (here,), ws, FOLDER)


def compare(ref, digest):
    return {"type": "compare", "before": {"path": ref, "sha256": digest}, "after": {"path": ref, "sha256": digest}}


def test_a_page_draws_a_bars_block_and_a_template_like_a_ticket(ws):
    html = draw(ws, f"# Page\n\n{fence(BARS)}\n\n{fence(pin_t(ws, {'widget': 'mermaid@1', 'data': MERMAID, 'id': 'flow'}))}\n")
    assert 'class="w w-t-bars"' in html and "Bundle size" in html and ">core<" in html
    assert 'id="w-flow"' in html and "agent HTML · mermaid@1" in html and "Show text" in html
    # the frame is the page frame route: addon + digest + page id, never a ticket address
    [url] = re.findall(r'data-doc-url="([^"]+)"', html)
    assert url.startswith("/wp/wiki/") and "?page=decisions%2FDEMO-0001&amp;n=" in url and "/w/" not in url
    assert "orch-frames.js" in html and "widgets/core.css" in html  # assets linked once before the first widget
    assert html.count("orch-frames.js") == 1


def test_agent_html_off_shows_the_text_alternative(configure):
    ws = configure(widgets={"html": False})
    html = draw(ws, fence(pin_t(ws, {"widget": "mermaid@1", "data": MERMAID})))
    assert "w-frame" not in html and "Agent HTML is off" in html


def test_a_block_may_stand_before_any_heading_and_under_a_gated_looking_title(ws):
    """A page has no gate: no widget-place refusal anywhere, whatever the heading is called."""
    html = draw(ws, f"{fence(BARS)}\n\n## Requirements\n\n{fence(BARS | {'title': 'second'})}\n")
    assert html.count('class="w w-t-bars"') == 2 and "Shown as code" not in html


def test_an_invalid_block_names_the_page_path_and_line_and_the_rest_renders(ws):
    html = draw(ws, f"# Page\n\ntext\n\n{F}orch\n{{\"type\": \"nope\"}}\n{F}\n\n{fence(BARS)}\n")
    assert "Widget not shown" in html and f"{FOLDER}/decisions/DEMO-0001.md, line 5" in html
    assert 'class="w w-t-bars"' in html


def test_other_fences_and_nested_fences_stay_code(ws):
    html = draw(ws, f"{F}json\n{{\"type\": \"bars\"}}\n{F}\n\n- item\n\n  {fence(BARS)}\n")
    assert "w-t-bars" not in html


def test_a_checks_block_on_a_page_is_a_claim_never_a_gate(ws, put):
    tid = put("testing", sections={"Verification": "- AC1 met"})
    block = {"type": "checks", "rows": [{"ac": "AC1", "verdict": "met", "evidence": "suite green"}]}
    html = draw(ws, fence(block))
    assert "suite green" in html and "Met" in html
    assert "not a gate, and not the ticket&#x27;s verdict" in html or "not a gate, and not the ticket's verdict" in html
    from orch.core import store
    t = store.load(ws, tid)[1]
    assert "w-" not in t.section("Verification")  # nothing was written to the ticket


def test_files_by_digest_inside_the_wiki_folder(ws):
    ref = put_file(ws, "a.png", PNG)
    html = draw(ws, fence(compare(ref, sha(PNG))))
    url = f"/wpf/wiki/{sha(PNG)}?page=decisions%2FDEMO-0001"
    assert html.count(f'src="{url}"') == 2 and "data:" not in html and "/a/" not in html  # never embedded
    changed = draw(ws, fence(compare(ref, sha(b"other"))))
    assert "changed since this widget was written" in changed and "/wpf/" not in changed


def test_a_file_outside_the_wiki_folder_a_symlink_or_missing_is_missing(ws, tmp_path):
    ref = put_file(ws, "a.png", PNG)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    (ws.root / FOLDER / pages.FILES / "link.png").symlink_to(outside)
    for bad in ("artifacts/DEMO-0001/a.png", f"{pages.FILES}/link.png", f"{pages.FILES}/nope.png",
                f"{pages.FILES}/../a.png", "artifact:a.png", f"{pages.FILES}/sub/a.png"):
        html = draw(ws, fence(compare(bad, sha(PNG))))
        assert "/wpf/" not in html and "data:image" not in html, bad
        assert "is missing" in html or "Widget not shown" in html, bad
    assert ref  # the good one still draws
    assert "/wpf/wiki/" in draw(ws, fence(compare(ref, sha(PNG))))


def test_a_linked_files_folder_is_not_read(ws, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "a.png").write_bytes(PNG)
    (ws.root / FOLDER).mkdir(parents=True)
    (ws.root / FOLDER / pages.FILES).symlink_to(outside, target_is_directory=True)
    assert "/wpf/" not in draw(ws, fence(compare(f"{pages.FILES}/a.png", sha(PNG))))


def test_a_page_never_makes_a_live_link_to_a_ticket_artifact(ws):
    block = {"type": "links", "items": [{"label": "other", "url": "/a/DEMO-0002/x.png"},
                                        {"label": "web", "url": "https://example.com/"}]}
    text = {"type": "text", "text": "![i](/a/DEMO-0002/x.png) [j](/a/DEMO-0002/x.png) [k](https://example.com/)"}
    html = draw(ws, fence(block) + "\n\n" + fence(text))
    assert 'href="/a/' not in html and 'src="/a/' not in html and "<img" not in html  # shown as text, never live
    assert 'href="https://example.com/"' in html


def test_frame_route_serves_the_page_block_with_the_sandbox_headers(dash, ws):
    tpl = pin_t(ws, {"widget": "mermaid@1", "data": MERMAID})
    text = f"intro\n\n{fence(tpl)}\n"
    seen = []

    def source(page):
        seen.append(page)
        return (text, FOLDER) if page == "decisions/DEMO-0001" else None
    obj = SimpleNamespace(page_source=source)
    dash.app.state.addons = SimpleNamespace(registry=SimpleNamespace(get=lambda n: SimpleNamespace(obj=obj) if n == "wiki" else None))
    from orch.widgets.blocks import make_block
    digest = make_block("x", 1, json.dumps(tpl)).digest
    r = dash.get(f"/wp/wiki/{digest}?page=decisions%2FDEMO-0001&n=abcdefgh12")
    assert r.status_code == 200
    assert r.headers["content-security-policy"].startswith("sandbox allow-scripts;")  # not replaced by the page CSP
    assert '<meta name="orch-frame" content="abcdefgh12">' in r.text
    assert dash.get(f"/wp/wiki/{'0' * 64}?page=decisions%2FDEMO-0001").status_code == 404
    assert dash.get(f"/wp/wiki/{digest}?page=other").status_code == 404
    assert dash.get(f"/wp/nope/{digest}?page=decisions%2FDEMO-0001").status_code == 404
    assert dash.get(f"/wp/wiki/{digest}?page=decisions%2FDEMO-0001&n=bad nonce!").status_code == 400
    assert dash.get("/wp/wiki/notadigest?page=x").status_code == 404


def test_frame_route_refuses_a_digest_a_page_does_not_hold_and_duplicate_ids(dash, ws):
    one = pin_t(ws, {"widget": "mermaid@1", "data": MERMAID, "id": "same"})
    two = pin_t(ws, {"widget": "mermaid@1", "data": {**MERMAID, "alt": "other"}, "id": "same"})
    text = fence(one) + "\n\n" + fence(two)
    obj = SimpleNamespace(page_source=lambda page: (text, FOLDER))
    dash.app.state.addons = SimpleNamespace(registry=SimpleNamespace(get=lambda n: SimpleNamespace(obj=obj)))
    from orch.widgets.blocks import make_block
    digest = make_block("x", 1, json.dumps(one)).digest
    assert dash.get(f"/wp/wiki/{digest}?page=p").status_code == 409
    html = draw(ws, text)
    assert html.count("used twice on this page") == 2


def test_text_alternatives_replace_the_json_for_search(ws):
    text = f"Intro DEMO-0001\n\n{fence(BARS)}\n\n{F}python\nprint(1)\n{F}\n\n{F}orch\nnot json\n{F}\n"
    out = pages.text_alternatives(ws, text, "p", FOLDER)
    assert "Intro DEMO-0001" in out and "main" in out and "412" in out and "branch" in out
    assert '"type"' not in out.split("```python")[0] and "bars" not in out.lower().split("bundle size")[0].splitlines()[-1]
    assert "print(1)" in out and "not json" in out  # other fences and a block that does not parse stay as written


def enable_wiki(ws, folder=FOLDER):
    userfiles.set_enabled(ws.root, "wiki", True)
    userfiles.save_addon_config(ws.root, "wiki", {"provider": "local", "folder": folder})


def test_check_reports_a_broken_block_in_a_wiki_page_with_path_and_line(ws, capsys):
    enable_wiki(ws)
    page = ws.root / FOLDER / "decisions" / "X.md"
    page.parent.mkdir(parents=True)
    page.write_text(f"---\ntitle: x\n---\n\ntext\n\n{F}orch\n{{\"type\": \"nope\"}}\n{F}\n\n{fence(BARS)}\n", encoding="utf-8")
    rows = pages.findings(ws)
    assert [(r["section"], r["line"], r["code"]) for r in rows] == [(f"{FOLDER}/decisions/X.md", 7, "widget-schema")]
    from orch.cli import run
    assert run(["widget", "check"]) == 5
    out = capsys.readouterr().out
    assert f"{FOLDER}/decisions/X.md:7" in out
    from orch.core.check import run_checks
    [f] = [f for f in run_checks(ws, emit_events=False) if f.code == "widget-schema"]
    assert f.message.startswith(f"{FOLDER}/decisions/X.md:7:")


def test_check_ignores_pages_when_the_wiki_is_off_or_not_local_or_the_folder_is_refused(ws, tmp_path):
    page = ws.root / FOLDER / "X.md"
    page.parent.mkdir(parents=True)
    page.write_text(f"{F}orch\nnope\n{F}\n", encoding="utf-8")
    assert pages.findings(ws) == []  # addon not enabled
    userfiles.set_enabled(ws.root, "wiki", True)
    userfiles.save_addon_config(ws.root, "wiki", {"provider": "github-wiki"})
    assert pages.findings(ws) == []
    for folder in ("orchestrator/tickets", "../out", "/etc"):
        enable_wiki(ws, folder)
        assert pages.findings(ws) == []
    enable_wiki(ws)
    assert len(pages.findings(ws)) == 1


def test_ticket_copy_keeps_blocks_of_verification_and_findings_with_digest_checked_files(ws, put):
    from orch.widgets.pages import ticket_copy
    tid = put("testing", sections={
        "Summary": "s", "Verification": f"- AC1 done\n\n{fence({'type': 'stats', 'items': [{'label': 'tests', 'value': '12'}]})}\n",
        "Findings": "x"})
    art = ws.artifacts_dir / tid
    art.mkdir(parents=True)
    (art / "a.png").write_bytes(PNG)
    from orch.core import store
    t = store.load(ws, tid)[1]
    t.set_section("Findings", fence(compare(f"artifacts/{tid}/a.png", sha(PNG))) + "\n\n"
                  + fence(compare(f"artifacts/{tid}/gone.png", sha(PNG))) + "\n")
    store.save(ws, t)
    got = ticket_copy(ws, tid, ("Verification", "Findings"))
    assert [s for s, _ in got["blocks"]] == ["Verification", "Findings"]
    assert got["files"] == {f"{tid}-a.png": PNG}
    assert f'"_files/{tid}-a.png"' in got["blocks"][1][1] and "artifacts/" not in got["blocks"][1][1]
    assert len(got["skipped"]) == 1 and "gone.png" in got["skipped"][0]
    (art / "a.png").write_bytes(b"swapped")  # a file that no longer matches its digest is not copied
    again = ticket_copy(ws, tid, ("Findings",))
    assert again["blocks"] == [] and len(again["skipped"]) == 2 and again["files"] == {}


def test_a_video_on_a_page_is_a_wpf_url_the_page_csp_lets_play(ws):
    blob = b"\x00\x00\x00\x18ftypmp42" + b"x" * 64
    ref = put_file(ws, "run.mp4", blob)
    html = draw(ws, fence({"type": "video", "path": ref, "sha256": sha(blob)}))
    assert f'<video class="w-video" controls preload="metadata" src="/wpf/wiki/{sha(blob)}?page=' in html
    assert "data:" not in html
    from orch.dashboard.app import PAGE_CSP
    assert "default-src 'self'" in PAGE_CSP and "media-src" not in PAGE_CSP  # same-origin media falls back to it


def _serve(dash, text):
    obj = SimpleNamespace(page_source=lambda page: (text, FOLDER) if page == "p" else None)
    dash.app.state.addons = SimpleNamespace(registry=SimpleNamespace(get=lambda n: SimpleNamespace(obj=obj) if n == "wiki" else None))


def test_page_file_route_serves_only_pinned_bytes_with_safe_headers(dash, ws):
    ref = put_file(ws, "a.png", PNG)
    put_file(ws, "unpinned.png", PNG + b"1")
    _serve(dash, fence(compare(ref, sha(PNG))))
    r = dash.get(f"/wpf/wiki/{sha(PNG)}?page=p")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert r.headers["content-security-policy"] == "sandbox"  # not replaced by the page CSP
    assert dash.get(f"/wpf/wiki/{sha(PNG + b'1')}?page=p").status_code == 404  # a file no block pins
    assert dash.get(f"/wpf/wiki/{sha(PNG)}?page=other").status_code == 404
    assert dash.get(f"/wpf/nope/{sha(PNG)}?page=p").status_code == 404
    assert dash.get("/wpf/wiki/xyz?page=p").status_code == 404
    (ws.root / FOLDER / pages.FILES / "a.png").write_bytes(b"swapped after the page was drawn")
    assert dash.get(f"/wpf/wiki/{sha(PNG)}?page=p").status_code == 404  # bytes no longer match: never served


def test_page_file_route_refuses_symlinks_non_media_and_a_block_with_an_error(dash, ws, tmp_path):
    outside = tmp_path / "o.png"
    outside.write_bytes(PNG)
    put_file(ws, "x.png", b"x")
    (ws.root / FOLDER / pages.FILES / "link.png").symlink_to(outside)
    html_blob = b"<script>1</script>"
    ref_html = put_file(ws, "n.html", html_blob)
    text = fence(compare(f"{pages.FILES}/link.png", sha(PNG))) + "\n\n" + fence({"type": "screens", "items": [
        {"label": "h", "path": ref_html, "sha256": sha(html_blob)}], "bogus": 1})
    _serve(dash, text)
    assert dash.get(f"/wpf/wiki/{sha(PNG)}?page=p").status_code == 404
    assert dash.get(f"/wpf/wiki/{sha(html_blob)}?page=p").status_code == 404
