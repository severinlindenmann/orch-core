"""The hub web API: routes, session and CSRF rules, role checks on the hub's own data (V3-D13, orch-core#893).

Rules every request goes through, in this order (``WebApi.handle``):

1. the method and path must match a route in :data:`ROUTES` (else ``unknown_op``);
2. the ``Host`` header must be one of the configured hosts (DNS rebinding) when hosts are configured;
3. a state-changing method (anything but GET) needs the non-simple header ``X-Orch-Csrf: 1``, a JSON content type when
   there is a body, and, when an ``Origin`` header is present, an origin on the allow list. A request that authenticates
   with the session cookie (an ambient credential) MUST carry an allowed ``Origin``; a request that carries its token
   explicitly (``Authorization: Bearer``, the phone) has nothing ambient to forge. Refusal: ``not_allowed``;
4. the session: the token (cookie or bearer) must be a live ``Channels`` session of a user the access model still has
   (else ``no_session``) whose scope fits the carrier (cookie = ``full``, the browser; bearer = ``pairing`` or
   ``channel``, the phone) and the route (:data:`SCOPE_ROUTES`: a pairing session can only pair and bind, a channel
   session is bound to one channel and answers ``channel_revoked`` / ``cert_expired`` once that channel is; else
   ``forbidden_role``); admin routes then need ``Access.is_hub_admin`` (else ``forbidden_role``);
5. the body is strict JSON (``canon.loads_strict``), an object, at most :data:`MAX_BODY` bytes (else ``malformed``);
6. the handler. Workspace-level routes re-read the user's role and the workspace's mode from the hub's own data; a
   workspace the user has no part in is ``unknown_workspace`` (the same answer as one that does not exist).

Signing never happens here: ``/sign/prepare`` asks the custodial engine for the event the hub built, ``/sign/sign``
hands the CHANNEL's proof to the engine (D6), which checks it and signs with the user's UK. There is no route that takes
event bytes or a digest to sign. Workspace-derived text leaves through ``render_text`` (protocol.md §8.4 step 3); the
``ctx`` of a prepare is returned byte-exact (the channel recomputes the digest from it) next to an escaped ``shown``.
Answers are ``{"error": code}`` with a protocol.md §14 code; an unexpected failure is ``internal`` with no detail.
Nothing secret is logged: this module does not log at all.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import re
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .. import __version__ as hub_version
from ..access import Access, AccessError
from ..access.store import ADMIN_GROUP, check_name
from ..activity.feed import feed as activity_feed
from ..changes import ChangeError
from ..channels import Channels
from ..channels.service import MIN_PASSWORD, ChannelError
from ..connect import LinkError, build_link
from ..contract import channelapi as CA
from ..contract import market_policy as MP
from ..contract import phase4 as P4
from ..contract import suite as S
from ..contract.canon import CanonError, loads_strict, unb64u
from ..contract.hub import HV, Refused
from ..relay.client import RelayError
from ..switch import SwitchError, clean_return_to, workspace_rows
from ..versions import MIN_SUPPORTED_VERSION, workspace_status
from .changes_api import ChangeHandlers, confirmed
from .static import StaticSite

MAX_BODY = 64 * 1024
COOKIE = "orch_hub_session"
CSRF_HEADER = "x-orch-csrf"
PREPARE_LIMIT = (10, 60_000)  # per user: 10 prepares a minute (orch-hub#25 item 6)
SIGN_LIMIT = (20, 60_000)
SWITCH_PEEK_LIMIT = (30, 60_000)  # per source: peek is unauthenticated and names the ticket's person
SIGNIN_SOURCE_LIMIT = (20, 60_000)  # per network source (IPv6: per /64), counted before any password hashing
SIGNIN_NET_LIMIT = (200, 60_000)  # looser aggregate per IPv6 /48 and per IPv4 /24: one allocation cannot spray
# No global request bucket, by design: any hub-wide bucket is a lockout lever a few sources can hold. What bounds the
# load is the concurrency cap below (refuse, never queue) plus the per-source cap; per-user guessing is bounded in
# Channels.sign_in (per (user, source) and per user for "password right, TOTP wrong").
SIGNIN_CONCURRENCY = 4  # sign-ins hashing at the same moment (scrypt is 16 MiB and ~50 ms each)
MAX_PREPARED = 4096
_HEX32 = re.compile(r"[0-9a-f]{32}")
_ID = r"[0-9a-f]{32}"
_GRP = r"[a-z0-9][a-z0-9-]{0,39}"
#: a change id is a ULID, not 32 hex: the path accepts one alphanumeric segment of that size and the handler holds it to the
#: exact ULID shape (anything else is ``unknown_change``, as an id that does not exist)
_PARAM = {"change_id": r"[0-9A-Za-z]{26,32}"}

#: wire code -> HTTP status. Anything not listed is answered as ``internal`` (500), never echoed.
STATUS = {
    "malformed": 400,
    "bad_pairing_code": 400,
    "weak_password": 400,
    "reserved_group": 400,
    "wrong_mode": 409,
    "event_changed": 409,
    "exists": 409,
    "channel_exists": 409,
    "key_in_use": 409,
    "conflict": 409,
    "stale_snapshot": 409,
    "unknown_snapshot": 409,
    # access changes that wait for a workspace owner (changes/)
    "owner_protected": 409,
    "owner_unknown": 409,
    "last_admin": 409,
    "stale_change": 409,
    "not_pending": 409,
    "proposer_gone": 409,
    "expired": 410,
    "unknown_change": 404,
    "no_channel": 403,
    "no_session": 401,
    "bad_credentials": 401,
    "unauthorized": 403,
    "bad_channel_proof": 403,
    "forbidden_role": 403,
    "not_allowed": 403,
    "not_granted": 403,
    "channel_revoked": 403,
    "factor_mismatch": 403,
    "factor_too_weak": 403,
    "other_user": 403,
    "other_hub": 403,
    "not_custodial": 403,
    "unknown_op": 404,
    "unknown_workspace": 404,
    "not_left": 409,
    "unknown_item": 404,
    "unknown_user": 404,
    "unknown_group": 404,
    "unknown_channel": 404,
    "unknown_prepared": 404,
    "prepared_expired": 410,
    "challenge_invalid": 400,
    "too_large": 413,
    "rate_limited": 429,
    "too_many_channels": 429,
    "unavailable": 503,
    "internal": 500,
    "not_bound": 409,
    "bad_ticket": 403,
    "reauth_required": 403,
    # contract refusals a registration or a prepare/sign can surface (protocol.md §14)
    "bad_signature": 403,
    "cert_invalid": 403,
    "cert_expired": 403,
    "revoked": 403,
    "wrong_suite": 400,
    "other_workspace": 403,
    "stale_timestamp": 400,
    "replay": 409,
    "dev_refused": 403,
    "dev_mismatch": 403,
    "stale_op_cert": 409,
    "view_gap": 409,
    "view_fork": 409,
    "bad_wrap": 409,
    "bad_epoch": 409,
}
#: local codes of the services that have a §14 name on the wire
WIRE = {
    "forbidden": "forbidden_role",
    "store_error": "unavailable",
    "issue_failed": "internal",
    "bad_private_file": "unavailable",
}


class SharesReader(Protocol):
    """D9's ``ShareService.list_shared`` (orch-hub#23): the verified shared items of a selective workspace that this
    user may see (the owner alias only). The web API calls nothing else of the shares package."""

    def list_shared(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]: ...


class ViewReader(Protocol):
    """The needs-you read of a FULL-mode workspace from the hub's ``log_sync`` view (§10.7). The projection code is a
    later task; until it lands the API reports full workspaces as ``unavailable`` instead of guessing."""

    def needs_you(self, workspace_id: str, user_id: str) -> list[dict[str, Any]]: ...

    def item(self, workspace_id: str, user_id: str, item_id: str) -> dict[str, Any] | None: ...


class SessionBridge:
    """D6's ``SessionCheck`` over D7: a token is valid for ``(user, channel)`` when it is a live session of that user and
    the channel is that user's active, unrevoked channel."""

    def __init__(self, channels: Channels) -> None:
        self._ch = channels

    def valid(self, session: Any, user_id: str, channel_id: str) -> bool:
        try:
            info = self._ch.session_info(session)
            if info["scope"] == "pairing" or (info["scope"] == "channel" and info["channel_id"] != channel_id):
                return False  # a pairing session never signs; a channel session signs for its own channel only
            return info["user_id"] == user_id and self._ch.active_channel(user_id, channel_id) is not None
        except ChannelError:
            return False


@dataclass
class WebDeps:
    keys: Any  # HubKeys (new user ids come from it)
    access: Access
    channels: Channels
    registry: Any  # link.Registry (workspaces, modes, owner alias)
    audit: Any  # AuditLog (admin read)
    engine: Any = None  # CustodialEngine; None = signing routes answer ``unavailable``
    shares: SharesReader | None = None
    view: ViewReader | None = None
    #: exact origins the browser console is served from, e.g. ``{"http://127.0.0.1:8443"}``; also derives the allowed Hosts
    allowed_origins: frozenset[str] = frozenset()
    #: addresses of the edge that may state the client address (``X-Forwarded-For``, last entry); any other peer is its own source
    trusted_peers: frozenset[str] = frozenset()
    #: True when the API is served behind the edge (not the loopback dev server): then ``trusted_peers`` is required,
    #: or every request would have the edge as its source and the per-source caps would be one shared bucket
    behind_edge: bool = False
    secure_cookies: bool = True  # the loopback dev server over plain http passes False
    #: the hub's public API origin, from ``HubConfig`` (the same value family as ``allowed_origins``); signed into
    #: ``hub.api_origin`` for ``GET /hub/card``. None = the route answers ``unavailable``
    api_origin: str | None = None
    #: the notifier that holds the push targets and keys (None = no relay: ``PUT /channels/{id}/push`` is ``unavailable``)
    push: Any = None
    #: the invite gate (``mint``) and the hub link address, for "Connect a workspace" (None = route is ``unavailable``)
    invites: Any = None
    link_endpoint: Callable[[], str | None] | None = None
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000)
    #: one-time first-admin token (``orch install hub`` writes it, ``POST /setup`` consumes it); None = no setup route
    setup_token_file: Path | None = None
    #: switch tickets and the rail (``orch_hub.switch.SwitchService``); None = those routes answer ``unavailable``
    switch: Any = None
    #: relay/publish health for the Services page (``orch_hub.services.Services``); None = ``unavailable``
    services: Any = None
    #: the route queue (``recent``): hand-offs between workspaces, for the Workspaces page; None = an empty list
    routes: Any = None
    delivery: Any = None  # custodial DeliveryQueue; None = no pending deliveries
    activity: Any = None  # hub-local admin history; None = no local events
    changes: Any = (
        None  # orch_hub.changes.Changes: access changes that need the owner; None = those routes answer ``unavailable``
    )


@dataclass(frozen=True)
class Route:
    method: str
    path: str  # template, ``{name}`` = one path segment
    auth: str  # none | session | admin
    purpose: str
    handler: str
    rx: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        pat = re.sub(
            r"\{(\w+)\}",
            lambda m: f"(?P<{m[1]}>{_PARAM.get(m[1]) or (_ID if m[1].endswith('id') else _GRP)})",
            self.path,
        )
        object.__setattr__(self, "rx", re.compile(pat))


def _r(method: str, path: str, auth: str, handler: str, purpose: str) -> Route:
    return Route(method, "/api/v1" + path, auth, purpose, handler)


ROUTES: tuple[Route, ...] = (
    # (0) the public card (phone pairing, protocol §6.4)
    _r("GET", "/hub/card", "none", "hub_card", "The held card and the signed API origin (pairing; no session)"),
    _r("POST", "/setup", "none", "setup", "First admin: the one-time setup token creates the admin (password, TOTP)"),
    # (a) session
    _r("POST", "/session", "none", "sign_in", "Sign in with user id, password and TOTP; sets the session cookie"),
    _r("GET", "/session", "session", "whoami", "Who am I: user id, name, hub-admin flag"),
    _r("DELETE", "/session", "session", "sign_out", "Sign out (the session dies at once)"),
    _r(
        "POST",
        "/session/channel",
        "session",
        "bind",
        "A phone's channel key binds its pairing session to itself; the pairing session is consumed",
    ),
    # (b) person home
    _r(
        "GET",
        "/me/handoffs",
        "session",
        "my_handoffs",
        "Tickets sent between my workspaces: who, when, delivered or waiting",
    ),
    _r("GET", "/workspaces/{workspace_id}/roles", "session", "workspace_roles", "Workspace owner and group roles"),
    _r("GET", "/me/workspaces", "session", "my_workspaces", "My workspaces with my role and the workspace mode"),
    _r(
        "GET",
        "/me/needs-you",
        "session",
        "needs_you",
        "Needs-you items (full: workspace view; selective: shared items)",
    ),
    _r(
        "GET", "/workspaces/{workspace_id}/items/{item_id}", "session", "item", "One ticket, question or needs-you item"
    ),
    _r("GET", "/me/channels", "session", "my_channels", "My registered channels (phone, browser)"),
    _r(
        "GET",
        "/me/changes",
        "session",
        "my_changes",
        "Access changes waiting for my approval, and overrides on my workspaces (session person only)",
    ),
    _r(
        "POST",
        "/me/changes/{change_id}/approve/prepare",
        "session",
        "change_prepare",
        "What I would sign to approve an access change: ctx and digest (owner of the workspace only)",
    ),
    _r(
        "POST",
        "/me/changes/{change_id}/approve",
        "session",
        "change_approve",
        "Approve an access change with my channel key's proof over its digest (single use)",
    ),
    _r(
        "POST",
        "/me/changes/{change_id}/reject",
        "session",
        "change_reject",
        "Say no to an access change (session and a registered channel)",
    ),
    # (c) signing
    _r("POST", "/sign/prepare", "session", "prepare", "Hub builds the event; returns ctx, digest and what to show"),
    _r(
        "GET",
        "/sign/prepared/{prepared_id}",
        "session",
        "prepared",
        "A request I prepared: what to show and the digest",
    ),
    _r("POST", "/sign/sign", "session", "sign", "Hand the channel's proof to the hub, which signs with my UK"),
    # (e) channel registration
    _r("POST", "/channels/register/begin", "session", "reg_begin", "Open a channel registration challenge for one key"),
    _r(
        "POST",
        "/channels/register/complete",
        "session",
        "reg_complete",
        "Return the channel's signed proof, get the cert",
    ),
    _r("POST", "/channels/{channel_id}/revoke", "session", "revoke", "Revoke one of my channels (permanent)"),
    _r(
        "PUT",
        "/channels/{channel_id}/push",
        "session",
        "push",
        "Register the phone's APNs target and push key (channel session of that channel)",
    ),
    # (d) admin console
    _r("GET", "/admin/users", "admin", "a_users", "List users"),
    _r("POST", "/admin/users", "admin", "a_user_add", "Create a user (new id and signing key)"),
    _r("DELETE", "/admin/users/{user_id}", "admin", "a_user_remove", "Remove a user"),
    _r("GET", "/admin/groups", "admin", "a_groups", "List groups with members and grants"),
    _r("POST", "/admin/groups", "admin", "a_group_add", "Create a group"),
    _r("DELETE", "/admin/groups/{group}", "admin", "a_group_remove", "Remove a group"),
    _r("PUT", "/admin/groups/{group}/members/{user_id}", "admin", "a_member_add", "Add a user to a group"),
    _r("DELETE", "/admin/groups/{group}/members/{user_id}", "admin", "a_member_remove", "Remove a user from a group"),
    _r("GET", "/admin/grants", "admin", "a_grants", "List workspace grants (optional ?workspace_id=)"),
    _r("PUT", "/admin/grants", "admin", "a_grant", "Grant a group a role in a workspace"),
    _r("POST", "/admin/grants/revoke", "admin", "a_grant_revoke", "Remove a group's grant on a workspace"),
    _r(
        "DELETE",
        "/admin/workspaces/{workspace_id}",
        "admin",
        "a_workspace_remove",
        "Remove a workspace that has left (deletes the hub's record)",
    ),
    _r("GET", "/admin/workspaces", "admin", "a_workspaces", "List workspaces with mode, binding status and grants"),
    _r(
        "GET",
        "/admin/services",
        "admin",
        "a_services",
        "Hub, relay and publish: state, version, last answer (run-11 #12)",
    ),
    _r("GET", "/admin/channels", "admin", "a_channels", "List every channel (optional ?user_id=)"),
    _r("POST", "/admin/channels/{channel_id}/revoke", "admin", "revoke", "Revoke any channel (permanent)"),
    _r("GET", "/admin/market-policy", "admin", "a_market_policy", "Read the hub marketplace policy (D14)"),
    _r(
        "PUT",
        "/admin/market-policy",
        "admin",
        "a_market_policy_set",
        "Replace the hub marketplace policy ({base_version, policy}); 409 conflict when base_version is stale",
    ),
    _r(
        "GET", "/admin/changes", "admin", "a_changes", "Access changes waiting for an owner, and recently finished ones"
    ),
    _r(
        "POST",
        "/admin/changes/{change_id}/override",
        "admin",
        "a_change_override",
        "Apply a waiting access change without its owner ({reason}, 3-200 characters); audited, the owner is told",
    ),
    _r(
        "POST",
        "/admin/changes/{change_id}/cancel",
        "admin",
        "a_change_cancel",
        "Withdraw a waiting access change (its proposer or a hub admin)",
    ),
    _r("POST", "/admin/connect", "admin", "a_connect", "Mint a workspace invite and return the one connect link"),
    _r("POST", "/switch/ticket", "session", "switch_ticket", "A one-time ticket to open one of my workspaces"),
    _r(
        "POST",
        "/switch/peek",
        "none",
        "switch_peek",
        "Look at a ticket from a workspace without using it (who, and who is signed in here)",
    ),
    _r("POST", "/switch/redeem", "none", "switch_redeem", "Redeem a ticket from a workspace: signs me in here"),
    _r("GET", "/rail", "session", "rail", "The hub and my workspaces, for the switcher"),
    _r("GET", "/admin/activity", "admin", "a_activity", "Read company activity"),
    _r("GET", "/admin/audit", "admin", "a_audit", "Read the audit chain (?since=N, up to 100 records)"),
)
#: handler -> the session scopes that may call it (protocol §6.4). ``full`` is the browser's cookie session; ``pairing``
#: (password + TOTP from the app) may only read the card, register a phone, bind and sign out; ``channel`` is bound to
#: one channel and never registers a channel or reaches an admin route. A route not listed here is ``full`` only.
_PAIR = frozenset({"sign_out", "reg_begin", "reg_complete", "bind"})
_CHAN = frozenset(
    {
        "whoami",
        "sign_out",
        "my_workspaces",
        "needs_you",
        "item",
        "my_channels",
        "prepare",
        "prepared",
        "sign",
        "revoke",
        "push",
    }
)
_FULL_NOT = frozenset({"bind", "push"})  # channel-session routes the browser cookie never uses
SCOPE_ROUTES: dict[str, frozenset[str]] = {
    h: frozenset(
        ({"full"} if h not in _FULL_NOT else set())
        | ({"pairing"} if h in _PAIR else set())
        | ({"channel"} if h in _CHAN else set())
    )
    for h in {rt.handler for rt in ROUTES}
}


#: What a session that came from a switch ticket (``via_switch``: a workspace vouched for it, no password sign-in) may
#: call: reading what it sees, the rail, opening another workspace, signing out. Every OTHER route (admin, channels,
#: signing, credentials) answers ``reauth_required`` until a real sign-in: routes default closed.
SWITCH_HANDLERS = frozenset(
    {
        "whoami",
        "sign_out",
        "my_workspaces",
        "my_handoffs",
        "needs_you",
        "item",
        "switch_ticket",
        "rail",
    }
)
#: E4: routes only the console PAGE may call (its cookie session, from its own origin). The console's own session sent
#: as a bearer is ``not_allowed``; an unknown bearer stays ``no_session`` and an app (pairing/channel) one
#: ``forbidden_role``, as on every route.
BROWSER_ONLY = frozenset({"switch_ticket"})


def route_scopes(rt: Route) -> frozenset[str]:
    """The session scopes that may call this ROUTE (keyed by route, not handler: ``/admin/channels/{id}/revoke`` shares the
    ``revoke`` handler with the channel's own revoke, but an admin route is ``full`` only, L2H1-1)."""
    return frozenset({"full"}) if rt.auth == "admin" else SCOPE_ROUTES.get(rt.handler, frozenset())


_BY_METHOD: dict[str, list[Route]] = {}
for _rt in ROUTES:
    _BY_METHOD.setdefault(_rt.method, []).append(_rt)


@dataclass
class Ctx:
    user_id: str = ""
    token: str = ""
    cookie: bool = False
    scope: str = ""  # full | pairing | channel
    channel_id: str | None = None  # the channel a ``channel`` session is bound to
    origin: str | None = None
    source: str = ""
    via_switch: bool = False  # the session came from a switch ticket, not a password sign-in
    cookie_value: str = ""  # the session cookie the browser sent, if any (auth-none routes may use it, never trust it)
    fetch_site: str | None = None  # the browser's Sec-Fetch-Site label, when it sent one


class _Limiter:
    def __init__(self, now: Callable[[], int]) -> None:
        self._now, self._hits, self._lock = now, {}, threading.Lock()  # type: ignore[var-annotated]

    def hit(self, kind: str, user: str, limit: tuple[int, int]) -> None:
        n, window = limit
        now = self._now()
        with self._lock:
            q = self._hits.setdefault((kind, user), deque())
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= n:
                raise ApiError("rate_limited")
            q.append(now)
            if len(self._hits) > 10_000:  # memory guard: drop the settled keys
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - window]:
                    del self._hits[k]


def _norm_ip(addr: str) -> str:
    """One source per network that a host can freely pick addresses in: an IPv6 /64 is one source."""
    ip = ipaddress.ip_address(addr)  # ValueError for "" and anything that is not an address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class ApiError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _render(x: Any) -> Any:
    """Every string that comes from a workspace goes out through the contract's ``render_text`` (L2P4-2d)."""
    if type(x) is str:
        return P4.render_text(x)
    if type(x) is list:
        return [_render(i) for i in x]
    if type(x) is dict:
        return {(P4.render_text(k) if type(k) is str else k): _render(v) for k, v in x.items()}
    return x


def _item_content(content: dict[str, Any]) -> dict[str, Any]:
    """Item content out through ``render_text``, except a verified question's ``text``, ``options`` and ``spec``: those go
    VERBATIM (a client hashes exactly what it receives; the hub only marks a question verified when it holds no character
    ``render_text`` would escape, apart from a tab in a multi-line field, which escaping would turn into ``<U+0009>``)."""
    if "spec" not in content or content.get("unverifiable") is True:  # the explicit flag, never the content's shape
        return _render(content)
    raw = ("text", "options", "spec")
    return {
        **_render({k: v for k, v in content.items() if k not in raw}),
        **{k: content[k] for k in raw if k in content},
    }


def _short(h: Any) -> str:
    """``sha256:3d3f5e93357c...`` as ``3d3f 5e93 357c`` (protocol 8.4: the one form; "" when not a hash)."""
    try:
        return P4.short_hash(h)
    except Refused:
        return ""


def _facts(ev: dict[str, Any]) -> list[list[str]]:
    """D15b-2 (protocol 8.4 step 3): the projection-derived values of a decision or a status move, for ``shown`` (the
    channel recomputes them from ``ctx``). Gate, short hash, ``gate_gen``, each source ``repo`` with its short ``sha``;
    "from -> to" for a move."""
    typ = ev.get("type")
    if typ in ("gate.approved", "gate.changes_requested", "verdict.given"):
        out = [
            ["Gate", "verify" if typ == "verdict.given" else str(ev.get("gate"))],
            ["Content reference", _short(ev.get("hash")) or str(ev.get("hash"))],
            ["Generation", str(ev.get("gate_gen"))],
        ]
        for e in ev.get("source_sha") or []:
            out.append(["Code", f"{e.get('repo')} {str(e.get('sha'))[:8]}" if type(e) is dict else str(e)])
        return out
    if typ in ("status.changed", "ticket.moved"):
        return [["Status", f"{ev.get('from')} \u2192 {ev.get('to')}"]]
    return []


def _need(body: dict[str, Any] | None, **types: type) -> dict[str, Any]:
    if body is None:
        raise ApiError("malformed")
    for k, t in types.items():
        if type(body.get(k)) is not t:
            raise ApiError("malformed")
    return body


def _hex32(x: Any) -> str:
    if type(x) is not str or not _HEX32.fullmatch(x):
        raise ApiError("malformed")
    return x


class WebApi(ChangeHandlers):
    def __init__(self, deps: WebDeps) -> None:
        if deps.behind_edge and not deps.trusted_peers:
            raise ValueError("web API behind the edge needs trusted_peers (the edge's addresses)")
        self.d = deps
        self._limit = _Limiter(deps.now_ms)
        self._prepared: dict[
            str, tuple[str, str, str, str, int, Any]
        ] = {}  # id -> user, channel, workspace, log, expires, Prepared
        self._plock = threading.Lock()
        self._origins: dict[tuple[str, str], dict[str, Any]] = {}
        self._hashing = threading.BoundedSemaphore(SIGNIN_CONCURRENCY)
        self._busy: set[str] = set()  # sources holding a hashing slot right now (max 1 each), under _plock
        self._hosts = frozenset(urllib.parse.urlsplit(o).netloc for o in deps.allowed_origins)
        self._site = StaticSite(hsts=deps.secure_cookies)

    def allow_origin(self, origin: str) -> None:
        """Add one exact origin before the server starts (the loopback dev server learns its port only after it
        binds). Origins and the Hosts derived from them are replaced as whole values, never edited in place."""
        self.d.allowed_origins = self.d.allowed_origins | {origin}
        self._hosts = frozenset(urllib.parse.urlsplit(o).netloc for o in self.d.allowed_origins)

    def allow_loopback_origin(self, origin: str) -> None:
        """The loopback dev server: its own bound address is both an allowed origin and the API origin it signs."""
        self.allow_origin(origin)
        self.d.api_origin = self.d.api_origin or origin

    # --- the request pipeline ----------------------------------------------------------------------------------------

    def error(self, status: int, code: str) -> tuple[int, list[tuple[str, str]], bytes]:
        return self._out(status, {"error": code}, [])

    def _out(self, status: int, obj: Any, extra: list[tuple[str, str]]) -> tuple[int, list[tuple[str, str]], bytes]:
        h = [
            ("Content-Type", "application/json"),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            *extra,
        ]
        return status, h, json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()

    def _source(self, headers: dict[str, str], peer: str) -> str:
        """The client address: the socket peer, or, only when that peer is the configured edge, the rightmost
        ``X-Forwarded-For`` entry that is not itself a trusted proxy (so a chain of trusted proxies works; entries
        left of the first untrusted one are client-controlled and never read). An IPv6 source is its /64 and an
        IPv4-mapped one its IPv4. Anything that is not an IP address is the peer."""
        if peer in self.d.trusted_peers:
            for entry in reversed(headers.get("x-forwarded-for", "").split(",")):
                entry = entry.strip()
                if entry in self.d.trusted_peers:
                    continue
                try:
                    return _norm_ip(entry)
                except ValueError:
                    break
        try:
            return _norm_ip(peer)
        except ValueError:
            return peer or "unknown"  # a source is never empty

    def handle(
        self, method: str, target: str, headers: dict[str, str], body: bytes, peer: str = ""
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        try:
            path = urllib.parse.urlsplit(target).path
            if not path.startswith("/api/"):
                return self._static(method, path, headers)
            return self._handle(method, target, headers, body, self._source(headers, peer))
        except ApiError as e:
            return self._fail(e.code)
        except (ChannelError, AccessError, Refused, ChangeError) as e:
            return self._fail(WIRE.get(e.code, e.code))
        except Exception:  # noqa: BLE001 - unexpected: no detail leaves the process
            return self._fail("internal")

    def _static(self, method: str, path: str, headers: dict[str, str]) -> tuple[int, list[tuple[str, str]], bytes]:
        """The console's own files (I7): GET/HEAD of an exact allow-listed path through the same Host check as the API;
        every other method or path is the JSON ``unknown_op`` (the origin serves nothing else)."""
        if self._hosts and headers.get("host", "") not in self._hosts:
            raise ApiError("not_allowed")
        hit = self._site.get(path) if method in ("GET", "HEAD") else None
        if hit is None:
            raise ApiError("unknown_op")
        return hit

    def _fail(self, code: str) -> tuple[int, list[tuple[str, str]], bytes]:
        if code not in STATUS:
            code = "internal"
        return self.error(STATUS[code], code)

    def _handle(
        self, method: str, target: str, headers: dict[str, str], raw: bytes, source: str = ""
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        u = urllib.parse.urlsplit(target)
        route, params = None, {}
        for rt in _BY_METHOD.get(method, ()):
            m = rt.rx.fullmatch(u.path)
            if m:
                route, params = rt, m.groupdict()
                break
        if route is None:
            raise ApiError("unknown_op")
        if self._hosts and headers.get("host", "") not in self._hosts:
            raise ApiError("not_allowed")
        cookie_token = self._cookie(headers.get("cookie", ""), self._cookie_name)
        auth = headers.get("authorization", "")
        bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if method != "GET":
            self._csrf(headers, bool(raw), use_cookie=not bearer and bool(cookie_token))
        ctx = Ctx(
            origin=headers.get("origin"),
            source=source,
            cookie_value=cookie_token,
            fetch_site=headers.get("sec-fetch-site"),
        )
        if route.auth != "none":
            ctx.token, ctx.cookie = (bearer, False) if bearer else (cookie_token, True)
            if not ctx.token:
                raise ApiError("no_session")
            info = self.d.channels.session_info(ctx.token)
            ctx.user_id, ctx.scope, ctx.channel_id = info["user_id"], info["scope"], info["channel_id"]
            ctx.via_switch = info["via"] == "switch"
            # a session is for the carrier it was issued for (cookie = browser: ``full``; bearer = app: ``pairing`` or
            # ``channel``); the scope is stored with the session, so it also holds across a restart
            if (ctx.scope == "full") != ctx.cookie:
                if ctx.scope == "full" and route.handler in BROWSER_ONLY:
                    raise ApiError("not_allowed")  # the console's session lifted into a script as a bearer: never mints
                raise ApiError("no_session")
            if ctx.scope not in route_scopes(route):
                raise ApiError("forbidden_role")
            if ctx.scope == "channel":  # every request re-checks the channel (revocation takes effect at once)
                state = self.d.channels.channel_state(ctx.user_id, ctx.channel_id or "")
                if state == "revoked":
                    raise ApiError("channel_revoked")
                if state == "expired":
                    raise ApiError("cert_expired")
                if state != "active":
                    raise ApiError("no_session")
            if ctx.via_switch and route.handler not in SWITCH_HANDLERS:  # a workspace vouched for this session
                raise ApiError("reauth_required")
            if route.auth == "admin" and not self.d.access.is_hub_admin(ctx.user_id):
                raise ApiError("forbidden_role")
        body = self._body(raw)
        try:
            q = urllib.parse.parse_qs(u.query, keep_blank_values=True, strict_parsing=bool(u.query), max_num_fields=8)
        except ValueError:
            raise ApiError("malformed") from None
        status, obj, *extra = getattr(self, "_h_" + route.handler)(ctx, params, {k: v[-1] for k, v in q.items()}, body)
        return self._out(status, obj, extra[0] if extra else [])

    @property
    def _cookie_name(self) -> str:
        """``__Host-`` (Secure, Path=/, no Domain) whenever the cookie is Secure: a sibling subdomain cannot set or shadow
        it. Only the loopback dev server over plain http uses the bare name."""
        return ("__Host-" if self.d.secure_cookies else "") + COOKIE

    @staticmethod
    def _cookie(header: str, name: str) -> str:
        for part in header.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _csrf(self, headers: dict[str, str], has_body: bool, *, use_cookie: bool) -> None:
        """Non-simple header always; Origin on the allow list whenever present; and REQUIRED when the request relies on
        the ambient cookie (or sets it: a browser sign-in)."""
        if headers.get(CSRF_HEADER) != "1":
            raise ApiError("not_allowed")
        if has_body and headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ApiError("not_allowed")
        origin = headers.get("origin")
        if origin is not None:
            if origin not in self.d.allowed_origins:
                raise ApiError("not_allowed")
        elif use_cookie:  # an ambient credential without a browser-supplied origin: refuse
            raise ApiError("not_allowed")

    @staticmethod
    def _body(raw: bytes) -> dict[str, Any] | None:
        if not raw:
            return None
        if len(raw) > MAX_BODY:
            raise ApiError("too_large")
        try:
            obj = loads_strict(raw)
        except CanonError, RecursionError, ValueError:
            raise ApiError("malformed") from None
        if type(obj) is not dict:
            raise ApiError("malformed")
        return obj

    # --- the hub's own data: what this user may see ------------------------------------------------------------------

    def _workspaces(self, user: str) -> list[dict[str, Any]]:
        rows = workspace_rows(self.d.registry, self.d.access, user)
        for row in rows:
            row["last_seen"] = self._last_seen(row["workspace_id"])
        return rows

    def _last_seen(self, workspace_id: str):
        """Last contact as epoch seconds, or None when unknown."""
        # Older registries do not retain a last-seen time. Never invent one.
        state = self.d.registry.binding_state(workspace_id) or {}
        return state.get("last_seen")

    def _mine(self, user: str, ws: str) -> dict[str, Any]:
        for w in self._workspaces(user):
            if w["workspace_id"] == ws:
                return w
        raise ApiError("unknown_workspace")  # not a member and not existing look the same

    # --- (a) session -------------------------------------------------------------------------------------------------

    def _guarded_credentials(self, c: Ctx, user_id: str | Callable[[], list[str]], check: Callable[[], Any]) -> Any:
        """Every password + TOTP check (sign-in and the browser registration re-entry) goes through these caps."""
        # caps BEFORE any hashing: per source, and the sign-ins hashing right now (never queue: refuse)
        self._limit.hit("signin-src", c.source, SIGNIN_SOURCE_LIMIT)
        # the aggregate (IPv6 /48, IPv4 /24): a neighbour cannot exhaust our bucket, one allocation cannot spray; a
        # source that signed in as this user is exempt (cheap indexed read, no hashing), so neighbours cannot lock it out
        ids = [user_id] if isinstance(user_id, str) else user_id()  # the only lookup, after the source cap
        # the exemption only for exactly one real user that knows this source: 0 or 2+ candidates always count
        if not (len(ids) == 1 and self.d.channels.known_source(ids[0], c.source)):
            try:
                net = ipaddress.ip_network(c.source, strict=False)
                net = net.supernet(new_prefix=48 if net.version == 6 else 24)
            except ValueError:  # "unknown" (no usable peer address) has no aggregate
                net = None
            if net is not None:
                self._limit.hit("signin-net", str(net), SIGNIN_NET_LIMIT)
        # at most one hashing slot per source, so one source cannot hold all of them
        with self._plock:
            if c.source in self._busy:
                raise ApiError("rate_limited")
            self._busy.add(c.source)
        try:
            if not self._hashing.acquire(blocking=False):
                raise ApiError("rate_limited")
            try:
                result = check()
            finally:
                self._hashing.release()
        finally:
            with self._plock:
                self._busy.discard(c.source)
        return result

    def _candidates(self, who: str) -> list[str]:
        """Sign-in takes the user id or the name chosen at setup (run-11 #10). A name nobody has yields a placeholder id,
        so the password hash still runs and the answer is the same ``bad_credentials`` as a wrong password. Two users
        with one name are both tried: the one whose password and code match signs in, nobody learns the name is shared."""
        who = who.strip()
        if _HEX32.fullmatch(who):
            return [who]
        hits = [
            u["user_id"] for u in self.d.access.users() if u["name"] is not None and u["name"].lower() == who.lower()
        ]
        return hits or ["0" * 32]

    def _h_sign_in(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, user_id=str, password=str, totp=str)
        client = b.get("client", "browser")
        if client not in ("browser", "app"):
            raise ApiError("malformed")
        if client == "browser" and c.origin is None:  # a cookie is only ever set for a request the browser labelled
            raise ApiError("not_allowed")
        who = b["user_id"].strip()[:64]
        cands: list[str] = []
        scope = "full" if client == "browser" else "pairing"

        def attempt() -> Any:  # run inside the caps: they are charged before any lookup
            failure: ChannelError | None = None
            for uid in cands:  # filled by the guard (once, after the source cap)
                try:
                    return self.d.channels.sign_in(uid, b["password"], b["totp"], c.source, scope=scope)
                except ChannelError as e:
                    if e.code != "bad_credentials":
                        raise
                    failure = e
            raise failure  # type: ignore[misc]  # _candidates never returns an empty list

        def resolve() -> list[str]:
            cands[:] = self._candidates(who)
            return cands

        s = self._guarded_credentials(c, resolve, attempt)
        out: dict[str, Any] = {"user_id": s["user_id"], "expires_ms": s["expires_ms"]}
        if client == "app":  # the phone keeps the token itself and sends it as a bearer
            out["token"] = s["token"]
            return 200, out, []
        ttl = max(0, (s["expires_ms"] - self.d.now_ms()) // 1000)
        attrs = f"{self._cookie_name}={s['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ttl}"
        return 200, out, [("Set-Cookie", attrs + ("; Secure" if self.d.secure_cookies else ""))]

    def _h_setup(self, c: Ctx, p: dict, q: dict, b: dict | None):
        """Prototype: the installer's token file is the proof; a right token makes one hub-admin and is deleted."""
        b = _need(b, token=str, name=str, password=str)
        if self.d.access.members(ADMIN_GROUP):
            raise ApiError("not_allowed")  # first admin only: a leftover token file never makes a second one
        f = self.d.setup_token_file
        with self._plock:
            try:
                want = f.read_text().strip() if f is not None else ""
            except OSError:
                want = ""
            if not want or not hmac.compare_digest(want.encode(), b["token"].encode()):
                raise ApiError("bad_credentials")
            check_name(b["name"])
            if len(b["password"]) < MIN_PASSWORD:
                raise ChannelError("weak_password", f"at least {MIN_PASSWORD} characters")
            uid = self.d.keys.issue_uk(None)["o"]["user_id"]
            self.d.access.add_user(uid, b["name"])
            self.d.access.add_member(ADMIN_GROUP, uid)
            self.d.channels.set_password(uid, b["password"])
            t = self.d.channels.new_totp(uid)
            f.unlink(missing_ok=True)  # single use
        return 200, {"user_id": uid, "totp_secret": t["secret"], "totp_uri": t["uri"]}

    def _h_hub_card(self, c: Ctx, p: dict, q: dict, b: dict | None):
        """Public: the held card and the ``hub.api_origin`` that names this hub's own API origin (§6.4, L2B-1)."""
        return 200, {"card": self.d.keys.card(), "api_origin": self._api_origin()}

    def _api_origin(self) -> dict[str, Any]:
        """Signed with the current HK_op from ``WebDeps.api_origin`` (HubConfig), never from a request. One object per
        (op cert, origin): reissued with every new HK_op (a new origin needs a new HK_op and a revoked old op cert)."""
        origin = self.d.api_origin
        if origin is None:
            raise ApiError("unavailable")
        cert_id = self.d.keys.op_cert()["o"]["cert_id"]
        with self._plock:
            hit = self._origins.get((cert_id, origin))
            if hit is not None:
                return hit
        dev = self.d.keys.card()["o"].get("dev")
        if type(dev) is not bool:
            raise ApiError("unavailable")
        if not self.d.channels.pin_api_origin(cert_id, origin):
            raise ApiError("unavailable")  # another origin under the same HK_op: rotate HK_op and revoke the old cert
        signed = self.d.keys.op_signer.sign_object(
            {
                "v": 2,
                "suite": S.SUITE_ID,
                "kind": "hub.api_origin",
                "hv": HV,
                "hub_id": self.d.keys.hub_id,
                "op_cert_id": cert_id,
                "origin": origin,
                "issued_ms": self.d.now_ms(),
                "dev": dev,
            }
        )
        with self._plock:
            self._origins = {k: v for k, v in self._origins.items() if k[0] == cert_id}
            self._origins[(cert_id, origin)] = signed
        return signed

    def _h_bind(self, c: Ctx, p: dict, q: dict, b: dict | None):
        """The channel key binds this pairing session to itself (§6.4); the pairing session is consumed atomically."""
        b = _need(b, auth=dict)
        got = self.d.channels.bind_session(c.token, b["auth"])
        return 200, {**got, "card": self.d.keys.card()}

    def _h_push(self, c: Ctx, p: dict, q: dict, b: dict | None):
        """Register the phone's APNs target and its push key (§6.4). The key arrives as a ``hub.channel_push`` signed by
        the channel key (``verify_channel_push``, newer than the held one); ``null`` keeps the held key."""
        b = _need(b, env=str, topic=str, token=str)
        if p["channel_id"] != c.channel_id:
            raise ApiError("other_user")
        if self.d.push is None:
            raise ApiError("unavailable")
        cert = self.d.channels.active_channel(c.user_id, p["channel_id"])
        if cert is None:
            raise ApiError("channel_revoked")
        try:
            token = unb64u(b["token"], None)
        except CanonError, TypeError:
            raise ApiError("malformed") from None
        pk, issued = None, None
        if "push_key" not in b:
            raise ApiError("malformed")
        if b["push_key"] is not None:
            o = CA.verify_channel_push(
                b["push_key"],
                hub_id=self.d.keys.hub_id,
                channel_id=p["channel_id"],
                key_pub=unb64u(cert["o"]["key_pub"], 65),
                now_ms=self.d.now_ms(),
                dev_mode=bool(self.d.keys.card()["o"]["dev"]),
                held_issued_ms=self.d.push.held_push_issued_ms(p["channel_id"]),
            )
            pk, issued = unb64u(o["push_pub"], 65), o["issued_ms"]
        elif self.d.push.held_push_issued_ms(p["channel_id"]) is None:
            raise ApiError("malformed")  # nothing to keep
        try:
            self.d.push.register_target(c.user_id, p["channel_id"], b["env"], b["topic"], token, pk, issued)
        except RelayError as e:
            raise ApiError(e.code if e.code in ("malformed", "replay", "unknown_channel") else "unavailable") from None
        return 200, {"registered": True}

    def _h_whoami(self, c: Ctx, p: dict, q: dict, b: dict | None):
        name = next((u["name"] for u in self.d.access.users() if u["user_id"] == c.user_id), None)
        return (
            200,
            {
                "user_id": c.user_id,
                "name": name,
                "is_admin": self.d.access.is_hub_admin(c.user_id),
                "needs_sign_in": c.via_switch,  # admin pages need a password sign-in (a workspace vouched for this one)
            },
            [],
        )

    def _h_sign_out(self, c: Ctx, p: dict, q: dict, b: dict | None):
        self.d.channels.sign_out(c.token)
        clear = f"{self._cookie_name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        return 200, {"ok": True}, [("Set-Cookie", clear + ("; Secure" if self.d.secure_cookies else ""))]

    # --- hub one-app: switch tickets and the rail ---------------------------------------------------------------------

    def _switch(self) -> Any:
        if self.d.switch is None:
            raise ApiError("unavailable")
        return self.d.switch

    def _h_switch_ticket(self, c: Ctx, p: dict, q: dict, b: dict | None):
        """E4 invariant: a ticket is minted only on an authenticated POST from the console's own origin (the route has no
        GET). The browser's cookie session, an allowed ``Origin`` and, when the browser labels it, ``Sec-Fetch-Site:
        same-origin``: a cross-site page cannot make the hub mint a ticket and land the person silently."""
        if not c.cookie or c.origin not in self.d.allowed_origins or c.fetch_site not in (None, "same-origin"):
            raise ApiError("not_allowed")
        b = _need(b, workspace_id=str, return_to=str, kind=str)
        sw = self._switch()
        ws = _hex32(b["workspace_id"])
        if b["kind"] not in ("open", "signout"):
            raise ApiError("malformed")
        if not any(w["workspace_id"] == ws for w in self._workspaces(c.user_id)):
            raise ApiError("forbidden_role")
        origin = sw.origin_of(ws)
        if origin is None:
            raise ApiError("unavailable")
        return 200, {"url": f"{origin}/#t={sw.issue_to_ws(c.user_id, ws, b['return_to'], b['kind'])}"}, []

    def _session_name(self, token: str) -> dict[str, Any] | None:
        """Who the presented cookie is, or None (no cookie, dead session)."""
        if not token:
            return None
        try:
            uid = self.d.channels.session_info(token)["user_id"]
        except ChannelError:
            return None
        return {"user_id": uid, "name": self._user_name(uid)}

    def _user_name(self, uid: str) -> str | None:
        return next((u["name"] for u in self.d.access.users() if u["user_id"] == uid), None)

    def _h_switch_peek(self, c: Ctx, p: dict, q: dict, b: dict | None):
        """The landing page asks before it signs anybody in (login CSRF): who the ticket is for and who, if anyone,
        is signed in here already. The ticket is not used."""
        b = _need(b, t=str)
        self._limit.hit("switch-peek", c.source, SWITCH_PEEK_LIMIT)
        try:
            r = self._switch().peek(b["t"], "to_hub")
        except SwitchError:
            raise ApiError("bad_ticket") from None
        return (
            200,
            {
                "kind": r["kind"],
                "user_id": r["user"],
                "name": self._user_name(r["user"]),
                "signed_in_as": self._session_name(c.cookie_value),
            },
            [],
        )

    def _h_switch_redeem(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, t=str)
        if c.origin is None:  # a cookie is only ever set for a request the browser labelled
            raise ApiError("not_allowed")
        replace = b.get("replace", False)
        if type(replace) is not bool:
            raise ApiError("malformed")
        sw = self._switch()
        try:
            want = sw.peek(b["t"], "to_hub")
        except SwitchError:
            raise ApiError("bad_ticket") from None
        mine = self._session_name(c.cookie_value)
        if want["kind"] == "open" and mine is not None and mine["user_id"] != want["user"] and not replace:
            raise ApiError("other_user")  # never replace another person's session silently (login CSRF)
        try:
            r = sw.redeem(b["t"], "to_hub")
        except SwitchError:
            raise ApiError("bad_ticket") from None
        clear = f"{self._cookie_name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        secure = "; Secure" if self.d.secure_cookies else ""
        if r["kind"] == "signout":
            if c.cookie_value:
                self.d.channels.sign_out(c.cookie_value)
            return 200, {"user_id": None, "return_to": "/", "kind": "signout"}, [("Set-Cookie", clear + secure)]
        if mine is not None and c.cookie_value:
            self.d.channels.sign_out(c.cookie_value)
        try:
            s = self.d.channels.open_session(r["user"])
        except ChannelError:
            raise ApiError("bad_ticket") from None
        ttl = max(0, (s["expires_ms"] - self.d.now_ms()) // 1000)
        attrs = f"{self._cookie_name}={s['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ttl}"
        out = {"user_id": s["user_id"], "return_to": clean_return_to(r["rt"]), "kind": "open"}
        return 200, out, [("Set-Cookie", attrs + secure)]

    def _h_rail(self, c: Ctx, p: dict, q: dict, b: dict | None):
        sw = self._switch()
        rows = sw.rail_rows(self.d.registry, self.d.access, c.user_id)
        for row in rows:
            row["last_seen"] = self._last_seen(row["workspace_id"])
        return (
            200,
            {
                "hub": {"name": sw.hub_name, "origin": sw.hub_origin()},
                "workspaces": rows,
            },
            [],
        )

    # --- (b) person home ---------------------------------------------------------------------------------------------

    def _h_my_workspaces(self, c: Ctx, p: dict, q: dict, b: dict | None):
        rows = []
        for w in self._workspaces(c.user_id):
            st = workspace_status(self.d.registry, w["workspace_id"])
            # Private telemetry belongs to hub admins and this workspace's owner, never phone sessions.
            private = c.scope != "channel" and (
                self.d.access.is_hub_admin(c.user_id)
                or w["role"] == "owner"
                or self.d.registry.owner_alias(w["workspace_id"]) == c.user_id
            )
            if not private:
                st = {k: v for k, v in st.items() if k not in ("host", "address", "last_seen_ms")}
            rows.append({**w, **st})
        return 200, {
            "workspaces": rows,
            "hub": {"version": hub_version, "min_supported": MIN_SUPPORTED_VERSION},
        }

    def _h_workspace_roles(self, c: Ctx, p: dict, q: dict, b: dict | None):
        ws = p["workspace_id"]
        row = next((w for w in self._workspaces(c.user_id) if w["workspace_id"] == ws), None)
        if row is None or row["role"] not in ("admin", "owner"):
            raise ApiError("unknown_workspace")
        names = {u["user_id"]: P4.render_text(u["name"]) for u in self.d.access.users()}
        owner = names.get(self.d.registry.owner_alias(ws))
        groups = []
        if row["mode"] == "full":
            for grant in self.d.access.grants(ws):
                groups.append(
                    {
                        "group": grant["group"],
                        "role": grant["role"],
                        "members": [names[u] for u in self.d.access.members(grant["group"]) if u in names],
                    }
                )
        return 200, {"owner": owner, "groups": groups, "profile": self.d.registry.profile(ws)}

    @staticmethod
    def _shared_signable(o: dict[str, Any]) -> bool:
        """Issue #57 / protocol §8.6: a shared item is signable only when it is a ticket-log item (not a needs_you
        summary) whose verified ``f1`` names exactly one target (one gate or one question), the one ``build_selective``
        can build. Display data for the phone; the hub build and the host stay the authority. Fail closed."""
        f1, chain = o.get("f1"), o.get("chain")
        if o.get("item_kind") == "needs_you" or type(f1) is not dict or type(chain) is not dict:
            return False
        if chain.get("log") == "workspace":
            return False
        gates, qs = f1.get("gates"), f1.get("questions")
        return type(gates) is dict and type(qs) is dict and len(gates) + len(qs) == 1

    def _shared_row(self, ws: dict[str, Any], it: dict[str, Any]) -> dict[str, Any]:
        o = it["item"]
        content = o["content"]
        return _render(
            {
                "workspace_id": ws["workspace_id"],
                "mode": "selective",
                "item_id": o["item_id"],
                "item_version": o["item_version"],
                "kind": o["item_kind"],
                "type": content.get("type"),
                "key": content.get("key"),
                "title": content.get("title"),
                "deadline": content.get("deadline"),
                "summary_only": o["item_kind"] == "needs_you",
                "chain": o.get("chain"),  # §10.2b, as verified by D9 (display data; the hub build is the authority)
                "f1": o.get("f1"),
                "signable": self._shared_signable(o),
            }
        )

    def _h_needs_you(self, c: Ctx, p: dict, q: dict, b: dict | None):
        items: list[dict[str, Any]] = []
        offline, unavailable, queued = [], [], []
        for ws in self._workspaces(c.user_id):
            w = ws["workspace_id"]
            if self.d.delivery is not None:
                queued.extend(
                    {"workspace_id": w, "type": row["type"], "queued_ms": row["enq_ms"]}
                    for row in self.d.delivery.pending(w)
                    if row["user_id"] == c.user_id
                )
            if not ws["online"]:
                offline.append(w)
            elif ws["mode"] == "selective":
                if self.d.shares is None:
                    unavailable.append(w)
                else:
                    items += [self._shared_row(ws, it) for it in self.d.shares.list_shared(c.user_id, w)]
            elif self.d.view is None:
                unavailable.append(w)
            else:
                for r in self.d.view.needs_you(w, c.user_id):
                    items.append(self._view_row(ws, r))
        return 200, {"items": items, "offline": offline, "unavailable": unavailable, "queued": queued}

    @staticmethod
    def _view_row(ws: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
        keep = {k: r.get(k) for k in ("item_id", "kind", "type", "key", "title", "deadline", "log", "hash", "gate")}
        return _render({"workspace_id": ws["workspace_id"], "mode": "full", **keep, "signable": ws["can_start"]})

    def _h_item(self, c: Ctx, p: dict, q: dict, b: dict | None):
        ws = self._mine(c.user_id, p["workspace_id"])
        if not ws["online"]:
            raise ApiError("not_bound")
        if ws["mode"] == "selective":
            if self.d.shares is None:
                raise ApiError("unavailable")
            for it in self.d.shares.list_shared(c.user_id, ws["workspace_id"]):
                if it["item"]["item_id"] == p["item_id"]:
                    return 200, {
                        **self._shared_row(ws, it),
                        "content": _render(it["item"]["content"]),
                        "item_hash": it["hash"],
                    }
            raise ApiError("unknown_item")
        if self.d.view is None:
            raise ApiError("unavailable")
        got = self.d.view.item(ws["workspace_id"], c.user_id, p["item_id"])
        if got is None:
            raise ApiError("unknown_item")
        return 200, {**self._view_row(ws, got), "content": _item_content(got.get("content", {}))}

    # --- (c) signing -------------------------------------------------------------------------------------------------

    def _engine(self) -> Any:
        if self.d.engine is None:
            raise ApiError("unavailable")
        return self.d.engine

    def _shown(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """What to show next to the signature prompt: every field of the event (WYSIWYS), strings escaped. The two
        certificates are not shown as text (the channel verifies the digest over the whole ``ctx``)."""
        ev = {k: v for k, v in ctx["event"].items() if k not in ("channel_cert", "uk_cert")}
        return _render(
            {
                "type": ev.get("type"),
                "workspace_id": ctx["workspace_id"],
                "log": ctx["log"],
                "fields": ev,
                "facts": _facts(ev),
            }
        )

    def _h_prepare(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, workspace_id=str, channel_id=str, action=dict)
        self._limit.hit("prepare", c.user_id, PREPARE_LIMIT)
        ws, ch = _hex32(b["workspace_id"]), _hex32(b["channel_id"])
        if c.scope == "channel" and ch != c.channel_id:
            raise ApiError("other_user")  # a channel session prepares for its own channel only
        self._mine(c.user_id, ws)
        pr = self._engine().prepare(c.token, ws, c.user_id, ch, b["action"])
        with self._plock:
            now = self.d.now_ms()
            for k in [k for k, v in self._prepared.items() if v[4] <= now]:
                del self._prepared[k]
            if len(self._prepared) >= MAX_PREPARED:
                raise ApiError("unavailable")
            self._prepared[pr.prepared_id] = (c.user_id, ch, ws, pr.log, pr.expires_ms, pr)
        return 200, {**pr.public(), "shown": self._shown(pr.ctx)}

    def _h_prepared(self, c: Ctx, p: dict, q: dict, b: dict | None):
        """The same answer as ``prepare`` for one request this user prepared (a push carries only the id). Read-only:
        it does not consume the request. Another user's id and an unknown id are both ``unknown_prepared``."""
        with self._plock:
            mine = self._prepared.get(p["prepared_id"])
        if mine is None or mine[0] != c.user_id or (c.scope == "channel" and mine[1] != c.channel_id):
            raise ApiError("unknown_prepared")
        if mine[4] <= self.d.now_ms():
            raise ApiError("prepared_expired")
        self._mine(c.user_id, mine[2])
        return 200, {**mine[5].public(), "shown": self._shown(mine[5].ctx)}

    def _h_sign(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, prepared_id=str, channel_id=str, channel_proof=str)
        self._limit.hit("sign", c.user_id, SIGN_LIMIT)
        with self._plock:  # single use here as in the engine: the first attempt consumes it
            mine = self._prepared.pop(b["prepared_id"], None)
        if mine is None or mine[0] != c.user_id:
            raise ApiError("unknown_prepared")  # another user's prepared id looks like an unknown one
        if mine[1] != b["channel_id"] or (c.scope == "channel" and b["channel_id"] != c.channel_id):
            raise ApiError("other_user")
        if not SessionBridge(self.d.channels).valid(c.token, c.user_id, mine[1]):
            raise ApiError("channel_revoked")
        self._mine(c.user_id, mine[2])
        ev = self._engine().sign(b["prepared_id"], b["channel_id"], b["channel_proof"])
        return 200, {"ok": True, "event_id": ev["id"], "type": ev["type"], "workspace_id": mine[2], "log": mine[3]}

    # --- (e) channel registration ------------------------------------------------------------------------------------

    def _h_reg_begin(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, kind=str, key_pub=str)
        if c.scope == "pairing" and b["kind"] != "phone":
            raise ApiError("forbidden_role")  # a pairing session registers a phone, nothing else
        label = b.get("label", "")
        try:
            key = unb64u(b["key_pub"], 65)
        except CanonError, TypeError:
            raise ApiError("malformed") from None
        if type(label) is not str:
            raise ApiError("malformed")
        if b["kind"] == "browser":
            # A8-3: a browser key is minted only by someone who knows the password and a fresh TOTP code, so a stolen
            # cookie or a script on the page cannot register a key it holds
            pw, totp = b.get("password"), b.get("totp")
            if type(pw) is not str or type(totp) is not str:
                raise ApiError("malformed")
            self._guarded_credentials(c, c.user_id, lambda: self.d.channels.reauth(c.user_id, pw, totp, c.source))
            return 200, self.d.channels.begin_registration(c.token, key, label)
        if b["kind"] == "phone":
            code = b.get("pairing_code")
            if type(code) is not str:
                raise ApiError("malformed")
            return 200, self.d.channels.begin_phone_registration(c.user_id, code, key, label, token=c.token)
        raise ApiError("malformed")

    def _h_reg_complete(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, challenge_id=str, signed=dict)
        return 200, {"channel_cert": self.d.channels.complete_registration(b["challenge_id"], b["signed"])}

    def _h_my_channels(self, c: Ctx, p: dict, q: dict, b: dict | None):
        return 200, {"channels": self.d.channels.channels(c.user_id)}

    def _h_revoke(self, c: Ctx, p: dict, q: dict, b: dict | None):
        if c.scope == "channel" and p["channel_id"] != c.channel_id:
            raise ApiError("other_user")  # a channel session revokes its own channel only
        self.d.channels.revoke_as(c.token, p["channel_id"])  # the channel's owner, or a hub-admin (checked there)
        if self.d.activity is not None:
            owner = next(
                (row["user_id"] for row in self.d.channels.channels() if row["channel_id"] == p["channel_id"]), None
            )
            self.d.activity.record(
                "channel.revoked", actor_id=c.user_id, subject_user=owner, detail={"channel_id": p["channel_id"]}
            )
        return 200, {"ok": True}

    # --- (d) admin console (route auth = hub-admin, checked in the pipeline) -----------------------------------------

    def _h_a_users(self, c: Ctx, p: dict, q: dict, b: dict | None):
        return 200, {"users": self.d.access.users()}

    def _h_a_user_add(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, name=str)
        check_name(b["name"])  # before any key is made: a refused name must leave no orphan UK behind
        uid = self.d.keys.issue_uk(None)["o"]["user_id"]
        try:
            self.d.access.add_user(uid, b["name"])
        except AccessError:  # names are not unique, so only a store error gets here: leave no orphan UK
            for f in (self.d.keys.state / "uk" / f"{uid}.key", self.d.keys._gen_dir() / "uk" / f"{uid}.json"):
                f.unlink(missing_ok=True)
            raise
        return 200, {"user_id": uid}

    def _h_a_user_remove(self, c: Ctx, p: dict, q: dict, b: dict | None):
        with self._chg().reducing("user_remove", confirm=confirmed(b), user_id=p["user_id"]):
            self.d.channels.remove_user(p["user_id"], actor=c.user_id)  # revokes and purges the user's channels too
        return 200, {"ok": True}

    def _h_a_groups(self, c: Ctx, p: dict, q: dict, b: dict | None):
        return 200, {"groups": self.d.access.groups()}

    def _h_a_group_add(self, c: Ctx, p: dict, q: dict, b: dict | None):
        self.d.access.add_group(_need(b, name=str)["name"])
        return 200, {"ok": True}

    def _h_a_group_remove(self, c: Ctx, p: dict, q: dict, b: dict | None):
        with self._chg().reducing("group_remove", confirm=confirmed(b), group=p["group"]):
            self.d.access.remove_group(p["group"], actor=c.user_id)
        return 200, {"ok": True}

    def _h_a_member_add(self, c: Ctx, p: dict, q: dict, b: dict | None):
        # an increase: a pending change (202) when a workspace owner must approve, else applied (changes/rules.py)
        return self._increase_answer(self._chg().member_add(c.user_id, p["group"], p["user_id"]))

    def _h_a_member_remove(self, c: Ctx, p: dict, q: dict, b: dict | None):
        with self._chg().reducing("member_remove", confirm=confirmed(b), group=p["group"], user_id=p["user_id"]):
            self.d.access.remove_member(p["group"], p["user_id"], actor=c.user_id)
        return 200, {"ok": True}

    def _h_a_grants(self, c: Ctx, p: dict, q: dict, b: dict | None):
        ws = q.get("workspace_id")
        return 200, {"grants": self.d.access.grants(_hex32(ws) if ws is not None else None)}

    def _h_a_grant(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, workspace_id=str, group=str, role=str)
        return self._increase_answer(
            self._chg().grant(c.user_id, b["workspace_id"], b["group"], b["role"], confirm=confirmed(b))
        )

    def _h_a_grant_revoke(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, workspace_id=str, group=str)
        with self._chg().reducing("revoke", confirm=confirmed(b), workspace_id=b["workspace_id"], group=b["group"]):
            self.d.access.revoke(b["workspace_id"], b["group"], actor=c.user_id)
        return 200, {"ok": True}

    def _h_a_workspace_remove(self, c: Ctx, p: dict, q: dict, b: dict | None):
        reg, ws = self.d.registry, p["workspace_id"]
        st = reg.binding_state(ws)
        if st is None:
            raise ApiError("unknown_workspace")
        if st["status"] != "unbound":
            raise ApiError("not_left")
        for grant in self.d.access.grants(ws):
            self.d.access.revoke(ws, grant["group"], actor=c.user_id)
        reg.forget(ws)
        return 200, {"ok": True}

    def _h_a_workspaces(self, c: Ctx, p: dict, q: dict, b: dict | None):
        reg, out = self.d.registry, []
        for ws in sorted(reg.pins()):
            st = reg.binding_state(ws)
            card_name = (reg.card(ws) or {}).get("name")
            out.append(
                {
                    "workspace_id": ws,
                    "name": P4.render_text(card_name) if type(card_name) is str and card_name else None,
                    "mode": reg.last_mode(ws),
                    "status": st["status"] if st else None,
                    "left_ms": reg.unbound_ms(ws) if st and st["status"] == "unbound" else None,
                    **workspace_status(reg, ws),
                    "owner_alias": reg.owner_alias(ws),
                    "grants": self.d.access.grants(ws),
                }
            )
        return 200, {"workspaces": out}

    def _h_a_services(self, c: Ctx, p: dict, q: dict, b: dict | None):
        if self.d.services is None:
            raise ApiError("unavailable")
        reg = self.d.registry
        states = [reg.binding_state(w) for w in reg.pins()]
        joined = [st for st in states if st is not None and st["status"] in ("bound", "unbound")]
        online = sum(1 for st in joined if st["status"] == "bound")
        return 200, {**self.d.services.snapshot(), "workspaces": {"count": len(joined), "online": online}}

    def _h_my_handoffs(self, c: Ctx, p: dict, q: dict, b: dict | None):
        mine = {w["workspace_id"]: w for w in self._workspaces(c.user_id)}
        rows = self.d.routes.recent(set(mine)) if self.d.routes is not None else []

        def end(ws: str) -> dict[str, Any]:  # a workspace the user has no part in is named only as "another workspace"
            w = mine.get(ws)
            return {
                "workspace_id": ws if w else None,
                "name": w["name"] if w else None,
                "online": bool(w and w["online"]),
            }

        return 200, {
            "handoffs": [
                {
                    "id": r["id"],
                    "from": end(r["from_ws"]),
                    "to": end(r["to_ws"]),
                    "sent_ms": r["sent_ms"],
                    "status": r["status"],
                }
                for r in rows
            ]
        }

    def _h_a_channels(self, c: Ctx, p: dict, q: dict, b: dict | None):
        u = q.get("user_id")
        return 200, {"channels": self.d.channels.channels(_hex32(u) if u is not None else None)}

    def _h_a_market_policy(self, c: Ctx, p: dict, q: dict, b: dict | None):
        return 200, {**self.d.access.market_policy(), "official_url": MP.OFFICIAL_URL}  # display: the one official URL

    def _h_a_market_policy_set(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, base_version=int, policy=dict)
        return 200, self.d.access.set_market_policy(b["base_version"], b["policy"], actor=c.user_id)

    def _h_a_connect(self, c: Ctx, p: dict, q: dict, b: dict | None):
        """Mint a single-use invite (30 min) and build ``<base>/c#h1.<hub_id>.<code>[.<endpoint>]`` (docs/connect-link.md).
        Body (all optional): ``endpoint`` = the host:port a workspace dials, default the hub's own link address."""
        if self.d.invites is None:
            raise ApiError("unavailable")
        b = b or {}
        endpoint = b.get("endpoint")
        if endpoint is not None and type(endpoint) is not str:
            raise ApiError("malformed")
        endpoint = (endpoint or "").strip() or (self.d.link_endpoint() if self.d.link_endpoint else None)
        # the advertised address first (works from other computers); the console's own origin only as a fallback
        base = self.d.api_origin or c.origin
        if base is None:
            raise ApiError("unavailable")
        # the workspace's owner on the hub: the admin who mints the link, or the user they pick (V3-D19)
        owner = b.get("owner_user", c.user_id)
        if type(owner) is not str or not any(u["user_id"] == owner for u in self.d.access.users()):
            raise ApiError("malformed")
        code, expires = self.d.invites.mint(self.d.now_ms(), owner_user=owner)
        try:
            link = build_link(base, self.d.keys.hub_id, code, endpoint, owner)
        except LinkError:
            raise ApiError("malformed") from None
        return 200, {
            "link": link,
            "code": code,
            "hub_id": self.d.keys.hub_id,
            "endpoint": endpoint,
            "owner_user": owner,
            "expires_ms": expires,
        }

    def _h_a_activity(self, c: Ctx, p: dict, q: dict, b: dict | None):
        try:
            return 200, activity_feed(self.d, q)
        except ValueError:
            raise ApiError("malformed") from None

    def _h_a_audit(self, c: Ctx, p: dict, q: dict, b: dict | None):
        since = q.get("since", "0")
        if not since.isascii() or not since.isdigit() or len(since) > 15:
            raise ApiError("malformed")
        store = self.d.audit.store
        rows = store.after(int(since), 100)
        head = store.head()
        return 200, {
            "records": [{"seq": s, "record": rec, "event": ev} for s, rec, ev in rows],
            "head": None if head is None else {"seq": head["seq"], "hash": head["hash"]},
        }
