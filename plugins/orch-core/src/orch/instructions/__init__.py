"""AGENTS.orch.md generator, skills, session-start and pre-compact hook text, stale check, harness files.

Everything an agent is told is generated or shipped from here and checked against the operation registry, so the text
cannot name a command that does not exist (core §2, ticket-format §10.2). The budgets are tests: AGENTS.orch.md at most
25 lines, the session-start text at most 6, each skill under a fixed size.
"""

from .agents_md import AGENTS_MAX_LINES, INSTRUCTIONS_REV, render_agents_md, stamp_rev
from .harness import plugin_files, workspace_files, write_workspace_files
from .hooks import PRE_COMPACT_MAX_LINES, SESSION_START_MAX_LINES, pre_compact_lines, session_start_lines
from .skills import SKILL_MAX_CHARS, Skill, builtin_skills, check_sidecar, parse_frontmatter
from .stale import stale_findings

__all__ = [
    "AGENTS_MAX_LINES",
    "INSTRUCTIONS_REV",
    "PRE_COMPACT_MAX_LINES",
    "SESSION_START_MAX_LINES",
    "SKILL_MAX_CHARS",
    "Skill",
    "builtin_skills",
    "check_sidecar",
    "parse_frontmatter",
    "plugin_files",
    "pre_compact_lines",
    "render_agents_md",
    "session_start_lines",
    "stale_findings",
    "stamp_rev",
    "workspace_files",
    "write_workspace_files",
]
