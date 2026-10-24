"""The error catalog: every error code the CLI can return, with its exit code and default hint and fix.

The ``code`` strings are the stable contract (format doc 10.4 item 4). An operation lists the codes it can return
in its declaration (``errors``, each with hint and fix); the catalog here is where each code is defined once:
exit code (10.4 item 5), whether a retry can help, a default message, and a default hint and fix. In ``hint`` and
``fix`` the placeholder ``{cmd}`` stands for the operation's registry name (``task.done``).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ERRORS",
    "EXIT_CODES",
    "GLOBAL_ERRORS",
    "ErrorSpec",
    "OrchError",
    "spec",
]

# Format doc 10.4 item 5.
EXIT_CODES: dict[int, str] = {
    0: "ok",
    1: "internal error",
    2: "usage or not found",
    3: "not allowed (transition or human-only)",
    4: "claim, lease or lock",
    5: "validation",
    6: "parse",
    7: "wait timeout with --strict-timeout",
    8: "base_rev conflict",
    9: "retryable",
}


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    exit: int
    retryable: bool
    message: str
    hint: str
    fix: tuple[str, ...]
    doc: str


def _e(code: str, exit_: int, retryable: bool, message: str, hint: str, fix: list[str], doc: str) -> ErrorSpec:
    return ErrorSpec(code, exit_, retryable, message, hint, tuple(fix), doc)


_SPECS = [
    # 1 internal
    _e(
        "internal",
        1,
        False,
        "internal error",
        "report this to the user",
        ["orch", "doctor"],
        "A bug or an undeclared error.",
    ),
    _e(
        "not_implemented",
        1,
        False,
        "not implemented yet",
        "this command lands in a later task group",
        ["orch", "help"],
        "The operation is declared but its handler is not written yet (C6/C7).",
    ),
    # 2 usage or not found
    _e(
        "usage",
        2,
        False,
        "invalid usage",
        "orch describe {cmd}",
        ["orch", "describe", "{cmd}"],
        "The arguments do not parse.",
    ),
    _e("unknown_command", 2, False, "unknown command", "orch help", ["orch", "help"], "No such command."),
    _e(
        "not_found",
        2,
        False,
        "not found",
        "orch list",
        ["orch", "list"],
        "No such ticket, task, question, artifact or person.",
    ),
    _e(
        "ambiguous_ref",
        2,
        False,
        "ambiguous reference",
        "name the ticket REF, for example DEMO-0043",
        ["orch", "status"],
        "REF left out and the session holds no claim or more than one; the candidates are in the message.",
    ),
    _e(
        "grant.secret_in_args",
        2,
        False,
        "an argument contains a grant secret",
        "never put ORCH_GRANT into arguments",
        ["orch", "help"],
        "An argument has the shape of an ORCH_GRANT secret; it is refused before anything runs or is stored.",
    ),
    # 3 not allowed
    _e(
        "human_only",
        3,
        False,
        "human only",
        "orch ask or orch wait",
        ["orch", "describe", "ask"],
        "A person's signature with user presence is needed; an agent never gets this.",
    ),
    _e(
        "custody.no_prompt",
        3,
        False,
        "no terminal to ask for your passphrase on",
        "run this command yourself, in your own terminal",
        ["orch", "help"],
        "A human signature needs the person's own terminal (/dev/tty); there is none (D65). An agent never gets this.",
    ),
    _e(
        "custody.no_key",
        3,
        False,
        "no device key of yours here",
        "orch init sets up your device key",
        ["orch", "help"],
        "The key file of the person's device is missing; a human signature needs it (D65).",
    ),
    _e(
        "custody.wrong_passphrase",
        3,
        False,
        "wrong passphrase",
        "run the command again and type your passphrase",
        ["orch", "help"],
        "The passphrase did not unlock the device key; nothing was signed and nothing was written.",
    ),
    _e(
        "sign.convenience_readers",
        3,
        False,
        "the new reader has not declared that it understands convenience signatures",
        "pass the reader's declaration (orch keys reader declare --enrol, then --reader-decl PATH)",
        ["orch", "help"],
        "Adding a reader (a member, a relay pairing, a device) to a workspace whose history has auth convenience "
        "events is refused unless the new reader declares and proves level 1; the reason is no_declaration or "
        "reader_too_old (orch-v2-host 0.8.6 item 9).",
    ),
    _e(
        "sign.convenience_unavailable",
        3,
        False,
        "a reader of this workspace has not declared that it understands convenience signatures",
        "declare the readers it names (orch keys reader declare), or remove them; sign with your passphrase key",
        ["orch", "help"],
        "A convenience signature is refused while another current reader (a device, another member's core, a relay) "
        "is below reader level 1; the reason (other_member, phone_device, relay_paired) names them "
        "(orch-v2-host 0.8.6 item 9.9).",
    ),
    _e(
        "sign.hub_only",
        3,
        False,
        "this workspace is joined to a hub in full mode: approve on your phone or in the hub",
        "approve in the hub or on your phone; to leave the hub (break-glass): orch hub leave",
        ["orch", "hub", "status"],
        "Full hub mode (D78, D81): the hub's role gate decides every human verb and a channel signs it; no local key, "
        "click signer or relay device does. Only the owner's break-glass leave is signed locally. Verbs the hub cannot "
        "build yet are not signable here either.",
    ),
    _e(
        "sign.key_exposed",
        3,
        False,
        "this device key was in one-click custody, so it cannot do this: make a new key with your recovery code",
        "orch keys custody passphrase --rotate --workspace W (needs your recovery code; works while the hub is down)",
        ["orch", "keys", "custody", "passphrase", "--rotate"],
        "#1097: a device key with a signed device.exposed mark (written before it entered convenience custody)"
        " cannot enter full hub mode, leave a hub on its own, or rotate "
        "the workspace key, because a process of yours may hold a copy. Only a new key (device.rotated) clears it.",
    ),
    _e(
        "relay.pair_open",
        3,
        False,
        "a relay pairing is already in progress",
        "one pairing at a time: cancel the live offer or wait for it and its cert request to end",
        ["orch", "host", "relay", "pair", "list"],
        "A relay pairing offer was refused because a live offer or an outstanding cert request exists in this "
        "workspace (orch-v2-host 11A.15.6 step 1).",
    ),
    _e(
        "relay.no_primary",
        3,
        False,
        "no verified primary for relay pairing",
        "set the primary (the device that signs phone certificates) first",
        ["orch", "host", "relay", "primary", "set"],
        "A relay pairing offer needs a verified membership/primary.json: the primary's certificate under the owner "
        "key (orch-v2-host 11A.15.1).",
    ),
    _e(
        "relay.quota",
        3,
        False,
        "the relay's membership quota is reached",
        "remove a device from the workspace before pairing another",
        ["orch", "host", "status"],
        "A relay pairing offer was refused under the membership_quota alarm or at the member-list cap "
        "(orch-v2-host 11A.15.6 step 1).",
    ),
    _e(
        "host.dev_refused",
        3,
        False,
        "refused by the Simulator dev gate",
        "a dev host serves throwaway dev-pair sandboxes only and a normal host never serves one; use a real workspace",
        ["orch", "host", "status"],
        "A host started with --dev-sim-convenience refused to start or refused a non-sandbox workspace, or a normal "
        "host refused a dev-pair sandbox workspace (prefix SBX); the reason is first in the message (orch-v2-host "
        "11A.17).",
    ),
    _e(
        "reader.decl_invalid",
        3,
        False,
        "the reader declaration does not verify",
        "make a new one with the reader's own orch (orch keys reader declare)",
        ["orch", "help"],
        "A reader declaration is refused for its shape, signature, workspace, device, keys, person, binding, time "
        "window or known-answer digests; the message starts with the reason (orch-v2-host 0.8.6 item 9.3).",
    ),
    _e(
        "custody.no_batch",
        3,
        False,
        "this key backend cannot sign a reviewed batch",
        "use a passphrase device key (orch init sets one up)",
        ["orch", "help"],
        "orch import v1 signs a reviewed batch under one unlock; a key backend that asks per signature cannot, so "
        "nothing is signed (F1 10.4a, 14.2).",
    ),
    _e(
        "grant.required",
        3,
        False,
        "no grant",
        "ask the person to run orch grant, then set ORCH_GRANT",
        ["orch", "status"],
        "An agent operation needs a standing grant (ORCH_GRANT).",
    ),
    _e(
        "grant.expired",
        3,
        False,
        "grant expired or revoked",
        "ask the person for a new grant",
        ["orch", "status"],
        "The grant is past its end, was revoked, or does not cover this ticket or verb.",
    ),
    _e(
        "grant.verb",
        3,
        False,
        "the grant does not cover this operation",
        "ask the person for a grant that lists this operation, or use another operation",
        ["orch", "status"],
        "A grant with a list of verbs names operations, matched exactly (F1 10.1); this operation is not in it.",
    ),
    _e(
        "role.denied",
        3,
        False,
        "your role does not allow this",
        "ask an owner or maintainer",
        ["orch", "status"],
        "The person's role, the ticket's people or its visibility do not allow the action.",
    ),
    _e(
        "transition.refused",
        3,
        False,
        "the ticket status does not allow this",
        "orch show REF",
        ["orch", "show"],
        "A status transition or an edit on a done or closed ticket that the lifecycle refuses.",
    ),
    _e(
        "verify.unapproved",
        3,
        False,
        "the task's verify command is neither approved nor yours",
        "a person approves the plan, or add your own verify command",
        ["orch", "show"],
        "task done --run: the verify command runs only if the plan gate applies and is approved at its current "
        "generation, or the caller's own person wrote it last (F1 Receipts); nothing was run.",
    ),
    _e(
        "stop",
        3,
        False,
        "STOP: report to the user",
        "the same refusal came three times; do not retry",
        ["orch", "status"],
        "The stop rule: the same refusal three times in a row in one session.",
    ),
    # 4 claim, lease, lock
    _e(
        "claim.required",
        4,
        False,
        "you hold no claim on the ticket",
        "orch claim",
        ["orch", "claim"],
        "The operation needs the session's claim.",
    ),
    _e(
        "claim.held",
        4,
        False,
        "another session holds the claim",
        "orch claim --takeover --reason TEXT, or pick another ticket",
        ["orch", "describe", "claim"],
        "One claim per ticket.",
    ),
    _e(
        "claim.not_live",
        4,
        False,
        "no live claim for that session",
        "orch status",
        ["orch", "status"],
        "A release names a claim that is not live.",
    ),
    _e(
        "lease.held",
        4,
        False,
        "another session holds the task lease",
        "pick another task: orch task next",
        ["orch", "task", "next"],
        "Two sessions on the same task are refused (A4).",
    ),
    _e(
        "lease.required",
        4,
        False,
        "you hold no lease on the task",
        "orch task start TASK",
        ["orch", "describe", "task.start"],
        "Done, skip, block and reopen need the session's lease.",
    ),
    _e(
        "lock.busy",
        4,
        True,
        "the workspace lock is busy",
        "retry in a moment",
        ["orch", "status"],
        "Another writer holds the file lock.",
    ),
    _e(
        "host.required",
        3,
        False,
        "this workspace is served by the orch host",
        "the host owns this workspace now; start its service instead of using direct mode",
        ["orch", "host", "list"],
        "A direct-mode call on a workspace the host has adopted (D-P2-18): there is no silent fallback.",
    ),
    _e(
        "host.untrusted",
        3,
        False,
        "the process behind the host socket is not the host the registry names",
        "do not use this socket; check orch host list and who owns the run directory",
        ["orch", "host", "list"],
        "Raised only by the client (H4b, H5b): the socket's owner or welcome does not match the trusted pin.",
    ),
    _e(
        "host.busy",
        4,
        False,
        "the host is already creating or serving this workspace",
        "orch host list",
        ["orch", "host", "list"],
        "init --host: the root or the workspace id is already registered, has a genesis or is being bootstrapped (N5).",
    ),
    _e(
        "custody.unsafe_mode",
        3,
        False,
        "the directory or one of its parents is not safe for the host",
        "use a directory owned by you whose parents only you or root can change",
        ["orch", "describe", "{cmd}"],
        "init --host: the root or an ancestor fails the host's ownership and mode checks (D-P2-17).",
    ),
    _e(
        "host.unavailable",
        9,
        True,
        "the orch host cannot be reached",
        "start the host service, then retry",
        ["orch", "host", "list"],
        "init --host: no host answers on the control socket, or the connection dropped (§0.6, raised by the client).",
    ),
    # 5 validation
    _e(
        "invalid.input",
        5,
        False,
        "invalid input",
        "orch describe {cmd}",
        ["orch", "describe", "{cmd}"],
        "The arguments parse but do not match the operation's input schema.",
    ),
    _e(
        "ac.evidence_missing",
        5,
        False,
        "acceptance criteria without evidence",
        "orch artifact add FILE --ac AC1",
        ["orch", "describe", "artifact.add"],
        "Submit needs evidence for every acceptance criterion; the missing ones are in the message.",
    ),
    _e(
        "verify.failed",
        5,
        False,
        "the task's verify command failed",
        "fix the cause, then run the task again",
        ["orch", "task", "next"],
        "task done --run: the exit code was not 0, nothing was appended.",
    ),
    _e(
        "observe.unavailable",
        5,
        False,
        "a linked repository cannot be observed",
        "orch show names the repository; fix settings.repos or links.branches",
        ["orch", "show"],
        "A linked repo has no working copy, or git does not know its branch or the ref names no commit: submit would "
        "judge code nobody looked at.",
    ),
    _e(
        "gate.stale",
        5,
        False,
        "the gate changed under the decision",
        "orch show",
        ["orch", "show"],
        "A decision names a gate hash or generation that is no longer current.",
    ),
    _e(
        "artifact.mismatch",
        5,
        False,
        "an evidence file differs from its recorded digest",
        "orch show",
        ["orch", "show"],
        "A bound artifact file does not hash to the digest in the log (F1 5.7); nothing was shown for signing.",
    ),
    _e(
        "gate.already_approved",
        5,
        False,
        "you already approved this gate at this generation",
        "orch show",
        ["orch", "show"],
        "A person counts once per gate generation; a second approval would change nothing.",
    ),
    _e(
        "source.missing",
        5,
        False,
        "a source ref is missing",
        "restore the branch, then retry",
        ["orch", "status"],
        "A verify or code decision while a linked repo ref is missing.",
    ),
    # 6 parse
    _e(
        "parse.json",
        6,
        False,
        "the input is not strict JSON",
        "orch describe {cmd}",
        ["orch", "describe", "{cmd}"],
        "Structured input is JSON only, strictly parsed.",
    ),
    _e(
        "parse.text",
        6,
        False,
        "the text breaks the text rules",
        "orch describe {cmd}",
        ["orch", "describe", "{cmd}"],
        "Controls, bidi or unassigned code points in text (format 11.3).",
    ),
    # 7 wait timeout
    _e(
        "wait.timeout",
        7,
        True,
        "wait timed out",
        "orch wait",
        ["orch", "wait"],
        "orch wait --strict-timeout reached its timeout.",
    ),
    # 8 base_rev
    _e(
        "conflict.section",
        8,
        True,
        "the section changed since you read it",
        "orch show --section SECTION, then redo the edit",
        ["orch", "show"],
        "base_rev conflict on a section; orch tracks base_rev itself, so re-reading fixes it.",
    ),
    _e(
        "conflict.field",
        8,
        True,
        "the field changed since you read it",
        "orch show, then redo the edit",
        ["orch", "show"],
        "base_rev conflict on a ticket field.",
    ),
    # 9 retryable
    _e(
        "quick.unlabelled",
        9,
        True,
        "the quick task was created but not labelled",
        "run the same orch quick add again: it labels that ticket instead of creating a second one",
        ["orch", "quick", "list"],
        "orch quick add: the ticket exists (its key is in the message) without the quick label; a retry with the same "
        "title labels it.",
    ),
    _e(
        "hub.refused",
        3,
        False,
        "the hub is refused",
        "check the hub card and that this is a dev workspace for a dev hub",
        ["orch", "status"],
        "The hub card or the join did not verify (hub contract section 14: bad signature, other hub, dev mismatch).",
    ),
    _e(
        "hub.already_joined",
        3,
        False,
        "this workspace is already joined to a hub",
        "leave the hub first: orch hub leave",
        ["orch", "status"],
        "A workspace has one hub. Leave it (break-glass) before joining another.",
    ),
    _e(
        "hub.not_joined",
        3,
        False,
        "this workspace is not joined to a hub",
        "nothing to leave",
        ["orch", "status"],
        "There is no joined hub on the workspace log.",
    ),
    _e(
        "retry.later",
        9,
        True,
        "temporary failure",
        "retry in a moment",
        ["orch", "status"],
        "Any other error a retry can fix.",
    ),
    _e(
        "quota.unattended",
        9,
        True,
        "unattended quota used up",
        "ask the person for a grant",
        ["orch", "status"],
        "More than the unattended events or bytes per hour (A3).",
    ),
    _e(
        "members.stale",
        9,
        True,
        "the member list changed",
        "retry; the person re-signs",
        ["orch", "status"],
        "A person event carries an old roster_v.",
    ),
]

# V3 G2: the add-on marketplace (docs/architecture/orch-v2-marketplace.md section 13, plus three of the host's own).
# Every refusal carries the fixed text below and never echoes index or package content. exit: 3 refused, 5 invalid,
# 9 retryable.
_MARKET = (
    ("market.malformed", 5, False, "the marketplace index is not valid", "the file or the URL breaks the format"),
    ("market.unpinned", 5, False, "an entry is not pinned to a full commit id", "a branch, tag or short id is refused"),
    ("market.bad_digest_format", 5, False, "an entry digest is not sha256: plus 64 hex", "fix the index"),
    ("market.duplicate_id", 5, False, "two entries have the same id", "fix the index"),
    ("market.source_mismatch", 3, False, "the index is not the one added from this URL", "check the marketplace URL"),
    (
        "market.index_unsigned",
        3,
        False,
        "the index has no valid signature by the pinned key",
        "wait for a signed index",
    ),
    ("market.rollback", 3, False, "this is older than what was already seen", "an older index or entry is refused"),
    (
        "market.community_unisolated",
        3,
        False,
        "only the official marketplace can be added for now",
        "other marketplaces need per-add-on user isolation",
    ),
    ("market.blocked", 3, False, "the hub blocks this marketplace", "ask the hub admin"),
    ("market.not_allowed", 3, False, "the hub does not allow this marketplace", "ask the hub admin"),
    ("market.unknown_addon", 3, False, "the marketplace has no add-on with this id", "orch market list --entries"),
    ("market.revoked", 3, False, "this add-on version is revoked by the marketplace", "pick another version"),
    ("market.host_not_allowed", 3, False, "the add-on repository host is not allowed", "orch market list"),
    ("market.reserved_id", 3, False, "this id belongs to a built-in or official add-on", "pick another id"),
    ("market.origin_changed", 3, False, "the installed add-on came from another marketplace", "orch addon list"),
    ("market.version_reused", 3, False, "the version number is installed with other bytes", "raise the version"),
    ("market.official_unsigned", 3, False, "an official entry lacks the official publisher signature", "wait"),
    ("market.bad_signature", 3, False, "a signature does not verify", "the entry is refused"),
    (
        "market.publisher_key_changed",
        3,
        False,
        "the publisher key differs from the remembered one",
        "orch market trust --replace",
    ),
    ("market.capability_ceiling", 3, False, "the add-on asks for more than the hub allows", "ask the hub admin"),
    ("market.min_orch", 3, False, "this orch is older than the add-on needs", "update orch"),
    (
        "market.address_refused",
        3,
        False,
        "the marketplace host resolves to an address orch does not connect to",
        "only public addresses are used",
    ),
    ("market.fetch_failed", 9, True, "the marketplace could not be fetched", "check the network and try again"),
    ("market.commit_mismatch", 3, False, "the fetched commit is not the pinned one", "the pin is refused"),
    ("market.package_invalid", 5, False, "the package breaks the package rules", "fix the package"),
    ("market.digest_mismatch", 3, False, "the package digest is not the one in the index", "the entry is refused"),
    ("market.manifest_mismatch", 3, False, "the manifest and the index entry disagree", "the entry is refused"),
    ("market.lock_missing", 5, False, "the lockfile named by the manifest is missing", "fix the package"),
    ("market.lock_invalid", 5, False, "the lockfile breaks the lockfile rules", "fix the package"),
    ("market.staged_changed", 3, False, "the staged package changed after review", "start again"),
    ("market.env_failed", 5, False, "the dependency environment could not be built", "see the add-on's lockfile"),
    ("market.env_changed", 3, False, "the dependency environment no longer matches its stamp", "reinstall the add-on"),
    ("market.actor_refused", 3, False, "a person does this at their own terminal; an agent cannot", "ask the person"),
    # the host's own (not in the spec's table)
    (
        "market.state_invalid",
        5,
        False,
        "the marketplace records of this workspace are not readable",
        "the host does not guess: repair or remove the market directory",
    ),
    ("market.refresh_running", 9, True, "a refresh of this marketplace is already running", "try again in a moment"),
    ("market.already_added", 3, False, "this marketplace is already added", "orch market refresh"),
    # V3 G3: install, update, remove
    ("market.not_installed", 3, False, "this add-on was not installed from a marketplace", "orch addon install ID"),
    ("market.ambiguous_addon", 3, False, "more than one added marketplace lists this id", "name one with --from URL"),
    ("market.still_enabled", 3, False, "the add-on is still turned on", "orch addon disable NAME, then remove it"),
)
_SPECS += [
    _e(code, exit_, retry, msg, hint, ["orch", "market", "list"], "Marketplace refusal (spec section 13): " + msg + ".")
    for code, exit_, retry, msg, hint in _MARKET
]

ERRORS: dict[str, ErrorSpec] = {s.code: s for s in _SPECS}
assert len(ERRORS) == len(_SPECS), "duplicate error code"

# Possible from any operation: the dispatcher, the parser, the stop rule and the safety net produce them.
GLOBAL_ERRORS = (
    "usage",
    "unknown_command",
    "grant.secret_in_args",
    "grant.verb",
    "invalid.input",
    "internal",
    "not_implemented",
    "stop",
    "retry.later",
    "host.required",
    "host.untrusted",
    # H12d-2 (§0.8.6 item 9.4): any person op signed with convenience custody (direct mode too) is refused while a
    # reader of the workspace is below level 1
    "sign.convenience_unavailable",
    # V3 C11 (D78/D81): any person op signed locally is refused while the workspace is in full hub mode
    "sign.hub_only",
    # #1097: a gated act signed by a device key that was in convenience custody (hub join/leave/mode, keys.rotate)
    "sign.key_exposed",
)


def spec(code: str) -> ErrorSpec:
    return ERRORS[code]


class OrchError(Exception):
    """An operation (or the CLI) refuses. ``hint`` and ``fix`` default to the operation's declared entry, then to
    the catalog's. ``fix`` is an argv list that starts with ``orch``."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        hint: str | None = None,
        fix: list[str] | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.code = code
        known = ERRORS.get(code)
        self.message = message if message is not None else (known.message if known else code)
        self.hint = hint
        self.fix = fix
        self.retryable = retryable
        super().__init__(f"{code}: {self.message}")
