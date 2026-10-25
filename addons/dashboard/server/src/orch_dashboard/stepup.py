"""The step-up operations, defined ONCE (#1198 R1-5, R1-6; host spec 0.8.6 10.5).

**Invariant: a step-up operation never runs for a session that was not opened on this computer unless that session
holds a passkey assertion made for exactly this operation and review, used once, at most `auth.STEPUP_FRESH` old.**
The route table (`routegate.py`) cannot read a body, so a route that serves both a plain click and a step-up variant
(`click/custody`, `click/runner`, `click/sign`, ...) is listed `allowed` there, and the variants are listed HERE as
(route, body predicate) pairs. Every such handler calls `refuse_step_up` as its first statement; `tests/test_stepup.py`
fires each variant through the front with a remote bearer and fails when a handler in this list does not call the
helper first. To add a step-up variant, add a line to `STEP_UP`; there is no per-branch check to remember.

What the helper answers, and what the handler does with it:
* a local session (a launch code on this computer): `{}`, nothing more is needed;
* a remote session: the arguments to add to the signer call, `{"remote": True}` for a plain variant and
  `{"remote": True, "stepup": <the assertion>}` for a step-up variant. The signer decides FROM THE OP whether it needs
  the assertion and checks it itself (core `orch.click.stepup`), so a mistake in this list cannot make it skip it;
* otherwise it raises `passkey.stepup_required`.

Steps that only open a review (`click/custody/seal`, `click/rotate/seal`, `click/runner/review`, `click/prepare`) are
plain: they sign and reveal nothing. The touch comes at the step that acts (`custody`, `rotate`, `runner`, `sign`),
when the review and its digest exist. Ops with no signer review (One click on, a hub connect, "Add a device",
removing a passkey) bind to `passkeys.action_digest`, which both sides compute.
"""

from collections.abc import Callable
from dataclasses import dataclass

from orch.click.passkeys import action_digest

from .errors import HostError

LOCAL = "local"
CLICK = "/api/w/{ws}/click/"
WORKSPACE = "/api/w/{ws}/"
HOST = "/api/"


def stepup_required(name: str):
    return HostError(
        {
            "code": "passkey.stepup_required",
            "message": f"Confirm with your fingerprint or PIN first: {name}.",
            "retryable": False,
        },
        3,
    )


def terminal_needed(message: str):
    return HostError({"code": "custody.terminal_needed", "message": message, "retryable": False}, 3)


def refusal_check(message: str):
    return HostError(
        {"code": "human.click_refused", "message": message, "retryable": False, "reason": "check_failed"}, 3
    )


@dataclass(frozen=True)
class StepUp:
    name: str  # what the person would call it
    route: str  # the route's path under `base`
    applies: Callable[[object], bool]  # the body predicate (the parsed, typed body)
    #: the digest the assertion is bound to: the review's own, or `action_digest(...)` for an op with no signer review
    digest: Callable[[object, str], str | None]
    action: str  # the name the browser asks the ceremony for (`/api/stepup/options`)
    base: str = CLICK

    @property
    def path(self) -> str:
        return self.base + self.route


def always(body) -> bool:
    return True


def _view(body, ws) -> str | None:
    digest = getattr(body, "view_digest", None)
    return digest if isinstance(digest, str) else None


def _fixed(action: str):
    return lambda body, ws: action_digest(action, ws)


#: The list. Every entry needs a passkey assertion from a session that was not opened on this computer. Whole-route
#: entries (predicate `always`) are ALSO `step_up` in routegate.TABLE (the gate lets them through to the handler, which
#: checks; a test keeps the two in step).
STEP_UP: tuple[StepUp, ...] = (
    StepUp("turning One click on", "custody", lambda b: b.on, _fixed("custody.on"), "custody.on"),
    StepUp(
        "turning One click off with a new sealed passphrase",
        "custody",
        lambda b: not b.on and b.sealed is not None,
        _view,
        "custody.off",
    ),
    StepUp("replacing this computer's key", "rotate", always, _view, "rotate"),
    StepUp("turning on dashboard execution", "runner", lambda b: b.on or b.terminals, _view, "runner.on"),
    StepUp(
        "signing with a typed key or a shown secret",
        "sign",
        lambda b: b.sealed is not None or b.reveal_key is not None,
        _view,
        "sign",
    ),
    StepUp("connecting a hub", "hub/connect", always, _fixed("hub.connect"), "hub.connect", WORKSPACE),
    StepUp(
        "adding a device",
        "passkeys/enroll-link",
        always,
        lambda b, ws: action_digest("device.add", "-"),
        "device.add",
        HOST,
    ),
    StepUp(
        "removing a passkey",
        "passkeys/{credential_id}",
        always,
        lambda b, ws: action_digest("passkey.remove", "-", b.credential_id),
        "passkey.remove",
        HOST,
    ),
)
#: Actions whose nonce the click signer draws and checks again (the op is one the signer runs). The others (a hub
#: connect, the passkey store) are the server's own: it draws the nonce and is the only check.
SIGNER_ACTIONS = frozenset({"custody.on", "custody.off", "rotate", "runner.on", "sign"})
#: Actions whose digest is the review's (the browser sends `view_digest`); the rest are `action_digest` of fixed parts.
VIEW_ACTIONS = frozenset({"custody.off", "rotate", "runner.on", "sign"})

#: The routes whose handlers must call `refuse_step_up` first (the test checks them): by path.
GUARDED_PATHS = tuple(dict.fromkeys(s.path for s in STEP_UP))


def refuse_step_up(auth, request, route: str, body, ws: str) -> dict:
    """Call FIRST in every handler of `GUARDED_PATHS`. Returns the arguments the handler adds to its signer call (see
    the module text) or raises `passkey.stepup_required`. ``route`` is the entry's `route` (the literal string)."""
    token = request.state.token
    if auth.login_kind(token) == LOCAL:
        return {}
    for step in STEP_UP:
        if step.route == route and step.applies(body):
            digest = step.digest(body, ws)
            if digest is None:
                raise stepup_required(step.name)
            fresh, proof = auth.stepup_take(token, digest)
            if not fresh:
                raise stepup_required(step.name)
            return {"remote": True, "stepup": proof} if proof is not None else {}
    return {"remote": True}
