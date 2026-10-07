"""Every CLI option that reads a file an agent names goes through cli._read / fsutil.agent_source (a bound factory
session hands orch only files inside the workspace). The options are derived from the command tree, never listed by
hand: a new path option fails here until it is proved refused or exempted with a reason."""
import json

import typer

from orch.cli import app
from test_dark_profile import _ok, _run
from test_factory_planner import _bound_session

# Options that do not read an agent-named file into orch's records, each with why.
EXEMPT = {
    ("init", "directory"): "the folder a new workspace is made in; nothing is read from it",
    ("addon", "check", "path"): "addon administration: checks a folder, reads nothing into a ticket",
    ("addon", "install", "path"): "addon administration is the human's (the guard refuses agents)",
    ("widget", "render", "out"): "a file orch writes, not one it reads",
    ("factory", "release", "set", "file"): "human only: refused to agents before the file is read",
    ("hook", "commit-msg", "file"): "git hands it its own message file; no baseline rule names `orch hook`",
    ("hooks", "install", "repo"): "a repository folder hooks are written into; nothing is read into a ticket",
}

# How to reach each reading option with the file `{f}` (`{t}`: a ticket id).
ARGS = {
    ("new", "body_file"): ["new", "-t", "y", "--body-file", "{f}"],
    ("new", "requirements_file"): ["new", "-t", "y", "--requirements-file", "{f}"],
    ("new", "acceptance_file"): ["new", "-t", "y", "--acceptance-file", "{f}"],
    ("new", "out_of_scope_file"): ["new", "-t", "y", "--out-of-scope-file", "{f}"],
    ("new", "summary_file"): ["new", "-t", "y", "--summary-file", "{f}"],
    ("state", "file"): ["state", "{t}", "--file", "{f}"],
    ("ask", "file"): ["ask", "{t}", "--file", "{f}"],
    ("section", "set", "file"): ["section", "set", "{t}", "Plan", "--file", "{f}"],
    ("artifact", "add", "file"): ["artifact", "add", "{t}", "{f}"],
    ("task", "add", "file"): ["task", "add", "{t}", "--file", "{f}"],
    ("widget", "add", "file"): ["widget", "add", "{t}", "--section", "Context", "--type", "stats", "--file", "{f}"],
    ("feedback", "add", "file"): ["feedback", "add", "--file", "{f}"],
}


def _path_options():
    def walk(c, path):
        if hasattr(c, "commands"):
            for name, sub in c.commands.items():
                yield from walk(sub, path + (name,))
        else:
            yield path, c
    out = set()
    for path, cmd in walk(typer.main.get_command(app), ()):
        for p in cmd.params:
            if "Path" in type(p.type).__name__ or "file" in p.name or "path" in p.name:
                out.add((*path, p.name))
    return out


def test_every_path_option_is_proved_or_exempted():
    found = _path_options()
    assert found, "the walk found no option at all"
    assert found == set(ARGS) | set(EXEMPT), (f"not covered: {sorted(found - set(ARGS) - set(EXEMPT))}; "
                                             f"gone: {sorted((set(ARGS) | set(EXEMPT)) - found)}")


def test_a_bound_session_is_refused_every_reading_option_outside_the_workspace(ws, human, tmp_path, capsys,
                                                                                monkeypatch):
    secret = tmp_path / "outside" / "secret.md"
    secret.parent.mkdir()
    secret.write_text("PRIVATE", encoding="utf-8")
    tid = json.loads(_ok(capsys, "new", "-t", "x", "--json"))["id"]
    _bound_session(ws, human, monkeypatch)
    for key, args in ARGS.items():
        code, out = _run(capsys, *[a.format(f=secret, t=tid) for a in args])
        assert code != 0 and "an agent cannot hand orch the file" in out.err, (key, out.err)
