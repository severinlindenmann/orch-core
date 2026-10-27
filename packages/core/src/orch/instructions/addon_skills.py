"""Add-on skills on disk (design addon-skills D2): the files follow the log.

For every **active** add-on with a skill, ``.agents/skills/orch-addon-<name>/`` (the canonical copy) and the generated
``.claude/skills`` copy hold ``SKILL.md`` and the sidecar ``orch.skill.json``
(``{scope: "addon", addon, version, package_sha256, text_sha256}``), in the workspace root and in every live worktree
the caller names. Folders of add-ons that are not active are removed when their text still hashes to the sidecar, and
moved out of discovery to ``.orch/removed-skills/<name>-<ts>/`` when somebody edited them. A folder core did not write
(no sidecar, another scope, another add-on) is never touched. Every path goes through the same no-link checks as the
rest of the harness files (:class:`~orch.instructions.harness.UnsafePath`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from orch.layout import STORE_DIR

from .agents_md import AddonRow
from .harness import (
    CLAUDE_SKILLS_DIR,
    SKILLS_DIR,
    UnsafePath,
    _check_chain,
    _ensure_line,
    _with_header,
    _write,
    safe_read,
)
from .skills import SIDECAR_NAME, SKILL_NAME, SkillError, check_sidecar

__all__ = ["IGNORE_LINES", "PREFIX", "reconcile", "skill_rels", "wanted"]

PREFIX = "orch-addon-"
#: the lines of the ``.gitignore`` of the workspace: generated skills are never committed
IGNORE_LINES = (f"{SKILLS_DIR}/{PREFIX}*/", f"{CLAUDE_SKILLS_DIR}/{PREFIX}*/")
_ROOTS = (SKILLS_DIR, CLAUDE_SKILLS_DIR)


@dataclass(frozen=True)
class Wanted:
    row: AddonRow
    text: str


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def wanted(rows: Sequence[AddonRow]) -> dict[str, Wanted]:
    """``{skill folder name: Wanted}`` for every active add-on whose package has a skill."""
    out: dict[str, Wanted] = {}
    for row in rows:
        if row.active and row.skill is not None:
            try:
                out[row.skill_name] = Wanted(row, row.skill.decode("utf-8"))
            except UnicodeDecodeError:
                continue
    return out


def skill_rels(rows: Sequence[AddonRow]) -> list[str]:
    """The paths below a project root the skills of ``rows`` are written to (for the pre-write safety check)."""
    return [f"{base}/{name}/{leaf}" for name in wanted(rows) for base in _ROOTS for leaf in (SKILL_NAME, SIDECAR_NAME)]


def _copy_text(base: str, text: str) -> str:
    return _with_header(text) if base == CLAUDE_SKILLS_DIR else text


def _sidecar(w: Wanted, written: str) -> str:
    doc = {
        "scope": "addon", "addon": w.row.name, "version": w.row.version,
        "package_sha256": w.row.digest, "text_sha256": _sha(written),
    }  # fmt: skip
    return json.dumps(doc, sort_keys=True) + "\n"


def _ours(base: Path, folder: str) -> dict[str, str] | None:
    """The sidecar of a folder core wrote (scope ``addon`` and the folder's own add-on), else ``None``."""
    try:
        raw = safe_read(base, f"{folder}/{SIDECAR_NAME}")
        if raw is None:
            return None
        doc = check_sidecar(json.loads(raw))
    except (OSError, ValueError, SkillError):  # UnsafePath is an OSError: a link in there is not ours either
        return None
    if doc["scope"] != "addon" or PREFIX + doc["addon"] != folder.rsplit("/", 1)[1]:
        return None
    return doc


def _listing(base: Path, root_rel: str) -> list[str]:
    """Names of ``orch-addon-*`` entries below ``base / root_rel`` ([] when it does not exist)."""
    path = _check_chain(base, root_rel)
    try:
        return sorted(n for n in os.listdir(path) if n.startswith(PREFIX))
    except (FileNotFoundError, NotADirectoryError):
        return []


def _quarantine(root: Path, store: Path | None, base: Path, folder: str, now: float) -> str:
    """Move ``base / folder`` to ``<store>/removed-skills/<name>-<ts>`` below the workspace ``root``; the new path."""
    store_rel = store.relative_to(root).as_posix() if store is not None and store != root else STORE_DIR
    parent_rel = f"{store_rel}/removed-skills"
    cur = root
    for part in Path(parent_rel).parts:  # make the missing directories one by one, each checked after it exists
        cur = cur / part
        if not cur.exists():
            cur.mkdir()
        _check_chain(root, str(cur.relative_to(root)))
    name = folder.rsplit("/", 1)[1]
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    dest_rel, n = f"{parent_rel}/{name}-{stamp}", 1
    while os.path.lexists(root / dest_rel):
        n += 1
        dest_rel = f"{parent_rel}/{name}-{stamp}-{n}"
    _check_chain(base, folder)
    _check_chain(root, dest_rel)
    os.rename(base / folder, root / dest_rel)
    return dest_rel


def _remove(base: Path, folder: str) -> None:
    for leaf in (SKILL_NAME, SIDECAR_NAME):
        _check_chain(base, f"{folder}/{leaf}")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(base / folder / leaf)
    with contextlib.suppress(OSError):
        os.rmdir(base / folder)


def _reconcile_one(
    root: Path,
    store: Path | None,
    base: Path,
    want: dict[str, Wanted],
    shown: str,
    *,
    force: bool,
    dry_run: bool,
    now: float,
) -> tuple[list[str], list[str]]:
    changed: list[str] = []
    lines: list[str] = []

    def at(rel: str) -> str:
        return rel if base == root else f"{shown}/{rel}"

    for root_rel in _ROOTS:
        for name in _listing(base, root_rel):
            folder = f"{root_rel}/{name}"
            side = _ours(base, folder)
            if side is None or name in want:
                continue
            try:
                current = safe_read(base, f"{folder}/{SKILL_NAME}")
            except UnicodeDecodeError:
                current = None
            entries = set(os.listdir(base / folder))
            untouched = (
                current is not None and _sha(current) == side["text_sha256"] and entries <= {SKILL_NAME, SIDECAR_NAME}
            )
            if dry_run:
                lines.append(f"would {'remove' if untouched else 'move'} {at(folder)}")
            elif untouched:
                _remove(base, folder)
                lines.append(f"removed {at(folder)}")
            else:
                try:
                    dest = _quarantine(root, store, base, folder, now)
                except OSError as e:
                    lines.append(f"kept {at(folder)} (edited by hand, could not move it: {e.strerror or 'error'})")
                else:
                    lines.append(f"moved {at(folder)} (edited by hand) to {dest}")
        for name, w in sorted(want.items()):
            folder = f"{root_rel}/{name}"
            text = _copy_text(root_rel, w.text)
            exists = os.path.lexists(_check_chain(base, folder))
            if exists:
                side = _ours(base, folder)
                if side is None:
                    lines.append(f"kept {at(folder)} (not orch's: no add-on sidecar)")
                    continue
                if side["addon"] != w.row.name:
                    lines.append(f"kept {at(folder)} (belongs to another add-on)")
                    continue
                try:
                    current = safe_read(base, f"{folder}/{SKILL_NAME}")
                except UnicodeDecodeError:
                    current = None
                if current is not None and _sha(current) != side["text_sha256"] and not force:
                    lines.append(f"kept {at(folder)} (edited by hand; --force overwrites it)")
                    continue
            for leaf, content in ((SKILL_NAME, text), (SIDECAR_NAME, _sidecar(w, text))):
                rel = f"{folder}/{leaf}"
                if dry_run:
                    if safe_read(base, rel) != content:
                        changed.append(at(rel))
                elif _write(base, rel, content):
                    changed.append(at(rel))
    return changed, lines


def reconcile(
    root: Path,
    rows: Sequence[AddonRow],
    *,
    store: Path | None = None,
    worktrees: Sequence[Path | str] = (),
    force: bool = False,
    dry_run: bool = False,
    now: float | None = None,
) -> tuple[list[str], list[str]]:
    """Bring the add-on skills of the workspace ``root`` and of each live worktree in line with ``rows``. Returns
    ``(changed, lines)``: the paths written (or that would be) and a line for each folder removed, moved to the
    quarantine or kept (and why). An unsafe path in ``root`` is :class:`UnsafePath` before that root is changed; a
    problem in a worktree is a line and the other worktrees are still done."""
    when = time.time() if now is None else now
    want = wanted(rows)
    changed: list[str] = []
    lines: list[str] = []
    if want and not dry_run:
        if any([_ensure_line(root, ".gitignore", line) for line in IGNORE_LINES]):
            changed.append(".gitignore")
    c, ln = _reconcile_one(root, store, root, want, str(root), force=force, dry_run=dry_run, now=when)
    changed += c
    lines += ln
    seen = {os.path.realpath(root)}
    for wt in worktrees:
        base = Path(wt)
        if not base.is_absolute() or base.is_symlink() or not base.is_dir():
            lines.append(f"skipped worktree {base} (not a plain directory)")
            continue
        real = os.path.realpath(base)
        if real in seen:
            continue
        seen.add(real)
        try:
            c, ln = _reconcile_one(root, store, base, want, str(base), force=force, dry_run=dry_run, now=when)
        except (UnsafePath, OSError) as e:
            lines.append(f"could not update worktree {base}: {e}")
            continue
        changed += c
        lines += ln
    return changed, lines
