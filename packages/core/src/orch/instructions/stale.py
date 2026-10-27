"""The stale check (ticket-format §10.2): installed instructions older than the CLI's generator.

``AGENTS.orch.md`` carries ``instructions rN`` on its first line, a built-in skill carries ``skill_version`` in its
sidecar. A lower number than the installed CLI's, a missing file or a file without a stamp is stale; a higher number
means the files were written by a newer orch and is reported too (the CLI is the one to update). A skill the owner
moved to another scope (``workspace``) or deleted is theirs: it is not checked.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from .agents_md import INSTRUCTIONS_REV, AddonRow, addon_fingerprint, stamp_rev, stamp_set
from .harness import AGENTS_FILE, SKILLS_DIR, UnsafePath, agents_rel, safe_read
from .skills import SIDECAR_NAME, SkillError, builtin_skills, check_sidecar

__all__ = ["stale_findings"]


def _tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def stale_findings(
    root: Path,
    store: Path | None = None,
    addons: Sequence[AddonRow] | None = None,
    worktrees: Sequence[Path | str] = (),
) -> list[dict[str, str]]:
    """``[{"where", "what"}]`` for every installed instruction file that is missing, unstamped, older or newer than
    this CLI's generator; empty when everything is current. Two roots: ``AGENTS.orch.md`` is read from ``store`` (the
    ``.orch`` directory, or the project itself in the legacy layout; ``None``: ``root``), the skills from the project
    ``root`` (``.agents/skills``, ``.claude/skills`` stay where harnesses look for them). ``addons`` is the workspace's
    add-on set (:func:`orch.instructions.addon_set.collect_rows`); when given, a file written for another set (names,
    states, package digests) is stale, and so is a skill folder of the root or of a worktree that is missing or
    left over from an add-on that is not active. ``None``: the set is not known here and is not checked."""
    out: list[dict[str, str]] = []
    try:
        text = safe_read(root, agents_rel(root, store))
    except UnsafePath as e:
        out.append({"where": AGENTS_FILE, "what": str(e).split(": ", 1)[1]})
    except (OSError, UnicodeDecodeError):
        out.append({"where": AGENTS_FILE, "what": "unreadable"})
    else:
        if text is None:
            out.append({"where": AGENTS_FILE, "what": "missing"})
        else:
            rev = stamp_rev(text)
            if rev is None:
                out.append({"where": AGENTS_FILE, "what": "no version stamp"})
            elif rev < INSTRUCTIONS_REV:
                out.append({"where": AGENTS_FILE, "what": f"r{rev}, this orch writes r{INSTRUCTIONS_REV}"})
            elif rev > INSTRUCTIONS_REV:
                out.append({"where": AGENTS_FILE, "what": f"r{rev} is newer than this orch (r{INSTRUCTIONS_REV})"})
            elif addons is not None and stamp_set(text) != addon_fingerprint(addons):
                out.append({"where": AGENTS_FILE, "what": "the add-ons changed since it was written"})
    for skill in builtin_skills():
        where = f"{SKILLS_DIR}/{skill.name}"
        try:
            raw = safe_read(root, f"{where}/{SIDECAR_NAME}")
            if raw is None:
                out.append({"where": where, "what": "no orch.skill.json" if (root / where).exists() else "missing"})
                continue
            have = check_sidecar(json.loads(raw))
        except UnsafePath as e:
            out.append({"where": where, "what": str(e).split(": ", 1)[1]})
            continue
        except (OSError, ValueError, SkillError):
            out.append({"where": where, "what": "sidecar unreadable"})
            continue
        if have["scope"] != "builtin":
            continue  # the owner took it over
        if _tuple(have["skill_version"]) < _tuple(skill.version):
            out.append({"where": where, "what": f"v{have['skill_version']}, this orch ships v{skill.version}"})
        elif _tuple(have["skill_version"]) > _tuple(skill.version):
            out.append({"where": where, "what": f"v{have['skill_version']} is newer than this orch"})
        else:
            try:
                same = safe_read(root, f"{where}/SKILL.md") == skill.text
            except (OSError, UnicodeDecodeError):
                same = False
            if not same:
                out.append(
                    {
                        "where": where,
                        "what": "SKILL.md differs from this orch's (edited by hand? set scope workspace in "
                        "orch.skill.json to keep it, or run orch instructions sync --force)",
                    }
                )
    if addons is not None:
        from .addon_skills import problems

        for base in [root, *(Path(w) for w in worktrees if Path(w).is_absolute() and Path(w).is_dir())]:
            for line in problems(base, addons):
                where, _, what = line.partition(": ")
                out.append({"where": where if base == root else f"{base}/{where}", "what": what})
    return out
