"""M7a/M7b projections of runs, terminal and agent previews and the runner (contract v1.6, section 12.3.2). Host prose
is display-only: a label, a command line, a path or a message with invisible characters is withheld (``*_hidden``),
never repaired. Nothing the page does not draw is passed on (the session, a run's grant id, the spec's inputs, argv,
the program path)."""

import re

from ..text import hidden
from .addons import display

RUN_FIELDS = "run rev kind ticket harness mode model as_person state started_at viewers writer cols rows replay_until"
GRANT_ID = re.compile(r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$")
KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,9}$")
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]/-]{0,63}$")
EXIT_FIELDS = "code signal at reason cleanup"


def text(value, limit=4096):
    return value if isinstance(value, str) and len(value) <= limit and not hidden(value) else None


def run(row):
    out = {key: row.get(key) for key in RUN_FIELDS.split()}
    label = row.get("label")
    out["label"] = display(label)
    out["label_hidden"] = isinstance(label, str) and hidden(label)
    out["subagent_model"] = display(row.get("subagent_model"))
    # M7b: whether an agent's runner grant still holds (`ended`: the host refuses its writes; Stop is offered). Not a
    # run state. The grant id and the session stay out (nothing on the page needs them).
    out["grant_state"] = row.get("grant_state") if row.get("grant_state") in ("ok", "ended") else None
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
    out = {key: info[key] for key in ("connected", "enabled", "terminals")}
    return {**out, "grant": runner_grant(info.get("grant"))}


def runner_grant(grant):
    """The caller person's runner grant as `{id, until, scope[, startable, startable_until]}`, or None (none, or not
    one this page can show). Never a secret: the host sends none here, and only these checked fields are passed on."""
    if (
        not isinstance(grant, dict)
        or not isinstance(grant.get("id"), str)
        or not GRANT_ID.fullmatch(grant["id"])
        or not isinstance(grant.get("until"), str)
        or not TIME.fullmatch(grant["until"])
        or grant.get("scope") not in ("all", "workable")
    ):
        return None
    out = {"id": grant["id"], "until": grant["until"], "scope": grant["scope"]}
    # run 8: the host's own launch rule (`whoami.runner.grant` only): may this grant start an agent now, and until when.
    # Passed on only when well formed; core stays the authority and refuses a launch it does not allow.
    if type(grant.get("startable")) is bool:
        out["startable"] = grant["startable"]
        until = grant.get("startable_until")
        out["startable_until"] = until if isinstance(until, str) and TIME.fullmatch(until) else None
    return out


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
        elif item.get("t") == "runner" and "grant" in item:
            # H11c-2: the person's runner grant changed (a deposit, a replace, a forget, a runner restart...).
            out.append({"t": "runner", "grant": runner_grant(item["grant"])})
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


BLOCKED_DETAIL = {"reason": ("none", "expiring", "scope", "revoked"), "missing": ("addon", "machine", "terminals")}


def blocked_of(blocked):
    """`{code, message, detail}`: code and message as plain text; of `detail` only the closed `reason`/`missing` values
    the dialog branches on (anything else is dropped)."""
    if not isinstance(blocked, dict):
        return None
    detail = blocked.get("detail") if isinstance(blocked.get("detail"), dict) else {}
    return {
        "code": text(blocked.get("code"), 64),
        "message": text(blocked.get("message"), 500),
        "detail": {k: detail[k] for k, allowed in BLOCKED_DETAIL.items() if detail.get(k) in allowed},
    }


def agent_preview(data):
    """`agent.preview` for the Start agent dialog (section 12.3): what runs (`display`), where (`cwd`, `cwd_path`, and
    whether the worktree is created first), with which environment names and runner grant, and what blocks it. Not
    `argv` or `program`: `display` is what the person is shown."""
    cwd = data.get("cwd") if isinstance(data.get("cwd"), dict) else {}
    kind = cwd.get("kind") if cwd.get("kind") in ("workspace", "repo", "worktree", "workdir") else None
    shown = {"kind": kind}
    for key in ("repo", "ticket"):
        value = text(cwd.get(key), 64)
        if value is not None:
            shown[key] = value
    ticket, spec = data.get("ticket"), data.get("spec")
    model, source = data.get("model"), data.get("model_source")
    # A ticket without a codebase runs in its work folder: whether it exists and is empty. The page starts a NEW
    # ticket's agent unattended only in a new or empty folder, so a missing answer must read as "not known".
    folder = data.get("workdir")
    workdir = (
        {"exists": folder["exists"], "empty": folder["empty"]}
        if isinstance(folder, dict) and type(folder.get("exists")) is bool and type(folder.get("empty")) is bool
        else None
    )
    return {
        "spec": spec if isinstance(spec, str) and re.fullmatch(r"[0-9a-f]{64}", spec) else None,
        "ticket": ticket if isinstance(ticket, str) and KEY.fullmatch(ticket) else None,
        "display": text(data.get("display")),
        "model": model if isinstance(model, str) and MODEL_NAME.fullmatch(model) else None,
        "model_source": source if source in ("chosen", "addon", "default") else None,
        "model_by": text(data.get("model_by"), 60) if source == "addon" else None,  # the package's own title: data
        "display_hidden": isinstance(data.get("display"), str) and text(data.get("display")) is None,
        "cwd": shown,
        "cwd_path": text(data.get("cwd_path")),
        "cwd_path_hidden": isinstance(data.get("cwd_path"), str) and text(data.get("cwd_path")) is None,
        "will_create_worktree": data.get("will_create_worktree") is True,
        **({"workdir": workdir} if workdir is not None else {}),
        "login_shell": data.get("login_shell") is True,
        "env": [name for name in data.get("env") or [] if text(name, 128)][:64],
        "grant": runner_grant(data.get("grant")),
        "blocked": blocked_of(data.get("blocked")),
        "warnings": [w for w in (text(w, 500) for w in data.get("warnings") or []) if w][:8],
    }
