"""#221: `orch close A B C -m why`: every ticket previewed, one typed confirmation ("CLOSE 3"), each close bound to the
file as previewed, skipped tickets reported with the reason. Closing stays human-only."""
import json

import pytest

from orch import actor
from orch.cli import run
from orch.core import store
from orch.core.events import read_events
from orch.core.workspace import Workspace


@pytest.fixture
def switch(monkeypatch, ws_root):
    class Switch:
        def agent(self):
            monkeypatch.setenv("ORCH_HARNESS", "test-agent")
            monkeypatch.setenv("ORCH_SESSION", "s-1")
            monkeypatch.setattr(actor, "is_interactive", lambda: False)

        def human(self, confirm):
            monkeypatch.delenv("ORCH_HARNESS", raising=False)
            monkeypatch.setattr(actor, "is_interactive", lambda: True)
            monkeypatch.setattr("builtins.input", lambda prompt="": confirm)

    s = Switch()
    s.agent()
    return s


def _status(ws_root, tid):
    return store.resolve(Workspace.open(ws_root), tid).status


def test_closes_several_with_one_confirmation(switch, ws, ws_root, put, capsys, monkeypatch):
    ids = [put("open", title=f"junk {i}") for i in range(3)]
    prompts = []
    switch.human("CLOSE 3")
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "CLOSE 3")
    assert run(["close", *ids, "-m", "test spill"]) == 0
    out = capsys.readouterr()
    assert len(prompts) == 1 and "CLOSE 3" in prompts[0]
    assert all(i in out.out and f"junk {n}" in out.out for n, i in enumerate(ids))  # the table lists every ticket
    assert [_status(ws_root, i) for i in ids] == ["done"] * 3
    assert sum(1 for e in read_events(Workspace.open(ws_root)) if e.kind == "ticket.moved") == 3


def test_agent_is_refused_and_nothing_is_closed(switch, ws_root, put, capsys):
    ids = [put("open"), put("open")]
    assert run(["close", *ids, "-m", "x"]) != 0
    assert run(["close", *ids, "-m", "x", "--dry-run"]) != 0
    assert [_status(ws_root, i) for i in ids] == ["open", "open"]


def test_wrong_confirmation_closes_nothing(switch, ws_root, put, capsys):
    ids = [put("open"), put("open")]
    switch.human("L-0001")                      # the single-ticket style answer is not the phrase
    assert run(["close", *ids, "-m", "x"]) != 0
    assert [_status(ws_root, i) for i in ids] == ["open", "open"]


def test_already_done_is_skipped_and_reported(switch, ws_root, put, capsys):
    a, b, c = put("open"), put("done"), put("backlog")
    switch.human("CLOSE 2")                     # only the two that can be closed count
    assert run(["close", a, b, c, "-m", "x"]) == 0
    out = capsys.readouterr().out
    assert f"{b}: skipped" in out
    assert [_status(ws_root, i) for i in (a, b, c)] == ["done", "done", "done"]


def test_changed_since_the_preview_is_skipped(switch, ws_root, put, capsys, monkeypatch):
    a, b = put("open", title="first"), put("open", title="second")

    def edit_then_confirm(prompt=""):
        ws = Workspace.open(ws_root)
        path, t = store.load(ws, a)
        t.set_section("Summary", "edited while the human typed")
        store.save(ws, t, path)
        return "CLOSE 2"

    switch.human("CLOSE 2")
    monkeypatch.setattr("builtins.input", edit_then_confirm)
    assert run(["close", a, b, "-m", "x"]) == 0
    out = capsys.readouterr().out
    assert f"{a}: skipped" in out and "changed since" in out
    assert (_status(ws_root, a), _status(ws_root, b)) == ("open", "done")


def test_as_by_and_message_apply_to_all(switch, ws_root, put, capsys):
    a, b, keep = put("open"), put("open"), put("open")
    switch.human("CLOSE 2")
    assert run(["close", a, b, "--as", "duplicate", "--by", keep, "-m", "dupes"]) == 0
    ws = Workspace.open(ws_root)
    for i in (a, b):
        t = store.load(ws, i)[1]
        assert t.status == "done" and t.meta["resolution"] == "duplicate" and t.meta["superseded_by"] == keep
        assert "dupes" in t.section("Log")


def test_dry_run_writes_nothing_and_asks_nothing(switch, ws_root, put, capsys):
    ids = [put("open"), put("open")]
    switch.human("never asked")
    assert run(["close", *ids, "-m", "x", "--dry-run"]) == 0
    assert "nothing was written" in capsys.readouterr().out
    assert [_status(ws_root, i) for i in ids] == ["open", "open"]


def test_json_lists_closed_and_skipped(switch, ws_root, put, capsys):
    a, b = put("open"), put("done")
    switch.human("CLOSE 1")
    assert run(["close", a, b, "-m", "x", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["closed"] == [a] and list(out["skipped"]) == [b]


def test_nothing_to_close_is_an_error(switch, ws_root, put, capsys):
    a, b = put("done"), put("done")
    switch.human("CLOSE 0")
    assert run(["close", a, b, "-m", "x"]) != 0


def test_single_ticket_keeps_the_ticket_id_confirmation(switch, ws_root, put, capsys):
    a = put("open")
    switch.human(a)
    assert run(["close", a, "-m", "x"]) == 0
    assert _status(ws_root, a) == "done"
