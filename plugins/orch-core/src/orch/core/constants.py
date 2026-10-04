STATUSES = ("backlog", "open", "in-progress", "waiting", "testing", "done")
TYPES = ("feature", "bug", "chore", "spike", "investigation", "epic")  # epic: groups children (orch.core.epics)
PRIORITIES = ("low", "normal", "high", "urgent")
PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
SIZES = ("xs", "s", "m", "l")
# The sections of a ticket. A file holds only the non-empty ones (an empty section is never written).
# "Summary" (#15) is the agent's one-to-three-bullet statement, shown first; "Current state" is shown as the
# agent's "Handoff". Context, Current state and Findings are agent notes; the rest is the human-facing contract.
SECTIONS = (
    "Ask", "Summary", "Context", "Requirements", "Acceptance criteria", "Out of scope",
    "Plan", "Tasks", "Current state", "Verification", "Log", "Findings",
)
# Sections of old tickets that new tickets no longer get: still parsed, rendered (under Agent notes) and kept
# in the file where they have text, but `orch section set` refuses them.
LEGACY_SECTIONS = ("Proposal", "Decisions")
# The order sections are written in, legacy ones at their old place.
FILE_ORDER = (
    "Ask", "Summary", "Context", "Requirements", "Acceptance criteria", "Out of scope", "Proposal",
    "Plan", "Tasks", "Current state", "Verification", "Decisions", "Log", "Findings",
)
AGENT_NOTES = ("Current state", "Context", "Findings")
FRONTMATTER_ORDER = (
    "id", "title", "type", "priority", "size", "status", "created", "updated", "external",
    "repos", "branches", "worktrees", "prs", "parent", "sprint", "blocked_by", "follow_ups", "labels",
    "gates", "questions", "claim", "sessions",
)
