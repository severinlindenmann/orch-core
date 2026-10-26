"""What an add-on's dependencies look like to a person, and how an update changes them (V3 G4, design sections 3 and 4).

Pure functions over the checked :class:`orch.addons.lockfile.Lock` rows (the only source: C2) and the host's plan: no
disk, no network. ``view`` is the block the stage result, the stage file and ``material["deps"]`` carry; ``lines`` are
its review lines; ``diff`` is what ``market.install.diff`` adds for an update (C6: ``widened`` when runtime is added or
changes kind, or any row is added or re-versioned; removals do not widen)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from orch.addons.lockfile import Lock, Planned

from . import constants

__all__ = ["KIND_WORDS", "NOTES", "diff", "lines", "row_name", "view"]

#: the characters of a diff line (the signer and the stage file apply the same one)
DIFF_LINE = re.compile(r"[A-Za-z0-9@._~/ >+()!,:-]{1,300}")
MAX_DIFF_LINES = 2 * 1000 + 1  # at most 1000 rows per lock: every old and new row, plus the runtime line
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


def diff_ok(d: Any) -> bool:
    """The update diff a stage carries to the signer: ``{lines: [short plain strings], widened: bool}``."""
    return (
        isinstance(d, dict)
        and set(d) == {"lines", "widened"}
        and type(d["widened"]) is bool
        and isinstance(d["lines"], list)
        and len(d["lines"]) <= MAX_DIFF_LINES
        and all(isinstance(x, str) and DIFF_LINE.fullmatch(x) for x in d["lines"])
    )


def consistent(kept: Any, fresh: Mapping[str, Any]) -> bool:
    """Is ``kept`` (the block a stage file holds: it also knows the download size and which wheels are compiled) the
    block recomputed now from the lockfile of this package? Names, versions, install flags, kind, lock and count must be
    equal; a note must be the plan's, or ``compiled`` on a row that installs (the one fact the plan cannot know);
    ``bytes`` must be a plain size within the download cap. Anything else is not used."""
    from .download import MAX_TOTAL

    if not isinstance(kept, dict) or set(kept) - {"diff"} != set(fresh):
        return False
    if "diff" in kept and not diff_ok(kept["diff"]):
        return False
    rows = kept["rows"]
    if (
        not isinstance(rows, list)
        or (kept["kind"], kept["lock"], kept["count"]) != (fresh["kind"], fresh["lock"], fresh["count"])
        or len(rows) != len(fresh["rows"])
        or type(kept["bytes"]) is not int
        or not 0 <= kept["bytes"] <= MAX_TOTAL
    ):
        return False
    for k, f in zip(rows, fresh["rows"], strict=True):
        if not isinstance(k, dict) or set(k) != set(f):
            return False
        if (k["name"], k["version"], k["installs"]) != (f["name"], f["version"], f["installs"]):
            return False
        if k["note"] != f["note"] and not (f["note"] is None and k["note"] == "compiled" and f["installs"]):
            return False
    return True


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


def _shape(p: Planned) -> tuple[Any, ...]:
    """What decides whether a package is installed on this computer: the row's flags and the plan's verdict."""
    r = p.row
    return (p.install, p.note, r.dev, r.optional, r.os, r.cpu, r.marker)


def diff(
    old_kind: str | None, old: Sequence[Planned] | None, new_kind: str | None, new: Sequence[Planned] | None
) -> dict[str, Any]:
    """The dependency part of an update: ``{runtime, added, removed, changed, lines, widened}`` where ``runtime`` is
    ``added``, ``removed``, ``kind_changed`` or ``None``. ``old``/``new`` are the PLANNED rows for this computer
    (``None`` when that side has no lockfile, or the old one cannot be read: every row then counts as new). A row is
    ``changed`` when its version or hash differs, or its install status or flags do (dev, optional, os, cpu, marker),
    so a package that keeps its version but becomes installed here is never unchanged (C6). ``widened``: runtime added
    or kind changed, any row added or changed; removals do not widen."""
    runtime = None
    if new_kind is not None and old_kind is None:
        runtime = "added"
    elif new_kind is None and old_kind is not None:
        runtime = "removed"
    elif new_kind != old_kind:
        runtime = "kind_changed"
    old_rows = {_ident(p.row): p for p in (old if old and old_kind == new_kind else ())}
    new_rows = {_ident(p.row): p for p in (new or ())}
    added = [{"name": row_name(p.row), "version": p.row.version} for k, p in new_rows.items() if k not in old_rows]
    removed = [{"name": row_name(p.row), "version": p.row.version} for k, p in old_rows.items() if k not in new_rows]
    pairs = []  # (identity, old, new): every line below comes from these, never from a second lookup by name
    for k, p in new_rows.items():
        o = old_rows.get(k)
        if o is not None and (o.row.version != p.row.version or o.row.hashes != p.row.hashes or _shape(o) != _shape(p)):
            pairs.append((k, o, p))
    changed = [{"name": row_name(p.row), "from": o.row.version, "to": p.row.version} for _, o, p in pairs]
    out = []
    if runtime == "added":
        out.append(f"+ dependencies ({KIND_WORDS[new_kind or '']}, new)")
    elif runtime == "removed":
        out.append(f"- dependencies ({KIND_WORDS[old_kind or '']}, removed)")
    elif runtime == "kind_changed":
        out.append(f"~ dependencies {KIND_WORDS[old_kind or '']} -> {KIND_WORDS[new_kind or '']}")
    # changed rows first, a same-version change (contents or install status) before a re-version, then added, removed
    for _, o, p in sorted(pairs, key=lambda t: t[1].row.version != t[2].row.version):
        if o.row.version != p.row.version:
            suffix = ""
        elif o.row.hashes != p.row.hashes:
            suffix = " (contents changed)"
        else:
            suffix = " (install status changed)"
        out.append(f"~ dep {row_name(p.row)} {o.row.version} -> {p.row.version}{suffix}")
    out += [f"+ dep {a['name']} {a['version']} (new)" for a in added]
    out += [f"- dep {x['name']} (removed)" for x in removed]
    return {
        "runtime": runtime, "added": added, "removed": removed, "changed": changed, "lines": out,
        "widened": runtime in ("added", "kind_changed") or bool(added) or bool(changed),
    }  # fmt: skip
