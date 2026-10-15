"""M6 projections. Host prose is display-only; hidden text is withheld, never repaired."""

from ..text import hidden
from .tickets import summary


def display(value):
    return value if isinstance(value, str) and not hidden(value) else None


def fields(row, names):
    return {key: row.get(key) for key in names.split()} if isinstance(row, dict) else None


def prose(row, key):
    value = row.get(key)
    return {key: display(value), f"{key}_hidden": isinstance(value, str) and hidden(value)}


def quick(data):
    rows = []
    for row in data["rows"]:
        item = summary(row)
        item["quick"] = fields(row.get("quick") or {}, "commits files over")
        rows.append(item)
    return {"available": True, "rows": rows, "next_page": data.get("next_page"), "total": data["total"]}


def records(data):
    repo, remote, pending = data.get("repo"), data.get("remote"), data.get("pending")
    return {
        "available": True,
        "configured": data.get("configured"),
        "repo": {"state": repo.get("state"), **prose(repo, "problem")} if repo else None,
        "pending": {
            **fields(pending, "events more workspace_events"),
            "tickets": [fields(t, "key events") for t in pending.get("tickets", [])],
        }
        if pending is not None
        else None,
        "last": fields(data.get("last"), "commit at by ws_seq pushed_at"),
        "last_noted": fields(data.get("last_noted"), "by at"),
        "auto": fields(data.get("auto"), "on every_min push"),
        "remote": {
            **fields(remote, "private export"),
            **prose(remote, "url"),
            **prose(remote, "current_url"),
            **{k: remote.get(k) if type(remote.get(k)) is bool else None for k in ("current_private", "matches_last")},
        }
        if remote
        else None,
    }


def runner(identity, supported):
    row = identity.get("runner") if supported else None
    if not isinstance(row, dict) or any(type(row.get(k)) is not bool for k in ("connected", "enabled", "terminals")):
        return None
    return fields(row, "connected enabled terminals")
