"""The Activity page: `activity.list` items as plain-word rows. Host prose is display-only; hidden text is withheld,
never repaired. The host already filters each item on its own (a restricted ticket's events never arrive for a viewer
who cannot see it), so a row carries only what the host sent for this viewer."""

import re

from ..text import hidden
from .shell import safe_name
from .tickets import ui_actor

# What a filter group lists. `workspace` is the host's workspace log (scope), the others are event types.
GROUP_TYPES = {
    "tickets": [
        "ticket.created",
        "ticket.updated",
        "status.changed",
        "ticket.moved",
        "ticket.submitted",
        "ticket.closed",
        "ticket.reopened",
        "visibility.changed",
        "people.changed",
    ],
    "work": [
        "claim.taken",
        "claim.released",
        "task.started",
        "task.reopened",
        "task.done",
        "task.skipped",
        "task.blocked",
        "branch.pushed",
        "handoff.written",
    ],
    "talk": ["log.added", "question.asked", "question.answered"],
    "reviews": ["gate.approved", "gate.changes_requested", "verdict.given", "gate.invalidated"],
    "files": ["artifact.added", "artifact.replaced"],
}
GROUPS = ("all", *GROUP_TYPES, "workspace")
GROUP_OF = {t: g for g, types in GROUP_TYPES.items() for t in types}

#: the start of the note a signed Publish share leaves in the ticket's log (see handoff.PUBLISH_MARK)
PUBLISH_NOTE = "Publish approval:"

GATE = {"requirements": "requirements", "plan": "plan", "verify": "result", "code": "code"}

# Types whose host summary is something a person wrote (a note, a reason, a comment); the others' summaries are
# technical ("section requirements", "Q1") and are not shown.
DETAIL = {
    "ticket.closed",
    "log.added",
    "task.done",
    "task.blocked",
    "task.skipped",
    "gate.changes_requested",
    "verdict.given",
}

SIMPLE = {
    "ticket.created": "created the ticket",
    "ticket.updated": "edited the ticket",
    "ticket.submitted": "sent the ticket on",
    "handoff.written": "wrote a hand-off note",
    "ticket.closed": "closed the ticket",
    "ticket.reopened": "reopened the ticket",
    "people.changed": "changed who is on the ticket",
    "claim.taken": "started working on the ticket",
    "claim.released": "stopped working on the ticket",
    "task.started": "started a task",
    "task.reopened": "reopened a task",
    "task.done": "finished a task",
    "task.skipped": "skipped a task",
    "task.blocked": "marked a task as blocked",
    "log.added": "wrote a note",
    "question.asked": "asked a question",
    "question.answered": "answered a question",
    "artifact.added": "added a file",
    "artifact.replaced": "replaced a file",
    "edit.external": "changed the ticket outside orch",
    "projection.repaired": "repaired a ticket file",
    "restore": "restored an earlier state",
    "invalid.acknowledged": "acknowledged a problem in the log",
    "workspace.created": "created the workspace",
    "policy.changed": "changed the approval rules",
    "settings.changed": "changed the workspace settings",
    "grant.issued": "gave an agent access",
    "grant.revoked": "ended an agent's access",
}


def label(value) -> str | None:
    """A host word (status, gate, role) in lower case with spaces, or None when it is not safe text."""
    if not isinstance(value, str) or not value or hidden(value) or len(value) > 40:
        return None
    return value.replace("_", " ").replace("-", " ")


def display(value):
    return value if isinstance(value, str) and not hidden(value) else None


def repo_sentence(repos) -> str | None:
    """Sentences for a repo change from the host's `repos` ({name: "set" | "removed"}); None when it names none.
    The event alone cannot tell a new repo from a changed one, so a set is "updated"."""
    if not isinstance(repos, dict):
        return None
    parts = []
    for name, how in sorted(repos.items()):
        shown = display(name)
        if shown and how in ("set", "removed"):
            parts.append(f"{'removed' if how == 'removed' else 'updated'} the repository “{shown}”")
    return "; ".join(parts) or None


def manual_acceptance(item: dict, can=None) -> dict | None:
    """Manual acceptance or withdrawal only when a person passes the authority callback.

    An agent's file, or a file of another kind with the same words in its name or
    summary, is never shown as one. Missing authority information returns None.
    """
    kind, data = item.get("type"), item.get("data") or {}
    if (
        kind not in ("artifact.added", "artifact.replaced")
        or data.get("kind") != "feedback"
        or (item.get("actor") or {}).get("kind") != "person"
        or can is None
        or not can((item.get("actor") or {}).get("id"))
    ):
        return None
    label = data.get("label")
    accepted = re.fullmatch(r"accepted (AC[1-9][0-9]*) [0-9a-f]{16}", label) if isinstance(label, str) else None
    if accepted and data.get("ac") == accepted[1]:
        return {"kind": "accepted", "ac": accepted[1]}
    withdrawn = re.fullmatch(r"withdrawn (AC[1-9][0-9]*)", label) if isinstance(label, str) else None
    if kind == "artifact.replaced" and "ac" not in data and withdrawn:
        return {"kind": "withdrawn", "ac": withdrawn[1]}
    return None


def sentence(item: dict, name_of, can=None) -> str:
    """What happened, in words, after the actor's name. Unknown types and unsafe fields get a general sentence."""
    kind, data = item["type"], item.get("data") or {}
    manual = manual_acceptance(item, can)
    if manual is not None:
        return (
            f"accepted {manual['ac']} manually"
            if manual["kind"] == "accepted"
            else f"withdrew the manual acceptance of {manual['ac']}"
        )
    if kind in ("status.changed", "ticket.moved"):
        before, after = label(data.get("from")), label(data.get("to"))
        return f"moved the ticket from {before} to {after}" if before and after else "moved the ticket"
    if kind == "gate.approved":
        return f"approved the {GATE.get(data.get('gate'), 'step')}"
    if kind == "gate.changes_requested":
        return f"asked for changes to the {GATE.get(data.get('gate'), 'step')}"
    if kind == "gate.invalidated":
        return f"reset the approval of the {GATE.get(data.get('gate'), 'step')}"
    if kind == "verdict.given":
        outcome = label(data.get("outcome"))
        return f"gave a verdict: {outcome}" if outcome else "gave a verdict"
    if kind == "visibility.changed":
        return "limited the ticket to some people" if data.get("restricted") else "opened the ticket to the workspace"
    if kind == "branch.pushed":
        repo = display(data.get("repo_name"))
        # the host saw the branch (a worktree was made, or work was committed): nobody "pushed" anything to a server
        return f"saw the ticket's branch in the repository “{repo}”" if repo else "saw the ticket's branch"
    if kind == "member.added":
        return f"added {name_of(data.get('person'))} as {label(data.get('role')) or 'a member'}"
    if kind == "member.removed":
        return f"removed {name_of(data.get('person'))} from the workspace"
    if kind == "role.changed":
        return f"changed the role of {name_of(data.get('person'))} to {label(data.get('role')) or 'another role'}"
    if kind in ("device.added", "device.removed", "device.revoked"):
        verb = "added" if kind == "device.added" else "removed"
        return f"{verb} a device of {name_of(data.get('person'))}" if data.get("person") else f"{verb} a device"
    if kind == "settings.changed":
        return repo_sentence(data.get("repos")) or SIMPLE[kind]
    if kind in ("addon.granted", "addon.enabled", "addon.disabled", "addon.purged"):
        verb = {
            "addon.granted": "installed (and left off)" if data.get("off") is True else "turned on",
            "addon.enabled": "turned on",
            "addon.disabled": "turned off",
            "addon.purged": "removed",
        }[kind]
        name = display(data.get("name"))
        return f"{verb} the {name} add-on" if name else f"{verb} an add-on"
    return SIMPLE.get(kind, "did something")


_FACTOR_WORDS = {"se-unlocked": "phone, unlocked", "password+totp": "browser, password + code", "passkey": "passkey"}


def via_words(a: dict) -> str:
    """``via hub · phone, unlocked`` for a hub-signed row (the raw factor label is never shown)."""
    words = _FACTOR_WORDS.get(str(a.get("channel_factor")))
    return "via hub" + (f" · {words}" if words else "")


def item(a: dict, names: dict[str, str], can=None) -> dict:
    """One activity item as a row. `jpos`, `ws_seq`, `log`, `uid` and `data` never reach the browser."""

    def name_of(person):
        return names.get(person) or "someone"

    actor = ui_actor(a["actor"])
    group = GROUP_OF.get(a["type"], "tickets" if a.get("key") else "workspace")
    out = {"id": a["id"], "at": a["at"], "type": a["type"], "group": group}
    if actor["kind"] == "person":
        raw = a["actor"]
        if raw.get("via"):  # signed through a hub channel (ticket-format 15.4): the member it is, else "Hub user ..."
            actor["name"] = (names.get(raw.get("person") or "") or f"Hub user {actor['id'][2:10]}") + " " + via_words(a)
        else:
            actor["name"] = names.get(actor["id"]) or "A former member"
    elif actor["kind"] == "agent":
        actor["for_name"] = names.get(actor.get("for")) or None
    out["actor"] = {k: v for k, v in actor.items() if k in ("kind", "id", "name", "for_name")}
    out["did"] = sentence(a, name_of, can)
    out["ticket"] = a.get("key") or ""
    title = a.get("title")
    out["title"] = display(title)
    out["title_hidden"] = isinstance(title, str) and hidden(title)
    summary = a.get("summary")
    if a["type"] == "log.added" and isinstance(summary, str) and summary.startswith(PUBLISH_NOTE):
        # The note a share approval leaves on the ticket (its text is cut by the host): say what it is, in words.
        out["did"] = "approved sharing files outside this computer"
    elif a["type"] in DETAIL and isinstance(a.get("summary"), str) and a["summary"]:
        out["detail"] = display(a["summary"])
        out["detail_hidden"] = hidden(a["summary"])
    return out


def feed(data: dict, roster: dict) -> dict:
    """Only workspace owners/maintainers get manual acceptance wording here.

    Ticket owner/reviewer-only acceptances remain generic file events: this feed
    has no ticket ownership or reviewer information.
    """
    can = acceptance_authority(roster)
    names = {}
    for m in roster["members"]:
        names[m["person"]] = safe_name(m)[0]
    seen, items = set(), []
    for a in data["items"]:
        if a["id"] not in seen:  # a page may repeat what the live tail already showed
            seen.add(a["id"])
            items.append(item(a, names, can))
    return {"available": True, "items": items, "next_page": data.get("next_page")}


def acceptance_authority(roster: dict, doc: dict | None = None):
    """Current non-viewer membership plus workspace or ticket authority."""
    roles = {m["person"]: m["role"] for m in roster["members"]}
    doc = doc or {}
    reviewers = (doc.get("people") or {}).get("reviewers") or []
    return lambda person: roles.get(person) in ("owner", "maintainer") or (
        roles.get(person) == "member" and (person == doc.get("owner") or person in reviewers)
    )
