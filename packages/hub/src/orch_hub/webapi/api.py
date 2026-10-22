"""The hub web API: routes, session and CSRF rules, role checks on the hub's own data (V3-D13, orch-core#893).

Rules every request goes through, in this order (``WebApi.handle``):

1. the method and path must match a route in :data:`ROUTES` (else ``unknown_op``);
2. the ``Host`` header must be one of the configured hosts (DNS rebinding) when hosts are configured;
3. a state-changing method (anything but GET) needs the non-simple header ``X-Orch-Csrf: 1``, a JSON content type when
   there is a body, and, when an ``Origin`` header is present, an origin on the allow list. A request that authenticates
   with the session cookie (an ambient credential) MUST carry an allowed ``Origin``; a request that carries its token
   explicitly (``Authorization: Bearer``, the phone) has nothing ambient to forge. Refusal: ``not_allowed``;
4. the session: the token (cookie or bearer) must be a live ``Channels`` session of a user the access model still has
   (else ``no_session``); admin routes then need ``Access.is_hub_admin`` (else ``forbidden_role``);
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

import hashlib
import ipaddress
import json
import re
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..access import Access, AccessError, role_rank
from ..access.store import check_name
from ..channels import Channels
from ..channels.service import ChannelError
from ..contract import phase4 as P4
from ..contract.canon import CanonError, loads_strict, unb64u
from ..contract.hub import Refused
from .static import StaticSite

MAX_BODY = 64 * 1024
COOKIE = "orch_hub_session"
CSRF_HEADER = "x-orch-csrf"
PREPARE_LIMIT = (10, 60_000)  # per user: 10 prepares a minute (orch-hub#25 item 6)
SIGN_LIMIT = (20, 60_000)
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
    "stale_snapshot": 409,
    "unknown_snapshot": 409,
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
            return (
                self._ch.session_user(session) == user_id and self._ch.active_channel(user_id, channel_id) is not None
            )
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
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000)


@dataclass(frozen=True)
class Route:
    method: str
    path: str  # template, ``{name}`` = one path segment
    auth: str  # none | session | admin
    purpose: str
    handler: str
    rx: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        pat = re.sub(r"\{(\w+)\}", lambda m: f"(?P<{m[1]}>{_ID if m[1].endswith('id') else _GRP})", self.path)
        object.__setattr__(self, "rx", re.compile(pat))


def _r(method: str, path: str, auth: str, handler: str, purpose: str) -> Route:
    return Route(method, "/api/v1" + path, auth, purpose, handler)


ROUTES: tuple[Route, ...] = (
    # (a) session
    _r("POST", "/session", "none", "sign_in", "Sign in with user id, password and TOTP; sets the session cookie"),
    _r("GET", "/session", "session", "whoami", "Who am I: user id, name, hub-admin flag"),
    _r("DELETE", "/session", "session", "sign_out", "Sign out (the session dies at once)"),
    # (b) person home
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
    _r("GET", "/admin/workspaces", "admin", "a_workspaces", "List workspaces with mode, binding status and grants"),
    _r("GET", "/admin/channels", "admin", "a_channels", "List every channel (optional ?user_id=)"),
    _r("POST", "/admin/channels/{channel_id}/revoke", "admin", "revoke", "Revoke any channel (permanent)"),
    _r("GET", "/admin/audit", "admin", "a_audit", "Read the audit chain (?since=N, up to 100 records)"),
)
_BY_METHOD: dict[str, list[Route]] = {}
for _rt in ROUTES:
    _BY_METHOD.setdefault(_rt.method, []).append(_rt)


@dataclass
class Ctx:
    user_id: str = ""
    token: str = ""
    cookie: bool = False
    origin: str | None = None
    source: str = ""


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


class WebApi:
    def __init__(self, deps: WebDeps) -> None:
        if deps.behind_edge and not deps.trusted_peers:
            raise ValueError("web API behind the edge needs trusted_peers (the edge's addresses)")
        self.d = deps
        self._limit = _Limiter(deps.now_ms)
        self._prepared: dict[
            str, tuple[str, str, str, str, int, Any]
        ] = {}  # id -> user, channel, workspace, log, expires, Prepared
        self._plock = threading.Lock()
        self._kinds: dict[str, tuple[str, int]] = {}  # sha256(token) -> (client kind, expires_ms), this process only
        self._hashing = threading.BoundedSemaphore(SIGNIN_CONCURRENCY)
        self._busy: set[str] = set()  # sources holding a hashing slot right now (max 1 each), under _plock
        self._hosts = frozenset(urllib.parse.urlsplit(o).netloc for o in deps.allowed_origins)
        self._site = StaticSite(hsts=deps.secure_cookies)

    def allow_origin(self, origin: str) -> None:
        """Add one exact origin before the server starts (the loopback dev server learns its port only after it
        binds). Origins and the Hosts derived from them are replaced as whole values, never edited in place."""
        self.d.allowed_origins = self.d.allowed_origins | {origin}
        self._hosts = frozenset(urllib.parse.urlsplit(o).netloc for o in self.d.allowed_origins)

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
        except (ChannelError, AccessError, Refused) as e:
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
        ctx = Ctx(origin=headers.get("origin"), source=source)
        if route.auth != "none":
            ctx.token, ctx.cookie = (bearer, False) if bearer else (cookie_token, True)
            if not ctx.token:
                raise ApiError("no_session")
            ctx.user_id = self.d.channels.session_user(ctx.token)
            # a session is for the carrier it was issued for (cookie = browser, bearer = app); one this process did not
            # issue (e.g. from before a restart) is refused and the person signs in again
            if self._kinds.get(hashlib.sha256(ctx.token.encode()).hexdigest(), ("", 0))[0] != (
                "browser" if ctx.cookie else "app"
            ):
                raise ApiError("no_session")
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
        """Workspaces the user has a part in: full mode by the role the access model gives; selective by being the
        admin-aliased owner (spec §8a: hub groups and hub-admins get no role there)."""
        reg, acc, out = self.d.registry, self.d.access, []
        for ws in sorted(reg.pins()):
            st = reg.binding_state(ws)
            mode = reg.last_mode(ws)
            if st is None or mode not in ("full", "selective"):
                continue
            if mode == "selective":
                if reg.owner_alias(ws) != user:
                    continue
                role = "owner"
            else:
                role = acc.role_of(user, ws)
                if role is None:
                    continue
            card = reg.card(ws) or {}
            name = card.get("name") if type(card.get("name")) is str else None
            out.append(
                {
                    "workspace_id": ws,
                    "name": P4.render_text(name) if name else None,
                    "mode": mode,
                    "role": role,
                    "online": st["status"] == "bound",
                    "hub_reads": "everything" if mode == "full" else "shared items and summaries",
                    "can_start": mode == "full" and role_rank(role) >= role_rank("edit"),
                }
            )
        return out

    def _mine(self, user: str, ws: str) -> dict[str, Any]:
        for w in self._workspaces(user):
            if w["workspace_id"] == ws:
                return w
        raise ApiError("unknown_workspace")  # not a member and not existing look the same

    # --- (a) session -------------------------------------------------------------------------------------------------

    def _guarded_credentials(self, c: Ctx, user_id: str, check: Callable[[], Any]) -> Any:
        """Every password + TOTP check (sign-in and the browser registration re-entry) goes through these caps."""
        # caps BEFORE any hashing: per source, and the sign-ins hashing right now (never queue: refuse)
        self._limit.hit("signin-src", c.source, SIGNIN_SOURCE_LIMIT)
        # the aggregate (IPv6 /48, IPv4 /24): a neighbour cannot exhaust our bucket, one allocation cannot spray; a
        # source that signed in as this user is exempt (cheap indexed read, no hashing), so neighbours cannot lock it out
        if not self.d.channels.known_source(user_id, c.source):
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

    def _h_sign_in(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, user_id=str, password=str, totp=str)
        client = b.get("client", "browser")
        if client not in ("browser", "app"):
            raise ApiError("malformed")
        if client == "browser" and c.origin is None:  # a cookie is only ever set for a request the browser labelled
            raise ApiError("not_allowed")
        s = self._guarded_credentials(
            c, b["user_id"], lambda: self.d.channels.sign_in(b["user_id"], b["password"], b["totp"], c.source)
        )
        out: dict[str, Any] = {"user_id": s["user_id"], "expires_ms": s["expires_ms"]}
        with self._plock:
            now = self.d.now_ms()
            if len(self._kinds) > 4096:
                self._kinds = {k: v for k, v in self._kinds.items() if v[1] > now}
            self._kinds[hashlib.sha256(s["token"].encode()).hexdigest()] = (client, s["expires_ms"])
        if client == "app":  # the phone keeps the token itself and sends it as a bearer
            out["token"] = s["token"]
            return 200, out, []
        ttl = max(0, (s["expires_ms"] - self.d.now_ms()) // 1000)
        attrs = f"{self._cookie_name}={s['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ttl}"
        return 200, out, [("Set-Cookie", attrs + ("; Secure" if self.d.secure_cookies else ""))]

    def _h_whoami(self, c: Ctx, p: dict, q: dict, b: dict | None):
        name = next((u["name"] for u in self.d.access.users() if u["user_id"] == c.user_id), None)
        return 200, {"user_id": c.user_id, "name": name, "is_admin": self.d.access.is_hub_admin(c.user_id)}

    def _h_sign_out(self, c: Ctx, p: dict, q: dict, b: dict | None):
        self.d.channels.sign_out(c.token)
        clear = f"{self._cookie_name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        return 200, {"ok": True}, [("Set-Cookie", clear + ("; Secure" if self.d.secure_cookies else ""))]

    # --- (b) person home ---------------------------------------------------------------------------------------------

    def _h_my_workspaces(self, c: Ctx, p: dict, q: dict, b: dict | None):
        return 200, {"workspaces": self._workspaces(c.user_id)}

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
                "signable": False,  # selective custodial signing over shared items is C6, not built (engine: wrong_mode)
            }
        )

    def _h_needs_you(self, c: Ctx, p: dict, q: dict, b: dict | None):
        items: list[dict[str, Any]] = []
        offline, unavailable = [], []
        for ws in self._workspaces(c.user_id):
            w = ws["workspace_id"]
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
        return 200, {"items": items, "offline": offline, "unavailable": unavailable}

    @staticmethod
    def _view_row(ws: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
        keep = {k: r.get(k) for k in ("item_id", "kind", "type", "key", "title", "deadline", "log", "hash")}
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
        return 200, {**self._view_row(ws, got), "content": _render(got.get("content", {}))}

    # --- (c) signing -------------------------------------------------------------------------------------------------

    def _engine(self) -> Any:
        if self.d.engine is None:
            raise ApiError("unavailable")
        return self.d.engine

    def _shown(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """What to show next to the signature prompt: every field of the event (WYSIWYS), strings escaped. The two
        certificates are not shown as text (the channel verifies the digest over the whole ``ctx``)."""
        ev = {k: v for k, v in ctx["event"].items() if k not in ("channel_cert", "uk_cert")}
        return _render({"type": ev.get("type"), "workspace_id": ctx["workspace_id"], "log": ctx["log"], "fields": ev})

    def _h_prepare(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, workspace_id=str, channel_id=str, action=dict)
        self._limit.hit("prepare", c.user_id, PREPARE_LIMIT)
        ws, ch = _hex32(b["workspace_id"]), _hex32(b["channel_id"])
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
        if mine is None or mine[0] != c.user_id:
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
        if mine[1] != b["channel_id"]:
            raise ApiError("other_user")
        if not SessionBridge(self.d.channels).valid(c.token, c.user_id, mine[1]):
            raise ApiError("channel_revoked")
        self._mine(c.user_id, mine[2])
        ev = self._engine().sign(b["prepared_id"], b["channel_id"], b["channel_proof"])
        return 200, {"ok": True, "event_id": ev["id"], "type": ev["type"], "workspace_id": mine[2], "log": mine[3]}

    # --- (e) channel registration ------------------------------------------------------------------------------------

    def _h_reg_begin(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, kind=str, key_pub=str)
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
        self.d.channels.revoke_as(c.token, p["channel_id"])  # the channel's owner, or a hub-admin (checked there)
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
        self.d.access.remove_user(p["user_id"], actor=c.user_id)
        return 200, {"ok": True}

    def _h_a_groups(self, c: Ctx, p: dict, q: dict, b: dict | None):
        return 200, {"groups": self.d.access.groups()}

    def _h_a_group_add(self, c: Ctx, p: dict, q: dict, b: dict | None):
        self.d.access.add_group(_need(b, name=str)["name"])
        return 200, {"ok": True}

    def _h_a_group_remove(self, c: Ctx, p: dict, q: dict, b: dict | None):
        self.d.access.remove_group(p["group"], actor=c.user_id)
        return 200, {"ok": True}

    def _h_a_member_add(self, c: Ctx, p: dict, q: dict, b: dict | None):
        self.d.access.add_member(p["group"], p["user_id"], actor=c.user_id)
        return 200, {"ok": True}

    def _h_a_member_remove(self, c: Ctx, p: dict, q: dict, b: dict | None):
        self.d.access.remove_member(p["group"], p["user_id"], actor=c.user_id)
        return 200, {"ok": True}

    def _h_a_grants(self, c: Ctx, p: dict, q: dict, b: dict | None):
        ws = q.get("workspace_id")
        return 200, {"grants": self.d.access.grants(_hex32(ws) if ws is not None else None)}

    def _h_a_grant(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, workspace_id=str, group=str, role=str)
        self.d.access.grant(b["workspace_id"], b["group"], b["role"], actor=c.user_id)
        return 200, {"ok": True}

    def _h_a_grant_revoke(self, c: Ctx, p: dict, q: dict, b: dict | None):
        b = _need(b, workspace_id=str, group=str)
        self.d.access.revoke(b["workspace_id"], b["group"], actor=c.user_id)
        return 200, {"ok": True}

    def _h_a_workspaces(self, c: Ctx, p: dict, q: dict, b: dict | None):
        reg, out = self.d.registry, []
        for ws in sorted(reg.pins()):
            st = reg.binding_state(ws)
            out.append(
                {
                    "workspace_id": ws,
                    "mode": reg.last_mode(ws),
                    "status": st["status"] if st else None,
                    "owner_alias": reg.owner_alias(ws),
                    "grants": self.d.access.grants(ws),
                }
            )
        return 200, {"workspaces": out}

    def _h_a_channels(self, c: Ctx, p: dict, q: dict, b: dict | None):
        u = q.get("user_id")
        return 200, {"channels": self.d.channels.channels(_hex32(u) if u is not None else None)}

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
