"""The View the dashboard shows (orch-v2-host §0.8.6 item 5): built by the signer, after every §0.8.1 check, from the
host's answer only; ``lines`` is the terminal review of ``orch sign``, line for line (same escaping)."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from orch import canon
from orch.host import signcontent, signer

from .protocol import DEPOSIT_LINE, DEPOSIT_MAX_HOURS, DEPOSIT_RISK, RISK, op_class, refused

__all__ = ["MAX_EVENTS", "MAX_VIEW_BYTES", "build", "check_prepared", "deposit_checks", "digest", "invariant"]

MAX_EVENTS = 16
MAX_VIEW_BYTES = 1024 * 1024
#: the fields of a prepared request that never change from prepare to submit (§0.8.1 "Invariant bytes"); the click
#: request is bound at prepare, so the events' ``actor.device`` and ``auth`` are fixed too
INVARIANT = ("op", "workspace_id", "log", "events", "material", "review_digest", "fingerprint", "origin")


def invariant(prepared: Mapping[str, Any]) -> bytes:
    """The canonical bytes of everything a click signs or relies on (byte-for-byte comparison at view and sign)."""
    return canon.dumps({k: prepared.get(k) for k in INVARIANT})


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


def check_prepared(
    prepared: Mapping[str, Any],
    *,
    workspace_id: str,
    person: str,
    device: str,
    files: Mapping[str, Any] | None,
    deposit: bool = False,
) -> str:
    """Every §0.8.1 signer check, then the click rules; returns the op class (``click``). Raises
    ``human.click_refused`` (``check_failed`` or ``terminal_only``). ``deposit``: the request was prepared in
    deposit-only grant mode; only :func:`deposit_checks`' grant passes then."""
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
    if deposit:
        deposit_checks(prepared)
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
) -> dict[str, Any]:
    """The View (after :func:`check_prepared`). ``checks`` are listed apart from ``lines`` so their time can change
    without changing :func:`digest`. A deposit grant (and only one) gets ``deposit: true`` and ``deposit_line``, and
    its ``lines`` end with the tier-A sentence and that line."""
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
    if deposit:
        deposit_checks(prepared)
        view["lines"] = [*view["lines"], "", DEPOSIT_RISK, DEPOSIT_LINE]
        view["deposit"] = True
        view["deposit_line"] = DEPOSIT_LINE
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
