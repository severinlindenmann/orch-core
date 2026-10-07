"""A differential corpus for the guard's path rules: every spelling of a protected path that reaches it (as the shell or
a file tool would read it) is refused, by every reader and every tool; a spelling that cannot reach it (a homoglyph, a
percent escape, shell syntax handed to a file tool, which takes paths literally) is shown to name another file. The
protected names come from the guard's own list, never copied here."""
import os

import pytest

from orch.hooks import guard
from orch.hooks.guard import evaluate

TOP = ("ledger", "launch.json", "remote-humans")  # names that sit in the config dir itself; the rest in permits/


def _rel(name):
    return name if name.startswith(TOP) else f"permits/{name}"


NAMES = sorted({_rel(n) for n in guard._SENSITIVE_NAMES})


def _bash_spellings(cfg_rel, t):
    d, _, leaf = t.rpartition("/")
    d = f"{cfg_rel}/{d}" if d else cfg_rel
    return {  # all of these reach the file when a shell reads them (APFS ignores case)
        "tilde": f"~/{d}/{leaf}", "$HOME": f"$HOME/{d}/{leaf}", "${HOME}": f"${{HOME}}/{d}/{leaf}",
        "dq-home": f'"$HOME/{d}/{leaf}"', "quote-split": f"~/{d}/{leaf[0]}''{leaf[1:]}",
        "backslash": f"~/{d}/{leaf[0]}\\{leaf[1:]}", "dot": f"~/./{d}/./{leaf}", "dotdot": f"~/{d}/../{d.split('/')[-1]}/{leaf}"
        if "/" in d else f"~/{d}/x/../{leaf}", "double-slash": f"~//{d}//{leaf}", "upper": f"~/{d}/{leaf.upper()}",
        "glob-q": f"~/{d}/{leaf[:-1]}?", "glob-star": f"~/{d}/{leaf[:3]}*", "brace": f"~/{d}/{{{leaf},x}}",
        "class": f"~/{d}/[{leaf[0]}]{leaf[1:]}",
    }


NOT_REACHING = {"unicode": lambda leaf: leaf.replace(leaf[1], "е" if leaf[1] == "e" else "а", 1)
                if leaf[1] in "ea" else "е" + leaf, "percent": lambda leaf: f"%{ord(leaf[0]):x}{leaf[1:]}"}

READERS = {
    "cat": "cat {p}", "head": "head -c 100 {p}", "sed": "sed -n 1p {p}", "awk": "awk 1 {p}",
    "python": "python3 -c \"print(open('{p}').read())\"", "find-exec": "find {d} -name x -exec cat {{}} +",
    "xargs": "echo {p} | xargs cat", "cp": "cp {p} /tmp/x",
}


@pytest.fixture
def cfg(ws, monkeypatch, tmp_path):
    home = tmp_path / "h"
    c = home / ".config" / "orch"
    for name in NAMES:
        f = c / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.mkdir() if "." not in name.rsplit("/", 1)[-1] else f.write_text("x")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORCH_STATE_DIR", str(c))
    return home, c


def _bash(ws, cmd):
    return evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)})


def test_the_names_come_from_the_guard():
    assert "ledger.key" in NAMES and "permits/release-records" in NAMES and "permits/child-clones" in NAMES


@pytest.mark.parametrize("name", NAMES)
def test_every_reaching_shell_spelling_is_refused(ws, cfg, name):
    home, c = cfg
    allowed = []
    for sname, p in _bash_spellings(".config/orch", name).items():
        for rname, form in READERS.items():
            cmd = form.format(p=p, d=os.path.dirname(p) or ".")
            if _bash(ws, cmd).allow:
                allowed.append((sname, rname, cmd))
    leaf = name.rsplit("/", 1)[-1]
    for cmd in (f"cd ~/.config && cat orch/{name}", f"cd {c} && cat {name}", f"cd ~/.config/orch; sed -n 1p {name}",
                f"cd {c}/permits && cat ../{name}", f"cd ~/.config/orch/ && ls"):
        if _bash(ws, cmd).allow:
            allowed.append(("cd", "", cmd))
    assert allowed == [], allowed
    for sname, f in NOT_REACHING.items():
        assert not (c / name.replace(leaf, f(leaf))).exists(), sname  # names another file: not a gap


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Grep", "Glob"])
def test_every_reaching_file_tool_spelling_is_refused(ws, cfg, tool):
    home, c = cfg
    allowed = []
    for name in NAMES:
        leaf = name.rsplit("/", 1)[-1]
        for p in (str(c / name), f"{c}/./{name}", f"{c}//{name}", f"{c}/permits/../{name}",
                  str(c / name).replace("/orch/", "/ORCH/"), str(c / name)[:-len(leaf)] + leaf.upper()):
            ti = {"file_path": p, "notebook_path": p, "path": p, "pattern": "x" if tool == "Grep" else p,
                  "content": "x", "old_string": "a", "new_string": "b", "new_source": "x",
                  "edits": [{"old_string": "a", "new_string": "b"}]}
            if evaluate(ws, {"tool_name": tool, "tool_input": ti, "cwd": str(ws.root)}).allow:
                allowed.append(p)
    assert allowed == [], allowed
    # a file tool takes its path literally: shell syntax in it names another file, which does not exist
    for literal in (f"{c}/l''edger.key", f"{c}/l\\edger.key", f"{c}/led*", f"{c}/{{ledger.key,x}}", f"{c}/l%65dger.key"):
        assert not os.path.exists(literal)


# -- sibling-path parity: the same rule for every tool and every spelling of the same place ---------------------------

WRITERS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SHELL_WRITES = ("echo x > {p}", "cp /dev/null {p}", "tee {p} < /dev/null")


def _write_ti(tool, p):
    if tool == "NotebookEdit":
        return {"notebook_path": p, "new_source": "x"}
    if tool == "MultiEdit":
        return {"file_path": p, "edits": [{"old_string": "a", "new_string": "b"}]}
    return {"file_path": p, "content": "x", "old_string": "a", "new_string": "b"}


def _variants(base: str, rel: str):
    head, _, tail = rel.partition("/")
    return [f"{base}/{rel}", f"{base}/{rel.upper()}", f"{base}/{head.upper()}/{tail}", f"{base}/./{head}//{tail}",
            f"{base}/x/../{rel}"]


def test_protected_places_are_refused_alike_by_every_writer(ws, cfg):
    from orch.core import factory_clones
    clone = factory_clones.root() / "wsid" / "L-0002" / "repo"
    for d in ("orchestrator/tickets/open", "orchestrator/.state", ".claude"):
        (clone / d).mkdir(parents=True)
    (clone / "orchestrator" / "config.json").write_text("{}")
    (ws.root / ".git").mkdir()
    (ws.root / ".git" / "config").write_text("")
    (ws.home / ".state").mkdir(exist_ok=True)
    places = {
        "clone config": _variants(str(clone), "orchestrator/config.json")
        + [str(clone).replace("orch-clones", "ORCH-CLONES") + "/orchestrator/config.json"],
        "clone state": _variants(str(clone), "orchestrator/.state/events.jsonl"),
        "clone tickets": _variants(str(clone), "orchestrator/tickets/open/L-0002-x.md"),
        "clone harness": _variants(str(clone), ".claude/settings.json"),
        "workspace state": _variants(str(ws.root), "orchestrator/.state/events.jsonl"),
        "workspace tickets": _variants(str(ws.root), "orchestrator/tickets/open/L-0009-x.md"),
        "git dir": _variants(str(ws.root), ".git/config"),
    }
    allowed = []
    for kind, paths in places.items():
        for p in paths:
            for tool in WRITERS:
                if evaluate(ws, {"tool_name": tool, "tool_input": _write_ti(tool, p), "cwd": str(ws.root)}).allow:
                    allowed.append((kind, tool, p))
            if kind == "clone harness":
                continue  # the shell's harness rule is user scope (~/.claude); a clone's settings are file tools only
            for form in SHELL_WRITES:
                if _bash(ws, form.format(p=f"'{p}'")).allow:
                    allowed.append((kind, "Bash", form))
    assert allowed == [], allowed
