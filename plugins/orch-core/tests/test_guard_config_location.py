"""Guard follow-ups from the runner review: a cd whose target names orch's own environment, and the config location
named together with a terminal multiplexer word (interpreter strings included)."""
import pytest


def _bash(ws, cmd):
    from orch.hooks.guard import evaluate
    return evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)})


@pytest.mark.parametrize("cmd", [
    'cd "$(python3 -c \'import os;print(os.environ["ORCH_STATE_DIR"])\')" && tar cf /tmp/o .',
    'cd "$(env | sed -n s/^ORCH_STATE_DIR=//p)" && cp -r . /tmp/x',
    'cd "$(printenv ORCH_STATE_DIR)" && ls -R',
    'cd "$XDG_CONFIG_HOME/orch" && cp -r . /tmp/x',
    'cd "$HOME/.config" && tar cf /tmp/o .',
    'cd "$(printenv XDG_CONFIG_HOME/orch)" && cp -r . /tmp/x',
    'cd "$(python3 -c \'import os;print(os.environ[chr(79)+"RCH_STATE_DIR"])\')" && cp -r . /tmp/x',
    'cd "$(echo $(echo b3JjaA== | base64 -d)/config | tr a b)" && cp -r . /tmp/x',
])
def test_a_cd_that_names_orchs_own_environment_is_a_cd_into_the_config_dir(ws, cmd):
    assert not _bash(ws, cmd).allow


@pytest.mark.parametrize("cmd", [
    'cd "$(brew --prefix)" && ls',
    'cd "$HOME/.config/nvim" && ls',
    'cd "$XDG_CONFIG_HOME/nvim" && ls',
    "cd ~/.config/nvim && ls",
    'cd "$(orch root)" && ls',
    'cd "$(orch --json root)" && tar cf /tmp/o .',
    'cd "$(printenv HOME)" && ls -R | head',
    'cd "$(printenv HOME)/code" && tar cf /tmp/o .',
    'cd "$(env | grep -m1 ^PWD= | cut -d= -f2)" && ls',
])
def test_a_cd_to_an_unrelated_place_is_still_allowed(ws, cmd):
    assert _bash(ws, cmd).allow


@pytest.mark.parametrize("cmd", [
    "perl -e 'my $p=\"{head}\".\"{tail}\"; system(\"tm\".\"ux\", \"-S\", \"$p/f.sock\", \"ls\")'",
    "python3 -c 'import os;os.system(\"TMUX= tm\"+\"ux -S \"+os.environ[\"ORCH_STATE_DIR\"]+\"/s ls\")'",
    "osascript -e 'do shell script \"SCREEN -ls $XDG_CONFIG_HOME/orch\"'",
    "node -e 'require(\"child_process\").exec(\"tmux -S ~/.config/orch/x ls\")'",
    "ruby -e 'system(\"t\\mux\", \"-S\", \"#{ENV[%q(ORCH_STATE_DIR)]}/s\")'",
])
def test_the_config_location_with_a_multiplexer_word_is_denied_inside_interpreter_strings(ws, cmd):
    from orch.core.ledger import base_dir
    s = str(base_dir())
    assert not _bash(ws, cmd.replace("{head}", s[:-4]).replace("{tail}", s[-4:])).allow


@pytest.mark.parametrize("cmd", [
    "cat ~/.config/tmux/tmux.conf",
    "ls ~/.config/screen",
    "echo $XDG_CONFIG_HOME",
    "screenshot --out /tmp/a.png",
])
def test_a_multiplexer_or_a_config_folder_alone_is_not_denied(ws, cmd):
    assert _bash(ws, cmd).allow
