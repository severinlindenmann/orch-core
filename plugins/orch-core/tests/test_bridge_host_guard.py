"""The bridge's records (host key, registry, audit log, request store, sequence state) live in the orch config dir's
permits folder, and orch's guard keeps agents' commands and file tools away from them. The operating-system user is
shared, so the guard is the only barrier (orch-tix docs/bridge-protocol.md §2.7)."""
from pathlib import Path

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
    # the relative spelling (a cd elsewhere first): refused wherever it is written, as for the factory's folders
    "cat permits/bridge*", "cat permits/bridge/", "cat permits//bridge", "ls PERMITS/bridge",
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


# -- the guard decides on the normalised path, not on how it is written (third review) --------------------------------

def _forms(tmp_path):
    """Spellings of the config dir for the current test: as is, case-changed, with dot and doubled segments, through
    a symlink, from its parent, and a sibling whose name only starts the same way."""
    import os
    from orch.core.ledger import base_dir
    base = base_dir()
    (base / "permits" / "bridge" / WS).mkdir(parents=True, exist_ok=True)
    link = tmp_path / "lnk"
    if not link.exists():
        link.symlink_to(base)
    sibling = Path(str(base) + "1")
    sibling.mkdir(exist_ok=True)
    return {"base": str(base), "up": str(base).upper(), "dot": f"{base}/.", "dbl": str(base).replace("/", "//"),
            "dotdot": f"{base}/x/..", "lnk": str(link), "parent": str(base.parent), "name": base.name,
            "sibling": str(sibling), "rel": os.path.relpath(base, tmp_path / "ws")}


@pytest.mark.parametrize("cmd", [
    "python3 -c \"import pathlib;print(pathlib.Path('{up}','permits','bridge'))\"",
    "python3 -c \"import pathlib;print(pathlib.Path('{dot}','permits','bridge'))\"",
    "python3 -c \"import pathlib;print(pathlib.Path('{dbl}','permits','bridge'))\"",
    "python3 -c \"import pathlib;print(pathlib.Path('{dotdot}','permits','bridge'))\"",
    "python3 -c \"import pathlib;print(pathlib.Path('{lnk}','permits','bridge'))\"",
    "D={base}; python3 -c \"import pathlib;print(pathlib.Path('$D','permits'))\"",
    "cd {parent} && python3 -c \"open('{name}/'+'permits/bridge/x')\"",
    "python3 -c \"import os;os.chdir('{parent}');open(os.path.join('{name}','permits'))\"",
])
def test_a_differently_written_config_dir_is_still_the_config_dir(ws, tmp_path, cmd):
    from orch.hooks.guard import evaluate
    c = cmd.format(**_forms(tmp_path))
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(ws.root)}).allow, c


def test_a_command_run_from_above_the_config_dir_that_mentions_permits_is_refused(ws, tmp_path):
    from orch.hooks.guard import evaluate
    f = _forms(tmp_path)
    c = "python3 -c \"open('" + f["name"] + "'+'/permits')\""
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": f["parent"]}).allow


@pytest.mark.parametrize("cmd", [
    "python3 -c \"import pathlib;print(pathlib.Path('{sibling}','permits'))\"",  # a sibling that only shares a prefix
    "echo permits > {ws_root}/notes.txt", "cat {ws_root}/permits/bridge.md", "cat docs/permits/bridges/x",
    "cat {ws_root}/permitsx/bridge/x",
])
def test_neighbours_of_the_config_dir_are_not_it(ws, tmp_path, cmd):
    from orch.hooks.guard import evaluate
    c = cmd.format(ws_root=ws.root, **_forms(tmp_path))
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(ws.root)})
    assert d.allow, (c, d.reason)


@pytest.mark.parametrize("tool,inp", [
    ("Glob", {"pattern": "*/bridge/*/registry.json", "path": "{up}"}),
    ("Glob", {"pattern": "*/bridge/*/registry.json", "path": "{lnk}"}),
    ("Glob", {"pattern": "*/bridge/*/registry.json", "path": "{dotdot}"}),
    ("Grep", {"pattern": "pub", "path": "{up}", "glob": "registry.json"}),
    ("Glob", {"pattern": "{rel}/*/bridge/*/registry.json", "path": "{ws_root}"}),  # a relative pattern escaping up
    ("Glob", {"pattern": "../*/../" + "{name}/*/bridge/*", "path": "{base}/permits"}),
    ("Glob", {"pattern": "*/../../*/*/bridge/*/registry.json", "path": "{ws_root}"}),  # `..` after a glob
    ("Read", {"file_path": "{up}/permits/bridge/" + WS + "/registry.json"}),
    ("Read", {"file_path": "{lnk}/permits/bridge/" + WS + "/registry.json"}),
    ("Write", {"file_path": "{dbl}//permits//bridge//x", "content": ""}),
    ("Edit", {"file_path": "{dotdot}/permits/bridge/x", "old_string": "a", "new_string": "b"}),
])
def test_file_tools_decide_on_the_normalised_path(ws, tmp_path, tool, inp):
    from orch.hooks.guard import evaluate
    f = {**_forms(tmp_path), "ws_root": str(ws.root)}
    got = {k: v.format(**f) for k, v in inp.items()}
    if "{rel}" in inp.get("pattern", ""):
        import os
        got["pattern"] = os.path.relpath(f["base"], ws.root) + "/*/bridge/*/registry.json"
    assert not evaluate(ws, {"tool_name": tool, "tool_input": got, "cwd": str(ws.root)}).allow, got


@pytest.mark.parametrize("tool,inp", [
    ("Glob", {"pattern": "*/bridge/*/registry.json", "path": "{sibling}"}),
    ("Glob", {"pattern": "../{ws_name}/src/*.py", "path": "{ws_root}"}),
    ("Read", {"file_path": "{sibling}/permits/notes.md"}),
])
def test_file_tools_still_allow_neighbours(ws, tmp_path, tool, inp):
    from orch.hooks.guard import evaluate
    f = {**_forms(tmp_path), "ws_root": str(ws.root), "ws_name": ws.root.name}
    got = {k: v.format(**f) for k, v in inp.items()}
    d = evaluate(ws, {"tool_name": tool, "tool_input": got, "cwd": str(ws.root)})
    assert d.allow, (got, d.reason)


# -- caps fail closed, and the forms the shell rewrites before it runs anything (fourth review) ------------------------

P = "{base}/permits/bridge/" + WS + "/registry.json"
R = "permits/bridge/" + WS + "/registry.json"
_CAP_AND_PARSER = {
    "long_filler": "echo " + "a" * 200_000 + "; cat " + P,
    "long_filler_rel": "echo " + "a" * 200_000 + "; cat " + R,
    "long_filler_join": "echo " + "a" * 200_000 + "; python3 -c \"import pathlib;pathlib.Path('{base}','permits')\"",
    "many_segments": "; ".join(["true"] * 5000) + "; cat " + P,
    "many_segments_rel": "; ".join(["true"] * 5000) + "; cat " + R,
    "many_words_join": "echo " + " ".join(["w"] * 5000) + " && python3 -c \"import pathlib;pathlib.Path('{base}','permits')\"",
    "many_paths_join": "ls " + " ".join(["./x"] * 3000) + " && python3 -c \"import pathlib;pathlib.Path('{base}','permits')\"",
    "deep_subst": "$(" * 200 + "cat " + P + ")" * 200,
    "deep_subst_rel": "$(" * 200 + "cat " + R + ")" * 200,
    "bash_c": "bash -c 'cat " + R + "'",
    "sh_c_dq": "sh -c \"cat " + R + "\"",
    "eval": "eval 'cat " + R + "'",
    "ansi_c": "cat $'permits/bri\\x64ge/" + WS + "/registry.json'",
    "ansi_c_abs": "cat $'{base}/perm\\x69ts/bridge/x'",
    "xargs": "echo " + R + " | xargs cat",
    "env": "env cat " + R,
    "command": "command cat " + R,
    "continuation": "cat perm\\\nits/bridge/" + WS + "/registry.json",
    "continuation_abs": "cat {base}/perm\\\nits/bridge/x",
    "nbsp": "cat " + R,
    "tab": "cat\t" + R,
    "backticks": "cat `echo " + R + "`",
    "heredoc_bash": "bash <<'EOF'\ncat " + R + "\nEOF",
    "and_or_pipe": "true && false || cat " + R + " | head &",
    "newline": "true\ncat " + R,
    "brace": "cat permits/{{bridge,x}}/" + WS + "/registry.json",
    "quote_split": "cat per'mi'ts/br\"id\"ge/x",
    "empty_quote": "cat permits/''bridge/x",
    "ansi_join": "python3 -c \"import pathlib;pathlib.Path('{base}',$'perm\\x69ts')\"",
}



@pytest.mark.parametrize("name", list(_CAP_AND_PARSER))
def test_caps_and_shell_forms_around_the_bridge_records_are_refused(ws, name):
    from orch.core.ledger import base_dir
    from orch.hooks.guard import evaluate
    c = _CAP_AND_PARSER[name].replace("{base}", str(base_dir())).replace("{{", "{").replace("}}", "}")
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(ws.root)}).allow, name


def test_braces_past_the_expansion_cap_near_the_records_are_refused(ws):
    from orch.hooks.guard import evaluate
    opts = ",".join(f"a{i}" for i in range(40)) + ",bridge"
    for c in ("cat permits/{" + opts + "}/" + WS + "/registry.json",
              "cat {x,y}{x,y}{x,y}{x,y}{x,y}{x,y}permits/{a,bridge}/" + WS):
        assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(ws.root)}).allow, c


@pytest.mark.parametrize("cmd", [
    "echo " + "a" * 5_000 + "; ls src", "; ".join(["true"] * 5000) + "; ls src",
    "echo " + " ".join(["./w"] * 3000), "bash -c 'ls src'", "sh -c \"git status\"", "echo $'hello\\tworld'",
    "ls src/{" + ",".join(f"a{i}" for i in range(40)) + "}", "env ls src", "command ls src", "echo x | xargs ls",
    "ls sr\\\nc", "echo $'bridge'",
])
def test_long_or_unusual_commands_away_from_the_records_still_pass(ws, cmd):
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)})
    assert d.allow, (cmd[:80], d.reason)


# -- an error or an unresolvable path in the new checks refuses (fail closed) --------------------------------------

def _boom(*a, **k):
    raise RuntimeError("injected")


@pytest.mark.parametrize("helper", ["_config_keys", "_path_key", "_resolve_vars"])
def test_an_error_in_the_joined_path_check_refuses_a_command_naming_permits(ws, monkeypatch, helper):
    from orch.hooks import guard
    monkeypatch.setattr(guard, helper, _boom)
    d = guard._bash_joins_permits("python3 -c \"import pathlib;pathlib.Path('/somewhere','permits')\"", str(ws.root))
    assert d is True
    assert guard._bash_joins_permits("ls src", str(ws.root)) is False  # no mention: nothing to refuse


@pytest.mark.parametrize("helper", ["_config_keys", "_path_key", "_is_config_dir_or_ancestor"])
def test_an_error_in_the_listing_checks_refuses(ws, monkeypatch, helper):
    from orch.core.ledger import base_dir
    from orch.hooks import guard
    monkeypatch.setattr(guard, helper, _boom)
    inp = {"pattern": "*/bridge/*/registry.json", "path": str(base_dir())}
    assert not guard.evaluate(ws, {"tool_name": "Glob", "tool_input": inp, "cwd": str(ws.root)}).allow


def test_a_glob_whose_start_cannot_be_resolved_is_refused(ws, monkeypatch):
    from orch.hooks import guard
    real = guard._resolve_root
    monkeypatch.setattr(guard, "_resolve_root", lambda raw: None if "unresolvable" in str(raw) else real(raw))
    inp = {"pattern": "/unresolvable/x/*/bridge/*", "path": str(ws.root)}
    assert not guard.evaluate(ws, {"tool_name": "Glob", "tool_input": inp, "cwd": str(ws.root)}).allow
    ok = {"pattern": "src/*.py", "path": str(ws.root)}
    assert guard.evaluate(ws, {"tool_name": "Glob", "tool_input": ok, "cwd": str(ws.root)}).allow
