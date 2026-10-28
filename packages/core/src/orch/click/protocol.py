"""The click signer's channel contract (orch-v2-host §0.8.6 item 5): constants, op classes, errors. No custody here."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

# a stdlib-only leaf (no custody, no I/O): the dashboard side may import it (§0.8.6 "Packaging and versions")
from orch.host.errors import DETAIL_KEYS as _HOST_DETAIL
from orch.host.errors import DEVICE_ID, DEVICES_MAX
from orch.model.types import GRANT_MAX_HOURS

__all__ = [
    "CLICK_OPS",
    "HOLD_OPS",
    "DETAIL_FIELDS",
    "DETAIL_REASONS",
    "DEPOSIT_LINE",
    "DEPOSIT_REASONS",
    "DEPOSIT_MAX_HOURS",
    "DEPOSIT_RISK",
    "OPS",
    "REVEAL_LINE",
    "ROTATE_LINE",
    "ROTATE_OP",
    "ROTATE_PURPOSES",
    "RUNNER_LINE",
    "RUNNER_OFF_LINE",
    "SEALED_OPS",
    "BACKUP_KEY_LINE",
    "REVEAL_RISK",
    "REASONS",
    "REFUSALS",
    "CONTRACT",
    "CONTRACT_MISMATCH",
    "FEATURES",
    "OP_TITLES",
    "ONE_PRESS_OPS",
    "RISK_BUILDERS",
    "headline",
    "headline_detail",
    "risk_lines",
    "ClickError",
    "RISK",
    "click_detail",
    "op_class",
]

#: 4 (run 13 C3): the View carries ``headline`` (signer words: the op's title and the ticket key, no free text),
#: ``headline_detail`` (a ticket's title, capped and quoted, or null) and ``risk_lines`` (the lines the signer marks as
#: risk: ``[]`` only for :data:`ONE_PRESS_OPS`, never empty for any other op), all in ``view_digest``; a client may
#: announce the contract it speaks (``contract`` in any request's args) and another one is refused with
#: :data:`CONTRACT_MISMATCH`.
#: 3 (#1198 C1, C2): no typed-word class: every op that was a word op is a click, Publish (``settings.set`` publish,
#: ``addon.grant`` of publish, ``publish.share``) included; ``sign`` reads no ``word`` and the ``word`` refusal is
#: gone; a client reads ``status.contract`` and sends nothing typed.
#: 2 (H11c-2): deposit support (``deposit`` on ``prepare``, ``deposit``/``deposit_reason`` on ``sign``); a contract-1
#: signer answers ``terminal_only`` for every ``grant``, so a client reads ``status.contract`` to tell the two apart
CONTRACT = 4
#: the refusal of a request whose ``args.contract`` is not :data:`CONTRACT` (exactly that int: ``True``, ``4.0`` and
#: ``"4"`` are other contracts); a client that announces nothing is not refused (it reads ``status.contract``)
CONTRACT_MISMATCH = "contract.mismatch"
#: the closed set of ``sign``'s ``deposit_reason`` (static codes, never host text; anything else is ``other``)
DEPOSIT_REASONS = frozenset(
    {"grant.forgotten", "grant.not_runner", "grant.verbs", "grant.expiring", "rate.limited", "runner.unavailable",
     "other"}
)  # fmt: skip
RISK = "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign."
#: v1.6 (D-P2-27, §0.8.6 item 6): the View of a deposit-only grant carries this line (and in ``lines``)
DEPOSIT_LINE = "The secret goes to your orch runner on this machine and is never shown"
#: the owner's plain-language risk of a click-issued runner grant (D-P2-27), the tier-A sentence of its View
DEPOSIT_RISK = (
    "Anything that takes over the dashboard can give agents standing authority to act for you on tickets for as long "
    "as the grant lasts (up to 7 days, the length you choose); you can stop it at any time."
)
#: #1198 C7: the View of a grant whose secret is shown once in the page (sealed to the page's key) carries this line
#: (and in ``lines``) and ``seal.purposes == ["grant.secret"]``
REVEAL_LINE = "The secret is shown once, on this page, only to you. orch keeps only a fingerprint of it"
#: the plain risk of a shown secret (#1198 C7): the page's key is chosen by the page, so whoever controls the dashboard
#: could ask for the secret under its own key; the review says so
REVEAL_RISK = "The secret will be shown once in this browser. Anyone who controls this dashboard could read it."
#: #1198 C10: the hub steps are clicks only together with the person's backup key, typed once in the page and sealed to
#: the signer's per-request key (sealed-field contract, purpose ``hub.backup_key``). op -> purposes its View asks for
SEALED_OPS: dict[str, tuple[str, ...]] = {
    "hub.join": ("hub.backup_key",),
    "hub.mode": ("hub.backup_key",),
    "hub.leave": ("hub.backup_key",),
}
#: #1198 C13: ``device.rotate`` (a new key for this computer) is a click only through the signer's own ``rotate`` op,
#: never through ``prepare``: the NEW key signs it, so the signer makes that key itself. It takes the recovery code
#: (to sign the new key's certificate) and the passphrase of the new key file, both typed once in the page and sealed
#: to the signer's per-offer key. ``device.rotate`` stays terminal-only in :func:`op_class` (and so in ``prepare``).
ROTATE_OP = "device.rotate"
ROTATE_PURPOSES: tuple[str, ...] = ("device.recovery_code", "custody.passphrase")
ROTATE_LINE = (
    "This step also needs your recovery code and a passphrase for the new key. "
    "You type them once here; orch does not keep them"
)
#: the View of a hub step ends with this line (and carries ``backup_key_line``); the page asks for the key
BACKUP_KEY_LINE = "This step also needs your backup key. You type it once here; orch does not keep it"
#: a deposit grant lasts at most this many hours (D-P2-27)
DEPOSIT_MAX_HOURS = GRANT_MAX_HOURS
#: #1198 C14: dashboard execution (the agent runner) on or off by click. The switch is the host-local file of
#: ``orch runner enable|disable``, not a signed event, so it goes through its own signer op (like the custody switches),
#: after a review the signer builds: the page shows these words and the signer only acts on the digest of that review.
RUNNER_LINE = "Agents may run commands on this computer as you when you start them."
RUNNER_OFF_LINE = (
    "Open terminals close within a second; agents already running keep running; the dashboard starts no new ones."
)
OPS = (
    "prepare",
    "view",
    "sign",
    "cancel",
    "cancel_holds",
    "status",
    "custody_on",
    "custody_off",
    "stash",
    "rotate",
    "runner",
    "github_token",
    "stepup",
)
#: what this signer can do beyond its contract, in ``status.features``: ``custody`` is ``custody_on``/``custody_off``,
#: the dashboard's click-to-sign switch (§0.8.6 item 3a); ``sequence`` is ``prepare {sequence}``: one review, N
#: signatures (batch 11); an older signer has no ``features`` and no switch, and ignores ``sequence`` (never asked)
#: ``images`` is ``stash`` (a pasted screenshot, in chunks, held by the signer) and ``prepare {sequence}`` naming it
#: (R3 part B); ``reveal`` (#1198 C7) is a ``grant`` without ``deposit``: ``sign`` takes ``reveal_key`` and answers
#: ``reveal`` (the secret sealed to the page's key); a signer without it answers ``terminal_only`` for that grant.
#: ``backup_key`` (#1198 C10) is the hub steps by click: the View carries ``seal`` and ``sign`` takes ``sealed``
#: ``sealed_passphrase`` (#1198 C12) is ``custody_off {begin}`` then ``custody_off {request_id, view_digest, sealed}``:
#: the new passphrase of "One click off" arrives sealed; a clear ``passphrase`` is refused
#: ``device_rotate`` (#1198 C13) is ``rotate {begin}`` (the review: old and new key, and the signer's key for the two
#: sealed secrets), then ``rotate {request_id, view_digest, sealed}``: a new device key for this computer
#: ``runner_switch`` (#1198 C14) is ``runner {review}`` then ``runner {on, view_digest}``: dashboard execution on or off
#: ``github_token`` (#1198 C16) is ``github_token {begin}`` (the signer's one-use key for the one purpose
#: ``github.token``), then ``github_token {request_id, view_digest, sealed}``: the GitHub token typed in the page is
#: opened here and stored as the add-on's key, in the person's custody; ``github_token {remove}`` deletes it
#: ``passkey_stepup`` (#1198 R1-6) is ``stepup {workspace_id, view_digest}`` (a one-use nonce for a passkey
#: assertion) and
#: the ``remote``/``stepup`` arguments of the listed ops (custody_on, custody_off {sealed}, rotate {sealed}, runner on,
#: sign with a sealed value or a reveal key): for a remote dashboard session the signer verifies the assertion itself
FEATURES = (
    "custody",
    "sequence",
    "images",
    "reveal",
    "backup_key",
    "sealed_passphrase",
    "device_rotate",
    "runner_switch",
    "github_token",
    "passkey_stepup",
)
#: the host's ``sign.convenience_unavailable`` reasons (orch-v2-host §0.8.6 item 1), from the host's closed schema
REASONS: tuple[str, ...] = tuple(_HOST_DETAIL["sign.convenience_unavailable"]["reason"])  # type: ignore[arg-type]
#: ``human.click_refused`` reasons (§0.8.6 item 5)
REFUSALS = (
    "terminal_only",
    "not_prepared_here",
    "expired",
    "view_changed",
    "too_soon",
    "custody_changed",
    "busy",
    "check_failed",
    "uncertain",
    "already_signed",
    "record_unavailable",
    "stopped",
    "resumable",
)
#: #517: the only codes whose host ``Error.detail`` the signer forwards, each with its closed ``reason`` set (§0.3).
#: Taken from the host's own closed schema (:data:`orch.host.errors.DETAIL_KEYS`): one source of truth
DETAIL_REASONS: dict[str, frozenset[str]] = {
    code: frozenset(_HOST_DETAIL[code]["reason"])  # type: ignore[arg-type]
    for code in ("sign.convenience_unavailable", "sign.convenience_readers")
}
#: the exact key set the host sends for both codes (``devices`` may be empty): anything else is not forwarded
DETAIL_FIELDS = frozenset({"reason", "devices"})
FRAME_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
WS_ID = re.compile(r"[0-9a-f]{32}")
REQ = re.compile(r"hr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
IDEM = re.compile(r"a_[0-7][0-9A-HJKMNP-TV-Z]{25}")
HASH = re.compile(r"sha256:[0-9a-f]{64}")
OP_NAME = re.compile(r"[a-z][a-z_]*(\.[a-z_]+)*")

#: §0.8.6 item 6: daily ops, confirmed by a click alone
CLICK_OPS = frozenset(
    {
        "approve",
        "request_changes",
        "verdict",
        "answer",
        "close",
        "reopen",
        "new",
        "set",
        "section.set",
        "ac.add",
        "ac.edit",
        "task.add",
        "task.edit",
        "task.block",
        "task.done",
        "task.reopen",
        "task.skip",
        "task.start",
        "ac.accept",  # R3: vouching for a criterion is as strong as an approval and shown as such ("one click")
        "ac.unaccept",
        "task.mark_done",
        "log",
        "ask",
        "claim",
        "release",
        "handoff",
        "submit",
        "move",
        "artifact.add",
        "artifact.replace",
        "apply",
        "grant.revoke",
        "grant",  # #1198 C7: with the secret sealed to the page (``reveal_key``), or deposited (``deposit``)
        "addon.disable",
        "settings.set",
        "hub.join",  # #1198 C10: only with the backup key sealed to the signer (SEALED_OPS); see view.check_prepared
        "hub.mode",
        "hub.leave",
        "device.remove",  # #1198 C15: refused for the signing device and an owner's last device (device.lockout)
        "addon.grant",  # every add-on, publish included (#1198 C2); the review lists what is signed
        "publish.share",  # #1198 C2: the staged copy's own words are the review; the person clicks
        # #1198 C1: these were the typed-word class; the person reads the review and clicks, nothing is typed
        "member.add",
        "member.role",
        "member.remove",
        "addon.purge",
        "policy.set",
        "people.set",  # B12D: who owns, works on or reviews a ticket decides who may act on it
        "schedule.add",  # emits schedule.set
        "schedule.cancel",  # emits schedule.cancelled
        "follow_up.reschedule",
        "follow_up.undo",
        "follow_up.cancel",  # run 12 (6.4): the Cancel button of a follow-up is one click, like the other daily ops
    }
)
#: D5: only log-only operations may wait unsigned. `set` is further restricted to labels/priority/size.
HOLD_OPS = frozenset({"log", "set", "move", "section.set", "follow_up.reschedule"})

#: every op not in :data:`CLICK_OPS` is terminal-only (fail closed for ops added later)

#: contract 4: the signer-owned words of the View's ``headline`` (see :func:`headline`): one short line per op
OP_TITLES: dict[str, str] = {
    "approve": "Approve a gate",
    "request_changes": "Request changes",
    "verdict": "Give a verdict",
    "answer": "Answer a question",
    "close": "Close a ticket",
    "reopen": "Reopen a ticket",
    "new": "Create a ticket",
    "set": "Change a ticket",
    "section.set": "Change a ticket section",
    "ac.add": "Add an acceptance criterion",
    "ac.edit": "Edit an acceptance criterion",
    "task.add": "Add a task",
    "task.edit": "Edit a task",
    "task.block": "Block a task",
    "task.done": "Mark a task done",
    "task.reopen": "Reopen a task",
    "task.skip": "Skip a task",
    "task.start": "Start a task",
    "ac.accept": "Accept a criterion",
    "ac.unaccept": "Withdraw the acceptance of a criterion",
    "task.mark_done": "Mark a task done",
    "log": "Add a note",
    "ask": "Ask a question",
    "claim": "Claim a ticket",
    "release": "Release a ticket",
    "handoff": "Hand off a ticket",
    "submit": "Submit for review",
    "move": "Move a ticket",
    "artifact.add": "Add a file",
    "artifact.replace": "Replace a file",
    "apply": "Apply a change",
    "grant.revoke": "Stop agent access",
    "grant": "Give agents access",
    "addon.disable": "Turn off an add-on",
    "settings.set": "Change the workspace settings",
    "hub.join": "Join a hub",
    "hub.mode": "Change the hub mode",
    "hub.leave": "Leave a hub",
    "device.remove": "Remove a device",
    "addon.grant": "Turn on an add-on",
    "publish.share": "Share files outside this computer",
    "member.add": "Add a member",
    "member.role": "Change a member's role",
    "member.remove": "Remove a member",
    "addon.purge": "Purge an add-on",
    "policy.set": "Change the approval rules",
    "people.set": "Change who works on a ticket",
    "schedule.add": "Schedule an agent run",
    "schedule.cancel": "Cancel a schedule",
    "follow_up.reschedule": "Move a follow-up",
    "follow_up.undo": "Restore a follow-up",
    "follow_up.cancel": "Cancel a follow-up",
    ROTATE_OP: "Replace this computer's key",
}


#: a ticket key as the signer checks it (``orch.host.signer._formats``): letters, digits and one dash, never free text
TICKET_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-(?:(?!0000)[0-9]{4}|[1-9][0-9]{4,})")
#: the most characters of free text (a ticket title) ``headline_detail`` shows, before the closing ``…``
DETAIL_CHARS = 60


def headline(op: str, key: Any = None) -> str:
    """The View's ``headline``: signer-owned words only, the op's title from :data:`OP_TITLES` and, when the request
    names a ticket, its key (``Close a ticket: DEMO-0043``). Never any free text: a title a person or a host chose goes
    to :func:`headline_detail`. A key that is not a well-formed ticket key is left out."""
    title = OP_TITLES.get(op, "Sign this change")
    if isinstance(key, str) and TICKET_KEY.fullmatch(key):
        return f"{title}: {key}"
    return title


def headline_detail(text: Any) -> str | None:
    """The free text beside the headline (a ticket's title), visually secondary: one line, at most
    :data:`DETAIL_CHARS` characters (cut with ``…``), always in quotes (``“…”``); ``None`` when there is none."""
    if not isinstance(text, str):
        return None
    flat = " ".join(
        "".join(c if c.isprintable() else " " for c in text if c not in '"“”‘’').split()
    )  # no control or line break
    if not flat:
        return None
    if len(flat) > DETAIL_CHARS:
        flat = flat[:DETAIL_CHARS].rstrip() + "…"
    return f"“{flat}”"


# -- contract 4: the risk lines (run 13 C3) ----------------------------------------------------------------------------
# The signer's own words only. A value of a signed event reaches a risk line only through a closed table (a capability's
# words, a role) or as a number (a grant's hours); never as text a client or a host chose.
GRANT_LENGTH_LINE = "Agents get this authority for {span}, until it ends or you stop it."
CAPABILITY_LINE = "It may {words}."
CAPABILITY_UNKNOWN = "It declares a capability orch has no words for; read the details before you turn it on."
PUBLISH_ADDON_RISK = "Turning on Publish lets files you share leave this computer, for anyone with the link to read."
PUBLISH_SETTINGS_RISK = "Files you share will be sent to this Publish server; whoever runs it can read them."
PUBLISH_SHARE_RISK = (
    "These files leave this computer: anyone with the link can read them, and a copy cannot be taken back."
)
HUB_SELECTIVE_RISK = "The hub receives what you choose to share; leaving later does not take back what it received."
HUB_FULL_RISK = (
    "The hub will be able to read everything in this workspace, restricted tickets too; leaving later cannot take back "
    "what it read."
)
HUB_LEAVE_RISK = (
    "Leaving does not take back what the hub already read; phones and publishing that go through the hub stop "
    "reaching this workspace."
)
HUB_RISK = "This changes what a hub can read of this workspace."
DEVICE_REMOVE_RISK = (
    "That device can no longer sign for this workspace; to use it again, it must be paired again as a new device."
)
KEY_REPLACE_RISK = "The old key stops signing everywhere at once; this cannot be undone."
PURGE_RISK = "Everything this add-on added to tickets is removed from what orch shows (the signed history stays)."
SCHEDULE_FIRING_LINE = "No click or confirmation is required at firing time."
#: a role that gives more authority than a member has; the signer cannot read the role it replaces from signed data,
#: so these targets are always marked
ROLE_RISKS: dict[str, str] = {
    "owner": "An owner can do everything in this workspace, change its members and their roles included.",
    "maintainer": "A maintainer gets more authority over everyone's work in this workspace.",
}
ROLE_CHANGE_RISK = "This changes who can do what in this workspace."
MEMBER_ADD_RISK = "Adds a person to this workspace."
MEMBER_REMOVE_RISK = "Removes a person from this workspace: their devices can no longer sign for it."
POLICY_RISK = "Changes who may approve and what is required before an approval counts."
PEOPLE_RISK = "Changes who works on or reviews this ticket, and so who may act on it."
TICKET_OWNER_RISK = "A ticket owner decides who works on and reviews this ticket and may change its approval rules."
#: the fail-closed line of an op outside :data:`ONE_PRESS_OPS` whose builder has nothing to say (or that has none)
NOT_DAILY_RISK = "This change is not a daily action: read it before you sign."

Events = list[Mapping[str, Any]]


def _hours_line(events: Events) -> list[str]:
    from orch.host.signcontent import span

    hours = events[0].get("hours") if events else None
    if type(hours) is not int or hours < 1:
        return []
    return [GRANT_LENGTH_LINE.format(span=span(hours))]


def _grant_risk(events: Events, deposit: bool, reveal: bool) -> list[str]:
    # deposit (the runner grant, "agent access" handed to the runner) or reveal (the secret shown once in the page);
    # any other grant never reaches a View (terminal_only), and is still marked here
    return [DEPOSIT_RISK if deposit else REVEAL_RISK, *_hours_line(events)]


def _addon_grant_risk(events: Events, deposit: bool, reveal: bool) -> list[str]:
    from orch.host.signcontent import _CAPABILITY_WORDS

    out: list[str] = []
    for ev in events:
        if ev.get("type") != "addon.granted":
            continue
        if ev.get("name") == "publish":
            out.append(PUBLISH_ADDON_RISK)
        caps = ev.get("capabilities")
        for cap in caps if isinstance(caps, list) else ([] if caps is None else [caps]):
            words = _CAPABILITY_WORDS.get(cap) if isinstance(cap, str) else None
            out.append(CAPABILITY_LINE.format(words=words) if words is not None else CAPABILITY_UNKNOWN)
    return list(dict.fromkeys(out))


def _roles(events: Events, typ: str) -> list[str]:
    roles = [ev.get("role") for ev in events if ev.get("type") == typ]
    return list(dict.fromkeys(ROLE_RISKS[r] for r in roles if isinstance(r, str) and r in ROLE_RISKS))


def _role_risk(events: Events, deposit: bool, reveal: bool) -> list[str]:
    return _roles(events, "role.changed") or [ROLE_CHANGE_RISK]


def _member_add_risk(events: Events, deposit: bool, reveal: bool) -> list[str]:
    return [MEMBER_ADD_RISK, *_roles(events, "member.added")]


def _people_risk(events: Events, deposit: bool, reveal: bool) -> list[str]:
    owner = any(ev.get("type") == "people.changed" and ev.get("role") == "owner" for ev in events)
    return [TICKET_OWNER_RISK, PEOPLE_RISK] if owner else [PEOPLE_RISK]


def _settings_risk(events: Events, deposit: bool, reveal: bool) -> list[str]:
    for ev in events:
        changed = ev.get("set")
        if ev.get("type") == "settings.changed" and isinstance(changed, Mapping):
            if any(str(k).split(".")[0] == "publish" and v is not None for k, v in changed.items()):
                return [PUBLISH_SETTINGS_RISK]
    return []


def _hub_risk(events: Events, deposit: bool, reveal: bool) -> list[str]:
    out: list[str] = []
    for ev in events:
        if ev.get("type") == "hub.joined":
            out.append(HUB_FULL_RISK if ev.get("mode") == "full" else HUB_SELECTIVE_RISK)
        elif ev.get("type") == "hub.left":
            out.append(HUB_LEAVE_RISK)
    return list(dict.fromkeys(out)) or [HUB_RISK]  # a hub step is always marked


def _schedule_risk(events: Events, deposit: bool, reveal: bool) -> list[str]:
    from orch.ops.schedule import RISK as SCHEDULE_RISK

    return [SCHEDULE_RISK, SCHEDULE_FIRING_LINE]


def _fixed(*lines: str) -> Callable[[Events, bool, bool], list[str]]:
    return lambda events, deposit, reveal: list(lines)


#: the closed allowlist of daily, low-risk click ops (the stop, undo and everyday ones): their View has no risk lines
#: and one press signs. EVERY other op (a click op not listed here, :data:`ROTATE_OP`, any op added later) is risky by
#: default: its ``risk_lines`` are never empty, so the page asks for the second press.
ONE_PRESS_OPS = frozenset(
    {
        "approve",
        "request_changes",
        "verdict",
        "answer",
        "close",
        "reopen",
        "new",
        "set",
        "section.set",
        "ac.add",
        "ac.edit",
        "ac.accept",
        "ac.unaccept",
        "task.add",
        "task.edit",
        "task.block",
        "task.done",
        "task.reopen",
        "task.skip",
        "task.start",
        "task.mark_done",
        "log",
        "ask",
        "claim",
        "release",
        "handoff",
        "submit",
        "move",
        "artifact.add",
        "artifact.replace",
        "apply",
        "follow_up.reschedule",
        "follow_up.undo",
        "follow_up.cancel",
        "schedule.cancel",
        "grant.revoke",
        "addon.disable",
    }
)

#: op -> builder ``(events, deposit, reveal) -> lines``: the signer's specific risk words of an op outside
#: :data:`ONE_PRESS_OPS`. An op without a builder, or whose builder finds nothing, still gets :data:`NOT_DAILY_RISK`.
RISK_BUILDERS: dict[str, Callable[[Events, bool, bool], list[str]]] = {
    "grant": _grant_risk,
    "addon.grant": _addon_grant_risk,
    "member.add": _member_add_risk,
    "member.role": _role_risk,
    "member.remove": _fixed(MEMBER_REMOVE_RISK),
    "policy.set": _fixed(POLICY_RISK),
    "people.set": _people_risk,
    "settings.set": _settings_risk,
    "publish.share": _fixed(PUBLISH_SHARE_RISK),
    "hub.join": _hub_risk,
    "hub.mode": _hub_risk,
    "hub.leave": _hub_risk,
    "device.remove": _fixed(DEVICE_REMOVE_RISK),
    ROTATE_OP: _fixed(KEY_REPLACE_RISK),
    "addon.purge": _fixed(PURGE_RISK),
    "schedule.add": _schedule_risk,
}


def risk_lines(op: str, events: Events | None = None, *, deposit: bool = False, reveal: bool = False) -> list[str]:
    """The View's ``risk_lines`` for ``op`` over its signed ``events``: ``[]`` only for :data:`ONE_PRESS_OPS`; for any
    other op its builder's lines (:data:`RISK_BUILDERS`), or :data:`NOT_DAILY_RISK` when there is no builder or it
    finds nothing (fail closed: never empty)."""
    if op in ONE_PRESS_OPS:
        return []
    build = RISK_BUILDERS.get(op)
    good = [e for e in events or [] if isinstance(e, Mapping)]
    lines = [str(line) for line in build(good, deposit, reveal)] if build is not None else []
    return lines or [NOT_DAILY_RISK]


#: ``settings.set`` keys that need more than a click: none (#1198 C2). ``publish`` (where shared files go) is a click
#: word in C1. (``addons``, the values an add-on reads, is a click: owner V3-D38, 24 Oct.) ``icon`` (cosmetic) is a
#: click as well.
SETTINGS_TERMINAL_KEYS: frozenset[str] = frozenset()


def _settings_class(changes: list[Any]) -> str:
    for changed in changes:
        if not isinstance(changed, dict):
            return "terminal"  # not a plain settings map: fail closed
        if any(str(k).split(".")[0] in SETTINGS_TERMINAL_KEYS for k in changed):
            return "terminal"
    return "click"


def op_class(op: str, events: list[dict[str, Any]] | None = None, args: dict[str, Any] | None = None) -> str:
    """``click`` or ``terminal``. ``settings.set`` is classed by the keys it changes (see the key sets above),
    from the signed events when there are any (**the class at sign time, which wins**), else from the request's
    ``args["set"]`` (the early check, before prepare); with neither it counts as a click op. A class the early check
    gives can only be raised by the sign-time check, never lowered."""
    if op == "settings.set" and (events is not None or args is not None):
        if events is not None:
            return _settings_class([ev.get("set") for ev in events])
        return _settings_class([(args or {}).get("set")])
    if op in CLICK_OPS:
        return "click"
    return "terminal"


def click_detail(code: str, detail: Any) -> dict[str, Any] | None:
    """The ``detail`` the signer forwards for ``code`` (#517), re-validated whole: only the two convenience codes, and
    exactly the shape the host sends for them, ``{reason, devices}``: ``reason`` from that code's closed set,
    ``devices`` a list of at most 64 device ids (maybe empty). Anything else, or any invalid part, gives ``None``:
    never a partial detail."""
    reasons = DETAIL_REASONS.get(code)
    if reasons is None or type(detail) is not dict or set(detail) != DETAIL_FIELDS:
        return None
    reason, devices = detail["reason"], detail["devices"]
    if type(reason) is not str or reason not in reasons:
        return None
    if type(devices) is not list or len(devices) > DEVICES_MAX:
        return None
    if not all(type(d) is str and DEVICE_ID.fullmatch(d) for d in devices):
        return None
    return {"reason": reason, "devices": list(devices)}


class ClickError(Exception):
    """One refused channel request: ``code`` (catalog), ``reason`` (for ``human.click_refused`` and
    ``sign.convenience_unavailable``), display ``message``, ``retry_after_ms`` for ``rate.limited``, ``detail`` only
    as :func:`click_detail` lets it through (#517)."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        reason: str | None = None,
        retry_after_ms: int | None = None,
        retryable: bool = False,
        detail: Any = None,
    ) -> None:
        self.code, self.message, self.reason = code, (message or code)[:400], reason
        self.retry_after_ms, self.retryable = retry_after_ms, retryable
        self.detail = click_detail(code, detail)
        super().__init__(f"{code}{'/' + reason if reason else ''}: {self.message}")

    def error(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.reason is not None:
            out["reason"] = self.reason
        if self.retry_after_ms is not None:
            out["retry_after_ms"] = int(self.retry_after_ms)
        if self.detail is not None:
            out["detail"] = self.detail
        return out

    @classmethod
    def of(cls, error: dict[str, Any]) -> ClickError:
        return cls(
            str(error.get("code") or "internal"),
            str(error.get("message") or ""),
            reason=error.get("reason"),
            retry_after_ms=error.get("retry_after_ms"),
            retryable=bool(error.get("retryable", False)),
            detail=error.get("detail"),
        )


def refused(reason: str, message: str) -> ClickError:
    assert reason in REFUSALS, reason
    return ClickError("human.click_refused", message, reason=reason)
