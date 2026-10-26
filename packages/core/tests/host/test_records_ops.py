"""H11e host ops (cap ``records/1``): ``records.snapshot`` names an immutable manifest, ``records.export_check`` is the
convenience-readers rule for a push, ``records.remote_private`` is the owner's one exact-URL mark."""

from __future__ import annotations

import json
import os

import pytest

from orch import records as rec
from orch.host import convenience
from orch.host import records as host_records
from tests.host.dispatch_helpers import err
from tests.host.live_helpers import ok as ok_
from tests.host.live_helpers import world
from tests.host.test_dispatch_matrix import as_agent_class

SHA = "a" * 40
URL_A = "https://example.org/me/records-a.git"
URL_B = "https://example.org/me/records-b.git"
REF = "refs/heads/main"


def ok(res):
    return ok_(res)["data"]


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


@pytest.fixture
def wq(tmp_path):
    (tmp_path / "q").mkdir()
    s = world(tmp_path / "q", map_to="quinn")
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


def snapshot(c, **args):
    data = ok(c.req("records.snapshot", args))
    files = list(data["files"])
    while data["next_page"]:
        data2 = ok(c.req("records.snapshot", {"page": data["next_page"]}))
        assert data2["manifest"] == data["manifest"]
        files += data2["files"]
        data = data2
    return data, files


def test_cap_and_ops_are_in_the_welcome(w):
    welcome = w.client().welcome
    assert "records/1" in welcome["caps"]
    for op in host_records.RECORDS_OPS:
        assert welcome["ops"][op] == 1


def test_snapshot_names_every_record_and_never_state(w):
    w.ticket("one")
    c = w.client()
    data, files = snapshot(c)
    paths = [f["path"] for f in files]
    assert "events/workspace.jsonl" in paths and "config.json" in paths
    assert any(p.endswith("/events.jsonl") and p.startswith("tickets/") for p in paths)
    assert all(rec.is_record_path(p) for p in paths)
    assert not any(p.startswith(".state") for p in paths)
    entries = [rec.Entry(f["path"], f["size"], f["sha256"]) for f in files]
    assert data["manifest"] == rec.manifest_id(w.wid, data["ws_seq"], data["heads"], entries)
    root = os.path.join(w.lab.root, ".orch")  # record paths are relative to the store
    for f in files:
        with open(os.path.join(root, f["path"]), "rb") as fh:
            assert len(fh.read()) >= f["size"]


def test_snapshot_pages_belong_to_one_manifest(w, monkeypatch):
    monkeypatch.setattr(rec, "PAGE_FILES", 2)
    w.ticket("one")
    c = w.client()
    first = ok(c.req("records.snapshot", {}))
    assert first["next_page"] and len(first["files"]) == 2
    w.ticket("two")  # a new cut does not change the pages of the first one
    data, files = snapshot(c)
    assert data["manifest"] != first["manifest"]
    second = ok(c.req("records.snapshot", {"page": first["next_page"]}))
    assert second["manifest"] == first["manifest"]


def test_a_page_of_another_principal_or_unknown_manifest_is_expired(w, monkeypatch):
    monkeypatch.setattr(rec, "PAGE_FILES", 1)
    c = w.client()
    first = ok(c.req("records.snapshot", {}))
    assert err(c.req("records.snapshot", {"page": "m_" + "0" * 64 + ".1"})) == "page.expired"
    held = w.service.records_state["manifests"][first["manifest"]]
    held["principal"] = "uid:999999"
    assert err(c.req("records.snapshot", {"page": first["next_page"]})) == "page.expired"


def test_snapshot_refuses_a_file_over_the_limit(w, monkeypatch):
    monkeypatch.setattr(rec, "FILE_MAX", 10)
    assert err(w.client().req("records.snapshot", {})) == "records.too_large"


def test_snapshot_refuses_a_symlinked_record(w, tmp_path):
    w.ticket("one")
    target = tmp_path / "elsewhere"
    target.write_text("x")
    os.symlink(target, os.path.join(w.lab.root, ".orch", "AGENTS.orch.md"))
    assert err(w.client().req("records.snapshot", {})) == "records.unsafe_tree"


def test_records_ops_are_p_only_and_manager_only(w, wq):
    as_agent_class(w)
    for op, args in (
        ("records.snapshot", {}),
        ("records.export_check", {"sha": SHA, "url": URL_A, "ref": REF, "history_convenience": False}),
        ("records.remote_private", {"url": URL_A, "on": True}),
    ):
        assert err(w.client().req(op, args, idem=True)) == "op.not_allowed"
        assert err(w.client().req(op, args, grant=w.grant, idem=True)) == "op.not_allowed"
        assert err(wq.client().req(op, args, idem=True)) == "op.not_allowed"  # quinn is a plain member


def test_export_check_without_convenience_is_allowed_and_audited(w):
    data = ok(
        w.client().req("records.export_check", {"sha": SHA, "url": URL_A, "ref": REF, "history_convenience": False})
    )
    assert data["policy"] == "none" and data["convenience"] is False
    assert data["sha"] == SHA and data["url"] == URL_A and data["ref"] == REF and data["check"].startswith("x_")
    doc = host_records.read_record(w.lab.state, w.wid, os.getuid())
    assert doc["exports"][-1]["url"] == URL_A and doc["exports"][-1]["policy"] == "none"


def test_export_check_with_convenience_needs_the_exact_private_url(w):
    convenience.set_enabled(w.lab.state, w.wid, True, os.getuid())
    c = w.client()
    args = {"sha": SHA, "url": URL_A, "ref": REF, "history_convenience": False}
    assert err(c.req("records.export_check", args)) == "sign.convenience_readers"
    marked = ok(c.req("records.remote_private", {"url": URL_A, "on": True}, idem=True))
    assert marked["private_remote"]["url"] == URL_A and "trusts your word" in marked["text"]
    assert ok(c.req("records.export_check", args))["policy"] == "private_remote"
    # the mark for URL A does not allow URL B (a changed pushurl, insteadOf or remote URL resolves to another string)
    res = c.req("records.export_check", {**args, "url": URL_B})
    assert err(res) == "sign.convenience_readers" and "changed" in res["error"]["message"]
    assert err(c.req("records.export_check", {**args, "url": URL_A + "/"})) == "sign.convenience_readers"
    ok(c.req("records.remote_private", {"on": False}, idem=True))
    assert err(c.req("records.export_check", args)) == "sign.convenience_readers"
    doc = host_records.read_record(w.lab.state, w.wid, os.getuid())
    assert [e["policy"] for e in doc["exports"]] == ["refused", "private_remote", "refused", "refused", "refused"]


def test_export_check_with_convenience_events_in_history(w, monkeypatch):
    monkeypatch.setattr(host_records, "has_convenience_events", lambda store: True)
    e = err(
        w.client().req("records.export_check", {"sha": SHA, "url": URL_A, "ref": REF, "history_convenience": False})
    )
    assert e == "sign.convenience_readers"


def test_the_pushed_history_report_refuses_that_push_only_and_never_sets_the_mark(w):
    """#441 (Opus #454 M1): the tool's report refuses the push it belongs to. Only the host's own observations write
    the durable mark, so a report (true, false or a lie) can never block member.add for good."""
    from orch.ops.readers import seen_path

    c = w.client()
    args = {"sha": SHA, "url": URL_A, "ref": REF, "history_convenience": True}
    res = c.req("records.export_check", args)
    assert err(res) == "sign.convenience_readers" and "convenience-signed events" in res["error"]["message"]
    assert not seen_path(w.lab.state, w.wid).exists()
    assert ok(c.req("records.export_check", {**args, "history_convenience": False}))["policy"] == "none"
    ok(c.req("records.remote_private", {"url": URL_A, "on": True}, idem=True))
    data = ok(c.req("records.export_check", args))
    assert data["policy"] == "private_remote" and data["convenience"] is True


def test_the_mark_survives_a_restore(w, monkeypatch):
    """An owner repair (abandon_tail, then restore) drops the convenience events from the merged history: an existing
    workspace's history sets the mark at the first check, and the mark keeps refusing after the restore."""
    from orch.ops.readers import seen_path

    c = w.client()
    args = {"sha": SHA, "url": URL_A, "ref": REF, "history_convenience": False}
    monkeypatch.setattr(host_records, "has_convenience_events", lambda store: True)
    assert err(c.req("records.export_check", args)) == "sign.convenience_readers"
    assert seen_path(w.lab.state, w.wid).exists()
    monkeypatch.setattr(host_records, "has_convenience_events", lambda store: False)  # the restore
    assert err(c.req("records.export_check", args)) == "sign.convenience_readers"


def test_export_check_needs_the_history_report(w):
    assert err(w.client().req("records.export_check", {"sha": SHA, "url": URL_A, "ref": REF})) == "invalid.input"


def test_export_check_refuses_bad_input(w):
    c = w.client()
    for bad in (
        {"sha": "xyz", "url": URL_A, "ref": REF},
        {"sha": SHA, "url": URL_A, "ref": "refs/tags/v1"},
        {"sha": SHA, "url": "https://x\n", "ref": REF},
    ):
        assert err(c.req("records.export_check", bad)) == "invalid.input"
    assert err(c.req("records.remote_private", {"on": True})) == "invalid.input"


# --- the mark is an owner-only write (Opus #430 finding 4) and refuses URLs that can hide a secret (finding 6) ------


def test_the_mark_needs_an_idem_and_replays_its_outcome(w):
    c = w.client()
    assert err(c.req("records.remote_private", {"url": URL_A, "on": True})) == "idem.required"
    assert err(c.req("records.remote_private", {"url": URL_A, "on": True}, dry_run=True)) == "invalid.input"
    first = ok_(c.req("records.remote_private", {"url": URL_A, "on": True}, idem="a_01J9ZP0000000000000000000C"))
    again = ok_(c.req("records.remote_private", {"url": URL_A, "on": True}, idem="a_01J9ZP0000000000000000000C"))
    assert again["duplicate"] is True and again["data"] == first["data"]
    other = c.req("records.remote_private", {"url": URL_B, "on": True}, idem="a_01J9ZP0000000000000000000C")
    assert err(other) == "idem.conflict"
    doc = host_records.read_record(w.lab.state, w.wid, os.getuid())
    assert doc["private_remote"]["url"] == URL_A
    assert [(m["action"], m["url"], m["by"]) for m in doc["marks"]] == [("set", URL_A, w.lab.owner)]
    ok(c.req("records.remote_private", {"on": False}, idem=True))
    doc = host_records.read_record(w.lab.state, w.wid, os.getuid())
    assert doc["private_remote"] is None and doc["marks"][-1]["action"] == "clear"


def test_a_maintainer_reads_and_checks_but_never_sets_the_mark(w, monkeypatch):
    import dataclasses

    real = host_records.Viewer.of
    monkeypatch.setattr(host_records.Viewer, "of", lambda call: dataclasses.replace(real(call), role="maintainer"))
    c = w.client()
    ok(c.req("records.snapshot", {}))
    ok(c.req("records.export_check", {"sha": SHA, "url": URL_A, "ref": REF, "history_convenience": False}))
    assert err(c.req("records.remote_private", {"url": URL_A, "on": True}, idem=True)) == "op.not_allowed"
    assert err(c.req("records.remote_private", {"on": False}, idem=True)) == "op.not_allowed"
    assert host_records.read_record(w.lab.state, w.wid, os.getuid())["private_remote"] is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/r.git?token=SECRET123",
        "https://example.org/r.git#SECRET123",
        "https://SECRET123@example.org/r.git",
    ],
)
def test_urls_that_can_hide_a_secret_are_refused_and_never_stored(w, url):
    c = w.client()
    for res in (
        c.req("records.remote_private", {"url": url, "on": True}, idem=True),
        c.req("records.export_check", {"sha": SHA, "url": url, "ref": REF, "history_convenience": False}),
    ):
        assert err(res) == "invalid.input" and "SECRET123" not in json.dumps(res)
    path = host_records.record_path(w.lab.state, w.wid)
    assert not path.exists() or "SECRET123" not in path.read_text()


# --- records.status and records.note (H11e part 2) ------------------------------------------------------------------


def test_status_before_any_note_counts_everything_pending(w):
    w.ticket("one")
    data = ok(w.client().req("records.status", {}))
    assert data["configured"] is False and data["last"] is None and data["remote"] is None and data["repo"] is None
    pend = data["pending"]
    assert pend["workspace_events"] > 0 and pend["events"] > pend["workspace_events"]
    assert pend["tickets"] and pend["tickets"][0]["key"] == "DEMO-0001" and pend["more"] is False


def test_note_after_a_commit_makes_pending_exact(w):
    w.ticket("one")
    c = w.client()
    snap = ok(c.req("records.snapshot", {}))
    note = {"manifest": snap["manifest"], "commit": SHA, "pushed": False, "repo": {"state": "ok"},
            "auto": {"on": True, "every_min": 15, "push": False}}  # fmt: skip
    ok(c.req("records.note", note, idem=True))
    data = ok(c.req("records.status", {}))
    assert data["configured"] and data["last"]["commit"] == SHA and data["last"]["ws_seq"] == snap["ws_seq"]
    assert data["last"]["pushed_at"] is None and data["repo"] == {"state": "ok"}
    assert data["pending"] == {"events": 0, "tickets": [], "more": False, "workspace_events": 0}
    assert data["auto"] == {"on": True, "every_min": 15, "push": False}
    w.ticket("two")
    pend = ok(c.req("records.status", {}))["pending"]
    assert pend["events"] >= 1 and [t["key"] for t in pend["tickets"]] and pend["workspace_events"] >= 0
    ok(c.req("records.note", {"commit": SHA, "pushed": True, "url": URL_A}, idem=True))
    data = ok(c.req("records.status", {}))
    assert data["last"]["pushed_at"] is not None
    assert data["remote"] == {
        "url": URL_A,
        "private": False,
        "export": "ok",
        "current_url": None,
        "current_private": None,
        "matches_last": None,
    }


def test_status_remote_says_blocked_without_the_mark(w):
    c = w.client()
    ok(c.req("records.note", {"commit": SHA, "pushed": True, "url": URL_A}, idem=True))
    convenience.set_enabled(w.lab.state, w.wid, True, os.getuid())
    assert ok(c.req("records.status", {}))["remote"]["export"] == "blocked_convenience"
    ok(c.req("records.remote_private", {"url": URL_A, "on": True}, idem=True))
    assert ok(c.req("records.status", {}))["remote"] == {
        "url": URL_A,
        "private": True,
        "export": "ok",
        "current_url": None,
        "current_private": None,
        "matches_last": None,
    }


def test_note_needs_an_idem_a_held_manifest_and_a_commit(w):
    c = w.client()
    assert err(c.req("records.note", {"pushed": False})) == "idem.required"
    bad = {"manifest": "m_" + "1" * 64, "commit": SHA, "pushed": False}
    assert err(c.req("records.note", bad, idem=True)) == "page.expired"
    assert err(c.req("records.note", {"pushed": True}, idem=True)) == "invalid.input"
    assert err(c.req("records.note", {"pushed": False, "repo": {"state": "weird"}}, idem=True)) == "invalid.input"


def test_status_never_names_a_ticket_the_caller_cannot_see(w):
    uid, key = w.ticket("secret")
    w.restrict(uid, [w.quinn.ref])
    data = ok(w.client().req("records.status", {}))
    assert key not in json.dumps(data["pending"]["tickets"]) and data["pending"]["events"] > 0


def test_a_maintainer_may_note_and_read_status(w, monkeypatch):
    import dataclasses

    real = host_records.Viewer.of
    monkeypatch.setattr(host_records.Viewer, "of", lambda call: dataclasses.replace(real(call), role="maintainer"))
    c = w.client()
    ok(c.req("records.note", {"pushed": False, "repo": {"state": "ok"}}, idem=True))
    assert ok(c.req("records.status", {}))["repo"] == {"state": "ok"}


# --- configured: the workspace's records repo exists ----------------------------------------------------------------


def test_configured_means_the_records_repo_exists_for_the_workspace(w, monkeypatch):
    import dataclasses

    c = w.client()
    ok(c.req("records.note", {"pushed": False, "auto": None}, idem=True))  # a note, but no repo report
    assert ok(c.req("records.status", {}))["configured"] is False
    ok(c.req("records.note", {"pushed": False, "repo": {"state": "missing"}}, idem=True))
    assert ok(c.req("records.status", {}))["configured"] is False
    ok(c.req("records.note", {"pushed": False, "repo": {"state": "ok"}}, idem=True))
    assert ok(c.req("records.status", {}))["configured"] is True
    # per workspace: another manager with no note of their own sees it too
    real = host_records.Viewer.of
    monkeypatch.setattr(
        host_records.Viewer, "of", lambda call: dataclasses.replace(real(call), role="maintainer", person="p_other")
    )
    data = ok(w.client().req("records.status", {}))
    assert data["configured"] is True and data["last"] is None
    ok(w.client().req("records.note", {"pushed": False, "repo": {"state": "not_dedicated"}}, idem=True))
    assert ok(w.client().req("records.status", {}))["configured"] is False  # the latest report wins


def test_configured_reads_a_record_from_before_the_workspace_report(w):
    doc = host_records.read_record(w.lab.state, w.wid, os.getuid())
    doc["lasts"] = {"p_x": {"repo": {"state": "index_differs"}}}
    host_records._write_record(w.lab.state, w.wid, os.getuid(), doc)  # noqa: SLF001
    assert ok(w.client().req("records.status", {}))["configured"] is True


# --- the current push URL for the dashboard's confirmation ----------------------------------------------------------


def test_status_names_the_current_push_url_its_mark_and_whether_it_changed(w, monkeypatch):
    import dataclasses

    c = w.client()
    current = "ssh://git@example.org:2222/me/records-a.git"
    data = ok(c.req("records.status", {"current_url": current}))
    assert data["remote"] == {
        "url": None, "private": False, "export": "ok",
        "current_url": "ssh://example.org:2222/me/records-a.git", "current_private": False, "matches_last": False,
    }  # fmt: skip
    ok(c.req("records.note", {"commit": SHA, "pushed": True, "url": current}, idem=True))
    ok(c.req("records.remote_private", {"url": current, "on": True}, idem=True))
    remote = ok(c.req("records.status", {"current_url": current}))["remote"]
    assert remote["current_private"] is True and remote["matches_last"] is True
    remote = ok(c.req("records.status", {"current_url": URL_B}))["remote"]
    assert remote["current_private"] is False and remote["matches_last"] is False and remote["private"] is True
    convenience.set_enabled(w.lab.state, w.wid, True, os.getuid())
    # a maintainer sees the host only
    real = host_records.Viewer.of
    monkeypatch.setattr(host_records.Viewer, "of", lambda call: dataclasses.replace(real(call), role="maintainer"))
    remote = ok(w.client().req("records.status", {"current_url": current}))["remote"]
    assert remote["current_url"] == "example.org:2222" and remote["current_private"] is True


def test_status_refuses_a_current_url_that_can_hide_a_secret(w):
    res = w.client().req("records.status", {"current_url": "https://tok@example.org/r.git"})
    assert err(res) == "invalid.input" and "tok@" not in json.dumps(res)


def test_display_url_strips_userinfo_and_shows_non_owners_the_host():
    d = host_records.display_url
    assert d("https://example.org/me/r.git", True) == "https://example.org/me/r.git"
    assert d("ssh://git@example.org/me/r.git", True) == "ssh://example.org/me/r.git"
    assert d("ssh://git@example.org/me/r.git", False) == "example.org"
    assert d("git@example.org:me/r.git", True) == "example.org:me/r.git"
    assert d("git@example.org:me/r.git", False) == "example.org"
    assert d("file:///home/me/r.git", True) == "file:///home/me/r.git"
    assert d("file:///home/me/r.git", False) == "local"
    assert d("/home/me/r.git", False) == "local"
