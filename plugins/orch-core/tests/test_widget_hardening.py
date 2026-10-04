"""Limits a hostile or careless block or template must not get past (security review of the widgets branch): image
inlining bounded per document, malformed templates left out instead of a 500, frames that navigate torn down, link
schemes, and at most 40 widgets drawn per ticket."""
import json
import shutil
import subprocess
from pathlib import Path

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

F = "```"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli(monkeypatch, ws_root, capsys):
    from orch import actor
    from orch.cli import run
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setattr(actor, "is_interactive", lambda: False)

    def call(*args):
        capsys.readouterr()
        code = run(list(args))
        out = capsys.readouterr()
        return code, out.out, out.err
    return call


def fence(obj) -> str:
    return f"{F}orch\n{json.dumps(obj)}\n{F}"

def pin_t(ws, obj: dict) -> dict:
    """`obj` with the template pin `orch widget add` would write (registry.template_digest of the version now)."""
    from orch.widgets import registry
    _, current = registry.template_state(ws.home, obj["widget"], None)
    return {**obj, "sha256": current or "0" * 64}  # no such template: a well-formed pin, so "no template" shows



def _shots_template(ws):
    folder = ws.home / "widgets" / "shots"
    folder.mkdir(parents=True)
    (folder / "widget.json").write_text(json.dumps({"name": "shots", "versions": {"1": {"schema": {
        "type": "object", "properties": {"imgs": {"type": "array", "items": {"type": "string"}}}}}}}))
    (folder / "v1.html").write_text("<script>orch.ready()</script>")


def _findings(ws, tid, text):
    from orch.core import store
    t = store.load(ws, tid)[1]
    t.set_section("Findings", text)
    store.save(ws, t)


def test_one_image_named_many_times_is_read_once_and_the_document_stays_bounded(dash, put, ws, monkeypatch, wurl):
    import hashlib

    from orch.widgets import artifacts
    _shots_template(ws)
    tid = put("in-progress")
    big = b"\x89PNG" + bytes(1024 * 1024)
    (ws.artifacts_dir / tid).mkdir(parents=True, exist_ok=True)
    (ws.artifacts_dir / tid / "big.png").write_bytes(big)
    ref = {"path": f"artifacts/{tid}/big.png", "sha256": hashlib.sha256(big).hexdigest()}
    reads = []
    real = artifacts.data_uri
    monkeypatch.setattr(artifacts, "data_uri", lambda *a: reads.append(a) or real(*a))
    _findings(ws, tid, fence(pin_t(ws, {"widget": "shots@1", "id": "few", "data": {"imgs": [ref] * 3}})) + "\n\n"
              + fence(pin_t(ws, {"widget": "shots@1", "id": "many", "data": {"imgs": [ref] * 200}})))
    few = dash.get(wurl(tid, "few") + "?n=abcdefgh12").text
    assert few.count("data:image/png;base64,") == 3 and len(reads) == 1
    many = dash.get(wurl(tid, "many") + "?n=abcdefgh12")
    assert many.status_code == 200 and len(many.content) < 1024 * 1024
    assert "too many or too large images" in many.text


def test_a_files_digest_is_hashed_once_per_version(tmp_path, monkeypatch):
    """Widgets use orch.core.artifacts.file_sha256, cached per (path, mtime, size, inode)."""
    import hashlib

    from orch.widgets import artifacts
    f = tmp_path / "a.bin"
    f.write_bytes(b"one")
    first = artifacts.sha256(f)
    monkeypatch.setattr(hashlib, "sha256", lambda *a: (_ for _ in ()).throw(AssertionError("hashed again")))
    assert artifacts.sha256(f) == first
    monkeypatch.undo()
    f.write_bytes(b"two!")
    assert artifacts.sha256(f) != first


@pytest.mark.parametrize("spec, why", [
    ({"min_height": "abc", "versions": {"1": {}}}, "min_height"),
    ({"versions": {"1": "x"}}, "versions/1"),
    ({"versions": {"1": {"schema": {"type": 7}}}}, "not a JSON Schema"),
    ({"versions": []}, "versions"),
])
def test_a_malformed_template_is_left_out_and_named(dash, put, ws, cli, spec, why, wurl):
    folder = ws.home / "widgets" / "bad"
    folder.mkdir(parents=True)
    (folder / "widget.json").write_text(json.dumps(spec))
    (folder / "v1.html").write_text("<script>orch.ready()</script>")
    tid = put("in-progress")
    _findings(ws, tid, fence(pin_t(ws, {"widget": "bad@1", "data": {}})))
    page = dash.get(f"/t/{tid}")
    assert page.status_code == 200 and "no template" in page.text
    assert dash.get(wurl(tid, "0") + "?n=abcdefgh12").status_code in (200, 404)
    lib = dash.get("/widgets")
    assert lib.status_code == 200 and "template left out" in lib.text and why in lib.text
    code, out, _ = cli("widget", "list", "--json")
    assert code == 0 and any(r["name"] == "bad" and why in r["problem"] for r in json.loads(out))
    code, out, _ = cli("widget", "check", "--json")
    assert any(r["code"] == "widget-template" and why in r["message"] for r in json.loads(out))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_frame_that_navigates_is_torn_down():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "frames_navigate.js"),
                        str(ROOT / "src" / "orch" / "dashboard" / "static" / "widgets" / "orch-frames.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "frames navigate ok" in r.stdout


@pytest.mark.parametrize("url, ok", [
    ("https://example.com", True), ("/t/L-1", True), ("#w-a", True), ("mailto:a@example.com", True),
    # R24 (canonical_link) decides, as for every link in ticket Markdown: a scheme-relative URL is a web link
    ("//evil.com", True), ("/\\evil.com", False), ("/\t/evil.com", False), ("/\n/evil.com", False),
    ("javascript:alert(1)", False), ("java\tscript:alert(1)", False), ("data:text/html,x", False),
    # the ticket's own files are shown only by digest (media types), never by a link; another ticket's are links
    ("/a/L-1/shot.png", False), ("/a/l-1/shot.png", False), ("/t/../a/L-1/shot.png", False),
    ("artifact:shot.png", False), ("artifacts/L-1/shot.png", False), ("/a/L-2/shot.png", True),
])
def test_safe_url(url, ok):
    from types import SimpleNamespace

    from orch.widgets.types import safe_url
    assert (safe_url(url, SimpleNamespace(ticket=SimpleNamespace(id="L-1"))) is not None) == ok


def test_inline_text_in_a_widget_never_shows_an_artifact(ws, put):
    """Widget JSON sits in a fence, outside what the gate and verdict hashes bind: an artifact named in its inline
    Markdown stays inert, even when the ticket links that file."""
    from orch.widgets import Ctx, render_html
    from orch.widgets.blocks import make_block
    t = SimpleNamespace(id="L-1", meta={"artifacts": [{"name": "shot.png", "sha256": "a" * 64}]})
    for text in ("![x](artifact:shot.png)", "[x](artifact:shot.png)", "[x](/a/L-1/shot.png)", "![x](/a/L-1/shot.png)"):
        b = make_block("Context", 1, json.dumps({"type": "text", "text": text}))
        html = str(render_html(b, Ctx(ticket=t, ws=None)))
        assert "<img" not in html and 'href="/a/' not in html, text


def test_only_the_first_40_widgets_are_drawn(dash, put, ws):
    from orch.addons.api import AddonContext
    blocks = "\n\n".join(fence({"type": "text", "text": f"n{i}"}) for i in range(45))
    tid = put("in-progress", sections={"Findings": blocks})
    body = dash.get(f"/t/{tid}").text
    assert body.count('class="w w-t-text"') == 40
    assert body.count("only the first 40 widgets of a ticket are drawn") == 5
    docs = [w["document"] for w in AddonContext(ws, "x").ticket_widgets(tid)]
    assert all(docs[:40]) and not any(docs[40:])


def test_widgets_name_ticket_artifacts_as_artifact_refs(dash, put, ws, wurl):
    import hashlib
    tid = put("in-progress")
    (ws.artifacts_dir / tid).mkdir(parents=True, exist_ok=True)
    page = b"<p id=r>replay</p>"
    (ws.artifacts_dir / tid / "replay.html").write_bytes(page)
    good = hashlib.sha256(page).hexdigest()
    _findings(ws, tid, fence({"html": "artifact:replay.html", "sha256": good, "id": "ok"}) + "\n\n"
              + fence({"html": "artifact:../escape.html", "sha256": good, "id": "out"}) + "\n\n"
              + fence({"html": "artifact:replay.html", "sha256": "0" * 64, "id": "changed"}))
    body = dash.get(f"/t/{tid}").text
    assert "changed since this widget was written" in body and "is missing" in body
    assert "<p id=r>replay</p>" in dash.get(wurl(tid, "ok") + "?n=abcdefgh12").text
    assert "window.orch" not in dash.get(wurl(tid, "out") + "?n=abcdefgh12").text


def test_widget_fences_are_drawn_without_changing_what_a_gate_binds():
    """The fence rule is a render rule on main's shared parser: the tokens, and so orch.core.artifacts.binding, are
    the same; an artifact named inside a widget's JSON is not bound or shown as an artifact link."""
    from types import SimpleNamespace

    from orch.core.artifacts import _parser, binding
    from orch.dashboard.markdown import ArtifactScope, SectionWidgets, render_markdown, render_section
    from orch.widgets import Ctx
    text = "See ![shot](artifact:a.png).\n\n" + fence({"type": "text", "text": "![x](artifact:b.png)"})
    ticket = SimpleNamespace(id="L-1", meta={"artifacts": [{"name": "a.png", "sha256": "1" * 64},
                                                           {"name": "b.png", "sha256": "2" * 64}]})
    assert binding(ticket, [text], None) == [("artifact a.png", "sha256:" + "1" * 64)]
    assert _parser().parse(text, {}) == _parser().parse(text, {})  # nothing stateful added to parsing
    scope = ArtifactScope("L-1", {e["name"]: e for e in ticket.meta["artifacts"]})
    drawn = render_section(text, "Findings", SectionWidgets(Ctx(), []), scope)
    plain = render_markdown(text, scope)
    assert 'class="w w-t-text"' in drawn and "<pre><code" in plain and 'class="w w-t-text"' not in plain
    for html in (drawn, plain):
        assert "/a/L-1/a.png?v=" in html and "/a/L-1/b.png" not in html


def test_wiki_pages_never_draw_widgets():
    """Local wiki pages (render_page_markdown, the md_page filter) keep their own link rules and show an ```orch
    fence as code: widgets in pages are a later change (#42)."""
    from orch.dashboard.markdown import md_page_filter, render_page_markdown
    text = "Intro\n\n" + fence({"type": "stats", "items": [{"label": "p95", "value": "41 ms"}]})
    for html in (render_page_markdown(text), md_page_filter(text, "wiki", "a.md", ["a.md"])):
        assert "<pre" in html and "w-stat" not in html and 'class="w ' not in html and "data-doc-url" not in html
