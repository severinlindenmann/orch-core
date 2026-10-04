"""`orch widget repin`: re-pin drifted template blocks through the section write path (docs/widgets.md)."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from orch.core import store  # noqa: E402
from orch.core.epics import verdict_hash  # noqa: E402
from orch.errors import ValidationError  # noqa: E402

from test_widget_verdict import _block, _template, _testing, cli  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("html_on")


def _drift(ws):
    _template(ws, body="<p id=g>gauge v1, edited</p><script>orch.ready()</script>")


def _section(ws, tid, name):
    return store.load(ws, tid)[1].section(name)


def test_a_drifted_block_is_repinned_and_the_check_is_clean(ws, aops, working, cli):
    _template(ws)
    aops.set_section(working, "Context", f"intro\n\n{_block(ws)}")
    _drift(ws)
    assert cli("widget", "check", working)[0] == 5
    code, out, _ = cli("widget", "repin", working, "--block", "g")
    assert code == 0 and "re-pinned" in out and "+" in out
    assert cli("widget", "check", working)[0] == 0
    assert "intro" in _section(ws, working, "Context")


def test_dry_run_writes_nothing(ws, aops, working, cli):
    _template(ws)
    aops.set_section(working, "Context", _block(ws))
    _drift(ws)
    before = _section(ws, working, "Context")
    code, out, _ = cli("widget", "repin", working, "--all", "--dry-run", "--json")
    assert code == 0 and json.loads(out)["repinned"] and _section(ws, working, "Context") == before


def test_a_verdict_section_warns_and_the_old_verdict_reads_stale(ws, aops, hops, working, plan_approved, close_tasks, cli, monkeypatch):
    _template(ws)
    t = _testing(ws, aops, working, plan_approved, close_tasks, f"- AC1: reads 3\n\n{_block(ws)}")
    _drift(ws)
    seen = verdict_hash([t], ws)
    code, out, _ = cli("widget", "repin", working, "--all")
    assert code == 0 and "stale" in out
    assert verdict_hash([store.load(ws, working)[1]], ws) != seen
    monkeypatch.delenv("ORCH_HARNESS")  # the verdict is the human's; only the stale hash is under test
    with pytest.raises(ValidationError, match="changed since you read them"):
        hops.verdict(working, "done", expected_hash=seen)


def test_an_unknown_template_is_refused(ws, aops, working, cli):
    _template(ws)
    aops.set_section(working, "Context", _block(ws))
    (ws.home / "widgets" / "gauge" / "widget.json").unlink()
    before = _section(ws, working, "Context")
    code, _, err = cli("widget", "repin", working, "--block", "g")
    assert code != 0 and "unknown" in err
    assert cli("widget", "repin", working, "--all")[0] == 0
    assert _section(ws, working, "Context") == before


def test_a_block_with_a_broken_file_pin_is_refused(ws, aops, working, cli):
    _template(ws)
    aops.set_section(working, "Context", _block(ws, data={"v": 1, "img": {"path": "artifacts/x/none.png", "sha256": "0" * 64}}))
    _drift(ws)
    before = _section(ws, working, "Context")
    code, _, err = cli("widget", "repin", working, "--block", "g")
    assert code != 0 and _section(ws, working, "Context") == before
