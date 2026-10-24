"""What an add-on's dependencies look like to a person, and how an update changes them (V3 G4, design sections 3 and 4).

Pure functions over the checked :class:`orch.addons.lockfile.Lock` rows (the only source: C2) and the host's plan: no
disk, no network. ``view`` is the block the stage result, the stage file and ``material["deps"]`` carry; ``lines`` are
its review lines; ``diff`` is what ``market.install.diff`` adds for an update (C6: ``widened`` when runtime is added or
changes kind, or any row is added or re-versioned; removals do not widen)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from orch.addons.lockfile import Lock, Planned

from . import constants

__all__ = ["KIND_WORDS", "NOTES", "diff", "lines", "row_name", "view"]

KIND_WORDS = {"python-requirements": "Python", "node-package-lock": "Node"}
#: ``note`` of a row: what a person should know about it (``None``: nothing)
NOTES = ("compiled", "other_platform", "dev")
_NOTE_WORDS = {
    "compiled": "(compiled code)",
    "other_platform": "(not on this computer)",
    "dev": "(development, not installed)",
}


def row_name(row: Any) -> str:
    """The name shown for a row: a Node package nested below another reads ``a > b``."""
    if row.key is None:
        return row.name
    return row.key[len("node_modules/") :].replace("/node_modules/", " > ")


def view(lock_path: str, lock: Lock, planned: Sequence[Planned], found: Mapping[int, tuple[int, bool]] | None) -> dict:
    """``{kind, lock, count, bytes, rows}``. ``found`` maps a row index to ``(bytes, compiled)`` of its verified
    download (``None`` before anything was downloaded: ``bytes`` is then 0)."""
    rows = []
    total = 0
    for i, p in enumerate(planned):
        size, compiled = (found or {}).get(i, (0, False))
        total += size
        note = p.note if p.note is not None else ("compiled" if compiled and p.install else None)
        rows.append({"name": row_name(p.row), "version": p.row.version, "installs": p.install, "note": note})
    return {"kind": lock.kind, "lock": lock_path, "count": len(rows), "bytes": total, "rows": rows}


def lines(v: Mapping[str, Any], show: Any = lambda x: ascii(str(x))[1:-1]) -> list[str]:
    """The review lines of a ``view`` (text from the package is escaped to printable ASCII by ``show``)."""
    n = v["count"]
    out = [
        f"dependencies {KIND_WORDS[v['kind']]} from {show(v['lock'])}: {n} package{'' if n == 1 else 's'}, "
        "installed after you sign; no package scripts run"
    ]
    for r in v["rows"]:
        note = f" {_NOTE_WORDS[r['note']]}" if r["note"] else ""
        out.append(f"dep {show(r['name'])} {show(r['version'])}{note}")
    if v["bytes"]:
        hosts = ", ".join(constants.DEP_HOSTS[v["kind"]])
        out.append(f"download about {max(v['bytes'] / 1e6, 0.1):.1f} MB from {hosts}")
    return out


def _ident(row: Any) -> tuple[str, str]:
    return (row.key or row.name, row.marker or "")


def diff(old_kind: str | None, old: Lock | None, new_kind: str | None, new: Lock | None) -> dict[str, Any]:
    """The dependency part of an update: ``{runtime, added, removed, changed, lines, widened}`` where ``runtime`` is
    ``added``, ``removed``, ``kind_changed`` or ``None``. ``old``/``new`` are ``None`` when that side has no lockfile
    (or the old one cannot be read: every row then counts as new)."""
    runtime = None
    if new_kind is not None and old_kind is None:
        runtime = "added"
    elif new_kind is None and old_kind is not None:
        runtime = "removed"
    elif new_kind != old_kind:
        runtime = "kind_changed"
    old_rows = {_ident(r): r for r in (old.rows if old and old_kind == new_kind else ())}
    new_rows = {_ident(r): r for r in (new.rows if new else ())}
    added = [{"name": row_name(r), "version": r.version} for k, r in new_rows.items() if k not in old_rows]
    removed = [{"name": row_name(r), "version": r.version} for k, r in old_rows.items() if k not in new_rows]
    changed = [
        {"name": row_name(r), "from": old_rows[k].version, "to": r.version}
        for k, r in new_rows.items()
        if k in old_rows and (old_rows[k].version != r.version or old_rows[k].hashes != r.hashes)
    ]
    out = []
    if runtime == "added":
        out.append(f"+ dependencies ({KIND_WORDS[new_kind or '']}, new)")
    elif runtime == "removed":
        out.append(f"- dependencies ({KIND_WORDS[old_kind or '']}, removed)")
    elif runtime == "kind_changed":
        out.append(f"~ dependencies {KIND_WORDS[old_kind or '']} -> {KIND_WORDS[new_kind or '']}")
    out += [f"+ dep {a['name']} {a['version']} (new)" for a in added]
    out += [
        f"~ dep {c['name']} {c['from']} -> {c['to']}" + (" (contents changed)" if c["from"] == c["to"] else "")
        for c in changed
    ]
    out += [f"- dep {x['name']} (removed)" for x in removed]
    return {
        "runtime": runtime, "added": added, "removed": removed, "changed": changed, "lines": out,
        "widened": runtime in ("added", "kind_changed") or bool(added) or bool(changed),
    }  # fmt: skip
