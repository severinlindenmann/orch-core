"""Focus phase 2, P2-1: ``run.transcript`` (cap ``focus/1``): host checks, the runner command, column P only."""

# ruff: noqa: F811

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from orch.host.runners import new_run_id
from orch.runner import transcript
from tests.host.dispatch_helpers import err, ok
from tests.host.test_runner_socket import GEN1, fields
from tests.host.test_terms import open_shell, ready, rstate, w  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")
FIX = Path(__file__).parent.parent / "runner" / "fixtures" / "transcripts"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    home = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def as_claude(w, run, cwd="/work/proj"):
    w.h.host.runners.store.records[run]["harness"] = "claude-code"
    w.runner.sup.table.update(run, harness="claude-code", cwd_path=cwd)


def test_a_shell_run_has_no_transcript(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    data = ok(c.call("run.transcript", {"run": run}))["data"]
    assert data["available"] is False and data["items"] == [] and data["next"] is None


def test_the_runner_reads_the_claude_transcript_by_cursor(cfg, ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    as_claude(w, run)
    first = ok(c.call("run.transcript", {"run": run}))["data"]
    assert first["available"] is False and first["reason"] == "transcript not found yet"
    proj = cfg / "projects" / "-work-proj"
    proj.mkdir(parents=True)
    shutil.copy(FIX / "claude_basic.jsonl", proj / f"{transcript.session_id(run)}.jsonl")
    page = ok(c.call("run.transcript", {"run": run, "limit": 3}))["data"]
    assert page["available"] is True and page["harness"] == "claude-code" and 3 <= len(page["items"]) < 8
    assert page["items"][0]["text"].startswith("Rate-limit") and page["eof"] is False
    rest = ok(c.call("run.transcript", {"run": run, "after": page["next"]}))["data"]
    assert rest["eof"] is True and rest["items"][0]["kind"] == "result"
    done = ok(c.call("run.transcript", {"run": run, "after": rest["next"]}))["data"]
    assert done["items"] == [] and done["eof"] is True and done["next"] == rest["next"]


def test_only_the_runs_person_reads_and_only_as_column_p(ready):
    w = ready
    c = w.conn()
    other = new_run_id()
    w.h.run(w.h.host.runners._save({**fields(w), "run": other, "person": w.quinn.ref, "workspace_id": w.wid,
                                     "state": "running", "exit": None, "cleanup": None, "runner_uid": 0,
                                     "gen": GEN1}))  # fmt: skip
    assert err(c.call("run.transcript", {"run": other})) == "not_found"
    assert err(c.call("run.transcript", {"run": new_run_id()})) == "not_found"
    mine = open_shell(c)["run"]
    assert err(c.call("run.transcript", {"run": mine}, grant=w.lab.ws.grant)) == "op.not_allowed"  # G


def test_a_run_on_a_ticket_the_person_cannot_see_is_not_found(ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket("hidden")
    w.restrict(uid, [w.quinn.ref])
    run = new_run_id()
    w.h.run(w.h.host.runners._save({**fields(w), "run": run, "ticket": key, "workspace_id": w.wid, "state": "running",
                                     "exit": None, "cleanup": None, "runner_uid": 0, "gen": GEN1}))  # fmt: skip
    assert err(c.call("run.transcript", {"run": run})) == "not_found"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"run": "x"},
        {"run": "r_01J0000000000000000000000A", "after": "7"},
        {"run": "r_01J0000000000000000000000A", "limit": 0},
        {"run": "r_01J0000000000000000000000A", "child": "../x"},
        {"run": "r_01J0000000000000000000000A", "extra": 1},
    ],
)
def test_arguments_are_checked(ready, args):
    assert err(ready.conn().call("run.transcript", args)) == "invalid.input"


def test_the_op_is_listed_under_cap_focus_1():
    from orch.host.dispatch import RegistryBackend, op_kind

    assert "focus/1" in RegistryBackend().caps
    assert op_kind("run.transcript") == ("person_read", "read")
