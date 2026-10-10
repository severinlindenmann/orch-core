"""Tasks and leases (A4), artifacts and evidence (ticket-format §6).

A task lives in ``ticket.json`` (its definition) and in events (its state). A done task always has a holder: the
session that held its lease, or took it implicitly by finishing it. Evidence is bound into the verify gate hash.
"""

from __future__ import annotations

from typing import Any

from orch.canon import ARTIFACT_KINDS

from . import claims, gates, generations, source
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
    for name, art in sorted(t.artifacts.items()):
        if art.evidence_ok and art.ac in out:
            out[art.ac].append(f"artifact:{name}")
    for task in t.fields["tasks"]:
        tk = t.tasks.get(task["id"])
        if tk is None or tk.state != "done" or tk.receipt is None or tk.receipt["exit"] != 0:
            continue
        repo = tk.receipt["repo"]
        if repo is not None and tk.receipt["commit"] != source.repo_sha(t, repo):
            continue
        for ac in task["proves"]:
            if ac in out:
                out[ac].append(f"task:{task['id']}")
    return out


# --- artifacts --------------------------------------------------------------------------------------------------


def artifact_event(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    typ, a, name = e["type"], e["actor"], e["name"]
    if t.status in ("done", "closed"):
        return Refusal(Code.TICKET_FROZEN, f"{typ} is refused on {t.status} tickets")
    unattended = a["kind"] == "agent" and a.get("unattended") is True
    if e["kind"] == "feedback" and a["kind"] != "person":
        return Refusal(Code.ARTIFACT_KIND, "feedback only from a person")
    is_file = "sha256" in e
    if is_file and e["kind"] not in ARTIFACT_KINDS:
        return Refusal(Code.ARTIFACT_KIND, f"unknown core kind {e['kind']!r}")
    if not is_file:
        ad = ws.addons.get(e.get("addon", ""))
        if ad is None or not ad.enabled or ad.purged:
            return Refusal(Code.ADDON_UNKNOWN, str(e.get("addon")))
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
        is_file and not unattended,
    )
    generations.mark(t, "verify")
    for g in ("requirements", "plan"):
        if name in gates.gate_refs(ws, t, g):
            generations.mark(t, g)
    return None
