"""Delete these provisional wrappers as stream A serves them."""

OPS = (
    "host.list",
    "whoami",
    "workspace.show",
    "host.status",
    "member.list",
    "workspace.settings",
    "grant.list",
    "ticket.query",
    "ticket.get",
    "needs",
    "activity.list",
    "diff",
    "artifact.get",
    "sub.open",
    "sub.close",
)

# The inner ops of the workspace hand-offs (M4): served through `human.prepare` only, listed in `welcome.ops`.
WRITE_OPS = (
    "settings.set",
    "policy.set",
    "member.add",
    "member.role",
    "member.remove",
    "device.remove",
    "grant.revoke",
)

# `human.*` (v1.4 section 0.8.1): stubbed by `humanops.py` until the real host (H5a) serves them.
HUMAN_OPS = ("human.prepare", "human.get", "human.pending", "human.cancel", "human.submit")
