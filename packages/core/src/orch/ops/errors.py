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
    _e(
        "tty.required",
        3,
        False,
        "signing requires a controlling terminal",
        "run it in your terminal",
        ["orch", "help"],
        "No controlling terminal; nothing signed.",
    ),
    _e(
        "sign.cancelled",
        3,
        False,
        "nothing was signed",
        "run it again to review",
        ["orch", "help"],
        "The signing review was cancelled.",
    ),
    _e(
        "followup.too_soon",
        3,
        False,
        "follow-up is less than one hour ahead",
        "wait inside the session",
        ["orch", "wait"],
        "follow-up is less than one hour ahead",
    ),
    _e(
        "followup.too_far",
        3,
        False,
        "follow-up exceeds the scheduling horizon",
        "choose a time within 14 days",
        ["orch", "follow-up", "--help"],
        "follow-up exceeds the scheduling horizon",
    ),
    _e(
        "followup.bad_brief",
        3,
        False,
        "brief must be non-empty clean text of at most 4096 bytes",
        "provide a brief of 1 to 4096 UTF-8 bytes",
        ["orch", "follow-up", "--help"],
        "brief must be non-empty clean text of at most 4096 bytes",
    ),
    _e(
        "followup.no_receiver",
        3,
        False,
        "this host does not receive webhooks",
        "ask an owner to run orch host webhooks on, then orch host restart",
        ["orch", "follow-up", "--help"],
        "this host does not receive webhooks",
    ),
    _e(
        "followup.too_many_hooks",
        3,
        True,
        "Too many new webhooks in the last hour. Try again later.",
        "wait a while, then run orch webhook new again; reuse a webhook you already made",
        ["orch", "webhook", "new", "--help"],
        "Too many new webhooks in the last hour. Try again later.",
    ),
    _e(
        "followup.denied",
        3,
        False,
        "the model refused the follow-up",
        "read the denial code and resolve it before retrying",
        ["orch", "show"],
        "the model refused the follow-up",
    ),
    _e(
        "schedule.denied",
        3,
        False,
        "the model refused the schedule",
        "read the denial and correct the schedule",
        ["orch", "schedule", "list"],
        "the model refused the schedule",
    ),
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
        "orch setup sets up your device key",
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
        "use a passphrase device key (orch setup sets one up)",
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
        "addon.off",
        3,
        False,
        "the add-on is off in this workspace",
        "tell the person; do not work around it",
        ["orch", "addon", "list"],
        "The command belongs to an add-on (quick tasks, schedules, ...) that is not granted or is switched off here. "
        "Nothing was changed. Existing data stays readable; an owner turns the add-on on in Add-ons.",
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
        "orch setup: the root or the workspace id is already registered, has a genesis or is being bootstrapped (N5).",
    ),
    _e(
        "custody.unsafe_mode",
        3,
        False,
        "the directory or one of its parents is not safe for the host",
        "use a directory owned by you whose parents only you or root can change",
        ["orch", "describe", "{cmd}"],
        "orch setup: the root or an ancestor fails the host's ownership and mode checks (D-P2-17).",
    ),
    _e(
        "host.unavailable",
        9,
        True,
        "the orch host cannot be reached",
        "start the host service, then retry",
        ["orch", "host", "list"],
        "orch setup: no host answers on the control socket, or the connection dropped (§0.6, raised by the client).",
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
        "wait.incomplete",
        5,
        False,
        "the gate still lacks items: nobody can approve it yet",
        "fill what is missing, then orch wait",
        ["orch", "show"],
        "orch wait on a ticket whose open gate (requirements or plan) still lacks items, with nothing else open for a "
        "person: the wait could never end. The message names each missing item and the command that fills it.",
    ),
    _e(
        "wait.build",
        5,
        False,
        "the plan is approved: build now, nothing waits for a person",
        "orch task next",
        ["orch", "task", "next"],
        "orch wait by the claim holder after the plan is approved, with no gate, question or decision open: the wait "
        "could never end. The message and hint name the next build step.",
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
        "hub.proof_invalid",
        3,
        False,
        "the hub step is not backed by your backup key",
        "repeat the hub step and enter your backup key",
        ["orch", "hub", "status"],
        "A hub step (join, switch to full, leave) needs the owner proof, signed with the person key derived from the "
        "backup key (hub contract 9.6). Detail reason: proof_invalid, proof_expired, proof_missing, dev_mismatch or "
        "dev_refused.",
    ),
    _e(
        "remote.proof_invalid",
        3,
        False,
        "the remote access step is not backed by your backup key",
        "repeat the step and enter your backup key",
        ["orch", "status"],
        "A remote access step (turning it on or changing it, adding a device) needs the owner proof, signed with the "
        "person key derived from the backup key (hub contract 10.2c). Detail reason: proof_invalid, proof_expired, "
        "proof_missing, dev_mismatch or dev_refused.",
    ),
    _e(
        "remote.agents_risk",
        3,
        False,
        "agents on this computer can act as you",
        "set up the separate agent user first, or confirm that you accept the risk",
        ["orch", "status"],
        "Agents run as the same operating-system user as the owner (tier A), so an agent could use a remote session "
        "as the owner (#1198 H1). Remote access is turned on only when agents are isolated or the owner says so.",
    ),
    _e(
        "remote.needs_selective",
        3,
        False,
        "remote access needs the hub in selective mode, or no hub",
        "leave the hub or run it in selective mode, then turn remote access on",
        ["orch", "hub", "status"],
        "In full mode the hub reads this workspace and can act for people in it, so a computer at a customer's place "
        "never offers remote access with it (D93).",
    ),
    _e(
        "remote.no_pending",
        3,
        False,
        "no device is waiting to be added",
        "make a new code and add the device again",
        ["orch", "status"],
        "The pending device is gone: its stream closed, the code ran out or another owner already decided.",
    ),
    _e(
        "remote.full",
        3,
        False,
        "this workspace already has the most remote devices it can hold",
        "remove a device you no longer use",
        ["orch", "status"],
        "At most 64 remote devices, and at most 4 outstanding enrollment codes, per workspace.",
    ),
    _e(
        "switch.refused",
        3,
        False,
        "this workspace cannot be opened from another computer through the hub",
        "open it on its own computer",
        ["orch", "status"],
        "A hub console switch is refused for a workspace that runs at a customer's place or has remote access with a "
        "selective hub (D93); the switch ticket stays unused.",
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
        "device.lockout",
        3,
        False,
        "removing this device would lock someone out",
        "remove it from another device, or add another device first",
        ["orch", "status"],
        "A device is not removed when it signs the removal itself, or when it is the last working device of an owner "
        "(#1198 C15).",
    ),
    _e(
        "remote.refused",
        3,
        False,
        "this is not available over remote access",
        "do it on the computer itself, or over the SSH tunnel",
        ["orch", "status"],
        "The remote dashboard answers only the routes its route table allows (#1198 R1-5); the rest, such as terminals "
        "or the hub signing, stay on the loopback dashboard.",
    ),
    _e(
        "passkey.invalid",
        3,
        False,
        "the passkey was not accepted",
        "try again, or use the SSH tunnel",
        ["orch", "status"],
        "A passkey login or registration failed verification (#1198 R1-5). The reason is logged on the server and "
        "never sent to the browser.",
    ),
    _e(
        "enroll.expired",
        3,
        False,
        "this enrollment link is not valid any more",
        "ask the administrator for a new link",
        ["orch", "status"],
        "The enrollment code is wrong, used, expired or killed by too many wrong tries; the answer is the same for all "
        "of them (#1198 R1-5).",
    ),
    _e(
        "passkey.stepup_required",
        3,
        False,
        "this needs a fresh fingerprint or PIN check first",
        "do the check when the dashboard asks, then click again",
        ["orch", "status"],
        "A big action on a remote dashboard session needs a passkey assertion for exactly this review, used once "
        "(#1198 R1-6). The signer checks it itself; the answer is the same for a missing, stale, replayed or "
        "wrong one.",
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
    ("market.blocked", 3, False, "the hub blocks this add-on", "ask the hub admin"),
    (
        "market.not_allowed",
        3,
        False,
        "the hub does not allow this marketplace, or its policy cannot be verified",
        "ask the hub admin, or reconnect: orch hub status --refresh",
    ),
    ("market.addon_not_allowed", 3, False, "the hub does not allow this add-on", "ask the hub admin"),
    (
        "market.publisher_unpinned",
        3,
        False,
        "the publisher key is not one the hub pins for this marketplace",
        "ask the hub admin",
    ),
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
    ("addon.stage_changed", 3, False, "the staged package changed after review", "stage and review again"),
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
    ("cosmetic.conflict", 3, False, "the workspace name or icon changed meanwhile", "look at it again, then change it"),
    # V3 G3: install, update, remove
    ("market.not_installed", 3, False, "this add-on was not installed from a marketplace", "orch addon install ID"),
    ("market.ambiguous_addon", 3, False, "more than one added marketplace lists this id", "name one with --from URL"),
    ("market.still_enabled", 3, False, "the add-on is still turned on", "orch addon disable NAME, then remove it"),
)
# Data across updates (spec: "Data across updates"): the open-time rule, the lock, the journal, the migrate command.
_MIGRATE = (
    ("format.older", 3, False, "this data was made by an older orch: run `orch update`", "orch update"),
    ("format.newer", 3, False, "this data was made by a newer orch: update orch", "update orch, then try again"),
    ("format.missing", 3, False, "this folder is not orch data", "check the folder"),
    ("format.exists", 3, False, "this folder already has a format", "nothing to do"),
    (
        "update.interrupted",
        3,
        False,
        "an update was interrupted: run `orch update` to finish or roll back",
        "orch update",
    ),
    ("migrate.running", 4, True, "orch is running: stop it first, then try again", "orch stop"),
    ("migrate.signed_path", 3, False, "a migration may never change signed data", "this is a bug in the migration"),
    (
        "migrate.backup_refused",
        3,
        False,
        "the backup cannot be made here",
        "keep backups on this machine, outside synced folders",
    ),
    (
        "migrate.actor_refused",
        3,
        False,
        "a person migrates data at their own terminal; an agent cannot",
        "ask the person",
    ),
    ("migrate.step_failed", 5, False, "a migration step failed; the data was backed up first", "restore the backup"),
)
_SPECS += [
    _e(code, exit_, retry, msg, hint, ["orch", "migrate", "--check"], "Data across updates: " + msg + ".")
    for code, exit_, retry, msg, hint in _MIGRATE
]
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
