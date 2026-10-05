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


def test_bridge_records_are_never_grantable(ws):
    from orch.core import permits
    assert permits.never_grantable(ws, f"cat permits/bridge/{WS}/registry.json")
    assert permits.never_grantable(ws, f"cp x $ORCH_STATE_DIR/permits/bridge/{WS}/registry.json")
