"""Widgets and the verdict: a Verification widget is part of what the human reads before the verdict, so the verdict
hash binds the template version and the files it pins (orch.core.artifacts.widget_binding), a template edited in
place is drift and never drawn, and the decision cards and the epic verdict draw widgets like the ticket page."""
from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("fastapi")

from orch.core import store  # noqa: E402
from orch.core.epics import verdict_hash  # noqa: E402
from orch.errors import ValidationError  # noqa: E402

F = "```"
PAGE = "<p id=g>gauge v1</p><script>orch.ready()</script>"


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


def _template(ws, body=PAGE, libs=None):
    folder = ws.home / "widgets" / "gauge"
    folder.mkdir(parents=True, exist_ok=True)
    spec = {"name": "gauge", "title": "Gauge", "moment": "verify", "versions": {"1": {"schema": {"type": "object"}}}}
    if libs is not None:
        spec["libs"] = libs
    (folder / "widget.json").write_text(json.dumps(spec))
    (folder / "v1.html").write_text(body)


def _block(ws, **extra) -> str:
    from orch.widgets import registry
    _, pin = registry.template_state(ws.home, "gauge@1", None)
    return f"{F}orch\n{json.dumps({'widget': 'gauge@1', 'sha256': pin, 'id': 'g', 'data': {'v': 3}, **extra})}\n{F}"


def _testing(ws, aops, tid, plan_approved, close_tasks, verification):
    plan_approved(tid)
    close_tasks(aops, tid)
    aops.set_section(tid, "Verification", verification)
    aops.move(tid, "testing")
    return store.load(ws, tid)[1]


def test_a_changed_template_invalidates_the_seen_verdict_hash_and_draws_as_drift(
        dash, ws, aops, hops, working, plan_approved, close_tasks, wurl):
    _template(ws)
    t = _testing(ws, aops, working, plan_approved, close_tasks,
                 f"- AC1: the gauge reads 3\n\n{_block(ws)}\n\n- AC2: cost compared, 12 % lower")
    page = dash.get(f"/t/{working}").text
    assert 'id="w-g"' in page and "w-frame" in page.split('id="w-g"', 1)[1].split("</figure>", 1)[0]
    seen = re.search(r'name="seen" value="([^"]+)"', page).group(1)
    assert seen == verdict_hash([t], ws)

    _template(ws, body="<p id=g>gauge v1, edited in place</p><script>orch.ready()</script>")
    assert verdict_hash([t], ws) != seen  # the pin no longer matches the bytes: the hash says drift
    with pytest.raises(ValidationError, match="changed since you read them"):
        hops.verdict(working, "done", expected_hash=seen)
    assert store.load(ws, working)[1].status == "testing"

    page = dash.get(f"/t/{working}").text
    fig = page.split('id="w-g"', 1)[1].split("</figure>", 1)[0]
    assert "<b>Drift:</b>" in fig and "changed since this widget was written" in fig and "w-frame" not in fig
    doc = dash.get(wurl(working, "g") + "?n=abcdefgh12").text
    assert "edited in place" not in doc and "window.orch" not in doc and "<b>Drift:</b>" in doc


def test_a_changed_library_list_is_drift_too(ws, aops, working, plan_approved, close_tasks):
    _template(ws)
    t = _testing(ws, aops, working, plan_approved, close_tasks, f"- AC1: the gauge reads 3\n\n{_block(ws)}")
    seen = verdict_hash([t], ws)
    _template(ws, libs=["mermaid"])
    assert verdict_hash([t], ws) != seen


def test_a_repinned_widget_changes_the_text_and_so_the_hash(ws, aops, working, plan_approved, close_tasks):
    _template(ws)
    t = _testing(ws, aops, working, plan_approved, close_tasks, f"- AC1: the gauge reads 3\n\n{_block(ws)}")
    seen = verdict_hash([t], ws)
    _template(ws, body="<p>v1 again, edited</p><script>orch.ready()</script>")
    t.set_section("Verification", f"- AC1: the gauge reads 3\n\n{_block(ws)}")
    assert verdict_hash([t], ws) != seen


def test_a_pinned_image_that_changed_is_drift_in_the_hash(ws, aops, working, plan_approved, close_tasks, tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG-one")
    aops.artifact_add(working, png, ac=1)
    import hashlib
    pin = hashlib.sha256(b"\x89PNG-one").hexdigest()
    block = {"type": "compare", "before": {"path": "artifact:shot.png", "sha256": pin},
             "after": {"path": "artifact:shot.png", "sha256": pin}}
    t = _testing(ws, aops, working, plan_approved, close_tasks,
                 f"- AC1: before and after look the same\n\n{F}orch\n{json.dumps(block)}\n{F}")
    seen = verdict_hash([t], ws)
    (ws.artifacts_dir / working / "shot.png").write_bytes(b"\x89PNG-two")
    assert verdict_hash([t], ws) != seen


def test_the_verdict_card_draws_widgets_like_the_ticket_page(dash, ws, aops, working, plan_approved, close_tasks):
    _template(ws)
    stats = {"type": "stats", "items": [{"label": "p95", "value": "41 ms"}]}
    _testing(ws, aops, working, plan_approved, close_tasks,
             f"- AC1: the gauge reads 3\n\n{_block(ws)}\n\n- AC2: timing\n\n{F}orch\n{json.dumps(stats)}\n{F}")
    html = dash.get("/").text
    card = html.split(f'id="d-{working}-verdict"', 1)[1].split("</article>", 1)[0]
    assert 'id="w-g"' in card and "w-frame" in card and "w-stat-v" in card
    assert "&#34;widget&#34;" not in card and "&quot;widget&quot;" not in card  # never the raw JSON
    assert "widgets/orch-frames.js" in html and "widgets/core.css" in html
    assert html.count("widgets/orch-frames.js") == 1  # linked once, before the first widget


def test_a_page_without_widgets_links_no_widget_assets(dash, ws, aops, working, plan_approved, close_tasks):
    _testing(ws, aops, working, plan_approved, close_tasks, "- AC1: ran the jobs\n- AC2: cost compared")
    html = dash.get("/").text
    assert f'id="d-{working}-verdict"' in html and "widgets/orch-frames.js" not in html


def test_the_epic_verdict_draws_each_childs_widgets(dash, ws, aops, hops, close_tasks):
    _template(ws)
    e = aops.new("Billing revamp", type="epic")
    aops.set_section(e.id, "Requirements", "r")
    aops.set_section(e.id, "Acceptance criteria", "- [ ] a")
    c = aops.new("Invoice export", epic=e.id)
    for name, text in (("Requirements", "r"), ("Acceptance criteria", "- [ ] a"), ("Plan", "1. do it")):
        aops.set_section(c.id, name, text)
    hops.approve(e.id, "requirements", delegate={})
    if store.load(ws, c.id)[1].meta.get("gates", {}).get("plan", {}).get("approved") is None:
        hops.approve(c.id, "requirements")
        hops.approve(c.id, "plan")
    aops.claim(c.id)
    close_tasks(aops, c.id)
    aops.set_section(c.id, "Verification", f"- AC1: the gauge reads 3\n\n{_block(ws)}")
    aops.move(c.id, "testing")
    page = dash.get(f"/t/{e.id}").text
    verdict = page.split('id="epic-verdict"', 1)[1].split("</section>", 1)[0]
    assert 'id="w-g"' in verdict and f'data-doc-url="/w/{c.id}/Verification/' in verdict
    assert "&quot;widget&quot;" not in verdict and "&#34;widget&#34;" not in verdict


def test_widget_add_pins_the_template_version(ws, put, cli):
    from orch.widgets import registry
    _template(ws)
    working = put("in-progress")
    code, out, err = cli("widget", "add", working, "--section", "Findings", "--widget", "gauge@1", "--data", '{"v": 3}',
                         "--json")
    assert code == 0, err
    assert json.loads(out)["block"]["sha256"] == registry.template_state(ws.home, "gauge@1", None)[1]


def test_a_frame_serves_only_the_block_of_its_section_and_digest(dash, ws, aops, working, plan_approved, close_tasks):
    """A block elsewhere in the ticket (not bound by the verdict hash) never fills a Verification frame."""
    _template(ws)
    folder = ws.home / "widgets" / "other"
    folder.mkdir(parents=True)
    (folder / "widget.json").write_text(json.dumps({"name": "other", "versions": {"1": {"schema": {"type": "object"}}}}))
    (folder / "v1.html").write_text("<p>OTHER-FROM-CONTEXT</p><script>orch.ready()</script>")
    from orch.widgets import registry
    other = {"widget": "other@1", "sha256": registry.template_state(ws.home, "other@1", None)[1], "id": "o"}
    aops.set_section(working, "Context", f"{F}orch\n{json.dumps(other)}\n{F}")
    _testing(ws, aops, working, plan_approved, close_tasks, f"- AC1: the gauge reads 3\n\n{_block(ws)}")
    page = dash.get(f"/t/{working}").text
    urls = [u.replace("&amp;", "&") for u in re.findall(r'data-doc-url="([^"]+)"', page)]
    ver = [u for u in urls if "/Verification/" in u]
    doc = dash.get(ver[0]).text
    assert len(ver) == 1 and "gauge v1" in doc and "OTHER-FROM-CONTEXT" not in doc
    ctx_url = next(u for u in urls if "/Context/" in u)
    swapped = ctx_url.split("?")[0].replace("/Context/", "/Verification/")
    assert dash.get(swapped).status_code == 404  # the Context block's digest under Verification: refused


def test_a_reused_id_is_refused_everywhere(dash, ws, aops, working, plan_approved, close_tasks):
    _template(ws)
    aops.set_section(working, "Context", _block(ws))  # the same id "g" as the Verification block
    t = _testing(ws, aops, working, plan_approved, close_tasks, f"- AC1: the gauge reads 3\n\n{_block(ws, title='V')}")
    page = dash.get(f"/t/{working}").text
    assert "is used twice in this ticket" in page and "data-doc-url" not in page
    from orch.widgets import ticket_blocks
    from orch.widgets.render import frame_path
    raw = store.resolve(ws, working).path.read_text(encoding="utf-8")
    for b in ticket_blocks(t, raw):
        r = dash.get(frame_path(working, b))
        assert r.status_code == 409 and "gauge v1" not in r.text


@pytest.mark.parametrize("wid", ["1", "0", "12", "-a"])
def test_an_id_that_could_read_as_an_index_is_refused(wid):
    from orch.widgets.blocks import make_block
    from orch.widgets.validate import validate
    b = make_block("Findings", 1, json.dumps({"type": "text", "text": "a", "id": wid}))
    assert [p.code for p in validate(b, None)] == ["widget-schema"]


def test_inlined_and_embedded_bytes_are_hashed_as_read(ws, put, monkeypatch):
    """A digest check that still says "ok" (a cached digest, or a check before a swap) never lets other bytes into a
    frame or a standalone document: what is embedded is hashed from the very bytes embedded."""
    import hashlib

    from orch.core import artifacts as core_artifacts
    from orch.widgets import Ctx, frames, render_document
    from orch.widgets.blocks import make_block
    tid = put("in-progress")
    folder = ws.artifacts_dir / tid
    folder.mkdir(parents=True)
    (folder / "a.png").write_bytes(b"\x89PNG-one")
    pin = hashlib.sha256(b"\x89PNG-one").hexdigest()
    (folder / "a.png").write_bytes(b"\x89PNG-two")
    monkeypatch.setattr(core_artifacts, "file_sha256", lambda path: pin)  # every cached check says "unchanged"
    data = frames.inline_images(ws, tid, {"img": {"path": f"artifacts/{tid}/a.png", "sha256": pin}})
    assert not isinstance(data["img"], str)  # not inlined
    t = store.load(ws, tid)[1]
    b = make_block("Findings", 1, json.dumps({"type": "compare", "before": {"path": "artifact:a.png", "sha256": pin},
                                             "after": {"path": "artifact:a.png", "sha256": pin}}))
    doc = render_document(b, Ctx(ticket=t, ws=ws))
    import base64
    assert base64.b64encode(b"\x89PNG-two").decode() not in doc and "data:image/png" not in doc
