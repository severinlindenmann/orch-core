"""M7a projections of runs and terminal previews (contract v1.6, section 12.3.2). Host prose is display-only: a label,
a command line, a path or a message with invisible characters is withheld (``*_hidden``), never repaired. Nothing the
page does not draw is passed on (the grant id, the session, the spec's inputs)."""

from ..text import hidden
from .addons import display

RUN_FIELDS = "run rev kind ticket harness mode model as_person state started_at viewers writer cols rows replay_until"
EXIT_FIELDS = "code signal at reason cleanup"


def text(value, limit=4096):
    return value if isinstance(value, str) and len(value) <= limit and not hidden(value) else None


def run(row):
    out = {key: row.get(key) for key in RUN_FIELDS.split()}
    label = row.get("label")
    out["label"] = display(label)
    out["label_hidden"] = isinstance(label, str) and hidden(label)
    cwd = row.get("cwd") if isinstance(row.get("cwd"), dict) else {}
    out["cwd"] = {key: display(cwd.get(key)) for key in ("kind", "repo", "ticket") if key in cwd}
    exit_ = row.get("exit")
    out["exit"] = {key: exit_.get(key) for key in EXIT_FIELDS.split()} if isinstance(exit_, dict) else None
    if isinstance(row.get("out_bytes"), int):
        out["out_bytes"] = row["out_bytes"]
    return out


def runner(info):
    if not isinstance(info, dict) or any(type(info.get(k)) is not bool for k in ("connected", "enabled", "terminals")):
        return None
    return {key: info[key] for key in ("connected", "enabled", "terminals")}


def items(frame_items):
    """`run.sub` items: `{id, t:"run", run}` or `{id, t:"gone", run:RunId}`; anything else is dropped."""
    out = []
    for item in frame_items:
        if not isinstance(item, dict):
            continue
        if item.get("t") == "run" and isinstance(item.get("run"), dict):
            out.append({"t": "run", "run": run(item["run"])})
        elif item.get("t") == "gone" and isinstance(item.get("run"), str):
            out.append({"t": "gone", "run": item["run"]})
    return out


def preview(data):
    blocked = data.get("blocked")
    return {
        "spec": data.get("spec"),
        "display": text(data.get("display")),
        "display_hidden": isinstance(data.get("display"), str) and text(data.get("display")) is None,
        "cwd_path": text(data.get("cwd_path")),
        "cwd_path_hidden": isinstance(data.get("cwd_path"), str) and text(data.get("cwd_path")) is None,
        "notice": text(data.get("notice"), 200),
        "as_person": data.get("as_person") is True,
        "login_shell": data.get("login_shell") is True,
        "env": [name for name in data.get("env") or [] if text(name, 128)],
        "blocked": {"code": text(blocked.get("code"), 64), "message": text(blocked.get("message"), 500)}
        if isinstance(blocked, dict)
        else None,
        "warnings": [w for w in (text(w, 500) for w in data.get("warnings") or []) if w][:8],
    }
