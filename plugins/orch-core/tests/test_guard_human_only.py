"""#19: the guard denies human-only orch commands to agents however they are spelled or wrapped, and denies commands
that strip the harness markers from the environment."""
import pytest

from orch.hooks.guard import evaluate


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


@pytest.mark.parametrize("cmd", [
    "orch approve L-1 requirements",
    "orch approve L-1 plan",
    "orch answer L-1 Q1 yes",
    "orch verdict L-1 done",
    "orch verdict L-1 follow-up -m 'missing tests'",
    "orch request-changes L-1 plan -m smaller",
    "orch move L-1 open",
    "orch move L-1 backlog",
    "orch move L-1 done",
    "orch move L-1 in-progress",
    "orch reopen L-1",
    "orch close L-1",
    "orch checks sign",
    "orch --json checks sign",
    "uv run orch checks sign",
    "orch --json approve L-1 plan",
    '"${CLAUDE_PLUGIN_ROOT}/bin/orch" approve L-1 plan',
    "'/a b/bin/orch' verdict L-1 done",
    "/home/u/.claude/plugins/orch-core/bin/orch approve L-1 plan",
    "uv run orch approve L-1 plan",
    "uv run --project /x/orch-core orch answer L-1 Q1 a",
    "uvx orch approve L-1 plan",
    "python -m orch approve L-1 plan",
    "python3 -m orch.cli verdict L-1 done",
    "o''rch approve L-1 plan",
    "\\orch approve L-1 plan",
    "orch show L-1 && orch approve L-1 plan",
    "sh -c 'orch approve L-1 plan'",
    'bash -lc "orch verdict L-1 done"',
    'eval "orch answer L-1 Q1 a"',
    "echo $(orch approve L-1 plan)",
    "bash <<'EOF'\norch approve L-1 plan\nEOF",
    "cat <<'EOF' | sh\norch verdict L-1 done\nEOF",
    "script -q /dev/null orch approve L-1 plan",
    'script -qc "orch approve L-1 plan" /dev/null',
    "unbuffer orch approve L-1 plan",
    "script -q /dev/null orch list",  # no reason to give orch a pseudo-terminal: denied whatever the subcommand
    "osascript -e 'tell application \"Terminal\" to do script \"orch approve L-1 plan\"'",
    "tmux send-keys -t 0 'orch approve L-1 plan' Enter",
    "expect -c 'spawn orch approve L-1 plan; send L-0001\\r; interact'",
    "python3 -c \"import pty; pty.spawn(['orch','approve','L-1','plan'])\"",
    "python3 -c \"import subprocess; subprocess.run(['orch', 'verdict', 'L-1', 'done'])\"",
    "python3 - <<'EOF'\nimport subprocess\nsubprocess.run([\"orch\", \"approve\", \"L-1\", \"plan\"])\nEOF",
    "uv run python -c \"from orch.core.ops import Ops; from orch.core.events import Actor; "
    "Ops(ws, Actor('human', 'you', 'tty')).approve('L-1', 'plan')\"",
])
def test_human_only_commands_denied(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow, cmd
    assert "human" in d.reason


@pytest.mark.parametrize("cmd", [
    "unset CLAUDECODE",
    "unset CLAUDE_CODE_SESSION_ID CLAUDECODE; orch list",
    "env -u CLAUDECODE orch list",
    "env --unset=CLAUDE_CODE_ENTRYPOINT ls",
    "env -i PATH=$PATH HOME=$HOME bash",
    "env - orch list",
    "env --ignore-environment ls",
    "CLAUDECODE= orch list",
    "CLAUDECODE='' CLAUDE_CODE_SESSION_ID= orch list",
    "export CLAUDECODE=",
    "export ORCH_HARNESS=''",
    "unset AI_AGENT",
])
def test_stripping_the_harness_environment_denied(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow, cmd
    assert "environment" in d.reason


@pytest.mark.parametrize("cmd", [
    "orch show L-1",
    "orch move L-1 testing",
    "orch list --json",
    "orch wait L-1 --json",
    "orch task start L-1 T1",
    "orch ask L-1 --file q.yaml",
    "orch claim L-1",
    "ORCH_HARNESS=copilot orch claim L-1",
    "env FOO=1 orch list",
    "echo 'ask the human to run orch approve L-1 plan'",
    "echo \"then the human runs: orch verdict L-1 done\"",
    "uv run pytest tests/test_approve_hash.py",
    "cd ~/src/orch-core && uv run python -m pytest tests/test_approve_hash.py -q",
    "grep -rn 'def approve' src/orch",
    "printenv CLAUDECODE",
    "docker run --env FOO=1 -it ubuntu bash",
    "env FOO=1 python -i script.py",
    "kubectl exec -it pod -- env",
    "orch artifact add L-1 script.sh",
    "orch log L-1 -m 'the login screen flickers'",
    "uv run python -m pytest -k approve && orch show L-1",
    "cat > notes.md <<'EOF'\nThe human runs orch approve L-1 plan.\nEOF",
])
def test_ordinary_agent_commands_allowed(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert d.allow, (cmd, d.reason)


# -- fix round 1 (I2): cheap rules for common indirections; the guard stays best effort --

@pytest.mark.parametrize("cmd", [
    "V=approve; orch $V L-1 plan",
    "orch \"$(echo approve)\" L-1 plan",
    "orch `printf approve` L-1 plan",
    "echo approve L-1 plan | xargs orch",
    "printf 'L-1\\n' | xargs -I{} orch verdict {} done",
    "perl -e 'system(\"orch\", \"approve\", \"L-1\", \"plan\")'",
    "ruby -e 'system(\"orch verdict L-1 done\")'",
    "node -e \"require('child_process').execSync('orch approve L-1 plan')\"",
])
def test_indirect_human_only_commands_denied(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "human" in d.reason, cmd


@pytest.mark.parametrize("cmd", [
    "export -n CLAUDECODE",
    "export -n CLAUDE_CODE_SESSION_ID; orch list",
    "exec -c bash",
])
def test_more_env_stripping_denied(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "environment" in d.reason, cmd


@pytest.mark.parametrize("cmd", [
    "echo b3JjaCBzZXJ2ZQ== | base64 -d | sh",
    "eval \"$(echo b3JjaA== | base64 --decode)\"",
    "base64 -D <<< b3JjaA== | bash",
    "xxd -r -p x.hex | bash",
])
def test_decoded_commands_denied(ws, cmd):
    assert not evaluate(ws, bash(cmd)).allow, cmd


@pytest.mark.parametrize("cmd", [
    "echo aGk= | base64 -d",
    "base64 -d token.b64 > token.bin",
    "export -p",
    "ls | xargs grep orch",
    "orch list | xargs -n1 echo",
])
def test_indirection_rules_leave_ordinary_commands_alone(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert d.allow, (cmd, d.reason)


# -- fix round 2 (M-c): decoders only when they feed a runner; xargs with a literal non-human subcommand --

@pytest.mark.parametrize("cmd", [
    "base64 -d payload.b64 > run.sh",
    "base64 -d payload.b64 | tee out.sh",
    "echo aGk= | base64 --decode > notes.txt && bash build.sh",
    "xxd -r -p x.hex > x.bin; sh ./configure.sh",
    "base64 -d p.b64 > p.bin && bash ./build.sh",
    "base64 -d p.b64 | tee p.bin | wc -c",
    "base64 -d p.b64 | env > /dev/null",
    "base64 -d p.b64 | sudo tee /etc/x.conf",
    "base64 -d p.b64 | awk '{print $1}'",
    "base64 -d p.b64 | timeout 5 wc -c",
    "base64 -d p.b64 | ( cd /tmp && cat > out.bin )",
    "ls | xargs -n1 orch show",
    "orch list --json | xargs -I{} orch show {}",
])
def test_decoder_and_xargs_false_positives_allowed(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert d.allow, (cmd, d.reason)


@pytest.mark.parametrize("cmd", [
    "echo x | base64 -d | sh",
    "base64 -d p.b64 | /bin/bash",
    "echo x | base64 -d | sudo bash",
    "eval \"$(echo x | base64 -d)\"",
    "source <(base64 -d p.b64) || bash -c \"$(base64 -d p.b64)\"",
    "printf 'L-1' | xargs orch approve",
    "echo show | xargs orch",
    "ls | xargs -n1 orch move",
    # fix round 3: process substitution, backticks and wrappers
    "sh <(base64 -d p.b64)",
    "bash <(echo eA== | base64 --decode)",
    "source <(base64 -d p.b64)",
    ". <(base64 -d p.b64)",
    "eval \"`echo eA== | base64 -d`\"",
    "sh -c \"`base64 -d p.b64`\"",
    "base64 -d p.b64 | env sh",
    "base64 -d p.b64 | command sh",
    "base64 -d p.b64 | exec bash",
    "base64 -d p.b64 | nice -n 5 sh",
    "base64 -d p.b64 | env -i PATH=/bin sh",
    "base64 -d p.b64 | xargs -0 sh -c",
    "base64 -d p.b64 | xargs -I{} bash -c {}",
    # fix round 4: wrapper options with values, grouping, more runners
    "base64 -d p.b64 | sudo -u root sh",
    "base64 -d p.b64 | exec -a x sh",
    "base64 -d p.b64 | timeout 5 bash",
    "base64 -d p.b64 | ionice -c 3 sh",
    "base64 -d p.b64 | chrt -f 10 sh",
    "base64 -d p.b64 | (sh)",
    "base64 -d p.b64 | { sh; }",
    "base64 -d p.b64 | busybox sh",
    "base64 -d p.b64 | php",
    "base64 -d p.b64 | pwsh",
    "base64 -d p.b64 | osascript",
    "base64 -d p.b64 | awk '{system($0)}'",
    "bash < <(base64 -d p.b64)",
    # fix round 5: compound groups after the pipe
    "base64 -d p.b64 | ( cd /tmp && sh )",
    "base64 -d p.b64 | { cd x; sh; }",
    "base64 -d p.b64 | (cd /tmp; exec bash)",
])
def test_decoder_runs_and_dynamic_xargs_denied(ws, cmd):
    assert not evaluate(ws, bash(cmd)).allow, cmd


def test_reading_the_signed_state_of_checks_stays_open_to_agents(ws):
    assert evaluate(ws, bash("orch checks")).allow and evaluate(ws, bash("orch checks status --json")).allow
