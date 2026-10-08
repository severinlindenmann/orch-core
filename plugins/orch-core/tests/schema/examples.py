"""Valid example documents. The ones from the format doc have its ellipses replaced by well-formed values."""

import copy

U1 = "01J9ZK4Q7M3R8T2V6X0B5N1C9D"
H = "sha256:" + "fa37" * 16
H2 = "sha256:" + "aa91" * 16
SIG = "p256:MEUCIQDabcdefghijklmnopqrstuvwxyz0123456789ABCD"

WORKSPACE = {
    "schema": "orch.workspace/2",
    "workspace": {"id": "6f1c0d2e-8b4a-4e1f-9c3d-2a7b5e9f0c11", "prefix": "DEMO", "name": "Acme energy data"},
    "host": {"id": "h_01J9Z7", "wsk_pub": SIG.replace("MEUC", "MFkw")},
    "members": [
        {"person": "p_sev", "name": "Severin", "role": "owner"},
        {"person": "p_mara", "name": "Mara", "role": "maintainer"},
        {"person": "p_tom", "name": "Tom", "role": "viewer"},
    ],
    "gates": {
        "requirements": {"approvers": "owner", "count": 1},
        "plan": {"approvers": "owner", "count": 1},
        "verify": {"approvers": "reviewers", "count": 1, "not": "assignees"},
    },
    "agents": {"run_for": ["owner", "maintainer", "member"]},
    "addons": {"dashboard": {"enabled": True}, "estimate": {"enabled": True}, "publish": {"enabled": False}},
}

KEYS_LINE = {"key": "DEMO-0043", "uid": U1}

TICKET = {
    "schema": "orch.ticket/2",
    "uid": U1,
    "key": "DEMO-0043",
    "title": "Load tariff tables as dbt seeds",
    "type": "feature",
    "priority": "high",
    "size": "m",
    "labels": ["dbt", "tariffs"],
    "parent": "DEMO-0040",
    "blocked_by": [],
    "due": None,
    "visibility": "workspace",
    "links": {
        "repos": ["acme-energy-dbt"],
        "branches": {"acme-energy-dbt": "feat/DEMO-0043-tariff-seeds"},
        "prs": [{"repo": "acme-energy-dbt", "url": "https://github.com/acme/energy-dbt/pull/31"}],
        "external": [],
    },
    "acceptance": [
        {"id": "AC1", "text": "`dbt seed` loads all 40 tariff tables without errors"},
        {"id": "AC2", "text": "Model `fct_billing` joins the seeds and its tests pass"},
        {"id": "AC3", "text": "The refresh command is documented in README"},
    ],
    "tasks": [
        {
            "id": "T1",
            "text": "Export CSVs into seeds/tariffs",
            "verify": {"cmd": "ls seeds/tariffs | wc -l"},
            "proves": [],
        },
        {
            "id": "T2",
            "text": "Seed configs with types",
            "verify": {"cmd": "dbt seed --select tariffs"},
            "proves": ["AC1"],
        },
        {
            "id": "T3",
            "text": "Join in fct_billing, add tests",
            "verify": {"cmd": "dbt test --select fct_billing"},
            "proves": ["AC2"],
            "assignee": "p_sev",
        },
        {"id": "T4", "text": "Document the refresh command", "verify": None, "proves": ["AC3"], "assignee": "p_mara"},
    ],
    "questions": [
        {
            "id": "Q1",
            "to": "p_mara",
            "text": "Which tariff export is the source of truth, the monthly CSV or the API?",
            "why": "The two differ for 3 of 40 tariffs; the seed must pick one.",
            "options": [
                {"key": "csv", "label": "Monthly CSV"},
                {"key": "api", "label": "Tariff API", "cost": "+1 day"},
            ],
            "recommended": "csv",
            "blocking": True,
        }
    ],
    "addons": {"estimate": {"points": 5}},
}

BODY_FEATURE = {
    "type": "feature",
    "sections": {
        "context": "Files: seeds/.",
        "requirements": "Load the tables.",
        "out_of_scope": "Other warehouses.",
        "plan": "T1..T4",
        "decisions": "CSV, not the API.",
        "verification": "dbt test",
        "current_state": "T2 next.",
    },
}

AGENT = {"kind": "agent", "id": "claude-code", "session": "s_77c2", "for": "p_sev", "grant": "gr_01J9Z8"}
PERSON = {"kind": "person", "id": "p_sev", "device": "d_mac"}


SIGNED = {
    "gate.approved",
    "gate.changes_requested",
    "verdict.given",
    "question.answered",
    "ticket.closed",
    "ticket.reopened",
    "people.changed",
    "member.added",
    "member.removed",
    "role.changed",
    "policy.changed",
    "addon.granted",
    "addon.purged",
    "session.granted",
    "restore",
}


def _env(t, actor, seq=5, **kw):
    e = {
        "v": 2,
        "id": "01J9ZPABCDEFGHJKMNPQRSTVWX",
        "seq": seq,
        "at": "2026-10-09T09:10:11Z",
        "type": t,
        "actor": actor,
        "based_on": H2,
        "prev": H2,
        "hash_v": 1,
        "host_sig": SIG,
    }
    e.update(kw)
    if t in SIGNED:
        e.update(sig=SIG, presence="touchid")
    return e


def _no_actor(t, **kw):
    e = _env(t, AGENT, **kw)
    for k in ("actor",):
        del e[k]
    return e


# one valid example per core event type (the three from the doc first)
EVENTS = {
    "gate.approved": _env(
        "gate.approved", PERSON, gate="requirements", hash=H, policy_hash="sha256:" + "31c2" * 16, list_seq=7
    ),
    "task.done": _env("task.done", AGENT, seq=9, task="T2", receipt={"exit": 0, "ms": 38200, "commit": "b7e1f02"}),
    "artifact.added": _env(
        "artifact.added",
        AGENT,
        seq=10,
        name="seeds-in-warehouse.png",
        kind="screenshot",
        sha256="3f9a" * 16,
        bytes=84213,
        ac="AC1",
    ),
    "artifact.replaced": _env("artifact.replaced", AGENT, name="a.log", kind="log", sha256="3f9a" * 16, bytes=1),
    "ticket.created": _env("ticket.created", PERSON, key="DEMO-0043"),
    "ticket.updated": _env("ticket.updated", AGENT, base_rev="r12", fields=["title"], sections=["plan"]),
    "ticket.submitted": _env("ticket.submitted", AGENT),
    "status.changed": _env("status.changed", AGENT, **{"from": "in-progress", "to": "testing"}),
    "ticket.closed": _env("ticket.closed", PERSON, reason="done"),
    "ticket.reopened": _env("ticket.reopened", PERSON, reason="regression"),
    "people.changed": _env("people.changed", PERSON, role="reviewers", add=["p_mara"], list_seq=3),
    "gate.changes_requested": _env("gate.changes_requested", PERSON, gate="plan", hash=H, text="Split T3"),
    "verdict.given": _env("verdict.given", PERSON, hash=H, outcome="accepted"),
    "question.asked": _env("question.asked", AGENT, question="Q1"),
    "question.answered": _env("question.answered", PERSON, question="Q1", hash=H, answer="csv"),
    "claim.taken": _env(
        "claim.taken",
        AGENT,
        **{"for": "p_sev", "expires_at": "2026-10-09T18:00:00Z"},
        takeover={"from_session": "s_11aa", "reason": "stuck"},
    ),
    "claim.released": _env("claim.released", AGENT, reason="handoff"),
    "claim.expired": _env("claim.expired", AGENT),
    "task.started": _env("task.started", {**AGENT, "session": "s_77c2.1"}, task="T3"),
    "task.skipped": _env("task.skipped", AGENT, task="T3", reason="not needed"),
    "task.blocked": _env("task.blocked", AGENT, task="T3", reason="needs Q1"),
    "task.reopened": _env("task.reopened", AGENT, task="T3"),
    "log": _env("log", {"kind": "agent", "id": "claude-code", "session": "s_77c2", "unattended": True}, text="note"),
    "handoff": _env("handoff", AGENT, text="T3 half done"),
    "edit.external": _no_actor("edit.external", files=["body.md"], sections=["plan"], voided_gates=["plan"]),
    "projection.repaired": _no_actor("projection.repaired", path="ticket.json", fields=["uid"]),
    "restore": _env("restore", PERSON, from_seq=4, reason="restored backup"),
    "member.added": _env("member.added", PERSON, person="p_tom", name="Tom", role="viewer", list_seq=2),
    "member.removed": _env("member.removed", PERSON, person="p_tom", list_seq=3),
    "role.changed": _env("role.changed", PERSON, person="p_tom", role="member", list_seq=4),
    "policy.changed": _env(
        "policy.changed",
        PERSON,
        gates={"plan": {"approvers": "owner", "count": 2}},
        policy_hash="sha256:" + "31c2" * 16,
    ),
    "addon.granted": _env(
        "addon.granted", PERSON, name="estimate", version="1.0.0", package_sha256="ab" * 32, capabilities=["network"]
    ),
    "addon.purged": _env("addon.purged", PERSON, name="estimate"),
    "session.granted": _env(
        "session.granted", PERSON, grant="gr_01J9Z8", expires_at="2026-10-09T18:00:00Z", **{"for": "p_sev"}
    ),
}
ADDON_EVENT = _env("estimate.updated", AGENT, points=5)

ARTIFACT = {
    "name": "seeds-in-warehouse.png",
    "kind": "screenshot",
    "sha256": "3f9a" * 16,
    "bytes": 84213,
    "ac": "AC1",
    "label": "after",
    "task": "T2",
    "actor": AGENT,
}
ARTIFACT_ADDON = {"name": "board", "kind": "board", "addon": "dashboard", "ref": "boards/1", "actor": AGENT}

MANIFEST = {
    "schema": "orch.addon/2",
    "name": "estimate",
    "version": "1.0.0",
    "fields": {
        "points": {"type": "integer", "set_by": ["maintainer", "agent"], "gate": "plan", "filter": True, "show": True}
    },
    "sections": [
        {"id": "estimate", "title": "Estimate", "placement": {"after": "plan"}, "gate": "plan", "types": ["feature"]}
    ],
    "artifact_kinds": ["chart"],
    "needs": [{"id": "no-points", "when": "points == null", "text": "Estimate missing"}],
    "cli": {"group": "estimate", "ops": []},
    "skills": ["skills/estimate.md"],
    "agents_md": "estimate: orch estimate set N",
    "capabilities": ["network"],
}

OPERATION = {
    "name": "task.done",
    "input": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
    "who": "agent",
    "pre": ["session holds the claim", "task has no other lease"],
    "emits": ["task.done", "artifact.added"],
    "output": {"text": "ok {key} task.done {task} seq={seq}\nnext: {next}", "json": {"type": "object"}},
    "errors": [
        {
            "code": "conflict.section",
            "hint": "re-read the section",
            "fix": {"argv": ["orch", "show", "--section", "Plan"]},
        }
    ],
}

CHECKPOINT_TICKET = {"v": 2, "uid": U1, "seq": 9, "head": H, "host_sig": SIG}
CHECKPOINT_WORKSPACE = {"v": 2, "heads": {U1: {"seq": 9, "head": H}}, "host_sig": SIG}
CLI_RESULT = {"v": "orch.cli/2.0", "ok": True, "data": {}, "key": "DEMO-0043", "seq": 18, "cursor": 18, "hints": []}
CLI_ERROR = {"ok": False, "error": {"code": "human_only", "message": "approve is human-only", "retryable": False}}
WAIT_RESULT = {"kind": "timeout", "cursor": 14, "next": "orch wait"}

VALID = {
    "workspace": WORKSPACE,
    "keys-line": KEYS_LINE,
    "ticket": TICKET,
    "body": BODY_FEATURE,
    "event": EVENTS["gate.approved"],
    "artifact": ARTIFACT,
    "addon-manifest": MANIFEST,
    "operation": OPERATION,
    "checkpoint": CHECKPOINT_TICKET,
    "cli-result": CLI_RESULT,
    "cli-error": CLI_ERROR,
    "wait-result": WAIT_RESULT,
}


def fresh(o):
    return copy.deepcopy(o)
