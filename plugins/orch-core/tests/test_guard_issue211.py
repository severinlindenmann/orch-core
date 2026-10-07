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
