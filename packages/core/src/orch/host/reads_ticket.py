"""H6b reads of one ticket: ``ticket.get`` (§0.8, §0.8.4, with every v1.4 ``read_ext/1`` and v1.5 ``read_ext/2``
field, §0.8.5) and ``ticket.text`` (§0.8.5, cap ``text/1``).

Both resolve the ref through :meth:`ReadCtx.ticket` (a ticket the viewer may not see is ``not_found``), and read only
that ticket's verified log (``store.events``) and its replayed state: the frozen view for what the model derives
(gates, tasks, criteria, questions, source list, claim), the replay core (``State._core``) for the few values the view
does not carry (a task's stored receipt, an artifact's ``bytes``/``label``/``addon``/``ref``, the claim's
``last_activity``, the observed branch heads), and the events for ``at``, ``seq``, actors and texts. Nothing is read
from a client path, git or ``ticket.json`` on disk; no ``sig``/``host_sig`` or old section text is returned.

**``TicketDoc``** (F1 §7, "the ticket document") is built here from the verified state: the ``ticket.json`` keys of
F1 §3 (``visibility`` with its person list, which only this op returns; ``links``; ``addons``), merged with the
derived state ``orch show --json`` gives (``status``, ``owner``, ``people``, ``claim``, ``waiting``, criteria with
their evidence refs, tasks with state, questions with ``answered``) and the section texts of ``body.md`` (checked
against the log by :meth:`Store.body_sections`).
"""

from __future__ import annotations

import json
from typing import Any

from orch import canon
from orch.model import source as model_source
from orch.model import tasks as model_tasks
from orch.model import visibility as model_visibility
from orch.schema import SECTIONS_BY_TYPE
from orch.store.render import HEADINGS, thaw

from . import readmodel
from .errors import HostError
from .frames import MAX_FRAME
from .reads import ReadCtx, handler, iso

__all__ = ["DECISIONS_MAX", "TEXT_FIELDS"]

DECISIONS_MAX = 100
PUSHES_MAX = 50
SECTION_REVS_MAX = 20
TAKEOVERS_MAX = 20
ARTIFACTS_MAX = 1000
#: room for the ``res`` envelope around ``result.data`` when judging the frame limit
_ENVELOPE = 2048
#: the largest ``ticket.get`` data (bytes of JSON); a name of its own so a test can lower it
RESULT_MAX = MAX_FRAME - _ENVELOPE

_KIND = {"approve": "approved", "changes": "changes_requested", "pass": "pass", "fail": "fail"}
_TASK_EVENTS = frozenset({"task.started", "task.done", "task.skipped", "task.blocked", "task.reopened"})

#: ``ticket.text``: the event types that serve each field (§0.8.5 pairs table)
TEXT_FIELDS: dict[str, frozenset[str]] = {
    "text": frozenset(
        {
            "log.added",
            "handoff.written",
            "ticket.closed",
            "ticket.reopened",
            "question.answered",
            "gate.changes_requested",
            "verdict.given",
            "task.done",
        }
    ),
    "reason": frozenset({"task.skipped", "task.blocked", "task.reopened", "status.changed", "ticket.moved"}),
}


def _digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value.startswith("sha256:") else f"sha256:{value}"


def _sources(raw: Any) -> list[dict[str, Any]]:
    return [
        {"repo": s.get("repo"), "ref": s.get("ref"), "sha": s.get("sha")} for s in (raw or []) if isinstance(s, dict)
    ]


def _attested_by(event: dict[str, Any] | None) -> str:
    """``person``, ``agent`` or ``unattended``: who stands behind an artifact (from its adding event's actor)."""
    actor = (event or {}).get("actor") or {}
    if actor.get("kind") == "person":
        return "person"
    return "unattended" if actor.get("unattended") is True else "agent"


class _Ticket:
    """What one ``ticket.get`` reads, once: the view, the replay core's ticket, the log and its indexes."""

    def __init__(self, ctx: ReadCtx, view: Any) -> None:
        self.ctx = ctx
        self.view = view
        core = ctx.store.state._core  # the replay core: see the module docstring for what is read from it
        self.ws = core.ws
        self.t = core.tickets[view.uid]
        self.events: list[dict[str, Any]] = ctx.store.events(view.uid)
        self.by_id = {e["id"]: e for e in self.events}
        self.by_seq = {e["seq"]: e for e in self.events}

    def can_see(self, person: str) -> bool:
        return model_visibility.can_see(self.ws, self.t, person)

    def managers(self) -> list[str]:
        return sorted(p for p, m in self.ws.members.items() if m.role in ("owner", "maintainer") and self.can_see(p))

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] in types]


# --- doc ------------------------------------------------------------------------------------------------------------


def _doc(x: _Ticket) -> dict[str, Any]:
    view = x.view
    texts = x.ctx.store.body_sections(view.uid)
    allowed = SECTIONS_BY_TYPE[view.type]
    fields = thaw(view.fields)
    verify = {t["id"]: t.get("verify") for t in fields["tasks"]}
    assignee = {t["id"]: t["assignee"] for t in fields["tasks"] if "assignee" in t}
    answered = {q.id: q.answered for q in view.questions}
    tasks = []
    for tk in view.tasks:
        row = {"id": tk.id, "text": tk.text, "state": tk.state, "proves": list(tk.proves), "verify": verify.get(tk.id)}
        if tk.id in assignee:
            row["assignee"] = assignee[tk.id]
        tasks.append(row)
    return {
        "uid": view.uid,
        "key": view.key,
        "title": view.title,
        "type": view.type,
        "status": view.status,
        "priority": fields["priority"],
        "size": fields["size"],
        "labels": list(fields["labels"]),
        "due": fields["due"],
        "parent": fields["parent"],
        "blocked_by": list(fields["blocked_by"]),
        "owner": view.owner,
        "people": {r: list(view.people.get(r, ())) for r in ("assignees", "reviewers", "watchers")},
        "visibility": thaw(view.visibility),
        "links": fields["links"],
        "addons": fields["addons"],
        "claim": {"session": view.claim.session, "live": view.claim.live} if view.claim else None,
        "waiting": view.waiting,
        "acceptance": [{"id": a.id, "text": a.text, "evidence": list(a.evidence)} for a in view.acceptance],
        "tasks": tasks,
        "questions": [{**q, "answered": answered.get(q["id"], False)} for q in fields["questions"]],
        "current_state": texts.get("current_state", ""),
        "sections": {s: texts.get(s, "") for s in HEADINGS if s in allowed},
        "head": _head(x),
    }


def _head(x: _Ticket) -> dict[str, Any]:
    """``doc.head`` (v1.7, §11A.14): ``{seq, head}`` of the log's last event, from the same read as the rest of the
    snapshot. A phone signs ``head`` as its answer's ``based_on`` (the reviewed head, F1 §5.1)."""
    last = x.events[-1]
    return {"seq": last["seq"], "head": canon.event_head(last)}


# --- log ------------------------------------------------------------------------------------------------------------


def _log(x: _Ticket, since: int, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """``LogItem``s after ``since`` (at most ``limit``) and ``log_more``. A ``ticket.updated`` summary names the
    criteria and tasks it added or edited against their state before the first returned event."""
    store, uid = x.ctx.store, x.view.uid
    tail = store.events(uid, after=since, limit=limit + 1)
    more = len(tail) > limit
    tail = tail[:limit]
    names = {n for n in ("acceptance", "tasks") if any(f"ticket.{n}" in e.get("set", {}) for e in tail)}
    state = store.event_fields_before(uid, since, names) if since and names else {"acceptance": {}, "tasks": {}}
    items = [
        {
            "seq": e["seq"],
            "type": e["type"],
            "at": e["at"],
            "actor": readmodel.actor(e, x.ws),
            "summary": readmodel.summary(e, state),
            "data": readmodel.log_data(e),
            **readmodel.hub_row(e),
        }
        for e in tail
    ]
    return items, more


# --- questions, criteria, tasks -------------------------------------------------------------------------------------


def _signer(event: dict[str, Any]) -> dict[str, str] | None:
    """``signer: {device, auth}`` of a person-signed event (§0.8.6 item 9.11), only when the production parser of a
    level-1 reader (:func:`orch.identity.reader.parse_signer`) accepts it: never an unknown ``auth``."""
    from orch.identity.reader import parse_signer

    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    signer = {"device": actor.get("device"), "auth": event.get("auth")}
    try:
        parse_signer("ticket.get.questions.answer", {"answer": {"signer": signer}})
    except ValueError:
        return None
    return signer  # type: ignore[return-value]


def _questions(x: _Ticket) -> list[dict[str, Any]]:
    out = []
    for q in x.view.questions:
        core = x.t.questions[q.id]
        d = core.question
        resolved = [p for p in q.addressees if x.can_see(p)] or x.managers()
        answer_event = x.by_id.get(core.answer["event"]) if core.answer else None
        state = "answered" if answer_event is not None else "open"
        if answer_event is not None and answer_event.get("hash") != core.hash:
            state, answer_event = "stale", None  # an answer to an older question hash is never returned
        item: dict[str, Any] = {
            "id": q.id,
            "state": state,
            "to": resolved,
            "blocking": q.blocking,
            "answered_by": core.answer["by"] if answer_event is not None else None,
            "answered_seq": answer_event["seq"] if answer_event is not None else None,
            "text": d["text"],
        }
        for k in ("why", "recommended"):
            if d.get(k):
                item[k] = d[k]
        item["to_ref"] = d["to"]
        if d.get("options"):
            item["options"] = [{k: o[k] for k in ("key", "label", "cost") if k in o} for o in d["options"]]
        asked = x.by_seq.get(core.asked_seq)
        if asked is not None:
            item.update({"asked_at": asked["at"], "asked_by": readmodel.actor(asked), "asked_seq": asked["seq"]})
        if answer_event is not None:
            answer = {
                "by": core.answer["by"],
                "at": answer_event["at"],
                "seq": answer_event["seq"],
                "option": core.answer.get("option"),
                "text": core.answer.get("text"),
            }
            if isinstance(answer_event.get("auth"), str):
                answer["auth"] = answer_event["auth"]
            signer = _signer(answer_event)
            if signer is not None:  # H12d-2 (§0.8.6 item 9.11): READER_VIEWS_1, ticket.get.questions.answer
                answer["signer"] = signer
            answer["id"] = answer_event["id"]  # v1.7 (§11A.14): the counting event's id
            item["answer"] = answer
        item["qid"] = core.qid
        item["hash"] = core.hash
        out.append(item)
    return out


def _receipt_counts(x: _Ticket, task_id: str) -> bool:
    """Does the task's receipt count as evidence now (F1 §6: exit 0 and ``commit`` = the current source ``sha`` of
    its ``repo``, or ``repo`` null)? The rule of :func:`orch.model.tasks.ac_evidence`, for one task."""
    tk = x.t.tasks.get(task_id)
    if tk is None or tk.state != "done" or tk.receipt is None or tk.receipt["exit"] != 0:
        return False
    repo, commit = tk.receipt["repo"], tk.receipt["commit"]
    return repo is None or (commit is not None and commit == model_source.repo_sha(x.t, repo))


def _manual_acceptance(x: _Ticket, ac_id: str, text: str) -> dict[str, Any]:
    from orch.host.acceptance import artifact_statement
    from orch.host.artifacts import artifact_file
    from orch.model.accept import criterion_hash, parse_label

    stale = False
    for art in sorted(x.t.artifacts.values(), key=lambda a: a.event, reverse=True):
        if art.ac != ac_id or art.kind != "feedback" or parse_label(art.label) is None:
            continue
        if type(art.bytes) is not int or art.bytes > 8192:  # a statement is a few lines: never read a big file
            continue
        path = artifact_file(x.ctx.service, x.view.uid, art.name)
        try:
            data = x.ctx.service.capabilities().files.read_bytes(path)
        except OSError:
            continue
        statement = artifact_statement(art, data)
        if statement is None:
            continue
        if statement["hash"] != criterion_hash(ac_id, text):
            stale = True
            continue
        event = x.by_id.get(art.event)
        if event is None or event.get("actor", {}).get("kind") != "person":
            continue
        accepted = {"by": art.by, "at": event["at"], "note": statement["note"], "statement_ok": True}
        if "auth" in event:
            accepted["auth"] = event["auth"]
        return {"proof": "accepted", "accepted": accepted}
    return {"accepted_stale": True} if stale else {}


def _criteria(x: _Ticket) -> list[dict[str, Any]]:
    manifest = model_tasks.ac_evidence(x.ws, x.t)
    out = []
    for a in x.view.acceptance:
        evidence = []
        for ref in a.evidence:
            kind, _, name = ref.partition(":")
            art = x.t.artifacts.get(name) if kind == "artifact" else None
            if art is not None and art.digest is not None:
                evidence.append(
                    {
                        "artifact": art.name,
                        "digest": _digest(art.digest),
                        "kind": art.kind,
                        "attested_by": _attested_by(x.by_id.get(art.event)),
                    }
                )
        proof = (
            "proven"
            if any(
                ref.startswith("task:") or (ref.startswith("artifact:") and x.t.artifacts[ref[9:]].kind == "receipt")
                for ref in manifest.get(a.id, [])
            )
            else "asserted"
            if manifest.get(a.id)
            else "pending"
        )
        manual = _manual_acceptance(x, a.id, a.text)
        if proof == "proven":  # a receipt is the stronger proof: the manual acceptance does not replace its state
            manual.pop("proof", None)
        out.append(
            {
                "id": a.id,
                "text": a.text,
                "evidence": evidence,
                "covered": bool(manifest.get(a.id)),
                "proof": proof,
                **manual,
                "tasks": [
                    {"id": tk.id, "evidence": f"task:{tk.id}" in manifest.get(a.id, [])}
                    for tk in x.view.tasks
                    if a.id in tk.proves
                ],
            }
        )
    return out


def _tasks(x: _Ticket) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for e in x.events:
        if e["type"] in _TASK_EVENTS:
            latest[e["task"]] = e
    out = []
    for tk in x.view.tasks:
        last = latest.get(tk.id)
        core = x.t.tasks.get(tk.id)
        done = x.by_id.get(core.done_event) if core is not None and core.done_event else None
        receipt = None
        if core is not None and core.receipt is not None and done is not None:
            r = core.receipt
            receipt = {
                "cmd": r.get("cmd"),
                "exit": r.get("exit"),
                "ms": r.get("ms", r.get("duration_ms")),
                "repo": r.get("repo"),
                "commit": r.get("commit"),
                "log": done.get("log"),
                "seq": done["seq"],
                "at": done["at"],
                "evidence": _receipt_counts(x, tk.id),
            }
            if r.get("attested_by") is not None:
                receipt["attested_by"] = r["attested_by"]
        out.append(
            {
                "id": tk.id,
                "text": tk.text,
                "state": tk.state if tk.state in ("open", "done", "skipped", "blocked") else "open",
                "proves": list(tk.proves),
                "leased_by": tk.leased_by,
                "by": readmodel.actor(last) if last else None,
                "at": last["at"] if last else None,
                "reason": last.get("reason") if last else None,
                "receipt": receipt,
            }
        )
    return out


# --- gates, verdict -------------------------------------------------------------------------------------------------


def _decision(x: _Ticket, d: Any) -> dict[str, Any] | None:
    e = x.by_id.get(d.id)
    if e is None:
        return None
    item: dict[str, Any] = {
        "id": d.id,
        "kind": _KIND[d.kind],
        "person": d.person,
        "at": e["at"],
        "seq": e["seq"],
        "gen": d.gen,
        "counting": d.counting,
        "voided": d.voided,
    }
    if isinstance(e.get("auth"), str):
        item["auth"] = e["auth"]
    readmodel.put_snip(item, "text", e.get("text"))
    if d.source_sha is not None:
        item["source_sha"] = _sources(thaw(d.source_sha))
    item["hash"] = d.hash
    item["policy_hash"] = d.policy_hash
    return item


def _gates(x: _Ticket) -> dict[str, dict[str, Any]]:
    out = {}
    for name, g in x.view.gates.items():
        full = g.hash
        decisions = [item for d in g.decisions if (item := _decision(x, d)) is not None]
        gate: dict[str, Any] = {
            "state": "n/a" if not g.applies else "approved" if g.approved else "open",
            "gen": g.gen,
            "hash12": full.removeprefix("sha256:")[:12] if full else None,
            "applies": g.applies,
            "waiting": g.waiting,
            "blocked": g.blocked,
            "count": g.needed,
            "approved_by": list(g.counting),
            "eligible": [p for p in g.eligible if x.can_see(p)],
            "policy": thaw(g.policy),
            "missing": list(g.missing),
            "decisions": decisions[-DECISIONS_MAX:],
        }
        if len(decisions) > DECISIONS_MAX:
            gate["decisions_more"] = True
        gate["revoked_decisions"] = list(g.revoked_decisions)
        gate["hash"] = full or None
        if name in ("requirements", "plan") and g.input is not None:
            from .seqgates import single_only

            if single_only(thaw(g.input)):  # display only: the combined approval cannot carry files or add-on fields
                gate["single_only"] = True
        gate["policy_hash"] = None if g.blocked or not full else g.policy_hash
        out[name] = gate
    return out


def _verdict(x: _Ticket) -> dict[str, Any] | None:
    given = x.of_type("verdict.given")
    if not given:
        return None
    e = given[-1]
    verify = x.view.gates["verify"]
    d = next((d for d in verify.decisions if d.id == e["id"]), None)
    out: dict[str, Any] = {
        "outcome": e["outcome"],
        "person": e["actor"].get("id"),
        "at": e["at"],
        "seq": e["seq"],
        "gen": e["gate_gen"],
        "text": None,
        "source_sha": _sources(e.get("source_sha")),
        "current": bool(d is not None and d.gen == verify.gen and d.counting),
        "counting": bool(d is not None and d.counting),
        "voided": bool(d is not None and d.voided),
    }
    readmodel.put_snip(out, "text", e.get("text"))
    if isinstance(e.get("auth"), str):
        out["auth"] = e["auth"]
    return out


# --- artifacts, claim, source, pushes, sections --------------------------------------------------------------------


def _artifacts(x: _Ticket) -> tuple[list[dict[str, Any]], bool]:
    replaced: dict[str, int] = {}
    for e in x.of_type("artifact.replaced"):
        replaced[e["name"]] = replaced.get(e["name"], 0) + 1
    names = sorted(x.t.artifacts)
    out = []
    for name in names[:ARTIFACTS_MAX]:
        a = x.t.artifacts[name]
        e = x.by_id[a.event]
        item: dict[str, Any] = {
            "name": a.name,
            "kind": a.kind,
            "digest": _digest(a.digest),
            "bytes": a.bytes if a.digest is not None else None,
            "ac": a.ac,
            "task": a.task,
            "label": a.label,
        }
        if a.addon is not None:
            item["addon"] = a.addon
        if a.ref is not None:
            item["ref"] = a.ref
        item.update(
            {
                "evidence": a.evidence_ok,
                "by": readmodel.actor(e),
                "at": e["at"],
                "seq": e["seq"],
                "replaced": replaced.get(name, 0),
            }
        )
        out.append(item)
    return out, len(names) > ARTIFACTS_MAX


def _claim(x: _Ticket) -> dict[str, Any] | None:
    c, v = x.t.claim, x.view.claim
    if c is None or v is None:
        return None
    taken = next((e for e in reversed(x.of_type("claim.taken")) if e["actor"].get("session") == c.session), None)
    takeovers = []
    for tk in x.t.takeovers:
        e = x.by_id.get(tk["event"])
        if e is not None:
            takeovers.append(
                {
                    "at": e["at"],
                    "session": e["actor"].get("session"),
                    "from_session": tk["from_session"],
                    "reason": tk["reason"],
                }
            )
    out: dict[str, Any] = {
        "session": c.session,
        "agent": ((taken or {}).get("actor") or {}).get("id") or "",
        "for": c.for_person,
        "grant": c.grant,
        "since": iso(c.taken_at),
        "last_at": iso(c.last_activity),
        "live": v.live,
        "lapsed": v.lapsed,
        "takeovers": takeovers[-TAKEOVERS_MAX:],
    }
    if len(takeovers) > TAKEOVERS_MAX:
        out["takeovers_more"] = True
    out["idle_until"] = iso(c.last_activity + int(x.ws.settings["claim_ttl_min"]) * 60)
    return out


def _source(x: _Ticket) -> list[dict[str, Any]]:
    heads, linked = x.t.branch_heads, model_source.linked_repos(x.t)
    out = []
    for s in x.view.source_list:
        key = (s["repo"], s["ref"], s["sha"])
        name = next(
            (n for n in linked if (h := heads.get(n)) is not None and (h["repo_id"], h["ref"], h["sha"]) == key),
            s["repo"],
        )
        out.append({"name": name, "repo": s["repo"], "ref": s["ref"], "sha": s["sha"]})
    return out


def _pushes(x: _Ticket) -> tuple[list[dict[str, Any]], bool]:
    pushed = x.of_type("branch.pushed")
    rows = [
        {
            "seq": e["seq"],
            "at": e["at"],
            "repo_name": e["repo_name"],
            "repo_id": e["repo_id"],
            "ref": e["ref"],
            "sha": e["sha"],
            "before": e["before"] if isinstance(e.get("before"), dict) else None,
        }
        for e in pushed[-PUSHES_MAX:]
    ]
    return rows, len(pushed) > PUSHES_MAX


def _sections(x: _Ticket) -> tuple[dict[str, list[dict[str, Any]]], dict[str, bool]]:
    """Each edited section id -> its edits, oldest first, the newest 20 (hashes only: F1 keeps no old texts)."""
    revs: dict[str, list[dict[str, Any]]] = {}
    for e in x.of_type("ticket.updated", "edit.external"):
        for sid, entry in (e.get("sections") or {}).items():
            revs.setdefault(sid, []).append(
                {
                    "seq": e["seq"],
                    "at": e["at"],
                    "hash": entry["hash"] if isinstance(entry, dict) else None,
                    "actor": readmodel.actor(e),
                    "via": "external" if e["type"] == "edit.external" else "edit",
                    "refs": sorted(entry.get("refs", [])) if isinstance(entry, dict) else [],
                }
            )
    more = {sid: True for sid, r in revs.items() if len(r) > SECTION_REVS_MAX}
    return {sid: r[-SECTION_REVS_MAX:] for sid, r in revs.items()}, more


def _closed(x: _Ticket) -> dict[str, Any] | None:
    if x.view.status != "closed":
        return None
    closing = None
    for e in x.of_type("ticket.closed", "ticket.reopened"):
        closing = e if e["type"] == "ticket.closed" else None
    if closing is None:
        return None
    return {
        "resolution": closing.get("resolution", "other"),
        "duplicate_of": closing.get("duplicate_of"),
        "at": closing["at"],
        "by": closing["actor"].get("id"),
        "text": closing.get("text"),
        "seq": closing["seq"],
        "event_id": closing["id"],
    }


# --- ticket.get -----------------------------------------------------------------------------------------------------


@handler("ticket.get")
def ticket_get(ctx: ReadCtx) -> dict[str, Any]:
    # v1.7 (§11A.14): one snapshot under one store lock (re-entrant), so `doc.head`, `questions[].answer.id` and
    # `members_v` are of the same state as everything else a phone reviews before it signs
    with ctx.store.locked():
        return _ticket_get(ctx)


def _ticket_get(ctx: ReadCtx) -> dict[str, Any]:
    cursor = ctx.sub_cursor()  # the journal head right before the state is read (H6a)
    view = ctx.ticket(ctx.args["ref"])
    x = _Ticket(ctx, view)
    log, log_more = _log(x, ctx.args.get("log_since", 0), ctx.args.get("log_limit", 100))
    artifacts, artifacts_more = _artifacts(x)
    pushes, pushes_more = _pushes(x)
    sections, sections_more = _sections(x)
    out: dict[str, Any] = {
        "doc": _doc(x),
        "log": log,
        "log_more": log_more,
        "questions": _questions(x),
        "criteria": _criteria(x),
        "gates": _gates(x),
        "sub_cursor": cursor,
        "status": view.status,
        "settled": view.status in ("done", "closed"),
        "members_v": x.ws.roster_v,  # v1.7 (§11A.14): the roster version a person event signs as roster_v
    }
    closed = _closed(x)
    if closed is not None:
        out["closed"] = closed
    out["tasks"] = _tasks(x)
    out["verdict"] = _verdict(x)
    out["artifacts"] = artifacts
    if artifacts_more:
        out["artifacts_more"] = True
    out["claim"] = _claim(x)
    out["source"] = _source(x)
    out["pushes"] = pushes
    if pushes_more:
        out["pushes_more"] = True
    out["sections"] = sections
    if sections_more:
        out["sections_more"] = sections_more
    size = len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > RESULT_MAX:
        raise HostError(
            "result.too_large",
            f"ticket.get is {size} bytes, over the {MAX_FRAME} byte frame",
            hint="Use a smaller log_limit (the log is the largest part); pass log_since to read on.",
        )
    return out


# --- ticket.text ----------------------------------------------------------------------------------------------------


@handler("ticket.text")
def ticket_text(ctx: ReadCtx) -> dict[str, Any]:
    """The full stored text of one field of one event of the ticket's verified log, in UTF-8 byte chunks that never
    split a scalar value (§0.8.5)."""
    a = ctx.args
    offset, length, field = a.get("offset", 0), a.get("length", 262144), a["field"]
    if offset > 0 and "event_id" not in a:
        raise HostError("invalid.input", "ticket.text: offset > 0 needs the event_id of the first response")
    view = ctx.ticket(a["ref"])
    found = ctx.store.events(view.uid, after=a["seq"] - 1, limit=1)
    event = found[0] if found and found[0]["seq"] == a["seq"] else None
    if event is None or ("event_id" in a and a["event_id"] != event["id"]):
        raise HostError("not_found", "no such event")
    if event["type"] not in TEXT_FIELDS[field] or not isinstance(event.get(field), str):
        raise HostError("not_found", f"event {event['seq']} has no {field}")
    raw = event[field].encode("utf-8")
    total = len(raw)
    if offset > total:
        raise HostError("invalid.input", f"ticket.text: offset {offset} is past the {total} bytes of the text")
    if offset < total and raw[offset] & 0xC0 == 0x80:
        raise HostError("invalid.input", f"ticket.text: offset {offset} is inside a character")
    end = min(offset + length, total)
    while end < total and raw[end] & 0xC0 == 0x80:  # back to a scalar boundary (a scalar is at most 4 bytes)
        end -= 1
    return {
        "uid": view.uid,
        "event_id": event["id"],
        "type": event["type"],
        "at": event["at"],
        "actor": readmodel.actor(event),
        "field": field,
        "text": raw[offset:end].decode("utf-8"),
        "bytes": total,
        "offset": offset,
        "next": end if end < total else None,
    }
