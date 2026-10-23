"""H12a, ``test_column_r_sites`` (orch-v2-host §11A.4): every site in ``orch/host`` that compares a column name, or
reads ``<…>column.name``, is listed here with column R's answer. A new site (or a changed count in a listed function)
fails this test until it is listed with R's answer, so no op is served to R by a branch nobody looked at.

The inventory is syntactic (AST): a comparison with a column-name literal (``"P"``, ``{"P", "G"}``, …) whose other
side mentions ``column``, and every attribute read ``….column.name``. Counts are per function.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from orch.host.auth import Verified
from orch.host.columns import Column, refusal, resolve_relay
from orch.host.errors import HostError

SRC = Path(__file__).resolve().parents[2] / "src" / "orch" / "host"
COLS = {"P", "G", "N", "W", "C", "R"}
PERSON = "p_" + "2" * 32
DEV = "d_" + "1" * 32

#: (module, function) -> (sites, R's answer). "unreachable" means the op class is outside R's closed op list
#: (``columns.RELAY_OPS``) and its matrix row, so ``Call.authorize`` refuses before the site runs; the answer given
#: is what the site would do anyway (it never grants R anything).
SITES: dict[tuple[str, str], tuple[int, str]] = {
    ("bus.py", "Bus._flush"): (1, "unreachable (sub.open is not an R op); owner is P only, so R would be no owner"),
    ("bus.py", "Bus.principal_end"): (
        3,
        "unreachable; fail closed: a subscription of any column but P, G or N ends at once (person.removed)",
    ),
    ("columns.py", "refusal"): (5, "R: only its closed op list under its matrix row; human ops op.not_allowed"),
    ("diff.py", "prepare"): (2, "R pins no base (only a P call may write the base pin): reads stay side-effect free"),
    ("dispatch.py", "RegistryBackend._context"): (2, "R carries no grant: the context's grant is None"),
    ("dispatch.py", "RegistryBackend._register"): (3, "unreachable (sub.open is not an R op)"),
    ("dispatch.py", "RegistryBackend.status"): (2, "unreachable (host.status is not an R op); would be N's answer"),
    ("dispatch.py", "RegistryBackend.whoami"): (4, "R gets no runner info (P and G only) and the relay caps (R only)"),
    ("outcomes.py", "principal_of"): (2, "raises for R (the principal is r:<device>, never derived from a uid)"),
    ("reads_ws.py", "_person_reader"): (1, "unreachable (session.list, grant.list are not R ops); refuses R"),
    ("reads_ws.py", "reader_list"): (
        2,
        "R: the current devices' levels only ({device, person, reader, level}: no decl, no dk_hash; H12d-2, 9.11)",
    ),
    ("reads_ws.py", "may_create"): (
        2,
        "R: true only for a non-viewer whose requesting device is its person's, valid, with decide; G and N: false",
    ),
    ("reads_ws.py", "member_list"): (1, "R reads as P: device labels (read_ext/3); G and N get label null"),
    ("reads_ws.py", "_next_key"): (1, "R: next_key only where may_create says so (G and N: null)"),
    ("reads_ws.py", "workspace_settings"): (
        4,
        "R reads as P: the full repo entries, settings stamps, Publish addresses (G, N: none); `check` is P only",
    ),
    ("runners.py", "RunnerHub.open_sub"): (3, "unreachable (run.sub is not an R op)"),
    ("runners.py", "RunnerHub.visible"): (2, "R sees no run (only P and G do)"),
    ("runners.py", "serve"): (1, "unreachable (run.list is not an R op); RunnerHub.visible gives R nothing"),
    ("sessions.py", "record"): (2, "R records no session (G only)"),
    ("terms.py", "serve"): (1, "unreachable (term.* are not R ops)"),
    ("runnergrants.py", "_deposit_plan"): (2, "unreachable (grant.deposit is no R op; G only)"),
    ("phonehints.py", "serve"): (
        2,
        "unreachable (phone.request and phone.cancel are not R ops): refused unless column P",
    ),
    ("workdirs.py", "serve"): (
        2,
        "unreachable (workdir.get and workdir.delete are not R ops): refused unless column P",
    ),
    ("reads_phone.py", "_person"): (
        4,
        "R as P for phone.requests only (the viewer's own open hints); phone.status is column P only",
    ),
    ("visibility.py", "Viewer.manager"): (1, "R as P: an owner's or maintainer's device is a manager"),
    ("visibility.py", "Viewer.needs_person"): (2, "R as P: its own person, another one only as an owner"),
    ("visibility.py", "Viewer.of"): (3, "R as P: owner and role from the device's person"),
    ("visibility.py", "Viewer.session_visible"): (2, "R as P (unreachable: session.list is not an R op)"),
    ("workspace.py", "Call.authorize"): (
        4,
        "R without a relay identity is refused first; a socket recompute answering R is refused",
    ),
    ("workspace.py", "Call._authorize_relay"): (
        8,
        "R only with a relay identity, a relay peer of its person and the R column from relay_columns; in R every op "
        "but reader.declare is bound to the device's current rd (H12d-3)",
    ),
    ("workspace.py", "StatusBackend.run"): (2, "unreachable (host.status is not an R op); would be N's answer"),
    ("workspace.py", "WorkspaceGate._column"): (6, "socket only: resolve() raises for a relay peer"),
    ("publishing.py", "_person"): (2, "unreachable (publish.* are not R ops); R is not P, so it would be refused"),
    ("publishing.py", "_prepare"): (4, "unreachable (publish.* are not R ops); R would be no person"),
    ("publishing.py", "facts_for"): (2, "unreachable (publish.share is prepared through human.prepare, P only)"),
    ("publishing.py", "_discard"): (2, "unreachable (publish.* are not R ops)"),
    ("publishing.py", "_list"): (6, "unreachable (publish.list is not an R op); R would see what P sees"),
    ("hubops.py", "_owner"): (2, "unreachable (hub.* are not R ops); R is not P, so it would be refused"),
    ("peerops.py", "_owner"): (2, "unreachable (peer.* are not R ops); R is not P, so it would be refused"),
    ("peerops.py", "_check"): (2, "unreachable (peer.* are not R ops); a relay peer is not column P or G"),
    ("shareops.py", "_owner"): (2, "unreachable (share.* are not R ops); R is not P, so it would be refused"),
    ("spaceops.py", "_owner"): (2, "unreachable (space.* are not R ops); R is not P, so it would be refused"),
    ("spaceops.py", "_check"): (2, "unreachable (space.* are not R ops); a relay peer is not column P or G"),
    ("worktrees.py", "_claim_ok"): (4, "unreachable (worktree.* are not R ops)"),
    ("worktrees.py", "_plan"): (2, "unreachable (worktree.* are not R ops)"),
    ("worktrees.py", "_register"): (4, "unreachable (worktree.* are not R ops)"),
}


def _is_cols(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and node.value in COLS:
        return True
    return (
        isinstance(node, (ast.Set, ast.Tuple, ast.List))
        and bool(node.elts)
        and all(isinstance(e, ast.Constant) and e.value in COLS for e in node.elts)
    )


def _columnish(node: ast.AST) -> bool:
    """``….column``, ``….column.name`` or ``getattr(….column, "name")``."""
    if isinstance(node, ast.Attribute) and node.attr in {"column", "name"}:
        text = ast.unparse(node)
        return text.endswith(".column") or text.endswith("column.name")
    if isinstance(node, ast.Name) and node.id == "column":
        return False  # a parameter or local named column: its comparisons are caught by the literal rule
    return _getattr_name(node)


def _getattr_name(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and ast.unparse(node.args[0]).endswith("column")
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "name"
    )


def _scope(node: ast.AST) -> list[ast.AST]:
    """The nodes of one function (or module) body, lambda bodies included, without nested functions and classes."""
    out: list[ast.AST] = []
    todo = list(ast.iter_child_nodes(node))
    while todo:
        child = todo.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # a lambda's body stays in its enclosing scope (Opus D3)
        out.append(child)
        todo.extend(ast.iter_child_nodes(child))
    return out


def _aliases(nodes: list[ast.AST]) -> set[str]:
    """Local names bound to a column object or name (``col = call.column``, ``c, p = call.column.name, …``)."""
    names: set[str] = set()
    for node in nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and _columnish(node.value):
                names.add(target.id)
            elif isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                for t, v in zip(target.elts, node.value.elts, strict=False):
                    if isinstance(t, ast.Name) and _columnish(v):
                        names.add(t.id)
    return names


def _sites(nodes: list[ast.AST]) -> int:
    aliases = _aliases(nodes)

    def mentions(n: ast.AST) -> bool:
        text = ast.unparse(n)
        return "column" in text or text in aliases or (isinstance(n, ast.Attribute) and ast.unparse(n.value) in aliases)

    count = 0
    for node in nodes:
        if isinstance(node, ast.Compare):
            parts = [node.left, *node.comparators]
            if any(_is_cols(p) for p in parts) and any(mentions(p) for p in parts if not _is_cols(p)):
                count += 1
        elif isinstance(node, ast.Attribute) and node.attr == "name":
            count += ast.unparse(node.value).endswith("column") or ast.unparse(node.value) in aliases
        elif _getattr_name(node):
            count += 1
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            count += _columnish(node.value) and not isinstance(node.value, ast.Attribute)  # getattr alias
        elif isinstance(node, ast.Match) and mentions(node.subject):
            count += len(node.cases)  # every arm of a match on a column is a site
        elif isinstance(node, ast.MatchValue) and _is_cols(node.value):
            count += 1
    return count


def inventory() -> dict[tuple[str, str], int]:
    """Sites per ``(path under orch/, qualified function)``, over ``orch/host/**`` and ``orch/relay/**``."""
    out: dict[tuple[str, str], int] = {}
    roots = [SRC, SRC.parent / "relay"]
    for root in roots:
        for path in sorted(root.rglob("*.py")) if root.is_dir() else []:
            rel = path.relative_to(SRC.parent).as_posix()
            name = rel[len("host/") :] if rel.startswith("host/") else rel
            tree = ast.parse(path.read_text())

            def walk(node: ast.AST, qual: list[str], name: str = name) -> None:
                n = _sites(_scope(node))
                if n:
                    out[(name, ".".join(qual))] = out.get((name, ".".join(qual)), 0) + n
                for child in _children_defs(node):
                    walk(child, [*qual, child.name])

            walk(tree, [])
    return out


def _children_defs(node: ast.AST) -> list[ast.AST]:
    """The functions and classes defined directly in ``node``'s scope (at any statement depth)."""
    out: list[ast.AST] = []
    todo = list(ast.iter_child_nodes(node))
    while todo:
        child = todo.pop(0)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(child)
            continue
        todo.extend(ast.iter_child_nodes(child))
    return out


def test_the_inventory_walks_lambda_bodies():
    """Opus D3: a comparison inside a lambda counts in its enclosing function."""
    tree = ast.parse("def f(call, xs):\n    return sorted(xs, key=lambda c: call.column.name == 'P')\n")
    fn = tree.body[0]
    assert _sites(_scope(fn)) >= 1


def test_the_inventory_catches_getattr_aliases_and_match_arms():
    """Opus L6: the detector sees the indirect forms too."""
    code = """
def f(call):
    col = call.column
    if col.name == "P":
        pass
def g(call):
    if getattr(call.column, "name") == "G":
        pass
def h(call):
    c, p = call.column.name, 1
    return c == "R"
def k(call):
    match call.column.name:
        case "P":
            return 1
        case _:
            return 0
"""
    tree = ast.parse(code)
    found = {fn.name: _sites(_scope(fn)) for fn in tree.body if isinstance(fn, ast.FunctionDef)}
    assert all(found[n] >= 1 for n in "fghk"), found
    assert found["h"] >= 2  # the alias read and its later comparison


def test_every_column_site_is_listed_with_rs_answer():
    found = inventory()
    listed = {k: n for k, (n, _answer) in SITES.items() if n}
    unlisted = {k: n for k, n in found.items() if k not in listed}
    assert not unlisted, f"new column sites: list each with R's answer in SITES: {unlisted}"
    changed = {k: (listed[k], found.get(k, 0)) for k in listed if found.get(k, 0) != listed[k]}
    assert not changed, f"column sites changed (listed, found): re-check R's answer: {changed}"
    for key, (_n, answer) in SITES.items():
        assert answer.strip(), key


# --- R's answers, checked where the site is pure ----------------------------------------------------------------------


class _Auth:
    def __init__(self, owners: set[str], roles: dict[str, str]) -> None:
        self._owners, self.roles = owners, roles

    def owner(self, person: Any) -> bool:
        return person in self._owners


class _Service:
    def __init__(self, auth: Any) -> None:
        self.auth = auth


class _Call:
    def __init__(self, column: Column, viewer: str | None, auth: Any) -> None:
        self.column, self.viewer, self.service = column, viewer, _Service(auth)


@pytest.mark.parametrize("role", ["owner", "maintainer", "member"])
def test_viewer_of_column_r_is_exactly_the_persons_p_view(role):
    from orch.host.visibility import Viewer

    auth = _Auth({PERSON} if role == "owner" else set(), {PERSON: role})
    p = Viewer.of(_Call(Column("P", "person"), PERSON, auth))
    r = Viewer.of(_Call(resolve_relay(True), PERSON, auth))
    assert (r.person, r.owner, r.role, r.manager) == (p.person, p.owner, p.role, p.manager)
    other = "p_" + "9" * 32
    if role == "owner":
        assert r.needs_person(other) == other
    else:
        with pytest.raises(HostError, match="owner"):
            r.needs_person(other)
    assert r.needs_person(None) == PERSON
    assert r.session_visible(PERSON) is True


def test_viewer_of_an_unknown_column_is_no_person():
    from orch.host.visibility import Viewer

    v = Viewer.of(_Call(Column("W", "none"), None, _Auth(set(), {})))
    assert v.owner is False and v.manager is False
    with pytest.raises(HostError):
        v.needs_person(PERSON)
    assert v.session_visible(PERSON) is False


def test_runner_visibility_gives_r_nothing():
    from orch.host.runners import RunnerHub

    rec = {"person": PERSON, "kind": "agent", "ticket": "T-1"}
    assert RunnerHub.visible(None, rec, "R", PERSON) is False  # type: ignore[arg-type]
    assert RunnerHub.visible(None, rec, "P", PERSON) is True  # type: ignore[arg-type]


def test_a_subscription_of_column_r_ends_at_revalidation():
    from orch.host.bus import Bus

    class Sub:
        column, person, uid, workspace_id, grant, grant_id = "R", PERSON, -1, "a" * 32, None, None

    assert Bus.principal_end(None, Sub(), _Auth(set(), {PERSON: "owner"}), 0.0) == "person.removed"  # type: ignore[arg-type]

    class NSub(Sub):
        column = "N"

    assert Bus.principal_end(None, NSub(), _Auth(set(), {}), 0.0) is None  # type: ignore[arg-type]


def test_r_never_gets_a_human_op_or_a_write_kind():
    r = resolve_relay(True)
    for kind in ("human", "human_api", "unattended", "agent", "pg_write", "person_write", "person_read", "pg_read"):
        assert refusal(r, kind, "show") == "op.not_allowed", kind
    assert Verified("absent").grant is None
