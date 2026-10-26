"""``quick.list`` in direct mode (H11f, #308): quick tasks are tickets labelled ``quick``; the size limits are display
only. The host's version is tested in tests/host/test_quick.py against the same expectations."""

from __future__ import annotations

import json

import pytest

from tests.ops.test_diff_core import LINKS


@pytest.fixture(autouse=True)
def _quick_on(ws):
    """Quick tasks is a built-in add-on: a workspace without its grant is Off."""
    s = ws.other()
    try:
        ws.tick()
        s.append(
            ws.person_event(
                ws.owner, "workspace", "addon.granted", name="quick", version="0.1.0",
                package_sha256="sha256:" + "0" * 64, capabilities=[], fields={}, sections=[], artifact_kinds=[],
            ),
            log="workspace",
        )  # fmt: skip
    finally:
        s.close()


def rows(cli, *args):
    r = cli("quick", "list", "--json", *args)
    assert r.code == 0, r.err
    return json.loads(r.out)["data"]


def test_quick_list_is_the_tickets_labelled_quick(ws, cli):
    assert cli("new", "plain").code == 0
    assert cli("new", "q one", "--type", "chore", "--label", "quick").code == 0
    assert cli("new", "q two", "--label", "quick,other").code == 0  # any ticket with the label counts
    assert cli("new", "other label", "--label", "other").code == 0
    data = rows(cli)
    assert [r["key"] for r in data["rows"]] == ["DEMO-0003", "DEMO-0002"]
    assert data["total"] == 2 and data["next_page"] is None
    assert data["rows"][0]["quick"] == {"commits": None, "files": None, "over": None}
    assert data["rows"][0]["labels"] == ["other", "quick"]
    r = cli("quick", "list")
    assert r.code == 0 and "DEMO-0003" in r.out and "DEMO-0001" not in r.out


def test_quick_list_pages_and_filters_status(ws, cli):
    for i in range(3):
        assert cli("new", f"q{i}", "--label", "quick").code == 0
    first = rows(cli, "--limit", "2")
    assert [r["key"] for r in first["rows"]] == ["DEMO-0003", "DEMO-0002"] and first["total"] == 3
    second = rows(cli, "--limit", "2", "--page", first["next_page"])
    assert [r["key"] for r in second["rows"]] == ["DEMO-0001"] and second["next_page"] is None
    assert rows(cli, "--status", "done")["rows"] == []
    assert cli("quick", "list", "--page", "bogus").code == 5


def test_counts_and_over_come_from_the_branch_and_are_display_only(ws, cli):
    from tests.ops.test_task_ac import claimed

    path = ws.repo()
    ws.git("branch", "main", "HEAD")
    (path / "impl.txt").write_text("fixed\n")
    ws.git("commit", "-qam", "fix")
    (path / "b.txt").write_text("b\n")
    ws.git("add", "b.txt")
    ws.git("commit", "-qm", "second")
    assert cli("new", "q", "--type", "chore", "--label", "quick").code == 0
    claimed(cli)
    assert cli("set", "1", LINKS).code == 0
    (row,) = rows(cli)["rows"]
    assert row["quick"] == {"commits": 2, "files": 2, "over": True}
    (row,) = rows(cli, "--max-commits", "2", "--max-files", "2")["rows"]
    assert row["quick"]["over"] is False
    assert "over" in cli("quick", "list").out


def test_an_unreadable_branch_gives_null_counts_not_an_error(ws, cli):
    ws.repo()
    assert cli("new", "q", "--label", "quick").code == 0
    assert cli("set", "1", 'links={"repos":["proj"],"branches":{"proj":"nope"}}').code == 0
    (row,) = rows(cli)["rows"]
    assert row["quick"] == {"commits": None, "files": None, "over": None}


def add(cli, *args):
    r = cli("quick", "add", *args)
    return r


def test_quick_add_is_a_chore_ticket_labelled_quick(ws, cli):
    r = cli("quick", "add", "fix the typo", "--json")
    assert r.code == 0, r.err
    doc = json.loads(r.out)
    assert doc["data"] == {
        "key": "DEMO-0001",
        "title": "fix the typo",
        "type": "chore",
        "labels": ["quick"],
        "created": True,
    }
    view = ws.view("DEMO-0001")
    assert (view.type, list(view.fields["labels"])) == ("chore", ["quick"])
    assert [e["type"] for e in ws.events("1")] == ["ticket.created", "ticket.updated"]  # no quick-specific event
    assert [r["key"] for r in rows(cli)["rows"]] == ["DEMO-0001"]
    assert cli("quick", "add", "text mode").out.startswith("ok DEMO-0002 quick.add created")


def test_quick_add_refuses_a_bad_title(ws, cli):
    for bad in ("", "x" * 121, "two\nlines"):
        r = cli("quick", "add", bad)
        assert r.code == 5 and "invalid.input" in r.err, bad
    assert cli("quick", "add").code != 0 and cli("quick", "add", "a", "b").code != 0
    assert "ok list 0" in cli("list").out
    assert cli("quick", "add", "x" * 120).code == 0


def test_a_failure_between_create_and_label_leaves_a_ticket_and_the_retry_labels_it(ws, cli, monkeypatch):
    import orch.cli.quick as q

    real = q._step
    fail = {"on": True}

    def flaky(argv, run):
        if argv[0] == "set" and fail["on"]:
            return 9, {"ok": False, "error": {"code": "lock.busy", "message": "the workspace lock is busy"}}
        return real(argv, run)

    monkeypatch.setattr(q, "_step", flaky)
    r = cli("quick", "add", "half done")
    assert r.code == 9 and "quick.unlabelled" in r.err and "DEMO-0001" in r.err and "retry:true" in r.err
    assert list(ws.view("DEMO-0001").fields["labels"]) == []  # an ordinary chore ticket
    assert rows(cli)["rows"] == []
    fail["on"] = False
    doc = json.loads(cli("quick", "add", "half done", "--json").out)
    assert doc["data"]["key"] == "DEMO-0001" and doc["data"]["created"] is False  # labelled, not duplicated
    assert [r["key"] for r in rows(cli)["rows"]] == ["DEMO-0001"]
    assert cli("list").out.count("half done") == 1


def test_the_retry_never_takes_a_ticket_that_is_not_an_untouched_chore(ws, cli):
    assert cli("new", "same", "--type", "chore", "-m", "has text").code == 0  # a second event
    assert cli("new", "same", "--type", "feature").code == 0  # not a chore
    assert cli("new", "same", "--type", "chore", "--label", "other").code == 0  # labelled already
    doc = json.loads(cli("quick", "add", "same", "--json").out)
    assert doc["data"]["key"] == "DEMO-0004" and doc["data"]["created"] is True


def test_the_retry_adopts_only_the_callers_own_recent_ticket(ws, cli, monkeypatch):
    """Another session's or another grant's untouched chore with the same title is never labelled, in both directions,
    and neither is the caller's own once it is older than ten minutes."""
    import orch.cli.quick as q
    from tests.ops.helpers import OTHER, Cli

    other_session = Cli(ws, ORCH_SESSION=OTHER)
    other_grant = Cli(ws, ORCH_GRANT=ws.narrow_grant(["new", "set", "show", "list"]))
    # their untouched chores are not mine
    assert other_session("new", "same", "--type", "chore").code == 0
    assert other_grant("new", "same", "--type", "chore").code == 0
    doc = json.loads(cli("quick", "add", "same", "--json").out)
    assert doc["data"] == {"key": "DEMO-0003", "title": "same", "type": "chore", "labels": ["quick"], "created": True}
    for key in ("DEMO-0001", "DEMO-0002"):
        assert list(ws.view(key).fields["labels"]) == []
    # and the other direction: my untouched chore is not theirs
    real = q._step
    monkeypatch.setattr(
        q,
        "_step",
        lambda argv, run: (
            (9, {"ok": False, "error": {"code": "lock.busy", "message": "m"}}) if argv[0] == "set" else real(argv, run)
        ),
    )
    r = cli("quick", "add", "mine")
    assert r.code == 9 and "orch set DEMO-0004 labels=quick" in r.err
    monkeypatch.setattr(q, "_step", real)
    doc = json.loads(other_session("quick", "add", "mine", "--json").out)
    assert doc["data"]["key"] == "DEMO-0005" and doc["data"]["created"] is True
    assert list(ws.view("DEMO-0004").fields["labels"]) == []
    # my own, but older than ten minutes: not adopted either
    ws.clock[0] += 16 * 60  # past the ten minutes and past the CLI's own 15-minute dedup of an identical call
    doc = json.loads(cli("quick", "add", "mine", "--json").out)
    assert doc["data"]["key"] == "DEMO-0006" and doc["data"]["created"] is True
    assert list(ws.view("DEMO-0004").fields["labels"]) == []


def test_direct_mode_has_the_same_total_budget_for_the_reader(ws, cli, monkeypatch):
    from types import SimpleNamespace

    ws.repo()
    ws.git("branch", "main", "HEAD")
    for i in range(2):
        assert cli("new", f"q{i}", "--label", "quick").code == 0
        assert cli("set", str(i + 1), LINKS).code == 0
    import orch.ops.commands.quick_list as ql
    import orch.ops.quick as core

    clock = iter(
        [0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0]
    )  # the deadline is set at 0; the second row finds it passed
    monkeypatch.setattr(ql, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(core, "BUDGET_S", 20.0)
    got = rows(cli)["rows"]
    assert [r["quick"]["commits"] is not None for r in got] == [True, False]
