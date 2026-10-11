"""State types: the mutable replay core (private to ``model/``) and the helpers shared by every module.

``Core`` is what replay and ``admit`` work on. It is never handed out: callers get the frozen views of ``views.py``.
Every event is applied to a scratch copy and committed only when it passes (``engine.py``), so a refused event can
never leave half an effect behind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

WORKSPACE = "workspace"  # the `log` name of the workspace log; a ticket log is named by its uid
GATES = ("requirements", "plan", "verify", "code")
TICKET_ROLES = ("ticket_owner", "assignees", "reviewers", "watchers")
WS_ROLES = ("owner", "maintainer", "member", "viewer")
DEFAULT_SETTINGS = {"grant_hours": 8, "claim_ttl_min": 120, "lease_ttl_min": 60}
DEFAULT_LINKS = {"repos": [], "branches": {}, "prs": [], "external": []}

_TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def ts(s: str) -> int:
    """Seconds since the epoch of a ``YYYY-MM-DDTHH:MM:SSZ`` timestamp (UTC, no clock is read)."""
    if not _TS.fullmatch(s):
        raise ValueError(f"bad timestamp {s!r}")
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())


def default_policies() -> dict[str, dict[str, Any]]:
    """The workspace gate defaults of the §2 ``config.json`` example (``code`` off, D59)."""

    def p(approvers, not_=(), applies="all", independent=False):
        return {
            "approvers": sorted(approvers),
            "count": 1,
            "not": sorted(not_),
            "applies": applies,
            "independent": independent,
        }

    return {
        "requirements": p(["owner"]),
        "plan": p(["owner"]),
        "verify": p(["reviewers"], ["assignees"]),
        "code": p(["maintainer", "owner"], ["assignees"], "off", True),
    }


@dataclass
class InvalidEvent:
    log: str
    seq: int
    id: str
    type: str
    head: str | None
    code: str
    detail: str
    freezes: bool = True  # events refused only because a freeze was active don't extend it


@dataclass
class LogCore:
    seq: int = 0
    head: str | None = None
    heads: dict[str, int] = field(default_factory=dict)
    ids: set[str] = field(default_factory=set)
    last_ws_seq: int = 0
    broken: str | None = None
    invalid: list[InvalidEvent] = field(default_factory=list)
    acked: set[int] = field(default_factory=set)

    def frozen(self) -> bool:
        return any(i.freezes and i.seq not in self.acked for i in self.invalid)


@dataclass
class Member:
    person: str
    name: str
    role: str
    pk_pub: str


@dataclass
class Device:
    id: str
    person: str
    cert: dict[str, Any]
    removed: bool = False
    revoked: str | None = None


@dataclass
class Grant:
    id: str
    person: str
    scope: str
    verbs: Any
    issued_at: str
    expires_at: str
    label: str | None = None
    revoked: bool = False
    device: str = ""  # the device that signed grant.issued


@dataclass
class Addon:
    name: str
    version: str
    package_sha256: str
    capabilities: list[str]
    fields: dict[str, Any]  # name -> {type, limits, set_by, gate?}: what the grant declared (§8.1)
    sections: list[dict[str, Any]]  # [{id: <addon>.<token>, types, gate?}]
    artifact_kinds: list[str]
    enabled: bool = True
    purged: bool = False

    @property
    def binds(self) -> dict[str, Any]:
        """The bindings of §5.7: the declared fields and sections that have a ``gate``."""
        return {
            "fields": {f: list(d["gate"]) for f, d in self.fields.items() if d.get("gate")},
            "sections": [
                {"id": s["id"], "gate": list(s["gate"]), "types": list(s["types"])}
                for s in self.sections
                if s.get("gate")
            ],
        }


@dataclass
class WsCore:
    created: bool = False
    workspace_id: str = ""
    prefix: str = ""
    host_id: str = ""
    wsk_pub: str = ""
    genesis: str | None = None
    members: dict[str, Member] = field(default_factory=dict)
    former: dict[str, Member] = field(default_factory=dict)
    roster_v: int = 0
    devices: dict[str, Device] = field(default_factory=dict)
    grants: dict[str, Grant] = field(default_factory=dict)
    policies: dict[str, dict[str, Any]] = field(default_factory=default_policies)
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    repos: dict[str, str] = field(default_factory=dict)
    addons: dict[str, Addon] = field(default_factory=dict)
    unattended: list[tuple[int, str, int]] = field(default_factory=list)  # (at seconds, session, artifact bytes)


@dataclass
class Decision:
    id: str
    seq: int
    gate: str
    kind: str  # approve | pass | fail | changes
    person: str
    device: str
    gen: int
    hash: str
    policy_hash: str
    source_sha: list[dict[str, str]] | None = None
    voided: bool = False


@dataclass
class GateCore:
    gen: int = 0
    decisions: list[Decision] = field(default_factory=list)
    pending_void: set[str] = field(default_factory=set)
    revoked_flag: set[str] = field(default_factory=set)  # counting decision ids of a revoked device on a settled ticket


@dataclass
class ClaimCore:
    session: str
    for_person: str
    grant: str
    taken_at: int
    last_activity: int
    ended: str | None = None  # ticket_done / ticket_closed


@dataclass
class TaskCore:
    state: str = "open"  # open | started | done | skipped | blocked
    holder: str | None = None
    done_event: str | None = None
    receipt: dict[str, Any] | None = None
    reason: str | None = None


@dataclass
class Lease:
    session: str
    at: int


@dataclass
class QuestionCore:
    qid: str
    hash: str
    question: dict[str, Any]
    asked_seq: int
    answer: dict[str, Any] | None = None


@dataclass
class ArtifactCore:
    name: str
    kind: str
    digest: str | None
    bytes: int | None
    ac: str | None
    task: str | None
    label: str | None
    addon: str | None
    ref: str | None
    event: str
    by: str  # person id, or agent session
    evidence_ok: bool  # file artifact from a person or an agent with a grant (§6, §12 O6)


@dataclass
class TCore:
    uid: str
    key: str
    owner: str
    status: str
    fields: dict[str, Any]
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> {"hash", "refs"}
    people: dict[str, set[str]] = field(
        default_factory=lambda: {"assignees": set(), "reviewers": set(), "watchers": set()}
    )
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    gates: dict[str, GateCore] = field(default_factory=lambda: {g: GateCore() for g in GATES})
    claim: ClaimCore | None = None
    takeovers: list[dict[str, Any]] = field(default_factory=list)
    leases: dict[str, Lease] = field(default_factory=dict)
    tasks: dict[str, TaskCore] = field(default_factory=dict)
    questions: dict[str, QuestionCore] = field(default_factory=dict)
    artifacts: dict[str, ArtifactCore] = field(default_factory=dict)
    branch_heads: dict[str, dict[str, str]] = field(default_factory=dict)  # repo name -> {repo_id, ref, sha}
    handoff: str | None = None
    marks: set[str] = field(default_factory=set)  # gates the current event raises directly (generations.py)
    workers: set[str] = field(default_factory=set)  # §5.7 "workers": assignees, claim holders, agents' `for` persons
    # voids a settled (done or closed) ticket was exempt from, applied when it becomes unsettled other than by a reopen
    exempt_persons: set[str] = field(default_factory=set)  # removed or role-changed while settled
    exempt_devices: set[str] = field(default_factory=set)  # revoked ``compromised`` while settled
    last_at: int = 0

    @property
    def ticket_type(self) -> str:
        return self.fields["type"]


@dataclass
class Core:
    ws: WsCore = field(default_factory=WsCore)
    tickets: dict[str, TCore] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)  # key -> uid
    created_at: dict[str, tuple[int, int, str, int]] = field(default_factory=dict)  # uid -> merged position of creation
    last_pos: tuple[int, int, str, int] | None = None  # position of the last committed ticket event
    logs: dict[str, LogCore] = field(
        default_factory=dict
    )  # "workspace" and every ticket uid: chain place, invalid events
    # workspace seqs a ticket event's ``ws_seq`` may not name (§5.10): a ``restore`` and the host's ``device.revoked``
    # re-appends right after it, except the last of them; ``rwin_last`` is the last of the window being read
    rwin_forbidden: set[int] = field(default_factory=set)
    rwin_last: int | None = None


def position(e: dict[str, Any], log: str) -> tuple[int, int, str, int]:
    """Merged-order position of a ticket event (§5.5): ``ws_seq``, then ``at``, then ticket uid, then ``seq``.
    ``admit`` requires it to grow with every append, so replay (which sorts by it) walks events in append order."""
    return (e["ws_seq"], ts(e["at"]), log, e["seq"])


def new_fields(ticket_type: str, title: str) -> dict[str, Any]:
    """``ticket.json`` defaults (§3): all 17 keys, minus the identity keys the core keeps itself."""
    return {
        "title": title,
        "type": ticket_type,
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
