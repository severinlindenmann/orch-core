import io
import json

import pytest

from orch.cli import run
from orch.core import store
from orch.hooks.guard import evaluate


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


@pytest.mark.parametrize("cmd", [
    "cat orchestrator/tickets/open/L-0001-x.md",
    "grep -r nightly orchestrator/tickets",
    "orch show L-0001 && orch move L-0001 testing",
    "cat orchestrator/tickets/open/L-0001-x.md 2>/dev/null | head",
    "ls orchestrator/.state",
    "git status && git diff",
    "git log --oneline -5",
])
def test_bash_allowed(ws, cmd):
    assert evaluate(ws, bash(cmd)).allow


@pytest.mark.parametrize("cmd,word", [
    ("mv orchestrator/tickets/open/L-0001-x.md orchestrator/tickets/done/", "orch"),
    ("rm orchestrator/tickets/backlog/L-0002-y.md", "orch"),
    ("sed -i 's/status: open/status: done/' orchestrator/tickets/open/L-0001-x.md", "orch"),
    ("echo x >> orchestrator/tickets/open/L-0001-x.md", "orch"),
    ("rm -rf orchestrator/.state", "orch"),
    ("git mv tickets/open/L-0001-x.md tickets/done/L-0001-x.md", "orch"),
    ("git commit -m 'L-0001 x'", "commit"),
    ('git -C "my repo" commit -m x', "commit"),
    ("git push origin main", "push"),
    ("gh pr create --fill", "PR"),
    ("glab mr create", "PR"),
])
def test_bash_denied(ws, cmd, word):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and word in d.reason


def test_bash_policy_allows_git_when_permitted(configure):
    ws = configure(git={"agent_may": {"commit": True, "push": True, "open_review": True}})
    assert evaluate(ws, bash("git commit -m 'L-0001 Add x'")).allow
    assert evaluate(ws, bash("git push -u origin feature/x")).allow
    assert evaluate(ws, bash("gh pr create --draft")).allow
    assert not evaluate(ws, bash("git commit --no-verify -m x")).allow
    assert not evaluate(ws, bash("git commit -n -m x")).allow
    assert not evaluate(ws, bash("git push --no-verify")).allow


def test_bash_push_dry_run_allowed_even_when_forbidden(ws):
    assert evaluate(ws, bash("git push --dry-run")).allow
    assert evaluate(ws, bash("git push -n")).allow


@pytest.mark.parametrize("cwd_attr,expect_allow", [("root", True), ("tickets_dir", False)])
def test_bash_cwd_determines_state_touch(ws, cwd_attr, expect_allow):
    cwd = getattr(ws, cwd_attr)
    d = evaluate(ws, {**bash("mv notes.txt archive/notes.txt"), "cwd": str(cwd)})
    assert d.allow == expect_allow


def test_bash_bare_mv_from_ticket_status_cwd_denied(ws):
    d = evaluate(ws, {**bash("mv L-0001-x.md /tmp/"), "cwd": str(ws.status_dir("open"))})
    assert not d.allow and "orch" in d.reason


def test_bash_cd_into_tickets_then_mv_denied(ws):
    d = evaluate(ws, bash("cd orchestrator/tickets && mv open/L-0001-x.md done/"))
    assert not d.allow and "orch" in d.reason


def test_bash_cd_into_lookalike_dir_allowed(ws):
    d = evaluate(ws, bash("cd ~/my-tickets-backup && rm -rf old"))
    assert d.allow


def test_bash_quoted_angle_bracket_not_a_redirect(ws):
    assert evaluate(ws, {**bash('grep ">" open/L-0001-x.md'), "cwd": str(ws.tickets_dir)}).allow
    assert evaluate(ws, {**bash('echo "a -> b" open/L-0001-x.md'), "cwd": str(ws.tickets_dir)}).allow


def test_bash_unquoted_redirect_via_variable_still_denied(ws):
    cmd = 'f=orchestrator/tickets/open/L-0001-x.md; echo x >> "$f"'
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "orch" in d.reason


def test_bash_cwd_in_tickets_read_stays_allowed(ws):
    d = evaluate(ws, {**bash("cat open/L-0001-x.md 2>/dev/null"), "cwd": str(ws.tickets_dir)})
    assert d.allow


def test_bash_redirect_outside_state_stays_allowed(ws):
    d = evaluate(ws, {**bash("ls > /tmp/out.txt"), "cwd": str(ws.root)})
    assert d.allow


def test_bash_indirect_redirect_write_denied(ws):
    cmd = 'f=orchestrator/tickets/open/L-0001-x.md; echo x >> "$f"'
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "orch" in d.reason


def test_bash_interpreter_write_denied(ws):
    cmd = 'python3 -c "open(\'orchestrator/tickets/open/L-0001-x.md\', \'a\').write(\'x\')"'
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "orch" in d.reason


def _edit(path, old, new):
    return {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": old, "new_string": new}}


def test_body_edit_allowed(ws, put):
    tid = put("in-progress", sections={"Plan": "1. old step"})
    path = store.resolve(ws, tid).path
    assert evaluate(ws, _edit(path, "1. old step", "1. new step")).allow


@pytest.mark.parametrize("old,new,what", [
    ("status: in-progress", "status: done", "status"),
    ("approved: null", "approved: '2026-09-30T09:00Z'", "gates"),
    ("session: null", "session: abc", "claim"),
    ("id: L-0001", "id: L-0009", "id"),
])
def test_protected_frontmatter_denied(ws, put, old, new, what):
    tid = put("in-progress")
    path = store.resolve(ws, tid).path
    d = evaluate(ws, _edit(path, old, new))
    assert not d.allow and what in d.reason


def test_answer_edit_denied_and_new_unanswered_question_allowed(ws, put):
    q = {"id": "Q1", "text": "?", "type": "text", "options": [], "blocking": True,
         "answer": None, "note": None, "answered": None, "via": None}
    tid = put("in-progress", questions=[q])
    path = store.resolve(ws, tid).path
    text = path.read_text(encoding="utf-8")
    forged = text.replace("  answer: null", "  answer: sure", 1)
    assert not evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": forged}}).allow
    added = text.replace("questions:\n", "questions:\n- id: Q2\n  text: new?\n  answer: null\n", 1)
    assert evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": added}}).allow


def test_multiedit_applies_edits_in_order(ws, put):
    tid = put("in-progress", sections={"Plan": "a"})
    path = store.resolve(ws, tid).path
    payload = {"tool_name": "MultiEdit", "tool_input": {"file_path": str(path), "edits": [
        {"old_string": "\na\n", "new_string": "\nb\n"},
        {"old_string": "status: in-progress", "new_string": "status: testing"},
    ]}}
    assert not evaluate(ws, payload).allow


def test_other_file_rules(ws, put):
    tid = put("open")
    path = store.resolve(ws, tid).path
    new_ticket = ws.status_dir("backlog") / "L-0099-fake.md"
    assert not evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(new_ticket), "content": "---\nid: L-0099\n---\n"}}).allow
    assert not evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(ws.state_dir / "events.jsonl"), "content": ""}}).allow
    assert not evaluate(ws, _edit(path, "---\nid", "--\nid")).allow  # would break frontmatter
    assert evaluate(ws, _edit(path, "not in the file", "x")).allow  # the Edit tool will fail on its own
    assert evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(ws.root / "src" / "a.py"), "content": "x"}}).allow
    assert evaluate(ws, {"tool_name": "Read", "tool_input": {"file_path": str(path)}}).allow
    rel = path.relative_to(ws.root).as_posix()
    assert not evaluate(ws, _edit(rel, "status: open", "status: done")).allow  # relative paths resolve from the workspace root


def _run_guard(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    return run(["guard"])


def test_cli_guard_blocks_with_exit_2(ws_root, monkeypatch, capsys):
    assert _run_guard(monkeypatch, {**bash("git push"), "cwd": str(ws_root)}) == 2
    assert "orch guard:" in capsys.readouterr().err
    assert _run_guard(monkeypatch, {**bash("ls"), "cwd": str(ws_root)}) == 0


def test_cli_guard_allows_outside_workspace_and_on_bad_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _run_guard(monkeypatch, {**bash("git push"), "cwd": str(tmp_path)}) == 0
    assert _run_guard(monkeypatch, "not json") == 0


def test_cli_guard_fails_closed_and_logs(ws_root, ws, monkeypatch):
    import orch.hooks.guard as guard

    def boom(ws, payload):
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "evaluate", boom)
    assert _run_guard(monkeypatch, {**bash("ls"), "cwd": str(ws_root)}) == 2  # inside a workspace: refused
    assert "RuntimeError: bug" in (ws.state_dir / "guard-errors.log").read_text(encoding="utf-8")


def test_cli_guard_refuses_on_workspace_open_failure(ws_root, monkeypatch, capsys):
    import orch.core.workspace as workspace_mod

    def boom(start=None):
        raise OSError("disk fell off")

    monkeypatch.setattr(workspace_mod.Workspace, "open", staticmethod(boom))
    assert _run_guard(monkeypatch, {**bash("ls"), "cwd": str(ws_root)}) == 2
    err = capsys.readouterr().err
    assert "the workspace cannot be read" in err and "OSError" in err


# -- final review fixes ------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "git push --dry-run && git push origin main",
    "git push -n; git push",
    "git push --dry-run | cat\ngit push origin main",
    "git status || git push",
])
def test_bash_push_checked_per_segment(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "push" in d.reason


def test_bash_push_dry_run_segments_all_allowed(ws):
    assert evaluate(ws, bash("git push --dry-run && git push -n origin main")).allow


@pytest.mark.parametrize("cmd", [
    "git commit --no-veri -m x",
    "git commit --no-v -m x",
    "git commit --no-verif -m x",
    "git push --no-ver origin main",
    "git -c core.hooksPath=/dev/null commit -m x",
    "git -c 'core.hooksPath=/dev/null' commit -m x",
    "git -C repo -c core.hookspath= commit -m x",
    "git config core.hooksPath /dev/null",
    "git config --local core.hooksPath /tmp/none",
    "git config --global core.hooksPath /tmp/none",
    "git config --worktree core.hooksPath /tmp/none",
    "git config --unset core.hooksPath",
    "sh -c \"git commit --no-verify -m x\"",
])
def test_bash_hook_bypass_denied(configure, cmd):
    ws = configure(git={"agent_may": {"commit": True, "push": True, "open_review": True}})
    d = evaluate(ws, bash(cmd))
    assert not d.allow and ("no-verify" in d.reason or "hooksPath" in d.reason)


@pytest.mark.parametrize("cmd", [
    "git config --get core.hooksPath",
    "git config --local --get core.hooksPath",
    "git config get core.hooksPath",
    "git config --list",
    "git commit --no-edit --amend",
])
def test_bash_hooks_path_reads_allowed(configure, cmd):
    ws = configure(git={"agent_may": {"commit": True, "push": True, "open_review": True}})
    assert evaluate(ws, bash(cmd)).allow


def test_bash_quoted_text_is_not_a_git_command(configure, ws):
    permissive = configure(git={"agent_may": {"commit": True, "push": True, "open_review": True}})
    assert evaluate(permissive, bash('git commit -m "L-0001 Fix -n handling"')).allow
    assert evaluate(permissive, bash("git commit -m 'L-0001 Drop --no-verify from docs'")).allow
    strict = configure()
    assert evaluate(strict, bash('echo "prepare, then git commit later"')).allow
    assert evaluate(strict, bash("echo 'run gh pr create when ready'")).allow
    assert evaluate(strict, bash('echo "git push origin main"')).allow


@pytest.mark.parametrize("cmd", [
    'sh -c "git commit -m x"',
    "bash -lc 'git push origin main'",
    'eval "gh pr create --fill"',
])
def test_bash_shell_c_payload_still_checked(ws, cmd):
    assert not evaluate(ws, bash(cmd)).allow


@pytest.mark.parametrize("cmd", [
    'sh -c "mv orchestrator/tickets/open/L-0001-x.md /tmp/"',
    "find orchestrator/tickets -name '*.md' -delete",
    "git checkout -- orchestrator/tickets/open/L-0001-x.md",
    "git restore orchestrator/tickets/open/L-0001-x.md",
    "dd if=/dev/zero of=orchestrator/.state/events.jsonl count=1",
])
def test_bash_more_write_forms_denied(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "orch" in d.reason


@pytest.mark.parametrize("cmd", [
    "find orchestrator/tickets -name '*.md'",
    "git checkout main",
    "git restore src/app.py",
])
def test_bash_read_forms_near_state_allowed(ws, cmd):
    assert evaluate(ws, bash(cmd)).allow


def test_cli_guard_reports_broken_config(ws_root, monkeypatch, capsys):
    (ws_root / "orchestrator" / "config.json").write_text("{not json", encoding="utf-8")
    assert _run_guard(monkeypatch, {**bash("ls"), "cwd": str(ws_root)}) == 2
    err = capsys.readouterr().err
    assert "the workspace cannot be read" in err and "invalid JSON" in err


def test_cli_guard_silent_outside_workspace(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert _run_guard(monkeypatch, {**bash("git push"), "cwd": str(tmp_path)}) == 0
    assert capsys.readouterr().err == ""


# -- command substitutions inside quotes are executed by the shell -------------------------

@pytest.mark.parametrize("cmd", [
    'git commit -m "$(git push origin main)"',
    "git commit --allow-empty -m '$(git push origin main)'",
    'git commit -m "$(gh pr create --fill)"',
    'git commit -m "$(git commit --no-verify -m x)"',
    'git commit -m "`git push origin main`"',
    'git commit -m "x $(echo a; git push) y"',
])
def test_bash_command_substitution_payload_checked(configure, cmd):
    ws = configure(git={"agent_may": {"commit": True, "push": False, "open_review": False}})
    assert not evaluate(ws, bash(cmd)).allow


def test_bash_nested_command_substitution_checked(ws):
    assert not evaluate(ws, bash('echo "$(echo "$(git push)")"')).allow


def test_bash_plain_quoted_text_still_allowed_with_substitution_checks(configure):
    ws = configure(git={"agent_may": {"commit": True, "push": False, "open_review": False}})
    assert evaluate(ws, bash('git commit -m "L-0001 Fix -n handling"')).allow
    assert evaluate(ws, bash('echo "prepare, then git commit later"')).allow
    assert evaluate(ws, bash('git commit -m "L-0001 Use $(date) in logs"')).allow


@pytest.mark.parametrize("cmd", [
    "orch serve",
    "orch serve --no-open --port 9",
    "nohup orch serve &",
    "nohup orch serve > /dev/null 2>&1 &",
    'sh -c "orch serve --port 9"',
    "bash -lc 'orch serve'",
    "echo $(orch serve --lan)",
    "script -q /dev/null orch serve",
    'script -q -c "orch serve" /dev/null',
    "uv run orch serve",
    "cd /tmp && orch serve",
    "python -m orch.cli serve",
])
def test_bash_denies_starting_the_dashboard(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "dashboard is the human's" in d.reason


@pytest.mark.parametrize("cmd", ["orch show L-0001", "orch server-status", "grep serve README.md"])
def test_bash_allows_other_orch_commands(ws, cmd):
    assert evaluate(ws, bash(cmd)).allow


@pytest.mark.parametrize("cmd", ["orch wait L-0001 --json", "orch wait L-0001 --timeout 600 --after 12 --json",
                                 "uv run orch wait L-0001 --json", "cd /tmp && orch wait L-0001"])
def test_bash_allows_agents_to_wait(ws, cmd):
    """orch wait is read-only and meant for agents (no lock, no event, no human check)."""
    assert evaluate(ws, bash(cmd)).allow


def test_rules_do_not_make_wait_human_only(ws):
    from orch.core.rules import render_rules
    human_only = next(line for line in render_rules(ws.config).split("\n") if line.startswith("human-only:"))
    assert "wait" not in human_only


# -- plugin hook mode: deny as JSON, never exit 2 ----------------------------------------


def _run_guard_json(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    return run(["guard", "--hook-json"])


def test_cli_guard_hook_json_denies_with_json_and_exit_0(ws_root, monkeypatch, capsys):
    assert _run_guard_json(monkeypatch, {**bash("git push"), "cwd": str(ws_root)}) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "orch guard: agents do not push in this workspace (git.agent_may.push is false)"}}


def test_cli_guard_hook_json_allow_prints_nothing(ws_root, monkeypatch, capsys):
    assert _run_guard_json(monkeypatch, {**bash("ls"), "cwd": str(ws_root)}) == 0
    assert capsys.readouterr().out == ""
    assert _run_guard_json(monkeypatch, "not json") == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("cmd", [
    '"/Users/me/.claude/plugins/cache/orch-core/bin/orch" serve',
    '"${CLAUDE_PLUGIN_ROOT}/bin/orch" serve',
    "'/a b/bin/orch' serve",
    'sh "/p q/bin/orch" serve --no-open',
    '"orch" serve',
    '"/x/bin/orch" --quiet serve --port 9',
    'cd /tmp && "${CLAUDE_PLUGIN_ROOT}/bin/orch" serve --lan',
])
def test_bash_denies_quoted_wrapper_serve(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "dashboard is the human's" in d.reason


@pytest.mark.parametrize("cmd", ["orchestra serve", '"orchestra" serve', '"${CLAUDE_PLUGIN_ROOT}/bin/orch" show L-0001'])
def test_bash_quoted_serve_no_false_positives(ws, cmd):
    assert evaluate(ws, bash(cmd)).allow


def test_bash_echo_quoted_orch_serve_unchanged(ws):
    assert not evaluate(ws, bash('echo "orch serve docs"')).allow  # conservative, as before


@pytest.mark.parametrize("cmd", [
    "orch addon install ./my-addon",
    "orch addon update --all",
    "orch addon update hello --check",
    "orch addon trust hello",
    "orch addon enable hello",
    "orch addon disable hello",
    "orch addon remove hello",
    "orch addon rollback hello",
    "uv run orch addon enable hello",
    "python -m orch.cli addon install .",
    '"${CLAUDE_PLUGIN_ROOT}/bin/orch" addon trust hello',
    "'/a b/bin/orch' addon enable hello",
    'sh -c "orch addon trust hello"',
    "cd /tmp && orch --quiet addon trust hello",
    "echo {} > ~/.config/orch/addons.json",
    "cp evil.json $HOME/.config/orch/workspaces.json",
    "rm -rf ~/.config/orch/addons/hello",
    "tee $XDG_CONFIG_HOME/orch/addons.json < x",
])
def test_guard_denies_addon_admin(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "human's" in d.reason, cmd


@pytest.mark.parametrize("cmd", [
    "orch addon list", "orch addon check ./my-addon", "orch addon check ./my-addon --static",
    "cat ~/.config/orch/addons.json", "sed -i 's/a/b/' src/orch/addons/loader.py",
    "git commit -m 'orch addon trust is human-only'",
])
def test_guard_allows_addon_reads_and_code_edits(ws, configure, cmd):
    ws = configure(git={"agent_may": {"commit": True, "push": False, "open_review": False}})
    assert evaluate(ws, bash(cmd)).allow, cmd


@pytest.mark.parametrize("cmd", [
    'python -c "from orch.addons import manage; manage.trust_addon(\'x\')"',
    "python3 -c 'import orch.addons.manage as m; m.enable(\".\", \"x\")'",
    'uv run python -c "from orch.addons.userfiles import record_trust"',
    "uv run python - <<'EOF'\nfrom orch.addons import userfiles\nuserfiles.set_enabled('.', 'x', True)\nEOF",
    "cat > /tmp/x.py <<EOF\nfrom orch.addons.manage import install\nEOF",
    'python -c "import orch.addons.userfiles as u; u.update_json(p, f)"',
])
def test_guard_denies_interpreter_payloads_that_reach_addon_admin(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "human's" in d.reason, cmd


@pytest.mark.parametrize("cmd", [
    "cd ~/.config/orch && echo {} > addons.json",
    "cd ~/.config && cp evil.json orch/workspaces.json",
    'D=~/.config/orch; echo {} > "$D/addons.json"',
    'CFG="$HOME/.config"; rm -rf "$CFG/orch/addons/hello"',
    "cd $XDG_CONFIG_HOME/orch/addons && rm -rf hello",
])
def test_guard_denies_config_writes_through_cd_or_variables(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "human's" in d.reason, cmd


@pytest.mark.parametrize("cmd", [
    "uv run pytest tests/test_addon_manage.py tests/test_addon_userfiles.py -q",
    "python -c 'print(1)' > out.txt",
    "grep -n set_enabled src/orch/addons/userfiles.py",
    "cd ~/.config/orch && cat addons.json",
    "echo hi > notes.txt && ls ~/.config",
])
def test_guard_admin_rules_do_not_over_deny(ws, cmd):
    assert evaluate(ws, bash(cmd)).allow, cmd


def test_guard_denies_editing_user_addon_files(ws):
    from orch.dashboard.launch import config_dir
    for target in (config_dir() / "addons.json", config_dir() / "workspaces.json",
                   config_dir() / "addons" / "hello" / "hello" / "__init__.py"):
        d = evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": "{}"}})
        assert not d.allow and "human's" in d.reason, target
    # launch.json holds the commands Start agent runs in the human's terminal: the human's, like the ledger
    assert not evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(config_dir() / "launch.json"), "content": "{}"}}).allow


def test_rules_name_addon_admin_as_human_only(ws):
    from orch.core.rules import render_rules
    assert "addon install/update/trust/enable/disable/remove/rollback" in render_rules(ws.config)
def test_direct_edit_of_the_tasks_section_denied(ws, put):
    tid = put("in-progress", sections={"Tasks": "- [ ] T1 a", "Current state": "old"})
    path = store.resolve(ws, tid).path
    d = evaluate(ws, _edit(path, "- [ ] T1 a", "- [x] T1 a"))
    assert not d.allow and "orch task" in d.reason
    assert evaluate(ws, _edit(path, "old", "new")).allow  # other sections stay editable


# -- remote-humans.json: the phone pairing keys (Task 7) ---------------------------------

@pytest.mark.parametrize("cmd", ["cat ~/.config/orch/remote-humans.json", "python -c \"open('remote-humans.json')\"",
                                 "cp /tmp/x \"$HOME/.config/orch/remote-humans.json\"", "jq . remote-humans.json"])
def test_guard_denies_agents_the_pairing_keys(ws, cmd):
    from orch.hooks.guard import evaluate
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
def test_guard_denies_file_tools_on_the_pairing_keys(ws, tool, tmp_path):
    from orch.hooks.guard import evaluate
    p = str(tmp_path / ".config" / "orch" / "remote-humans.json")
    assert not evaluate(ws, {"tool_name": tool, "tool_input": {"file_path": p, "content": "{}"}}).allow


@pytest.mark.parametrize("cmd", [
    "cat ~/.config/orch/remote-humans*",
    "cat ~/.config/orch/*",
    "grep -r ph_ ~/.config/orch",
    "rg key $XDG_CONFIG_HOME/orch/",
    "cd ~/.config/orch && cat r*.json",
    "cd ~/.config/orch && grep -r key .",
    "tar czf /tmp/x.tgz ~/.config/orch",
    'python -c "from orch.remote import store; print(store.phones(\'.\'))"',
    "uv run python -c 'import orch.remote.store as s'",
    "base64 < REMOTE-HUMANS.JSON",
])
def test_guard_denies_other_ways_to_the_pairing_keys(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "pairing keys" in d.reason, cmd


@pytest.mark.parametrize("cmd", [
    "cat ~/.config/orch/addons.json", "cd ~/.config/orch && cat addons.json", "ls ~/.config",
    "grep -rn remote src/orch/addons", "uv run pytest tests/test_remote_verify.py -q",
])
def test_pairing_key_rules_do_not_over_deny(ws, cmd):
    assert evaluate(ws, bash(cmd)).allow, cmd


def test_guard_denies_reading_the_real_file_and_a_link_to_it(ws, tmp_path):
    from orch.remote import store as phones
    phones.pair(ws.root, label="iPhone", addon="x")
    link = tmp_path / "innocent.txt"
    link.symlink_to(phones.path())
    for p in (phones.path(), link):
        assert not evaluate(ws, {"tool_name": "Read", "tool_input": {"file_path": str(p)}}).allow, p
    assert evaluate(ws, {"tool_name": "Read", "tool_input": {"file_path": str(ws.root / "README.md")}}).allow


def test_guard_denies_grep_over_the_pairing_keys(ws):
    from orch.dashboard.launch import config_dir
    from orch.remote import store as phones
    for p in (str(phones.path()), str(config_dir())):
        assert not evaluate(ws, {"tool_name": "Grep", "tool_input": {"pattern": "key", "path": p}}).allow, p
    assert not evaluate(ws, {"tool_name": "Grep", "tool_input": {"pattern": "k", "glob": "remote-humans.json"}}).allow
    assert evaluate(ws, {"tool_name": "Grep", "tool_input": {"pattern": "k", "path": str(ws.root)}}).allow
    assert evaluate(ws, {"tool_name": "Read", "tool_input": {"file_path": str(config_dir() / "addons.json")}}).allow


def test_hook_matchers_include_read_and_grep():
    import json as _json
    from pathlib import Path
    from orch.instructions.settings import GUARD_MATCHER
    hooks = _json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for matcher in (hooks["hooks"]["PreToolUse"][0]["matcher"], GUARD_MATCHER):
        assert set(matcher.split("|")) >= {"Bash", "Edit", "Write", "MultiEdit", "Read", "Grep"}


def test_hook_matchers_include_glob_and_notebookedit():
    import json as _json
    from pathlib import Path
    from orch.instructions.settings import GUARD_MATCHER
    hooks = _json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for matcher in (hooks["hooks"]["PreToolUse"][0]["matcher"], GUARD_MATCHER):
        assert set(matcher.split("|")) == {"Bash", "Edit", "Write", "MultiEdit", "Read", "Grep", "Glob", "NotebookEdit"}


# -- Grep/Read/Glob on an ancestor of the config dir (incl. the default cwd) (batch 5 review I2) --------------

def test_guard_denies_grep_on_an_ancestor_of_the_config_dir(ws):
    from orch.dashboard.launch import config_dir
    ancestor = str(config_dir().parent)
    assert not evaluate(ws, {"tool_name": "Grep", "tool_input": {"pattern": "key", "path": ancestor}}).allow
    # no path at all: the tool falls back to cwd
    assert not evaluate(ws, {"tool_name": "Grep", "tool_input": {"pattern": "key"}, "cwd": ancestor}).allow
    # a glob that plainly cannot match remote-humans.json is fine
    assert evaluate(ws, {"tool_name": "Grep", "tool_input": {"pattern": "key", "path": ancestor, "glob": "*.py"}}).allow
    # a glob that could still reach it (an extension it has, or a recursive one) is denied
    assert not evaluate(ws, {"tool_name": "Grep", "tool_input": {"pattern": "key", "path": ancestor, "glob": "*.json"}}).allow
    assert not evaluate(ws, {"tool_name": "Grep", "tool_input": {"pattern": "key", "path": ancestor, "glob": "**/*.py"}}).allow


def test_guard_denies_glob_on_an_ancestor_of_the_config_dir(ws):
    from orch.dashboard.launch import config_dir
    ancestor = str(config_dir().parent)
    assert not evaluate(ws, {"tool_name": "Glob", "tool_input": {"pattern": "**/*"}, "cwd": ancestor}).allow
    assert not evaluate(ws, {"tool_name": "Glob", "tool_input": {"pattern": "*", "path": ancestor}}).allow
    assert not evaluate(ws, {"tool_name": "Glob", "tool_input": {"pattern": "remote*", "path": str(config_dir())}}).allow
    assert evaluate(ws, {"tool_name": "Glob", "tool_input": {"pattern": "*.py", "path": ancestor}}).allow
    assert evaluate(ws, {"tool_name": "Glob", "tool_input": {"pattern": "*.py"}, "cwd": str(ws.root)}).allow


def test_guard_denies_reading_an_ancestor_directory_of_the_config_dir(ws):
    from orch.dashboard.launch import config_dir
    assert not evaluate(ws, {"tool_name": "Read", "tool_input": {"file_path": str(config_dir().parent)}}).allow
    assert not evaluate(ws, {"tool_name": "Read", "tool_input": {"file_path": str(config_dir())}}).allow
    assert evaluate(ws, {"tool_name": "Read", "tool_input": {"file_path": str(ws.root)}}).allow


# -- Bash normalisation and ancestor-of-config-dir bypasses (batch 5 review I3) --------------------------------

@pytest.mark.parametrize("cmd", [
    'D=~/.config/orch; cat "$D/remote-humans.json"',
    'D=~/.config/orch; grep -r key "$D"',
    r"cat ~/.config/orch/remote\-humans.json",
    r'cat ~/.config/orch/"remote-humans.json"',
    "cat ~/.config/orch/remote-humans.json{,.bak}",
    "cat re{mote-hu,}mans.json",
    "tar czf /tmp/x.tgz ~/.config",
    "tar czf /tmp/x.tgz $HOME/.config",
    'find ~/.config -name "*.json" -exec cat {} +',
    "rsync -a ~/.config/ /tmp/leak/",
    "cp -r ~/.config /tmp/leak",
    "ln -s ~/.config/orch /tmp/innocent_dir",
    "ln -s $HOME/.config /tmp/leak",
])
def test_guard_denies_normalised_and_ancestor_bypasses_of_the_pairing_keys(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow, cmd


@pytest.mark.parametrize("cmd", [
    "ls ~/.config", "cd ~ && ls -la", 'find ~/project -name "*.py"', "cat $HOME/.bashrc",
    "D=src/orch; cat $D/cli.py", "echo {a,b,c}", "tar czf /tmp/x.tgz ~/project",
])
def test_the_normalisation_and_ancestor_rules_do_not_over_deny(ws, cmd):
    assert evaluate(ws, bash(cmd)).allow, cmd


# -- Final review: Grep type/brace/negated filters on an ancestor of the config dir ----------------------------

@pytest.mark.parametrize("extra", [{"type": "json"}, {"type": "jsonl"}, {"type": "all"}, {"type": "config"},
                                   {"glob": "*.{json,md}"}, {"glob": "!*.md"}, {"glob": "{remote,x}*"}])
def test_guard_denies_grep_filters_that_could_still_reach_the_pairing_keys(ws, extra):
    from orch.dashboard.launch import config_dir
    ancestor = str(config_dir().parent)
    assert not evaluate(ws, {"tool_name": "Grep", "tool_input": {"pattern": "key", "path": ancestor, **extra}}).allow


@pytest.mark.parametrize("pattern", ["*.{json,md}", "!*.md", "{a,remote}*"])
def test_guard_denies_glob_patterns_with_braces_or_negation_on_an_ancestor(ws, pattern):
    from orch.dashboard.launch import config_dir
    ancestor = str(config_dir().parent)
    assert not evaluate(ws, {"tool_name": "Glob", "tool_input": {"pattern": pattern, "path": ancestor}}).allow


@pytest.mark.parametrize("extra", [{"type": "py"}, {"type": "md"}, {"type": "txt"}, {"glob": "*.md"},
                                   {"glob": "*.py", "type": "py"}])
def test_guard_allows_grep_filters_that_cannot_match_the_pairing_keys(ws, extra):
    from orch.dashboard.launch import config_dir
    ancestor = str(config_dir().parent)
    assert evaluate(ws, {"tool_name": "Grep", "tool_input": {"pattern": "key", "path": ancestor, **extra}}).allow


# -- Final review: symlink flags in any position ---------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["ln -sf ~/.config/orch /tmp/x", "ln -sfn ~/.config/orch /tmp/x",
                                 "ln --symbolic ~/.config/orch /tmp/x", "ln -f --symbolic ~/.config /tmp/x",
                                 "ln -nfs $HOME/.config/orch /tmp/x"])
def test_guard_denies_symlinks_to_the_config_dir_with_any_flag_spelling(ws, cmd):
    assert not evaluate(ws, bash(cmd)).allow, cmd


@pytest.mark.parametrize("cmd", ["ln -sf src/a /tmp/x", "ln ~/.config/orch/addons.json /tmp/x.json",
                                 "echo ln -s is a flag"])
def test_symlink_rule_does_not_over_deny(ws, cmd):
    assert evaluate(ws, bash(cmd)).allow, cmd


# -- Final review: the literal home path is an ancestor too ----------------------------------------------------

def _home_forms():
    from pathlib import Path
    return sorted({str(Path.home()), str(Path.home().resolve())})


@pytest.mark.parametrize("template", ["tar czf /tmp/a.tgz {h}", "rg key {h}", "ln -s {h} /tmp/h",
                                      "grep -r key {h}/", "cp -r {h} /tmp/h"])
def test_guard_denies_recursive_reads_of_the_literal_home_path(ws, template):
    for home in _home_forms():
        cmd = template.format(h=home)
        assert not evaluate(ws, bash(cmd)).allow, cmd


def test_literal_home_path_rule_does_not_over_deny(ws):
    for home in _home_forms():
        for cmd in (f"ls {home}", f"cat {home}/.bashrc", f"tar czf /tmp/a.tgz {home}/project"):
            assert evaluate(ws, bash(cmd)).allow, cmd


# -- Final review: cheap Bash variants (cd flags, pushd, export/declare, $(echo), path normalisation,
# long recursive flags, ditto/pax, relative paths against the payload cwd) --------------------------------------

@pytest.mark.parametrize("cmd", [
    "cd -- ~/.config/orch && cat *",
    "cd -P ~/.config/orch && cat *",
    "pushd ~/.config/orch && cat *",
    "export D=~/.config/orch; cat $D/*",
    "declare D=~/.config/orch; grep -r key $D",
    "cat $(echo ~/.config/orch)/*",
    "cat `echo ~/.config/orch`/*",
    "cat ~/.config/./orch/*",
    "cat ~//.config/orch/*",
    "cat ~/.config/x/../orch/*",
    "tar czf /tmp/x.tgz ~/.config/orch/..",
    "cp --recursive ~/.config /tmp/x",
    "grep --recursive key ~/.config",
    "ditto ~/.config /tmp/x",
    "pax -w ~/.config > /tmp/x.pax",
])
def test_guard_denies_cheap_bash_variants_on_the_config_dir(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "pairing keys" in d.reason, cmd


@pytest.mark.parametrize("where,cmd", [
    ("parent", "tar czf /tmp/x.tgz {name}"),
    ("parent", "cat {name}/*"),
    ("parent", "grep -r key ."),
    ("parent", "cd {name} && cat r*"),
    ("dir", "cat *"),
    ("dir", "tar czf /tmp/x.tgz ."),
    ("dir", "ln -s . /tmp/x"),
    ("home", "rsync -a .config /tmp/x"),
    ("home", "cat .config/orch/./*"),
])
def test_guard_resolves_relative_bash_paths_against_the_payload_cwd(ws, where, cmd):
    from pathlib import Path
    from orch.dashboard.launch import config_dir
    cwd = {"parent": config_dir().parent, "dir": config_dir(), "home": Path.home()}[where]
    cmd = cmd.format(name=config_dir().name)  # the test config dir is not named `orch`
    d = evaluate(ws, {**bash(cmd), "cwd": str(cwd)})
    assert not d.allow and "pairing keys" in d.reason, (where, cmd)


@pytest.mark.parametrize("cmd", [
    "cd -- src && cat *.py", "pushd src && ls", "export D=src; cat $D/x", "declare -a A=(1 2)",
    "cat ./README.md", "ls ../x", "grep --recursive foo src", "cp --recursive src /tmp/x", "ditto src /tmp/x",
    "cat $(echo README.md)", "grep -r key .", "tar czf /tmp/x.tgz .", "cat *", "ln -s src /tmp/x",
])
def test_cheap_bash_variant_rules_do_not_over_deny(ws, cmd):
    assert evaluate(ws, {**bash(cmd), "cwd": str(ws.root)}).allow, cmd
