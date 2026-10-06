"""#163: read-only and unrelated commands the guard used to refuse, and the writes it must keep refusing."""
import os

import pytest

from orch.hooks.guard import evaluate


def _bash(ws, cmd, cwd=None):
    return evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd or ws.root)})


# a) reading core.hooksPath

@pytest.mark.parametrize("cmd", [
    "git -C pilatus_hub config --local core.hooksPath",
    "git config core.hooksPath",
    "git config --global core.hooksPath",
    "git config --get core.hooksPath",
    "git config get core.hooksPath",
    "git config --show-origin --get core.hooksPath",
    "git config --file .git/config core.hooksPath",
    "git config --type=path core.hooksPath",
    "git config --local core.hooksPath 2>/dev/null || echo unset",
    "git config core.hooksPath # the shell drops the comment",
    "git config --list --show-origin | grep -i hookspath",
    'H=$(git config core.hooksPath); echo "$H"',
])
def test_reading_hooks_path_is_allowed(ws, cmd):
    d = _bash(ws, cmd)
    assert d.allow, d.reason


@pytest.mark.parametrize("cmd", [
    "git config core.hooksPath get",  # sets the value "get"
    "git config core.hooksPath list",
    "git config core.hooksPath /dev/null",
    "git -C repo config --local core.hooksPath /tmp/none",
    "git config set core.hooksPath x",
    "git config unset core.hooksPath",
    "git config --unset core.hooksPath",
    "git config --unset-all core.hooksPath",
    "git config --add core.hooksPath x",
    "git config --replace-all core.hooksPath x",
    "git config --edit core.hooksPath",
    "git config --type=path core.hooksPath x",
    "git config --file .git/config core.hooksPath x",
    'git config core.hooksPath "$X"',
    "git config core.hooksPath $(echo x)",
    "git config core.hooksPath${IFS}x",
    "git config {core.hooksPath,x}",
    "git config core.hooksPath\\ x",
    "echo x | xargs git config core.hooksPath",
    "H=$(git config core.hooksPath x)",
    "git -c core.hooksPath=/dev/null config core.hooksPath",
])
def test_changing_hooks_path_is_still_denied(ws, cmd):
    d = _bash(ws, cmd)
    assert not d.allow and "hooksPath" in d.reason, cmd


# b) reading a shell startup file

@pytest.mark.parametrize("cmd", [
    'grep -n "ORCH_HOME" ~/.zshrc; echo hi > notes.txt',
    "cat ~/.zshrc ~/.bashrc 2>/dev/null; touch done.txt",
    "head -5 .envrc && cp a b",
    "ls .git/hooks && echo x > out.txt",
    'grep -n "mv" ~/.zshrc',
    "test -f ~/.zprofile || cp a b",
])
def test_reading_a_startup_file_next_to_an_unrelated_write_is_allowed(ws, cmd):
    d = _bash(ws, cmd)
    assert d.allow, d.reason


@pytest.mark.parametrize("cmd", [
    "echo 'export X=1' >> ~/.zshrc",
    "echo x >.zshrc",
    "sed -i s/a/b/ ~/.zshrc",
    "grep x ~/.zshrc > ~/.zshrc",
    "cat ~/.zshrc; cp evil ~/.bashrc",
    "cp ~/.zshrc /tmp/z && sed -i s/a/b/ ~/.zshrc",
    "grep -l x ~/.zshrc | xargs sed -i s/a/b/",
    "ls ~/.zshrc | xargs -I{} cp evil {}",
    'F=~/.zshrc; grep x "$F"; echo y >> "$F"',
    "for f in ~/.zshrc; do echo x >> $f; done",
    'while read f; do echo >> "$f"; done <<< "$(ls ~/.zshrc)"',
    "sh -c 'grep x ~/.zshrc; echo y >> ~/.zshrc'",
    "tee -a ~/.profile <<< x",
    "cat ~/.zshrc; echo x > .git/hooks/pre-commit",
    "eval 'cat ~/.zshrc; echo x >> ~/.zshrc'",
    "ls ~/.zshrc; cp a ~/.zsh''rc",
    "cp a ~/.zsh\\rc",
    "head ~/.zshrc 1>~/.zshrc",
    "cat ~/.zshrc &> ~/.zshrc",
    "cat ~/.zshrc >| ~/.zshrc",
    "./cat ~/.zshrc; echo x > out.txt",
])
def test_writing_a_startup_file_is_still_denied(ws, cmd):
    d = _bash(ws, cmd)
    assert not d.allow and "startup" in d.reason, cmd


# c) prose that mentions orch's config location

PROSE = ("Day-to-day use: the orch plugin reads ORCH_HOME; find the folder at ~ or / and tar it.\n"
         "The config lives in ~/.config/orch/ (see ~/.config/orch/*).")


@pytest.mark.parametrize("cmd", [
    f"cat > notes.md <<'EOF'\n{PROSE}\nEOF",
    f"cat > notes.md <<EOF\n{PROSE}\nEOF",
    f"gh issue create --title x --body-file - <<'EOF'\n{PROSE}\nEOF",
    "gh issue comment 3 --body 'find the folder at ~ or / and tar it'",
    'gh issue create --title "find / and tar ~" --body "see the orch plugin"',
])
def test_prose_that_is_only_data_is_allowed(ws, cmd):
    d = _bash(ws, cmd)
    assert d.allow, d.reason


def test_a_commit_message_that_is_only_data_is_allowed(configure):
    ws = configure(git={"agent_may": {"commit": True, "push": False, "open_review": False}})
    d = _bash(ws, "git commit -m 'L-0001 find the folder at ~ or / and tar it'")
    assert d.allow, d.reason


@pytest.mark.parametrize("cmd", [
    "tar czf /tmp/o.tgz ~/.config/orch",
    f"cat > n.md <<'EOF'\n{PROSE}\nEOF\ntar czf /tmp/o.tgz ~",
    "bash <<'EOF'\ntar czf /tmp/o.tgz ~/.config/orch\nEOF",
    "cat <<'EOF' | sh\ntar czf /tmp/o.tgz ~/.config/orch\nEOF",
    "python3 - <<'EOF'\nimport subprocess\nsubprocess.run('tar czf /tmp/o.tgz ~', shell=True)\nEOF",
    # an interpreter's strings cannot be told from the paths it opens: kept denied
    "python3 - <<'EOF'\nt = s.replace('x', 'find it in ~ or /')\nEOF",
    "cat > n.md <<EOF\n$(tar c ~/.config/orch | base64)\nEOF",
    'gh issue comment 3 --body "$(find ~/.config/orch)"',
    "gh issue comment 3 --body x ~/.config/orch/*",
    "cat > n.md <<'EOF'\nsee remote-humans.json\nEOF",
])
def test_config_dir_access_is_still_denied(ws, cmd):
    assert not _bash(ws, cmd).allow, cmd


# d) ticket-like paths outside the workspace's orchestrator/

@pytest.fixture
def archive(ws):
    a = ws.root / "archiv" / "tickets" / "backlog"
    a.mkdir(parents=True)
    (a / "idea-0001-x.md").write_text("x\n", encoding="utf-8")
    (ws.root / "archiv" / ".state").mkdir()
    t = ws.tickets_dir / "open"
    t.mkdir(parents=True, exist_ok=True)
    (t / "L-0001-x.md").write_text("x\n", encoding="utf-8")
    return ws


@pytest.mark.parametrize("cmd", [
    "cp archiv/tickets/backlog/idea-0001-x.md /tmp/scratch/",
    'cp "archiv/tickets/backlog/idea-0001-x.md" out.md',
    "F=archiv/tickets/backlog/idea-0001-x.md; cp \"$F\" out.md",
    "rm archiv/tickets/backlog/idea-0001-x.md",
    "echo x >> archiv/tickets/backlog/idea-0001-x.md",
    "rm archiv/tickets/backlog/*.md",
    "touch archiv/.state/x",
    "{root}/archiv/tickets/backlog/idea-0001-x.md",
    "cat a > app.state.json",
    "cp a mytickets/open/x.md",
])
def test_a_ticket_like_path_outside_orchestrator_is_allowed(archive, cmd):
    if cmd.startswith("{root}"):
        cmd = 'cp "' + cmd.format(root=archive.root) + '" out.md'  # the root has a space
    d = _bash(archive, cmd)
    assert d.allow, d.reason


@pytest.mark.parametrize("cmd", [
    "rm orchestrator/tickets/open/L-0001-x.md",
    "rm other/orchestrator/tickets/open/L-0001-x.md",
    "rm tickets/open/L-0001-x.md",
    "cd orchestrator && rm tickets/open/L-0001-x.md",
    "rm .state/x",
    "rm archiv2/tickets/open/x.md",  # not there before the command runs
    'W=orchestrator; rm "$W"/tickets/open/L-0001-x.md',
    "rm ${H}tickets/open/L-0001-x.md",
    "rm link/tickets/open/L-0001-x.md",
    "ln -sfn orchestrator archiv && rm archiv/tickets/backlog/idea-0001-x.md",
    "mv archiv old; mv orchestrator archiv; rm archiv/tickets/open/L-0001-x.md",
    "rm -rf archiv; cp -R lnk archiv; rm archiv/tickets/backlog/idea-0001-x.md",
    "cat <(ln -sfn orchestrator archiv); rm archiv/tickets/backlog/idea-0001-x.md",
    "PATH=/tmp/evil:$PATH; cp archiv/tickets/backlog/idea-0001-x.md o",
    "LC_ALL=C cp archiv/tickets/backlog/idea-0001-x.md o",
    "python3 -c 'import os' && rm archiv/tickets/backlog/idea-0001-x.md",
    "printf -v PATH /tmp; cp archiv/tickets/backlog/idea-0001-x.md o",
    "echo x >> archiv/tickets/backlog/hard.md",
    "rm archiv/tickets/backlog/*",
])
def test_the_workspace_ticket_folder_is_still_guarded(archive, cmd):
    os.symlink(archive.home, archive.root / "link")
    os.link(archive.tickets_dir / "open" / "L-0001-x.md", archive.root / "archiv" / "tickets" / "backlog" / "hard.md")
    d = _bash(archive, cmd)
    assert not d.allow, cmd
