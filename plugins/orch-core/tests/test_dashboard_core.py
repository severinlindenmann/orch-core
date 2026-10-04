import pytest

from orch.config.load import DEFAULTS, validate_schema, deep_merge
from orch.core import store
from orch.core.events import Actor, read_events
from orch.core.lifecycle import allowed_targets
from orch.core.ops import Ops
from orch.core.protect import protected_changes
from orch.core.query import open_blockers
from orch.errors import HumanOnlyError, ValidationError

DASH = Actor("human", "you", "dashboard")


def test_protected_changes():
    old = {"id": "L-1", "status": "open", "gates": {}, "claim": {}, "questions": [{"id": "Q1", "answer": None}]}
    assert protected_changes(old, dict(old)) == []
    assert protected_changes(old, {**old, "status": "done"}) == ["status"]
    assert protected_changes(old, {**old, "questions": [{"id": "Q1", "answer": "A"}]}) == ["question answers"]
    assert protected_changes(old, {**old, "title": "new"}) == []


def test_open_blockers(ws, put):
    done = put("done")
    busy = put("in-progress")
    tid = put("open", blocked_by=[done, busy, "L-0999"])
    _, t = store.load(ws, tid)
    assert open_blockers(ws, t) == [busy, "L-0999"]


def test_allowed_targets(ws, put, human, agent):
    _, t = store.load(ws, put("in-progress", sections={"Verification": "ok", "Tasks": "- [x] T1 the work"}, size="xs"))
    assert allowed_targets(t, human, plan_skip_sizes=("xs",)) == ["backlog", "testing"]
    assert allowed_targets(t, agent, plan_skip_sizes=("xs",)) == ["testing"]
    _, t = store.load(ws, put("testing"))
    assert allowed_targets(t, human, plan_skip_sizes=("xs",)) == ["backlog"]  # done/in-progress only via verdict


def test_replace_raw(ws, put, aops):
    tid = put("backlog")
    ops = Ops(ws, DASH)
    path = store.resolve(ws, tid).path
    mtime = path.stat().st_mtime_ns
    text = path.read_text(encoding="utf-8") + "\n## Ask\n\nRewritten by the human.\n"
    t = ops.replace_raw(tid, text, mtime)
    assert t.section("Ask") == "Rewritten by the human." and "[you] edited the ticket file" in t.section("Log")
    assert read_events(ws, tid)[-1].data == {"raw": True}
    with pytest.raises(ValidationError, match="changed since"):
        ops.replace_raw(tid, text, mtime)  # stale mtime
    fresh = store.resolve(ws, tid).path
    with pytest.raises(ValidationError, match="status"):
        ops.replace_raw(tid, fresh.read_text(encoding="utf-8").replace("status: backlog", "status: open"))
    with pytest.raises(ValidationError, match="id"):
        ops.replace_raw(tid, fresh.read_text(encoding="utf-8").replace(f"id: {tid}", "id: L-0999"))
    with pytest.raises(HumanOnlyError):
        aops.replace_raw(tid, fresh.read_text(encoding="utf-8"))


def test_pull_seconds_config():
    assert DEFAULTS["dashboard"]["pull_seconds"] == 60
    assert validate_schema(deep_merge(DEFAULTS, {"customer": "x", "dashboard": {"pull_seconds": 1}}))
