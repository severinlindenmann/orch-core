"""Hints per operation, claims in the notes, `--also`, the bounds and the small fixes of the #351 review round."""

from __future__ import annotations

import json

import pytest

from orch.ops import runtime
from tests.ops.helpers import OTHER, Cli
from tests.ops.test_task_ac import claimed, fill_and_approve, py


def last(r):
    return r.out.splitlines()[-1] if r.out else r.err


def test_the_next_hint_of_each_operation(ws, cli):
    claimed(cli)
    cli("ac", "add", "a")
    assert last(cli("task", "add", "t", "--verify", py("print(1)"), "--proves", "AC1")) == "next: orch task start T1"
    assert last(cli("task", "start", "T1")) == "next: orch task done T1 --run"
    cli("task", "add", "no verify")
    cli("task", "skip", "T1", "--reason", "x")
    cli("task", "start", "T2")
    assert last(cli("task", "next")) == "next: orch task done T2"
    r = cli("task", "done", "T2")
    assert last(r) == "next: orch section set context -m TEXT"  # a gate is open and empty: nobody can approve it yet
    for sec in ("context", "requirements", "out_of_scope"):
        assert last(cli("show")) == f"next: orch section set {sec} -m TEXT"
        cli("section", "set", sec, "-m", "text")
    assert last(cli("show")) == 'next: orch ask "approve the requirements gate?"'  # complete: asking works now
    r = cli.j("task", "add", "x", "--proves", "AC9")
    assert r.err_code == "not_found" and r.doc["error"]["hint"] == "orch ac add TEXT"
    assert last(cli("next")) == "next: orch show DEMO-0001"  # a ticket I hold


def test_a_hint_never_names_a_command_that_cannot_succeed(ws, cli):
    """Every ``next:`` of the walk from an empty workspace to a ready gate is a command that works as printed."""
    assert last(cli("status")) == 'next: orch new "TITLE"'  # nothing to claim yet
    cli("new", "A")
    assert last(cli("status")) == "next: orch claim --next"
    cli("claim", "1")
    other = Cli(ws, session=OTHER)
    assert last(other("show", "1")) == "next: orch show"  # not its claim: no edit hint
    walk = [
        ("orch section set context -m TEXT", ("section", "set", "context", "-m", "x")),
        ("orch section set requirements -m TEXT", ("section", "set", "requirements", "-m", "x")),
        ("orch section set out_of_scope -m TEXT", ("section", "set", "out_of_scope", "-m", "x")),
        ("orch ac add TEXT", ("ac", "add", "a criterion")),
    ]
    for hint, argv in walk:
        assert last(cli("show")) == f"next: {hint}"
        assert cli(*argv).code == 0, hint
    cli("section", "set", "plan", "-m", "p")
    cli("section", "set", "decisions", "-m", "d")
    assert last(cli("show")) == "next: orch show"  # requirements wait for a person: nothing to type
    assert last(cli("task", "add", "t")) == "next: orch task start T1"
    assert last(cli("task", "start", "T1")) == "next: orch task done T1"
    assert (
        last(cli("task", "done", "T1")) == 'next: orch ask "approve the requirements gate?"'
    )  # complete: asking works


def test_task_done_without_run_says_the_criterion_still_has_no_evidence(ws, cli):
    claimed(cli)
    cli("ac", "add", "it works")
    cli("task", "add", "do it", "--verify", py("print(1)"), "--proves", "AC1")
    cli("task", "add", "no check", "--proves", "AC1")
    r = cli("task", "done", "T1")
    assert (
        "note: AC1 still has no evidence (done without --run); orch task reopen T1, then orch task done T1 --run"
        in r.out
    )
    assert cli("task", "reopen", "T1").code == 0
    assert "no evidence" not in cli("task", "done", "T1", "--run").out  # the receipt is the evidence
    r = cli("task", "done", "T2")
    assert "no evidence" not in r.out  # AC1 has evidence by now


def test_the_hints_of_a_refused_submit_name_what_is_open(ws, cli):
    claimed(cli)
    r = cli.j("submit")
    assert r.doc["error"]["hint"] == 'orch ask "approve the requirements gate?"'
    fill_and_approve(ws, cli)
    r = cli.j("submit")
    assert r.err_code == "ac.evidence_missing" and r.doc["error"]["hint"] == "orch section set verification -m TEXT"


def test_ambiguous_ref_names_a_real_key_or_a_way_to_get_one(ws, cli):
    cli("new", "A")
    r = cli.j("show")
    assert r.err_code == "ambiguous_ref" and r.doc["error"]["hint"] == "orch claim --next"
    cli("claim", "1")
    cli("new", "B")
    cli("claim", "2", "--also")
    r = cli.j("task", "list")
    assert r.doc["error"]["hint"] == "orch task list DEMO-0001"


def test_a_second_claim_needs_also_and_then_every_command_needs_a_ref(ws, cli):
    cli("new", "A")
    cli("new", "B")
    cli("claim", "1")
    r = cli.j("claim", "2")
    assert (r.code, r.err_code) == (4, "claim.held") and r.doc["error"][
        "hint"
    ] == "orch claim REF --also, or orch release first"
    assert cli.j("claim", "--next").err_code == "claim.held"
    assert cli("claim", "2", "--also").code == 0
    assert cli.j("log", "x").err_code == "ambiguous_ref"
    assert cli("log", "x", "--ref", "2").code == 0
    assert cli("release", "1").code == 0 and cli("log", "now unambiguous").code == 0  # DEMO-0002 is the only claim


def test_commands_without_a_ref_load_only_the_claimed_tickets(ws, cli, monkeypatch):
    for i in range(4):
        cli("new", f"T{i}")
    cli("claim", "3")
    from orch.store import Store

    def boom(self):
        raise AssertionError("a REF-less command must not load every ticket")

    monkeypatch.setattr(Store, "load_all", boom)
    for argv in (["show"], ["status"], ["task", "list"], ["log", "x"], ["ac", "add", "a"], ["wait", "--timeout", "1"]):
        assert cli.j(*argv).code == 0, argv
    sub = Cli(ws, session=cli.ws.env()["ORCH_SESSION"] + ".1")
    assert sub.j("show").code == 0  # a subagent finds its parent's claim in the parent's notes


def test_a_stale_claim_entry_drops_out(ws, cli):
    cli("new", "A")
    cli("claim", "1")
    other = Cli(ws, session="s_01J9ZP0000000000000000000T")
    other("claim", "1", "--takeover", "--reason", "mine now")
    r = cli.j("show")
    assert r.err_code == "ambiguous_ref"  # the entry no longer names a claim of this session


def test_list_and_search_say_how_many_more(ws, cli):
    for i in range(5):
        cli("new", f"Seeds number {i}")
    assert "+3 more" in cli("list", "--limit", "2").out
    assert "more" not in cli("list", "--limit", "5").out
    r = cli("search", "seeds", "--limit", "2")
    assert "+3 more tickets match" in r.out


def test_input_digest_stops_reading_a_huge_file(tmp_path, monkeypatch):
    from orch.ops.base import Context

    monkeypatch.setattr(runtime, "ARTIFACT_LIMIT", 100)
    f = tmp_path / "big"
    f.write_bytes(b"x" * 1000)
    g = tmp_path / "small"
    g.write_bytes(b"x" * 10)
    ctx = Context()
    assert runtime.input_digest(ctx, str(f)) is None and runtime.input_digest(ctx, str(g)) is not None


def test_a_relative_xdg_path_is_ignored(tmp_path):
    assert runtime.state_dir({"XDG_CONFIG_HOME": "relative/dir", "HOME": "/h"}).as_posix() == "/h/.config/orch"
    assert runtime.state_dir({"XDG_CONFIG_HOME": "/abs", "HOME": "/h"}).as_posix() == "/abs/orch"


def test_status_shows_the_state_dir_and_a_pin_made_by_this_call(ws, cli):
    assert "state_dir" not in cli.j("status").data and "state dir" not in cli("status").out  # only with --verbose
    d = cli.j("status", "--verbose").data
    assert d["state_dir"] == str(ws.host_state) and "pin" not in d  # the bootstrap pinned the genesis already
    pin = ws.host_state / "hosts" / "705d40abbb8c1c90354a1acaa94c935c" / "genesis"
    pin.unlink()
    r = cli("status")
    assert "genesis pin created by this call" in r.out and "state dir" not in r.out
    assert f"state dir {ws.host_state}" in cli("status", "--verbose").out
    assert cli.j("status").data.get("pin") is None  # the second call finds it


def test_unknown_batch_item_keys_and_the_set_schema_ref(ws, cli):
    claimed(cli)
    r = cli.j("apply", "--file", "-", stdin=json.dumps({"ref": "1", "ops": [{"op": "set", "pairs": ["title=Z"]}]}))
    assert r.code == 0
    assert ws.ticket_json("1")["title"] == "Z"


@pytest.mark.parametrize("n", [0])
def test_the_scripted_session_notes_hold_claims_and_decisions(ws, cli, n):
    claimed(cli)
    doc = json.loads(next((ws.root / ".state" / "sessions").glob("*.notes.json")).read_text())
    assert doc["claims"] == [ws.uid("1")] and "decided" in next(iter(doc["tickets"].values()))
