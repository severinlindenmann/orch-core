"""#213: `orch new --from X --epic E` and the default of --from to the source's epic."""
import pytest

from orch.cli import run
from orch.core import epics, store
from orch.errors import ValidationError


def _epic(aops, title="Billing"):
    return aops.new(title, type="epic")


def _load(ws, tid):
    return store.load(ws, tid)[1]


def test_from_and_epic_together(ws, aops):
    e = _epic(aops)
    src = aops.new("source")
    c = aops.new("child", from_ref=src.id, epic=e.id)
    t = _load(ws, c.id)
    assert t.meta["parent"] == e.id
    assert "Follow-up from " + src.id in t.sections["Context"]
    assert _load(ws, src.id).meta["follow_ups"] == [c.id]
    assert epics.parent_epic(ws, t).id == e.id
    created = [ev for ev in store_events(ws, c.id) if ev["kind"] == "ticket.created"][0]["data"]
    assert created["from"] == src.id and created["epic"] == e.id


def store_events(ws, tid):
    from orch.core.events import read_events
    return [{"kind": ev.kind, "data": ev.data} for ev in read_events(ws, tid)]


def test_from_alone_defaults_to_source_epic(ws, aops):
    e = _epic(aops)
    src = aops.new("source", epic=e.id)
    c = aops.new("child", from_ref=src.id)
    assert _load(ws, c.id).meta["parent"] == e.id
    assert _load(ws, src.id).meta["follow_ups"] == [c.id]
    assert aops.notices and e.id in aops.notices[0] and src.id in aops.notices[0]


def test_no_epic_opts_out(ws, aops):
    e = _epic(aops)
    src = aops.new("source", epic=e.id)
    c = aops.new("child", from_ref=src.id, no_epic=True)
    assert _load(ws, c.id).meta["parent"] == src.id
    assert not aops.notices


def test_no_default_when_source_has_no_epic_or_new_type_is_epic(ws, aops):
    src = aops.new("source")
    assert _load(ws, aops.new("c", from_ref=src.id).id).meta["parent"] == src.id
    e = _epic(aops)
    in_epic = aops.new("in epic", epic=e.id)
    top = aops.new("top", type="epic", from_ref=in_epic.id)
    assert _load(ws, top.id).meta["parent"] == in_epic.id


def test_no_default_when_source_epic_is_done(ws, aops, hops):
    e = _epic(aops)
    src = aops.new("source", epic=e.id)
    p = store.resolve(ws, e.id).path
    t = _load(ws, e.id)
    t.meta["status"] = "done"
    store.save(ws, t, p)
    c = aops.new("child", from_ref=src.id)
    assert _load(ws, c.id).meta["parent"] == src.id


def test_explicit_epic_must_be_valid_with_from(ws, aops):
    src = aops.new("source")
    other = aops.new("not an epic")
    with pytest.raises(ValidationError, match="not an epic"):
        aops.new("x", from_ref=src.id, epic=other.id)


def test_cli_both_and_notice(ws_root, monkeypatch, capsys):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setenv("ORCH_SESSION", "s-1")
    assert run(["new", "--title", "E", "--type", "epic"]) == 0
    assert run(["new", "--title", "S", "--epic", "L-0001"]) == 0
    capsys.readouterr()
    assert run(["new", "--title", "C", "--from", "L-0002"]) == 0
    err = capsys.readouterr().err
    assert "created in epic L-0001 (from L-0002's epic)" in err
    assert run(["new", "--title", "D", "--from", "L-0002", "--epic", "L-0001"]) == 0
    capsys.readouterr()
    assert run(["new", "--title", "F", "--from", "L-0002", "--no-epic"]) == 0
