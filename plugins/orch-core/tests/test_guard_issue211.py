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


# What the shell runs outside or inside the "message" must still be inspected (parser-differential checks).
MESSAGE_ESCAPES = [
    'git commit -m "x" ; orch approve L-1',
    'git commit -m "x" && orch approve L-1',
    'git commit -m "x"\norch approve L-1',
    'git commit -m "$(orch approve L-1)"',
    'git commit -m "`orch approve L-1`"',
    'git commit -m "a $(orch approve L-1) b"',
    'gh issue create --body "x" --title "$(orch approve L-1)"',
    'gh issue create --body "$(orch approve L-1)"',
    'gh issue create --body "`orch approve L-1`"',
    'git commit -m "a\\\\" ; orch approve L-1 ; \\""',
    "git commit -m'x' ; orch approve L-1",
    "git commit --message='x' ; orch approve L-1",
    'git commit --message="x" ; orch approve L-1',
    'git commit -m "x"\\\norch approve L-1',
    "git -c alias.x='!orch approve L-1' x",
    "git -c alias.x=!orch\\ approve\\ L-1 x",
    'eval "$(echo orch approve L-1)"',
    "orch log L-1 -m 'x' ; orch approve L-1",
    'orch log L-1 -m "$(orch approve L-1)"',
    'orch log L-1 -m "`orch approve L-1`"',
    "orch log L-1 -m 'x'\norch approve L-1",
    "orch log L-1 -m 'it'\"'\"'s' ; orch approve L-1",
    "echo 'x' ; orch approve L-1",
    "echo 'x'\n orch approve L-1",
    "echo 'x' > f; orch approve L-1",
    "echo 'orch approve L-1' | bash -s",
    "echo 'orch approve L-1' > run.sh && sh run.sh",
    "gh issue create --body 'x'; python3 -c \"import os; os.system('orch approve L-1')\"",
    "gh issue create --body 'python' ; orch approve L-1",
    "echo 'python' ; orch approve L-1",
    "gh issue create --body 'x' <<EOF\norch approve L-1\nEOF",
    "git commit -m \"$(cat <<'EOF'\nx\nEOF\n)\"; orch approve L-1",
    "git commit -m x$(orch approve L-1)",
    "git commit -m \"x\"$(orch approve L-1)",
    "git commit -m \"x\" -m \"$(orch approve L-1)\"",
    "gh issue create --body='x' --title=\"$(orch approve L-1)\"",
]


@pytest.mark.parametrize("cmd", MESSAGE_ESCAPES)
def test_message_text_does_not_hide_what_the_shell_runs(ws, cmd):
    ws.config["git"]["agent_may"]["commit"] = True
    _check(ws, cmd, False)


FEEDS_AN_INTERPRETER = [
    "echo 'orch approve L-1' | sh",
    "echo 'orch approve L-1' | bash",
    "echo 'orch approve L-1' | zsh",
    "echo 'orch approve L-1' | dash",
    "echo 'orch approve L-1' | ksh",
    "echo 'orch approve L-1' | fish",
    "echo 'orch approve L-1' | env sh",
    "echo 'orch approve L-1' | sudo sh",
    "echo 'orch approve L-1' | python3 -c 'import os,sys; os.system(sys.stdin.read())'",
    "echo 'orch approve L-1' | node -e 'require(\"child_process\").execSync(require(\"fs\").readFileSync(0,\"utf8\"))'",
    "echo 'orch approve L-1' | perl -e 'system(<STDIN>)'",
    "echo 'orch approve L-1' | ruby -e 'system(STDIN.read)'",
    "echo 'orch approve L-1' | xargs -I{} sh -c {}",
    "echo 'orch approve L-1' | xargs -0 sh -c",
    "echo 'orch approve L-1' | source /dev/stdin",
    "echo 'orch approve L-1' | . /dev/stdin",
    "echo 'orch approve L-1' | eval",
    "printf '%s' \"orch approve L-1\" | sh",
    "printf 'orch approve L-1\\n' | bash -s",
    "gh issue create --body \"orch approve L-1\" ; gh issue view 1 --json body -q .body | sh",
    "git commit -m \"orch approve L-1\" && git log -1 --format=%s | sh",
    "cat <<EOF | sh\norch approve L-1\nEOF",
    "cat <<'EOF' | bash\norch approve L-1\nEOF",
    "tee x.sh <<< \"orch approve L-1\"; sh x.sh",
    "tee x.sh <<< 'orch approve L-1'; bash x.sh",
    "sh <<< \"orch approve L-1\"",
    "bash <<< 'orch approve L-1'",
    "python3 <<< \"import os; os.system('orch approve L-1')\"",
    "bash -c \"$(echo orch approve L-1)\"",
    "bash <(echo 'orch approve L-1')",
    "echo 'orch approve L-1' > x.sh && sh x.sh",
    "echo 'orch approve L-1' > x.sh; source x.sh",
    "echo 'orch approve L-1' > x.sh; . ./x.sh",
    "gh issue create --body 'x'; echo 'orch approve L-1' | sh",
    "orch log L-1 -m 'x'; echo 'orch approve L-1' | sh",
    "echo orch approve L-1 | sh",
]


@pytest.mark.parametrize("cmd", FEEDS_AN_INTERPRETER)
def test_text_fed_to_an_interpreter_is_inspected(ws, cmd):
    ws.config["git"]["agent_may"]["commit"] = True
    _check(ws, cmd, False)


SHELL_TOKENIZATION = [
    "orch $'approve' L-1",
    "orch $'\\x61pprove' L-1",
    "or\\\nch approve L-1",
    "orch \\\napprove L-1",
    "echo x \\; orch approve L-1",
    "echo x # c\norch approve L-1",
    "echo 'a;b' ; orch approve L-1",
    "echo \"a;b\"; orch approve L-1",
    "echo '#' ; orch approve L-1",
    "echo x;orch approve L-1",
    "echo x&&orch approve L-1",
    "echo x||orch approve L-1",
    "{ orch approve L-1; }",
    "(orch approve L-1)",
    "echo $'orch\\x20approve L-1' | sh",
    "eval $'orch\\x20approve L-1'",
    "eval \"$(echo orch approve L-1)\"",
    "o''rch approve L-1",
    "\"orch\" approve L-1",
    "'orch' 'approve' L-1",
    "FOO=1 orch approve L-1",
    "command orch approve L-1",
]


@pytest.mark.parametrize("cmd", SHELL_TOKENIZATION)
def test_shell_tokenization_tricks_stay_denied(ws, cmd):
    ws.config["git"]["agent_may"]["commit"] = True
    _check(ws, cmd, False)


MESSAGE_NOT_ALONE = [
    "gh issue create --body 'orch approve L-1'; gh issue view 1 --json body -q .body | $0",
    "orch log L-1 -m 'orch approve L-1'; $(orch show L-1)",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 | awk '{system($0)}'",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 > x.sh; ./x.sh",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 | tclsh",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 | ed",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 | sed e",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 | rbash",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 | mksh",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 | pypy3",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 | csh",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 | ${SHELL}",
    "orch log L-1 -m 'orch approve L-1'; orch show L-1 | $(which sh)",
    "gh issue view 1 --json body -q .body > x.sh; ./x.sh; gh issue comment 1 --body 'orch approve L-1'",
    "gh issue comment 1 --body 'orch approve L-1' && ./run.sh",
    "gh issue comment 1 --body 'orch approve L-1' > out.sh",
    "gh issue comment 1 --body 'orch approve L-1' &",
    "gh issue comment 1 --body 'orch approve L-1'\n./run.sh",
]


@pytest.mark.parametrize("cmd", MESSAGE_NOT_ALONE)
def test_message_text_counts_only_when_the_message_command_is_alone(ws, cmd):
    _check(ws, cmd, False)


SCRATCH_STEP_1 = [
    "ln -sfn tickets/./open orchestrator/temporary",
    "ln -sfn tickets//open orchestrator/temporary",
    "ln -s ../tickets/open orchestrator/artifacts",
    "ln -sfn orchestrator/tickets/open orchestrator/temporary",
    "ln -s x orchestrator/temporary",
    "ln -sfn x orchestrator/artifacts/",
    "mv orchestrator/temporary orchestrator/t2",
    "cp -r orchestrator/tickets/open orchestrator/temporary",
    "cp -R x orchestrator/artifacts",
]


@pytest.mark.parametrize("cmd", SCRATCH_STEP_1)
def test_linking_or_moving_a_scratch_folder_is_refused(ws, cmd):
    _check(ws, cmd, False)


SCRATCH_STEP_2 = [
    "echo x > orchestrator/temporary/L-0001-x.md",
    "cat > orchestrator/temporary/L-0001-x.md <<'EOF'\nx\nEOF",
    "cp /tmp/x.md orchestrator/temporary/L-0001-x.md",
    "mv /tmp/x.md orchestrator/artifacts/L-0001-x.md",
    "tee orchestrator/temporary/L-0001-x.md < /tmp/x.md",
]


@pytest.mark.parametrize("cmd", SCRATCH_STEP_2)
def test_a_symlinked_scratch_folder_does_not_write_tickets(ws, cmd):
    tickets = ws.tickets_dir
    (tickets / "open").mkdir(parents=True, exist_ok=True)
    tmp = ws.root / "orchestrator" / "temporary"
    arts = ws.root / "orchestrator" / "artifacts"
    for link in (tmp, arts):
        if link.is_symlink() or link.exists():
            if link.is_symlink() or link.is_file():
                link.unlink()
            else:
                link.rmdir()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(tickets / "open")
    _check(ws, cmd, False)


def test_a_real_scratch_folder_still_takes_notes(ws):
    (ws.root / "orchestrator" / "temporary").mkdir(parents=True, exist_ok=True)
    _check(ws, "echo x > orchestrator/temporary/L-0001-x.md", True)


ANSI_C_DENIED = [
    "eval $'\\x6frch \\x61pprove L-1 # \\xZ'",
    "eval $'\\x6frch approve L-1 \\xZ'",
    "eval $'orch approve L-1\\q'",
    "eval $'\\157rch \\141pprove L-1'",
    "eval $'\\u006frch approve L-1'",
    "eval $'\\U0000006frch approve L-1'",
    "eval $'o\\x72ch approve L-1'",
    "eval $'orch\\x20approve\\x20L-1'",
    "eval $'orch approve L-1\\cA'",
    "eval $'orch approve L-1\\e'",
    "eval $'orch approve L-1' # $'",
    "eval $'orch approve L-1",
    "eval $'orch\\nl approve",
    "sh -c $'\\x6frch approve L-1'",
    "echo $'\\x6frch approve L-1' | sh",
]


@pytest.mark.parametrize("cmd", ANSI_C_DENIED)
def test_ansi_c_strings_are_decoded_like_bash_or_refused(ws, cmd):
    _check(ws, cmd, False)


def test_a_dollar_and_quote_in_plain_text_is_not_ansi_c(ws):
    _check(ws, "grep 'foo$' notes.txt", True)
    _check(ws, "grep -n 'a$' x.txt | wc -l", True)
    _check(ws, "echo $'plain text'", True)


QUOTED_PROGRAM_DENIED = [
    'x=approve; "orch" $x L-0001',
    'o"rch" $x L-1',
    'command "orch" $x L-1',
    'orch() { command "orch" ${x} L-0001; }; x=approve; orch approve --help',
    "x=approve; 'orch' ${x} L-1",
    "x=approve; env orch $x L-1",
    "x=approve; orch `echo $x` L-1",
    "orch app* L-1",
    "orch appro?e L-1",
    "orch -C x $v L-1",
    "orch --json $x L-1",
    "uv run orch $x L-1",
    "python3 -m orch.cli $x L-1",
    "orch() { :; }; orch approve --help",
    "function orch { :; }; orch approve --help",
    "alias orch='orch --json'; orch approve --help",
    "alias orch=echo",
    "approve() { :; }",
    "f() { :; }; orch approve --help",
    "alias a=b; orch approve --help",
]


@pytest.mark.parametrize("cmd", QUOTED_PROGRAM_DENIED)
def test_a_quoted_or_built_program_or_subcommand_is_refused(ws, cmd):
    _check(ws, cmd, False)


def test_plain_words_after_orch_stay_allowed(ws):
    _check(ws, "orch show $id", True)
    _check(ws, 'orch -C "$d" show L-0001', True)


CONFIG_INDIRECT_DENIED = [
    "cd ~/.config/orch && cat $(ls)",
    "cd ~/.config/orch && for f in $(ls); do cat $f; done",
    "cd ~/.config/orch && for f in *; do cat $f; done",
    "cd ~/.config/orch && cat <(ls)",
    "cd ~/.config/orch && cat `ls`",
    "cd ~/.config/orch && ls | xargs cat",
    "cd ~/.config/orch && git add -A && git diff --cached",
    "git -C ~/.config/orch add -A",
    "git -C ~/.config/orch diff",
    "diff -ruN /tmp/e ~/.config/orch",
    "diff -r ~/.config/orch /tmp/e",
    "diff --recursive ~/.config/orch /tmp/e",
    "bsdtar cf - ~/.config/orch",
    "tar cf - ~/.config/orch",
    "zip -r /tmp/x.zip ~/.config/orch",
    "rsync -a ~/.config/orch/ /tmp/e/",
    "cp -r ~/.config/orch /tmp/e",
    "rclone copy ~/.config/orch /tmp/e",
]


@pytest.mark.parametrize("cmd", CONFIG_INDIRECT_DENIED)
def test_the_config_dir_is_not_readable_by_indirection(ws, cmd):
    _check(ws, cmd, False)


def test_a_cwd_in_the_config_dir_refuses_indirection(ws):
    from orch.dashboard.launch import config_dir
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    for cmd in ("cat $(ls)", "for f in $(ls); do cat $f; done", "cat `ls`", "ls | xargs cat", "git add -A"):
        assert not _bash(ws, cmd, cwd=d).allow, cmd


def test_plain_commands_in_other_folders_stay_allowed(ws):
    _check(ws, "cat $(ls)", True)
    _check(ws, "diff -ru /tmp/a /tmp/b", True)
    _check(ws, "git diff --cached", True)
    _check(ws, "tar cf - /tmp/x", True)


FD_REDIRECTS_DENIED = [
    "cat nonexistent 2>orchestrator/tickets/open/L-0001-x.md",
    "cat x 1>orchestrator/tickets/open/L-0001-x.md",
    "printf x 3>orchestrator/tickets/open/L-0001-x.md",
    "cat x 2>>orchestrator/tickets/open/L-0001-x.md",
    "cat x &>orchestrator/tickets/open/L-0001-x.md",
    "cat x &>>orchestrator/tickets/open/L-0001-x.md",
    "cat x >|orchestrator/tickets/open/L-0001-x.md",
    "cat x >& orchestrator/tickets/open/L-0001-x.md",
    "cat x {fd}>orchestrator/tickets/open/L-0001-x.md",
    "exec {fd}>orchestrator/tickets/open/L-0001-x.md",
    "cat x 3<>orchestrator/tickets/open/L-0001-x.md",
    "cat x 2> orchestrator/.state/err",
    "cat orchestrator/.state/x 2>orchestrator/.state/y",
    "cat orchestrator/.state/x 2>$E",
    "cat x 1>orchestrator/tickets/open/L-0001-x.md 2>/dev/null",
    "cat x >> /dev/null >> orchestrator/tickets/open/L-0001-x.md",
]


@pytest.mark.parametrize("cmd", FD_REDIRECTS_DENIED)
def test_fd_redirects_into_tickets_and_state_are_writes(ws, cmd):
    _check(ws, cmd, False)


def test_harmless_redirects_stay_allowed(ws):
    _check(ws, "cat orchestrator/.state/x 2>/dev/null", True)
    _check(ws, "cat orchestrator/.state/x 2>&1", True)
    _check(ws, "cat orchestrator/.state/x &>/dev/null", True)
    _check(ws, "cat orchestrator/.state/x 2>/tmp/err.txt", True)
    _check(ws, "cat orchestrator/.state/x > /dev/null 2>&1", True)


T = "orchestrator/tickets/open/L-0001-x.md"
WRITE_CAPABLE_READERS_DENIED = [
    f"sort -o {T} {T}",
    f"sort -o{T} {T}",
    f"sort --output={T} {T}",
    f"sort {T} -o {T}",
    f"uniq {T} {T}",
    f"uniq in.txt {T}",
    f"xxd -r in.hex {T}",
    f"xxd in.bin {T}",
    f"sed -n 'w {T}' {T}",
    f"sed 'w {T}' in.txt",
    f"sed -n '1,3w {T}' in.txt",
    f"sed -n '/x/w {T}' in.txt",
    f"sed -n 'W {T}' in.txt",
    f"sed 's/a/b/w {T}' in.txt",
    "sed 's/a/b/e' orchestrator/.state/x",
    "sed e orchestrator/.state/x",
    "sed -n '1e id' orchestrator/.state/x",
    f"rg --pre ./x.sh pattern {T}",
    f"rg --pre=./x.sh pattern {T}",
    "less -o log.txt orchestrator/.state/x",
    "less --log-file=log.txt orchestrator/.state/x",
    "more -o log orchestrator/.state/x",
    "file -C orchestrator/.state/x",
    f"awk '{{print > \"{T}\"}}' in.txt",
    f"awk '{{system(\"rm {T}\")}}' in.txt",
    "gawk 'BEGIN{while ((getline l < \"x\") > 0) print l}' orchestrator/.state/x",
]


@pytest.mark.parametrize("cmd", WRITE_CAPABLE_READERS_DENIED)
def test_commands_that_can_write_are_not_readers(ws, cmd):
    _check(ws, cmd, False)


def test_plain_reads_with_those_tools_stay_allowed(ws):
    _check(ws, f"sort {T} | head -3", True)
    _check(ws, f"sed -n '1,5p' {T}", True)
    _check(ws, f"sed 's/a/b/g' {T} | wc -l", True)
    _check(ws, f"uniq -c {T}", True)
    _check(ws, f"rg pattern {T}", True)


MUX_DISGUISED_DENIED = [
    "scr''een -S x -X stuff y",
    'scr""een -S x -X stuff y',
    "scr\\een -S x -X stuff y",
    "t''mux -L orch send-keys x",
    "/usr/bin/tm?x -L orch send-keys x",
    "/usr/bin/tm*x -L orch ls",
    "/usr/bin/scr?en -S x -X stuff y",
    "/usr/bin/scr*n -ls",
    "/usr/bin/t[m]ux ls",
    "T=tm; ${T}ux -L orch ls",
    "T=tmux; $T -L orch send-keys x",
    "T=scr; ${T}een -S x -X stuff y",
    "$(echo tmux) -L orch ls",
    "`echo screen` -S x -X stuff y",
    "T=tmux; $T send-keys -t x 'orch approve L-1' Enter",
    "T=tmux; $T attach",
]


@pytest.mark.parametrize("cmd", MUX_DISGUISED_DENIED)
def test_a_disguised_tmux_or_screen_is_refused(ws, cmd):
    _check(ws, cmd, False)


def test_ordinary_dollar_commands_stay_allowed(ws):
    _check(ws, "$HOME/bin/tool --flag", True)
    _check(ws, "ls $D", True)
    _check(ws, "echo screen", True)


WRITE_NOW_RUN_LATER_DENIED = [
    "cat > x.sh <<'EOF'\norch approve L-1\nEOF",
    "cat > notes.md <<'EOF'\n#!/bin/sh\norch approve L-1\nEOF",
    "cat >> run.txt <<EOF\norch verdict L-1 pass\nEOF",
    "tee x.sh <<'EOF'\norch approve L-1\nEOF",
    "cat <<'EOF' | tee x.sh\norch approve L-1\nEOF",
    "cat <<'EOF' > /tmp/anything\nuv run orch answer L-1 q1 yes\nEOF",
    "echo 'orch approve L-1' > x.sh",
    "echo 'orch approve L-1' >> x.sh",
    "echo orch approve L-1 > x",
    "printf 'orch approve L-1\\n' > x.sh",
    "printf '%s\\n' 'orch close L-1' > /tmp/x",
    "echo 'orch approve L-1' | tee x.sh",
    "echo 'orch approve L-1' 1> x.sh",
    "echo 'orch approve L-1' &> x.sh",
    "tee x.sh <<< 'orch approve L-1'",
    "cat > x.sh <<< 'orch approve L-1'",
    "echo '#!/bin/sh' > x.sh; echo 'orch approve L-1' >> x.sh",
    "echo 'orch move L-1 done' > x.sh",
    "echo 'orch epic pause E-1' > x.sh",
    "echo 'orch permit grant a b' > x.sh",
]


@pytest.mark.parametrize("cmd", WRITE_NOW_RUN_LATER_DENIED)
def test_a_file_holding_a_human_command_is_not_written(ws, cmd):
    _check(ws, cmd, False)


def test_writing_other_text_and_printing_a_mention_stay_allowed(ws):
    _check(ws, "cat > x.sh <<'EOF'\norch show L-1\nEOF", True)
    _check(ws, "echo 'orch show L-1' > x.sh", True)
    _check(ws, "echo 'orch approve L-1'", True)
    _check(ws, "cat > notes.md <<'EOF'\nplain notes\nEOF", True)


QT = "orchestrator/tickets/open/L-0001-x.md"
QS = "orchestrator/.state/ledger.json"
QUOTED_TARGETS_DENIED = [
    f"echo x > '{QT}'",
    f'echo x > "{QS}"',
    f"cat /etc/hosts > '{{ws}}/{QS}'",
    f"cat > '{QT}' <<'EOF'\nx\nEOF",
    f"printf x > '{QT}'",
    f"head -1 /etc/hosts > '{QT}'",
    f"grep a /etc/hosts > '{QS}'",
    f"jq . /tmp/x.json > '{QS}'",
    f"ls > '{QT}'",
    f"wc -l /etc/hosts > '{QT}'",
    f"diff a b > '{QT}'",
    f"stat /etc/hosts > '{QS}'",
    f"echo x >> '{QT}'",
    f'echo x > "$PWD/{QS}"',
    f"echo x 2> '{QS}'",
    f"echo x 1> \"{QT}\"",
    f"echo x &> '{QT}'",
    f"echo x >| '{QT}'",
    f"echo x > o'rchestrator'/tickets/open/L-0001-x.md",
    "cat orchestrator/.state/x > ''",
]


@pytest.mark.parametrize("cmd", QUOTED_TARGETS_DENIED)
def test_a_quoted_redirect_target_is_still_a_target(ws, cmd):
    _check(ws, cmd, False)


def test_a_quoted_target_through_a_symlinked_alias_is_denied(ws, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(ws.root)
    d = _bash(ws, f"echo x > '{alias}/orchestrator/tickets/open/L-0001-a.md'")
    assert not d.allow, d.reason


def test_a_quoted_target_outside_state_stays_allowed(ws):
    _check(ws, "cat orchestrator/.state/x > '/tmp/out.txt'", True)
    _check(ws, 'echo x > "/tmp/out.txt"', True)


CONTINUATION_AND_COMMENTS = [
    "echo a # c \\\nor\\\nch approve L-1",
    "echo a # c \\\nor\\\nch approve L-1 # d",
    "echo a # c\nor\\\nch approve L-1",
    "echo a # c \\\ntm\\\nux -L orch ls",
    "echo a # c \\\nbash -c 'or\\\nch approve'",
    "bash -c 'or\\\nch approve L-1'",
    "echo x > orchestrator/tick\\\nets/open/L-0001-x.md",
    "echo x > orchestrator/.sta\\\nte/y",
    "true # \\\n o\\\nrch approve L-1",
    'true # \\\n orch approve L-1',
    "echo hi # c \\\no''rch approve L-1",
    'true # \\\n or\\ch approve L-1',
    'true # \\\norch approve L-1',
    'true # x \\\n\norch approve L-1',
    "echo 'a \\\n' ; orch approve L-1",
]


@pytest.mark.parametrize("cmd", CONTINUATION_AND_COMMENTS)
def test_a_backslash_does_not_continue_a_comment(ws, cmd):
    _check(ws, cmd, False)


def test_escaped_quotes_inside_one_message_are_still_one_message(ws):
    ws.config["git"]["agent_may"]["commit"] = True
    _check(ws, 'git commit -m "a\\" ; orch approve L-1 ; \\""', True)


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


# --- 4. read-only access to .state ----------------------------------------------------------------------------------
STATE_ALLOWED = [
    "orch show L-0001 --json | jq '.state' > /tmp/out.json",
    "orch show L-0001 --json | jq -r '.state' | python3 -c 'import sys; print(sys.stdin.read())'",
    "orch list --json | jq -r '.[] | select(.state == \"open\") | .id' > /tmp/ids",
    "jq --arg s open '.state == $s' /tmp/x.json > /tmp/y.json",
    "cat orchestrator/.state/x.json | python3 -c 'import json,sys; print(json.load(sys.stdin))'",
    "cat orchestrator/.state/x.json > /tmp/copy.json",
    "grep foo orchestrator/.state/x.json 2>/dev/null > /tmp/o",
    "python3 -c 'print(1)'; cat orchestrator/.state/x.json",
    "ls orchestrator/.state > /tmp/listing; python3 -c 'print(2)'",
    "jq '.a' orchestrator/.state/x.json | python3 -c 'import sys; sys.stdin.read()'",
]


@pytest.mark.parametrize("cmd", STATE_ALLOWED)
def test_reading_state_is_allowed(ws, cmd):
    _check(ws, cmd, True)


STATE_DENIED = [
    "echo x > orchestrator/.state/x",
    "cat a > orchestrator/.state/x",
    "cat orchestrator/.state/x > orchestrator/.state/y",
    "cat orchestrator/.state/x >> orchestrator/.state/y",
    "cat orchestrator/.state/x > $OUT",
    "cat orchestrator/.state/x > ~/y",
    "F=orchestrator/.state/x; cat a >> $F",
    "F=orchestrator/.state/x\ncat orchestrator/.state/y; echo x >> $F",
    "export F=orchestrator/.state/x; echo x >> $F",
    "cd orchestrator/.state && echo x > y",
    "cd orchestrator/.state; cat y > /tmp/z",
    "cd orchestrator && cd .state && rm x",
    "pushd orchestrator/.state; touch y",
    "rm orchestrator/.state/x",
    "mv orchestrator/.state/x /tmp/x",
    "cp /tmp/x orchestrator/.state/x",
    "cat orchestrator/.state/x; rm -rf orchestrator/.stat*",
    "cat orchestrator/.state/x; rm -rf $D",
    "ls orchestrator/.state | xargs rm",
    "find orchestrator/.state -name x | xargs rm",
    "find orchestrator/.state -delete",
    "for f in orchestrator/.state/*; do : > $f; done",
    "python3 -c \"open('orchestrator/.state/x','w').write('1')\"",
    "jq . x.json > orchestrator/.state/y.json",
    "jq '.a' x.json | tee orchestrator/.state/y",
    "jq '.state' x.json | sed -i s/a/b/ orchestrator/.state/z",
    "sed -i s/a/b/ orchestrator/.state/z",
    "echo '{}' | tee orchestrator/.state/x.json",
    "ln -sfn orchestrator archiv && python3 -c 'print(1)' && rm archiv/.state/x",
    "orch show L-1 --json | jq '.state' | tee /tmp/ok; ln -sfn orchestrator a; rm a/.state/x",
]


@pytest.mark.parametrize("cmd", STATE_DENIED)
def test_writing_state_stays_denied(ws, cmd):
    _check(ws, cmd, False)


# --- 5. `orch -C "$d" ...` and 6. options that take a value ---------------------------------------------------------
OPT_ALLOWED = [
    'orch -C "$d" artifact add L-0001 f.md',
    "orch -C $d artifact add L-0001 f.md",
    'orch -C "$d" show L-0001',
    "orch -C /tmp/x show L-0001 --json",
    'orch --json -C "$PWD" list',
    "orch log L-0001 -m approve",
    "orch show L-0001 --json",
]


@pytest.mark.parametrize("cmd", OPT_ALLOWED)
def test_known_option_values_are_not_the_subcommand(ws, cmd):
    _check(ws, cmd, True)


OPT_DENIED = [
    "orch -C x approve L-1",
    'orch -C "$d" approve L-1',
    "orch -C x -C y verdict L-1 pass",
    "orch --foo bar approve L-1",
    "orch --foo bar --baz qux approve L-1",
    "orch --foo bar answer L-1",
    "orch --foo bar epic pause E-1",
    "orch --foo bar permit grant x",
    "orch --foo bar move L-1 done",
    "orch -C x serve",
    "orch --foo bar serve",
    "orch --foo bar addon trust x",
    "orch -C x addon install y",
    "uv run orch --foo bar approve L-1",
    "/usr/bin/orch --foo bar approve L-1",
    "orch -C x $SUB approve",
    'orch -C x "$SUB" L-1',
    "orch $SUB L-1",
    "orch -C $d $SUB",
    "orch --json $(echo approve) L-1",
    "orch -- approve L-1",
    "orch --foo=bar approve L-1",
]


@pytest.mark.parametrize("cmd", OPT_DENIED)
def test_an_option_value_cannot_hide_the_subcommand(ws, cmd):
    _check(ws, cmd, False)


def test_a_cwd_inside_state_stays_denied(ws):
    state = ws.root / "orchestrator" / ".state"
    state.mkdir(parents=True, exist_ok=True)
    for cmd in ("echo x > y", "cat y > /tmp/z", "python3 -c 'print(1)'", "rm y"):
        d = _bash(ws, cmd, cwd=state)
        assert not d.allow, cmd
