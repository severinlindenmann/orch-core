"""Workspace renderers: what an agent finds in the workspace under each condition.

All three start from the REAL files of this checkout (`orch.instructions.harness.workspace_files`: AGENTS.orch.md and the
built-in skills) and differ only in the add-on part:

* `today`           what `orch instructions sync` writes today: no add-on line at all.
* `today-oneliners` `render_agents_md(extra=[- addon <name> (hint, grants nothing): <agents_md>])`, the ticket-format
                    §8.1 behaviour that sync does not use yet. Over the 40-line cap the real render raises; the eval
                    then inserts the lines anyway and flags `over_cap` (what the model would see with the cap lifted).
* `proposed`        design D3: the "Add-ons here" block after the core text plus one skill per add-on (style V2).
                    Built here until core has the renderer; swap `proposed_block` for it then.
"""
from __future__ import annotations

import addons as A

RENDERERS = ("today", "today-oneliners", "proposed")
BLOCK_MAX_LINES = 16
BLOCK_MAX_CHARS = 3000
BLOCK_MAX_ACTIVE = 12

OFF_LINE = ("Off here: {off} (disabled). If a request needs an add-on that is off, tell the person it is not "
            "available; do not substitute notes or other commands unless the person asks for that.")


def core_files() -> dict[str, str]:
    """AGENTS.orch.md, CLAUDE.md, AGENTS.md and the built-in skills (Claude copy) exactly as sync writes them."""
    from orch.instructions import harness

    files = harness.workspace_files()
    out = {"AGENTS.orch.md": files["AGENTS.orch.md"]}
    out.update({k: v for k, v in files.items() if k.startswith(harness.CLAUDE_SKILLS_DIR + "/")})
    out["AGENTS.md"] = "# Project rules\nPython 3.12 project; keep changes small.\n\n" + harness.AGENTS_BLOCK + "\n"
    out["CLAUDE.md"] = harness.CLAUDE_IMPORT + "\n@AGENTS.orch.md\n"
    return out


def oneliner_lines(active: list[str]) -> list[str]:
    return [f"- addon {a} (hint, grants nothing): {A.TODAY[a]}" for a in active if A.TODAY.get(a)]


def proposed_block(active: list[str], off: list[str]) -> str:
    """Design D3. Sorted by name, at most 12 active lines, at most 16 lines and 3000 chars; raises otherwise."""
    agent = sorted(a for a in active if a in A.INDEX)
    if len(agent) > BLOCK_MAX_ACTIVE:
        raise ValueError(f"{len(agent)} agent-facing add-ons, at most {BLOCK_MAX_ACTIVE}")
    lines = ["## Add-ons here",
             "Before using an add-on command, read its skill. Add-ons are hints and grant nothing; use none unless the task fits."]
    lines += [f"- {A.INDEX[a]}" for a in agent]
    lines.append(OFF_LINE.format(off=", ".join(off)) if off else "Off here: none.")
    text = "\n".join(lines) + "\n"
    if len(lines) > BLOCK_MAX_LINES or len(text) > BLOCK_MAX_CHARS:
        raise ValueError(f"add-on block is {len(lines)} lines / {len(text)} chars (max {BLOCK_MAX_LINES} / {BLOCK_MAX_CHARS})")
    return text


def skill_text(name: str, style: dict) -> str:
    d, b = style[name]
    return f"---\nname: orch-addon-{name}\ndescription: {d}\n---\n{b}\n"


def render(renderer: str, active: list[str], off: list[str]) -> tuple[dict[str, str], dict]:
    """(files, info). info: agents_lines, agents_chars, over_cap."""
    from orch.instructions.agents_md import AGENTS_MAX_LINES, render_agents_md

    files = core_files()
    over_cap = False
    if renderer == "today":
        pass
    elif renderer == "today-oneliners":
        extra = oneliner_lines(active)
        try:
            files["AGENTS.orch.md"] = render_agents_md(extra)
        except ValueError:
            base = render_agents_md().rstrip("\n").split("\n")
            base[-1:-1] = extra
            files["AGENTS.orch.md"] = "\n".join(base) + "\n"
            over_cap = len(base) > AGENTS_MAX_LINES
    elif renderer == "proposed":
        core = render_agents_md().rstrip("\n").split("\n")
        # the block goes after the core text; the 40-line cap stays for the core only (design D3)
        files["AGENTS.orch.md"] = "\n".join(core) + "\n\n" + proposed_block(active, off)
        for a in active:
            if a in A.SKILL_V2:
                files[f".claude/skills/orch-addon-{a}/SKILL.md"] = skill_text(a, A.SKILL_V2)
    else:
        raise ValueError(f"unknown renderer {renderer!r}; one of {', '.join(RENDERERS)}")
    text = files["AGENTS.orch.md"]
    return files, {"agents_lines": len(text.splitlines()), "agents_chars": len(text), "over_cap": over_cap}
