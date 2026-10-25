"""The remote route gate (#1198 R1-5, requirement 1): ONE table of what the remote listener may answer.

Every route of the app is listed here with a rule for the front (remote) listener; a route that is not listed is refused
there (default refuse), and `tests/test_remote_route_gate.py` walks the app's real routes and fails when one has no
entry, so a new route cannot be added without deciding this. `security.Security` consults the gate in one place for
every request that arrives on a listener with ``route_gate`` set (the remote one); the loopback listener never asks.

Rules:
* ``ALLOWED``    an admin action or read by plain click after the shown review. The route's own host and signer checks
                 still apply (a remote session never reaches the local-only variants, R1-4).
* ``REFUSED``    never on remote (a shell on the server, launch codes, hub signing, the setup page).
* ``STEP_UP``    needs a fresh passkey assertion bound to the request (host spec 0.8.6 10.5). The signer's check lands
                 in R1-6; until then the gate refuses these like ``REFUSED``.
* ``PRE_LOGIN``  answers without a session on remote (passkey login and enrollment); on loopback they do not exist.
* ``TERMINALS``  a shell on the server: allowed only where the listener's policy has ``terminals`` on (off on remote
                 until the Settings switch of R1-8 turns it on).

A route that serves several bodies where only some are step-up (`click/custody`, `click/runner`, `click/prepare`,
`click/sign`) is listed ``ALLOWED`` with that noted: the body decides, and the handler refuses the step-up variants for
every session that is not local (R1-4, pinned by `test_remote_listener.py`) until R1-6.
"""

from starlette.routing import Match

ALLOWED, REFUSED, STEP_UP, PRE_LOGIN, TERMINALS = "allowed", "refused", "step_up", "pre_login", "terminals"
RULES = (ALLOWED, REFUSED, STEP_UP, PRE_LOGIN, TERMINALS)

W = "/api/w/{ws}"
BODY = "body decides: off is plain, on is step-up (the handler refuses on, R1-4)"
GET, POST, PUT, DELETE, WEBSOCKET = "GET", "POST", "PUT", "DELETE", "WEBSOCKET"

#: (method, route template) -> rule, with the reason where it is not obvious.
TABLE: dict[tuple[str, str], tuple[str, str]] = {
    # --- the app shell and the session
    (GET, "/{path:path}"): (ALLOWED, "the closed SPA surface; which paths answer before login is policy.pre_login"),
    (GET, "/auth/probe"): (REFUSED, "launcher discovery on the local computer"),
    (POST, "/auth/session"): (REFUSED, "launch codes are a local credential"),
    (POST, "/auth/logout"): (ALLOWED, "ends the session"),
    (GET, "/api/session"): (ALLOWED, ""),
    (GET, "/api/events"): (ALLOWED, "the live stream"),
    (GET, "/api/workspaces"): (ALLOWED, ""),
    (GET, "/api/host"): (ALLOWED, ""),
    (GET, "/api/me"): (ALLOWED, ""),
    (GET, W + "/head"): (ALLOWED, ""),
    (GET, W + "/identity"): (ALLOWED, ""),
    # --- passkey login and enrollment (the only pre-login routes besides the shell)
    (POST, "/auth/passkey/options"): (PRE_LOGIN, "login challenge"),
    (POST, "/auth/passkey/login"): (PRE_LOGIN, "login assertion"),
    (POST, "/auth/enroll/options"): (PRE_LOGIN, "enrollment challenge, needs the one-time code"),
    (POST, "/auth/enroll/register"): (PRE_LOGIN, "enrollment registration, needs the one-time code"),
    # --- first-run setup: the setup page is run on the computer (decision for R1-7/R1-8 whether it opens remotely)
    (POST, "/auth/setup"): (REFUSED, "the setup nonce is minted on the computer"),
    (GET, "/api/setup/{route:path}"): (REFUSED, "setup is local until the remote setup flow is built"),
    (POST, "/api/setup/{route:path}"): (REFUSED, "setup is local until the remote setup flow is built"),
    # --- reads
    (GET, W + "/today"): (ALLOWED, ""),
    (GET, W + "/tickets"): (ALLOWED, ""),
    (GET, W + "/board"): (ALLOWED, ""),
    (GET, W + "/tickets/{key}"): (ALLOWED, ""),
    (GET, W + "/tickets/{key}/events"): (ALLOWED, ""),
    (GET, W + "/tickets/{key}/changes"): (ALLOWED, ""),
    (GET, W + "/tickets/{key}/artifacts/{name}"): (ALLOWED, ""),
    (GET, W + "/tickets/{key}/workdir"): (ALLOWED, ""),
    (GET, W + "/settings"): (ALLOWED, ""),
    (GET, W + "/members"): (ALLOWED, ""),
    (GET, W + "/readers"): (ALLOWED, ""),
    (GET, W + "/grants"): (ALLOWED, ""),
    (GET, W + "/versions"): (ALLOWED, ""),
    (GET, W + "/add-ons"): (ALLOWED, ""),
    (GET, W + "/quick"): (ALLOWED, ""),
    (GET, W + "/activity"): (ALLOWED, ""),
    (GET, W + "/usage"): (ALLOWED, ""),
    (GET, W + "/worktrees"): (ALLOWED, ""),
    (GET, W + "/explorer"): (ALLOWED, ""),
    (GET, W + "/explorer/tree"): (ALLOWED, ""),
    (GET, W + "/explorer/file"): (ALLOWED, ""),
    (GET, W + "/explorer/names"): (ALLOWED, ""),
    (GET, W + "/explorer/search"): (ALLOWED, ""),
    (GET, W + "/explorer/history"): (ALLOWED, ""),
    (GET, W + "/explorer/commit"): (ALLOWED, ""),
    (GET, W + "/explorer/compare"): (ALLOWED, ""),
    (GET, W + "/explorer/ticket/{key}"): (ALLOWED, ""),
    (GET, W + "/wiki"): (ALLOWED, ""),
    (GET, W + "/wiki/page"): (ALLOWED, ""),
    (GET, W + "/wiki/search"): (ALLOWED, ""),
    (GET, W + "/wiki/backlinks"): (ALLOWED, ""),
    (GET, W + "/widgets/catalog"): (ALLOWED, ""),
    (GET, "/widget-frame/{rest:path}"): (
        REFUSED,
        "the sandboxed widget frame is loopback only",
    ),
    (POST, W + "/repos/check"): (ALLOWED, "a read-only check"),
    (GET, W + "/model-routing"): (ALLOWED, ""),
    # --- hand-off: prepares the request for the ticket actions (approve, verdict, close, ...); signing is the click
    # routes below or a Terminal, where the passkey/step-up rules apply
    (POST, W + "/handoff"): (ALLOWED, "prepares a request; it signs nothing"),
    (GET, W + "/handoff/{request_id}"): (ALLOWED, ""),
    (DELETE, W + "/handoff/{request_id}"): (ALLOWED, "cancels a request"),
    # --- click-to-sign
    (GET, W + "/click/status"): (ALLOWED, ""),
    (POST, W + "/click/custody/seal"): (STEP_UP, "turning One click off with a new passphrase (sealed to the signer)"),
    (POST, W + "/click/rotate/seal"): (STEP_UP, "device rotate"),
    (POST, W + "/click/rotate"): (STEP_UP, "device rotate"),
    (POST, W + "/click/custody"): (ALLOWED, BODY),
    (POST, W + "/click/runner/review"): (
        ALLOWED,
        BODY,
    ),
    (POST, W + "/click/runner"): (ALLOWED, BODY),
    (POST, W + "/click/images"): (ALLOWED, ""),
    (POST, W + "/click/prepare"): (ALLOWED, "body decides: grant secret reveal and hub steps are refused (R1-4)"),
    (POST, W + "/click/view"): (ALLOWED, ""),
    (POST, W + "/click/state"): (ALLOWED, ""),
    (POST, W + "/click/sign"): (ALLOWED, "sealed bodies (typed secrets) are refused for a remote session (R1-4)"),
    (POST, W + "/click/cancel"): (ALLOWED, ""),
    # --- hub
    (POST, W + "/hubsign"): (REFUSED, "remote mode signs nothing for the hub (D81)"),
    (GET, W + "/hub"): (ALLOWED, ""),
    (GET, W + "/hub/share"): (ALLOWED, ""),
    (POST, W + "/hub/share/preview"): (ALLOWED, "a read-only preview"),
    (POST, W + "/hub/connect"): (STEP_UP, "the second half of a hub join"),
    (POST, W + "/hub/command"): (ALLOWED, "returns text only"),
    # --- settings and small preferences
    (GET, W + "/me/sign_default"): (ALLOWED, ""),
    (PUT, W + "/me/sign_default"): (ALLOWED, ""),
    (GET, W + "/me/risk_notices"): (ALLOWED, ""),
    (POST, W + "/me/risk_notices"): (ALLOWED, ""),
    # --- phone signing
    (GET, W + "/phone/available"): (ALLOWED, ""),
    (POST, W + "/phone/request"): (ALLOWED, ""),
    (POST, W + "/phone/cancel"): (ALLOWED, ""),
    (GET, W + "/phone/status/{id}"): (ALLOWED, ""),
    # --- records
    (GET, W + "/records"): (ALLOWED, ""),
    (GET, W + "/records/current"): (ALLOWED, ""),
    (POST, W + "/records/init"): (ALLOWED, "admin action, plain click"),
    (POST, W + "/records/remote"): (ALLOWED, "admin action, plain click"),
    (POST, W + "/records/push-initial"): (ALLOWED, "admin action, plain click"),
    (POST, W + "/records/commit"): (ALLOWED, "admin action, plain click"),
    (POST, W + "/records/auto-off"): (ALLOWED, "turning off"),
    # --- add-ons and the marketplace (install is an admin action by plain click; running one needs execution on)
    (GET, W + "/market"): (ALLOWED, ""),
    (GET, W + "/market/installed"): (ALLOWED, ""),
    (POST, W + "/market/stage"): (ALLOWED, "admin action, plain click"),
    (DELETE, W + "/market/stage/{stage_id}"): (ALLOWED, ""),
    (POST, W + "/market/uninstall"): (ALLOWED, "turning off"),
    (POST, W + "/market/refresh"): (ALLOWED, ""),
    # --- runs, agents, terminals
    (GET, W + "/runs"): (ALLOWED, ""),
    (POST, W + "/runs/{run}/stop"): (ALLOWED, "turning off"),
    (POST, W + "/runs/{run}/attach"): (TERMINALS, "a terminal is a shell on the server"),
    (POST, W + "/terminals/open"): (TERMINALS, "a terminal is a shell on the server"),
    (POST, W + "/terminals/preview"): (TERMINALS, "a terminal is a shell on the server"),
    (WEBSOCKET, "/term"): (TERMINALS, "the terminal socket"),
    (POST, W + "/agents/preview"): (ALLOWED, "a read-only preview"),
    (POST, W + "/agents/start"): (ALLOWED, "plain click once the runner is on (the host's own gate); on is step-up"),
    (GET, W + "/runner"): (ALLOWED, ""),
    (GET, W + "/runner/deposits/{deposit_id}"): (ALLOWED, ""),
    (POST, W + "/runner/forget"): (ALLOWED, "turning off"),
    # --- tester's checklist
    (GET, "/api/testrun/{run}"): (ALLOWED, ""),
    (GET, "/api/testrun/{run}/previous"): (ALLOWED, ""),
    (PUT, "/api/testrun/{run}/steps/{step}"): (ALLOWED, ""),
    (POST, "/api/testrun/{run}/steps/{step}/images"): (ALLOWED, ""),
    (GET, "/api/testrun/{run}/images/{name}"): (ALLOWED, ""),
    (GET, "/api/testrun/{run}/screenshots.zip"): (ALLOWED, ""),
    (DELETE, "/api/testrun/{run}/images/{name}"): (ALLOWED, ""),
    # --- worktrees
    (POST, W + "/worktrees/create"): (ALLOWED, "admin action, plain click"),
    (POST, W + "/worktrees/remove"): (ALLOWED, "turning off"),
    (POST, W + "/workdir/delete"): (ALLOWED, "turning off"),
    # --- shares
    (GET, W + "/shares"): (ALLOWED, ""),
    (POST, W + "/shares/prepare"): (ALLOWED, "admin action, plain click"),
    (POST, W + "/shares/send"): (ALLOWED, "admin action, plain click"),
    (POST, W + "/shares/revoke"): (ALLOWED, "turning off"),
    (POST, W + "/shares/discard"): (ALLOWED, "turning off"),
    (POST, W + "/shares/register"): (ALLOWED, "admin action, plain click"),
}
assert all(rule in RULES for rule, _ in TABLE.values())


def rule_of(method: str, template: str) -> str:
    """The rule for a route; anything not in the table is refused."""
    return TABLE.get((method, template), (REFUSED, ""))[0]


class RouteGate:
    """Finds which route a request would reach and returns its rule. ``router`` is the app's router, read at request
    time, so routes registered after the middleware was added are seen."""

    def __init__(self, router):
        self.router = router

    def template(self, scope):
        """The route template the app would run for this request, or None when no route matches fully (the app would
        answer 404 or 405)."""
        for route in self.router.routes:
            match, _ = route.matches(scope)
            if match == Match.FULL:
                return route.path
        return None

    def rule(self, scope) -> str:
        template = self.template(scope)
        if template is None:
            return REFUSED
        return rule_of("WEBSOCKET" if scope["type"] == "websocket" else scope["method"], template)
