STATUSES = ("backlog", "open", "in-progress", "waiting", "testing", "done")
TYPES = ("feature", "bug", "chore", "spike", "investigation", "epic")  # epic: groups children (orch.core.epics)
PRIORITIES = ("low", "normal", "high", "urgent")
PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
SIZES = ("xs", "s", "m", "l")
# An optional `due` date (#174, `YYYY-MM-DD`, orch.core.due): an open ticket due within this many days, or overdue,
# comes first within its priority in `orch next`. Planning only: no gate depends on it, nothing reminds.
DUE_SOON_DAYS = 3
# The sections of a ticket. A file holds only the non-empty ones (an empty section is never written).
# "Summary" (#15) is the agent's one-to-three-bullet statement, shown first; "Current state" is shown as the
# agent's "Handoff". Context, Current state and Findings are agent notes; the rest is the human-facing contract.
SECTIONS = (
    "Ask", "Summary", "Context", "Requirements", "Acceptance criteria", "Out of scope",
    "Plan", "Tasks", "Current state", "Verification", "Log", "Findings",
)
# Sections older orch versions wrote. A ticket file that still has text under one fails to load until `orch migrate`
# has moved it into Context (orch.core.migrate).
OLD_SECTIONS = ("Proposal", "Decisions")
# The order sections are written in.
FILE_ORDER = (
    "Ask", "Summary", "Context", "Requirements", "Acceptance criteria", "Out of scope",
    "Plan", "Tasks", "Current state", "Verification", "Log", "Findings",
)
AGENT_NOTES = ("Current state", "Context", "Findings")
FRONTMATTER_ORDER = (
    "id", "title", "type", "priority", "size", "status", "created", "updated", "external",
    "repos", "branches", "worktrees", "prs", "parent", "sprint", "due", "blocked_by", "follow_ups", "resolution", "superseded_by", "labels",
    "gates", "questions", "claim", "sessions",
)
# Why a done ticket is done (orch close --as). Absent on a done ticket means completed (closed before it existed).
RESOLUTIONS = ("completed", "wont-do", "superseded", "duplicate")
SUCCEEDED = ("superseded", "duplicate")  # these name the ticket that replaces them in `superseded_by`
