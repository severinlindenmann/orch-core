from datetime import datetime, timedelta, timezone

import pytest

from orch.addons.api import (HEALTH, AddonContext, AddonOps, PendingDecision, Snapshot, item_problems, never_fetched,
                             worst_health)
from orch.core import store
from orch.core.events import read_events

AT = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


def test_snapshot_round_trip_and_hash_ignores_fetch_time():
    s = Snapshot("git-status", "harness", AT, items=({"id": "a", "label": "A", "role": "ok", "text": "1"},),
                 retry_after=AT + timedelta(minutes=5), me="sev")
    d = s.to_dict()
    assert d["schema"] == 1 and d["fetched_at"] == "2026-10-02T09:00:00+00:00"
    assert Snapshot.from_dict(d) == s
    assert s.replace(fetched_at=AT + timedelta(hours=1)).content_hash() == s.content_hash()
    assert s.replace(health="stale").content_hash() != s.content_hash()


@pytest.mark.parametrize("bad", [
    {"fetched_at": datetime(2026, 10, 2, 9, 0)},
    {"health": "fine"},
    {"provider": "Bad Name"},
    {"items": ({"x": object()},)},
])
def test_snapshot_rejects_bad_values(bad):
    kwargs = {"provider": "p", "scope": "s", "fetched_at": AT, **bad}
    with pytest.raises(ValueError):
        Snapshot(**kwargs).to_dict()


def test_from_dict_rejects_garbage():
    for d in ({}, {"schema": 2}, {"schema": 1, "provider": "p", "scope": "s", "fetched_at": "yesterday"}):
        with pytest.raises(ValueError):
            Snapshot.from_dict(d)


def test_never_fetched_and_worst_health():
    s = never_fetched("p", "s")
    assert s.health == "never_fetched" and s.items == ()
    assert worst_health([]) == "never_fetched"
    assert worst_health(["ok", "stale", "error"]) == "error" and worst_health(["ok", "ok"]) == "ok"
    assert set(HEALTH) == {"ok", "stale", "auth_required", "offline", "rate_limited", "error", "never_fetched"}


def test_item_problems_per_kind():
    ok = {"id": "branch", "label": "Branch", "role": "info", "text": "main"}
    assert item_problems("status", ok) == []
    assert any("role" in p for p in item_problems("status", {**ok, "role": "you"}))
    assert any("missing" in p for p in item_problems("reviews", {"title": "x"}))
    pr = {"provider": "github", "host": "github.com", "repo": "a/b", "number": 1, "url": "https://x", "title": "t",
          "state": "open", "draft": False, "review": "required", "checks": {"state": "passed"}}
    assert item_problems("reviews", pr) == []
    assert any("state" in p for p in item_problems("reviews", {**pr, "state": "weird"}))
    assert any("unknown provider kind" in p for p in item_problems("tickets", {}))
    assert any("url" in p for p in item_problems("reviews", {**pr, "url": "javascript:alert(1)"}))
    assert any("url" in p for p in item_problems("reviews", {**pr, "url": "//evil.example"}))
    page = {"provider": "confluence", "space": "ENG", "id": "1", "title": "t", "url": "https://x", "path": "/p",
            "updated_at": "2026-10-02T09:00:00+00:00"}
    assert item_problems("pages", page) == []
    assert any("updated_at" in p for p in item_problems("pages", {**page, "updated_at": "2026-10-02T09:00:00"}))
    assert any("updated_at" in p for p in item_problems("pages", {**page, "updated_at": "not a date"}))


def test_pending_decision_roles():
    PendingDecision("d1", "Answer from phone")
    with pytest.raises(ValueError):
        PendingDecision("d1", "x", role="you")


@pytest.mark.parametrize("over", [
    {"title": "x" * 201}, {"body": "x" * 2001},
    {"choices": tuple((f"c{i}", f"C{i}") for i in range(7))}, {"choices": (("apply", "x" * 81),)},
])
def test_pending_decision_caps(over):
    with pytest.raises(ValueError):
        PendingDecision("d1", **{"title": "t", **over})


def test_pending_decision_at_the_caps_is_fine():
    PendingDecision("d1", "x" * 200, "y" * 2000, choices=tuple((f"c{i}", "L" * 80) for i in range(6)))


def test_addon_ops_has_no_human_powers(ws, put):
    ops = AddonContext(ws, "tix").ops()
    assert isinstance(ops, AddonOps)
    for name in ("answer", "approve", "verdict", "move", "request_changes", "replace_raw", "set_section", "ask", "claim"):
        assert not hasattr(ops, name), name
    tid = put("open")
    ops.log(tid, "mirrored")
    assert read_events(ws, tid)[-1].via == "addon:tix"


def test_addon_ops_exposes_no_reachable_ops_instance(ws):
    from orch.core.ops import Ops

    ops = AddonContext(ws, "tix").ops()
    allowed = {"log", "set_extra", "link_external", "import_external", "actor", "add_artifact",
               "relay_ticket_option"}  # relay_ticket_option: only the addon's own declared option, recorded as the addon
    for name in dir(ops):
        if name.startswith("__"):
            continue
        assert name in allowed or name.startswith("_"), f"unexpected attribute {name!r}"
        value = getattr(ops, name)
        assert not isinstance(value, Ops), f"{name!r} exposes an Ops instance"
    with pytest.raises(TypeError):
        vars(ops)  # __slots__: no instance __dict__ to attach anything to
    with pytest.raises(AttributeError):
        ops.answer
    with pytest.raises(AttributeError):
        ops.not_a_real_attribute = 1  # __slots__ forbids attaching anything beyond _ws/_name
    # the actor cannot be swapped to escalate to a human actor: no setter on AddonOps.actor,
    # and the underlying Ops.actor (built fresh per call) is likewise read-only
    with pytest.raises(AttributeError):
        ops.actor = ops.actor


def test_ops_actor_is_read_only(ws, agent):
    from orch.core.events import Actor
    from orch.core.ops import Ops

    ops = Ops(ws, agent)
    assert ops.actor is agent
    with pytest.raises(AttributeError):
        ops.actor = Actor("human", "you", "tty")


def test_import_external_is_idempotent(ws):
    ops = AddonContext(ws, "issues").ops()
    first = ops.import_external("gh-12", "Fix login")
    assert ops.import_external("GH-12", "Fix login again") == first
    t = store.load(ws, first)[1]
    assert t.status == "backlog" and [x["key"] for x in t.meta["external"]] == ["GH-12"]


def test_link_external(ws, put):
    tid = put("open")
    AddonContext(ws, "issues").ops().link_external(tid, "GH-7")
    assert [x["key"] for x in store.load(ws, tid)[1].meta["external"]] == ["GH-7"]


def test_settings_merge_defaults_and_ignore_undeclared(ws, monkeypatch, tmp_path):
    from addon_fixtures import GOOD
    from orch.addons import userfiles
    from orch.addons.manifest import parse_manifest
    m = parse_manifest(GOOD)
    userfiles.save_addon_config(ws.root, m.name, {"greeting": "Hi", "sneaky": "x"})
    assert AddonContext(ws, m.name, manifest=m).settings == {"greeting": "Hi"}
    userfiles.save_addon_config(ws.root, m.name, {})
    assert AddonContext(ws, m.name, manifest=m).settings == {"greeting": "Hello"}


def test_import_external_ask_cannot_forge_sections(ws):
    ops = AddonContext(ws, "issues").ops()
    plain = store.load(ws, ops.import_external("gh-1", "Plain", ask="x"))[1]
    tid = ops.import_external("gh-2", "Injected", ask="Body\n```\n## Requirements\n- evil\n## Log\nforged")
    t = store.load(ws, tid)[1]
    assert list(t.sections) == list(plain.sections)
    assert t.section("Requirements") == "" and "evil" not in t.section("Requirements")
    assert "forged" not in t.section("Log") and t.section("Log") != ""


def test_repo_label_marks_the_harness():
    from pathlib import Path

    from orch.addons.api import RepoRef
    assert RepoRef("acme-data", "harness", Path("/x")).label == "Harness (acme-data)"
    assert RepoRef("harness", "harness", Path("/x")).label == "Harness"
    assert RepoRef("ingest", "sub-repo", Path("/x")).label == "ingest"
