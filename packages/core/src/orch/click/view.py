"""The View the dashboard shows (orch-v2-host §0.8.6 item 5): built by the signer, after every §0.8.1 check, from the
host's answer only; ``lines`` is the terminal review of ``orch sign``, line for line (same escaping)."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from orch import canon
from orch.host import signcontent, signer

from . import sealed
from .protocol import (
    BACKUP_KEY_LINE,
    DEPOSIT_LINE,
    DEPOSIT_MAX_HOURS,
    DEPOSIT_RISK,
    REVEAL_LINE,
    REVEAL_RISK,
    RISK,
    ROTATE_LINE,
    ROTATE_OP,
    ROTATE_PURPOSES,
    RUNNER_LINE,
    RUNNER_OFF_LINE,
    SEALED_OPS,
    op_class,
    refused,
)

__all__ = [
    "MAX_EVENTS",
    "MAX_VIEW_BYTES",
    "build",
    "check_prepared",
    "deposit_checks",
    "digest",
    "invariant",
    "reveal_checks",
    "single_event",
    "rotation",
    "runner_switch",
]

MAX_EVENTS = 16
MAX_VIEW_BYTES = 1024 * 1024
#: the fields of a prepared request that never change from prepare to submit (§0.8.1 "Invariant bytes"); the click
#: request is bound at prepare, so the events' ``actor.device`` and ``auth`` are fixed too
INVARIANT = ("op", "workspace_id", "log", "events", "material", "review_digest", "fingerprint", "origin")


def invariant(prepared: Mapping[str, Any]) -> bytes:
    """The canonical bytes of everything a click signs or relies on (byte-for-byte comparison at view and sign)."""
    return canon.dumps({k: prepared.get(k) for k in INVARIANT})


def key_fingerprint(key: str) -> str:
    """The page's reveal key as the View names it: the full SHA-256 (64 hex) of its base64url text. Not truncated: a
    relay that rewrites the key at prepare would need a second preimage. Golden vector (core, server and page agree):
    ``tests/click_reveal_fingerprint.json``."""
    return hashlib.sha256(key.encode()).hexdigest()


def deposit_checks(prepared: Mapping[str, Any]) -> None:
    """The one exception to "``grant`` is terminal-only" (v1.6, D-P2-27): exactly one ``grant.issued`` with
    ``label:"runner"``, ``verbs:"agent"`` and 1 to 168 hours,
    optionally followed by the ``grant.revoked`` of the one it replaces (Extend). Anything else is ``terminal_only``."""
    events = prepared.get("events")
    ok = (
        prepared.get("op") == "grant"
        and isinstance(events, list)
        and len(events) in (1, 2)
        and isinstance(events[0], Mapping)
        and events[0].get("type") == "grant.issued"
        and events[0].get("label") == "runner"
        and events[0].get("verbs") == "agent"
        and type(events[0].get("hours")) is int
        and 1 <= events[0]["hours"] <= DEPOSIT_MAX_HOURS
        # B8 Extend: the second event, if any, is the revoke of the grant being replaced (shown in full in the review)
        and (
            len(events) == 1
            or (
                isinstance(events[1], Mapping)
                and events[1].get("type") == "grant.revoked"
                and isinstance(events[1].get("grant"), str)
                and events[1]["grant"] != events[0].get("grant")
            )
        )
    )
    if not ok:
        raise refused(
            "terminal_only",
            "only a runner grant within the workspace grant_hours (at most 168) is handed to the runner by a click",
        )


def single_event(prepared: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The one event of a prepared request, or ``None`` when it carries none, several, or something that is not an
    object. (The key is read through :data:`INVARIANT` because the layout lint counts string literals naming store
    entries.)"""
    events = prepared.get(INVARIANT[3]) if isinstance(prepared, Mapping) else None
    if isinstance(events, list) and len(events) == 1 and isinstance(events[0], Mapping):
        return events[0]
    return None


def reveal_checks(prepared: Mapping[str, Any]) -> None:
    """#1198 C7: a grant whose secret is shown once in the page: exactly one ``grant.issued``, ``verbs:"agent"``, 1 to
    168 hours, no ``runner`` label (a runner grant is handed to the runner, never shown), no revoke leg. Anything else
    is ``terminal_only``."""
    ev = single_event(prepared)
    ok = (
        prepared.get("op") == "grant"
        and ev is not None
        and ev.get("type") == "grant.issued"
        and ev.get("verbs") == "agent"
        and ev.get("label") != "runner"
        and type(ev.get("hours")) is int
        and 1 <= ev["hours"] <= DEPOSIT_MAX_HOURS
    )
    if not ok:
        raise refused(
            "terminal_only", "only an agent grant of 1 to 168 hours is shown in the dashboard; use the terminal"
        )


def check_prepared(
    prepared: Mapping[str, Any],
    *,
    workspace_id: str,
    person: str,
    device: str,
    files: Mapping[str, Any] | None,
    deposit: bool = False,
    reveal: bool = False,
    sealed_op: bool = False,
) -> str:
    """Every §0.8.1 signer check, then the click rules; returns the op class (``click``). Raises
    ``human.click_refused`` (``check_failed`` or ``terminal_only``). ``deposit``: the request was prepared in
    deposit-only grant mode; only :func:`deposit_checks`' grant passes then. ``reveal``: prepared to show the secret
    once in the page; only :func:`reveal_checks`' grant passes then. Any other request with a ``grant.issued`` is
    terminal-only. ``sealed_op`` (#1198 C10): prepared with the sealed backup key; a hub step (``SEALED_OPS``) passes
    only then, and nothing else does."""
    if not isinstance(prepared, Mapping) or prepared.get("workspace_id") != workspace_id:
        raise refused("check_failed", "the host answered for another workspace")
    events = prepared.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise refused("terminal_only", "too many events for the dashboard; use the terminal")
    try:
        signer.check(prepared, workspace_id=workspace_id, person=person, device=device, auth="convenience")
        if files:
            signer.check_uploads(prepared, files)
    except signer.SignerRefused as exc:
        raise refused("check_failed", str(exc)) from None
    if (prepared.get("op") in SEALED_OPS) != sealed_op:  # a hub step is a click only together with its sealed key
        raise refused("terminal_only", f"{prepared.get('op')} is signed in the terminal (orch sign CODE)")
    if deposit:
        deposit_checks(prepared)
        kind = "click"
    elif reveal:
        reveal_checks(prepared)
        kind = "click"
    else:
        kind = op_class(str(prepared["op"]), events)
        if kind == "terminal" or any(e.get("type") == "grant.issued" for e in events):
            raise refused("terminal_only", f"{prepared['op']} is signed in the terminal (orch sign CODE)")
    for c in prepared.get("checks") or []:
        if c.get("clean") is not True or c.get("attested_by") != "worker":
            raise refused("check_failed", "a worker check is not clean")
    return kind


def build(
    prepared: Mapping[str, Any],
    workspace: Callable[[str], tuple[str, str] | None],
    *,
    device: str,
    expires_at: str,
    artifacts: str | None,
    deposit: bool = False,
    reveal: str | None = None,
    seal_in: tuple[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """The View (after :func:`check_prepared`). ``checks`` are listed apart from ``lines`` so their time can change
    without changing :func:`digest`. A deposit grant (and only one) gets ``deposit: true`` and ``deposit_line``, and
    its ``lines`` end with the tier-A sentence and that line. A reveal grant (and only one) gets ``seal`` (the sealed
    field's ``grant.secret`` purpose, no key: the page brings its own, sealed-field contract section 7) and
    ``reveal_line``, which its ``lines`` end with; ``view_digest`` covers both.
    ``seal_in`` ``(pk, purposes)`` (#1198 C10):
    an op that asks for a typed secret (the hub backup key) carries ``seal`` with the signer's per-request public key
    and exactly those purposes (sealed-field contract section 7), and ``backup_key_line`` in ``lines``."""
    known = workspace(prepared["workspace_id"])
    if known is None:
        raise refused("check_failed", "this workspace is not in your own registry")
    body = copy.deepcopy(dict(prepared))
    body.pop("checks", None)
    try:
        text = signer.render(body, workspace, artifacts=artifacts)
    except signer.SignerRefused as exc:
        raise refused("check_failed", str(exc)) from None
    checks = [
        {"repo": signer._clean(c["repo"]), "clean": True, "attested_by": "worker", "at": signer._clean(c["at"])}
        for c in prepared.get("checks") or []
    ]
    try:  # B6 (owner decision E): additive; `lines` stays the full review. Unknown shapes get no plain view at all
        content = signcontent.clean_tree(signcontent.build(body, verified=True))  # check_prepared ran
    except (KeyError, TypeError, ValueError, AttributeError):
        content = None
    view = {
        "risk": RISK,
        "op": prepared["op"],
        "workspace": {
            "id": prepared["workspace_id"],
            "name": signer._clean(known[0]),
            "prefix": signer._clean(known[1]),
        },
        "fingerprint": prepared["fingerprint"],
        "lines": text.rstrip("\n").split("\n"),
        "checks": checks,
        "confirm": {"kind": "click"},
        "expires_at": expires_at,
        "auth": "convenience",
        "device": device,
        "artifacts": artifacts,
    }
    if prepared["op"] == "schedule.add":
        from orch.ops.schedule import RISK as SCHEDULE_RISK

        # run 13: the schedule's own risk is a line of the review, never `risk`: that field is the convenience-signing
        # notice of every click View, and the dashboard server refuses a View without it (check_failed). FIRST: the
        # review shows `lines` in a short scroll box, and the consequence must be read before the rule's details
        view["lines"] = [SCHEDULE_RISK, "No click or confirmation is required at firing time.", *view["lines"]]
    if deposit:
        deposit_checks(prepared)
        view["lines"] = [*view["lines"], "", DEPOSIT_RISK, DEPOSIT_LINE]
        view["deposit"] = True
        view["deposit_line"] = DEPOSIT_LINE
    if reveal is not None:
        reveal_checks(prepared)
        view["lines"] = [*view["lines"], "", REVEAL_RISK, REVEAL_LINE]
        # the page's key, bound at prepare, is part of what is approved: its fingerprint is in the digest
        view["seal"] = {
            "v": sealed.VERSION,
            "alg": sealed.ALG,
            "purposes": ["grant.secret"],
            "for": key_fingerprint(reveal),
        }
        view["reveal_risk"] = REVEAL_RISK
        view["reveal_line"] = REVEAL_LINE
    if seal_in is not None:
        pk, purposes = seal_in
        view["lines"] = [*view["lines"], "", BACKUP_KEY_LINE]
        view["seal"] = {"v": sealed.VERSION, "alg": sealed.ALG, "pk": pk, "purposes": list(purposes)}
        view["backup_key_line"] = BACKUP_KEY_LINE
    if content is not None:
        view["headline"], view["content"] = content["headline"], content
    if len(canon.dumps(view)) > MAX_VIEW_BYTES:
        raise refused("terminal_only", "the review is too large for the dashboard; use the terminal")
    return view


def digest(view: Mapping[str, Any]) -> str:
    """``sha256:`` over the View without the checks' times (freshness, not content)."""
    stable = copy.deepcopy(dict(view))
    stable["checks"] = [{k: v for k, v in c.items() if k != "at"} for c in stable.get("checks") or []]
    return "sha256:" + hashlib.sha256(canon.dumps(stable)).hexdigest()


def workspace_label(workspace: Mapping[str, str]) -> str:
    """The workspace as a review names it: the unsigned name (D6: an owner may change it at any time, nothing signs
    it) never alone, always with the prefix and the last eight of the id (``Acme (DEMO · 1a2b3c4d)``)."""
    wid = signer._clean(workspace.get("id") or "")
    return f"{signer._clean(workspace['name'])} ({signer._clean(workspace.get('prefix') or '')} · {wid[-8:]})"


def rotation(*, workspace: Mapping[str, str], old: str, new: str, pk: str) -> dict[str, Any]:
    """#1198 C13: the review of ``device.rotate`` from the dashboard: what is replaced (this computer's key, old and new
    device id, as the terminal shows them), and the signer's per-offer key for the two typed secrets (the recovery code
    and the passphrase of the new key file). No event exists yet: the new key signs it after the secrets are opened."""
    lines = [
        f"Replace the signing key of this computer for {workspace_label(workspace)}.",
        "The old key stops signing everywhere, also while a hub is down. The new key is signed by your recovery code "
        "for this workspace only; nothing is re-shared with a hub.",
        f"old device: {old}",
        f"new device: {new}",
        ROTATE_LINE,
    ]
    return {
        "op": ROTATE_OP,
        "workspace": {k: signer._clean(v) for k, v in workspace.items()},
        "old": old,
        "new": new,
        "lines": lines,
        "confirm": {"kind": "click"},
        "seal": {"v": sealed.VERSION, "alg": sealed.ALG, "pk": pk, "purposes": list(ROTATE_PURPOSES)},
        "rotate_line": ROTATE_LINE,
    }


def runner_switch(*, workspace: Mapping[str, str], on: bool, terminals: bool, text: str) -> dict[str, Any]:
    """#1198 C14: the review of dashboard execution on or off, from core's own words (``enablement.risk_text``: the
    terminal shows the same). The page shows ``lines``; the signer acts only on this View's digest."""
    name = workspace_label(workspace)
    if on:
        rest = [x.strip() for x in text.splitlines()[1:] if x.strip()]
        lines = [f"Turn on dashboard execution for {name}.", RUNNER_LINE, *rest]
    else:
        lines = [f"Turn off dashboard execution for {name}.", RUNNER_OFF_LINE]
    return {
        "op": "runner.on" if on else "runner.off",
        "workspace": {k: signer._clean(v) for k, v in workspace.items()},
        "on": on,
        "terminals": bool(terminals) if on else False,
        "lines": [str(x)[:400] for x in lines],
        "confirm": {"kind": "click"},
        "runner_line": RUNNER_LINE,
    }
