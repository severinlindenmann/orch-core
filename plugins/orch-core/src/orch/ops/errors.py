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
        "orch show --section current_state",
        ["orch", "show"],
        "A status transition or an edit on a done or closed ticket that the lifecycle refuses.",
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
