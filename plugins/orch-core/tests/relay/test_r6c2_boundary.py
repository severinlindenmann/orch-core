"""R6c-2, the hard rule of the Opus review of #588 by AST: the host's settlement paths of step 9 (``settle``,
``committed``, ``refused``, ``recover``, ``adopt_existing``, ``precommit``) are called only where the design puts
them, never through the pair thread, the internal API, a control op or the CLI.

Scope: every module of ``orch`` that names the pairing (``relaypair``, ``orch.relay.r6.pair``, ``PairEngine``,
``PairThread``, ``R6PairDeviceSource``, ``pairdevice``) . In those, each call ``<x>.<name>(...)`` of a settlement
name is listed with its module and enclosing function, and the list must be exactly :data:`ALLOWED`."""

from __future__ import annotations

import ast
from pathlib import Path

import orch

ROOT = Path(orch.__file__).resolve().parent
NAMES = {"settle", "committed", "refused", "recover", "adopt_existing", "precommit"}
MARKERS = ("relaypair", "r6.pair", "PairEngine", "PairThread", "R6PairDeviceSource", "pairdevice")
ALLOWED = {
    # the append choke point's step-9 hooks (H12c-e) call the source
    ("host/pairdevice.py", "hooks.before", "precommit"),
    ("host/pairdevice.py", "hooks.before", "refused"),
    ("host/pairdevice.py", "hooks.after", "committed"),
    ("host/pairdevice.py", "hooks.failed", "refused"),
    # the source forwards to the engine, under the store lock
    ("host/relaypair.py", "R6PairDeviceSource.precommit", "precommit"),
    ("host/relaypair.py", "R6PairDeviceSource.committed", "committed"),
    ("host/relaypair.py", "R6PairDeviceSource.refused", "refused"),
    ("host/relaypair.py", "R6PairDeviceSource.allocate_existing", "adopt_existing"),
    # open-time recovery
    ("host/relaypair.py", "register", "recover"),
    # not pairing: the outcome store's own settle of a human request (human.py names pairdevice for its hooks)
    ("host/human.py", "roll_forward", "settle"),
    # the engine's own settlement (the tick through the host's store lock; adopt_existing completes by committed)
    ("relay/r6/pair.py", "PairEngine.adopt_existing", "committed"),
    ("relay/r6/pair.py", "PairEngine.tick", "settle"),
}
# the 9A path and the ops never name a settlement path at all
SURFACES = [("host/relaypair.py", "api"), ("host/control.py", "ControlOps.relay_pair"), ("relay/r6/pair.py",
            "PairThread"), ("relay/r6/pair.py", "run_api"), ("relay/r6/pair.py", "PairEngine.api_table")]  # fmt: skip


def _sites() -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for path in sorted(ROOT.rglob("*.py")):
        text = path.read_text()
        rel = str(path.relative_to(ROOT))
        if not any(m in text for m in MARKERS):
            continue

        def visit(node: ast.AST, ctx: list[str], rel: str = rel) -> None:
            for ch in ast.iter_child_nodes(node):
                c = ctx + [ch.name] if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ctx
                if isinstance(ch, ast.Call) and isinstance(ch.func, ast.Attribute) and ch.func.attr in NAMES:
                    out.add((rel, ".".join(c), ch.func.attr))
                visit(ch, c, rel)

        visit(ast.parse(text), [])
    return out


def test_settlement_paths_are_called_only_where_the_design_puts_them():
    assert _sites() == ALLOWED


def _node(rel: str, qual: str) -> ast.AST:
    tree = ast.parse((ROOT / rel).read_text())
    node: ast.AST = tree
    for part in qual.split("."):
        node = next(n for n in ast.iter_child_nodes(node) if getattr(n, "name", None) == part)
    return node


def test_the_api_the_thread_and_the_ops_never_name_a_settlement_path():
    for rel, qual in SURFACES:
        node = _node(rel, qual)
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute):
                assert n.attr not in NAMES, (rel, qual, n.attr)
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                assert n.value not in NAMES, (rel, qual, n.value)


def test_the_cli_never_names_a_settlement_path():
    tree = ast.parse((ROOT / "host" / "cli.py").read_text())
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            assert n.attr not in NAMES, n.attr


# -- Opus review of #589, L3: attributes and strings too (getattr, aliases), paircli, and PairThread._call -------------


#: the pairing modules proper (the wider scan: every attribute and string constant, not only calls)
PAIRING_MODULES = ("relay/r6/pair.py", "relay/r6/pairstore.py", "relay/r6/pairrelay.py", "host/relaypair.py",
                   "host/paircli.py", "host/pairdevice.py")  # fmt: skip


#: the string "refused" as a PairRelayError kind (a relay's definite refusal), not the settlement method
RELAY_KIND = {
    ("relay/r6/pair.py", "PairEngine._post_request", "refused"),
    ("relay/r6/pair.py", "PairEngine.publish", "refused"),
    ("relay/r6/pairrelay.py", "SessionPairRelay._call", "refused"),
    ("relay/r6/pairrelay.py", "SessionPairRelay.post_baseline", "refused"),
    ("relay/r6/pairrelay.py", "_id", "refused"),
}


def _all_mentions() -> set[tuple[str, str, str]]:
    """Every ``x.<name>`` (called or not: an alias ``f = e.settle`` counts) and every string constant equal to a
    settlement name (``getattr(e, "settle")``), in the pairing modules, with its module and enclosing function."""
    out: set[tuple[str, str, str]] = set()
    for rel in PAIRING_MODULES:
        text = (ROOT / rel).read_text()

        def visit(node: ast.AST, ctx: list[str], rel: str = rel) -> None:
            for ch in ast.iter_child_nodes(node):
                c = ctx + [ch.name] if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ctx
                if isinstance(ch, ast.Attribute) and ch.attr in NAMES:
                    out.add((rel, ".".join(c), ch.attr))
                if isinstance(ch, ast.Constant) and isinstance(ch.value, str) and ch.value in NAMES:
                    out.add((rel, ".".join(c), ch.value))
                visit(ch, c, rel)

        visit(ast.parse(text), [])
    return out


def test_no_alias_or_dynamic_lookup_of_a_settlement_path():
    assert _all_mentions() == {a for a in ALLOWED if a[0] in PAIRING_MODULES} | RELAY_KIND


def test_paircli_never_names_a_settlement_path():
    tree = ast.parse((ROOT / "host" / "paircli.py").read_text())
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            assert n.attr not in NAMES and n.attr not in ("_call", "api_table", "engine"), n.attr
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            assert n.value not in NAMES, n.value


def test_pair_thread_call_is_used_only_by_pair_thread_api():
    for rel in ("relay/r6/pair.py", "host/relaypair.py", "host/control.py", "host/paircli.py"):

        def visit(node: ast.AST, ctx: list[str], rel: str = rel) -> None:
            for ch in ast.iter_child_nodes(node):
                c = ctx + [ch.name] if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ctx
                if isinstance(ch, ast.Attribute) and ch.attr == "_call":
                    assert (rel, ".".join(c)) == ("relay/r6/pair.py", "PairThread.api"), (rel, c)
                if isinstance(ch, ast.Constant) and ch.value == "_call":
                    raise AssertionError((rel, c))
                visit(ch, c, rel)

        visit(ast.parse((ROOT / rel).read_text()), [])
