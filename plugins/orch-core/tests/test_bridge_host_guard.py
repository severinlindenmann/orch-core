"""The bridge's records (host key, registry, audit log, request store, sequence state) live in the orch config dir's
permits folder, and orch's guard keeps agents' commands and file tools away from them. The operating-system user is
shared, so the guard is the only barrier (orch-tix docs/bridge-protocol.md §2.7)."""
import pytest

from orch.remote.bridge_host import files

WS = "3e7afa7ff8e262a2b4d26f4442d0b890"
NAMES = ("host-key.pem", "registry.json", "audit.jsonl", "requests/" + "ab" * 16 + ".json", "seq/" + "cd" * 16 + ".json")


def _root():
    from orch.core.ledger import base_dir
    return files.bridge_dir(base_dir(), WS)


def test_the_bridge_dir_is_inside_the_guarded_permits_folder():
    from orch.core.ledger import base_dir
    assert _root().relative_to(base_dir()).parts[:2] == ("permits", "bridge")


@pytest.mark.parametrize("cmd", [
    "cat {base}/permits/bridge/{ws}/host-key.pem", "rm {base}/permits/bridge/{ws}/registry.json",
    "echo x >> $ORCH_STATE_DIR/permits/bridge/{ws}/audit.jsonl",
    "cp /dev/null ${{XDG_CONFIG_HOME}}/orch/permits/bridge/{ws}/registry.json",
    "ls ~/.config/orch/permits/bridge", "cd {base}/permits/bridge && cat {ws}/registry.json",
    "cd {base} && sed -i s/look/type/ permits/bridge/{ws}/registry.json", "cat permits/bridge/{ws}/registry.json",
    "tee permits/bridge/{ws}/seq/x.json < /dev/null", "python3 -c \"open('{base}/permits/bridge/{ws}/registry.json','w')\"",
    "cat {base}/permits/bridge/*/registry.json", "find {base}/permits -name '*.pem'",
    # spellings re-checked in the review: quoting, escapes, dot segments, case, globs, cd, redirects, links,
    # copies of the parent, here-documents, substitution, variables, Windows separators, interpreters
    'cat "{base}"/permits/bridge/{ws}/registry.json', "cat {base}/perm\\its/bridge/{ws}/registry.json",
    "cat {base}/permits/../permits/bridge/{ws}/registry.json", "cat {base}/PERMITS/Bridge/{ws}/registry.json",
    "cat {base}/p?rmits/bridge/{ws}/registry.json", "cat {base}/[p]ermits/bridge/{ws}/registry.json",
    "cd {base}/permits/bridge/{ws} && cat registry.json", "cd {base}/permits && cat bridge/{ws}/registry.json",
    "cat < {base}/permits/bridge/{ws}/registry.json", "dd if={base}/permits/bridge/{ws}/host-key.pem",
    "cp -r {base}/permits /tmp/x", "cp -r {base} /tmp/x", "tar cf /tmp/x.tar {base}", "rsync -a {base}/ /tmp/x",
    "ln {base}/permits/bridge/{ws}/registry.json /tmp/r", "ln -s {base}/permits /tmp/p",
    "mv {base}/permits/bridge /tmp/b", "truncate -s0 {base}/permits/bridge/{ws}/audit.jsonl",
    "find {base} -name registry.json -exec cat {{}} +", "ls -R {base}", "grep -r device {base}",
    "cat <<EOF > {base}/permits/bridge/{ws}/registry.json\n{{}}\nEOF", "cat $(echo {base})/permits/bridge/x",
    "D={base}/permits; cat $D/bridge/x", "cat $ORCH_STATE_DIR/PERMITS/bridge/x", "git -C {base}/permits/bridge status",
    "cat {base}\\permits\\bridge\\x", "cp x permits\\bridge\\{ws}\\registry.json", "cat Permits/BRIDGE/x",
    "node -e \"require('fs').readFileSync('{base}/permits/bridge/{ws}/registry.json')\"",
    "perl -e 'open F,\"<{base}/permits/bridge/{ws}/registry.json\"'",
    # a path joined from separate words by an interpreter (refused since this review)
    "python3 -c \"import pathlib;print(list(pathlib.Path('{base}','permits','bridge').iterdir()))\"",
    "python3 -c \"import pathlib;print(pathlib.Path('{base}') / 'permits' / 'bridge')\"",
    "node -e \"require('path').join('{base}','permits','bridge')\"",
    "python3 -c \"import os;print(os.listdir(os.path.join(os.environ['ORCH_STATE_DIR'],'permits')))\"",
])
def test_guard_keeps_agents_commands_away_from_the_bridge_records(ws, cmd):
    from orch.core.ledger import base_dir
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd.format(base=base_dir(), ws=WS)},
                      "cwd": str(ws.root)})
    assert not d.allow, cmd


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("tool,key", [("Read", "file_path"), ("Write", "file_path"), ("Edit", "file_path"),
                                      ("Grep", "path"), ("Glob", "path")])
def test_guard_keeps_agents_file_tools_away_from_the_bridge_records(ws, tool, key, name):
    from orch.hooks.guard import evaluate
    inp = {key: str(_root() / name), "content": "{}", "old_string": "a", "new_string": "b", "pattern": "x"}
    assert not evaluate(ws, {"tool_name": tool, "tool_input": inp, "cwd": str(ws.root)}).allow


@pytest.mark.parametrize("cmd", [
    "cat src/orch/remote/bridge_host/registry.py", "grep -rn bridge docs", "python3 -c \"print('permits')\"",
    "ls /tmp/bridge", "git status", "uv run pytest -q tests/test_bridge_host.py", "cat {ws_root}/README.md",
])
def test_guard_still_allows_ordinary_commands_near_these_words(ws, cmd):
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd.format(ws_root=ws.root)}, "cwd": str(ws.root)})
    assert d.allow, (cmd, d.reason)


@pytest.mark.parametrize("tool,inp", [
    ("Glob", {"pattern": "*/bridge/*/registry.json", "path": "{base}"}),
    ("Glob", {"pattern": "*/*/*/host-key.pem", "path": "{base}"}),
    ("Glob", {"pattern": "P*/b*/*/seq/*", "path": "{base}"}),
    ("Glob", {"pattern": "{base}/*/bridge/*/registry.json"}),
    ("Glob", {"pattern": "{base}/*/b*/*/a*"}),
    ("Glob", {"pattern": "*/*/*/*/registry.json", "path": "{parent}"}),
    ("Grep", {"pattern": "pub", "path": "{base}", "glob": "registry.json"}),
    ("Grep", {"pattern": "pub", "path": "{base}", "glob": "*/bridge/*/*.json"}),
])
def test_guard_keeps_listings_and_filters_away_from_the_bridge_records(ws, tool, inp):
    from orch.core.ledger import base_dir
    from orch.hooks.guard import evaluate
    (base_dir() / "permits" / "bridge" / WS).mkdir(parents=True, exist_ok=True)
    got = {k: v.replace("{base}", str(base_dir())).replace("{parent}", str(base_dir().parent)) for k, v in inp.items()}
    assert not evaluate(ws, {"tool_name": tool, "tool_input": got, "cwd": str(ws.root)}).allow


@pytest.mark.parametrize("tool,inp", [
    ("Glob", {"pattern": "src/*/registry.py", "path": "{root}"}),
    ("Glob", {"pattern": "*/registry.json", "path": "{root}"}),
    ("Glob", {"pattern": "{root}/*/bridge/*.md"}),
    ("Grep", {"pattern": "pub", "path": "{root}", "glob": "registry.json"}),
])
def test_guard_still_allows_listings_outside_the_config_dir(ws, tool, inp):
    from orch.hooks.guard import evaluate
    got = {k: v.replace("{root}", str(ws.root)) for k, v in inp.items()}
    d = evaluate(ws, {"tool_name": tool, "tool_input": got, "cwd": str(ws.root)})
    assert d.allow, d.reason


def test_bridge_records_are_never_grantable(ws):
    from orch.core import permits
    assert permits.never_grantable(ws, f"cat permits/bridge/{WS}/registry.json")
    assert permits.never_grantable(ws, f"cp x $ORCH_STATE_DIR/permits/bridge/{WS}/registry.json")
