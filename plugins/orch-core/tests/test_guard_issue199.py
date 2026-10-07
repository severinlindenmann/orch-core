"""#199: config-dir, ticket-file and pairing-file denials on commands that touch none of them."""
import pytest

from orch.hooks.guard import evaluate


def _bash(ws, cmd, cwd=None):
    return evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd or ws.root)})


ALLOWED = [
    # 1. config dir: `cd ~` next to a listing / heredoc that has nothing to do with it
    "cd ~; DATABRICKS_HOST=https://x.example databricks workspace list /Workspace/m/.bundle/artifacts/.internal -o json; databricks jobs list",
    "cd ~ && databricks workspace list /Workspace/x -o json",
    "cd ~ && databricks jobs list --output json",
    "cd ~ && echo hi",
    "cd ~; ls /tmp; cat > x.md <<'EOF'\nplan\nEOF",
    "cd {ws} && mkdir -p orchestrator/temporary && cat > orchestrator/temporary/L-0004-plan.md <<'EOF'\n# plan\nEOF\n"
    "&& orch section set L-0004 Plan --file orchestrator/temporary/L-0004-plan.md && orch task add L-0004 'x'",
    # 2. ticket file named as an argument of a read-only command in the same call
    "orch section set L-0004 \"Acceptance criteria\" --file orchestrator/temporary/L-0004-ac.md\n"
    "orch show L-0004 --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[\"gates\"])'",
    "orch section set L-0004 AC --file orchestrator/temporary/L-0004-ac.md && orch show L-0004 --json | python3 -c 'import sys; print(1)'",
    # 3. pairing file name only mentioned in text
    "gh issue create --repo a/b --title t --body \"the guard denied remote-humans.json for a mention\"",
    "gh issue create --repo a/b --title t --body 'denied: the orch config dir holds remote-humans.json'",
    "gh issue comment 3 --body 'see remote-humans.json'",
    # more of the same shapes
    "cd ~ && databricks workspace list /W -o json | jq '.[] | select(.path)'",
    "cd ~ && curl -s 'https://a.example/x?y=1' | jq '.items[]'",
    "cd ~ && databricks bundle find x",
    "cd ~ && ls /Workspace/a/*",
    "cd ~ && cat > n.md <<'EOF'\nfind every *.md and tar them? ok\nEOF\norch section set L-0004 Plan --file n.md",
    "cd ~ && mkdir -p w && cat > w/plan.md <<'EOF'\nrun find * in ~/.config? no\nEOF\n&& orch task add L-0004 'x'",
    "cat > orchestrator/temporary/L-0004-plan.md <<'EOF'\nplan\nEOF\norch section set L-0004 Plan --file orchestrator/temporary/L-0004-plan.md",
    "cp /tmp/x.md orchestrator/artifacts/L-0004-notes.md",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_unrelated_commands_are_allowed(ws, cmd):
    d = _bash(ws, cmd.replace("{ws}", str(ws.root)))
    assert d.allow, d.reason


DENIED = [
    # the config dir, read wholesale after a cd
    "cd ~ && cat *",
    "cd ~/.config && tar czf /tmp/x.tgz orch",
    "cd ~ && find . -name '*.json'",
    "cd ~ && sudo find .config",
    "cd ~ && env X=1 grep -r key .config",
    "cd ~ && grep -r key .config",
    "cd ~/.config/orch && cat *",
    "cd ~ && databricks jobs list; cat .config/orch/*",
    "cat > f <<'EOF'\ntar czf /tmp/x ~/.config/orch\nEOF\nbash f && orch show L-0004",
    # the pairing file
    "cat remote-humans.json",
    "gh issue create --body \"$(cat ~/.config/orch/remote-humans.json)\"",
    "gh issue create --body x; cat remote-humans.json",
    "gh issue create --body x ~/.config/orch/remote-humans.json",
    "cat > n.md <<'EOF'\nsee remote-humans.json\nEOF\norch show L-0004",
    # ticket files
    "rm orchestrator/tickets/open/L-0004-x.md",
    "cd orchestrator/tickets/open && rm L-0004-x.md",
    "mv L-0004-x.md /tmp",
    "rm orchestrator/temporary/../tickets/open/L-0004-x.md",
    "echo x >> orchestrator/tickets/open/L-0004-x.md && orch show L-0004",
    "cat > f <<'EOF'\nrm L-0004-x.md\nEOF\nbash f; rm L-0004-x.md",
]


@pytest.mark.parametrize("cmd", DENIED)
def test_real_accesses_are_still_denied(ws, cmd):
    assert not _bash(ws, cmd).allow, cmd
