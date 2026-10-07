"""#10: files pinned by a ticket's widget blocks are listed in the Artifacts panel with their pin state, the section
of the block and, only while the pin holds, a link through the read-once `?v=` route (never a filesystem path)."""
from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("fastapi")

F = "```"


def _page(dash, aops, working, tmp_path, body=b"\x89PNG-one", ref="artifact:shot.png"):
    png = tmp_path / "shot.png"
    png.write_bytes(body)
    aops.artifact_add(working, png)
    pin = hashlib.sha256(body).hexdigest()
    block = {"type": "compare", "before": {"path": ref, "sha256": pin}, "after": {"path": ref, "sha256": pin}}
    aops.set_section(working, "Verification", f"- AC1: same\n\n{F}orch\n{json.dumps(block)}\n{F}")
    return pin


def _panel(dash, working):
    html = dash.get(f"/t/{working}").text
    return html.split('id="artifacts"', 1)[1].split("</section>", 1)[0]


def test_a_matching_pin_is_listed_ok_with_its_section_and_a_read_once_link(dash, ws, aops, working, tmp_path):
    pin = _page(dash, aops, working, tmp_path)
    panel = _panel(dash, working)
    assert "Widget files" in panel and "Verification" in panel and "pinned" in panel
    assert f'href="/a/{working}/shot.png?v={pin[:16]}"' in panel
    assert str(ws.home) not in panel
    assert dash.get(f"/a/{working}/shot.png?v={pin[:16]}").content == b"\x89PNG-one"


def test_a_changed_file_is_flagged_and_has_no_widget_link(dash, ws, aops, working, tmp_path):
    pin = _page(dash, aops, working, tmp_path)
    (ws.artifacts_dir / working / "shot.png").write_bytes(b"\x89PNG-two")
    wf = _panel(dash, working).split("widget-files", 1)[1]
    assert "changed since pinned" in wf and "<a " not in wf and pin[:16] not in wf


def test_a_missing_file_is_flagged_gone(dash, ws, aops, working, tmp_path):
    _page(dash, aops, working, tmp_path)
    (ws.artifacts_dir / working / "shot.png").unlink()
    wf = _panel(dash, working).split("widget-files", 1)[1]
    assert "gone" in wf and "<a " not in wf


def test_another_tickets_file_is_missing_and_never_linked(dash, ws, aops, working, tmp_path):
    _page(dash, aops, working, tmp_path, ref="artifacts/B-9999/shot.png")
    wf = _panel(dash, working).split("widget-files", 1)[1]
    assert "gone" in wf and "<a " not in wf and "/a/B-9999" not in wf


def test_the_count_includes_each_file_once(dash, ws, aops, working, tmp_path):
    _page(dash, aops, working, tmp_path)  # shot.png is an artifact and pinned twice in one block
    aops.set_section(working, "Plan", f"x\n\n{F}orch\n" + json.dumps({"type": "compare", "before": {
        "path": "artifact:shot.png", "sha256": hashlib.sha256(b"\x89PNG-one").hexdigest()}}) + f"\n{F}")
    assert 'Files <span class="count">1</span>' in _panel(dash, working)


def test_a_long_ref_wraps(dash, ws, aops, working, tmp_path):
    _page(dash, aops, working, tmp_path, ref="artifact:" + "x" * 120 + ".png")
    assert 'class="artifact-url">artifact:xxx' in _panel(dash, working)
