"""The View the dashboard shows (orch-v2-host §0.8.6 item 5): built by the signer, after every §0.8.1 check, from the
host's answer only; ``lines`` is the terminal review of ``orch sign``, line for line (same escaping)."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from orch import canon
from orch.host import signer

from .protocol import RISK, op_class, refused

__all__ = ["MAX_EVENTS", "MAX_VIEW_BYTES", "build", "check_prepared", "digest", "invariant", "word"]

MAX_EVENTS = 16
MAX_VIEW_BYTES = 1024 * 1024
#: the fields of a prepared request that never change from prepare to submit (§0.8.1 "Invariant bytes"); the click
#: request is bound at prepare, so the events' ``actor.device`` and ``auth`` are fixed too
INVARIANT = ("op", "workspace_id", "log", "events", "material", "review_digest", "fingerprint", "origin")


def invariant(prepared: Mapping[str, Any]) -> bytes:
    """The canonical bytes of everything a click signs or relies on (byte-for-byte comparison at view and sign)."""
    return canon.dumps({k: prepared.get(k) for k in INVARIANT})


def word(prepared: Mapping[str, Any], workspace: Callable[[str], tuple[str, str] | None]) -> str:
    """The typed word (orch-52 decision, 12 Oct 2026): workspace-scope ``policy.set``/``settings.set`` are
    ``<PREFIX>~<WS8>/POLICY`` and ``<PREFIX>~<WS8>/SETTINGS`` (``WS8``: the last 8 characters of the workspace id, the
    prefix from the signer's own registry); addon ops ``<name>~<WS8>`` (addon names repeat across workspaces); ticket
    ops ``KEY/FRAG`` and member ops the person id (unique already), as §0.8.1 defines."""
    op, wid = prepared["op"], prepared["workspace_id"]
    known = workspace(wid)
    if known is None:
        raise refused("check_failed", "this workspace is not in your own registry")
    tag = f"{signer._clean(known[1])}~{wid[-8:]}"
    ticket = prepared["material"].get("ticket")
    if op in ("policy.set", "settings.set") and ticket is None:
        return f"{tag}/{'POLICY' if op == 'policy.set' else 'SETTINGS'}"
    if op in ("addon.grant", "addon.purge"):
        return f"{signer._clean(prepared['events'][0].get('name'))}~{wid[-8:]}"
    return signer.confirm_word(prepared)


def check_prepared(
    prepared: Mapping[str, Any],
    *,
    workspace_id: str,
    person: str,
    device: str,
    files: Mapping[str, Any] | None,
) -> str:
    """Every §0.8.1 signer check, then the click rules; returns the op class (``click`` or ``word``). Raises
    ``human.click_refused`` (``check_failed`` or ``terminal_only``)."""
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
    kind: str,
    device: str,
    expires_at: str,
    artifacts: str | None,
) -> dict[str, Any]:
    """The View (after :func:`check_prepared`). ``checks`` are listed apart from ``lines`` so their time can change
    without changing :func:`digest`."""
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
        "confirm": {"kind": "word", "word": word(prepared, workspace)} if kind == "word" else {"kind": "click"},
        "expires_at": expires_at,
        "auth": "convenience",
        "device": device,
        "artifacts": artifacts,
    }
    if len(canon.dumps(view)) > MAX_VIEW_BYTES:
        raise refused("terminal_only", "the review is too large for the dashboard; use the terminal")
    return view


def digest(view: Mapping[str, Any]) -> str:
    """``sha256:`` over the View without the checks' times (freshness, not content)."""
    stable = copy.deepcopy(dict(view))
    stable["checks"] = [{k: v for k, v in c.items() if k != "at"} for c in stable.get("checks") or []]
    return "sha256:" + hashlib.sha256(canon.dumps(stable)).hexdigest()
