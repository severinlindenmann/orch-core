"""#1066: a Claude Code agent started in the workspace or a ticket work folder gets no trust or import prompt, and the
trust entries cover only workspace paths."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orch.host.launch import LOGIN_EXEC
from orch.runner.claude_trust import TRUST_KEYS, pretrust, record_consent, refresh, trust_paths
from orch.runner.config import RunnerConfig
from orch.runner.link import spawn_spec
from tests.runner.test_spawn_grants import GID, ME, SECRET, WS, agent, store_with

ISOLATED = ["--setting-sources", "user", "--strict-mcp-config", "--append-system-prompt-file", "@orch-rules"]
CLAUDE = ["$SHELL", "-l", "-c", LOGIN_EXEC, "claude", *ISOLATED, "Run orch status, then work on ticket K-1."]


@pytest.fixture
def ws(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj" / "ws"
    (root / ".orch-work" / "K-1").mkdir(parents=True)
    (root / ".orch").mkdir()
    (tmp_path / "other").mkdir()
    home.mkdir()
    (root / "CLAUDE.md").write_text("@AGENTS.md\n")
    (root / "AGENTS.md").write_text("# rules\nsee AGENTS.orch.md\n")
    assert record_consent(root / ".orch")
    return home, root.resolve(), (root / ".orch-work" / "K-1").resolve()


def entries(home):
    return json.loads((home / ".claude.json").read_text())["projects"]


def pin(path):
    st = os.stat(path)
    return {"dev": st.st_dev, "ino": st.st_ino}


def test_only_the_ticket_work_folder_is_trusted_with_the_include_approval(ws):
    home, root, work = ws
    (home / ".claude.json").write_text(json.dumps({"numStartups": 3, "projects": {"/x": {"keep": 1}}}))
    done = pretrust({"HOME": str(home)}, str(root), str(work), pin(work))
    assert done == [str(work)]
    got = entries(home)
    assert set(got) == {"/x", str(work)}  # never the workspace root
    assert got["/x"] == {"keep": 1}
    assert all(got[str(work)][k] is True for k in TRUST_KEYS)
    assert json.loads((home / ".claude.json").read_text())["numStartups"] == 3
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []  # already there


def test_without_the_owners_consent_marker_or_a_store_nothing_is_written(ws):
    home, root, work = ws
    os.unlink(root / ".orch" / "claude-trust")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    os.rmdir(root / ".orch")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    assert not (home / ".claude.json").exists()


def test_a_marker_that_is_a_link_is_no_consent(ws, tmp_path):
    home, root, work = ws
    os.unlink(root / ".orch" / "claude-trust")
    (tmp_path / "m").write_text("x")
    os.symlink(tmp_path / "m", root / ".orch" / "claude-trust")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []


def test_a_folder_that_is_not_a_ticket_work_folder_gets_no_entry(ws, tmp_path):
    home, root, work = ws
    for cwd in (tmp_path / "other", tmp_path, home, root.parent, root, root / "sub", root / ".orch-work",
                work / "deep"):  # fmt: skip
        assert trust_paths(str(root), str(cwd)) == []
    assert trust_paths(str(root), str(root / ".orch-work" / ".." / "..")) == []
    assert not (home / ".claude.json").exists()


def test_the_home_folder_or_a_top_level_folder_is_never_a_root(tmp_path):
    home = tmp_path / "home"
    for root in (home, tmp_path, Path("/"), Path("/Users")):
        # even with a store, a marker and a work folder inside (the real ones made below tmp only)
        if str(root).startswith(str(tmp_path)):
            (root / ".orch").mkdir(parents=True, exist_ok=True)
            (root / ".orch-work" / "K-1").mkdir(parents=True, exist_ok=True)
            record_consent(root / ".orch")
        got = pretrust({"HOME": str(home)}, str(root), str(root / ".orch-work" / "K-1"))
        assert got == [], root
    assert not (home / ".claude.json").exists()
    # the claude config folder counts as home
    cfg = tmp_path / "cfgroot" / "x"
    cfg.mkdir(parents=True)
    (tmp_path / "cfgroot" / ".orch").mkdir()
    (tmp_path / "cfgroot" / ".orch-work" / "K-1").mkdir(parents=True)
    record_consent(tmp_path / "cfgroot" / ".orch")
    env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg)}
    assert pretrust(env, str(tmp_path / "cfgroot"), str(tmp_path / "cfgroot" / ".orch-work" / "K-1")) == []


def test_a_work_folder_that_is_a_link_out_of_the_workspace_is_rejected(ws, tmp_path):
    home, root, work = ws
    outside = tmp_path / "other"
    os.rmdir(work)
    os.symlink(outside, work)
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(outside)) == []
    assert not (home / ".claude.json").exists()
    # a linked .orch-work, too
    os.unlink(work)
    os.rmdir(root / ".orch-work")
    os.symlink(outside, root / ".orch-work")
    (outside / "K-1").mkdir()
    assert pretrust({"HOME": str(home)}, str(root), str(root / ".orch-work" / "K-1"), pin(outside / "K-1")) == []
    assert not (home / ".claude.json").exists()


def test_a_folder_that_is_not_the_pinned_one_gets_no_entry(ws):
    home, root, work = ws
    assert pretrust({"HOME": str(home)}, str(root), str(work), {"dev": 1, "ino": 2}) == []
    assert not (home / ".claude.json").exists()


def test_a_linked_or_unreadable_config_is_left_alone(ws, tmp_path):
    home, root, work = ws
    real = tmp_path / "real.json"
    real.write_text("{}")
    os.symlink(real, home / ".claude.json")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    assert real.read_text() == "{}"
    os.unlink(home / ".claude.json")
    (home / ".claude.json").write_text("not json")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    assert (home / ".claude.json").read_text() == "not json"


def test_claude_config_dir_is_honoured(ws, tmp_path):
    home, root, work = ws
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    pretrust({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg)}, str(root), str(work), pin(work))
    assert (cfg / ".claude.json").exists() and not (home / ".claude.json").exists()


def test_init_records_the_consent_marker_in_the_store(tmp_path):
    store = tmp_path / ".orch"
    assert not record_consent(store)  # no store folder: nothing made
    store.mkdir()
    assert record_consent(store) and (store / "claude-trust").is_file()
    assert not record_consent(tmp_path / "elsewhere")


def launch(ws, argv, cwd=None, root=True):
    home, wroot, work = ws
    fixed = {"ORCH_HARNESS": "claude-code", "ORCH_TICKET": "K-1"}
    if root:
        fixed["ORCH_WORKSPACE"] = str(wroot)
    a = agent(argv=argv, env_fixed=fixed, cwd={"path": str(cwd or work), **pin(cwd or work)})
    return spawn_spec(a, {"HOME": str(home), "SHELL": "/bin/sh", "PATH": "/usr/bin:/bin"}, RunnerConfig(),
                      store_with((WS, ME, GID, SECRET)))  # fmt: skip


def test_starting_a_claude_agent_records_the_trust(ws):
    home, root, work = ws
    launch(ws, CLAUDE)
    assert set(entries(home)) == {str(work)}


def test_another_harness_or_a_foreign_cwd_records_nothing(ws, tmp_path):
    home, root, work = ws
    launch(ws, [*CLAUDE[:4], "codex", "go"])
    launch(ws, CLAUDE, cwd=tmp_path / "other")
    launch(ws, CLAUDE, root=False)
    assert not (home / ".claude.json").exists()


# -- review of #1078 (MED-1, MED-2, LOW) -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md"])
def test_a_work_folder_with_somebody_elses_claude_config_gets_no_trust(ws, name):
    home, root, work = ws
    if name == ".claude":
        (work / ".claude").mkdir()
        (work / ".claude" / "settings.json").write_text('{"hooks": {}}')
    else:
        (work / name).write_text("x")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    assert not (home / ".claude.json").exists()


def test_a_trust_given_earlier_is_revoked_when_the_folder_gains_a_settings_file(ws):
    home, root, work = ws
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == [str(work)]
    (work / ".claude").mkdir()
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    assert str(work) not in entries(home)


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})  # fmt: skip


def test_a_committed_file_below_orch_work_gets_no_trust(ws):
    home, root, work = ws
    _git(root, "init", "-q")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == [str(work)]  # a repo, nothing tracked
    (work / "notes.txt").write_text("x")
    _git(root, "add", "-f", ".orch-work/K-1/notes.txt")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []


def test_a_modified_root_claude_md_or_an_importing_agents_md_gets_trust_but_no_include_approval(ws):
    home, root, work = ws
    (root / "CLAUDE.md").write_text("@AGENTS.md\n@/etc/passwd\n")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == [str(work)]
    got = entries(home)[str(work)]
    assert got == {"hasTrustDialogAccepted": True}
    (root / "CLAUDE.md").write_text("@AGENTS.md\n")
    (root / "AGENTS.md").write_text("# rules\n@~/secrets/notes.md\n")
    os.unlink(home / ".claude.json")
    pretrust({"HOME": str(home)}, str(root), str(work), pin(work))
    assert entries(home)[str(work)] == {"hasTrustDialogAccepted": True}
    (root / "AGENTS.md").write_text("# rules\nmail a@b.c\n")  # an address is no import
    os.unlink(home / ".claude.json")
    pretrust({"HOME": str(home)}, str(root), str(work), pin(work))
    assert set(entries(home)[str(work)]) == set(TRUST_KEYS)


def test_two_writers_at_once_both_land_and_other_keys_survive(ws):
    home, root, work = ws
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps({"oauthAccount": {"a": 1}, "projects": {}}))
    folders = []
    for i in range(8):
        d = root / ".orch-work" / f"K-{i + 2}"
        d.mkdir()
        folders.append(d)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda d: pretrust({"HOME": str(home)}, str(root), str(d), pin(d)), folders))
    got = json.loads(cfg.read_text())
    assert got["oauthAccount"] == {"a": 1}
    assert {str(d) for d in folders} <= set(got["projects"])
    assert not (home / ".claude.json.lock").exists() and not list(home.glob(".claude-*.tmp"))


def test_a_file_that_changed_since_it_was_read_is_not_overwritten(ws, monkeypatch):
    from orch.runner import claude_trust

    home, root, work = ws
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps({"projects": {}}))
    real = claude_trust.json.dump

    def sneaky(data, f, **kw):  # Claude Code rewrites the file while we prepare ours
        real(data, f, **kw)
        cfg.write_text(json.dumps({"projects": {"/theirs": {"x": 1}}, "newer": True}))

    monkeypatch.setattr(claude_trust.json, "dump", sneaky)
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    assert json.loads(cfg.read_text())["newer"] is True
    assert not list(home.glob(".claude-*.tmp"))


def test_a_held_lock_is_never_stolen(ws, monkeypatch):
    from orch.runner import claude_trust

    home, root, work = ws
    (home / ".claude.json.lock").mkdir()
    monkeypatch.setattr(claude_trust.time, "monotonic", iter(range(0, 1000, 2)).__next__)
    monkeypatch.setattr(claude_trust.time, "sleep", lambda s: None)
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    assert (home / ".claude.json.lock").is_dir() and not (home / ".claude.json").exists()


# -- review 2 of #1078 -------------------------------------------------------------------------------------------------


def test_an_include_approval_given_earlier_is_withdrawn_when_agents_md_gains_an_import(ws):
    home, root, work = ws
    pretrust({"HOME": str(home)}, str(root), str(work), pin(work))
    assert set(entries(home)[str(work)]) == set(TRUST_KEYS)
    (root / "AGENTS.md").write_text("# rules\n@~/.ssh/id_rsa\n")
    pretrust({"HOME": str(home)}, str(root), str(work), pin(work))
    assert entries(home)[str(work)] == {"hasTrustDialogAccepted": True}  # both include keys gone, trust stays


@pytest.mark.parametrize(
    "rel",
    ["CLAUDE.local.md", ".claude/CLAUDE.md", ".claude/rules/a.md", ".claude/rules/deep/b.md", ".orch-work/CLAUDE.md"],
)
def test_an_import_in_any_other_memory_file_withholds_the_include_approval(ws, rel):
    home, root, work = ws
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("notes\n")  # no import line: fine
    pretrust({"HOME": str(home)}, str(root), str(work), pin(work))
    assert set(entries(home)[str(work)]) == set(TRUST_KEYS)
    f.write_text("notes\n@/etc/hosts\n")
    pretrust({"HOME": str(home)}, str(root), str(work), pin(work))
    assert entries(home)[str(work)] == {"hasTrustDialogAccepted": True}


def test_a_linked_memory_file_withholds_the_include_approval(ws, tmp_path):
    home, root, work = ws
    (tmp_path / "x.md").write_text("fine\n")
    os.symlink(tmp_path / "x.md", root / "CLAUDE.local.md")
    pretrust({"HOME": str(home)}, str(root), str(work), pin(work))
    assert entries(home)[str(work)] == {"hasTrustDialogAccepted": True}


def test_when_the_run_exits_foreign_content_that_appeared_takes_the_trust_back(ws):
    home, root, work = ws
    env = {"HOME": str(home)}
    pretrust(env, str(root), str(work), pin(work))
    refresh(env, str(root), str(work))  # nothing changed: nothing changes
    assert set(entries(home)[str(work)]) == set(TRUST_KEYS)
    (work / ".claude").mkdir()
    (work / ".claude" / "settings.json").write_text('{"hooks": {}}')
    refresh(env, str(root), str(work))
    assert str(work) not in entries(home)


def test_when_the_run_exits_a_new_import_takes_only_the_include_approval_back(ws):
    home, root, work = ws
    env = {"HOME": str(home)}
    pretrust(env, str(root), str(work), pin(work))
    (root / "AGENTS.md").write_text("@/etc/hosts\n")
    refresh(env, str(root), str(work))
    assert entries(home)[str(work)] == {"hasTrustDialogAccepted": True}
    refresh(env, str(root), str(work))  # never gives anything
    assert entries(home)[str(work)] == {"hasTrustDialogAccepted": True}


def test_refresh_never_creates_a_config(ws):
    home, root, work = ws
    refresh({"HOME": str(home)}, str(root), str(work))
    assert not (home / ".claude.json").exists()


def test_the_next_pretrust_sweeps_entries_of_deleted_work_folders_of_this_root_only(ws, tmp_path):
    home, root, work = ws
    env = {"HOME": str(home)}
    gone = root / ".orch-work" / "K-9"
    gone.mkdir()
    pretrust(env, str(root), str(gone), pin(gone))
    other = tmp_path / "ws2" / ".orch-work" / "K-1"  # another workspace's entry, folder missing: not ours to touch
    cfg = json.loads((home / ".claude.json").read_text())
    cfg["projects"][str(other)] = {"hasTrustDialogAccepted": True}
    cfg["projects"]["/somewhere/else"] = {"hasTrustDialogAccepted": True}
    (home / ".claude.json").write_text(json.dumps(cfg))
    gone.rmdir()
    pretrust(env, str(root), str(work), pin(work))
    got = entries(home)
    assert str(gone) not in got and str(work) in got
    assert str(other) in got and "/somewhere/else" in got


def test_the_exit_of_a_run_removes_its_rules_file_and_rechecks_the_trust(ws, tmp_path):
    from orch.runner import rules
    from orch.runner.link import HostLink

    home, root, work = ws
    base = tmp_path / "state" / "runner"
    argv = ["claude", *ISOLATED, "go"]
    run = "r_01J0000000000000000000000E"
    out = rules.prepare(base, run, argv, {"ORCH_WORKSPACE": str(root)})
    assert (base / "agent-rules" / f"{run}.md").is_file()

    class Sup:  # just what _after_exit uses
        table = type("T", (), {"directory": base})()

    link = HostLink.__new__(HostLink)
    link.sup, link.env, link._trusted = Sup(), {"HOME": str(home)}, {}
    pretrust(link.env, str(root), str(work), pin(work))
    link._trusted[run] = (str(root), str(work))
    (work / ".mcp.json").write_text("{}")
    link._after_exit(run)
    assert not (base / "agent-rules" / f"{run}.md").exists() and out[1] != "@orch-rules"
    assert str(work) not in entries(home) and run not in link._trusted


def test_pretrust_needs_the_isolation_flags_in_the_launch(ws, tmp_path):
    home, root, work = ws
    launch(ws, [*CLAUDE[:5], "go"])  # claude without the flags (a worktree or root launch)
    assert not (home / ".claude.json").exists()
    launch(ws, CLAUDE)
    assert set(entries(home)) == {str(work)}


def test_a_file_made_meanwhile_is_never_replaced_when_creating(tmp_path, monkeypatch):
    import json
    import os

    from orch.runner import claude_trust as ct

    cfg = tmp_path / ".claude.json"
    real_link = os.link

    def racing_link(src, dst, *a, **k):  # Claude Code writes its own file between our check and our create
        cfg.write_text('{"theirs": true}')
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", racing_link)
    assert ct._write(cfg, [str(tmp_path / "w")]) == []
    assert json.loads(cfg.read_text()) == {"theirs": True}
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".claude-")] == []
