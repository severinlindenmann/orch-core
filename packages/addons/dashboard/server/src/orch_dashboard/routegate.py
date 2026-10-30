"""The remote route gate (#1198 R1-5, requirement 1): ONE table of what the remote listener may answer.

Every route of the app is listed here with a rule for the front (remote) listener; a route that is not listed is refused
there (default refuse), and `tests/test_remote_route_gate.py` walks the app's real routes and fails when one has no
entry, so a new route cannot be added without deciding this. `security.Security` consults the gate in one place for
every request that arrives on a listener with ``route_gate`` set (the remote one); the loopback listener never asks.

Rules:
* ``ALLOWED``    an admin action or read by plain click after the shown review. The route's own host and signer checks
                 still apply (a remote session never reaches the local-only variants, R1-4).
* ``REFUSED``    never on remote (a shell on the server, launch codes, hub signing, the setup page).
* ``STEP_UP``    a whole route that needs a fresh passkey assertion bound to the request (host spec 0.8.6 10.5). The
                 gate lets it through to its handler, whose first statement is `stepup.refuse_step_up`: the check is
                 ONE helper, not one per route (R1-6).
* ``PRE_LOGIN``  answers without a session on remote (passkey login and enrollment); on loopback they do not exist.
* ``TERMINALS``  a shell on the server: allowed only where the listener's policy has ``terminals`` on (off on remote
                 until the Settings switch of R1-8 turns it on).

A route that serves several bodies where only some are step-up (`click/custody`, `click/runner`, `click/prepare`,
`click/sign`) is listed ``ALLOWED`` with that noted: the body decides, and the handler asks `stepup.refuse_step_up`,
which needs a passkey assertion for the step-up variants of every session that was not opened on this computer.
"""

from starlette.routing import Match

ALLOWED, REFUSED, STEP_UP, PRE_LOGIN, TERMINALS = "allowed", "refused", "step_up", "pre_login", "terminals"
RULES = (ALLOWED, REFUSED, STEP_UP, PRE_LOGIN, TERMINALS)

W = "/api/w/{ws}"
BODY = "body decides: off is plain, on is step-up (the handler asks refuse_step_up)"
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
    (GET, W + "/hub/rail"): (ALLOWED, "H1: the company rail (names and counts the hub gives this workspace's owner)"),
    (POST, W + "/hub/switch"): (ALLOWED, "H1: a one-use ticket to open the hub console, or to sign out of it"),
    # --- passkey login and enrollment (the only pre-login routes besides the shell)
    (POST, "/api/trusted/enrol"): (REFUSED, "browser keys are enrolled on this computer by a real local sign-in"),
    (GET, "/api/trusted"): (REFUSED, "trusted browsers are managed on this computer"),
    (DELETE, "/api/trusted/{kid}"): (REFUSED, "trusted browsers are managed on this computer"),
    (POST, "/auth/switch/challenge"): (REFUSED, "E4: browser keys live on loopback; remote never redeems silently"),
    (POST, "/auth/switch"): (PRE_LOGIN, "H1: a one-use hub switch ticket (the host asks the hub over its link)"),
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
    (GET, W + "/add-ons/{name}/settings"): (ALLOWED, ""),
    (GET, W + "/quick"): (ALLOWED, ""),
    (GET, W + "/activity"): (ALLOWED, ""),
    (GET, W + "/usage"): (ALLOWED, ""),
    (GET, W + "/followups"): (ALLOWED, ""),
    (GET, W + "/schedules"): (ALLOWED, ""),
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
    (GET, W + "/addons"): (ALLOWED, "the add-on packages with declarative views that are on in this workspace"),
    (GET, W + "/view/{name}"): (ALLOWED, "named reads of declarative add-on views (addon_reads.py)"),
    (GET, W + "/wiki"): (ALLOWED, ""),
    (GET, W + "/wiki/page"): (ALLOWED, ""),
    (POST, W + "/wiki/create"): (ALLOWED, "admin action, plain click"),
    (GET, W + "/wiki/search"): (ALLOWED, ""),
    (GET, W + "/wiki/backlinks"): (ALLOWED, ""),
    (GET, W + "/widgets/catalog"): (ALLOWED, ""),
    (GET, "/widget-frame/{rest:path}"): (
        REFUSED,
        "the sandboxed widget frame is loopback only",
    ),
    (POST, W + "/repos/check"): (ALLOWED, "a read-only check"),
    (GET, W + "/model-routing"): (ALLOWED, ""),
    # --- click-to-sign: the only prepare route; the passkey/step-up rules apply at the touch
    (GET, W + "/click/status"): (ALLOWED, ""),
    (POST, W + "/click/custody/seal"): (
        ALLOWED,
        "the review of a sealed new passphrase; the touch is at /click/custody",
    ),
    (POST, W + "/click/rotate/seal"): (ALLOWED, "the review of a device rotate; the touch is at /click/rotate"),
    (POST, W + "/click/rotate"): (STEP_UP, "device rotate"),
    (GET, W + "/click/github"): (ALLOWED, "whether a GitHub token is saved; reads the add-on list, never the token"),
    (POST, W + "/click/github/seal"): (
        ALLOWED,
        "the signer's key for a sealed GitHub token; the touch is at /click/github",
    ),
    (POST, W + "/click/github"): (STEP_UP, "the GitHub token, sealed to the signer"),
    (POST, W + "/click/github/remove"): (ALLOWED, "deletes the stored token: takes authority away, a plain click"),
    (POST, W + "/click/custody"): (ALLOWED, BODY),
    (POST, W + "/click/runner/review"): (ALLOWED, "the review of dashboard execution; the touch is at /click/runner"),
    (POST, W + "/click/runner"): (ALLOWED, BODY),
    (POST, W + "/click/images"): (ALLOWED, ""),
    (POST, W + "/click/prepare"): (ALLOWED, "prepares a review of any kind; the touch is at /click/sign"),
    (POST, W + "/click/view"): (ALLOWED, ""),
    (POST, W + "/click/state"): (ALLOWED, ""),
    (POST, W + "/click/sign"): (
        ALLOWED,
        "body decides: sealed bodies and reveal keys are step-up",
    ),
    (POST, W + "/click/cancel"): (ALLOWED, ""),
    # --- hub
    (POST, W + "/hubsign"): (REFUSED, "remote mode signs nothing for the hub (D81)"),
    (POST, W + "/hub/approve/prepare"): (REFUSED, "FB15: an add-on hand-off for the hub; on this computer only (D81)"),
    (GET, W + "/hub/approve/{request_id}"): (REFUSED, "FB15: polls that hand-off; on this computer only (D81)"),
    (GET, W + "/hub"): (ALLOWED, ""),
    (GET, W + "/hub/share"): (ALLOWED, ""),
    (GET, W + "/hub/share/audience"): (ALLOWED, "a read: hub people and teams, display names only"),
    (GET, W + "/hub/shared"): (ALLOWED, "a read-only list of tickets others shared with this owner"),
    (POST, W + "/hub/share/preview"): (ALLOWED, "a read-only preview"),
    (POST, W + "/hub/share/revoke"): (ALLOWED, "turning off: owner, plain click, no signature, full session only"),
    (POST, W + "/hub/share/rule-remove"): (ALLOWED, "turning off: owner, plain click, no signature, full session only"),
    (POST, W + "/hub/share/rule-add"): (REFUSED, "sharing more is decided on this computer"),
    (POST, W + "/hub/share/sync"): (REFUSED, "sharing more is decided on this computer"),
    (GET, W + "/remote"): (REFUSED, "R2: remote access is changed and read on this computer only"),
    (POST, W + "/remote/off"): (REFUSED, "R2: on this computer only; an owner's remote session ends it in R3"),
    (POST, W + "/remote/code"): (REFUSED, "R2: a device code is shown on this computer only"),
    (POST, W + "/remote/devices/{device_id}/remove"): (REFUSED, "R2: devices are managed on this computer"),
    (GET, W + "/org"): (ALLOWED, "company offers: names and status"),
    (GET, W + "/org/item"): (ALLOWED, "a read; the host answers the owner only"),
    (POST, W + "/org/accept"): (REFUSED, "turning a company item on is decided on this computer"),
    (POST, W + "/org/decline"): (ALLOWED, "turning off: owner, plain click, full session only"),
    (POST, W + "/org/sync"): (ALLOWED, "fetches the offers; grants nothing"),
    (POST, W + "/org/tool"): (ALLOWED, "owner, plain click; turning ON is refused unless the session is local"),
    (POST, W + "/hub/link"): (ALLOWED, "reads the link's hub card; nothing is signed or kept"),
    (POST, W + "/hub/connect"): (STEP_UP, "the second half of a hub join"),
    (POST, W + "/hub/refresh"): (
        ALLOWED,
        "asks the host for the hub snapshot again; a remote session is refused in the handler",
    ),
    # --- the passkey step-up ceremony and the passkey store (R1-6)
    (POST, W + "/stepup/options"): (ALLOWED, "a challenge for this session; the actions are a closed list"),
    (POST, "/api/stepup/options"): (ALLOWED, "a challenge for this session; the actions are a closed list"),
    (POST, "/api/stepup/verify"): (ALLOWED, "the assertion, held for this session only"),
    (GET, "/api/passkeys"): (ALLOWED, ""),
    (POST, "/api/passkeys/enroll-link"): (STEP_UP, "Add a device"),
    (DELETE, "/api/passkeys/{credential_id}"): (STEP_UP, "remove a passkey"),
    # --- settings and small preferences
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
    # --- the workspace name and icon (unsigned cosmetic settings, owner only; the host checks the role)
    (PUT, W + "/workspace/cosmetic"): (ALLOWED, "owner, plain click, no signature (run 13 D6)"),
    (POST, W + "/records/auto-on"): (REFUSED, "starts a timer on this computer: local only"),
    (GET, W + "/claude-trust"): (REFUSED, "what Claude Code may open is decided on this computer"),
    (POST, W + "/claude-trust"): (REFUSED, "what Claude Code may open is decided on this computer"),
    # --- add-ons and the marketplace (install is an admin action by plain click; running one needs execution on)
    (GET, W + "/market"): (ALLOWED, ""),
    (GET, W + "/market/installed"): (ALLOWED, ""),
    (POST, W + "/market/stage"): (ALLOWED, "admin action, plain click"),
    (DELETE, W + "/market/stage/{stage_id}"): (ALLOWED, ""),
    (POST, W + "/market/uninstall"): (ALLOWED, "turning off"),
    (POST, W + "/market/add"): (ALLOWED, "admin action, plain click"),
    (POST, W + "/market/refresh"): (ALLOWED, ""),
    # --- runs, agents, terminals
    (GET, W + "/runs"): (ALLOWED, ""),
    (POST, W + "/runs/{run}/stop"): (ALLOWED, "turning off"),
    (GET, W + "/runs/{run}/transcript"): (ALLOWED, "focus mode: the chat view of a run, read only"),
    (POST, W + "/runs/{run}/link"): (ALLOWED, "focus mode: attach a scratch session to a ticket"),
    (POST, W + "/runs/{run}/detach"): (ALLOWED, "focus mode: make a delegated session a separate one"),
    (GET, W + "/runs/{run}/sources"): (ALLOWED, "focus mode: what the agent read, text only, read only"),
    (GET, W + "/runs/{run}/sources/{source_id}"): (ALLOWED, "focus mode: the text the agent got for one source"),
    (GET, W + "/tickets/{key}/delegations"): (ALLOWED, "focus mode: the delegation tree of a ticket, read only"),
    (GET, W + "/tickets/{key}/worktree-diff"): (ALLOWED, "focus mode: live worktree diff, read only"),
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
    (POST, W + "/shares/register"): (REFUSED, "connecting to the publish server is decided on this computer"),
}
assert all(rule in RULES for rule, _ in TABLE.values())


# --- the `remote-device` column (R3, orch-core #1591; spec D89/D90) -------------------------------------------------
#
# A stream of a remote device is answered in-process (no listener). Its routes are a SECOND column beside `TABLE`:
# every route has a rule and the least access level that may use it, and a route that is not listed is refused.
# Enforced in ONE place, `Security.route_check`; `tests/test_remote_device_gate.py` walks the app's real routes.
#
# Rules: ALLOWED (reads, and plain writes that only turn something off or are small), PRESENCE (a fresh presence
# signature of the device covers this very request, D88), BY_KIND (`click/prepare`, `click/sign`, ...: the click signer
# decides from the signed op kind, `core click/remote_kinds.py`), REFUSED. Levels, least to most: view (reads only),
# work (writes and presence routes), terminals (shell on the server).
PRESENCE, BY_KIND = "presence", "by_kind"
DEVICE_RULES = (ALLOWED, PRESENCE, BY_KIND, REFUSED)
VIEW, WORK, TERMINAL_LEVEL = "view", "work", "terminals"
LEVELS = (VIEW, WORK, TERMINAL_LEVEL)  # in order: a higher level may do everything a lower one may
READ_METHODS = ("GET", "HEAD")


def _rows(rule, level, method, *templates):
    assert rule in DEVICE_RULES and level in LEVELS
    return {(method, t): (rule, level) for t in templates}


REMOTE_DEVICE: dict[tuple[str, str], tuple[str, str]] = {
    **_rows(
        ALLOWED,
        VIEW,
        GET,
        "/api/session",
        "/api/events",
        "/api/workspaces",
        "/api/host",
        "/api/me",
        W + "/head",
        W + "/identity",
        W + "/hub/rail",
        W + "/today",
        W + "/tickets",
        W + "/board",
        W + "/tickets/{key}",
        W + "/tickets/{key}/events",
        W + "/tickets/{key}/changes",
        W + "/tickets/{key}/artifacts/{name}",
        W + "/tickets/{key}/workdir",
        W + "/settings",
        W + "/members",
        W + "/readers",
        W + "/grants",
        W + "/versions",
        W + "/add-ons",
        W + "/add-ons/{name}/settings",
        W + "/quick",
        W + "/activity",
        W + "/usage",
        W + "/followups",
        W + "/schedules",
        W + "/worktrees",
        W + "/explorer",
        W + "/explorer/tree",
        W + "/explorer/file",
        W + "/explorer/names",
        W + "/explorer/search",
        W + "/explorer/history",
        W + "/explorer/commit",
        W + "/explorer/compare",
        W + "/explorer/ticket/{key}",
        W + "/addons",
        W + "/view/{name}",
        W + "/wiki",
        W + "/wiki/page",
        W + "/wiki/search",
        W + "/wiki/backlinks",
        W + "/widgets/catalog",
        W + "/model-routing",
        W + "/click/status",
        W + "/hub",
        W + "/hub/share",
        W + "/hub/share/audience",
        W + "/hub/shared",
        W + "/org",
        W + "/org/item",
        W + "/me/risk_notices",
        W + "/records",
        W + "/records/current",
        W + "/runs",
        W + "/runs/{run}/transcript",
        W + "/runs/{run}/sources",
        W + "/runs/{run}/sources/{source_id}",
        W + "/tickets/{key}/delegations",
        W + "/tickets/{key}/worktree-diff",
        W + "/runner",
        W + "/shares",
    ),
    **_rows(
        ALLOWED,
        WORK,
        POST,
        W + "/wiki/create",
        W + "/repos/check",
        W + "/click/images",
        W + "/hub/share/preview",
        W + "/hub/share/revoke",
        W + "/hub/share/rule-remove",
        W + "/me/risk_notices",
        W + "/records/commit",
        W + "/records/auto-off",
        W + "/runs/{run}/stop",
        W + "/agents/preview",
        W + "/runner/forget",
        W + "/worktrees/remove",
        W + "/workdir/delete",
        W + "/shares/revoke",
        W + "/shares/discard",
    ),
    **_rows(
        ALLOWED,
        WORK,
        PUT,
        W + "/workspace/cosmetic",
    ),
    **_rows(
        ALLOWED,
        VIEW,
        POST,
        W + "/remote/off",
    ),
    **_rows(
        PRESENCE,
        WORK,
        POST,
        W + "/runs/{run}/link",
        W + "/runs/{run}/detach",
        W + "/agents/start",
    ),
    **_rows(
        PRESENCE,
        TERMINAL_LEVEL,
        POST,
        W + "/runs/{run}/attach",
        W + "/terminals/open",
        W + "/terminals/preview",
    ),
    **_rows(
        BY_KIND,
        WORK,
        POST,
        W + "/click/prepare",
        W + "/click/view",
        W + "/click/state",
        W + "/click/sign",
        W + "/click/cancel",
    ),
    **_rows(
        REFUSED,
        VIEW,
        GET,
        "/{path:path}",
        "/auth/probe",
        "/api/trusted",
        "/api/setup/{route:path}",
        "/widget-frame/{rest:path}",
        W + "/click/github",
        W + "/hub/approve/{request_id}",
        W + "/remote",
        "/api/passkeys",
        W + "/phone/available",
        W + "/phone/status/{id}",
        W + "/claude-trust",
        W + "/market",
        W + "/market/installed",
        W + "/runner/deposits/{deposit_id}",
        "/api/testrun/{run}",
        "/api/testrun/{run}/previous",
        "/api/testrun/{run}/images/{name}",
        "/api/testrun/{run}/screenshots.zip",
    ),
    **_rows(
        REFUSED,
        VIEW,
        POST,
        "/auth/session",
        "/auth/logout",
        W + "/hub/switch",
        "/api/trusted/enrol",
        "/auth/switch/challenge",
        "/auth/switch",
        "/auth/passkey/options",
        "/auth/passkey/login",
        "/auth/enroll/options",
        "/auth/enroll/register",
        "/auth/setup",
        "/api/setup/{route:path}",
        W + "/click/custody/seal",
        W + "/click/rotate/seal",
        W + "/click/rotate",
        W + "/click/github/seal",
        W + "/click/github",
        W + "/click/github/remove",
        W + "/click/custody",
        W + "/click/runner/review",
        W + "/click/runner",
        W + "/hubsign",
        W + "/hub/approve/prepare",
        W + "/hub/share/rule-add",
        W + "/hub/share/sync",
        W + "/remote/code",
        W + "/remote/devices/{device_id}/remove",
        W + "/org/accept",
        W + "/org/decline",
        W + "/org/sync",
        W + "/org/tool",
        W + "/hub/link",
        W + "/hub/connect",
        W + "/hub/refresh",
        W + "/stepup/options",
        "/api/stepup/options",
        "/api/stepup/verify",
        "/api/passkeys/enroll-link",
        W + "/phone/request",
        W + "/phone/cancel",
        W + "/records/init",
        W + "/records/remote",
        W + "/records/push-initial",
        W + "/records/auto-on",
        W + "/claude-trust",
        W + "/market/stage",
        W + "/market/uninstall",
        W + "/market/add",
        W + "/market/refresh",
        "/api/testrun/{run}/steps/{step}/images",
        W + "/worktrees/create",
        W + "/shares/prepare",
        W + "/shares/send",
        W + "/shares/register",
    ),
    **_rows(
        REFUSED,
        VIEW,
        PUT,
        "/api/testrun/{run}/steps/{step}",
    ),
    **_rows(
        REFUSED,
        VIEW,
        DELETE,
        "/api/trusted/{kid}",
        "/api/passkeys/{credential_id}",
        W + "/market/stage/{stage_id}",
        "/api/testrun/{run}/images/{name}",
    ),
    **_rows(PRESENCE, TERMINAL_LEVEL, WEBSOCKET, "/term"),
    # R3 T4 adds these handlers: the in-stream presence routes (the device key signs, the signer checks; neither route
    # needs a batch itself), and the owner's End button (a loopback act, never from a stream).
    **_rows(ALLOWED, WORK, POST, W + "/remote/presence/nonce", W + "/remote/presence"),
    **_rows(REFUSED, VIEW, POST, W + "/remote/sessions/{stream_id}/end"),
}
#: Writes that stay open at the `view` level: turning remote access off needs no more than turning it on (D84).
VIEW_WRITES = frozenset({(POST, W + "/remote/off")})
assert all(
    rule == REFUSED or method in READ_METHODS or level != VIEW or (method, t) in VIEW_WRITES
    for (method, t), (rule, level) in REMOTE_DEVICE.items()
), "a write that the view level may call"


def rule_device(method: str, template: str, level: str) -> tuple[str, str | None]:
    """(rule, refusal) of a route for a stream of this access level: refusal is None when the level may use it,
    ``"remote.level"`` when only a higher level may and ``"remote.refused"`` when no level may (or the route is not
    listed, or the level is not one of ours). The ONE decision: `Security.route_check` asks nothing else."""
    row = REMOTE_DEVICE.get((method, template))
    if row is None or level not in LEVELS:
        return REFUSED, "remote.refused"
    rule, least = row
    if rule == REFUSED:
        return REFUSED, "remote.refused"
    if method not in READ_METHODS and (method, template) not in VIEW_WRITES:
        least = max(least, WORK, key=LEVELS.index)  # a write is never a view-level act
    return rule, None if LEVELS.index(level) >= LEVELS.index(least) else "remote.level"


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
