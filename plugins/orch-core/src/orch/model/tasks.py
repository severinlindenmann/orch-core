"""Tasks and leases (A4), artifacts and evidence (ticket-format §6).

A task lives in ``ticket.json`` (its definition) and in events (its state). A done task always has a holder: the
session that held its lease, or took it implicitly by finishing it. Evidence is bound into the verify gate hash.
"""

from __future__ import annotations

import re
from typing import Any

from orch.canon import ARTIFACT_KINDS

from . import addon_rules, claims, gates, generations, source
from .accept import WORKABLE, criterion_hash, may_accept, parse_label, parse_withdraw_label
from .codes import Code, Refusal
from .types import ArtifactCore, Lease, TaskCore, TCore, WsCore, ts

_FROM = {
    "task.started": ("open", "blocked"),
    "task.done": ("open", "started", "blocked"),
    "task.skipped": ("open", "started", "blocked"),
    "task.blocked": ("open", "started"),
    "task.reopened": ("done", "skipped", "blocked"),
}
_TO = {
    "task.started": "started",
    "task.done": "done",
    "task.skipped": "skipped",
    "task.blocked": "blocked",
    "task.reopened": "open",
}


def task_ids(t: TCore) -> list[str]:
    return [x["id"] for x in t.fields["tasks"]]


def lease_active(ws: WsCore, lease: Lease, at: int) -> bool:
    return at - lease.at <= ws.settings["lease_ttl_min"] * 60


def task_event(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    typ, tid, a, at = e["type"], e["task"], e["actor"], ts(e["at"])
    if t.status in ("done", "closed"):
        return Refusal(Code.TICKET_FROZEN, f"{typ} is refused on {t.status} tickets")
    if (r := claims.require_holder(ws, t, e)) is not None:
        return r
    if tid not in task_ids(t):
        return Refusal(Code.TASK_UNKNOWN, tid)
    tk = t.tasks.get(tid) or TaskCore()
    if tk.state not in _FROM[typ]:
        return Refusal(Code.TASK_STATE, f"{typ} from {tk.state}")
    who = a["session"] if a["kind"] == "agent" else a["id"]
    lease = t.leases.get(tid)
    if a["kind"] == "agent" and lease is not None and lease.session != who and lease_active(ws, lease, at):
        return Refusal(Code.TASK_LEASED, f"{tid} is leased by {lease.session}")
    if typ == "task.done":
        if a["kind"] == "person":  # R3: a receipt is a command's result that an agent or the host recorded
            if "receipt" in e:
                return Refusal(Code.TASK_BAD_RECEIPT, "a person's task.done carries no receipt")
            if not may_accept(ws, t, a["id"]):
                return Refusal(
                    Code.ROLE_DENIED,
                    "a task is marked done by a person only if they are the ticket owner, a reviewer or a maintainer",
                )
        if (r := _receipt(t, tid, e)) is not None:
            return r
        if "log" in e and e["log"] not in t.artifacts:
            return Refusal(Code.ARTIFACT_UNKNOWN, e["log"])
        tk.receipt = e.get("receipt")
        tk.done_event = e["id"]
    if typ in ("task.started",):
        t.leases[tid] = Lease(who, at)
    else:
        t.leases.pop(tid, None)
    tk.state = _TO[typ]
    if typ in ("task.started", "task.done", "task.skipped"):
        tk.holder = who
    tk.reason = e.get("reason")
    t.tasks[tid] = tk
    if typ in ("task.done", "task.reopened", "task.skipped"):
        generations.mark(t, "verify")
    return None


def _receipt(t: TCore, tid: str, e: dict[str, Any]) -> Refusal | None:
    r = e.get("receipt")
    if r is None:
        return None
    task = next(x for x in t.fields["tasks"] if x["id"] == tid)
    verify = task.get("verify")
    if verify is None or r["cmd"] != verify["cmd"]:
        return Refusal(Code.TASK_BAD_RECEIPT, "receipt.cmd must equal the task's verify.cmd")
    if r["exit"] != 0:
        return Refusal(Code.TASK_BAD_RECEIPT, "a receipt with a non-zero exit is never appended")
    if r["repo"] is not None and r["repo"] not in source.linked_repos(t):
        return Refusal(Code.SOURCE_UNLINKED, f"receipt repo {r['repo']} is not linked")
    return None


# --- evidence ---------------------------------------------------------------------------------------------------


def ac_evidence(ws: WsCore, t: TCore) -> dict[str, list[str]]:
    """Per acceptance criterion, its evidence (§6): a file artifact from a person or a granted agent naming it in
    ``ac``, or a done task that ``proves`` it with an exit-0 receipt at the current source ``sha`` of its repo."""
    out: dict[str, list[str]] = {a["id"]: [] for a in t.fields["acceptance"]}
    texts = {a["id"]: a["text"] for a in t.fields["acceptance"]}
    for name, art in sorted(t.artifacts.items()):
        if art.kind == "feedback" and (art.label or "").startswith("accepted "):
            if art.ac not in texts or parse_label(art.label) != (art.ac, criterion_hash(art.ac, texts[art.ac])):
                continue
        if art.evidence_ok and art.ac in out:
            out[art.ac].append(f"artifact:{name}")
    for task in t.fields["tasks"]:
        tk = t.tasks.get(task["id"])
        if tk is None or tk.state != "done" or tk.receipt is None or tk.receipt["exit"] != 0:
            continue
        repo = tk.receipt["repo"]
        if repo is not None and (tk.receipt["commit"] is None or tk.receipt["commit"] != source.repo_sha(t, repo)):
            continue  # no commit (a dirty tree) or not the code the host observed: not evidence
        for ac in task["proves"]:
            if ac in out:
                out[ac].append(f"task:{task['id']}")
    return out


def acceptance_state(t: TCore, ac_id: str) -> str | None:
    """Current person feedback only; malformed labels never claim acceptance."""
    ac = next((a for a in t.fields["acceptance"] if a["id"] == ac_id), None)
    if ac is None:
        return None
    state = None
    for art in t.artifacts.values():
        parsed = parse_label(art.label)
        if (
            art.kind != "feedback"
            or re.fullmatch(r"p_[0-9a-f]{32}", art.by) is None
            or not art.evidence_ok
            or art.ac != ac_id
        ):
            continue
        if parsed is None or parsed[0] != ac_id:
            continue
        if parsed[1] == criterion_hash(ac_id, ac["text"]):
            return "accepted"
        state = "stale"
    return state


# --- artifacts --------------------------------------------------------------------------------------------------


def artifact_event(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    typ, a, name = e["type"], e["actor"], e["name"]
    if t.status in ("done", "closed"):
        return Refusal(Code.TICKET_FROZEN, f"{typ} is refused on {t.status} tickets")
    unattended = a["kind"] == "agent" and a.get("unattended") is True
    if e["kind"] == "feedback" and a["kind"] != "person":
        return Refusal(Code.ARTIFACT_KIND, "feedback only from a person")
    if e["kind"] == "receipt" and (typ == "artifact.replaced" or "task" not in e):
        return Refusal(Code.ARTIFACT_KIND, "a receipt is made by task done --run (it names its task), never replaced")
    is_file = "sha256" in e
    if is_file and e["kind"] not in ARTIFACT_KINDS:
        return Refusal(Code.ARTIFACT_KIND, f"unknown core kind {e['kind']!r}")
    if not is_file:
        ad = ws.addons.get(e.get("addon", ""))
        if ad is None or not ad.enabled or ad.purged:
            return Refusal(Code.ADDON_UNKNOWN, str(e.get("addon")))
        if (r := addon_rules.check_artifact_kind(ad, e["kind"])) is not None:
            return r
    if "ac" in e and e["ac"] not in {x["id"] for x in t.fields["acceptance"]}:
        return Refusal(Code.TICKET_BAD_REFERENCE, f"unknown acceptance criterion {e['ac']}")
    if "task" in e and e["task"] not in task_ids(t):
        return Refusal(Code.TICKET_BAD_REFERENCE, f"unknown task {e['task']}")
    old = t.artifacts.get(name)
    if typ == "artifact.added":
        if old is not None:
            return Refusal(Code.ARTIFACT_EXISTS, name)
    else:
        if old is None:
            return Refusal(Code.ARTIFACT_UNKNOWN, name)
        if not is_file or old.digest != e["replaces"]:
            return Refusal(Code.ARTIFACT_BAD_REPLACES, "`replaces` must be the current digest of this name")
    by = a["session"] if a["kind"] == "agent" else a["id"]
    counts = is_file and not unattended
    if (
        e["kind"] == "feedback"
        and (e.get("label") or "").startswith("accepted ")
        and not (a["kind"] == "person" and may_accept(ws, t, a["id"]) and t.status in WORKABLE)
    ):
        counts = False  # R3: a manual acceptance of anyone else, or on a ticket not open for work, is never evidence
    withdrawn = parse_withdraw_label(e.get("label"))
    if (
        typ == "artifact.replaced"
        and e["kind"] == "feedback"
        and "ac" not in e
        and withdrawn in {x["id"] for x in t.fields["acceptance"]}
        and a["kind"] == "person"
        and may_accept(ws, t, a["id"])
        and t.status in WORKABLE
    ):
        for art in t.artifacts.values():
            if art.kind == "feedback" and art.ac == withdrawn and (art.label or "").startswith("accepted "):
                art.evidence_ok = False
    t.artifacts[name] = ArtifactCore(
        name,
        e["kind"],
        e.get("sha256"),
        e.get("bytes"),
        e.get("ac"),
        e.get("task"),
        e.get("label"),
        e.get("addon"),
        e.get("ref"),
        e["id"],
        by,
        counts,
    )
    generations.mark(t, "verify")
    for g in ("requirements", "plan"):
        if name in gates.gate_refs(ws, t, g):
            generations.mark(t, g)
    return None


_NO_SHELL = (
    "a verify command runs without a shell, so {op} would be passed to the program as plain text. "
    "Use one plain command, or wrap the whole thing: sh -c '...'"
)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


def verify_cmd_problem(cmd: str) -> str | None:
    """Why ``cmd`` cannot work as a verify command, or ``None``. A verify command runs WITHOUT a shell (``shlex``
    split, then exec), so an unquoted ``&&``, ``|``, ``;``, ``>``, ``<``, ``&``, a backtick or ``$(`` would be handed
    to the program as plain arguments and fail at run time. Refusing them when the command is written is clearer than
    a crash later. ``sh -c '...'`` is the way to ask for a shell, and its script is not looked at."""
    import shlex

    try:
        argv = shlex.split(cmd)
    except ValueError:
        return "it has an unbalanced quote"
    if not argv:
        return "it is empty"
    if argv[0].rpartition("/")[2] in _SHELLS:
        return None
    quote = ""
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = ""
            elif ch == "`" or cmd.startswith("$(", i):  # a substitution nobody performs: it would arrive as text
                return _NO_SHELL.format(op="$(" if ch == "$" else ch)
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = ch
        elif ch in "&|;<>`" or cmd.startswith("$(", i):
            op = ch if ch in "`" else cmd[i : i + 2] if cmd[i : i + 2] in ("&&", "||", ">>", "<<", "$(") else ch
            return _NO_SHELL.format(op=op)
        i += 1
    return None
