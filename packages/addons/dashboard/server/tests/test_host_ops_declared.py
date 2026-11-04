"""Every host op this server calls is declared in `hostlink.OPS` with the kind core serves it as.

`HostLink.call` treats an op that is not in `OPS` as a WRITE and demands an idem, so a read that was never listed
fails at runtime with a ValueError (HTTP 500): `gate.input` did, and approve_both never worked on a real host (run 12).
This scans the server source (AST) for every literal op name and checks it against core's own op table.

Call shapes covered (a string constant among the first two positional arguments, the op name):
  * `<link>.call("op", ...)`, `self.call("op", ...)`      (HostLink.call, a read unless `write=`/`idem=` is given)
  * `<gateway>.call(ws, "op", ...)`                          (Gateway.call: always a read of the enabled link)
  * `<x>.call("op", ..., write=True)`                        (a write; `idem=` counts as write too)
  * `link_for(ws, "op")` and helpers: their result is a HostLink, so the `.call(` on it is the site that is checked
Not covered, by design: ops built from variables (listed in DYNAMIC below with the reason) and the signer child's own
connection (`click_signer`, `human.*` phases are checked by their own tests).
"""

import ast
import re
from pathlib import Path

import pytest
from orch.host.dispatch import op_kind, served_ops

from orch_dashboard import hostlink

SRC = Path(hostlink.__file__).parent
NAME = re.compile(r"^[a-z][a-z_0-9]*(\.[a-z_0-9]+)*$")
#: files whose `.call` is not a HostLink call: the signer child's channel (its own protocol, `op` is a variable)
SKIP_FILES = {"click_signer.py", "click.py", "stepup_routes.py"}  # `signer.call("custody_off", ...)` etc.: not host ops
#: served on the control socket (control/1), not the workspace socket: not in core's `served_ops()`
CONTROL_OPS = {"host.list": "host.list is the control socket's registry read"}
#: core serves these as person writes but they take NO idem (plain person writes, listed in OPS on purpose): called
#: without `write=`/`idem=` by design (the OPS comments say so). Anything else core calls a write must say so here.
PLAIN_PERSON_WRITES = {
    "run.stop": "plain person write, no idem",
    "run.link": "plain person write, no idem (P2-6)",
    "run.detach": "plain person write, no idem (P2-6)",
    "term.attach": "opens an attach ticket: a write in core, no idem, one use",
    "attach.cancel": "withdraws an attach ticket: a write in core, no idem",
}
#: ops that are sent without a literal name at the call site, with the reason (each is checked in OPS below)
DYNAMIC = {
    "hostlink.py": "OPS itself and the helpers that forward `op` (the table is what is being checked)",
    "gateway.py": "Gateway.call(ws, op, args) forwards its argument",
}
#: in OPS but not a plain op name core serves in `served_ops()`: none today. Add with a reason, never silently.
NOT_IN_CORE: dict[str, str] = {}
#: ops core lists with a phase other than read/write (human API, wait): sent by their own routes with write semantics
HUMAN_PHASES = {"prepare", "submit", "cancel", "get", "pending"}


def sites():
    """(op, file, line, is_write) for every `.call` with a literal op name."""
    out = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in SKIP_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "call"):
                continue
            op = next(
                (
                    a.value
                    for a in node.args[:2]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and NAME.match(a.value)
                ),
                None,
            )
            if op is None:
                continue
            write = any(
                (k.arg == "write" and not (isinstance(k.value, ast.Constant) and k.value.value is False))
                or k.arg == "idem"
                for k in node.keywords
            )
            out.append((op, path.name, node.lineno, write))
    return out


def test_the_scan_finds_the_call_sites():
    found = sites()
    assert len(found) > 100 and ("gate.input", "signing.py", 0, False)[0] in {s[0] for s in found}


def check_sites(ops, found):
    """Problems as 'op at file:line: why' strings."""
    problems = []
    for op, file, line, write in found:
        where = f"{op!r} at {file}:{line}"
        if op in hostlink.FORBIDDEN:
            continue
        if op in CONTROL_OPS:
            continue
        if op not in ops and write:
            if op_kind(op) is None:
                problems.append(f"{where}: written, but core does not serve this op")
            continue
        if op not in ops:
            problems.append(
                f"{where}: not in hostlink.OPS, so HostLink.call treats it as a write and demands an idem "
                "(add it to OPS if core serves it as a read)"
            )
            continue
        core = op_kind(op)
        if core is None:
            problems.append(f"{where}: core does not serve this op")
            continue
        _, phase = core
        if phase == "wait":
            continue
        if phase in ("read",) and write:
            problems.append(f"{where}: called as a write, but core serves it as a read")
        elif phase == "write" and not write and op != "human.submit" and op not in PLAIN_PERSON_WRITES:
            problems.append(f"{where}: called as a read (no write=/idem=), but core serves it as a write")
    return problems


def test_every_op_the_server_calls_is_declared_with_its_core_kind():
    problems = check_sites(hostlink.OPS, sites())
    assert not problems, "\n".join(problems)


def test_every_declared_op_exists_in_core():
    served = set(served_ops())
    stale = sorted(op for op in hostlink.OPS if op not in served and op not in NOT_IN_CORE and op not in CONTROL_OPS)
    assert not stale, f"in hostlink.OPS but not served by core (stale or misspelled): {stale}"


def test_the_check_would_have_caught_gate_input():
    """The run-12 bug: `gate.input` missing from OPS. The same check, with it removed in memory, names op and place."""
    without = {k: v for k, v in hostlink.OPS.items() if k != "gate.input"}
    problems = check_sites(without, sites())
    assert any(p.startswith("'gate.input' at signing.py:") and "not in hostlink.OPS" in p for p in problems), problems


@pytest.mark.parametrize("file", sorted(DYNAMIC))
def test_dynamic_files_are_listed_with_a_reason(file):
    assert (SRC / file).is_file() and DYNAMIC[file]
