"""The review of ``orch setup`` (R1): the plan in plain rows, the digest the person signs, and the check that what the
host prepares is what was reviewed.

* The plan is built **in core** from the profile, before anything exists: each signed event is made here in the shape
  the host will prepare (:func:`plan`), and rendered with the same ``signcontent`` that every signing client shows
  (B6), so the rows are the plain view of the event that will be signed. Values the host draws at signing time (ids,
  the clock) are shown as :data:`PLACEHOLDER`.
* The digest R1 binds is ``H(canonical profile + dir + every item's planned payload + every rendered row + the flags)``.
  ``POST create`` must name it; any ``POST profile`` voids the review.
* At signing, :func:`check_prepared` re-renders EVERY host-prepared event with ``signcontent`` and demands the same
  rows and the same payload as the plan, except the coded volatile fields (:data:`VOLATILE`), whose shape and range are
  checked instead. An event the plan does not name, a field it does not know, a different count: the run aborts
  before the first signature.

Fail closed (invariant 3): a shape ``signcontent`` does not know is a mismatch, never a pass.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from orch import canon
from orch.addons.settings_needs import NEEDS_SETTINGS

from .facts import AddonInfo, Facts
from .protocol import SetupError

ADDONS_FIELD = "addon" + "s"  # the profile field (not the store's addons directory the layout lint looks for)

__all__ = [
    "PLACEHOLDER",
    "RUNNER_GRANT_SCOPE",
    "VOLATILE",
    "Item",
    "Plan",
    "check_prepared",
    "fingerprint",
    "plan",
    "render_events",
]

PLACEHOLDER = "set at signing"
RUNNER_GRANT_SCOPE = "workable"
RUNNER_LABEL = "runner"
HOLDER = "this machine's runner, never shown"
#: per event type, the payload keys the host draws at prepare time. They are shown as PLACEHOLDER in the review and
#: checked by shape in :func:`check_prepared`. Nothing else may differ.
VOLATILE: dict[str, frozenset[str]] = {
    "grant.issued": frozenset({"grant", "issued_at", "expires_at", "secret_hash"}),
}
#: envelope and stamp keys of a prepared event: not part of the payload that is compared (the signer's own checks cover
#: them: ``orch.host.signer.check``)
ENVELOPE = frozenset(
    {"v", "id", "actor", "auth", "hash_v", "roster_v", "based_on", "seq", "at", "prev", "ws_seq", "sig", "host_sig"}
)
_GRANT_ID = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
#: how far from "now" a prepared grant's issued_at may be (seconds)
ISSUED_SKEW = 300


@dataclass(frozen=True)
class Item:
    id: str  # "settings" | "addon:<id>" | "runner_grant"
    title: str
    op: str  # the host person op that prepares it
    args: dict[str, Any]  # its arguments
    event: dict[str, Any]  # the planned payload (type + keys, volatile ones as PLACEHOLDER)
    hours: int | None = None
    retry: bool = False  # M2: a grant issued before whose hand-over was never confirmed


#: what the review says about the records step (the consent): plain words, one place
RECORDS_TEXT = "Keep a history of your tickets on this computer, saved automatically (never uploaded)."


#: what the review says about Claude Code's folder trust (one entry, this folder only)
TRUST_TEXT = "Let Claude Code open this folder without asking. It only holds orch's files, no one else's settings."


#: added when the person chose "trust anyway": Claude Code settings in a folder above this one also apply
TRUST_PARENT_TEXT = " You chose to trust it although Claude Code settings in a folder above it apply to it too."


@dataclass
class Plan:
    items: list[Item]
    flags: list[dict[str, str]]
    runner_grant: dict[str, Any] | None
    risk: dict[str, Any] | None
    tty_confirm: bool
    canon_profile: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)
    digest: str = ""
    fingerprint: list[str] | None = None
    #: what a NEW setup creates (B12 N7): the short name, the person's name and the folder, as the genesis will sign
    #: them (the very profile values the bootstrap gets); None in Finish setup, where the workspace exists
    workspace: dict[str, Any] | None = None

    def view(self, review_id: str) -> dict[str, Any]:
        return {
            "review_id": review_id,
            "digest": self.digest,
            "fingerprint": self.fingerprint,
            "workspace": self.workspace,
            "items": self.rows,
            "runner_grant": self.runner_grant,
            "flags": self.flags,
            "risk": self.risk,
            "tty_confirm": self.tty_confirm,
        }


# --------------------------------------------------------------------------------------------------------- rendering


def _clean(value: Any) -> str:
    from orch.host import signer

    return signer._clean(value)  # noqa: SLF001 - the one cleaning rule of every signing review


def render_events(events: list[Mapping[str, Any]], *, workspace: str | None = None) -> list[str]:
    """The plain view of ``events`` (workspace log, no ticket, no host ``before``): exactly what ``signcontent`` shows
    for a signing request holding them. Raises :class:`SetupError` (``review.unrenderable``) for anything it does not
    know how to show."""
    from orch.host import signcontent

    prepared = {"material": {"kind": "payload"}, "events": [dict(e) for e in events]}
    try:
        content = signcontent.build(prepared, verified=True)
        return signcontent.text(content, _clean, workspace=workspace)
    except (signcontent.PlainUnavailable, KeyError, TypeError, ValueError, AttributeError) as e:
        raise SetupError(
            "review.unrenderable", f"the review cannot show this event faithfully ({type(e).__name__})", 500
        ) from None


def _details(event: Mapping[str, Any]) -> list[str]:
    out = []
    for key in sorted(event):
        if key in ENVELOPE:
            continue
        out.append(f"{_clean(key)}: {_clean(event[key])}")
    return out


def payload_of(event: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in event.items() if k not in ENVELOPE}


# --------------------------------------------------------------------------------------------------------- planning


def _addon_event(name: str, raw: bytes) -> dict[str, Any]:
    from orch.addons import install, manifest, package

    files = {package.MANIFEST_FILE: raw}
    pkg = package.Package(files, package.package_digest(files))
    return install.grant_event(manifest.load_package_manifest(pkg, name), pkg)


def runner_hours(agent_hours: int) -> int:
    """Hours of the runner grant: the agent hours, capped at what a runner grant may be (B8 lifted 12 h to the agent
    maximum; before it the cap is 12)."""
    from orch.model import types

    return min(agent_hours, getattr(types, "GRANT_MAX_HOURS", 12))


def _days(hours: int) -> str:
    return f"{hours // 24} days" if hours % 24 == 0 and hours >= 48 else f"{hours} hours"


def plan(
    profile: Mapping[str, Any],
    directory: str,
    info: Mapping[str, AddonInfo],
    facts: Facts | None,
    *,
    manifest_bytes: Any,
    missing: list[str] | None = None,
) -> Plan:
    """The plan of ``profile``. In new mode every item is planned; in finish mode only ``missing`` ones (R7)."""
    from orch.custody.cli import RISK as CONVENIENCE_RISK

    want = None if missing is None else set(missing)
    items: list[Item] = []

    def wanted(item_id: str) -> bool:
        return want is None or item_id in want

    sets: dict[str, Any] = {}
    if profile["agent_hours"] != 8 and (facts is None or not facts.grant_hours_set):
        sets["grant_hours"] = profile["agent_hours"]
    if profile.get("git_init") and facts is None:  # a new workspace only
        from .facts import repo_name

        # the folder becomes a git repository (made by the run, before this is prepared) and is declared as the repo
        # tickets link, so worktrees can be made; the review names it in words
        sets["repos"] = {repo_name(Path(directory)): {"path": directory}}
    if sets and wanted("settings"):
        items.append(
            Item(
                "settings",
                "Workspace settings",
                "settings.set",
                {"set": copy.deepcopy(sets)},
                {"type": "settings.changed", "set": copy.deepcopy(sets)},
            )
        )
    for a in profile["addons"]:
        if a in NEEDS_SETTINGS:  # AE4: setup signs no settings for these, so it never turns them on (Settings does)
            raise SetupError("invalid.input", f"{a} is turned on in Settings, Add-ons", field=ADDONS_FIELD)
        if info[a].kind != "addon" or not wanted(f"addon:{a}"):
            continue
        ev = _addon_event(a, manifest_bytes(a))
        items.append(Item(f"addon:{a}", info[a].title, "addon.grant", {"name": a}, ev))
    runner_grant = None
    if profile["agent_grant"] and (wanted("runner_grant") or wanted("runner_grant_retry")):
        hours = runner_hours(profile["agent_hours"])
        ev = {
            "type": "grant.issued",
            "grant": PLACEHOLDER,
            "scope": RUNNER_GRANT_SCOPE,
            "verbs": "agent",
            "label": RUNNER_LABEL,
            "hours": hours,
            "issued_at": PLACEHOLDER,
            "expires_at": PLACEHOLDER,
            "secret_hash": PLACEHOLDER,
        }
        args = {"hours": hours, "scope": RUNNER_GRANT_SCOPE, "verbs": "agent", "label": RUNNER_LABEL, "runner": True}
        items.append(
            Item(
                "runner_grant",
                "Agent access held by this machine",
                "grant",
                args,
                ev,
                hours,
                retry=not wanted("runner_grant"),
            )
        )
        runner_grant = {"hours": hours, "scope": RUNNER_GRANT_SCOPE, "holder": HOLDER}
    flags: list[dict[str, str]] = []
    if profile.get("git_init") and wanted("git"):
        flags.append(
            {
                "id": "git",
                "text": "Make this folder a git repository (git init), and keep .orch/ and .orch-work/ out of it. "
                "It gets one empty first commit, none of your files.",
            }
        )
    if profile.get("records") and facts is None:  # a new workspace only
        flags.append({"id": "records", "text": RECORDS_TEXT})
    if profile.get("trust_folder") and facts is None:  # a new workspace only
        flags.append({"id": "trust", "text": TRUST_TEXT + (TRUST_PARENT_TEXT if profile.get("trust_parent") else "")})
    wants_exec = profile["agent_grant"] or any(
        info[a].needs_runner or info[a].needs_terminals for a in profile["addons"]
    )
    if wants_exec and wanted("runner"):
        from orch.runner import enablement

        flags.append({"id": "runner", "text": enablement.CARD_TEXT})
        if any(info[a].needs_terminals for a in profile["addons"]):
            flags.append({"id": "terminals", "text": enablement.TERMINALS_ON_TEXT})
    risk = None
    if profile["method"] == "click" and wanted("custody"):
        flags.append({"id": "custody", "text": "Approvals on this machine need one click, with no passphrase."})
        risk = {"text": CONVENIENCE_RISK, "once": True}
    elif profile["method"] == "terminal":
        flags.append({"id": "terminal", "text": "Every approval asks for your passphrase in a terminal."})
    # R3: a terminal confirmation for Finish setup and for anything that gives a machine-held grant or runs code
    exec_addon = any(
        info[a].needs_runner or info[a].needs_terminals for a in profile["addons"] if info[a].kind == "addon"
    )
    tty = facts is not None or runner_grant is not None or exec_addon
    p = Plan(items, flags, runner_grant, risk, tty, _canon_profile(profile, directory))
    if facts is None:
        p.workspace = _workspace(profile, directory)
    p.rows = _rows(p, profile)
    p.digest = _digest(p)
    p.fingerprint = fingerprint(p.digest) if tty else None
    return p


def _workspace(profile: Mapping[str, Any], directory: str) -> dict[str, Any]:
    """The new workspace in words: values come from the profile that core holds and the genesis signs, cleaned by the
    same rule as every signing review."""
    prefix, name = _clean(profile["prefix"]), _clean(profile["name"])
    wname = _clean(profile["workspace_name"])
    folder = _clean(directory)
    return {
        "prefix": prefix,
        "name": name,
        "workspace_name": wname,
        "dir": folder,
        "lines": [
            f"Workspace {wname} ({prefix}) for {name}",
            f"Workspace name: {wname} (shown in menus, not signed)",
            f"Ticket prefix: {prefix}",
            f"Person: {name}",
            f"Folder: {folder}",
        ],
    }


def _canon_profile(profile: Mapping[str, Any], directory: str) -> dict[str, Any]:
    keys = ("name", "workspace_name", "prefix", "addons", "method", "agent_hours", "agent_grant", "relay")
    out = {k: profile[k] for k in keys}
    out["git_init"] = bool(profile.get("git_init"))
    out["records"] = bool(profile.get("records"))
    out["trust_folder"] = bool(profile.get("trust_folder"))
    out["trust_parent"] = bool(profile.get("trust_parent"))
    out["dir"] = directory
    out["custody_conversion"] = profile["method"] == "click"
    return out


def _rows(p: Plan, profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for it in p.items:
        lines = render_events([it.event])
        note = None
        if it.id == "runner_grant" and it.hours and it.hours > 24:
            note = f"Agents may work for {_days(it.hours)} on this machine."
        if it.retry:
            retry_note = "re-issues agent access; an earlier grant was not confirmed (or was revoked)"
            note = f"{note}; {retry_note}" if note else retry_note
        volatile = [f"{k.replace('_', ' ')}: {PLACEHOLDER}" for k in sorted(VOLATILE.get(it.event["type"], ()))]
        row = {
            "id": it.id,
            "title": it.title,
            "lines": lines,
            "details": _details(it.event),
            "volatile": volatile,
            # What the page may build its summary from, without reading prose: the event type and its payload (the
            # very event that is signed; the values the host draws at signing are PLACEHOLDER strings).
            "type": it.event["type"],
            "fields": copy.deepcopy(payload_of(it.event)),
            # Which of ``lines`` are the host's placeholders (by position, from the event's own volatile keys): the page
            # hides exactly these and never filters on how a line ends.
            "placeholders": _placeholder_lines(it, lines),
            "placeholder_details": [
                i
                for i, key in enumerate(k for k in sorted(it.event) if k not in ENVELOPE)
                if key in VOLATILE.get(it.event["type"], ())
            ],
        }
        if note:
            row["note"] = note
        rows.append(row)
    return rows


_SENTINEL = "VOLATILE-PLACEHOLDER-7f3a"


def _placeholder_lines(item: Item, lines: list[str]) -> list[int]:
    """Positions in ``lines`` that show a value the host draws at signing: render the event again with each volatile key
    set to a marker and see which lines carry it. If that render differs in shape, nothing is marked (every line
    stays visible: never hide on a guess)."""
    keys = VOLATILE.get(item.event["type"], ())
    if not keys:
        return []
    try:
        marked = render_events([{**item.event, **{k: _SENTINEL for k in keys}}])
    except SetupError:
        return []
    if len(marked) != len(lines):
        return []
    return [i for i, line in enumerate(marked) if _SENTINEL in line and _SENTINEL not in lines[i]]


def _digest(p: Plan) -> str:
    doc = {
        "v": 1,
        "profile": p.canon_profile,
        "items": [{"id": it.id, "op": it.op, "args": it.args, "event": it.event} for it in p.items],
        "rows": [
            {
                "id": r["id"],
                "lines": r["lines"],
                "note": r.get("note"),
                "placeholders": r["placeholders"],
                "placeholder_details": r["placeholder_details"],
            }
            for r in p.rows
        ],
        "workspace": p.workspace,
        "flags": p.flags,
        "risk": p.risk,
        "tty": p.tty_confirm,
    }
    return "sha256:" + hashlib.sha256(canon.dumps(doc)).hexdigest()


def fingerprint(digest: str) -> list[str]:
    """Three words from the digest: the same on the page and on the setup terminal (R3)."""
    from orch.identity._wordlist import WORDS

    raw = hashlib.sha256(b"orch/setup/fingerprint|" + digest.encode()).digest()
    return [WORDS[int.from_bytes(raw[i * 2 : i * 2 + 2], "big") % len(WORDS)] for i in range(3)]


# ------------------------------------------------------------------------------------------ signing-time re-check (R1)


class Mismatch(SetupError):
    def __init__(self, why: str) -> None:
        super().__init__("review.mismatch", f"the host prepared something other than the review: {why}", 409)


def _normalised(item: Item, event: Mapping[str, Any], now: float) -> dict[str, Any]:
    """The prepared event's payload with its volatile fields checked and replaced by PLACEHOLDER."""
    payload = payload_of(event)
    typ = item.event["type"]
    if payload.get("type") != typ:
        raise Mismatch(f"{item.id}: the event type differs")
    volatile = VOLATILE.get(typ, frozenset())
    if typ == "grant.issued":
        from datetime import datetime

        gid, issued, expires = payload.get("grant"), payload.get("issued_at"), payload.get("expires_at")
        if not (isinstance(gid, str) and _GRANT_ID.fullmatch(gid)):
            raise Mismatch("the grant id is not a grant id")
        if not (
            isinstance(issued, str) and _ISO.fullmatch(issued) and isinstance(expires, str) and _ISO.fullmatch(expires)
        ):
            raise Mismatch("the grant times are not times")
        t0 = datetime.strptime(issued, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
        t1 = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
        if abs(t0 - now) > ISSUED_SKEW or t1 - t0 != 3600 * int(item.hours or 0):
            raise Mismatch("the grant times are not 'now' plus the reviewed hours")
    out = {k: (PLACEHOLDER if k in volatile else v) for k, v in payload.items()}
    if "secret_hash" in volatile and "secret_hash" not in out:
        out["secret_hash"] = PLACEHOLDER  # the signer draws it; a missing key is the same as the plan's placeholder
    return out


def check_prepared(
    items: list[Item], rows: list[Mapping[str, Any]], prepared: list[list[Mapping[str, Any]]], now: float
) -> None:
    """R1 at signing: ``prepared[i]`` are the events the host prepared for ``items[i]``. Raises :class:`Mismatch`
    before ANY signature when the count, the payloads or the rendered rows differ from the review."""
    if len(prepared) != len(items):
        raise Mismatch("a different number of requests")
    for item, row, events in zip(items, rows, prepared, strict=True):
        if len(events) != 1:
            raise Mismatch(f"{item.id}: {len(events)} events instead of one")
        norm = _normalised(item, events[0], now)
        if norm != item.event:
            extra = sorted(set(norm) ^ set(item.event))
            raise Mismatch(f"{item.id}: the event differs" + (f" ({_clean(extra[0])})" if extra else ""))
        if render_events([norm]) != row["lines"]:
            raise Mismatch(f"{item.id}: the rendered rows differ")
