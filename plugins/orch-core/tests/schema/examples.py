"""Valid example documents, shaped like the ones in the format doc (F1) with its ellipses filled in."""

import base64
import copy
import hashlib


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _bytes(tag: str, n: int) -> bytes:
    return hashlib.sha256(tag.encode()).digest()[:n] if n <= 32 else (hashlib.sha256(tag.encode()).digest() * 3)[:n]


def sig(tag: str = "sig") -> str:
    return b64u(_bytes(tag, 64))  # 64-byte r||s


def pub(tag: str = "pub") -> str:
    return b64u(b"\x04" + _bytes(tag, 64))  # 65-byte uncompressed point


def digest(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


def hex32(tag: str) -> str:
    return hashlib.sha256(tag.encode()).hexdigest()[:32]


U1 = "01J9ZK4Q7M3R8T2V6X0B5N1C9D"
EID = "01J9ZPABCDEFGHJKMNPQRSTVWX"
SIG = sig()
H = digest("gate")
H2 = digest("prev")
H3 = digest("policy")
WS = hex32("workspace")
PID, PID2, DID, DID2 = hex32("sev"), hex32("mara"), hex32("mac"), hex32("phone")
PERSON = {"kind": "person", "id": "p_" + PID, "device": "d_" + DID}
AGENT = {"kind": "agent", "id": "claude-code", "session": "s_" + U1, "for": "p_" + PID, "grant": "gr_" + U1}
UNATT = {"kind": "agent", "id": "claude-code", "session": "s_" + U1, "unattended": True}
HOST = {"kind": "host"}
ACTORS = {"person": PERSON, "agent": AGENT, "unattended": UNATT, "host": HOST}

WORKSPACE_ONLY = {
    "workspace.created",
    "member.added",
    "member.removed",
    "role.changed",
    "device.added",
    "device.removed",
    "device.revoked",
    "settings.changed",
    "grant.issued",
    "grant.revoked",
    "addon.granted",
    "addon.disabled",
    "addon.purged",
}


def policy(approvers=("owner",), count=1, not_=(), applies="all", independent=False):
    return {
        "approvers": list(approvers),
        "count": count,
        "not": list(not_),
        "applies": applies,
        "independent": independent,
    }


GATES = {
    "requirements": policy(),
    "plan": policy(),
    "verify": policy(("reviewers",), 1, ("assignees",)),
    "code": policy(("maintainer", "owner"), 1, ("assignees",), "off", True),
}

WORKSPACE = {
    "schema": "orch.workspace/2",
    "workspace": {"id": WS, "prefix": "DEMO", "name": "Acme energy data"},
    "host": {"id": "h_" + U1, "wsk_pub": pub("wsk")},
    "members": [
        {"person": "p_" + PID, "name": "Severin", "role": "owner"},
        {"person": "p_" + PID2, "name": "Mara", "role": "maintainer"},
        {"person": "p_" + hex32("tom"), "name": "Tom", "role": "viewer"},
    ],
    "gates": GATES,
    "settings": {
        "grant_hours": 8,
        "claim_ttl_min": 120,
        "lease_ttl_min": 60,
        "repos": {"acme-energy-dbt": {"path": "../acme-energy-dbt"}},
    },
    "agents": {"run_for": ["owner", "maintainer", "member"]},
    "addons": {"dashboard": {"enabled": True}, "estimate": {"enabled": True}, "publish": {"enabled": False}},
}

KEYS_LINE = {"key": "DEMO-0043", "uid": U1, "at": "2026-10-09T09:00:00Z"}

QUESTION = {
    "id": "Q1",
    "to": "p_" + PID2,
    "text": "Which tariff export is the source of truth, the monthly CSV or the API?",
    "why": "The two differ for 3 of 40 tariffs; the seed must pick one.",
    "options": [{"key": "csv", "label": "Monthly CSV"}, {"key": "api", "label": "Tariff API", "cost": "+1 day"}],
    "recommended": "csv",
    "blocking": True,
}

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
            "assignee": "p_" + PID,
        },
        {
            "id": "T4",
            "text": "Document the refresh command",
            "verify": None,
            "proves": ["AC3"],
            "assignee": "p_" + PID2,
        },
    ],
    "questions": [QUESTION],
    "addons": {"estimate": {"points": 5}},
}
# the defaults of a new ticket (section 3)
TICKET_NEW = {
    "schema": "orch.ticket/2",
    "uid": U1,
    "key": "DEMO-0043",
    "title": "New",
    "type": "bug",
    "priority": "medium",
    "size": None,
    "labels": [],
    "parent": None,
    "blocked_by": [],
    "due": None,
    "visibility": "workspace",
    "links": {"repos": [], "branches": {}, "prs": [], "external": []},
    "acceptance": [],
    "tasks": [],
    "questions": [],
    "addons": {},
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

SOURCE = [{"repo": "https://github.com/acme/energy-dbt", "ref": "refs/heads/feat/DEMO-0043", "sha": "b7e1f02c" * 5}]


def cert(person, device, scopes=("look", "decide", "operate", "type"), tag=""):
    return {
        "o": {
            "v": 2,
            "suite": 2,
            "kind": "device_cert",
            "device_id": device,
            "person_id": person,
            "dk_sig_pub": pub("dk" + device + tag),
            "dk_kx_pub": pub("kx" + device + tag),
            "label_sealed": b64u(b"sealed label"),
            "created_ms": 1_760_000_000_000,
            "expires_ms": None,
            "scopes_max": list(scopes),
        },
        "sig": sig("cert" + device + tag),
    }


def revocation(person, device, reason="lost"):
    return {
        "o": {
            "v": 2,
            "suite": 2,
            "kind": "revocation",
            "person_id": person,
            "device_id": device,
            "revoked_ms": 1_760_000_100_000,
            "reason": reason,
        },
        "sig": sig("rev" + device),
    }


def _env(t, actor, seq=5, **kw):
    e = {
        "v": 2,
        "id": EID,
        "seq": seq,
        "at": "2026-10-09T09:10:11Z",
        "type": t,
        "actor": actor,
        "based_on": None if seq == 1 else H2,
        "prev": None if seq == 1 else H2,
        "hash_v": 1,
        "host_sig": SIG,
    }
    if t not in WORKSPACE_ONLY:
        e["ws_seq"] = 31
    e.update(kw)
    if actor["kind"] == "person":
        e.update(sig=SIG, auth="passphrase", roster_v=0 if t == "workspace.created" else 7)
    return e


def make(t, actor, seq=5, **kw):
    """An event of type ``t`` for ``actor`` (a key of ACTORS or an actor dict); for tests that vary the actor."""
    return _env(t, ACTORS.get(actor, actor) if isinstance(actor, str) else actor, seq, **kw)


# one valid example per type: (actor, payload) with seq where it matters
_SPEC = {
    "ticket.created": (
        AGENT,
        {"key": "DEMO-0043", "ticket_type": "feature", "title": "Load tariffs", "owner": "p_" + PID},
        1,
    ),
    "ticket.updated": (
        AGENT,
        {
            "base_rev": {"ticket.title": H, "body.plan": H2},
            "set": {"ticket.title": "Load tariff tables"},
            "sections": {"plan": {"hash": H3, "refs": ["a.png", "b.png"]}},
        },
        5,
    ),
    "status.changed": (AGENT, {"from": "open", "to": "backlog", "reason": "later"}, 5),
    "ticket.submitted": (AGENT, {}, 5),
    "ticket.closed": (PERSON, {"resolution": "duplicate", "duplicate_of": "DEMO-0040", "text": "same"}, 5),
    "ticket.reopened": (PERSON, {"text": "regression"}, 5),
    "visibility.changed": (PERSON, {"visibility": {"restricted": ["p_" + PID]}}, 5),
    "people.changed": (PERSON, {"role": "reviewers", "add": ["p_" + PID2], "remove": []}, 5),
    "policy.changed": (PERSON, {"gates": {"verify": policy(("reviewers",), 2, ("assignees",))}}, 5),
    "claim.taken": (AGENT, {"takeover": {"from_session": "s_" + EID, "reason": "stuck"}}, 5),
    "claim.released": (AGENT, {"session": AGENT["session"], "reason": "handoff"}, 5),
    "task.started": (AGENT, {"task": "T3"}, 5),
    "task.done": (
        AGENT,
        {
            "task": "T2",
            "receipt": {
                "cmd": "dbt seed --select tariffs",
                "exit": 0,
                "ms": 38200,
                "repo": "acme-energy-dbt",
                "commit": "b7e1f02c" * 5,
            },
            "log": "seed.log",
            "text": "ok",
        },
        9,
    ),
    "task.skipped": (AGENT, {"task": "T3", "reason": "not needed"}, 5),
    "task.blocked": (AGENT, {"task": "T3", "reason": "needs Q1"}, 5),
    "task.reopened": (AGENT, {"task": "T3"}, 5),
    "handoff.written": (AGENT, {"text": "T3 half done"}, 5),
    "log.added": (UNATT, {"text": "note"}, 5),
    "question.asked": (AGENT, {"question": QUESTION, "qid": hex32("qid"), "hash": H}, 5),
    "question.answered": (PERSON, {"question": "Q1", "hash": H, "option": "csv", "text": "csv, monthly"}, 5),
    "gate.approved": (
        PERSON,
        {"gate": "requirements", "gate_gen": 2, "hash": H, "policy_hash": H3},
        5,
    ),
    "gate.changes_requested": (
        PERSON,
        {"gate": "plan", "gate_gen": 0, "hash": H, "policy_hash": H3, "text": "Split T3"},
        5,
    ),
    "verdict.given": (
        PERSON,
        {"outcome": "pass", "gate_gen": 1, "hash": H, "policy_hash": H3, "source_sha": SOURCE, "text": "good"},
        5,
    ),
    "gate.invalidated": (HOST, {"gate": "verify", "cause": "new_commits", "voided": [EID]}, 5),
    "branch.pushed": (
        HOST,
        {
            "repo_name": "acme-energy-dbt",
            "repo_id": "https://github.com/acme/energy-dbt",
            "ref": "refs/heads/feat/DEMO-0043",
            "sha": "b7e1f02c" * 5,
            "before": None,
        },
        5,
    ),
    "artifact.added": (
        AGENT,
        {
            "name": "seeds-in-warehouse.png",
            "kind": "screenshot",
            "sha256": digest("png"),
            "bytes": 84213,
            "ac": "AC1",
            "task": "T2",
            "label": "after",
        },
        10,
    ),
    "artifact.replaced": (
        AGENT,
        {
            "name": "seeds-in-warehouse.png",
            "kind": "screenshot",
            "sha256": digest("png2"),
            "bytes": 84300,
            "replaces": digest("png"),
        },
        5,
    ),
    "edit.external": (
        HOST,
        {
            "sections": {"plan": {"hash": H, "refs": []}, "decisions": None},
            "voided_gates": ["plan"],
            "normalised": False,
        },
        5,
    ),
    "projection.repaired": (
        HOST,
        {"path": "ticket.json", "cause": "projection_mismatch", "fields": ["ticket.title"]},
        5,
    ),
    "restore": (
        PERSON,
        {
            "from_seq": 4,
            "head": H2,
            "abandoned": {"seq": 9, "head": H},
            "abandoned_decisions": [EID],
            "reason": "restored backup",
        },
        5,
    ),
    "invalid.acknowledged": (PERSON, {"invalid_seq": 4, "head": H, "reason": "forged by an agent"}, 5),
    "workspace.created": (
        PERSON,
        {
            "workspace_id": WS,
            "prefix": "DEMO",
            "host_id": "h_" + U1,
            "wsk_pub": pub("wsk"),
            "owner": {"person": "p_" + PID, "name": "Severin", "pk_pub": pub("pk")},
            "delegation": {
                "o": {
                    "v": 2,
                    "suite": 2,
                    "kind": "ws_delegation",
                    "workspace_id": WS,
                    "wsk_pub": pub("wsk"),
                    "owner_person_id": PID,
                    "client_hosted": False,
                    "issued_ms": 1_760_000_000_000,
                },
                "sig": sig("deleg"),
            },
            "device_cert": cert(PID, DID),
        },
        1,
    ),
    "member.added": (
        PERSON,
        {
            "person": "p_" + PID2,
            "name": "Mara",
            "role": "maintainer",
            "pk_pub": pub("pk2"),
            "device_cert": cert(PID2, DID2),
        },
        5,
    ),
    "member.removed": (PERSON, {"person": "p_" + PID2}, 5),
    "role.changed": (PERSON, {"person": "p_" + PID2, "role": "member"}, 5),
    "device.added": (PERSON, {"device": "d_" + DID2, "cert": cert(PID, DID2)}, 5),
    "device.removed": (PERSON, {"device": "d_" + DID2, "reason": "sold"}, 5),
    "device.revoked": (
        PERSON,
        {"device": "d_" + DID2, "reason": "lost", "revocation": revocation(PID, DID2, "lost")},
        5,
    ),
    "settings.changed": (
        PERSON,
        {"set": {"grant_hours": 12, "repos": {"acme-energy-dbt": {"path": "../dbt"}, "old": None}}},
        5,
    ),
    "grant.issued": (
        PERSON,
        {
            "grant": "gr_" + U1,
            "scope": "workable",
            "verbs": "agent",
            "issued_at": "2026-10-09T09:10:00Z",
            "hours": 8,
            "expires_at": "2026-10-09T17:10:00Z",
            "secret_hash": digest("secret"),
            "label": "laptop",
        },
        5,
    ),
    "grant.revoked": (PERSON, {"grant": "gr_" + U1, "reason": "done"}, 5),
    "addon.granted": (
        PERSON,
        {
            "name": "estimate",
            "version": "1.2.0",
            "package_sha256": digest("pkg"),
            "capabilities": ["serve_http"],
            "binds": {
                "fields": {"points": ["plan"]},
                "sections": [{"id": "notes", "gate": ["plan"], "types": ["feature", "bug"]}],
            },
        },
        5,
    ),
    "addon.disabled": (PERSON, {"name": "estimate"}, 5),
    "addon.purged": (PERSON, {"name": "estimate"}, 5),
}

EVENTS = {t: _env(t, a, s, **p) for t, (a, p, s) in _SPEC.items()}
# the same types in the other log
EVENTS_WS_VARIANTS = {
    "policy.changed": {k: v for k, v in EVENTS["policy.changed"].items() if k != "ws_seq"},
    "projection.repaired": {k: v for k, v in EVENTS["projection.repaired"].items() if k != "ws_seq"},
    "restore": {k: v for k, v in EVENTS["restore"].items() if k != "ws_seq"},
    "invalid.acknowledged": {k: v for k, v in EVENTS["invalid.acknowledged"].items() if k != "ws_seq"},
}
EVENTS["gate.approved.code"] = _env(
    "gate.approved", PERSON, gate="code", gate_gen=0, hash=H, policy_hash=H3, source_sha=SOURCE
)
ADDON_EVENT = _env("estimate.updated", AGENT, points=5)

ARTIFACT = {
    "name": "seeds-in-warehouse.png",
    "kind": "screenshot",
    "sha256": digest("png"),
    "bytes": 84213,
    "ac": "AC1",
    "label": "after",
    "task": "T2",
    "actor": AGENT,
}
ARTIFACT_ADDON = {"name": "board", "kind": "board", "addon": "dashboard", "ref": "boards/1", "actor": AGENT}

CHECKPOINT_TICKET = {
    "o": {
        "v": 2,
        "suite": 2,
        "kind": "ticket_checkpoint",
        "workspace_id": WS,
        "uid": U1,
        "seq": 9,
        "head": H,
        "at": "2026-10-09T10:00:00Z",
    },
    "sig": SIG,
}
CHECKPOINT_WORKSPACE = {
    "o": {
        "v": 2,
        "suite": 2,
        "kind": "workspace_checkpoint",
        "workspace_id": WS,
        "genesis": H2,
        "n": 3,
        "at": "2026-10-09T10:00:00Z",
        "workspace_log": {"seq": 12, "head": H3},
        "tickets": {U1: {"seq": 9, "head": H}},
    },
    "sig": SIG,
}

MANIFEST = {
    "schema": "orch.addon/2",
    "name": "estimate",
    "version": "1.2.0",
    "title": "Estimate",
    "entry": {"cmd": ["python", "-m", "orch_estimate"]},
    "fields": {
        "points": {
            "type": "integer",
            "min": 0,
            "max": 100,
            "set_by": ["owner", "maintainer", "agent"],
            "gate": ["plan"],
            "show": True,
            "filter": True,
        },
        "mood": {"type": "enum", "values": ["good", "bad"], "set_by": ["addon"]},
    },
    "sections": [
        {"id": "notes", "heading": "Estimate notes", "after": "plan", "gate": ["plan"], "types": ["feature", "bug"]}
    ],
    "artifact_kinds": [{"kind": "chart", "label": "Chart"}],
    "capabilities": ["serve_http"],
}

GATE_INPUT = {
    "workspace_id": WS,
    "uid": U1,
    "gate": "verify",
    "schema": "orch.ticket/2",
    "hash_v": 1,
    "sections": {"verification": H},
    "fields": {
        "ticket_type": "feature",
        "size": "m",
        "acceptance": [{"id": "AC1", "text": "loads"}],
        "links": TICKET["links"],
        "addons": {},
    },
    "addon_packages": {},
    "tasks": [],
    "artifacts": {"a.png": {"kind": "screenshot", "digest": digest("png"), "ac": "AC1", "task": None}},
    "receipts": {"T2": {"event": EID, "repo": "acme-energy-dbt", "commit": "b7e1f02c" * 5, "exit": 0}},
    "source_sha": SOURCE,
    "prior": {"requirements": {"gen": 2, "approvals": [EID]}, "plan": {"gen": 1, "approvals": [EID]}},
    "policy_hash": H3,
    "people_hash": H2,
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

CLI_RESULT = {"v": "orch.cli/2.0", "ok": True, "data": {}, "key": "DEMO-0043", "seq": 18, "cursor": 18, "hints": []}
CLI_ERROR = {"ok": False, "error": {"code": "human_only", "message": "approve is human-only", "retryable": False}}
WAIT = {
    "timeout": {"kind": "timeout", "key": "DEMO-0043", "cursor": 14, "next": "orch wait"},
    "answered": {
        "kind": "answered",
        "key": "DEMO-0043",
        "seq": 15,
        "cursor": 15,
        "next": "orch task next",
        "question": "Q1",
        "option": "csv",
        "by": "p_" + PID2,
    },
    "approved": {
        "kind": "approved",
        "key": "DEMO-0043",
        "seq": 16,
        "cursor": 16,
        "next": "orch task next",
        "gate": "plan",
        "by": "p_" + PID,
    },
    "changes_requested": {
        "kind": "changes_requested",
        "key": "DEMO-0043",
        "seq": 16,
        "cursor": 16,
        "next": "orch show",
        "gate": "plan",
        "text": "Split T3",
        "by": "p_" + PID,
    },
    "verdict": {
        "kind": "verdict",
        "key": "DEMO-0043",
        "seq": 17,
        "cursor": 17,
        "next": "orch show",
        "outcome": "fail",
        "text": "tests fail",
        "by": "p_" + PID,
    },
    "invalidated": {
        "kind": "invalidated",
        "key": "DEMO-0043",
        "seq": 18,
        "cursor": 18,
        "next": "orch show",
        "gate": "verify",
    },
}

VALID = {
    "workspace": WORKSPACE,
    "keys-line": KEYS_LINE,
    "ticket": TICKET,
    "body": BODY_FEATURE,
    "event": EVENTS["gate.approved"],
    "artifact": ARTIFACT,
    "addon-manifest": MANIFEST,
    "gate-input": GATE_INPUT,
    "operation": OPERATION,
    "checkpoint": CHECKPOINT_TICKET,
    "cli-result": CLI_RESULT,
    "cli-error": CLI_ERROR,
    "wait-result": WAIT["timeout"],
}


def fresh(o):
    return copy.deepcopy(o)
