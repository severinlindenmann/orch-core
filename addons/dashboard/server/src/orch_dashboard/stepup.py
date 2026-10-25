"""The step-up operations, defined ONCE (#1198 R1-5; host spec 0.8.6 10.5).

**Invariant: a step-up operation never runs for a session whose kind is outside `REVEAL_SESSION_KINDS` until R1-6
(the passkey step-up in the signer) exists.** The route table (`routegate.py`) cannot read a body, so a route that
serves both a plain click and a step-up variant (`click/custody`, `click/runner`, `click/prepare`, `click/sign`, ...)
is listed `allowed` there, and the variants are listed HERE as (route, body predicate) pairs. Every such handler calls
`refuse_step_up` as its first statement; `tests/test_stepup.py` fires each variant through the front with a remote
bearer and fails when a handler in this list does not call the helper first. To add a step-up variant, add a line to
`STEP_UP`; there is no per-branch check to remember.

R1-6 replaces the refusal by "needs a fresh passkey assertion bound to this View".
"""

from collections.abc import Callable
from dataclasses import dataclass

from .errors import HostError

#: How a session was opened (`Auth.login_kind`) for which a step-up operation may run. Until the passkey step-up
#: exists (R1-6), only a session opened by `orch dashboard` on this computer; an unknown kind and any remote or hub kind
#: are refused (fail closed).
REVEAL_SESSION_KINDS = frozenset({"local"})

SEALED_LOCAL_ONLY = "A new passphrase for your key needs a dashboard opened on this computer."
RUNNER_LOCAL_ONLY = "Turning on dashboard execution needs a dashboard opened on this computer."
ROTATE_LOCAL_ONLY = "Replacing this computer's key needs a dashboard opened on this computer."
CUSTODY_ON_LOCAL_ONLY = (
    "Turning One click on needs a dashboard opened on this computer. Use the terminal: "
    "orch host convenience on --workspace {ws}"
)
REVEAL_LOCAL_ONLY = "Agent access with a shown secret needs a dashboard opened on this computer."
HUB_LOCAL_ONLY = "A hub step with your backup key needs a dashboard opened on this computer."
SIGN_LOCAL_ONLY = "This signing step needs a dashboard opened on this computer."


def refusal_check(message: str):
    return HostError(
        {"code": "human.click_refused", "message": message, "retryable": False, "reason": "check_failed"}, 3
    )


def terminal_needed(message: str):
    return HostError({"code": "custody.terminal_needed", "message": message, "retryable": False}, 3)


@dataclass(frozen=True)
class StepUp:
    name: str  # what the person would call it
    route: str  # the route's last path segments under /api/w/{ws}/click/
    applies: Callable[[object], bool]  # the body predicate (the parsed, typed body)
    message: str
    code: str = "human.click_refused"


def always(body) -> bool:
    return True


def _action(*names):
    return lambda body: getattr(body, "action", None) in names


#: The list. Every entry is refused for a session outside REVEAL_SESSION_KINDS. The three wholly step-up routes
#: (predicate `always`) are ALSO `step_up` in routegate.TABLE: refused at the gate and again in the handler, on purpose
#: (defence in depth; a test keeps the two in step). Adding a passkey or a device has no route yet (an enrollment code
#: is minted on the computer, R1-7/R1-6): `NO_ROUTE_YET` names them so R1-6 cannot forget.
STEP_UP: tuple[StepUp, ...] = (
    StepUp(
        "One click on (current passphrase, or resume)",
        "custody",
        lambda b: b.on,
        CUSTODY_ON_LOCAL_ONLY,
        "custody.terminal_needed",
    ),
    StepUp(
        "One click off with a new sealed passphrase",
        "custody",
        lambda b: not b.on and b.sealed is not None,
        SEALED_LOCAL_ONLY,
    ),
    StepUp("seal a new passphrase (One click off)", "custody/seal", always, SEALED_LOCAL_ONLY),
    StepUp("device rotate, begin", "rotate/seal", always, ROTATE_LOCAL_ONLY),
    StepUp("device rotate", "rotate", always, ROTATE_LOCAL_ONLY),
    StepUp(
        "dashboard execution on (or terminals), review",
        "runner/review",
        lambda b: b.on or b.terminals,
        RUNNER_LOCAL_ONLY,
    ),
    StepUp("dashboard execution on (or terminals)", "runner", lambda b: b.on or b.terminals, RUNNER_LOCAL_ONLY),
    StepUp("grant whose secret is revealed", "prepare", _action("agent_access"), REVEAL_LOCAL_ONLY),
    StepUp("hub join / leave with the backup key", "prepare", _action("hub_join", "hub_leave"), HUB_LOCAL_ONLY),
    StepUp(
        "a sealed secret or a reveal key at sign",
        "sign",
        lambda b: b.sealed is not None or b.reveal_key is not None,
        SIGN_LOCAL_ONLY,
    ),
)
#: Hub join / mode / connect: join and leave go through `prepare` and `sign` above, `hub/connect` is a STEP_UP route in
#: routegate.TABLE (refused at the gate), hub mode is CLI-only.
#: name -> path fragments that would be its route. R1-6: when one of these gets a route, add its STEP_UP entry and
#: delete the name here; `test_no_route_adds_a_passkey_or_a_device_yet` fails if a route appears first.
NO_ROUTE_YET = {
    "adding a passkey": ("passkey/add", "passkey/new", "passkeys/add"),
    "adding a device (enrollment link)": ("device/add", "enroll/link", "enroll/mint", "devices/link"),
    "removing a passkey": ("passkey/remove", "passkeys/remove", "passkey/delete"),
}

#: The routes whose handlers must call `refuse_step_up` first (the test checks them).
GUARDED_ROUTES = tuple(dict.fromkeys(s.route for s in STEP_UP))


def refuse_step_up(auth, request, route: str, body, ws: str) -> None:
    """Call FIRST in every handler of `GUARDED_ROUTES`. Raises the refusal when `body` is a step-up variant of `route`
    and the session was not opened on this computer."""
    if auth.login_kind(request.state.token) in REVEAL_SESSION_KINDS:
        return
    for step in STEP_UP:
        if step.route == route and step.applies(body):
            message = step.message.format(ws=ws)
            raise terminal_needed(message) if step.code == "custody.terminal_needed" else refusal_check(message)
