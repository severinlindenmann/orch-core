"""``orch migrate --check | --apply [--workspace PATH] [--json]``: a person's command (never an agent's).

``--check`` lists, per data root, the migration steps that are waiting. ``--apply`` backs up every root it is about to
change (always, first), then migrates it. A workspace that cannot be reached is skipped and listed; the others go on.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from orch.layout import Layout
from orch.ops.errors import ERRORS, OrchError
from orch.ops.runtime import state_dir

from . import backup as backup_mod
from .format import lock_root, read_format
from .journal import restore_pending
from .runner import Plan, apply, plan
from .steps import CURRENT

__all__ = ["main"]

USAGE = "usage: orch migrate --check | --apply [--workspace PATH] [--json]\n"
AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")


def _workspace_roots(env: Mapping[str, str], only: str | None) -> tuple[list[tuple[str, Path, Path]], list[str]]:
    """(label, project, store) per workspace to look at, and notes on registry problems."""
    if only is not None:
        project = Path(os.path.abspath(only))
        return [(str(project), project, Layout.new_at(project).store)], []
    notes: list[str] = []
    out: list[tuple[str, Path, Path]] = []
    try:
        from orch.host.custody import read_json
        from orch.host.registry import registry_path

        doc = read_json(registry_path(state_dir(env)), os.getuid()) or {}
        for w in doc.get("workspaces") or []:
            if isinstance(w, dict) and isinstance(w.get("root"), str):
                project = Path(w["root"])
                out.append((str(project), project, Layout.new_at(project).store))
    except Exception as exc:  # noqa: BLE001 - an unreadable list must not hide the host's own data
        notes.append(f"the list of workspaces could not be read ({exc}); use --workspace PATH")
    return out, notes


def _part_roots(env: Mapping[str, str]) -> tuple[list[tuple[str, str, Path]], list[str]]:
    """(label, kind, data dir) of every installed part (hub, relay, publish) whose data dir exists AND has a
    ``format.json``, and notes. The kind is the part's name. A data dir with no ``format.json`` (installed before parts
    had a format) is no root while the code is at the baseline format: it is never migrated or backed up here, and a
    note says how to mark it. Once the code's format is past the baseline, unmarked data is refused (OrchError), never
    skipped. (A damaged ``format.json`` stays a root: it is refused, never skipped silently.)"""
    from orch.install.cli import ADOPT_BASELINE_ONLY_AT
    from orch.install.parts import PARTS
    from orch.install.state import Layout as InstallLayout
    from orch.install.state import load

    layout = InstallLayout(env)
    installed = load(layout).get("parts") or {}
    out: list[tuple[str, str, Path]] = []
    notes: list[str] = []
    for part in PARTS:
        data = layout.data(part)
        if part not in installed or not data.is_dir():
            continue
        try:
            marked = read_format(data, part) is not None
        except OrchError:
            marked = True
        if marked:
            out.append((f"the {part}'s data", part, data))
        elif CURRENT[part] != ADOPT_BASELINE_ONLY_AT and any(data.iterdir()):
            # the code's format is past the baseline: this data could be at any older shape, and a later
            # `orch install` refuses it too. Never run the new code on it unmigrated: refuse (before the flip, or the
            # update rolls back)
            raise OrchError(
                "format.missing",
                f"the {part}'s data has no format.json, so it cannot be migrated: "
                "restore it from a backup or remove it",
            )
        else:
            notes.append(f"the {part}'s data has no format yet: run `orch install {part}` again")
    return out, notes


def _may_be_committed(project: Path) -> bool:
    """Could the store folder be committed to git? Decided WITHOUT running git (a project's own git config can run a
    program: fsmonitor, hooks): true when the project is a git checkout and neither ``.gitignore`` nor
    ``.git/info/exclude`` ignores the store folder."""
    git = project / ".git"
    if not git.exists():
        return False
    name = Layout.new_at(project).store.name
    ignored = {name, f"/{name}", f"{name}/", f"/{name}/"}
    for f in (project / ".gitignore", git / "info" / "exclude"):
        try:
            with open(f, "rb") as h:
                text = h.read(1 << 20).decode("utf-8", "replace")
        except OSError:
            continue
        if any(line.strip() in ignored for line in text.splitlines()):
            return False
    return True


def _describe(p: Plan) -> dict[str, Any]:
    return {"at": p.at, "target": p.target, "steps": [s.name for s in p.steps]}


def main(argv: Sequence[str], *, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    as_json = "--json" in argv
    if any(env.get(k) for k in AGENT_ENV):
        spec = ERRORS["migrate.actor_refused"]
        if as_json:
            out.write(json.dumps({"ok": False, "code": spec.code, "message": spec.message}) + "\n")
        else:
            err.write(f"orch migrate: {spec.message}\n")
        return spec.exit
    args = list(argv)
    mode = "--check" if "--check" in args else "--apply" if "--apply" in args else None
    only: str | None = None
    rest = [a for a in args if a not in ("--check", "--apply", "--json")]
    if "--workspace" in rest:
        i = rest.index("--workspace")
        if i + 1 >= len(rest):
            mode = None
        else:
            only = rest[i + 1]
            del rest[i : i + 2]
    if mode is None or rest or ("--check" in args and "--apply" in args):
        err.write(USAGE)
        return 2

    targets: list[tuple[str, str, Path]] = []  # (label, kind, root)
    skipped: list[dict[str, str]] = []
    projects: dict[str, Path] = {}
    wss, notes = _workspace_roots(env, only)
    if only is None:
        targets.append(("this computer's orch data", "host", state_dir(env)))
        try:
            part_roots, part_notes = _part_roots(env)
        except OrchError as exc:  # unmarked part data the code cannot adopt: nothing is migrated, in any root
            spec = ERRORS[exc.code]
            if as_json:
                out.write(json.dumps({"ok": False, "code": exc.code, "message": exc.message}) + "\n")
            else:
                err.write(f"orch migrate: {exc.message}\n")
            return spec.exit
        targets.extend(part_roots)
        notes.extend(part_notes)
    for label, project, store in wss:
        targets.append((label, "workspace", store))
        projects[label] = project
    result: list[dict[str, Any]] = []
    warnings = list(notes)
    code = 0
    for label, kind, root in targets:
        try:
            pending = restore_pending(root)
            if pending and mode == "--apply":
                backup_mod.recover_interrupted(root, warnings.append)
            elif pending:
                raise OrchError("update.interrupted", pending)
            p = plan(root, kind)
        except OrchError as exc:
            if exc.code in (
                "migrate.backup_refused",
                "update.interrupted",
                "migrate.running",
            ):  # a half-done restore: never "skipped, ok"
                row = {"root": label, "kind": kind, "error": {"code": exc.code, "message": exc.message}}
                result.append(row)
                code = max(code, ERRORS[exc.code].exit)
                continue
            skipped.append({"root": label, "why": exc.message})
            continue
        except OSError as exc:
            skipped.append({"root": label, "why": exc.message if isinstance(exc, OrchError) else str(exc)})
            continue
        row: dict[str, Any] = {"root": label, "kind": kind, **_describe(p)}
        if mode == "--apply" and p.pending:
            fd = None
            try:
                fd = lock_root(root, exclusive=True)  # ONE exclusive lock for the backup and the steps
                p = plan(root, kind)  # re-read under the lock
                row.update(_describe(p))
                b = backup_mod.create_backup(root, kind, p.at, p.target, env)
                row["backup"] = str(b)
                row["applied"] = apply(root, kind, lock_fd=fd)
                backup_mod.prune_backups(env, root, pin_from=p.at)  # only after success
            except OrchError as exc:
                row["error"] = {"code": exc.code, "message": exc.message}
                code = max(code, ERRORS[exc.code].exit if exc.code in ERRORS else 1)
            except Exception as exc:  # noqa: BLE001 - one root failing is reported, never a traceback
                row["error"] = {"code": "migrate.step_failed", "message": f"{exc}"}
                code = max(code, ERRORS["migrate.step_failed"].exit)
            if "error" in row and "backup" in row and fd is not None:
                try:  # a failed step: put the root back as it was, while we still hold the lock
                    backup_mod.restore(
                        Path(row["backup"]), root, env, own_failure=True, lock_fd=fd, say=warnings.append
                    )
                    row["restored"] = True
                except Exception as exc:  # noqa: BLE001
                    row["restore_error"] = str(exc)
            if fd is not None:
                os.close(fd)
        result.append(row)
        project = projects.get(label)
        if project is not None and _may_be_committed(project):
            warnings.append(
                f"{label}: the {Layout.new_at(project).store.name} folder is not ignored by git; "
                "it holds signed data and must never be committed: add it to .gitignore"
            )

    if as_json:
        out.write(json.dumps({"ok": code == 0, "roots": result, "skipped": skipped, "warnings": warnings}) + "\n")
        return code
    for row in result:
        if "error" in row:
            out.write(f"{row['root']}: could not migrate ({row['error']['message']}).")
            if row.get("restored"):
                out.write(f" Your data was put back as it was (backup: {row['backup']}).")
            elif "backup" in row:
                out.write(f" Your data was backed up first: {row['backup']}")
            if "restore_error" in row:
                out.write(f" Putting it back failed too ({row['restore_error']}).")
            out.write("\n")
        elif row.get("applied"):
            out.write(f"{row['root']}: migrated ({', '.join(row['applied'])}). Backup: {row['backup']}\n")
        elif row["steps"]:
            out.write(f"{row['root']}: {len(row['steps'])} step(s) waiting ({', '.join(row['steps'])})\n")
        else:
            out.write(f"{row['root']}: up to date (format {row['at']})\n")
    for s in skipped:
        out.write(f"skipped {s['root']}: {s['why']}\n")
    for w in warnings:
        out.write(f"warning: {w}\n")
    return code
