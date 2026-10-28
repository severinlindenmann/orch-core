"""H12d-2, the append-path table (orch-v2-host §0.8.6 item 9.4): every call in ``orch`` that appends an event to a log
(``<…>.append(…, log=…)``) is listed here with how invariant I holds on it. A new append path fails this test until
it is listed: either it runs ``check_readers`` at the append, or the table says why it cannot add a reader or a
convenience event. The inventory is syntactic (AST), counted per function."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "orch"

#: (file, function) -> (calls, how invariant I holds there)
PATHS: dict[tuple[str, str], tuple[int, str]] = {
    ("host/human.py", "append_signed"): (
        1,
        "checked: _readers_refusal under the store lock before the first append (human.finish, relay.submit, R6)",
    ),
    ("ops/human.py", "Human._sign_and_append"): (
        1,
        "checked: direct mode's precommit Human._readers_check (check_readers, then the records, then the mark)",
    ),
    ("host/control.py", "ControlOps._run_bootstrap"): (
        1,
        "exempt: the owner-signed genesis of host.bootstrap; no history yet, required_reader is 0",
    ),
    ("ops/runtime.py", "Projection.commit"): (
        1,
        "exempt: agent and unattended events (columns G and N); never a person event, never a roster event or auth",
    ),
    ("ops/import_run.py", "Importer._append"): (
        1,
        "exempt: orch import v1 builds a new workspace from v1 history, which has no convenience events",
    ),
    ("host/hubdeliver.py", "StoreLanding.land"): (
        4,
        "exempt: a hub-signed event is a custodial type only (authz.HUB_TYPES: never member, role or device add, "
        "never grant.issued) with auth hub, so it adds no reader and no convenience event; the other calls are the "
        "host's own hub.pinned ledger line and the host-actor grant.issued that answers an admitted grant.requested "
        "(the model admits it only with that request: a grant adds no reader and no convenience event), and the "
        "host-actor addon.granted that answers an admitted addon.requested (FB15: the model admits it only with that "
        "request, once per hand-off; an add-on grant adds no reader and no convenience event)",
    ),
    ("host/schedules.py", "ScheduleRunner._create_child"): (
        1,
        "exempt: the person-actor ticket.created of a new-ticket schedule's child, written by core after the host's own "
        "schedule.fired{child} (deterministic uid, recovery never mints twice); the model admits it only against the "
        "live signed schedule.set (via), re-checks the person's role and device, and it adds no reader and no "
        "convenience event",
    ),
    ("ops/human.py", "Human.run_many"): (
        1,
        "exempt: the signed schedule.add of a new-ticket schedule writes the anchor ticket.created and the schedule.set "
        "in one human action; both are person-signed and the model admits the pair together",
    ),
    ("host/followups.py", "FollowupRunner._append"): (
        1,
        "exempt: the host-actor followup.fired and followup.outcome lines; they add no reader and no convenience event",
    ),
}


def _sites() -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("store/"):
            continue  # the store's own append, and its callers within the store
        tree = ast.parse(path.read_text())

        def walk(node: ast.AST, scope: list[str], rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    walk(child, [*scope, child.name])
                    continue
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "append"
                    and any(k.arg == "log" for k in child.keywords)
                ):
                    key = (rel, ".".join(scope))
                    out[key] = out.get(key, 0) + 1
                walk(child, scope)

        walk(tree, [])
    return out


def test_every_append_path_is_listed_with_how_invariant_i_holds():
    found = _sites()
    listed = {k: v[0] for k, v in PATHS.items()}
    assert found == listed, {"new or changed": {k: v for k, v in found.items() if listed.get(k) != v}}
