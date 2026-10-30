"""The "Needs attention" list at the top of the hub's skills, tool servers and secrets pages (#1617 step 1). Computed from
what the hub already holds, one plain line each with one action (a page to open). Hub side only: nothing here asks a
workspace anything.

What it lists:
  - a secret that has expired, or expires within 14 days;
  - a secret shared with workspaces that have no value yet (a workspace bound later gets no wrap until an admin types the
    value again: the hub holds no key that could make one);
Not listed yet: a tool server offered but not accepted for more than 7 days. The hub does not learn what a workspace owner
accepted, so there is nothing to compute from until the status report (#1620 ``org.report``) exists.

Text that comes from a workspace goes through ``render_text``; a line never carries an id or a value."""

from __future__ import annotations

from typing import Any

from ..contract import phase4 as P4

DAY_MS = 86_400_000
SOON_DAYS = 14
ENTER = "Enter the value"


def _who(w: dict[str, Any] | None) -> str:
    name = (w or {}).get("name")
    if type(name) is str and name:
        return P4.render_text(name)
    owner = (w or {}).get("owner_name")
    return P4.render_text(owner) + "'s workspace" if type(owner) is str and owner else "A workspace"


def needs_attention(
    secrets: list[dict[str, Any]], workspaces: list[dict[str, Any]], now_ms: int
) -> list[dict[str, str]]:
    """``secrets`` and ``workspaces`` are the payload of ``GET /admin/secrets``. Most urgent first: expired, then expiring
    soonest, then missing values."""
    by_id = {w["workspace_id"]: w for w in workspaces}
    expiry: list[tuple[int, dict[str, str]]] = []
    waiting: list[dict[str, str]] = []
    for s in sorted(secrets, key=lambda x: x["name"]):
        name, left = s["name"], s["expires_ms"] - now_ms
        if left <= 0:
            expiry.append(
                (
                    left,
                    {
                        "kind": "secret_expired",
                        "text": f"{name} has expired.",
                        "page": "secrets",
                        "action": "Enter a new value",
                    },
                )
            )
        elif left <= SOON_DAYS * DAY_MS:
            days = -(-left // DAY_MS)
            when = "today" if days <= 1 else f"in {days} days"
            expiry.append(
                (
                    left,
                    {
                        "kind": "secret_expiring",
                        "text": f"{name} expires {when}.",
                        "page": "secrets",
                        "action": "Enter a new value",
                    },
                )
            )
        missing = sorted(w for w, st in s["state"].items() if st["state"] != "ready")
        if missing and left > 0:  # an expired secret already has its line
            who = _who(by_id.get(missing[0])) + " is" if len(missing) == 1 else f"{len(missing)} workspaces are"
            waiting.append(
                {
                    "kind": "secret_waiting",
                    "text": f"{who} waiting for the value of {name}.",
                    "page": "secrets",
                    "action": ENTER,
                }
            )
    return [i for _, i in sorted(expiry, key=lambda x: x[0])] + waiting
