"""#211: guard false positives on commands that only mention a protected word in text, data or a read."""
import pytest

from orch.hooks.guard import evaluate


def _bash(ws, cmd, cwd=None):
    return evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd or ws.root)})


def _check(ws, cmd, allow):
    d = _bash(ws, cmd.replace("{ws}", str(ws.root)))
    assert d.allow is allow, d.reason


# --- 1. tmux / screenshot in data ----------------------------------------------------------------------------------
MUX_ALLOWED = [
    "gh issue create --title t --body 'a screenshot of the pane'",
    "echo screenshot",
    "cat > notes.md <<'EOF'\ntmux is nice; define foo() { :; } and alias x=y\nfunction f\nEOF",
    "cat > notes.md <<'EOF'\nthe screen shows a () pair\nEOF\nls notes.md",
    "ls screenshots/",
    "orch log L-0001 'fix the screenshot flow, alias handling and foo() calls'",
    "open screenshot.png && alias ll='ls -l'",
]


@pytest.mark.parametrize("cmd", MUX_ALLOWED)
def test_mux_word_in_data_is_allowed(ws, cmd):
    _check(ws, cmd, True)


MUX_DENIED = [
    "python - <<EOF\nimport subprocess\nsubprocess.run([\"tmux\",\"-L\",\"orch\"])\nEOF",
    "tmux -L orch send-keys x",
    "tmux send-keys -t x 'orch approve L-1' Enter",
    "tmux -S /tmp/../x attach",
    "f() { tmux -L orch ls; }; f",
    "alias t=tmux; t -L orch ls",
    "function t { tmux \"$@\"; }; t -L orch ls",
    "screen -S x -X stuff 'a'; foo() { :; }",
    "tmux ls; foo() { :; }",
    "cat > notes.md <<'EOF'\nscreen\nEOF\norch show L-0001; tmux ls; foo() { :; }",
    "orch show L-1 | tmux load-buffer - <<EOF\nfoo() { :; }\nEOF",
    "cat <<EOF | sh\ntmux -L orch ls\nEOF",
    "source /dev/stdin <<EOF\nf() { tmux ls; }\nEOF",
]


@pytest.mark.parametrize("cmd", MUX_DENIED)
def test_real_mux_use_stays_denied(ws, cmd):
    _check(ws, cmd, False)


# --- 2. human verbs only mentioned in quoted message text ---------------------------------------------------------
TEXT_ALLOWED = [
    "echo 'orch approve L-1'",
    'echo "run orch approve L-1 yourself"',
    "gh issue create --title t --body \"the guard blocks python scripts that run orch approve L-1\"",
    "gh issue create --title 'orch approve is human-only' --body 'node and python mention orch verdict'",
    "gh issue comment 3 --body 'a python heredoc << that calls orch approve was denied'",
    "orch log L-0001 -m 'tried python to orch approve L-1; denied'",
    "orch log L-0001 --message \"python: orch answer L-1 is the human's\"",
    "orch section set L-0001 Plan -m 'never run orch approve in perl'",
]


@pytest.mark.parametrize("cmd", TEXT_ALLOWED)
def test_verb_only_in_message_text_is_allowed(ws, cmd):
    _check(ws, cmd, True)


TEXT_DENIED = [
    'script -c "orch approve L-1" /dev/null',
    "script -q /dev/null orch approve L-1",
    "expect -c 'spawn orch approve L-1; interact'",
    "watch \"orch approve L-1\"",
    "watch -n1 'orch approve L-1'",
    "ssh host \"orch approve L-1\"",
    "bash -c \"orch approve L-1\"",
    "sh -c 'orch verdict L-1 pass'",
    "echo 'orch approve L-1' | sh",
    "echo 'orch approve L-1'; orch show L-1 | sh",
    "echo \"$(orch approve L-1)\"",
    "orch approve L-1 -m 'x'",
    "gh issue create --body x; python3 -c \"import os; os.system('orch approve L-1')\"",
    "gh issue create --body \"$(orch approve L-1)\"",
    "orch log L-0001 -m x && python3 -c 'import os; os.system(\"orch approve L-1\")'",
    "python3 - <<EOF\nimport os\nos.system('orch approve L-1')\nEOF",
]


@pytest.mark.parametrize("cmd", TEXT_DENIED)
def test_quoted_verb_run_by_anything_stays_denied(ws, cmd):
    _check(ws, cmd, False)


# --- 3. --help ----------------------------------------------------------------------------------------------------
HELP_ALLOWED = [
    "orch approve --help",
    "orch verdict --help",
    "orch request-changes --help",
    "orch epic pause --help",
    "orch permit grant --help",
    "orch checks sign --help",
    "orch schedule arm --help",
    "orch quick drop --help",
    "orch move --help",
    "orch serve --help",
    "orch approve --help; orch show L-0001",
]


@pytest.mark.parametrize("cmd", HELP_ALLOWED)
def test_bare_help_is_allowed(ws, cmd):
    _check(ws, cmd, True)


HELP_DENIED = [
    "orch approve L-1 --help",
    "orch approve L-1 --note --help",
    "orch approve --note --help",
    "orch approve --help L-1",
    "orch approve --help --note x",
    "orch approve --help; orch approve L-1",
    "orch approve --help && orch approve L-1",
    "orch approve $X --help",
    "orch approve `echo L-1` --help",
    "orch approve --help $(orch approve L-1)",
    "orch approve --help | sh",
    "orch approve --help > f; orch approve L-1",
    "orch serve --port 1 --help",
    "orch serve --remote --help",
    "env X=1 orch approve --help",
    "orch approve -h L-1",
    "orch approve L-1",
    "orch serve",
]


@pytest.mark.parametrize("cmd", HELP_DENIED)
def test_help_with_anything_else_stays_denied(ws, cmd):
    _check(ws, cmd, False)
