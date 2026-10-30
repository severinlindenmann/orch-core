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
#: B8 (owner decision 16 Oct, D60): the longest a grant may last, in hours (7 days); also the ``grant_hours`` ceiling
GRANT_MAX_HOURS = 168
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
    acc: str = "sha256:" + "0" * 64
    seq: int = 0
    head: str | None = None
    heads: dict[str, int] = field(default_factory=dict)
    ids: set[str] = field(default_factory=set)
    last_ws_seq: int = 0
    last_at: int = 0  # the previous line's `at` in this log (§5.5: (ws_seq, at) never goes back along a log)
    broken: str | None = None
    broken_code: str = "chain.broken"
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
    excluded: str | None = None  # C10: the reason of a hub-signed device.excluded (lost | retired | compromised)
    revoked: str | None = None
    #: #1097: positive evidence that this key was in convenience custody (a signed ``device.exposed`` mark, the only
    #: evidence). Monotonic: nothing clears it, only a new key (``device.rotated``) does.
    exposed: bool = False


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
    #: C8 (L2-P5-1): the hub whose ``auth: "hub"`` request line this grant answers, else ``None``. Such a grant ends at
    #: that hub's ``hub.left`` (state, no new line). Set by the C10 host-actor ``grant.issued`` that names its request.
    hub_id: str | None = None
    parent: str | None = None
    root_kind: str = "grant"  # schedule roots have no bearer credential
    #: a follow-up grant that has served its run (its outcome is recorded): unusable, but not a revocation, so
    #: later follow-ups set during that run keep a valid chain
    ended: bool = False


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
    #: V3 G4 (D6): ``{kind, lock, packages}`` when the grant also installs dependencies, else ``None``
    runtime: dict[str, Any] | None = None

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
    #: the `at` (ms) of the event being judged / last applied: the deterministic "now" of every hub role decision (an end
    #: date in the pinned snapshot is read against it, never against the wall clock, so a replay gives the same answer)
    at_ms: int = 0
    devices: dict[str, Device] = field(default_factory=dict)
    # Effective revocation evidence and tombstones: device id -> person id -> {revocation, device_cert, pk_pub}.
    # Keyed by (device, person) (§5.10): another person's evidence for one device id is kept, never merged or replaced.
    revocations: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    grants: dict[str, Grant] = field(default_factory=dict)
    policies: dict[str, dict[str, Any]] = field(default_factory=default_policies)
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    repos: dict[str, dict[str, str]] = field(default_factory=dict)
    addons: dict[str, Addon] = field(default_factory=dict)
    # The one hub of this workspace (hub contract 9.1): {hub_id, hk_root_pub, mode, binding_seq, status:
    # "joined" | "left", aliases: {person_id: user_id} of this hub,
    # alias_book: {hub_id: aliases} of every hub ever joined, member?: {kind: "hub", hub_id}}; empty until the first
    # hub.joined. ``binding_seq`` only grows. ``member`` exists only in full mode and only while joined (C2b).
    hub: dict[str, Any] = field(default_factory=dict)
    unattended: list[tuple[int, str, int]] = field(default_factory=list)  # (at seconds, session, artifact bytes)
    #: C10 (hub protocol 8.8): the admitted ``grant.requested`` lines by event id: ``{person, role, scope, verbs, hours,
    #: label?, handoff, at, hub_id, used}``. A host-actor ``grant.issued`` consumes one (``used``), once.
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: FB15 (hub protocol 8.8b): the admitted ``addon.requested`` lines by event id: ``{person, role, name, version,
    #: package_sha256, capabilities, runtime, at, hub_id, used}``. A host-actor ``addon.granted`` consumes one, once.
    addon_requests: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Decision:
    id: str
    seq: int
    gate: str
    kind: str  # approve | pass | fail | changes
    person: str
    signer: str  # identity.Signer.key: a device id (d_...) or "hub:<hub_id>/<channel>" (ticket-format 15.4)
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
class FollowupRec:
    id: str
    state: str
    trigger: str
    set_at: str
    due: str | None
    deadline: str | None
    brief: str
    setter_session: str
    person: str
    parent_grant: str
    set_event_id: str
    fired_at: str | None = None
    grant: str | None = None
    run: str | None = None
    outcome: str | None = None
    reason: str | None = None
    fallback: str | None = None
    cancelled_at: str | None = None
    cancelled_by: str | None = None
    cancel_prev_state: str | None = None
    cancel_reason: str | None = None
    label: str | None = None
    on: dict[str, Any] | None = None  # the event match of an orch/webhook follow-up
    hook: str | None = None  # sha256 of the webhook id (never the id)
    event: dict[str, Any] | None = None  # the event that woke it (closed shape)


@dataclass
class ScheduleRec:
    id: str
    set_event: str
    person: str
    device: str
    rule: dict[str, Any]
    starts: str
    ends: str
    brief: str
    run_hours: int
    set_at: str  # the `at` of schedule.set: no occurrence before set_at + min lead is ever fired or settled
    label: str | None = None
    cancelled: bool = False
    target: str = "existing"
    title: str | None = None
    set_sig_hash: str = ""
    # occurrence -> {"fired_at", "grant", "run", "released"?: true when the firing cleared the setter's claim,
    # "outcome"?, "reason"?}
    occurrences: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class TCore:
    uid: str
    key: str
    owner: str
    status: str
    fields: dict[str, Any]
    anchor_schedule: str | None = None
    via: dict[str, str] | None = None
    close_resolution: str | None = None
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> {"hash", "refs"}
    people: dict[str, set[str]] = field(
        default_factory=lambda: {"assignees": set(), "reviewers": set(), "watchers": set()}
    )
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    gates: dict[str, GateCore] = field(default_factory=lambda: {g: GateCore() for g in GATES})
    schedules: dict[str, ScheduleRec] = field(default_factory=dict)
    followups: dict[str, FollowupRec] = field(default_factory=dict)
    fu_chain: dict[str, Any] = field(default_factory=dict)
    fu_sessions: dict[str, str] = field(default_factory=dict)
    claim: ClaimCore | None = None
    takeovers: list[dict[str, Any]] = field(default_factory=list)
    leases: dict[str, Lease] = field(default_factory=dict)
    tasks: dict[str, TaskCore] = field(default_factory=dict)
    # task id -> who made the last change to its ``verify`` (``kind``, ``id``, ``session``, ``person``: the person an
    # agent works for, ``None`` for an unattended one; ``grant``: an agent's grant); ``task done --run`` trusts only an
    # approved plan or its own
    verify_by: dict[str, dict[str, Any]] = field(default_factory=dict)
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
