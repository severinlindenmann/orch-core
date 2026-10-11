"""The stale check: installed instructions older than the CLI's generator are reported by ``orch check``."""

from __future__ import annotations

import io
import json

import pytest

from orch.cli.main import main
from orch.instructions import INSTRUCTIONS_REV, stale_findings, workspace_files, write_workspace_files
from orch.instructions.hooks import STALE_LINE


@pytest.fixture
def root(tmp_path):
    write_workspace_files(tmp_path)
    return tmp_path


def where(findings):
    return {f["where"]: f["what"] for f in findings}


def test_fresh_files_are_current(root):
    assert stale_findings(root) == []


def test_nothing_installed_is_reported_as_missing(tmp_path):
    w = where(stale_findings(tmp_path))
    assert w["AGENTS.orch.md"] == "missing" and len(w) == 4


def test_an_older_stamp_is_stale(root):
    p = root / "AGENTS.orch.md"
    p.write_text(p.read_text().replace(f"(instructions r{INSTRUCTIONS_REV})", "(instructions r0)"))
    assert where(stale_findings(root)) == {"AGENTS.orch.md": f"r0, this orch writes r{INSTRUCTIONS_REV}"}


def test_a_newer_stamp_is_reported_but_not_as_old(root):
    p = root / "AGENTS.orch.md"
    p.write_text(p.read_text().replace(f"(instructions r{INSTRUCTIONS_REV})", "(instructions r99)"))
    assert "newer" in where(stale_findings(root))["AGENTS.orch.md"]


def test_no_stamp_is_stale(root):
    (root / "AGENTS.orch.md").write_text("some other file\n")
    assert where(stale_findings(root)) == {"AGENTS.orch.md": "no version stamp"}


def test_an_older_skill_version_is_stale(root):
    side = root / ".claude/skills/orch-tickets/orch.skill.json"
    doc = json.loads(side.read_text())
    doc["skill_version"] = "0.9.0"
    side.write_text(json.dumps(doc))
    assert where(stale_findings(root)) == {".claude/skills/orch-tickets": "v0.9.0, this orch ships v1.0.0"}


def test_a_hand_edited_builtin_skill_is_reported_and_a_taken_over_one_is_not(root):
    skill = root / ".claude/skills/orch-tickets/SKILL.md"
    skill.write_text(skill.read_text() + "- my own rule\n")
    assert ".claude/skills/orch-tickets" in where(stale_findings(root))
    side = root / ".claude/skills/orch-tickets/orch.skill.json"
    side.write_text(json.dumps({**json.loads(side.read_text()), "scope": "workspace"}))
    assert stale_findings(root) == []  # the owner took it over: not checked
    write_workspace_files(root, pointers=False, owned=workspace_files())  # and a sync leaves it alone
    assert "my own rule" in skill.read_text()


def test_a_broken_sidecar_is_reported(root):
    (root / ".claude/skills/orch-tickets/orch.skill.json").write_text("{not json")
    assert where(stale_findings(root)) == {".claude/skills/orch-tickets": "sidecar unreadable"}
    (root / ".claude/skills/orch-tickets/orch.skill.json").unlink()
    assert where(stale_findings(root)) == {".claude/skills/orch-tickets": "no orch.skill.json"}


# ---------------------------------------------------------------------------------------------- through the commands


def cli(ws, *argv, grant=True):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), env=ws.env(grant=grant), stdout=out, stderr=err, now=lambda: ws.clock[0])
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def ws(tmp_path):
    from tests.ops.helpers import Ws

    w = Ws(tmp_path)
    w.bootstrap()
    yield w
    w.store.close()


def test_orch_check_reports_stale_instructions_and_sync_fixes_them(ws):
    code, out, err = cli(ws, "check")
    assert code == 5 and out.splitlines()[0] == "ok check 4", out + err  # problems: exit 5 so a hook can gate
    assert "AGENTS.orch.md: missing" in out and out.splitlines()[-1] == "next: orch instructions sync"
    code, out, err = cli(ws, "instructions", "sync", grant=False)  # no grant needed, no event
    assert code == 0 and out.splitlines()[0] == f"ok instructions.sync r{INSTRUCTIONS_REV}", out + err
    assert (ws.root / "AGENTS.orch.md").is_file()
    code, out, _ = cli(ws, "check")
    assert code == 0 and out.splitlines()[0] == "ok check 0"
    code, out, err = cli(ws, "instructions", "sync")
    assert code == 0 and "already current" in out


def test_the_session_start_text_carries_the_stale_line(ws):
    out = cli(ws, "instructions", "hook", "session-start")[1]
    assert STALE_LINE in out.splitlines()
    cli(ws, "instructions", "sync")
    assert STALE_LINE not in cli(ws, "instructions", "hook", "session-start")[1].splitlines()


def test_outside_a_workspace_sync_and_check_say_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for argv in (["instructions", "sync"], ["check"]):
        out, err = io.StringIO(), io.StringIO()
        code = main(argv, env={"HOME": str(tmp_path)}, stdout=out, stderr=err)
        assert code == 2 and err.getvalue().startswith("err not_found no orch workspace here"), (argv, err.getvalue())


def test_sync_dry_run_writes_nothing_and_force_is_needed_for_a_hand_edited_skill(ws):
    code, out, _ = cli(ws, "instructions", "sync", "--dry-run")
    assert code == 0 and "would write AGENTS.orch.md" in out and not (ws.root / "AGENTS.orch.md").exists()
    cli(ws, "instructions", "sync")
    skill = ws.root / ".claude/skills/orch-tickets/SKILL.md"
    skill.write_text(skill.read_text() + "- my own rule\n")
    code, out, _ = cli(ws, "instructions", "sync")
    assert code == 0 and "kept .claude/skills/orch-tickets (edited by hand; --force overwrites it" in out
    assert "my own rule" in skill.read_text()
    code, out, _ = cli(ws, "instructions", "sync", "--dry-run", "--force")
    assert "would write .claude/skills/orch-tickets/SKILL.md" in out and "my own rule" in skill.read_text()
    code, out, _ = cli(ws, "instructions", "sync", "--force")
    assert "wrote .claude/skills/orch-tickets/SKILL.md" in out and "my own rule" not in skill.read_text()
    assert cli(ws, "check")[0] == 0


def test_an_old_skill_version_is_upgraded_without_force(ws):
    cli(ws, "instructions", "sync")
    side = ws.root / ".claude/skills/orch-tickets/orch.skill.json"
    side.write_text(json.dumps({**json.loads(side.read_text()), "skill_version": "0.1.0"}))
    (ws.root / ".claude/skills/orch-tickets/SKILL.md").write_text("an old text\n")
    assert "wrote .claude/skills/orch-tickets/SKILL.md" in cli(ws, "instructions", "sync")[1]


def test_stale_check_does_not_follow_a_symlink(root, tmp_path):
    (tmp_path / "secret").write_text("orch v2.0 (instructions r1) \u00b7 not the real one\n")
    (root / "AGENTS.orch.md").unlink()
    (root / "AGENTS.orch.md").symlink_to(tmp_path / "secret")
    assert "symbolic link" in where(stale_findings(root))["AGENTS.orch.md"]
