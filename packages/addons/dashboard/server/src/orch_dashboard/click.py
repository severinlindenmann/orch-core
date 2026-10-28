"""The click-to-sign routes (H5c, orch-v2-host section 0.8.6 item 7): prepare, view, sign, cancel and the status card.

A closed set of typed routes, like the hand-off. The browser names a typed action (the same bodies as the hand-off);
the op and its arguments are chosen here, in `host_call`, never by the browser. The View comes from the signer and is
passed on unchanged: **this server never builds a preview for a click request, never logs a View or a
credential, and never signs.** It also never calls the signer from an addon route: only these four handlers do.

**One action intent -> at most one request -> at most one signature.** The idem handed to the signer's `prepare` is
derived from canonical content plus only the gate/question version (`intent.py`), not the click or session;
the signer forwards it to `human.prepare`
and the host answers every tab, retry and re-login with the SAME request (or its terminal state). The records below are
a convenience (which ids this dashboard prepared, what a View is checked against, a settling display), never the guard.

A request obtained through an intent belongs to the PERSON in that workspace: any session of that person may view, sign,
poll and cancel it (the signer separately refuses anything it did not prepare itself, and signs a request once).
"""

import hashlib
import re
import threading
from collections import OrderedDict
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from orch.click.protocol import ONE_PRESS_OPS
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from . import adapters
from .click_signer import lost_sign
from .errors import HostError, fail
from .gateway import Gateway
from .hubapprove import mode_of
from .reads import workspace
from .signing import (
    AddonEnable,
    AgentAccess,
    EditTicket,
    HubJoin,
    HubLeave,
    HubModeFull,
    NewTicketSequence,
    PrepareBody,
    QuickStart,
    RepoSet,
    RequestId,
    RunnerGrant,
    done_error,
    expected,
    expected_material,
    plan,
    read_done,
    send_prepare,
    sequence_progress,
)
from .stepup import refusal_check, refuse_step_up

# The card text, verbatim (owner decision, 12 Oct 2026). A View that carries anything else is not shown.
RISK = "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign."
ViewDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
TTL = 660  # a request lives 10 minutes at most (the signer caps it); the session forgets it a minute later
REMEMBERED = 256
#: refusals after which a request can no longer be signed: its slot is free again
ENDED = {"expired", "not_prepared_here", "custody_changed", "human.request_expired", "human.request_state"}
#: the signer says `the request is <state>` when an intent's idem returns a request that already ended
SUBMITTED = "the request is submitted"  # cancelled/expired never get here: send_prepare steps past them (#794)

# M7b, the runner grant (D-P2-27, section 0.8.6 item 6): a click request of its own action. The texts are core's
# constants (orch.click.protocol); a View of a deposit grant that lacks either is not shown.
DEPOSIT_LINE = "The secret goes to your orch runner on this machine and is never shown"
DEPOSIT_RISK = (
    "Anything that takes over the dashboard can give agents standing authority to act for you on tickets for as long "
    "as the grant lasts (up to 7 days, the length you choose); you can stop it at any time."
)
#: core's constants (orch.click.protocol REVEAL_LINE, REVEAL_RISK): a shown-secret View that lacks either is not shown
REVEAL_LINE = "The secret is shown once, on this page, only to you. orch keeps only a fingerprint of it"
REVEAL_RISK = "The secret will be shown once in this browser. Anyone who controls this dashboard could read it."
DEPOSITS = ("confirmed", "rejected", "unknown")
#: H11c-2's closed set of `deposit_reason` codes (static codes, never host text); anything else reads `other`
DEPOSIT_REASONS = (
    "grant.forgotten",
    "grant.not_runner",
    "grant.verbs",
    "grant.expiring",
    "rate.limited",
    "runner.unavailable",
    "other",
)
ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
GRANT_ID = re.compile(r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")

Passphrase = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class CustodyBody(BaseModel):
    """The click-to-sign switch (section 0.8.6 item 3a). `on` needs the person's CURRENT passphrase: the proof the key
    is theirs, checked by the signer against the key in passphrase custody (Opus H1 on #539). `off` may carry the NEW
    passphrase the key moves back under (V3-D31, narrowing Opus H2), but only SEALED to the signer's one-use key
    (#1198 C12, purpose `custody.passphrase`): `request_id`, `view_digest` and `sealed` come from `click/custody/seal`.
    The signer takes it only while the switch is on and trades that authority for it in one locked step, else it
    refuses (`custody.terminal_needed`). Without them it only turns the flag off. This server is a relay: it never
    sees a new passphrase, and nothing here is logged or echoed (the validation handler answers a bare
    `invalid.input`)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    on: bool
    passphrase: Passphrase | None = None
    #: #1198 C17: `on` without a passphrase, for a key already in this computer's key store (idle switch-off): a plain
    #: click after the review; only from a dashboard opened on this computer (the route checks)
    resume: bool = False
    request_id: Annotated[str, StringConstraints(pattern=r"^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")] | None = None
    view_digest: ViewDigest | None = None
    sealed: "PassphraseSealed | None" = None

    @model_validator(mode="after")
    def _shape(self):
        if self.resume and (not self.on or self.passphrase is not None):
            raise ValueError("resume is on without a passphrase")
        if self.on and self.passphrase is None and not self.resume:
            raise ValueError("on needs the passphrase")
        if self.on and (self.request_id or self.view_digest or self.sealed):
            raise ValueError("on takes the current passphrase only")
        if not self.on and self.passphrase is not None:
            raise ValueError("a new passphrase is sent sealed")
        if not self.on and "passphrase" in self.model_fields_set:
            raise ValueError("off takes no passphrase field")
        if not self.on and len({self.request_id is None, self.view_digest is None, self.sealed is None}) != 1:
            raise ValueError("a sealed passphrase comes with its request id and review digest")
        return self


class SealBegin(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


#: Every typed action a click request may carry: the hand-off's, plus the runner grant (click only, never a hand-off).
# The runner grant is a click request, or a terminal hand-off with `runner` set by this server, never by the browser.
ClickBody = PrepareBody


#: contract 4 (run 13 C3): a View's `risk_lines` is at most this many lines of at most this many characters
RISK_LINES_MAX, RISK_LINE_CHARS, HEADLINE_CHARS = 64, 400, 60


def contract4_view_ok(view: dict) -> bool:
    """Contract 4's View fields: `headline`, one line of text (not empty), and `risk_lines`, a list of lines of text
    (maybe empty), and `headline_detail`, null or one line of text (the capped, quoted free text beside the headline),
    all the signer's and in its `view_digest`. Passed on unchanged; only their shape is checked here."""
    head, risk, detail = view.get("headline"), view.get("risk_lines"), view.get("headline_detail")
    return (
        isinstance(head, str)
        and 0 < len(head) <= HEADLINE_CHARS
        and "\n" not in head
        and (
            detail is None or (isinstance(detail, str) and 0 < len(detail) <= HEADLINE_CHARS + 3 and "\n" not in detail)
        )
        and isinstance(risk, list)
        and (risk == []) == (view.get("op") in ONE_PRESS_OPS)
        and len(risk) <= RISK_LINES_MAX
        and all(isinstance(x, str) and 0 < len(x) <= RISK_LINE_CHARS and "\n" not in x for x in risk)
    )


def deposit_view_ok(view: dict, ws: str) -> bool:
    """A deposit grant's View (section 0.8.6 item 6): `deposit:true`, core's deposit line in `deposit_line` and in
    `lines`, the owner's risk sentence in `lines`, this workspace and a plain click
    confirmation (no typed word)."""
    confirm = view.get("confirm")
    lines = view.get("lines")
    workspace = view.get("workspace")
    return (
        isinstance(workspace, dict)
        and workspace.get("id") == ws
        and isinstance(confirm, dict)
        and view.get("deposit") is True
        and view.get("deposit_line") == DEPOSIT_LINE
        and isinstance(lines, list)
        and DEPOSIT_LINE in lines
        and DEPOSIT_RISK in lines
        and isinstance(view.get("risk_lines"), list)
        and view["risk_lines"][:1] == [DEPOSIT_RISK]  # contract 4: the signer marks it as the risk
        and confirm == {"kind": "click"}
    )


def key_fingerprint(key: str) -> str:
    """Core's `view.key_fingerprint`: the full SHA-256 (64 hex) of the key's base64url text (golden vectors shared
    with core and the page: `packages/core/tests/click_reveal_fingerprint.json`)."""
    return hashlib.sha256(key.encode()).hexdigest()


def reveal_view_ok(view: dict, ws: str, reveal_key: str) -> bool:
    """A shown-secret grant's View: no deposit, the sealed field's `grant.secret` purpose bound to the page's own key
    (its fingerprint), core's risk and reveal lines, this workspace and a plain click."""
    seal = view.get("seal")
    lines = view.get("lines")
    workspace = view.get("workspace")
    return (
        isinstance(workspace, dict)
        and workspace.get("id") == ws
        and view.get("deposit") is None
        and isinstance(seal, dict)
        and seal.get("purposes") == ["grant.secret"]
        and seal.get("for") == key_fingerprint(reveal_key)
        and isinstance(lines, list)
        and REVEAL_LINE in lines
        and REVEAL_RISK in lines
        and isinstance(view.get("risk_lines"), list)
        and view["risk_lines"][:1] == [REVEAL_RISK]  # contract 4: the signer marks it as the risk
        and view.get("confirm") == {"kind": "click"}
    )


#: core's constant (orch.click.protocol RUNNER_LINE): the review of turning it on must carry it
RUNNER_LINE = "Agents may run commands on this computer as you when you start them."
#: core's constant (orch.click.protocol BACKUP_KEY_LINE): a hub step's View that lacks it is not shown
BACKUP_KEY_LINE = "This step also needs your backup key. You type it once here; orch does not keep it"
HUB_STEPS = (HubJoin, HubModeFull, HubLeave)
B64URL = r"[A-Za-z0-9_-]"


def backup_view_ok(view: dict, ws: str) -> bool:
    """A hub step's View (#1198 C10): the sealed field's `hub.backup_key` purpose with the signer's own per-request key
    (a P-256 point, 87 base64url characters), core's backup-key line, this workspace and a plain click."""
    seal = view.get("seal")
    lines = view.get("lines")
    workspace = view.get("workspace")
    return (
        isinstance(workspace, dict)
        and workspace.get("id") == ws
        and view.get("deposit") is None
        and isinstance(seal, dict)
        and set(seal) == {"v", "alg", "pk", "purposes"}
        and seal["v"] == 1
        and type(seal["v"]) is int
        and seal["alg"] == "P256-HKDF-SHA256-A256GCM"
        and seal["purposes"] == ["hub.backup_key"]
        and isinstance(seal["pk"], str)
        and re.fullmatch(rf"{B64URL}{{87}}", seal["pk"]) is not None
        and isinstance(lines, list)
        and BACKUP_KEY_LINE in lines
        and view.get("backup_key_line") == BACKUP_KEY_LINE
        and view.get("confirm") == {"kind": "click"}
    )


def plain_hub_view_ok(view: dict, ws: str) -> bool:
    """Run-9 feedback 6: a hub step whose person key the signer kept asks for nothing sealed: a plain click, this
    workspace, no seal, no backup-key line."""
    workspace = view.get("workspace")
    return (
        isinstance(workspace, dict)
        and workspace.get("id") == ws
        and view.get("deposit") is None
        and view.get("seal") is None
        and view.get("backup_key_line") is None
        and view.get("confirm") == {"kind": "click"}
    )


def reveal_answer(got: dict) -> dict:
    """Of a shown-secret grant's `sign` answer, only the grant id and the sealed envelope, checked for shape and size:
    exactly `v`, `alg`, `epk`, `ct` with the sealed field's values. Never `result`. The envelope is ciphertext for the
    page's key; it is relayed and never logged. A malformed one is dropped (`reveal: null`), never repaired."""
    grant = (got.get("result") or {}).get("data", {}).get("grant") if isinstance(got.get("result"), dict) else None
    out: dict = {"grant": grant if isinstance(grant, str) and GRANT_ID.fullmatch(grant) else None, "reveal": None}
    rev = got.get("reveal")
    env = rev.get("envelope") if isinstance(rev, dict) and rev.get("purpose") == "grant.secret" else None
    if (
        isinstance(env, dict)
        and set(env) == {"v", "alg", "epk", "ct"}
        and env["v"] == 1
        and type(env["v"]) is int
        and env["alg"] == "P256-HKDF-SHA256-A256GCM"
        and isinstance(env["epk"], str)
        and re.fullmatch(r"[A-Za-z0-9_-]{87}", env["epk"])
        and isinstance(env["ct"], str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,6000}", env["ct"])
    ):
        out["reveal"] = {"purpose": "grant.secret", "envelope": dict(env)}
    return out


def deposit_answer(got: dict, request_id: str) -> dict:
    """Of a deposit grant's `sign` answer, only `deposit` (enum), `deposit_id` (the request's ULID), `grant` (a `gr_`
    id) and, when rejected, `deposit_reason` (the closed set). Never `result`, never anything else: the secret exists
    only inside the signer, and nothing else of its answer is passed on."""
    deposit = got.get("deposit") if got.get("deposit") in DEPOSITS else "unknown"
    out: dict = {"deposit": deposit}
    did = got.get("deposit_id")
    out["deposit_id"] = did if isinstance(did, str) and ULID.fullmatch(did) and did == request_id[3:] else None
    grant = got.get("grant")
    out["grant"] = grant if isinstance(grant, str) and GRANT_ID.fullmatch(grant) else None
    if deposit == "rejected":
        reason = got.get("deposit_reason")
        out["deposit_reason"] = reason if reason in DEPOSIT_REASONS else "other"
    return out


class ImageChunk(BaseModel):
    """At most 512 KiB of a picture per call (base64 under the 1 MiB body limit); the signer holds it in order."""

    model_config = ConfigDict(extra="forbid", strict=True)
    sha256: ViewDigest
    size: Annotated[int, Field(ge=1, le=5 * 1024 * 1024)]
    offset: Annotated[int, Field(ge=0, le=5 * 1024 * 1024)]
    b64: Annotated[str, StringConstraints(max_length=720_000, pattern=r"^[A-Za-z0-9+/]*={0,2}$")]
    last: bool


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: RequestId


class Envelope(BaseModel):
    """A sealed value as the page made it (sealed-field contract section 4): ciphertext for the signer's key. This
    server relays it and never opens, keeps or logs it; the signer checks everything else."""

    model_config = ConfigDict(extra="forbid", strict=True)
    v: Annotated[int, Field(ge=1, le=1)]
    alg: Annotated[str, StringConstraints(pattern=r"^P256-HKDF-SHA256-A256GCM$")]
    epk: Annotated[str, StringConstraints(pattern=rf"^{B64URL}{{87}}$")]
    ct: Annotated[str, StringConstraints(pattern=rf"^{B64URL}{{363,5483}}$")]


class Sealed(BaseModel):
    """The typed secrets of a sign call, by purpose (sealed-field contract section 7). Only the hub backup key today."""

    model_config = ConfigDict(extra="forbid", strict=True)
    hub_backup_key: Envelope = Field(alias="hub.backup_key")


class PassphraseSealed(BaseModel):
    """The new passphrase of "One click off", sealed (purpose `custody.passphrase`): relayed as ciphertext."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)
    custody_passphrase: Envelope = Field(alias="custody.passphrase")


CustodyBody.model_rebuild()


class RotateSealed(BaseModel):
    """The two typed secrets of "Replace this device's key", sealed (purposes `device.recovery_code` and
    `custody.passphrase`): relayed as ciphertext."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)
    device_recovery_code: Envelope = Field(alias="device.recovery_code")
    custody_passphrase: Envelope = Field(alias="custody.passphrase")


class RotateBody(BaseModel):
    """#1198 C13: `request_id`, `view_digest` and `sealed` come from `click/rotate/seal`; the signer does the rest."""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Annotated[str, StringConstraints(pattern=r"^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
    view_digest: ViewDigest
    sealed: RotateSealed


class GithubSealed(BaseModel):
    """The GitHub token, sealed (purpose `github.token`): relayed as ciphertext."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)
    github_token: Envelope = Field(alias="github.token")


class GithubBody(BaseModel):
    """#1198 C16: `request_id`, `view_digest` and `sealed` come from `click/github/seal`; the signer does the rest."""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Annotated[str, StringConstraints(pattern=r"^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
    view_digest: ViewDigest
    sealed: GithubSealed


class RunnerBody(BaseModel):
    """#1198 C14: dashboard execution on or off. `view_digest` is that of the review `click/runner/review` returned for
    the same `on` and `terminals`; the signer acts only on that."""

    model_config = ConfigDict(extra="forbid", strict=True)
    on: bool
    terminals: bool = False  # an extra the person asks for; never on by default
    view_digest: ViewDigest | None = None


class Sign(Step):
    view_digest: ViewDigest
    #: a shown-secret grant only: the key the review was made for (the signer refuses any other)
    reveal_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{87}$")] | None = None
    #: a hub step only (#1198 C10): the backup key sealed to the signer's key, by purpose
    sealed: Sealed | None = None


class Clicks:
    """What each open click request was prepared for (workspace, op, typed body, signer epoch): the View is checked
    against it, and an orphan of an earlier signer is found by its epoch. Not a guard of anything."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ops: OrderedDict[str, tuple[str, str, object, int]] = OrderedDict()

    def add(self, ws: str, request_id: str, op: str, body: object, epoch: int) -> None:
        with self.lock:
            self.ops[request_id] = (ws, op, body, epoch)
            self.ops.move_to_end(request_id)
            while len(self.ops) > REMEMBERED:
                self.ops.popitem(last=False)

    def get(self, request_id: str) -> tuple[str, str, object, int]:
        with self.lock:
            return self.ops.get(request_id, ("", "", None, 0))

    def stale(self, epoch: int) -> list[tuple[str, str]]:
        """(workspace, request id) of every click request a signer of an earlier life prepared."""
        with self.lock:
            return [(ws, rid) for rid, (ws, _, _, e) in self.ops.items() if e != epoch]

    def forget(self, request_id: str) -> None:
        with self.lock:
            self.ops.pop(request_id, None)


def verify(prepared: dict, ws: str, **expect) -> dict:
    """The hand-off's checks on the host's own record (`adapters.handoff.request`): op, ticket key, exact events."""
    return adapters.handoff.request(prepared, ws, **expect)


def register(app: FastAPI, gateway: Gateway):
    def contract(request: Request):
        if request.headers.get("X-Orch-Click-Contract") != "4":
            raise fail("click.contract", "Reload the page: this page is out of date.", exit=9)

    clicks = app.state.clicks = Clicks()
    failed_bumps: dict[str, int] = {}
    handoffs = app.state.handoffs  # the quota of open requests is one per session, shared with the hand-off
    signer = gateway.click
    signer.unsettled = handoffs.any_unsettled  # a hung signer is not replaced while a signature may be mid-submit
    STATES = ("prepared", "bound", "submitted", "cancelled", "expired")

    def ready(step: dict) -> dict:
        """A remote session's call goes to a signer that checks the passkey itself (`remote`, `stepup`), or to none: an
        older signer would ignore both arguments."""
        if step and not signer.passkey_stepups():
            raise fail("click.contract", "Update orch-core: its signing helper cannot check a passkey yet.", exit=9)
        return step

    def human(ws: str, op: str, args: dict):
        return app.state.human(ws, op, args)

    def sweep() -> None:
        """After a signer restart its approvals are gone: cancel the orphaned click requests at the host through OUR
        link and give their quota slots back, so they cannot hold a session's limit for 11 minutes."""
        for ws, rid in clicks.stale(signer.restarts):
            if handoffs.is_unsettled(rid):
                continue  # its signature may be at the host: it settles by itself, nothing is cancelled or forgotten
            try:
                human(ws, "human.cancel", {"request_id": rid})
            except Exception:  # noqa: BLE001 - already settled, or the host is away: the host expires it anyway
                pass
            clicks.forget(rid)
            handoffs.settle_all(rid)
            handoffs.disown(rid)

    def mine(ws: str, request_id: str) -> None:
        """A request of this PERSON in this workspace (any of their sessions); another's, or none, is `not_found`."""
        gateway.enabled(ws)  # the workspace is pinned and the dashboard addon is enabled in it, like the hand-off
        sweep()
        if not handoffs.owns(app.state.person_of(ws), ws, request_id):
            raise fail("not_found", exit=2)

    @app.get("/api/w/{ws}/click/status", dependencies=[Depends(contract)])
    def status(ws: str = Depends(workspace)):
        gateway.enabled(ws)
        card = adapters.click_card(signer.status().get(ws)) or {
            "custody": "none",
            "enabled": False,
            "available": False,
            "problem": "no_signer",
        }
        mode = mode_of(gateway, ws)  # V3 C11: advisory, so the page shows the hub panel; the host decides
        return {**card, "switchable": signer.switchable(), **({"mode": mode} if mode else {})}

    def seal_view_ok(got: object, purpose: str = "custody.passphrase") -> bool:
        """The signer offer for a sealed new passphrase: a request id, a review digest and exactly the one purpose with
        the signer's own key (a P-256 point, 87 base64url characters)."""
        if not isinstance(got, dict) or set(got) != {"request_id", "view_digest", "seal", "expires_in"}:
            return False
        seal = got["seal"]
        return (
            isinstance(got["request_id"], str)
            and re.fullmatch(r"hr_[0-7][0-9A-HJKMNP-TV-Z]{25}", got["request_id"]) is not None
            and isinstance(got["view_digest"], str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", got["view_digest"]) is not None
            and type(got["expires_in"]) is int
            and isinstance(seal, dict)
            and set(seal) == {"v", "alg", "pk", "purposes"}
            and seal["v"] == 1
            and type(seal["v"]) is int
            and seal["alg"] == "P256-HKDF-SHA256-A256GCM"
            and seal["purposes"] == [purpose]
            and isinstance(seal["pk"], str)
            and re.fullmatch(rf"{B64URL}{{87}}", seal["pk"]) is not None
        )

    def owner(ws: str) -> bool:
        me = app.state.person_of(ws)
        return any(m.person == me and m.role == "owner" for m in adapters.members(gateway.call(ws, "member.list")))

    @app.post("/api/w/{ws}/click/custody/seal", dependencies=[Depends(contract)])
    def custody_seal(_body: SealBegin, request: Request, ws: str = Depends(workspace)):
        """The signer's one-use key for the NEW passphrase of "One click off" (#1198 C12): request id, review digest
        and the public key. Owners. Nothing secret passes here."""
        gateway.enabled(ws)
        if not owner(ws):
            raise fail("role.denied", "Only an owner switches signing in the dashboard.", exit=3)
        if not signer.passphrase_seals():
            raise fail(
                "click.contract", "Update orch-core: its signing helper cannot take a sealed passphrase yet.", exit=9
            )
        got = signer.call("custody_off", {"workspace_id": ws, "begin": True})
        if not seal_view_ok(got):
            raise refusal_check(
                "The signing helper's offer for your passphrase is not what it should be. Nothing changed."
            )
        return got

    def rotate_offer_ok(got: object, ws: str) -> bool:
        """The signer's review of a key replacement: this workspace, the old and new device ids, a plain click, and
        exactly the two purposes with the signer's own key (a P-256 point, 87 base64url characters)."""
        if not isinstance(got, dict) or set(got) != {"request_id", "view_digest", "view", "expires_in"}:
            return False
        view = got["view"]
        if not isinstance(view, dict) or type(got["expires_in"]) is not int:
            return False
        seal, workspace = view.get("seal"), view.get("workspace")
        lines = view.get("lines")
        device = re.compile(r"^d_[0-9a-f]{32}$")
        return (
            isinstance(got["request_id"], str)
            and re.fullmatch(r"hr_[0-7][0-9A-HJKMNP-TV-Z]{25}", got["request_id"]) is not None
            and isinstance(got["view_digest"], str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", got["view_digest"]) is not None
            and view.get("op") == "device.rotate"
            and isinstance(workspace, dict)
            and workspace.get("id") == ws
            and isinstance(view.get("old"), str)
            and device.fullmatch(view["old"]) is not None
            and isinstance(view.get("new"), str)
            and device.fullmatch(view["new"]) is not None
            and view["old"] != view["new"]
            and view.get("confirm") == {"kind": "click"}
            and isinstance(lines, list)
            and all(isinstance(x, str) for x in lines)
            and isinstance(seal, dict)
            and set(seal) == {"v", "alg", "pk", "purposes"}
            and seal["v"] == 1
            and type(seal["v"]) is int
            and seal["alg"] == "P256-HKDF-SHA256-A256GCM"
            and seal["purposes"] == ["device.recovery_code", "custody.passphrase"]
            and isinstance(seal["pk"], str)
            and re.fullmatch(rf"{B64URL}{{87}}", seal["pk"]) is not None
        )

    def rotate_gate(request: Request, ws: str) -> None:
        """An owner, a signer that can (begin and sign alike)."""
        gateway.enabled(ws)
        if not owner(ws):
            raise fail("role.denied", "Only an owner replaces a device key in the dashboard.", exit=3)
        if not signer.rotations():
            raise fail("click.contract", "Update orch-core: its signing helper cannot replace a key yet.", exit=9)

    @app.post("/api/w/{ws}/click/rotate/seal", dependencies=[Depends(contract)])
    def rotate_seal(_body: SealBegin, request: Request, ws: str = Depends(workspace)):
        """The review of "Replace this device's key" with the signer's one-use key for the recovery code and the new
        passphrase (#1198 C13). Nothing secret passes here; the signer refuses while One click is on."""
        rotate_gate(request, ws)
        got = signer.call("rotate", {"workspace_id": ws, "begin": True})
        if got.get("done") is True:  # an earlier run had landed the event and only the install was left
            return {"done": True, **(adapters.click_card(got) or {})}
        if not rotate_offer_ok(got, ws):
            raise refusal_check(
                "The signing helper's review of the key replacement is not what it should be. Nothing changed."
            )
        return got

    @app.post("/api/w/{ws}/click/rotate", dependencies=[Depends(contract)])
    def rotate(body: RotateBody, request: Request, ws: str = Depends(workspace)):
        """Replace this computer's device key: ciphertext only, relayed and never opened here (#1198 C13)."""
        step = refuse_step_up(app.state.auth, request, "rotate", body, ws)
        step = ready(step)
        rotate_gate(request, ws)
        row = signer.call(
            "rotate",
            {
                **step,
                "workspace_id": ws,
                "request_id": body.request_id,
                "view_digest": body.view_digest,
                "sealed": {
                    "device.recovery_code": body.sealed.device_recovery_code.model_dump(),
                    "custody.passphrase": body.sealed.custody_passphrase.model_dump(),
                },
            },
        )
        return {"done": True, **(adapters.click_card(row) or {}), "switchable": True}

    def github_gate(ws: str) -> None:
        """An owner, a signer that can (begin, store and remove alike). The token is the owner's own key for GitHub."""
        gateway.enabled(ws)
        if not owner(ws):
            raise fail("role.denied", "Only an owner saves the GitHub token in the dashboard.", exit=3)
        if not signer.github_tokens():
            raise fail("click.contract", "This dashboard needs an update before it can save a GitHub token.", exit=9)

    @app.get("/api/w/{ws}/click/github", dependencies=[Depends(contract)])
    def github_state(ws: str = Depends(workspace)):
        """Is a GitHub token saved? `stored`, `missing`, or `null` when the GitHub add-on is off or asks for no key.
        Read from `addon.list`; the token itself is never read here. Owners only, like the other GitHub routes."""
        gateway.enabled(ws)
        if not owner(ws):
            raise fail("role.denied", "Only an owner sees the GitHub token.", exit=3)
        rows = gateway.call(ws, "addon.list").get("addons", [])
        row = next(
            (r for r in rows if isinstance(r, dict) and r.get("name") == "github" and r.get("state") == "active"), None
        )
        state = None if row is None else row.get("credential")
        return {"credential": state if state in ("stored", "missing") else None}

    @app.post("/api/w/{ws}/click/github/seal", dependencies=[Depends(contract)])
    def github_seal(_body: SealBegin, ws: str = Depends(workspace)):
        """The signer's one-use key for the GitHub token (#1198 C16). Owners. Nothing secret passes here; the signer
        refuses when the GitHub add-on is off."""
        github_gate(ws)
        got = signer.call("github_token", {"workspace_id": ws, "begin": True})
        if not seal_view_ok(got, "github.token"):
            raise refusal_check(
                "The signing helper's offer for your GitHub token is not what it should be. Nothing changed."
            )
        return got

    @app.post("/api/w/{ws}/click/github", dependencies=[Depends(contract)])
    def github_token(body: GithubBody, request: Request, ws: str = Depends(workspace)):
        """Save the GitHub token: ciphertext only, relayed and never opened here (#1198 C16). On a remote session
        this needs the passkey step-up (`stepup.py`)."""
        step = refuse_step_up(app.state.auth, request, "github", body, ws)
        step = ready(step)
        github_gate(ws)
        got = signer.call(
            "github_token",
            {
                **step,
                "workspace_id": ws,
                "request_id": body.request_id,
                "view_digest": body.view_digest,
                "sealed": {"github.token": body.sealed.github_token.model_dump()},
            },
        )
        if got != {"workspace_id": ws, "credential": "stored"}:
            raise fail("internal", "The signing helper answered something unexpected.", exit=9)
        return {"credential": "stored"}

    @app.post("/api/w/{ws}/click/github/remove", dependencies=[Depends(contract)])
    def github_remove(_body: SealBegin, ws: str = Depends(workspace)):
        """Delete the stored GitHub token. It only takes authority away, so it is a plain click everywhere."""
        github_gate(ws)
        got = signer.call("github_token", {"workspace_id": ws, "remove": True})
        if got != {"workspace_id": ws, "credential": "missing"}:
            raise fail("internal", "The signing helper answered something unexpected.", exit=9)
        return {"credential": "missing"}

    @app.post("/api/w/{ws}/click/custody", dependencies=[Depends(contract)])
    def custody(body: CustodyBody, request: Request, ws: str = Depends(workspace)):
        """Switch signing in the dashboard on or off (section 0.8.6 item 3a): owners only; the signer does the work
        (this process never opens a key) and answers the workspace's card. No route here changes a credential."""
        step = refuse_step_up(app.state.auth, request, "custody", body, ws)
        step = ready(step)
        gateway.enabled(ws)
        if not owner(ws):
            raise fail("role.denied", "Only an owner switches signing in the dashboard.", exit=3)
        if not signer.switchable():
            raise fail("click.contract", "Update orch-core: its signing helper cannot switch signing yet.", exit=9)
        if body.resume:
            row = signer.call("custody_on", {**step, "workspace_id": ws, "resume": True})
        elif body.on:  # the proof goes to the signer only; nothing here reads, keeps or logs it
            row = signer.call("custody_on", {**step, "workspace_id": ws, "passphrase": body.passphrase})
        elif body.sealed is not None:  # off with a NEW passphrase (V3-D31): ciphertext only, relayed and never opened
            if not signer.passphrase_seals():
                raise fail(
                    "click.contract",
                    "Update orch-core: its signing helper cannot take a sealed passphrase yet.",
                    exit=9,
                )
            row = signer.call(
                "custody_off",
                {
                    **step,
                    "workspace_id": ws,
                    "request_id": body.request_id,
                    "view_digest": body.view_digest,
                    "sealed": {"custody.passphrase": body.sealed.custody_passphrase.model_dump()},
                },
            )
        else:  # off: the flag only
            row = signer.call("custody_off", {**step, "workspace_id": ws})
        return {**(adapters.click_card(row) or {}), "switchable": True}

    def runner_gate(request: Request, ws: str, body: RunnerBody) -> None:
        """An owner, a signer that can. ON needs a passkey step-up on a remote session (`refuse_step_up`); OFF only
        takes authority away and is a plain click everywhere."""
        gateway.enabled(ws)
        if not owner(ws):
            raise fail("role.denied", "Only an owner switches dashboard execution.", exit=3)
        if not signer.runner_switches():
            raise fail(
                "click.contract", "Update orch-core: its signing helper cannot switch dashboard execution yet.", exit=9
            )

    def runner_view_ok(got: object, ws: str, body: RunnerBody) -> bool:
        if not isinstance(got, dict) or set(got) != {"view", "view_digest"}:
            return False
        view = got["view"]
        if not isinstance(view, dict) or re.fullmatch(r"sha256:[0-9a-f]{64}", str(got["view_digest"])) is None:
            return False
        workspace, lines = view.get("workspace"), view.get("lines")
        return (
            view.get("op") == ("runner.on" if body.on else "runner.off")
            and view.get("on") is body.on
            and isinstance(workspace, dict)
            and workspace.get("id") == ws
            and view.get("confirm") == {"kind": "click"}
            and isinstance(lines, list)
            and all(isinstance(x, str) for x in lines)
            and (RUNNER_LINE in lines) is body.on
            and (view.get("terminals") is body.terminals if body.on else True)
        )

    @app.post("/api/w/{ws}/click/runner/review", dependencies=[Depends(contract)])
    def runner_review(body: RunnerBody, request: Request, ws: str = Depends(workspace)):
        """The review of turning dashboard execution on or off: core's own words, relayed after a check."""
        runner_gate(request, ws, body)
        got = signer.call("runner", {"workspace_id": ws, "review": True, "on": body.on, "terminals": body.terminals})
        if not runner_view_ok(got, ws, body):
            raise refusal_check(
                "The signing helper's review of dashboard execution is not what it should be. Nothing changed."
            )
        return got

    @app.post("/api/w/{ws}/click/runner", dependencies=[Depends(contract)])
    def runner_switch(body: RunnerBody, request: Request, ws: str = Depends(workspace)):
        """Turn dashboard execution on or off after the review (#1198 C14): the signer does what `orch runner enable` or
        `disable` does. This process starts no service and writes no switch."""
        step = refuse_step_up(app.state.auth, request, "runner", body, ws)
        step = ready(step)
        runner_gate(request, ws, body)
        if body.view_digest is None:
            raise fail("invalid.input", "Read the review first.", exit=5)
        got = signer.call(
            "runner",
            {**step, "workspace_id": ws, "on": body.on, "terminals": body.terminals, "view_digest": body.view_digest},
        )
        enabled, terminals = got.get("enabled"), got.get("terminals")
        if type(enabled) is not bool or type(terminals) is not bool:
            raise fail("internal", "The signing helper answered something unexpected.", exit=9)
        return {"enabled": enabled, "terminals": terminals}

    @app.post("/api/w/{ws}/click/images", dependencies=[Depends(contract)])
    def images(body: ImageChunk, ws: str = Depends(workspace)):
        """R3 part B: one chunk of a pasted screenshot, relayed to the signer, which holds the bytes and checks them
        against the digest of the intent the person reviews. This server keeps nothing of them."""
        gateway.enabled(ws)
        if not signer.images():
            raise fail("click.contract", "Update orch-core: its signing helper cannot take pictures yet.", exit=9)
        return signer.call("stash", {"workspace_id": ws, **body.model_dump()})

    @app.post("/api/w/{ws}/click/prepare", dependencies=[Depends(contract)])
    def prepare(body: ClickBody, request: Request, ws: str = Depends(workspace)):
        token = request.state.token
        if app.state.auth.login_kind(token) == "switch":
            raise fail("session.readonly", "Open a full session to sign.", exit=3)
        gateway.enabled(ws)  # pinned workspace, dashboard addon enabled in it (the hand-off's `human()` does this)
        with handoffs.lock:
            handoffs.reserve(token)  # check and take a slot in one step (shared with the hand-off)
        try:
            return _prepare(body, token, ws)
        except BaseException:
            handoffs.release(token)
            raise

    def _prepare(body, token, ws):
        if isinstance(body, NewTicketSequence | QuickStart) and body.agent is not None:
            raise HostError(
                {
                    "code": "human.click_refused",
                    "reason": "terminal_only",
                    "retryable": False,
                    "message": "Agent access needs its own grant confirmation. Create without starting first.",
                },
                exit=3,
            )
        grant = isinstance(body, RunnerGrant)
        if isinstance(body, AgentAccess) and not signer.reveals():
            raise fail("click.contract", "Update orch-core: its signing helper cannot show a grant secret yet.", exit=9)
        if isinstance(body, NewTicketSequence | EditTicket | QuickStart | AddonEnable) and not signer.sequences():
            # an older signer would ignore `sequence` and sign the first step only
            raise fail(
                "click.contract", "Update orch-core: its signing helper can't sign a ticket and its text yet.", exit=9
            )
        if isinstance(body, HUB_STEPS) and not signer.backup_keys():
            raise fail("click.contract", "Update orch-core: its signing helper cannot take a backup key yet.", exit=9)
        if isinstance(body, RepoSet):  # the host's path check, before any request exists (plain refusal in the dialog)
            gateway.check_repo_path(ws, body.path)
        planned = plan(gateway, ws, body)  # the only place an op name is chosen
        if planned.sequence and planned.sequence["intent"].get("images") and not signer.images():
            raise fail("click.contract", "Update orch-core: its signing helper cannot sign pictures yet.", exit=9)
        files = planned.files

        def call(idem: str):
            sent = {"workspace_id": ws, "op": planned.op, "args": planned.args, "idem": idem}
            if files:
                sent["files"] = files
            if planned.sequence:
                sent["sequence"] = planned.sequence
            if isinstance(body, AgentAccess):
                sent["reveal_key"] = body.reveal_key  # public; bound into the review the signer builds
            if grant:
                sent["deposit"] = True  # the deposit-only grant mode: the secret goes to the runner, never here
            return signer.call("prepare", sent)

        idem_key = planned.idem(ws, 0)
        start = max(body.retry, failed_bumps.get(idem_key, 0))
        try:
            got = send_prepare(planned, ws, start, call, lambda: human(ws, "human.pending", {}))
        except HostError as exc:
            sweep()
            said = str(exc.error.get("message", "")) if exc.error["code"] == "human.request_state" else ""
            if said != SUBMITTED:
                raise
            # This intent's attempt was signed: answer "Already done" from the host's record.
            # The signer drops the terminal request id. Replay the exact binding through our link to obtain
            # the host record; opts.idem guarantees this cannot create another request.
            signed_retry = getattr(exc, "retry_used", start)  # the retry whose attempt was signed
            device = signer.status().get(ws, {}).get("device")
            if not device:
                raise fail("host.unavailable", "Cannot read the earlier signing record.") from None
            try:
                prepared = app.state.human(
                    ws,
                    "human.prepare",
                    {"op": planned.op, "args": planned.args, "signer": {"device": device, "auth": "convenience"}},
                    idem=planned.idem(ws, signed_retry),
                    files=files,
                )
            except HostError as exc:
                # The same mapping as send_prepare: never a raw idem code to the browser (Opus re-check N3).
                code = exc.error.get("code")
                if code == "idem.expired":
                    raise fail(
                        "intent.expired",
                        "This attempt is older than the host keeps records (24 h). Start again only if you intend "
                        "another attempt; an earlier one may have been signed.",
                        exit=5,
                    ) from None
                if code == "idem.conflict":
                    raise fail(
                        "intent.blocked",
                        "The earlier attempt was signed, but its record can't be read here. Start again only if "
                        "you intend another attempt; an earlier one may have been signed.",
                        exit=5,
                    ) from None
                raise
            if prepared.get("request", {}).get("state") != "submitted":
                raise fail("internal", "The host did not return the submitted intent.")
            error = read_done(human, ws, prepared["request"]["id"])
            if error.error["code"] == "intent.failed":
                failed_bumps[idem_key] = signed_retry + 1
            raise error from None
        rid = got["request_id"]
        clicks.forget(rid)  # adopted by the new signer; it is no longer an orphan
        sweep()
        clicks.add(ws, rid, planned.op, body, signer.restarts)
        if planned.sequence:  # a display aid for `state` (how far a stopped run got); it decides nothing
            handoffs.note_intent(planned.person, rid, planned.sequence["intent"])
        handoffs.own(planned.person, ws, rid, TTL)
        handoffs.commit(token, ws, rid, until=handoffs.clock() + TTL, kind="click")
        return {k: got[k] for k in ("request_id", "expires_at", "op", "confirm") if k in got}

    @app.post("/api/w/{ws}/click/view", dependencies=[Depends(contract)])
    def view(body: Step, ws: str = Depends(workspace)):
        mine(ws, body.request_id)
        rid = body.request_id
        _, op, wanted, _ = clicks.get(rid)
        # The hand-off's gate on the host's own record: it must be the action that was asked for (the ticket, the exact
        # events and values), read through OUR link. A host that prepared anything else is refused before it is shown.
        try:
            prepared = human(ws, "human.get", {"request_id": rid})
            verified = verify(
                prepared,
                ws,
                expect_op=op,
                expect_key=getattr(wanted, "key", None),
                expect_events=expected(wanted, app.state.person_of(ws)) if wanted is not None else None,
                expect_material=expected_material(wanted) if wanted is not None else None,
            )
        except HostError as exc:
            if exc.error.get("code") != "internal":
                raise
            ended(ws, rid)
            raise refusal_check(
                "The host prepared something other than what was asked for. Nothing was shown."
            ) from None
        got = _signer_call("view", ws, rid)
        # The signer's View and digest, unchanged: nothing is added, hidden or re-rendered here.
        shown = got.get("view") if isinstance(got, dict) else None
        if not isinstance(shown, dict) or shown.get("op") != op or shown.get("risk") != RISK:
            raise refusal_check("The review is not what was asked for, or lacks the risk line. Cancel it.")
        if not contract4_view_ok(shown):  # run 13 C3: the headline and the signer's own risk lines
            raise refusal_check("The review lacks its headline or its risk lines. Nothing was shown. Cancel it.")
        # The runner grant asked for `deposit:true`; its View must say so (and carry the deposit and risk lines), and
        # no other View may claim a deposit: the two must agree, or nothing is shown (no Sign button).
        if isinstance(wanted, RunnerGrant) != (shown.get("deposit") is True) or (
            isinstance(wanted, RunnerGrant) and not deposit_view_ok(shown, ws)
        ):
            raise refusal_check("The review does not say where the grant goes. Nothing was shown. Cancel it.")
        # Review I1: the grant's View must be of the very record `expected()` checked (the host's fingerprint), so the
        # hours and scope signed are the ones verified.
        # A shown-secret grant: the View must carry the sealed purpose for THIS page's key, and no other View may.
        if isinstance(
            wanted, HUB_STEPS
        ):  # a hub step: the signer's own key for the backup key, and nothing else sealed
            if not (backup_view_ok(shown, ws) or plain_hub_view_ok(shown, ws)):
                raise refusal_check(
                    "The review of this hub step is not the one expected. Nothing was shown. Cancel it."
                )
        elif isinstance(wanted, AgentAccess) != (shown.get("seal") is not None) or (
            isinstance(wanted, AgentAccess) and not reveal_view_ok(shown, ws, wanted.reveal_key)
        ):
            raise refusal_check(
                "The review does not say that the secret is shown here once. Nothing was shown. Cancel it."
            )
        if isinstance(wanted, RunnerGrant | AgentAccess) and (
            not isinstance(verified, dict)
            or not verified.get("fingerprint")
            or shown.get("fingerprint") != verified["fingerprint"]
        ):
            raise refusal_check("The review is not of the request the host prepared. Nothing was shown. Cancel it.")
        return got

    def ended(ws: str, rid: str) -> None:
        """Nothing more can be signed on this request: cancel it at the host (best effort), free its slots."""
        if handoffs.is_unsettled(rid):
            return  # never cancel a signature that may be in flight or unsettled
        for call in (
            lambda: signer.call("cancel", {"workspace_id": ws, "request_id": rid}),
            lambda: human(ws, "human.cancel", {"request_id": rid}),
        ):
            try:
                call()
                break
            except Exception:  # noqa: BLE001
                continue
        clicks.forget(rid)
        handoffs.settle_all(rid)
        handoffs.disown(rid)  # it can never be signed: it looks like one that never existed

    def _signer_call(op: str, ws: str, rid: str, **extra):
        try:
            return signer.call(op, {"workspace_id": ws, "request_id": rid, **extra})
        except HostError:
            sweep()  # a restart may have happened inside this call
            raise

    @app.post("/api/w/{ws}/click/state", dependencies=[Depends(contract)])
    def state(body: Step, ws: str = Depends(workspace)):
        """Where a request stands at the host (read through our own link): the answer to an unknown outcome."""
        rid = body.request_id
        mine(ws, rid)
        try:
            rows = human(ws, "human.pending", {"request_id": rid})["requests"]
        except HostError:
            if handoffs.is_unsettled(rid):
                return {"id": rid, "state": "unknown"}
            raise
        row = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get("id") == rid else None
        if row is None:
            if handoffs.is_unsettled(rid):
                return {"id": rid, "state": "unknown"}
            raise fail("not_found", exit=2)
        got = row.get("state")
        if got not in STATES:
            return {"id": rid, "state": "unknown"}  # the host sent something this dashboard does not know: say so
        if got not in ("prepared", "bound"):
            handoffs.settle_all(rid)  # every session's slot
            handoffs.settled(rid)
            clicks.forget(rid)
        if got == "submitted" and row.get("seq") is None:
            raise done_error({}, row)
        out: dict = {"id": rid, "state": got}
        intent = handoffs.intent_of(app.state.person_of(ws), rid)
        key = row.get("key")
        if intent is not None and isinstance(key, str) and got != "prepared":
            # A stopped sequence: which ticket exists and how many steps are in it, read from the host (never typed).
            progress = sequence_progress(
                gateway, ws, key, intent, person=app.state.person_of(ws), since=row.get("created_at")
            )
            if progress is not None:
                if intent.get("agent") is not None:
                    progress["last"] = "grant"
                out |= {"key": key, "progress": progress}
        return out

    @app.post("/api/w/{ws}/click/sign", dependencies=[Depends(contract)])
    def sign(body: Sign, request: Request, ws: str = Depends(workspace)):
        step = refuse_step_up(app.state.auth, request, "sign", body, ws)  # where a typed key arrives: fail closed
        step = ready(step)
        mine(ws, body.request_id)
        rid = body.request_id
        wanted = clicks.get(rid)[2]
        # In flight counts as unsettled from here on, BEFORE the channel is used: nothing here cancels the request or
        # replaces the signer under it, and the mark is already there if the answer is lost. (A second signature of
        # the request is refused by the signer and the host, which are the ones that know.)
        handoffs.begin_sign(ws, rid, handoffs.clock() + TTL + 60)
        try:
            sent = {**step, "workspace_id": ws, "request_id": rid, "view_digest": body.view_digest}
            if isinstance(wanted, AgentAccess):
                if body.reveal_key != wanted.reveal_key:  # the key of the review, nothing else (the signer checks too)
                    raise fail("invalid.input", "reveal_key is not the key this review was made for", exit=1)
                sent["reveal_key"] = body.reveal_key
            elif body.reveal_key is not None:
                raise fail("invalid.input", "only a grant shown in the page takes reveal_key", exit=1)
            if isinstance(wanted, HUB_STEPS):  # the backup key, sealed: relayed as ciphertext, never read or logged
                if body.sealed is not None:  # none: the signer kept the person key (run-9 feedback 6)
                    sent["sealed"] = {"hub.backup_key": body.sealed.hub_backup_key.model_dump()}
            elif body.sealed is not None:
                raise fail("invalid.input", "only a hub step takes a sealed value", exit=1)
            got = signer.call("sign", sent, lost=lost_sign())
        except HostError as exc:
            # Before follow-up host reads, which may themselves fail. A definite failure never clears an earlier or a
            # concurrent unknown outcome; the rule lives in Handoffs.end_sign.
            outcome = "uncertain" if exc.error.get("reason") == "uncertain" else "definite"
            handoffs.end_sign(ws, rid, handoffs.clock() + TTL + 60, outcome)
            if exc.error.get("code") == "human.request_state":
                rows = human(ws, "human.pending", {"request_id": rid}).get("requests", [])
                if len(rows) == 1 and rows[0].get("state") == "submitted":
                    prepared = human(ws, "human.get", {"request_id": rid})
                    handoffs.settled(rid)
                    handoffs.settle_all(rid)
                    clicks.forget(rid)
                    raise done_error(prepared, rows[0]) from None
            sweep()
            if exc.error.get("reason") == "disabled":
                ended(ws, rid)  # the flag was switched off: this request can never be signed, it reads as ended
            elif (exc.error.get("reason") or exc.error["code"]) in ENDED:
                clicks.forget(rid)
                handoffs.settle_all(rid)
                handoffs.disown(rid)
            raise  # an uncertain outcome keeps its slot and its request: it settles at the host
        except BaseException:
            # Anything else: nobody knows whether the signer signed.
            handoffs.end_sign(ws, rid, handoffs.clock() + TTL + 60, "uncertain")
            raise
        handoffs.end_sign(ws, rid, handoffs.clock() + TTL + 60, "ok")
        clicks.forget(rid)
        handoffs.settle_all(rid)
        handoffs.settled(rid)
        result = got.get("result") if isinstance(got.get("result"), dict) else {}
        out = {"state": "submitted", "duplicate": bool(got.get("duplicate"))}
        if isinstance(wanted, AgentAccess):
            return out | reveal_answer(got)  # the grant id and the sealed envelope; never `result`
        if not isinstance(wanted, RunnerGrant):
            # A ticket or quick task whose sequence ends in the agent access: the page needs the ticket's key to start
            # the agent for exactly that ticket, so the key and the seq are answered in this case too.
            if isinstance(result.get("key"), str) and adapters.runs.KEY.fullmatch(result["key"]):
                out["key"] = result["key"]
            if type(result.get("seq")) is int:
                out["seq"] = result["seq"]
            if "key" not in out and "deposit" in got:
                # The result is the last step's (the grant): the ticket's key is on the host's own row of this request.
                try:
                    rows = human(ws, "human.pending", {"request_id": rid}).get("requests", [])
                    row = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get("id") == rid else {}
                except HostError:
                    row = {}
                if isinstance(row.get("key"), str) and adapters.runs.KEY.fullmatch(row["key"]):
                    out["key"] = row["key"]
        if isinstance(wanted, RunnerGrant) or "deposit" in got:
            # Of the deposit only its enum, its id and the grant id; never `result` (section 0.8.6 item 6).
            return out | deposit_answer(got, rid)
        return out

    @app.post("/api/w/{ws}/click/cancel", dependencies=[Depends(contract)])
    def cancel(body: Step, ws: str = Depends(workspace)):
        mine(ws, body.request_id)
        rid = body.request_id
        if handoffs.is_unsettled(rid):
            raise refusal_check("A signature of this is settling; it cannot be cancelled now.")
        try:
            got = _signer_call("cancel", ws, rid)
        except HostError as exc:
            if (exc.error.get("reason") or exc.error["code"]) in ENDED | {"not_found"}:
                ended(ws, rid)  # the signer forgot it: cancel it at the host ourselves
            raise
        clicks.forget(rid)
        handoffs.settle_all(rid)
        handoffs.disown(rid)
        return {"id": rid, "state": got.get("state")}

    def cancel_session(token: str):
        """Logout: the click requests this session left open are cancelled (best effort, each on its own), except one
        whose signature is unknown and one another session of the person holds too."""
        for rid, ws in handoffs.held(token, "click"):
            if handoffs.is_unsettled(rid):
                continue  # logout never cancels a signature whose outcome is unknown
            if handoffs.shared(token, rid):
                handoffs.settle(token, rid)
                continue
            try:
                signer.call("cancel", {"workspace_id": ws, "request_id": rid})
            except Exception:  # noqa: BLE001 - one failed cancel must not stop the rest, nor the logout
                pass
            clicks.forget(rid)
            handoffs.settle(token, rid)

    app.state.cancel_clicks = cancel_session
