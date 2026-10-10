"""Refusal codes (ticket-format §5.4 to §5.11, §10.4) and the result of ``admit``.

One central enum: the ``code`` strings are the stable contract that the store, the operation registry and the CLI
error envelope use. ``admit`` returns :data:`OK` or a :class:`Refusal`; replay turns the same refusal into an
``auth.invalid_event`` record (§5.11).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Code(StrEnum):
    # --- chain and envelope (the store checks these; the model repeats the ones it needs)
    CHAIN_BROKEN = "chain.broken"
    CHAIN_DIVERGED = "chain.diverged"
    CHAIN_BAD_WS_SEQ = "chain.bad_ws_seq"
    EVENT_BAD_BASE = "event.bad_base"
    EVENT_DUPLICATE_ID = "event.duplicate_id"
    EVENT_BAD_ACTOR = "event.bad_actor"
    EVENT_UNKNOWN_TYPE = "event.unknown_type"
    # --- authorization replay (§5.11)
    AUTH_INVALID_EVENT = "auth.invalid_event"
    SIG_INVALID = "sig.invalid"
    MEMBERS_STALE = "members.stale"
    FREEZE_ACTIVE = "freeze.active"
    ACK_UNKNOWN = "ack.unknown"
    TRUST_GENESIS_MISMATCH = "trust.genesis_mismatch"
    GENESIS_INVALID = "genesis.invalid"
    ROLE_DENIED = "role.denied"
    # --- people, devices, grants
    MEMBER_UNKNOWN = "member.unknown"
    MEMBER_EXISTS = "member.exists"
    MEMBERS_LAST_OWNER = "members.last_owner"
    DEVICE_UNKNOWN = "device.unknown"
    DEVICE_EXISTS = "device.exists"
    DEVICE_INVALID = "device.invalid"
    DEVICE_SCOPE = "device.scope"
    DEVICE_CERT = "device.cert"
    GRANT_INVALID = "grant.invalid"
    GRANT_SCOPE = "grant.scope"
    GRANT_VERB = "grant.verb"
    GRANT_TERMS = "grant.terms"
    GRANT_EXISTS = "grant.exists"
    GRANT_UNKNOWN = "grant.unknown"
    QUOTA_UNATTENDED = "quota.unattended"
    UNATTENDED_DENIED = "unattended.denied"
    SETTINGS_INVALID = "settings.invalid"
    ADDON_UNKNOWN = "addon.unknown"
    # --- tickets
    TICKET_UNKNOWN = "ticket.unknown"
    TICKET_EXISTS = "ticket.exists"
    TICKET_FROZEN = "ticket.frozen"
    TICKET_NOT_VISIBLE = "ticket.not_visible"
    TICKET_BAD_REFERENCE = "ticket.bad_reference"
    STATUS_TRANSITION = "status.transition"
    SUBMIT_INCOMPLETE = "submit.incomplete"
    PEOPLE_INVALID = "people.invalid"
    CONFLICT_SECTION = "conflict.section"
    PATH_PROTECTED = "path.protected"
    BODY_UNKNOWN_SECTION = "body.unknown_section"
    BODY_UNKNOWN_ARTIFACT = "body.unknown_artifact"
    REPO_UNKNOWN = "repo.unknown"
    RESTORE_BAD_HEAD = "restore.bad_head"
    # --- gates and policies
    GATE_STALE = "gate.stale"
    GATE_NO_ELIGIBLE = "gate.no_eligible"
    GATE_NOT_ELIGIBLE = "gate.not_eligible"
    GATE_INCOMPLETE = "gate.incomplete"
    GATE_NOT_APPLICABLE = "gate.not_applicable"
    GATE_STATUS = "gate.status"
    GATE_INVALIDATED_MISMATCH = "gate.invalidated_mismatch"
    POLICY_INVALID = "policy.invalid"
    SOURCE_MISSING = "source.missing"
    SOURCE_UNLINKED = "source.unlinked"
    SOURCE_NOT_NEW = "source.not_new"
    # --- claims, tasks, artifacts, questions
    CLAIM_NOT_LIVE = "claim.not_live"
    CLAIM_EXISTS = "claim.exists"
    CLAIM_NOT_HOLDER = "claim.not_holder"
    TASK_UNKNOWN = "task.unknown"
    TASK_LEASED = "task.leased"
    TASK_STATE = "task.state"
    TASK_BAD_RECEIPT = "task.bad_receipt"
    ARTIFACT_EXISTS = "artifact.exists"
    ARTIFACT_UNKNOWN = "artifact.unknown"
    ARTIFACT_BAD_REPLACES = "artifact.bad_replaces"
    ARTIFACT_KIND = "artifact.kind"
    QUESTION_UNKNOWN = "question.unknown"
    QUESTION_STALE = "question.stale"
    QUESTION_ANSWERED = "question.answered"
    QUESTION_BAD_ID = "question.bad_id"
    QUESTION_BAD_HASH = "question.bad_hash"
    QUESTION_REASK = "question.reask"
    ANSWER_NOT_ALLOWED = "answer.not_allowed"
    ANSWER_BAD_OPTION = "answer.bad_option"


@dataclass(frozen=True)
class Refusal:
    """An event the model refuses: the stable ``code`` plus a human detail (never parsed)."""

    code: Code
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.code.value}: {self.detail}" if self.detail else self.code.value


@dataclass(frozen=True)
class Ok:
    """The event is admitted."""


OK = Ok()
