"""#13: heredoc bodies are data only when they feed a known text sink and nothing in the command runs code."""
import pytest

from orch.hooks.guard import evaluate


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


@pytest.mark.parametrize("cmd", [
    "cat > notes.md <<'EOF'\nrun orch serve to open it\nEOF",
    "cat > notes.md <<EOF\nrun orch serve to open it\nEOF",
    'cat > notes.md <<"EOF"\nrun orch serve to open it\nEOF',
    "cat > notes.md <<-EOF\n\trun orch serve to open it\n\tEOF",
    "cat > notes.md <<'EOF'\nthe human runs orch addon trust x\nEOF",
    "cat > notes.md <<'EOF'\nthen git push origin main\nEOF",
    "cat > notes.md <<'EOF'\nnever git commit --no-verify\nEOF",
    "cat > notes.md <<'EOF'\nthen gh pr create\nEOF",
    "cat > notes.md <<'EOF'\nfiles live in orchestrator/tickets/open\nEOF",
    "cat > notes.md <<'EOF'\nthe human runs orch approve L-1 plan and orch verdict L-1 done\nEOF",
    "gh issue create --title x --body-file - <<'EOF'\nRun `orch serve`.\nEOF",
    "gh issue create --title x --body-file - <<'EOF'\nThen `orch addon trust x`, don't forget.\nEOF",
    "gh issue comment 3 -F - <<'EOF'\nrun orch serve\nEOF",
    "cat <<'EOF' | tee notes.md\nrun orch serve\nEOF",
    "mkdir -p docs && cat > docs/n.md <<'EOF'\nrun orch serve\nEOF",
    "cat > a.md <<'A' && cat > b.md <<'B'\norch serve\nA\ngit push origin main\nB",
    "cat > notes.md <<'EOF'\ndon't run orch serve; it's the human's\nEOF",
])
def test_heredoc_prose_allowed(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert d.allow, d.reason


def test_git_commit_message_from_heredoc_allowed(configure):
    ws = configure(git={"agent_may": {"commit": True, "push": False, "open_review": False}})
    assert evaluate(ws, bash("git commit -F - <<'EOF'\nL-0001 Explain why git push is the human's\nEOF")).allow


@pytest.mark.parametrize("payload", ["orch serve", "orch addon trust x", "git push origin main"])
@pytest.mark.parametrize("shape", [
    "bash <<'EOF'\n{p}\nEOF",
    "sh -s <<EOF\n{p}\nEOF",
    "zsh <<'EOF'\n{p}\nEOF",
    "node <<'EOF'\n{p}\nEOF",
    "bash -s -- arg <<'EOF'\n{p}\nEOF",
    "cat <<'EOF' | bash\n{p}\nEOF",
    "cat <<'EOF' | $SH\n{p}\nEOF",
    "cat > s.sh <<'EOF'\n{p}\nEOF\nbash s.sh",
    "tee s.sh <<'EOF'\n{p}\nEOF\nsh s.sh",
    "cat > s.sh <<'EOF'\n{p}\nEOF\nchmod +x s.sh && ./s.sh",
    "eval \"$(cat <<'EOF'\n{p}\nEOF\n)\"",
    "x=$(cat <<'EOF'\n{p}\nEOF\n)",
    "source /dev/stdin <<'EOF'\n{p}\nEOF",
    ". /dev/stdin <<'EOF'\n{p}\nEOF",
    "xargs -I{{}} sh -c {{}} <<'EOF'\n{p}\nEOF",
    "bash <<< '{p}'",
    # an apostrophe in the body must not hide the commands after the terminator (old quoting bypass)
    "cat > n.md <<'EOF'\ndon't\nEOF\n{p}\n# it's",
    "cat > n.md <<EOF\ndon't\nEOF\n{p}\necho it's'",
    # two heredocs on one line, then a real command
    "cat > a.md <<'A'; cat > b.md <<'B'\ndon't\nA\nit's\nB\n{p}",
    # the delimiter quoted inside the body does not end it
    "cat > n.md <<'EOF'\n'EOF'\nEOF\n{p}",
    # unterminated: the rest is code
    "cat > n.md <<'EOF'\n{p}",
])
def test_heredoc_execution_still_denied(ws, shape, payload):
    d = evaluate(ws, bash(shape.format(p=payload)))
    assert not d.allow, shape


def test_no_verify_after_heredoc_denied(configure):
    ws = configure(git={"agent_may": {"commit": True, "push": True, "open_review": True}})
    assert not evaluate(ws, bash("cat > n.md <<'EOF'\ndon't\nEOF\ngit commit --no-verify -m x\n# it's")).allow


@pytest.mark.parametrize("cmd", [
    "echo hi # it's\norch serve\n# that's",
    "echo don\\'t && orch serve && echo it\\'s",
    "env X=\\' orch serve \\'",
])
def test_comments_and_escaped_quotes_do_not_hide_commands(ws, cmd):
    assert not evaluate(ws, bash(cmd)).allow


def test_heredoc_writing_a_ticket_file_still_denied(ws):
    assert not evaluate(ws, bash("cat > orchestrator/tickets/open/L-0001-x.md <<'EOF'\nx\nEOF")).allow


def test_pairing_keys_in_a_heredoc_body_still_denied(ws):
    assert not evaluate(ws, bash("cat > n.md <<'EOF'\ncp ~/.config/orch/remote-humans.json /tmp\nEOF")).allow


# -- fix round 1: a bare `<<EOF` body still runs its substitutions (C1); `--body "$(cat <<'EOF' …)"` is data (I5) --

@pytest.mark.parametrize("inner", [
    "orch serve",
    "orch addon trust x",
    "orch approve L-1 plan",
    "echo x > orchestrator/tickets/open/L-0001-x.md",
    "unset CLAUDECODE",
    "env -i orch list",
])
@pytest.mark.parametrize("form", ["$({c})", "`{c}`"])
def test_substitutions_in_an_unquoted_data_heredoc_are_checked(ws, inner, form):
    cmd = "cat > notes.md <<EOF\nsee " + form.format(c=inner) + "\nEOF"
    assert not evaluate(ws, bash(cmd)).allow, cmd


def test_no_verify_in_an_unquoted_data_heredoc_is_checked(configure):
    ws = configure(git={"agent_may": {"commit": True, "push": True, "open_review": True}})
    assert not evaluate(ws, bash("cat > n.md <<EOF\n$(git push --no-verify)\nEOF")).allow
    assert evaluate(ws, bash("cat > n.md <<'EOF'\n$(git push --no-verify)\nEOF")).allow  # quoted: plain text


@pytest.mark.parametrize("prose", [
    "Run `orch serve` to open it.",
    "The human runs orch approve L-1 plan; don't run it yourself.",
    "Files live in orchestrator/tickets/open and \"quotes\" stay text.",
    "then git push origin main and orch addon trust x",
])
def test_gh_body_from_a_quoted_cat_heredoc_is_data(ws, prose):
    cmd = f"gh issue create --title x --body \"$(cat <<'EOF'\n{prose}\nEOF\n)\""
    d = evaluate(ws, bash(cmd))
    assert d.allow, (cmd, d.reason)


@pytest.mark.parametrize("cmd", [
    "eval \"$(cat <<'EOF'\norch serve\nEOF\n)\"",
    "bash -c \"$(cat <<'EOF'\norch serve\nEOF\n)\"",
    "gh issue create --body \"$(cat <<EOF\n$(orch serve)\nEOF\n)\"",
    "gh issue create --body \"$(cat <<'EOF'\nsay \"hi\nEOF\n)\"; orch serve; echo \"",
    "gh issue create --body \"$(cat <<'EOF' | sh\norch serve\nEOF\n)\"",
])
def test_executed_or_unquoted_cat_substitutions_still_denied(ws, cmd):
    assert not evaluate(ws, bash(cmd)).allow, cmd
